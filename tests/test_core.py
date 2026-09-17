import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from pairwise_console.commands import run_command
from pairwise_console.config import OLD_APP_DIR, load_config
from pairwise_console.db import Database, now_iso
from pairwise_console.gitops import GitOps
from pairwise_console.analytics import dashboard
from pairwise_console.artifact import isolated_compose_environment
from pairwise_console.api import Handler
from pairwise_console.exports import build_xlsx
from pairwise_console.importer import import_historical_tasks
from pairwise_console.prompts import gsb_prompt, gsb_recheck_prompt
from pairwise_console.recording import RecordingManager
from pairwise_console.service import PairwiseService


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(Path(__file__).resolve().parents[1])
        from dataclasses import replace
        self.config = replace(
            self.config,
            data_dir=self.root / "data",
            db_path=self.root / "data" / "test.db",
            projects_dir=self.root / "projects",
            old_db_path=self.root / "old.db",
            git_author_email="test@example.com",
        )
        self.db = Database(self.config.db_path)
        self.db.initialize()
        self.service = PairwiseService(self.config, self.db)

    def tearDown(self):
        self.service.executor.shutdown(wait=False, cancel_futures=True)
        self.service.monitor_executor.shutdown(wait=False, cancel_futures=True)
        self.temp.cleanup()

    def insert_ready_task(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-1", "test", "zero_to_one", "hard-project", "Build a hard project with Docker Compose",
             "困难", '["跨模块状态","异常恢复"]', "fingerprint-1", "ready", stamp, stamp),
        )

    def test_defaults_use_codex_for_review_and_claude_for_development(self):
        self.assertEqual(self.db.setting("codex_model"), "gpt-5.6-sol")
        self.assertEqual(self.db.setting("codex_default_effort"), "medium")
        self.assertEqual(self.db.setting("codex_bug_effort"), "high")
        self.assertEqual(self.db.setting("claude_model"), "auto_model/urm")
        self.assertEqual(self.db.setting("first_prompt_stop_minutes"), 40)
        self.assertEqual(self.db.setting("ab_prompt_stagger_seconds"), 30)

    def test_original_prompts_are_staggered_between_a_and_b(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, sent_at in (("A", stamp), ("B", None)):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pair["id"] + "-" + arm.lower(), pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 "developing" if sent_at else "running", sent_at, stamp, stamp),
            )
        b_arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm='B'", (pair["id"],))
        with patch("pairwise_console.service.time.sleep") as sleep, \
             patch.object(self.service.claude, "send_prompt") as send_prompt:
            self.service._send_prompt_with_pair_stagger(pair["id"], b_arm, "same prompt")
        self.assertEqual(send_prompt.call_count, 1)
        waited = sleep.call_args.args[0]
        self.assertGreaterEqual(waited, 30)
        self.assertLessEqual(waited, 31)

    def test_failed_ready_pair_cannot_be_revived_by_a_queued_start(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='ready_to_start' WHERE id=?",
            (pair["id"],),
        )
        with self.assertRaisesRegex(ValueError, "Pair 已停止"):
            self.service.start_pair(pair["id"])

    def test_completed_arm_is_scheduled_for_validation_before_peer_finishes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        for arm, status, sha in (("A", "completed", "a" * 40), ("B", "developing", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,trace_path,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-early-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", status,
                 str(self.root / "traces" / arm), sha, stamp, stamp),
            )
        with patch.object(self.service, "_inspect_trace", return_value=(Path("A.jsonl"), "2.1.269", [])), \
             patch.object(self.service, "_submit_auto", return_value=True) as submit:
            self.service._refresh_pair_after_arm(pair["id"])
        submit.assert_called_once_with(
            "artifact-%s-A-%s" % (pair["id"], "a" * 12),
            self.service._validate_completed_arm, pair["id"], "A",
        )
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "development",
        )

    def test_pair_records_only_after_both_current_commits_pass(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, status, sha in (("A", "completed", "a" * 40), ("B", "developing", "")):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("arm-gate-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", status,
                 sha, stamp, stamp),
            )

        def passed_check(pair_id, arm, workspace, commit_sha):
            check_id = "check-gate-" + arm
            self.db.execute(
                """INSERT OR REPLACE INTO artifact_checks
                   (id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                (check_id, pair_id, arm, commit_sha, stamp, stamp),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,))

        with patch.object(self.service.artifacts, "validate", side_effect=passed_check):
            self.service._validate_pair_artifacts(pair["id"], ["A"])
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "development",
        )
        self.db.execute(
            "UPDATE arm_runs SET status='completed',commit_sha=? WHERE pair_id=? AND arm='B'",
            ("b" * 40, pair["id"]),
        )
        with patch.object(self.service.artifacts, "validate", side_effect=passed_check), \
             patch.object(self.service, "_submit_auto", return_value=True):
            self.service._validate_pair_artifacts(pair["id"], ["B"])
        self.assertEqual(
            self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"],
            "difficulty_review",
        )

    def _prepare_pair_for_difficulty_review(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='difficulty_review' WHERE id=?",
            (pair["id"],),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,result,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?)""",
                ("arm-difficulty-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha,
                 "implemented and verified", stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?,?)""",
                ("check-difficulty-" + arm, pair["id"], arm, sha,
                 '[{"name":"verify_service","passed":true,"detail":"business scenarios passed"}]',
                 stamp, stamp),
            )
        return pair

    def test_actual_difficulty_review_passes_and_moves_to_recording(self):
        pair = self._prepare_pair_for_difficulty_review()
        result = {
            "aDifficulty": "困难", "bDifficulty": "地狱", "difficulty": "困难",
            "reason": "两侧都实现了跨模块状态恢复、并发一致性和异常链路，真实验收覆盖了关键边界。",
            "evidence": ["state.py 的事务恢复", "Docker verify_service 覆盖并发冲突"],
        }
        with patch.object(self.service.codex, "run", return_value=result):
            review = self.service.reassess_actual_difficulty(pair["id"])
        self.assertEqual(review["status"], "passed")
        self.assertEqual(review["assessed_difficulty"], "困难")
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "running", "stage": "recording"},
        )
        self.assertEqual(
            self.db.one("SELECT difficulty FROM tasks WHERE id='task-1'")["difficulty"],
            "困难",
        )

    def test_actual_difficulty_review_rejects_medium_and_discards_pair(self):
        pair = self._prepare_pair_for_difficulty_review()
        result = {
            "aDifficulty": "中等", "bDifficulty": "中等", "difficulty": "中等",
            "reason": "实际交付只沿现有结构增加局部数据流和输入校验，没有架构取舍或复杂状态链路。",
            "evidence": ["只改动局部处理函数", "Docker 验收仅覆盖常规输入校验"],
        }
        with patch.object(self.service.codex, "run", return_value=result):
            review = self.service.reassess_actual_difficulty(pair["id"])
        self.assertEqual(review["status"], "rejected")
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "failed", "stage": "difficulty_rejected"},
        )
        delivery = self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "discarded")
        self.assertIn("低于困难/地狱", delivery["error"])

    def test_full_monitor_capacity_does_not_starve_user_operations(self):
        release = threading.Event()
        started = threading.Event()
        start_lock = threading.Lock()
        start_count = 0

        def monitor():
            nonlocal start_count
            with start_lock:
                start_count += 1
                if start_count == 8:
                    started.set()
            release.wait(5)

        try:
            for index in range(8):
                self.service._submit_monitor("test-monitor-%d" % index, monitor)
            self.assertTrue(started.wait(2), "monitor pool did not reach full capacity")
            completed = threading.Event()
            self.service._submit("test-user-operation", completed.set)
            self.assertTrue(completed.wait(2), "user operation was starved by arm monitors")
        finally:
            release.set()

    def test_user_paths_and_github_credential_helper_are_portable(self):
        self.assertEqual(OLD_APP_DIR, Path.home() / "Library/Application Support/Claude Eval Console")
        with patch("pairwise_console.gitops.shutil.which", return_value="/usr/local/bin/gh"), \
                patch("pairwise_console.gitops.run_command") as command:
            GitOps._github_git(["ls-remote", "origin"])
        args = command.call_args.args[0]
        self.assertIn("credential.helper=!/usr/local/bin/gh auth git-credential", args)
        self.assertEqual(command.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_pair_requires_ready_hard_task_and_creates_chain(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.assertEqual(pair["status"], "queued")
        self.assertEqual(pair["stage"], "repository")
        chain = self.db.one("SELECT * FROM project_chains WHERE id=?", (pair["chain_id"],))
        self.assertEqual(chain["followup_required"], 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-1'")["status"], "used")

    def test_pair_parallelism_has_a_hard_ceiling_of_three(self):
        self.db.set_setting("max_pairs_parallel", 9)
        stamp = now_iso()
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-limit-{index}", "test", "zero_to_one", f"hard-{index}", "Build a hard project",
                 "困难", '["跨模块状态"]', f"fingerprint-limit-{index}", "ready", stamp, stamp),
            )
        for index in range(3):
            self.service.create_pair(f"task-limit-{index}")
        with self.assertRaisesRegex(ValueError, "最多 3 个 Pair"):
            self.service.create_pair("task-limit-3")

    def test_concurrent_pair_creation_cannot_exceed_three(self):
        stamp = now_iso()
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-race-{index}", "test", "zero_to_one", f"hard-race-{index}", "Build a hard project",
                 "困难", '["并发状态"]', f"fingerprint-race-{index}", "ready", stamp, stamp),
            )
        barrier = threading.Barrier(4)
        outcomes = []
        outcome_lock = threading.Lock()

        def create(index):
            barrier.wait()
            try:
                self.service.create_pair(f"task-race-{index}")
                outcome = "created"
            except ValueError as exc:
                outcome = str(exc)
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(outcomes.count("created"), 3)
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM pairs")["count"], 3)
        self.assertTrue(any("最多 3 个 Pair" in outcome for outcome in outcomes))

    def test_concurrent_repository_preparation_is_serialized_per_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        local_root = self.root / "repository-lock"
        local_root.mkdir()
        active = 0
        maximum_active = 0
        calls_lock = threading.Lock()

        def prepare_repo(*_args):
            nonlocal active, maximum_active
            with calls_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.08)
            with calls_lock:
                active -= 1
            return {"local_root": str(local_root), "status": "ready"}

        with patch.object(self.service.git, "create_pair_repository", side_effect=prepare_repo), \
             patch.object(self.service.claude, "prepare_arm"):
            threads = [threading.Thread(
                target=self.service.prepare_pair_repository, args=(pair["id"],),
            ) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(maximum_active, 1)

    def test_one_click_automation_is_persistent_and_forces_three_pair_target(self):
        self.db.set_setting("max_pairs_parallel", 1)
        with patch.object(self.service, "_schedule_auto_pipeline_once") as schedule:
            status = self.service.set_auto_pipeline(True)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["targetPairs"], 3)
        self.assertEqual(self.db.setting("max_pairs_parallel"), 3)
        schedule.assert_called_once_with()
        stopped = self.service.set_auto_pipeline(False)
        self.assertFalse(stopped["enabled"])

    def test_automation_consumes_existing_ready_tasks_before_refill(self):
        stamp = now_iso()
        for index in range(3):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-auto-{index}", "test", "zero_to_one", f"hard-auto-{index}",
                 "Build a hard project with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-auto-{index}", "ready", stamp, stamp),
            )
        submitted = []
        with patch.object(self.service, "_submit_auto", side_effect=lambda operation, fn, *args: submitted.append(operation) or True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["activePairs"], 3)
        self.assertEqual(status["readyTasks"], 0)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 3)
        self.assertEqual(len([item for item in submitted if item.startswith("repo-pair-")]), 3)
        refill.assert_not_called()

    def test_automation_refills_when_a_completed_pair_releases_a_slot(self):
        stamp = now_iso()
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-cycle-{index}", "test", "zero_to_one", f"hard-cycle-{index}",
                 "Build a hard project with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-cycle-{index}", "ready", stamp, stamp),
            )
        with patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            self.service._schedule_auto_pipeline_once()
        first = self.db.one("SELECT id FROM pairs ORDER BY created_at,id LIMIT 1")
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, first["id"]),
        )
        with patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once"):
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["activePairs"], 3)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 4)

    def test_gsb_confirmation_strips_backticks_and_completes_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO gsb_reviews(id,pair_id,verdict,reason,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gsb-1", pair["id"], "Same", "两边都完成了相同功能，但各有一些可以复核的实现差异。", "draft", stamp, stamp),
        )
        self.db.execute("UPDATE pairs SET stage='recording' WHERE id=?", (pair["id"],))
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-confirm-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-confirm-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,width,height,duration_seconds,status,created_at,updated_at)
                   VALUES(?,?,?,?,1280,720,30,'passed',?,?)""",
                ("rec-" + arm, pair["id"], arm, str(self.root / (arm + ".mov")), stamp, stamp),
            )
        result = self.service.confirm_gsb(
            pair["id"], "A better",
            "A 在 app/main.py 的 create 方法完成主要流程和异常路径，pytest 验收与录像结果稳定。",
            "B 在 app/main.py 的 create 方法完成主要流程，但 pytest 显示异常路径仍有可复现失败。",
            "刘昱",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(result["gsb"]["confirmed_by"], "刘昱")
        self.assertEqual(result["gsb"]["draft_verdict"], "Same")
        self.assertEqual(result["gsb"]["final_verdict"], "A better")
        self.assertEqual(result["delivery"]["status"], "ready_to_submit")

    def test_generated_gsb_self_corrects_missing_locators_and_is_default_confirmed(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='gsb_ready' WHERE id=?", (pair["id"],))
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-gsb-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""", ("check-gsb-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'passed',?,?)""", ("rec-gsb-" + arm, pair["id"], arm,
                    str(self.root / (arm + ".mp4")), sha, stamp, stamp),
            )
        result_payload = {
            "verdict": "A better",
            "aReason": "A 在 app/main.py 的 create 方法完成全部主要流程，pytest 覆盖异常路径与持久化结果，因此更倾向 A。",
            "bReason": "B 在 app/main.py 的 create 方法完成核心流程，但 pytest 显示异常恢复仍有可复现偏差，因此不优先。",
            "evidence": ["A Docker 通过", "B 异常路径失败"],
        }
        first_payload = dict(result_payload)
        first_payload.update({
            "aReason": "A 完成了全部主要流程，异常路径与持久化结果都有可见验收证据，因此更倾向 A。",
            "bReason": "B 完成了核心流程，但异常恢复仍有可复现偏差，因此相比 A 不优先。",
        })
        with patch.object(self.service.codex, "run", side_effect=[first_payload, result_payload]) as mocked_run:
            review = self.service.generate_gsb(pair["id"])
        self.assertEqual(mocked_run.call_count, 2)
        self.assertEqual(review["a_reason"], result_payload["aReason"])
        self.assertEqual(review["b_reason"], result_payload["bReason"])
        self.assertNotIn("preference_reason", review)
        self.assertNotIn("偏好依据：", review["reason"])
        self.assertEqual(review["status"], "confirmed")
        self.assertEqual(review["confirmed_by"], "刘昱（按授权默认确认）")
        completed = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(completed, {"status": "completed", "stage": "completed"})

    def test_gsb_recheck_persists_split_review_suggestions(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-recheck-source", pair["id"], "A better", "A：原 A 评价 B：原 B 评价",
             "原 A 评价有足够的具体事实与验收依据。", "原 B 评价说明了真实存在的交付差异。", stamp, stamp),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-recheck-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-recheck-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        payload = {
            "status": "suggested_revision",
            "suggestedVerdict": "A better",
            "suggestedAReason": "A 的 app/main.py 开发说明与 docker compose run verify 后续独立验收已明确区分。",
            "suggestedBReason": "B 的 app/main.py 接口偏差由 pytest 验证，评价写明了实际行为和客观后果。",
            "issues": ["原评价需要明确验收发生阶段。"],
            "evidenceRefs": ["checks[A]", "checks[B]"],
        }
        with patch.object(self.service.codex, "run", return_value=payload):
            result = self.service._recheck_gsb(pair["id"])
        self.assertEqual(result["result_status"], "suggested_revision")
        self.assertEqual(result["suggested_a_reason"], payload["suggestedAReason"])
        self.assertEqual(result["suggested_b_reason"], payload["suggestedBReason"])
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM gsb_rechecks")["count"], 1)
        batch = self.service.apply_latest_gsb_rechecks([pair["id"]])
        self.assertEqual(batch["applied"], 1)
        applied = self.db.one("SELECT * FROM gsb_rechecks WHERE id=?", (result["id"],))
        self.assertTrue(applied["applied_at"])
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(review["a_reason"], payload["suggestedAReason"])
        self.assertEqual(review["b_reason"], payload["suggestedBReason"])
        self.assertEqual(review["status"], "confirmed")

    def test_gsb_recheck_cannot_pass_reasons_without_evidence_locators(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-locator-source", pair["id"], "Same", "A：评价 A B：评价 B",
             "A 的结果完整，验收结果稳定。", "B 的结果完整，验收结果也稳定。", stamp, stamp),
        )
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-locator-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-locator-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        payload = {
            "status": "passed", "suggestedVerdict": "Same",
            "suggestedAReason": "A 完成了核心要求，后续验收结果稳定，没有发现影响交付的问题。",
            "suggestedBReason": "B 也完成了核心要求，后续验收结果相同，因此两边表现接近。",
            "issues": [], "evidenceRefs": ["checks[A]", "checks[B]"],
        }
        with patch.object(self.service.codex, "run", return_value=payload) as mocked_run:
            result = self.service._recheck_gsb(pair["id"])
        self.assertEqual(result["result_status"], "suggested_revision")
        self.assertIn("A 评价缺少可核对的具体证据", result["issues_json"])
        self.assertIn("B 评价缺少可核对的具体证据", result["issues_json"])
        self.assertEqual(mocked_run.call_count, 2)

    def test_gsb_recheck_rewrites_mechanical_case_lists_even_when_model_first_passes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        mechanical_a = (
            "A 在 rehearsal.spec.ts 第15、16、21、22项覆盖边界，"
            "83个单测、23个端到端测试和38秒录像通过，未见已发生的功能缺陷。"
        )
        mechanical_b = (
            "B 在 rehearsal.spec.ts 第15、17、19、21至24项覆盖边界，"
            "82个单测、25个端到端测试和38秒录像通过。"
        )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-mechanical", pair["id"], "Same",
             self.service._compose_gsb_reason(mechanical_a, mechanical_b),
             mechanical_a, mechanical_b, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
                ("arm-mechanical-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )
        first = {
            "status": "passed", "suggestedVerdict": "Same",
            "suggestedAReason": mechanical_a, "suggestedBReason": mechanical_b,
            "issues": [], "evidenceRefs": ["arms[A]", "arms[B]"],
        }
        revised_a = (
            "A 在 rehearsal.spec.ts 实际跑过边界两侧、写回和失效处理，"
            "这些流程都通过，功能链路完整。"
        )
        revised_b = (
            "B 在 rehearsal.spec.ts 也验证了平行边、非法输入和写回后重新考证，"
            "现有结果与 A 没有明显差距。"
        )
        second = {
            "status": "passed", "suggestedVerdict": "Same",
            "suggestedAReason": revised_a, "suggestedBReason": revised_b,
            "issues": [], "evidenceRefs": ["arms[A]", "arms[B]"],
        }
        with patch.object(self.service.codex, "run", side_effect=[first, second]) as mocked_run:
            result = self.service._recheck_gsb(pair["id"])
        self.assertEqual(mocked_run.call_count, 2)
        self.assertEqual(result["result_status"], "suggested_revision")
        self.assertEqual(result["suggested_a_reason"], revised_a)
        self.assertIn("机械罗列测试编号", result["issues_json"])

    def test_new_evidence_review_and_delivery_schema_is_available(self):
        recording_columns = {row["name"] for row in self.db.all("PRAGMA table_info(recordings)")}
        self.assertTrue({"commit_sha", "commit_match", "steps_json", "direct_url", "attempt_id", "capture_mode", "entry_url",
                         "review_status", "reviewed_by", "reviewed_at"} <= recording_columns)
        task_columns = {row["name"] for row in self.db.all("PRAGMA table_info(tasks)")}
        self.assertIn("project_category", task_columns)
        attempt_columns = {row["name"] for row in self.db.all("PRAGMA table_info(recording_attempts)")}
        self.assertIn("interaction_mode", attempt_columns)
        gsb_columns = {row["name"] for row in self.db.all("PRAGMA table_info(gsb_reviews)")}
        self.assertTrue({"draft_verdict", "final_verdict", "evidence_version", "a_reason", "b_reason", "preference_reason"} <= gsb_columns)
        recheck_columns = {row["name"] for row in self.db.all("PRAGMA table_info(gsb_rechecks)")}
        self.assertTrue({"applied_at", "applied_by"} <= recheck_columns)
        arm_columns = {row["name"] for row in self.db.all("PRAGMA table_info(arm_runs)")}
        self.assertTrue({"attempt_no", "error_retry_count"} <= arm_columns)
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='gsb_rechecks'"))
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_submissions'"))
        delivery_columns = {row["name"] for row in self.db.all("PRAGMA table_info(delivery_submissions)")}
        self.assertTrue({"payload_sha256", "remote_status", "qc_summary", "remote_updated_at"} <= delivery_columns)
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='recording_attempts'"))
        self.assertEqual(self.db.setting("gsb_recheck_model"), "gpt-6-astra")
        self.assertEqual(self.db.setting("gsb_recheck_effort"), "high")

    def test_browser_recording_attempt_is_promoted_only_after_it_passes(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = (arm.lower() * 40)[:40]
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-browser-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            attempt_id = "attempt-browser-" + arm
            self.db.execute(
                """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,entry_url,
                   width,height,duration_seconds,sha256,status,started_at,finished_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'browser','http://127.0.0.1:9000',1280,720,30,?,'passed',?,?,?,?)""",
                (attempt_id, pair["id"], arm, sha, str(self.root / (arm + ".webm")), arm * 64,
                 stamp, stamp, stamp, stamp),
            )
            self.service.recordings._promote(attempt_id)
        rows = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair["id"],))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "passed" and row["capture_mode"] == "browser" for row in rows))
        self.assertTrue(all(row["review_status"] == "confirmed" and row["reviewed_at"] for row in rows))
        updated = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(updated["stage"], "gsb_ready")

    def test_rerecording_same_commits_preserves_confirmed_gsb(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = (arm.lower() * 40)[:40]
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-rerecord-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,sha256,width,height,duration_seconds,
                   attempt_id,commit_match,review_status,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1280,720,30,?,1,'confirmed','passed',?,?)""",
                ("rec-old-" + arm, pair["id"], arm, str(self.root / (arm + "-old.mp4")), sha,
                 arm * 64, "attempt-old-" + arm, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,confirmed_by,
               confirmed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed','刘昱',?,?,?)""",
            ("gsb-rerecord", pair["id"], "Same", "A：A 完成要求。 B：B 完成要求。",
             "A 完成要求。", "B 完成要求。", stamp, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO gsb_rechecks(id,pair_id,evidence_version,input_verdict,input_reason,result_status,
               model,reasoning_effort,created_at) VALUES(?,?,?,?,?,'passed','gpt-6-astra','high',?)""",
            ("recheck-rerecord", pair["id"], "old-version", "Same", "原评价", stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'submitted',?,?)""",
            ("delivery-rerecord", pair["id"], stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',winner='Same',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,entry_url,
               width,height,duration_seconds,sha256,status,started_at,finished_at,created_at,updated_at)
               VALUES(?,?,?,?,?,'browser','http://127.0.0.1:9000',1280,720,42,?,'passed',?,?,?,?)""",
            ("attempt-new-A", pair["id"], "A", "a" * 40, str(self.root / "A-new.mp4"),
             "n" * 64, stamp, stamp, stamp, stamp),
        )

        self.service.recordings._promote("attempt-new-A")

        review = self.db.one("SELECT status,confirmed_by FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(review, {"status": "confirmed", "confirmed_by": "刘昱"})
        self.assertIsNotNone(self.db.one("SELECT id FROM gsb_rechecks WHERE id='recheck-rerecord'"))
        current = self.db.one("SELECT status,stage,winner FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "completed", "stage": "completed", "winner": "Same"})
        delivery = self.db.one("SELECT status FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "needs_review")

    def test_recording_stop_immediately_enters_saving_state(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        output = self.root / "manual.mp4"
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,status,created_at,updated_at)
               VALUES(? ,?,'A',?,?, 'recording',?,?)""",
            ("attempt-stop", pair["id"], "a" * 40, str(output), stamp, stamp),
        )
        process = MagicMock()
        process.poll.return_value = None
        self.service.recordings._processes["attempt-stop"] = process

        row = self.service.recordings.stop(pair["id"], "A")

        self.assertEqual(row["status"], "stopping")
        self.assertTrue(Path(str(output) + ".stop").is_file())
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='attempt-stop' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(event["event_type"], "recording.stop_requested")

    def test_service_restart_recovers_a_completed_stop_save(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,status,created_at,updated_at)
               VALUES(?,?,'B',?,?, 'stopping',?,?)""",
            ("attempt-recover", pair["id"], "b" * 40, str(self.root / "recovered.mp4"), stamp, stamp),
        )
        inspected = {
            "ok": True, "sha256": "c" * 64, "width": 1280, "height": 720,
            "duration_seconds": 32.5, "error": "",
        }
        with patch("pairwise_console.recording.inspect_recording", return_value=inspected), \
             patch.object(RecordingManager, "_promote") as promote:
            RecordingManager(self.config, self.db)

        recovered = self.db.one("SELECT * FROM recording_attempts WHERE id='attempt-recover'")
        self.assertEqual(recovered["status"], "passed")
        self.assertEqual(recovered["width"], 1280)
        promote.assert_called_once_with("attempt-recover")

    def test_delivery_preflight_allows_style_suggestion_but_blocks_fact_conflict(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = (arm.lower() * 40)[:40]
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,session_id,prompt_id,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?)""",
                ("arm-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm, "screen-" + arm,
                 "auto_model/urm", "image", "session-" + arm, "prompt-" + arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,sha256,width,height,duration_seconds,
                   commit_match,status,created_at,updated_at) VALUES(?,?,?,?,?,?,1280,720,30,1,'passed',?,?)""",
                ("rec-" + arm, pair["id"], arm, str(self.root / (arm + ".mov")), sha, arm * 64, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,draft_verdict,draft_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'draft',?,?)""",
            ("gsb-complete", pair["id"], "Same", "A 和 B 均完成主要要求，验收结果一致，最终交付没有影响使用的差异。",
             "Same", "A 和 B 均完成主要要求，验收结果一致，最终交付没有影响使用的差异。", stamp, stamp),
        )
        self.service.confirm_gsb(
            pair["id"], "Same",
            "A 的 app/main.py 完成全部主要要求，docker compose run verify 显示核心流程可用，与 B 接近。",
            "B 的 app/main.py 也完成全部主要要求，docker compose run verify 呈现相同结果，因此判为 Same。",
            "刘昱",
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'ready',?,?)""",
            ("repo-preflight", pair["id"], "owner", "repo", "public", str(self.root), stamp, stamp),
        )
        check = self.service.delivery_preflight(pair["id"])
        self.assertTrue(check["eligible"])
        self.assertTrue(check["warnings"])
        self.db.execute("UPDATE git_repositories SET visibility='private' WHERE pair_id=?", (pair["id"],))
        private = self.service.delivery_preflight(pair["id"])
        self.assertFalse(private["eligible"])
        self.assertIn("GitHub 仓库不是公开仓库，SOLO-QA 无法核验分支与提交", private["blockers"])
        self.db.execute("UPDATE git_repositories SET visibility='public' WHERE pair_id=?", (pair["id"],))
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        version = self.service.gsb_evidence_version(pair["id"], review["verdict"], review["reason"])
        self.db.execute(
            """INSERT INTO gsb_rechecks(id,pair_id,evidence_version,input_verdict,input_reason,result_status,
               suggested_verdict,suggested_reason,model,reasoning_effort,created_at)
               VALUES(?,?,?,?,?,'fact_conflict',?,?, 'gpt-6-astra','high',?)""",
            ("recheck-1", pair["id"], version, review["verdict"], review["reason"], "A better",
             "A 的验收更完整，B 存在会影响主要流程的问题，因此 A 更好。", stamp),
        )
        blocked = self.service.delivery_preflight(pair["id"])
        self.assertFalse(blocked["eligible"])
        self.assertIn("模型复检发现公开理由存在事实冲突", blocked["blockers"])

    def test_delivery_xlsx_is_a_valid_workbook(self):
        payload, filename = build_xlsx([{
            "project_number": "chain-1", "pair_id": "pair-1", "title": "任务", "task_type": "feature",
            "difficulty": "困难", "prompt": "实现复杂功能", "main_sha": "1" * 40,
            "a_session_id": "sa", "a_prompt_id": "pa", "a_commit": "2" * 40,
            "b_session_id": "sb", "b_prompt_id": "pb", "b_commit": "3" * 40,
            "verdict": "A better", "a_reason": "A 的实际交付更完整。", "b_reason": "B 的主流程存在可复现问题。",
            "preference_reason": "A 的主要功能更可靠。",
            "readiness": "ready", "submission_status": "ready_to_submit",
        }])
        self.assertTrue(filename.endswith(".xlsx"))
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("项目编号", sheet)
        self.assertIn("pair-1", sheet)

    def test_delivery_list_exposes_each_missing_material(self):
        row = {
            "a_session_id": "session-a", "a_prompt_id": "prompt-a", "a_commit": "a" * 40,
            "a_check_status": "failed", "a_recording_status": "passed", "a_recording_match": 1,
            "a_recording_review_status": "confirmed",
            "b_session_id": "", "b_prompt_id": "", "b_commit": "", "b_check_status": None,
            "b_recording_status": None, "b_recording_match": 0, "b_recording_review_status": "pending",
            "gsb_status": "draft", "recheck_status": "fact_conflict",
        }
        result = Handler._decorate_delivery(row)
        self.assertEqual(result["readiness"], "blocked")
        self.assertIn("A Docker 验收失败", result["readiness_issues"])
        self.assertIn("B 缺少 SessionID", result["readiness_issues"])
        self.assertIn("B 缺少通过的 Docker 验收", result["readiness_issues"])
        self.assertIn("B 缺少合格录像", result["readiness_issues"])
        self.assertIn("GSB 尚未确认", result["readiness_issues"])
        self.assertIn("复检发现公开理由存在事实冲突", result["readiness_issues"])

    def test_historical_import_excludes_bug_and_medium(self):
        source = sqlite3.connect(str(self.config.old_db_path))
        source.executescript("""
        CREATE TABLE runs(id TEXT,repo_name TEXT,task_type TEXT,task_difficulty TEXT,language_framework TEXT,
          repo_path TEXT,repo_url TEXT,base_sha TEXT,first_prompt TEXT,status_detail TEXT,phase TEXT,
          created_at TEXT,deleted_at TEXT);
        """)
        rows = [
            ("1","hard-zero","0-1 代码生成","困难","Python","","","abc","hard prompt","","complete","2026-01-01",None),
            ("2","hard-bug","Bug 修复","困难","Python","","","def","bug prompt","","complete","2026-01-02",None),
            ("3","medium-zero","0-1 代码生成","中等","Python","","","ghi","medium prompt","","complete","2026-01-03",None),
        ]
        source.executemany("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        source.commit(); source.close()
        result = import_historical_tasks(self.db, self.config.old_db_path)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM tasks")["count"], 1)
        self.assertEqual(self.db.one("SELECT project_category FROM tasks")["project_category"], "纯后端")

    def test_dashboard_counts_each_pair_once_and_groups_task_and_system_types(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET project_category='全栈' WHERE id='task-1'")
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-dashboard-" + arm, pair["id"], arm, arm * 8, stamp, stamp),
            )
        result = dashboard(self.db)
        self.assertEqual(result["summary"]["totalPairs"], 1)
        self.assertEqual(result["taskTypes"], [{"task_type": "zero_to_one", "count": 1}])
        self.assertEqual(result["projectCategories"], [{"project_category": "全栈", "count": 1}])
        self.assertNotIn("recentPairs", result)
        self.assertEqual(result["summary"]["completedPairs24h"], 0)
        self.assertEqual(len(result["trend24h"]), 24)

    def test_manual_recording_attempt_is_saved_as_manual_mode(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        compose = self.root / "compose.yaml"
        compose.write_text("services: {}\n", encoding="utf-8")
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-manual", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,compose_file,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'passed',?,?)""",
            ("check-manual", pair["id"], "A", "a" * 40, str(compose), stamp, stamp),
        )
        with patch("pairwise_console.recording.threading.Thread.start"):
            attempt = self.service.start_recording(pair["id"], "A", manual=True)
        self.assertEqual(attempt["interaction_mode"], "manual")

    def test_invalidating_current_recording_preserves_attempt_history(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'passed',?,?)""",
            ("attempt-old", pair["id"], "A", "a" * 40, str(self.root / "old.mp4"), stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,attempt_id,commit_match,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,'passed',?,?)""",
            ("recording-old", pair["id"], "A", str(self.root / "old.mp4"),
             "a" * 40, "attempt-old", stamp, stamp),
        )

        self.assertEqual(self.service._invalidate_recordings(pair["id"], ["A"], "Arm 已重新开发"), 1)
        self.assertIsNone(self.db.one("SELECT id FROM recordings WHERE pair_id=?", (pair["id"],)))
        self.assertIsNotNone(self.db.one("SELECT id FROM recording_attempts WHERE id='attempt-old'"))
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (pair["id"],),
        )
        self.assertEqual(event["event_type"], "recording.current_invalidated")

    def test_failed_artifact_cannot_start_delivery_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-failed-recording", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?, 'failed',?,?,?,?)""",
            ("check-failed", pair["id"], "A", "a" * 40,
             '[{"name":"compose_file","passed":false,"detail":"未找到 Compose 文件"}]',
             "缺少 Docker Compose", stamp, stamp),
        )
        with self.assertRaisesRegex(ValueError, "Docker 产物验收未通过"):
            self.service.start_recording(pair["id"], "A")

    def test_failed_artifact_restarts_only_failed_arm_before_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-artifact-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        checks = [
            {"arm": "A", "status": "failed", "error": "缺少 Docker Compose 或 Dockerfile"},
            {"arm": "B", "status": "passed", "error": ""},
        ]
        restarted = {"id": "arm-artifact-A", "arm": "A", "status": "developing"}
        with patch.object(self.service.artifacts, "validate", side_effect=checks), \
             patch.object(self.service, "_restart_arm_from_delivered_commit", return_value=restarted) as retry, \
             patch.object(self.service, "_submit_monitor") as submit:
            result = self.service._validate_pair_artifacts(pair["id"])
        self.assertEqual(result["restarted"], ["A"])
        self.assertIn("Docker 产物验收失败", retry.call_args.args[3])
        submit.assert_called_once_with("monitor-arm-artifact-A", self.service._monitor_arm,
                                       pair["id"], "arm-artifact-A", "Build a hard project with Docker Compose")
        current = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["stage"], "development")

    def test_compose_port_variables_are_all_isolated(self):
        compose = self.root / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  api:\n    ports: ['${API_PORT:-8000}:8000']\n"
            "  web:\n    ports: ['${WEB_PORT:-8080}:80']\n",
            encoding="utf-8",
        )
        env, assigned = isolated_compose_environment(compose)
        self.assertEqual(env["API_PORT"], assigned["API_PORT"])
        self.assertEqual(env["WEB_PORT"], assigned["WEB_PORT"])
        self.assertNotEqual(assigned["API_PORT"], assigned["WEB_PORT"])
        self.assertTrue(all(value.isdigit() for value in assigned.values()))

    def test_host_port_collision_reuses_completed_commits(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-port-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
        collision = {
            "status": "failed", "error": "Docker Compose 清洁启动失败",
            "checks_json": json.dumps([{
                "name": "clean_start", "passed": False,
                "detail": "Bind for 0.0.0.0:8080 failed: port is already allocated",
            }]),
        }
        checks = [{**collision, "arm": "A"}, {**collision, "arm": "B"}]
        with patch.object(self.service.artifacts, "validate", side_effect=checks), \
             patch.object(self.service, "_restart_arm_from_delivered_commit") as retry:
            result = self.service._validate_pair_artifacts(pair["id"])
        retry.assert_not_called()
        self.assertEqual(result["reused"], ["A", "B"])
        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "artifact_validation"})
        arms = self.db.all("SELECT status,commit_sha FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair["id"],))
        self.assertEqual([arm["status"] for arm in arms], ["completed", "completed"])
        self.assertEqual([arm["commit_sha"] for arm in arms], ["a" * 40, "b" * 40])

    def test_recording_failure_waits_for_manual_rerecording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-reuse-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed',?,?)""",
                ("check-reuse-" + arm, pair["id"], arm, sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='recording_failed',error='old failure' WHERE id=?",
            (pair["id"],),
        )
        self.assertFalse(self.service._resume_one_reusable_pair())
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "recording_failed", "error": "old failure"})
        arms = self.db.all("SELECT status,commit_sha FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair["id"],))
        self.assertEqual([arm["status"] for arm in arms], ["completed", "completed"])
        self.assertEqual([arm["commit_sha"] for arm in arms], ["a" * 40, "b" * 40])

    def test_recording_prefers_frontend_published_port(self):
        compose_ps = json.dumps([
            {"Service": "api", "Publishers": [{"PublishedPort": 51001}]},
            {"Service": "web", "Publishers": [{"PublishedPort": 51002}]},
        ])
        with patch("pairwise_console.recording.run_command", return_value=MagicMock(stdout=compose_ps)):
            port = RecordingManager._published_port(["docker", "compose"], self.root, {})
        self.assertEqual(port, 51002)

    def test_artifact_failure_pair_reopens_for_commit_based_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-artifact-reuse-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='artifact_failed',error='missing Dockerfile' WHERE id=?",
            (pair["id"],),
        )
        self.assertTrue(self.service._resume_one_reusable_pair())
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "running", "stage": "artifact_validation", "error": ""})
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (pair["id"],),
        )
        self.assertEqual(event["event_type"], "artifact.revalidation_started")

    def test_reusable_pair_waits_when_all_pair_slots_are_occupied(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-capacity-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image",
                 arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='artifact_failed',error='missing Dockerfile' WHERE id=?",
            (pair["id"],),
        )
        for index in range(3):
            self.db.execute(
                """INSERT INTO pairs(id,task_id,chain_id,status,stage,created_at,updated_at)
                   VALUES(?,?,?,'running','development',?,?)""",
                ("pair-active-%d" % index, "task-1", pair["chain_id"], stamp, stamp),
            )

        self.assertFalse(self.service._resume_one_reusable_pair())
        current = self.db.one("SELECT status,stage,error FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {
            "status": "failed", "stage": "artifact_failed", "error": "missing Dockerfile",
        })

    def test_completed_trace_prompt_mismatch_restarts_only_invalid_arm(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,session_id,prompt_id,trace_path,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
                ("arm-trace-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", "session-" + arm, "prompt-" + arm,
                 str(self.root / "traces" / arm), arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-trace", pair["id"], "Same", "old", "old A", "old B", stamp, stamp),
        )
        restarted = {"id": "arm-trace-B", "arm": "B", "status": "developing"}
        with patch.object(self.service, "_inspect_trace", side_effect=[
                (Path("A.jsonl"), "2.1.269", []),
                (Path("B.jsonl"), "2.1.269", ["B 轨迹中没有与题面逐字一致的首轮 User Prompt"]),
             ]), \
             patch.object(self.service, "_restart_arm_from_baseline", return_value=restarted) as restart, \
             patch.object(self.service, "_submit_monitor") as submit, \
             patch.object(self.service, "_submit") as submit_operation:
            self.service._refresh_pair_after_arm(pair["id"])
        restart.assert_called_once()
        restart_args, restart_kwargs = restart.call_args
        self.assertEqual(restart_args[0], pair["id"])
        self.assertEqual(restart_args[1]["id"], "arm-trace-B")
        self.assertEqual(restart_args[2:4], (
            "Build a hard project with Docker Compose",
            "轨迹首轮题面不一致，按数据库原题面重新运行",
        ))
        self.assertEqual(restart_kwargs, {"count_development_failure": False})
        submit.assert_called_once_with(
            "monitor-arm-trace-B", self.service._monitor_arm,
            pair["id"], "arm-trace-B", "Build a hard project with Docker Compose",
        )
        submit_operation.assert_not_called()
        states = {row["arm"]: row["status"] for row in self.db.all(
            "SELECT arm,status FROM arm_runs WHERE pair_id=?", (pair["id"],)
        )}
        self.assertEqual(states, {"A": "completed", "B": "waiting_retry"})
        self.assertEqual(self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))["stage"], "development")
        self.assertEqual(self.db.one("SELECT status FROM gsb_reviews WHERE pair_id=?", (pair["id"],))["status"], "draft")

    def test_archived_completed_trace_is_restored_before_prompt_validation(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm_id = pair["id"] + "-a"
        session_id = "session-archived-A"
        missing = self.config.data_dir / "claude-runs" / arm_id / "traces"
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,session_id,prompt_id,trace_path,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?)""",
            (arm_id, pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", session_id, "prompt-A", str(missing), "a" * 40,
             stamp, stamp),
        )
        archived = (
            self.config.data_dir / "claude-attempts" /
            (arm_id + "-attempt-1-archive") / "traces" / "-workspace"
        )
        archived.mkdir(parents=True)
        (archived / (session_id + ".jsonl")).write_text(
            json.dumps({
                "type": "user", "version": "2.1.269", "sessionId": session_id,
                "message": {"role": "user", "content": "Build a hard project with Docker Compose"},
            }) + "\n",
            encoding="utf-8",
        )
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))

        trace, version, issues = self.service._inspect_trace(
            arm, "Build a hard project with Docker Compose",
        )

        self.assertEqual(issues, [])
        self.assertEqual(version, "2.1.269")
        self.assertTrue(trace.is_file())
        self.assertTrue(str(trace).startswith(str(missing.resolve())))
        self.assertEqual(
            self.db.one("SELECT trace_path FROM arm_runs WHERE id=?", (arm_id,))["trace_path"],
            str(missing),
        )
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM audit_events WHERE entity_id=? AND event_type='claude.archived_trace_restored'",
            (arm_id,),
        ))

    def test_missing_trace_is_not_reported_as_prompt_mismatch(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm = {
            "id": pair["id"] + "-a", "pair_id": pair["id"], "arm": "A",
            "status": "completed", "attempt_no": 1,
        }
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
            (arm["id"], pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'needs_review',?,?)""",
            ("delivery-trace-label", pair["id"], stamp, stamp),
        )
        restarted = {**arm, "status": "developing"}
        with patch.object(self.service, "_restart_arm_from_baseline", return_value=restarted), \
             patch.object(self.service, "_submit_monitor"):
            self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project with Docker Compose", ["A 轨迹目录无效"],
            )

        delivery = self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "needs_review")
        self.assertIn("轨迹文件校验未通过", delivery["error"])
        self.assertNotIn("题面不一致", delivery["error"])

    def test_retired_pair_delivery_is_discarded_instead_of_waiting_for_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
               VALUES(?,?,'needs_review','轨迹题面不一致，等待单侧重跑和重新验收',?,?)""",
            ("delivery-retired", pair["id"], stamp, stamp),
        )
        with patch.object(self.service, "_submit") as submit:
            self.service._retire_pair_and_schedule_replacement(
                pair["id"], pair["id"] + "-b", "首轮超时且无代码产出",
            )

        delivery = self.db.one("SELECT status,error FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "discarded")
        self.assertIn("已停止交付", delivery["error"])
        submit.assert_called_once_with(
            "replace-task-" + pair["id"], self.service._start_replacement_pair, pair["id"],
        )

    def test_gsb_process_evidence_excludes_discarded_arm_sessions(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, sent_at in (("A", "2026-09-17T10:00:00+00:00"), ("B", "2026-09-17T12:00:00+00:00")):
            arm_id = pair["id"] + "-" + arm.lower()
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,prompt_sent_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
                (arm_id, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sent_at, stamp, stamp),
            )
        events = (
            (pair["id"] + "-a", "A-old", "2026-09-17T09:00:00+00:00"),
            (pair["id"] + "-a", "A-current", "2026-09-17T10:30:00+00:00"),
            (pair["id"] + "-b", "B-old", "2026-09-17T11:00:00+00:00"),
            (pair["id"] + "-b", "B-current", "2026-09-17T12:30:00+00:00"),
            (pair["id"], "pair-old", "2026-09-17T11:30:00+00:00"),
            (pair["id"], "pair-current", "2026-09-17T12:30:00+00:00"),
        )
        for entity_id, marker, created_at in events:
            self.db.execute(
                """INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at)
                   VALUES('claude.test','arm_run',?,?,?)""",
                (entity_id, json.dumps({"marker": marker}), created_at),
            )
        markers = [json.loads(row["detail_json"])["marker"] for row in self.service._current_process_events(pair["id"])]
        self.assertEqual(markers, ["A-current", "B-current", "pair-current"])

    def test_gsb_trace_evidence_uses_real_jsonl_steps_and_visible_tool_results(self):
        trace_dir = self.root / "trace-evidence"
        trace_dir.mkdir()
        rows = [
            {"type": "system", "message": {"content": "start"}},
            {"message": {"content": [{
                "type": "tool_use", "name": "Write",
                "input": {"file_path": "/workspace/app/dating.go", "content": "package app"},
            }]}},
            {"message": {"content": [{
                "type": "tool_result", "content": "File written successfully",
            }]}},
            {"message": {"content": [{
                "type": "tool_use", "name": "Bash",
                "input": {"command": "go test ./..."},
            }]}},
            {"message": {"content": [{
                "type": "tool_result", "is_error": True,
                "content": "use of internal package not allowed",
            }]}},
        ]
        (trace_dir / "session.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8",
        )
        evidence = self.service._trace_action_evidence({"trace_path": str(trace_dir)})
        self.assertTrue(evidence["available"])
        self.assertEqual([event["step"] for event in evidence["events"]], [2, 3, 4, 5])
        self.assertEqual(evidence["events"][0]["tool"], "Write")
        self.assertIn("dating.go", evidence["events"][0]["detail"])
        self.assertEqual(evidence["events"][2]["detail"], "go test ./...")
        self.assertTrue(evidence["events"][3]["isError"])

    def test_gsb_prompt_requires_plain_language_trace_and_reproduced_bug_evidence(self):
        prompt = gsb_prompt("开发送检单", "A evidence", "B evidence", "process events")
        self.assertIn("traceEvidence", prompt)
        self.assertIn("不要为了显得简短而删掉能支撑结论的证据", prompt)
        self.assertIn("公开理由不得出现“第175步”", prompt)
        self.assertIn("step 只供内部找到证据", prompt)
        self.assertIn("不额外追求最短", prompt)
        self.assertIn("没有实际跑接口流程", prompt)
        self.assertIn("process events", prompt)

    def test_gsb_recheck_prioritizes_logic_and_preserves_useful_evidence(self):
        prompt = gsb_recheck_prompt("开发送检单", "A better", "A reason", "B reason", "evidence")
        self.assertIn("复检首先检查首次生成的 GSB 逻辑是否正确", prompt)
        self.assertIn("不要因为理由较长或证据较多就要求精简", prompt)
        self.assertIn("必须保留原评价中所有会影响结论的有效证据", prompt)
        self.assertIn("建议理由中不得出现第几步", prompt)
        self.assertIn("不得仅因篇幅、数字数量或代码细节较多判为需要修改", prompt)

    def test_gsb_style_check_targets_mechanical_numbers_without_rejecting_real_evidence(self):
        issues = self.service._gsb_conversational_issues(
            "rehearsal.spec.ts 第15、16、21、22项通过，83个单测、23个端到端测试和38秒录像也通过，未见已发生的功能缺陷。",
            "B 在第43步运行接口测试，先因测试库冲突失败，换成独立数据库后通过。",
        )
        self.assertTrue(any("机械罗列测试编号" in issue for issue in issues))
        self.assertTrue(any("堆叠测试数量" in issue for issue in issues))
        self.assertTrue(any("录像时长" in issue for issue in issues))
        self.assertTrue(any("生硬的无缺陷套话" in issue for issue in issues))
        self.assertTrue(any(issue.startswith("B ") and "轨迹步骤号" in issue for issue in issues))

    def test_gsb_cleanup_removes_trace_step_numbers_without_dropping_evidence(self):
        source = (
            "B 中途第439步的回溯属性错误已在第448步修正，"
            "第569步及 Docker 又跑通重复码分叉，第646步测试全过。"
        )
        cleaned = self.service._clean_gsb_part(source, 300)
        self.assertNotRegex(cleaned, r"第\d+步")
        self.assertIn("回溯属性错误后来已修正", cleaned)
        self.assertIn("Docker 又跑通重复码分叉", cleaned)
        self.assertIn("测试全过", cleaned)

    def test_completed_pair_with_current_failed_artifact_is_quarantined(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, status in (("A", "failed"), ("B", "passed")):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-quarantine-" + arm, pair["id"], arm, arm, str(self.root / arm), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("check-quarantine-" + arm, pair["id"], arm, sha, status, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.service._quarantine_invalid_completed_pairs()
        current = self.db.one("SELECT status,stage,completed_at FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "artifact_failed", "completed_at": None})

    def test_api_error_invalidates_attempt_even_if_trace_later_finishes(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-api-error", "container_name": "container-api-error"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "isApiErrorMessage": True,
             "message": {"content": [{"type": "text", "text": "API Error: 504 Gateway Timeout"}]}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Finished after internal retry"}],
            }},
            {"type": "system", "subtype": "turn_duration"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertIn("504", state["api_error"])
        self.assertFalse(hasattr(self.service.claude, "send_continue"))

    def test_failed_trace_copy_retains_old_container_and_prepares_fresh_session(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "projects" / pair["id"] / "workspaces" / "A"
        workspace.mkdir(parents=True)
        (workspace / "partial.py").write_text("print('partial')\n", encoding="utf-8")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,session_id,prompt_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?,?,?)""",
            ("arm-copy-fail", pair["id"], "A", "A", str(workspace), "old-container", "old-screen",
             "auto_model/urm", "image", stamp, "session-1", "prompt-1", stamp, stamp),
        )
        failed_copy = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "copy failed"})()
        with patch.object(self.service.claude, "_graceful_stop"), \
             patch.object(self.service.claude, "_container_exists", return_value=True), \
             patch.object(self.service.claude, "_copy_traces", return_value=failed_copy), \
             patch.object(self.service.claude, "_screen_running", return_value=False), \
             patch.object(self.service.claude, "_close_terminal_window"), \
             patch("pairwise_console.claude_runner.run_command") as command:
            updated = self.service.claude.archive_failed_attempt(
                self.db.one("SELECT * FROM arm_runs WHERE id='arm-copy-fail'"), "API Error: 429", True,
                count_development_failure=False,
                count_error_retry=False,
            )
        self.assertEqual(updated["attempt_no"], 1)
        self.assertEqual(updated["error_retry_count"], 0)
        self.assertEqual(updated["status"], "queued")
        self.assertNotEqual(updated["container_name"], "old-container")
        self.assertNotEqual(updated["workspace_path"], str(workspace))
        self.assertTrue(Path(updated["workspace_path"]).is_dir())
        self.assertFalse(any(call.args[0][:2] == ["docker", "rm"] for call in command.call_args_list))

    def test_completed_trace_is_verified_before_container_removal(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "verified-workspace"
        workspace.mkdir()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,session_id,prompt_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?,?)""",
            ("arm-verified", pair["id"], "A", "A", str(workspace), "verified-container", "verified-screen",
             "auto_model/urm", "image", stamp, "session-verified", "prompt-verified", stamp, stamp),
        )
        root = self.service.claude.runtime_dir / "arm-verified"
        root.mkdir(parents=True)
        (root / "prompt.txt").write_text("Build verified output", encoding="utf-8")

        def copy_trace(_container, destination):
            events = [
                {"type": "user", "promptId": "prompt-verified", "message": {"content": "Build verified output"}},
                {"type": "system", "subtype": "turn_duration"},
            ]
            (destination / "session-verified.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        removed = type("Result", (), {"returncode": 0, "stdout": "verified-container", "stderr": ""})()
        with patch.object(self.service.claude, "_graceful_stop"), \
             patch.object(self.service.claude, "_copy_traces", side_effect=copy_trace), \
             patch.object(self.service.claude, "_screen_running", return_value=False), \
             patch.object(self.service.claude, "_close_terminal_window"), \
             patch("pairwise_console.claude_runner.run_command", return_value=removed) as command:
            trace_dir = self.service.claude.export_and_stop(
                self.db.one("SELECT * FROM arm_runs WHERE id='arm-verified'"),
            )
        self.assertTrue((trace_dir / "session-verified.jsonl").is_file())
        command.assert_called_once_with(["docker", "rm", "verified-container"], check=False, timeout=60)

    def test_third_development_failure_retires_pair_and_schedules_new_task(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-third-failure", "pair_id": pair["id"], "attempt_no": 3, "arm": "A"}
        with patch.object(self.service.claude, "archive_failed_attempt", return_value={**arm, "status": "failed"}) as archive, \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", "container exited")
        self.assertEqual(result["status"], "failed")
        archive.assert_called_once()
        replace.assert_called_once_with(pair["id"], arm["id"], "container exited")

    def test_429_is_free_but_504_consumes_a_development_attempt(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-transient-api", "pair_id": pair["id"], "attempt_no": 2, "arm": "A"}
        errors = (
            ("API Error: Request rejected (429) · litellm.RateLimitError: max_parallel_requests", False, False),
            ("API Error: 504 Gateway Timeout", True, True),
        )
        for error, count_development_failure, count_error_retry in errors:
            with self.subTest(error=error), \
                 patch.object(self.service, "_restart_arm_from_baseline",
                              return_value={**arm, "status": "developing"}) as restart, \
                 patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
                result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", error)
            self.assertEqual(result["status"], "developing")
            restart.assert_called_once_with(
                pair["id"], arm, "same prompt", error,
                count_development_failure=count_development_failure,
                count_error_retry=count_error_retry,
            )
            replace.assert_not_called()

    def test_third_504_retires_pair_and_schedules_replacement(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-third-504", "pair_id": pair["id"], "attempt_no": 3, "arm": "A"}
        error = "API Error: 504 Gateway Timeout"
        with patch.object(self.service.claude, "archive_failed_attempt",
                          return_value={**arm, "status": "failed"}) as archive, \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", error)
        self.assertEqual(result["status"], "failed")
        archive.assert_called_once()
        replace.assert_called_once_with(pair["id"], arm["id"], error)

    def test_only_twice_reproduced_hard_bug_converts_to_task(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,model,image,
               status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("arm-a", pair["id"], "A", "A", str(self.root), "container", "screen", "auto_model/urm",
             "image", "completed", "abc123", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
               reproduction_steps_json,actual_result,expected_result,reproduce_count,difficulty,
               difficulty_evidence_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("bug-1", pair["id"], "A", "abc123", "Concurrent commit loses update", "two clients",
             '["send two requests"]', "one update disappears", "both updates persist", 2, "困难",
             '["并发事务","异常恢复"]', "reproduced", stamp, stamp),
        )
        task = self.service.convert_bug_to_task("bug-1")
        self.assertEqual(task["task_type"], "bugfix")
        self.assertEqual(task["parent_pair_id"], pair["id"])
        self.assertEqual(task["status"], "ready")

    def test_arm_repository_is_imported_only_after_empty_container_start(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        source = self.root / "source-A"
        destination = self.root / "runtime-A"
        source.mkdir()
        destination.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=source)
        run_command(["git", "config", "user.name", "Test"], cwd=source)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=source)
        (source / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=source)
        run_command(["git", "commit", "-m", "baseline"], cwd=source)
        expected_sha = run_command(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
        arm = self.service.claude.prepare_arm(pair, "A", destination)

        with patch.object(self.service.claude, "_container_running", return_value=True):
            self.service.claude.materialize_repository(arm, source, expected_sha)

        self.assertEqual((destination / "README.md").read_text(encoding="utf-8"), "baseline\n")
        self.assertEqual(run_command(["git", "branch", "--show-current"], cwd=destination).stdout.strip(), "A")
        self.assertEqual(run_command(["git", "status", "--porcelain"], cwd=destination).stdout.strip(), "")
        (destination / "app.py").write_text("print('ready')\n", encoding="utf-8")
        run_command(["git", "add", "app.py"], cwd=destination)
        run_command(["git", "commit", "-m", "implementation"], cwd=destination)
        self.assertTrue(self.service.claude.has_business_code(destination, expected_sha))
        run_command(["git", "reset", "--hard", expected_sha], cwd=destination)
        nested = destination / "untracked-package"
        nested.mkdir()
        (nested / "worker.py").write_text("print('work')\n", encoding="utf-8")
        self.assertTrue(self.service.claude.has_business_code(destination, expected_sha))

    def test_dependency_and_build_directories_do_not_count_as_business_code(self):
        workspace = self.root / "dependency-only"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
        run_command(["git", "add", "README.md"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        for relative in (
            "backend/.venv/lib/python3.11/site-packages/helper.py",
            "frontend/node_modules/example/index.js",
            "backend/__pycache__/main.py",
            "frontend/dist/assets/app.js",
        ):
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("generated\n", encoding="utf-8")
        self.assertFalse(self.service.claude.has_business_code(workspace, baseline))

        source = workspace / "backend" / "app" / "main.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('business code')\n", encoding="utf-8")
        self.assertTrue(self.service.claude.has_business_code(workspace, baseline))


if __name__ == "__main__":
    unittest.main()
