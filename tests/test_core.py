import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pairwise_console.commands import run_command
from pairwise_console.config import OLD_APP_DIR, load_config
from pairwise_console.db import Database, now_iso
from pairwise_console.gitops import GitOps
from pairwise_console.analytics import dashboard
from pairwise_console.api import Handler
from pairwise_console.exports import build_xlsx
from pairwise_console.importer import import_historical_tasks
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
            "A 完成了主要流程和异常路径，真实验收覆盖完整，录像中的操作结果稳定。",
            "B 完成了主要流程，但异常路径仍有可复现失败，部分结果无法正常返回。",
            "刘昱",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(result["gsb"]["confirmed_by"], "刘昱")
        self.assertEqual(result["gsb"]["draft_verdict"], "Same")
        self.assertEqual(result["gsb"]["final_verdict"], "A better")
        self.assertEqual(result["delivery"]["status"], "ready_to_submit")

    def test_generated_gsb_is_default_confirmed_and_keeps_two_review_sections(self):
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
            "aReason": "A 完成了全部主要流程，异常路径与持久化结果都有可见验收证据，因此本次更倾向 A。",
            "bReason": "B 完成了核心流程，但异常恢复场景仍有可复现偏差，因此相比 A 不优先选择 B。",
            "evidence": ["A Docker 通过", "B 异常路径失败"],
        }
        with patch.object(self.service.codex, "run", return_value=result_payload):
            review = self.service.generate_gsb(pair["id"])
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
            "suggestedAReason": "A 的建议评价明确区分开发说明与后续独立验收，并保留可核对的具体结果。",
            "suggestedBReason": "B 的建议评价指出实际接口偏差及其客观后果，措辞限定在现有证据范围内。",
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
            "A 完成了全部主要要求，Docker 验收与录像均显示核心流程可用，与 B 的结果接近。",
            "B 也完成了全部主要要求，Docker 验收与录像呈现相同结果，因此两边判为 Same。",
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
             patch.object(self.service, "_handle_attempt_failure", return_value=restarted) as retry, \
             patch.object(self.service, "_submit_monitor") as submit:
            result = self.service._validate_pair_artifacts(pair["id"])
        self.assertEqual(result["restarted"], ["A"])
        self.assertIn("Docker 产物验收失败", retry.call_args.args[3])
        submit.assert_called_once_with("monitor-arm-artifact-A", self.service._monitor_arm,
                                       pair["id"], "arm-artifact-A", "Build a hard project with Docker Compose")
        current = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["stage"], "development")

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
            )
        self.assertEqual(updated["attempt_no"], 2)
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


if __name__ == "__main__":
    unittest.main()
