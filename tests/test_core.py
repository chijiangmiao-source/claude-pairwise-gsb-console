import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from pairwise_console.commands import run_command
from pairwise_console.classification import normalize_stack
from pairwise_console.config import OLD_APP_DIR, load_config
from pairwise_console.db import Database, now_iso
from pairwise_console.gitops import GitOps
from pairwise_console.analytics import dashboard
from pairwise_console.artifact import isolated_compose_environment
from pairwise_console.api import Handler
from pairwise_console.exports import build_xlsx
from pairwise_console.importer import import_historical_tasks
from pairwise_console.prompts import (
    feature_generation_prompt, gsb_prompt, gsb_recheck_prompt,
    task_generation_prompt, task_validation_prompt,
)
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

    def mark_completed_feature_source(self, pair_id, completed_at=None):
        stamp = completed_at or now_iso()
        commit = (pair_id.replace("pair-", "") + "a" * 40)[:40]
        self.db.execute(
            """UPDATE pairs SET status='completed',stage='completed',winner='A better',
               completed_at=?,updated_at=? WHERE id=?""",
            (stamp, stamp, pair_id),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            (pair_id + "-source-a", pair_id, "A", "A", str(self.root), pair_id + "-container-a",
             pair_id + "-screen-a", "auto_model/urm", "image", commit, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
               VALUES(?,?, 'A',?,'passed','[]',?,?)""",
            (pair_id + "-source-check-a", pair_id, commit, stamp, stamp),
        )

    def test_defaults_use_codex_for_review_and_claude_for_development(self):
        self.assertEqual(self.db.setting("codex_model"), "gpt-5.6-sol")
        self.assertEqual(self.db.setting("codex_default_effort"), "medium")
        self.assertEqual(self.db.setting("codex_bug_effort"), "high")
        self.assertEqual(self.db.setting("claude_model"), "auto_model/urm")
        self.assertEqual(self.db.setting("first_prompt_stop_minutes"), 40)
        self.assertEqual(self.db.setting("ab_prompt_stagger_seconds"), 30)
        self.assertIsNone(self.db.setting("task_mix_zero_to_one"))
        self.assertIsNone(self.db.setting("task_mix_feature"))
        self.assertIsNone(self.db.setting("task_mix_bugfix"))

    def test_service_restart_closes_stale_generation_batches(self):
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO generation_batches(id,status,requested_count,created_at,updated_at)
               VALUES('batch-stale','running',1,?,?)""",
            (stamp, stamp),
        )
        self.service._recover_interrupted_background_jobs()
        batch = self.db.one(
            "SELECT status,error,finished_at FROM generation_batches WHERE id='batch-stale'",
        )
        self.assertEqual(batch["status"], "failed")
        self.assertIn("结束陈旧状态", batch["error"])
        self.assertTrue(batch["finished_at"])

    def test_task_prompts_prejudge_minimum_necessary_complexity(self):
        validation = task_validation_prompt("task", "known", "baseline", "rejected examples")
        generated = task_generation_prompt("known", "zero_to_one")
        feature = feature_generation_prompt("original", "artifact", "known", "全栈")
        self.assertIn("最小实现", validation)
        self.assertIn("准确基线", validation)
        self.assertIn("近期开发完成后的真实难度案例", validation)
        self.assertIn("Bug 修复可用这些案例校准难度", validation)
        self.assertIn("至少两个相互制约", generated)
        self.assertIn("当前不存在且相互制约", feature)

    def test_task_duplicate_guard_checks_full_local_history(self):
        stamp = now_iso()
        original = "实现带断点恢复、分片摘要和幂等确认的大型扫描上传，并用 Docker Compose 验收异常恢复。"
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,'困难','[]',?,'used',?,?)""",
            ("task-history", "test", "zero_to_one", "历史扫描上传", original, "history-key", stamp, stamp),
        )
        reason = self.service._deterministic_task_duplicate({"title": "换名后的扫描上传", "prompt": original})
        self.assertIn("完全重复", reason)

    def test_task_duplicate_guard_rejects_a9_bug_template_and_long_shared_fragment(self):
        retired = (
            "旧 Bug\n\n前置条件：两个终端同时编辑\n\n复现步骤：\n1. 保存\n\n"
            "实际结果：远端修改丢失\n\n预期结果：保留双方修改\n\n"
            "请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收。"
        )
        reason = self.service._deterministic_task_duplicate({"title": "另一个 Bug", "prompt": retired})
        self.assertIn("A-9", reason)

        stamp = now_iso()
        shared = "这一段连续业务验收文字故意保持完全一致用于模拟低比例模板骨架重复并验证系统能够在整体相似度较低时提前拦截"
        left_context = "".join(chr(0x4E00 + index) for index in range(80))
        right_context = "".join(chr(0x5200 + index) for index in range(80))
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,'困难','[]',?,'used',?,?)""",
            ("task-fragment", "test", "zero_to_one", "历史任务", left_context + shared,
             "fragment-key", stamp, stamp),
        )
        reason = self.service._deterministic_task_duplicate({
            "title": "新任务", "prompt": right_context + shared,
        })
        self.assertIn("重复长骨架", reason)

    def test_task_duplicate_context_includes_previous_submission_prompts(self):
        connection = sqlite3.connect(self.config.old_db_path)
        connection.execute(
            """CREATE TABLE solo_qa_prompt_history(
               remote_submission_id TEXT,repo_name TEXT,prompt TEXT,task_type TEXT,remote_status TEXT,
               submitted_at TEXT,remote_updated_at TEXT,last_synced_at TEXT)"""
        )
        connection.execute(
            "INSERT INTO solo_qa_prompt_history VALUES('900','历史冷库项目',?,'0-1代码生成','QC_PASSED',?,?,?)",
            ("冷库断电后恢复告警序列并保持去重游标", now_iso(), now_iso(), now_iso()),
        )
        connection.commit()
        connection.close()
        context = self.service._task_generation_context()
        self.assertTrue(any(item["source"] == "历史提交题库" and item["title"] == "历史冷库项目" for item in context))
        prompt = "冷库断电后恢复告警序列并保持去重游标"
        self.assertIn("历史提交题库", self.service._deterministic_task_duplicate({"title": "新生成", "prompt": prompt}))
        self.assertEqual(
            self.service._deterministic_task_duplicate({"source": "legacy", "title": "允许复用", "prompt": prompt}),
            "",
        )

    def test_submission_claim_and_remote_binding_are_idempotent(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        first = self.service.update_solo_qa_state({"pair_id": pair["id"], "status": "submitting"})
        self.assertEqual(first["status"], "submitting")
        with self.assertRaisesRegex(ValueError, "重复上传"):
            self.service.update_solo_qa_state({"pair_id": pair["id"], "status": "submitting"})
        self.service.update_solo_qa_state({
            "pair_id": pair["id"], "status": "qc_pending", "remote_id": "474", "remote_status": "SUBMITTED",
        })
        with self.assertRaisesRegex(ValueError, "禁止改绑"):
            self.service.update_solo_qa_state({
                "pair_id": pair["id"], "status": "qc_pending", "remote_id": "475", "remote_status": "SUBMITTED",
            })
        synced = self.service.update_solo_qa_state({
            "pair_id": pair["id"], "status": "qc_passed", "remote_id": "474", "remote_status": "QC_PASSED",
        })
        self.assertEqual((synced["remote_id"], synced["status"]), ("474", "qc_passed"))

    def test_confirming_gsb_keeps_pending_fix_record_in_repair_flow(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-confirm-" + arm, pair["id"], arm, arm, str(self.root), "container-" + arm,
                 "screen-" + arm, "auto_model/urm", "image", arm.lower() * 40, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,created_at,updated_at)
                   VALUES(?,?,?,?, 'passed','[]',?,?)""",
                ("check-confirm-" + arm, pair["id"], arm, arm.lower() * 40, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'draft',?,?)""",
            ("gsb-confirm-repair", pair["id"], "Same", "", "", "", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,remote_status,created_at,updated_at)
               VALUES(?,?,'needs_fix','470','PENDING_FIX',?,?)""",
            ("delivery-confirm-repair", pair["id"], stamp, stamp),
        )
        self.service.confirm_gsb(
            pair["id"], "Same",
            "A 在 app.py 完成状态恢复，Docker 验收跑通关键异常路径，最终行为符合题面。",
            "B 在 app.py 也完成状态恢复，Docker 验收覆盖相同业务流程，结果与 A 接近。",
            "刘昱",
        )
        delivery = self.db.one("SELECT status,remote_id FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery, {"status": "needs_fix", "remote_id": "470"})

    def test_ready_task_selection_uses_oldest_available_type_without_ratio(self):
        stamps = {
            "feature": "2026-01-01T00:00:00+00:00",
            "bugfix": "2026-01-02T00:00:00+00:00",
            "zero_to_one": "2026-01-03T00:00:00+00:00",
        }
        for task_type in ("zero_to_one", "feature", "bugfix"):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,'ready',?,?)""",
                ("task-ready-" + task_type, "test", task_type, task_type, "hard task",
                 "ready-" + task_type, stamps[task_type], stamps[task_type]),
            )
        self.assertEqual(self.service._next_ready_task()["task_type"], "feature")

    def test_ready_task_selection_prefers_new_zero_to_one_and_rejects_duplicate_title(self):
        rows = [
            ("task-used", "test", "共享标题", "已经开发过的复杂状态恢复任务", "used", "2019-01-01T00:00:00+00:00"),
            ("task-duplicate", "test", "共享标题", "完全不同的说明也不应复用标题", "ready", "2020-01-01T00:00:00+00:00"),
            ("task-legacy", "legacy", "旧题", "旧的复杂零到一任务", "ready", "2021-01-01T00:00:00+00:00"),
            ("task-new", "generated", "新题", "新的复杂零到一任务", "ready", "2022-01-01T00:00:00+00:00"),
        ]
        for task_id, source, title, prompt, status, created_at in rows:
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,?,?,?)""",
                (task_id, source, "zero_to_one", title, prompt, task_id, status, created_at, created_at),
            )
        selected = self.service._next_ready_task()
        self.assertEqual(selected["id"], "task-new")
        rejected = self.db.one("SELECT status,rejection_reason FROM tasks WHERE id='task-duplicate'")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("标题", rejected["rejection_reason"])

    def test_missing_feature_and_bug_tasks_use_real_completed_pair_sources(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.mark_completed_feature_source(pair["id"], stamp)
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append((operation, fn, args)) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("feature"))
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted[0][0], "feature-" + pair["id"])
        self.assertIs(submitted[0][1].__func__, self.service.generate_followup_feature.__func__)
        self.assertEqual(submitted[1][0], "bugs-" + pair["id"])
        self.assertIs(submitted[1][1].__func__, self.service.discover_bugs.__func__)

    def test_discarded_delivery_is_not_reused_as_feature_or_bug_source(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES('delivery-discarded',?,'discarded',?,?)""",
            (pair["id"], stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("feature"))
            self.assertTrue(self.service._schedule_task_source("bugfix"))
        self.assertEqual(submitted, ["generate-mix-zero-to-one", "generate-mix-zero-to-one"])

    def test_failed_winner_artifact_is_not_retried_as_feature_source(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.mark_completed_feature_source(pair["id"])
        self.db.execute(
            "UPDATE artifact_checks SET status='observed_failed' WHERE pair_id=? AND arm='A'",
            (pair["id"],),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ):
            self.assertTrue(self.service._schedule_task_source("feature"))
        self.assertEqual(submitted, ["generate-mix-zero-to-one"])

    def test_latest_zero_to_one_gets_at_most_three_feature_tasks_then_new_project(self):
        self.insert_ready_task()
        older = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, older["id"]),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at)
               VALUES('task-latest-root','test','zero_to_one','latest root','hard project','困难','[]',
                      'latest-root','ready',?,?)""",
            (stamp, stamp),
        )
        latest = self.service.create_pair("task-latest-root")
        later = datetime.now(timezone.utc).isoformat()
        self.mark_completed_feature_source(latest["id"], later)
        statuses = ("used", "rejected", "used")
        for index, status in enumerate(statuses):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   parent_pair_id,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,?,?,?,?)""",
                (f"feature-limit-{index}", "generated_followup", "feature", f"feature {index}",
                 f"hard feature {index}", latest["id"], f"feature-limit-{index}", status, later, later),
            )
            submitted = []
            with patch.object(
                self.service, "_submit_auto",
                side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
            ):
                self.assertTrue(self.service._schedule_task_source("feature"))
            expected = "feature-" + latest["id"] if index < 2 else "generate-mix-zero-to-one"
            self.assertEqual(submitted, [expected])

        # The older project is still below its limit, but saturation of the
        # latest root deliberately starts a fresh 0–1 instead of mining old roots.
        self.assertFalse(self.db.one(
            "SELECT 1 FROM tasks WHERE parent_pair_id=? AND task_type='feature'",
            (older["id"],),
        ))
        with self.assertRaisesRegex(ValueError, "最多生成 3 个 Feature"):
            self.service.generate_followup_feature(latest["id"])

    def test_feature_limit_groups_legacy_rows_by_baseline_repository(self):
        for index in range(4):
            status = "used" if index < 3 else "candidate"
            suffix = ".git" if index == 3 else ""
            created_at = "2026-09-18T00:00:0%d+00:00" % index
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   baseline_path,baseline_repo_url,baseline_sha,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,?,?,?,?,?,?)""",
                (f"legacy-feature-{index}", "legacy", "feature", "same legacy project",
                 f"hard feature {index}", str(self.root),
                 "https://github.com/example/same-project" + suffix, "a" * 40,
                 f"legacy-feature-{index}", status, created_at, created_at),
            )
        with patch.object(self.service.codex, "run") as run:
            result = self.service.validate_task("legacy-feature-3")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("最多保留 3 个 Feature", result["result"]["reason"])
        run.assert_not_called()

    def test_refill_does_not_generate_a_missing_type_when_ready_pool_is_healthy(self):
        stamp = now_iso()
        for index in range(6):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
                   difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'困难','[]',?,'ready',?,?)""",
                ("task-pool-%d" % index, "test", "zero_to_one", "ready", "hard task",
                 "pool-%d" % index, stamp, stamp),
            )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,
               difficulty_evidence_json,fingerprint,status,created_at,updated_at)
               VALUES('task-old-candidate','test','zero_to_one','candidate','hard task',
                      '困难','[]','old-candidate','candidate',?,?)""",
            (stamp, stamp),
        )
        with patch.object(self.service, "_schedule_any_task_source") as schedule, \
             patch.object(self.service, "validate_task_async") as validate:
            self.service._schedule_refill_once()
        schedule.assert_not_called()
        validate.assert_not_called()

    def test_language_framework_field_keeps_only_technology_names(self):
        self.assertEqual(
            normalize_stack(
                "Python 3.13、FastAPI、Pydantic、SQLAlchemy/Alembic、持久化后台 worker；"
                "TypeScript、React、Vite；pytest、Vitest、Playwright；Docker、Docker Compose"
            ),
            "Python 3.13, FastAPI, TypeScript, React",
        )
        self.assertEqual(
            normalize_stack(
                "Python 3.13、FastAPI、Pydantic、pytest、Docker Compose；"
                "沿用现有算法，不引入外部服务。"
            ),
            "Python 3.13, FastAPI",
        )

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

    def test_baseline_preflight_rejects_feature_before_claude_launch(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        repo_root = self.root / "baseline-preflight"
        (repo_root / "A").mkdir(parents=True)
        self.db.execute("UPDATE tasks SET task_type='feature' WHERE id='task-1'")
        self.db.execute(
            "UPDATE pairs SET status='queued',stage='ready_to_start',baseline_sha=? WHERE id=?",
            ("b" * 40, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-preflight", pair["id"], "owner", "repo", "public", str(repo_root),
             "b" * 40, "b" * 40, "b" * 40, stamp, stamp),
        )
        for arm in ("A", "B"):
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                ("arm-preflight-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", stamp, stamp),
            )
        failed = {
            "status": "failed", "error": "缺少 Docker Compose 或 Dockerfile",
            "checks": [{"name": "compose_file", "passed": False, "detail": "未找到 Compose 文件"}],
        }
        with patch.object(self.service.artifacts, "preflight", return_value=failed), \
             patch.object(self.service.claude, "launch") as launch, \
             patch.object(self.service, "_submit") as submit:
            result = self.service.start_pair(pair["id"])
        launch.assert_not_called()
        submit.assert_called_once()
        self.assertEqual(result["stage"], "baseline_preflight_failed")
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-1'")["status"], "rejected")

    def test_prompt_is_canonicalized_before_native_send(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE tasks SET prompt=? WHERE id='task-1'",
            ("First paragraph.\r\n\r\nSecond paragraph.\n\n\nThird paragraph.\n",),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pair["id"] + "-a", pair["id"], "A", "A", str(self.root / "A"),
             "container-A", "screen-A", "auto_model/urm", "image", "running", stamp, stamp),
        )
        arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm='A'", (pair["id"],))
        with patch.object(self.service.claude, "send_prompt") as send_prompt:
            self.service._send_prompt_with_pair_stagger(pair["id"], arm, "stale fallback")
        expected = "First paragraph.\nSecond paragraph.\nThird paragraph."
        self.assertEqual(send_prompt.call_args.args[1], expected)
        task = self.db.one("SELECT prompt FROM tasks WHERE id='task-1'")
        self.assertEqual(task["prompt"], expected)
        audit = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='task-1' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(audit["event_type"], "task.prompt_canonicalized_for_native_trace")

    def test_claude_prompt_canonicalization_matches_native_shape(self):
        self.assertEqual(
            self.service.claude.canonical_prompt("A\r\n\r\nB\n \nC\n\n\n"),
            "A\nB\nC",
        )

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

    def test_actual_difficulty_review_promotes_passed_medium_bugfix_to_hard(self):
        pair = self._prepare_pair_for_difficulty_review()
        self.db.execute("UPDATE tasks SET task_type='bugfix' WHERE id='task-1'")
        result = {
            "aDifficulty": "中等", "bDifficulty": "中等", "difficulty": "中等",
            "reason": "真实缺陷需要理解跨模块数据流并修正边界处理，但不涉及架构重设计。",
            "evidence": ["两次清洁环境已复现", "Docker 验收覆盖原始缺陷路径"],
        }
        with patch.object(self.service.codex, "run", return_value=result):
            review = self.service.reassess_actual_difficulty(pair["id"])
        self.assertEqual(review["status"], "passed")
        self.assertEqual(review["assessed_difficulty"], "困难")
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "running", "stage": "recording"},
        )
        self.assertEqual(self.db.one("SELECT difficulty FROM tasks WHERE id='task-1'")["difficulty"], "困难")

    def test_evidence_filter_finds_any_manual_rerecord_attempt(self):
        pair = self._prepare_pair_for_difficulty_review()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(
                 id,pair_id,arm,commit_sha,path,interaction_mode,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual','passed',?,?)""",
            ("manual-rerecord", pair["id"], "A", "a" * 40, str(self.root / "manual.mp4"), stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO recording_attempts(
                 id,pair_id,arm,commit_sha,path,interaction_mode,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual','recording',?,?)""",
            ("manual-active", pair["id"], "B", "b" * 40, str(self.root / "active.mp4"), stamp, stamp),
        )
        handler = Handler.__new__(Handler)
        handler.server = MagicMock(db=self.db)
        manual = handler._evidence_page({"manual_rerecorded": ["yes"], "page": ["1"], "size": ["20"]})
        automatic = handler._evidence_page({"manual_rerecorded": ["no"], "page": ["1"], "size": ["20"]})
        self.assertEqual([(row["arm"], row["manual_rerecorded"]) for row in manual["items"]], [("A", 1)])
        self.assertEqual([(row["arm"], row["manual_rerecorded"]) for row in automatic["items"]], [("B", 0)])

    def test_active_recording_is_available_outside_the_current_evidence_page(self):
        pair = self._prepare_pair_for_difficulty_review()
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO recording_attempts(
                 id,pair_id,arm,commit_sha,path,interaction_mode,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'manual','recording',?,?)""",
            ("manual-active", pair["id"], "A", "a" * 40, str(self.root / "manual.mp4"), stamp, stamp),
        )
        handler = Handler.__new__(Handler)
        handler.server = MagicMock(db=self.db)

        active = handler._active_recording()

        self.assertEqual(active["id"], "manual-active")
        self.assertEqual(active["pair_id"], pair["id"])
        self.assertEqual(active["interaction_mode"], "manual")
        self.assertEqual(active["title"], "hard-project")

    def test_colloquial_api_route_starts_preview_operation(self):
        handler = Handler.__new__(Handler)
        service = MagicMock()
        service.colloquialize_gsb_async.return_value = "gsb-colloquial-test"
        handler.server = MagicMock(service=service)
        handler._path_query = MagicMock(return_value=("/api/pairs/pair-test/gsb/colloquialize", {}))
        source = {"verdict": "Same", "aReason": "A reason long enough for preview", "bReason": "B reason long enough for preview"}
        handler._body = MagicMock(return_value=source)
        handler._json = MagicMock()

        handler.do_POST()

        service.colloquialize_gsb_async.assert_called_once_with("pair-test", source)
        handler._json.assert_called_once_with(202, {"operationId": "gsb-colloquial-test"})

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

    def test_monitor_recovery_does_not_reactivate_a_replaced_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='replaced',error='already replaced' WHERE id=?",
            (pair["id"],),
        )
        self.service._resume_active_monitors()
        current = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "failed", "stage": "replaced"})

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

    def test_pair_parallelism_has_a_hard_ceiling_of_four(self):
        self.db.set_setting("max_pairs_parallel", 9)
        stamp = now_iso()
        for index in range(5):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-limit-{index}", "test", "zero_to_one", f"hard-{index}", "Build a hard project",
                 "困难", '["跨模块状态"]', f"fingerprint-limit-{index}", "ready", stamp, stamp),
            )
        for index in range(4):
            self.service.create_pair(f"task-limit-{index}")
        with self.assertRaisesRegex(ValueError, "最多 4 个 Pair"):
            self.service.create_pair("task-limit-4")

    def test_concurrent_pair_creation_cannot_exceed_four(self):
        stamp = now_iso()
        for index in range(5):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-race-{index}", "test", "zero_to_one", f"hard-race-{index}", "Build a hard project",
                 "困难", '["并发状态"]', f"fingerprint-race-{index}", "ready", stamp, stamp),
            )
        barrier = threading.Barrier(5)
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

        threads = [threading.Thread(target=create, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(outcomes.count("created"), 4)
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM pairs")["count"], 4)
        self.assertTrue(any("最多 4 个 Pair" in outcome for outcome in outcomes))

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

    def test_one_click_automation_is_persistent_and_keeps_configured_pair_target(self):
        self.db.set_setting("max_pairs_parallel", 3)
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
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-auto-{index}", "test", "zero_to_one", f"hard-auto-{index}",
                 f"Build a distinct hard project number {index} with Docker Compose", "困难", '["跨模块状态"]',
                 f"fingerprint-auto-{index}", "ready", stamp, stamp),
            )
        submitted = []
        with patch.object(self.service, "_submit_auto", side_effect=lambda operation, fn, *args: submitted.append(operation) or True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["activePairs"], 4)
        self.assertEqual(status["readyTasks"], 0)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 4)
        self.assertEqual(len([item for item in submitted if item.startswith("repo-pair-")]), 4)
        refill.assert_not_called()

    def test_automation_scheduler_respects_configured_pair_target(self):
        self.db.set_setting("max_pairs_parallel", 3)
        stamp = now_iso()
        for index in range(4):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-target-{index}", "test", "zero_to_one", f"hard-target-{index}",
                 f"Build a distinct hard target project number {index} with Docker Compose",
                 "困难", '["跨模块状态"]', f"fingerprint-target-{index}", "ready", stamp, stamp),
            )
        submitted = []
        with patch.object(
            self.service, "_submit_auto",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertEqual(status["targetPairs"], 3)
        self.assertEqual(status["activePairs"], 3)
        self.assertEqual(status["readyTasks"], 1)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 3)
        self.assertEqual(len([item for item in submitted if item.startswith("repo-pair-")]), 3)
        refill.assert_not_called()

    def test_automation_refills_when_a_completed_pair_releases_a_slot(self):
        stamp = now_iso()
        for index in range(5):
            self.db.execute(
                """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
                   fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"task-cycle-{index}", "test", "zero_to_one", f"hard-cycle-{index}",
                 f"Build a distinct lifecycle project number {index} with Docker Compose", "困难", '["跨模块状态"]',
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
        self.assertEqual(status["activePairs"], 4)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 5)

    def test_gsb_confirmation_strips_backticks_and_completes_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO gsb_reviews(id,pair_id,verdict,reason,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gsb-1", pair["id"], "Same", "两边都完成了相同功能，但各有一些可以复核的实现差异。", "draft", stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET stage='recording',error='旧录像失败信息' WHERE id=?",
            (pair["id"],),
        )
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
        self.assertEqual(result["error"], "")
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

    def test_gsb_records_unstartable_delivery_with_failure_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET status='running',stage='gsb_ready' WHERE id=?", (pair["id"],))
        for arm, check_status, error in (
            ("A", "observed_failed", "Docker Compose 清洁启动失败"),
            ("B", "passed", ""),
        ):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-failed-gsb-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("check-failed-gsb-" + arm, pair["id"], arm, sha, check_status,
                 '[{"name":"clean_start","passed":false,"detail":"dependency path missing"}]' if error else "[]",
                 error, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,commit_match,review_status,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,1,'confirmed','passed',?,?)""",
                ("rec-failed-gsb-" + arm, pair["id"], arm, str(self.root / (arm + "-failure.mp4")),
                 sha, stamp, stamp),
            )
        payload = {
            "verdict": "B better",
            "aReason": "A 在 compose.yaml 的启动流程因依赖路径缺失而失败，清洁 Docker 无法启动，原始交付不可用。",
            "bReason": "B 在 compose.yaml 完成相同功能并通过清洁 Docker 验收，因此相较无法启动的 A 更可靠。",
            "evidence": ["A clean_start failed", "B Docker passed"],
        }
        with patch.object(self.service.codex, "run", return_value=payload) as run:
            review = self.service.generate_gsb(pair["id"])
        self.assertEqual(review["status"], "confirmed")
        self.assertIn("observed_failed", run.call_args.args[1])
        final = self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(final, {"status": "completed", "stage": "completed"})
        delivery = self.db.one("SELECT status FROM delivery_submissions WHERE pair_id=?", (pair["id"],))
        self.assertEqual(delivery["status"], "ready_to_submit")

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

    def test_colloquial_preview_does_not_persist_until_human_confirmation(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        original_a = "A 在 merge.py 修好了保存问题，接口测试通过，不过浏览器流程没有执行。"
        original_b = "B 在 rebase.ts 完成了页面流程，Docker 验收通过，但新增测试是否运行无法确认。"
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'confirmed',?,?)""",
            ("gsb-colloquial-source", pair["id"], "Same",
             self.service._compose_gsb_reason(original_a, original_b),
             original_a, original_b, stamp, stamp),
        )
        rewritten = {
            "verdict": "Same",
            "aReason": "A 在 merge.py 把保存问题修好了，接口也实际测过，不过浏览器流程还没跑。",
            "bReason": "B 在 rebase.ts 把页面流程接好了，Docker 验收通过，不过还不能确认新增测试是否运行。",
        }
        with patch("pairwise_console.service.rewrite_preview", return_value=rewritten):
            operation = self.service.colloquialize_gsb_async(pair["id"], {
                "verdict": "Same", "aReason": original_a, "bReason": original_b,
            })
            for _ in range(50):
                result = self.service.operation(operation)
                if result["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(result, {"id": operation, "status": "completed", "result": rewritten})
        stored = self.db.one("SELECT verdict,a_reason,b_reason FROM gsb_reviews WHERE pair_id=?", (pair["id"],))
        self.assertEqual(stored, {"verdict": "Same", "a_reason": original_a, "b_reason": original_b})

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
        bug_columns = {row["name"] for row in self.db.all("PRAGMA table_info(bug_candidates)")}
        self.assertIn("source_paths_json", bug_columns)
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
        self.assertEqual(delivery["status"], "ready_to_submit")

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
        with self.assertRaisesRegex(ValueError, "Docker 产物验收尚未形成最终结论"):
            self.service.start_recording(pair["id"], "A")

    def test_observed_artifact_failure_starts_short_evidence_recording(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
            ("arm-observed-recording", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", "a" * 40, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,checks_json,error,created_at,updated_at)
               VALUES(?,?,?,?, 'observed_failed',?,?,?,?)""",
            ("check-observed", pair["id"], "A", "a" * 40,
             '[{"name":"verify_service","passed":false,"command":"docker compose run --rm verify","exit_code":1,"detail":"ModuleNotFoundError: app"}]',
             "Docker 验收未全部通过", stamp, stamp),
        )
        with patch("pairwise_console.recording.threading.Thread.start"):
            attempt = self.service.start_recording(pair["id"], "A")
        self.assertEqual(attempt["status"], "starting")
        self.assertEqual(attempt["interaction_mode"], "auto")
        process = MagicMock()
        process.stdout.readline.return_value = '{"event":"ready"}'
        check = self.db.one("SELECT * FROM artifact_checks WHERE id='check-observed'")
        with patch("pairwise_console.recording.subprocess.Popen", return_value=process), \
             patch.object(self.service.recordings, "_wait") as wait:
            self.service.recordings._launch_failure_evidence(
                attempt["id"], self.root, Path(attempt["path"]), check,
            )
        failure_page = Path(attempt["path"]).with_suffix(".failure.html")
        html = failure_page.read_text(encoding="utf-8")
        self.assertIn("docker compose run --rm verify", html)
        self.assertIn("ModuleNotFoundError: app", html)
        self.assertIn("exit code 1", html)
        self.assertIn("border-radius:50%", html)
        wait.assert_called_once()

    def test_failed_artifact_is_preserved_for_gsb_without_claude_repair(self):
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
        checks = {
            "A": {"status": "failed", "error": "缺少 Docker Compose 或 Dockerfile"},
            "B": {"status": "passed", "error": ""},
        }

        def validate(pair_id, arm, _workspace, commit_sha):
            item = checks[arm]
            check_id = "check-artifact-" + arm
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (check_id, pair_id, arm, commit_sha, item["status"], item["error"], stamp, stamp),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,))

        with patch.object(self.service.artifacts, "validate", side_effect=validate), \
             patch.object(self.service, "_restart_arm_from_delivered_commit") as retry, \
             patch.object(self.service, "_submit_monitor") as submit:
            result = self.service._validate_pair_artifacts(pair["id"])
        self.assertEqual(result["preserved"], ["A"])
        retry.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT status FROM artifact_checks WHERE id='check-artifact-A'")["status"],
            "observed_failed",
        )
        current = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current["stage"], "recording")

    def test_pending_artifact_retry_recovers_from_delivered_commit(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        delivered = "a" * 40
        stamp = now_iso()
        repo_root = self.root / "pair-repo"
        (repo_root / "A").mkdir(parents=True)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',baseline_sha=? WHERE id=?",
            (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-retry", pair["id"], "owner", "repo", "public", str(repo_root),
             baseline, delivered, baseline, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,commit_sha,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'running','',?,?,?)""",
            ("arm-retry-A", pair["id"], "A", "A", str(self.root / "runtime-A"),
             "container-A", "screen-A", "auto_model/urm", "image",
             "Docker 产物验收失败：未通过清洁 Compose 验收", stamp, stamp),
        )
        prepared = repo_root / "A"
        with patch.object(self.service.claude, "reset_unsent_arm"), \
             patch.object(self.service.git, "prepare_arm_commit", return_value=prepared) as prepare, \
             patch.object(self.service.claude, "launch"), \
             patch.object(self.service.claude, "wait_until_ready"), \
             patch.object(self.service.claude, "materialize_repository") as materialize, \
             patch.object(self.service, "_send_prompt_with_pair_stagger"), \
             patch.object(self.service, "_monitor_arm", return_value={"status": "completed"}):
            result = self.service._recover_pending_retry(
                pair["id"], "arm-retry-A", "Build a hard project with Docker Compose"
            )
        prepare.assert_called_once_with(pair["id"], "A", delivered)
        self.assertEqual(materialize.call_args.args[2], delivered)
        self.assertEqual(result, {"status": "completed"})

    def test_artifact_retry_compares_new_work_with_delivered_commit(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        delivered = "a" * 40
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET baseline_sha=? WHERE id=?", (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            ("repo-source", pair["id"], "owner", "repo", "public", str(self.root),
             baseline, delivered, baseline, stamp, stamp),
        )
        self.assertEqual(self.service._arm_comparison_sha(pair["id"], "A"), delivered)
        self.assertEqual(self.service._arm_comparison_sha(pair["id"], "B"), baseline)

    def test_scheduler_recovers_only_stale_unsent_retry(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'running',?,?)""",
            ("arm-stale-A", pair["id"], "A", "A", str(self.root / "runtime-A"),
             "container-A", "screen-A", "auto_model/urm", "image",
             "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        with patch.object(self.service, "_submit_monitor") as submit:
            self.service._schedule_pending_arm_retries(pair["id"])
        submit.assert_called_once_with(
            "retry-recover-arm-stale-A", self.service._recover_pending_retry,
            pair["id"], "arm-stale-A", "Build a hard project with Docker Compose",
        )

    def test_checkpointed_arm_retries_only_git_push(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        traces = self.root / "completed-traces"
        traces.mkdir()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'exported',?,?,?,?)""",
            ("arm-checkpoint-A", pair["id"], "A", "A", str(self.root / "workspace-A"),
             "container-A", "screen-A", "auto_model/urm", "image", str(traces),
             "finished", stamp, stamp),
        )
        delivered = "d" * 40
        with patch.object(self.service.git, "push_arm", return_value=delivered) as push:
            result = self.service._finish_checkpointed_arm(pair["id"], "arm-checkpoint-A")
        push.assert_called_once_with(pair["id"], "A")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["commit_sha"], delivered)
        self.assertEqual(result["trace_path"], str(traces))

    def test_checkpointed_push_failure_preserves_code_and_trace(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        traces = self.root / "completed-traces"
        traces.mkdir()
        workspace = self.root / "workspace-A"
        workspace.mkdir()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?)""",
            ("arm-checkpoint-fail-A", pair["id"], "A", "A", str(workspace),
             "container-A", "screen-A", "auto_model/urm", "image", str(traces),
             "finished", stamp, stamp),
        )
        with patch.object(self.service.git, "push_arm", side_effect=TimeoutError("network timeout")):
            with self.assertRaisesRegex(TimeoutError, "network timeout"):
                self.service._finish_checkpointed_arm(pair["id"], "arm-checkpoint-fail-A")
        current = self.db.one("SELECT status,trace_path,error FROM arm_runs WHERE id='arm-checkpoint-fail-A'")
        self.assertEqual(current["status"], "checkpointing")
        self.assertEqual(current["trace_path"], str(traces))
        self.assertIn("等待重试 Git 推送", current["error"])
        self.assertTrue(workspace.is_dir())

    def test_stale_checkpoint_push_failure_does_not_overwrite_completed_arm(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        traces = self.root / "completed-traces"
        traces.mkdir()
        delivered = "e" * 40
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,trace_path,result,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?,?,?)""",
            ("arm-race-A", pair["id"], "A", "A", str(self.root / "workspace-A"),
             "container-A", "screen-A", "auto_model/urm", "image", str(traces),
             "finished", stamp, stamp),
        )

        def stale_push(*_args):
            self.db.execute(
                """UPDATE arm_runs SET status='completed',commit_sha=?,error='',
                   finished_at=?,updated_at=? WHERE id='arm-race-A'""",
                (delivered, stamp, stamp),
            )
            raise RuntimeError("remote ref changed while pushing")

        with patch.object(self.service.git, "push_arm", side_effect=stale_push):
            result = self.service._finish_checkpointed_arm(pair["id"], "arm-race-A")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["commit_sha"], delivered)
        self.assertEqual(result["error"], "")

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
        for index in range(4):
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

    def test_trace_repair_does_not_restart_a_replaced_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        arm = {
            "id": pair["id"] + "-a", "pair_id": pair["id"], "arm": "A",
            "status": "completed", "attempt_no": 3,
        }
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)""",
            (arm["id"], pair["id"], "A", "A", str(self.root / "A"), "container-A", "screen-A",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='replaced',error='已自动换题' WHERE id=?",
            (pair["id"],),
        )
        with patch.object(self.service, "_restart_arm_from_baseline") as restart, \
             patch.object(self.service, "_submit_monitor") as monitor:
            result = self.service._restart_trace_invalid_arms(
                pair["id"], [arm], "Build a hard project with Docker Compose", ["A 轨迹目录无效"],
            )

        self.assertEqual(result["skipped"], "terminal_pair")
        restart.assert_not_called()
        monitor.assert_not_called()
        self.assertEqual(
            self.db.one("SELECT status,stage FROM pairs WHERE id=?", (pair["id"],)),
            {"status": "failed", "stage": "replaced"},
        )

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

    def test_completed_pair_with_recorded_artifact_failure_survives_restart_check(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        for arm, status in (("A", "observed_failed"), ("B", "passed")):
            sha = arm.lower() * 40
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
                ("arm-observed-complete-" + arm, pair["id"], arm, arm, str(self.root / arm),
                 "container-" + arm, "screen-" + arm, "auto_model/urm", "image", sha, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO artifact_checks(id,pair_id,arm,commit_sha,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("check-observed-complete-" + arm, pair["id"], arm, sha, status, stamp, stamp),
            )
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,commit_match,review_status,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,1,'confirmed','passed',?,?)""",
                ("rec-observed-complete-" + arm, pair["id"], arm,
                 str(self.root / (arm + ".mp4")), sha, stamp, stamp),
            )
        self.db.execute(
            "UPDATE pairs SET status='completed',stage='completed',completed_at=? WHERE id=?",
            (stamp, pair["id"]),
        )
        self.service._quarantine_invalid_completed_pairs()
        current = self.db.one("SELECT status,stage,completed_at FROM pairs WHERE id=?", (pair["id"],))
        self.assertEqual(current, {"status": "completed", "stage": "completed", "completed_at": stamp})
        self.assertEqual(
            int((self.db.one("SELECT COUNT(*) count FROM recordings WHERE pair_id=?", (pair["id"],)) or {}).get("count") or 0),
            2,
        )

    def test_api_error_is_kept_as_evidence_and_later_completion_is_accepted(self):
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
        self.assertTrue(state["complete"])
        self.assertTrue(state["prompt_matches"])
        self.assertEqual(state["result"], "Finished after internal retry")
        self.assertIn("504", state["api_error"])
        self.assertFalse(hasattr(self.service.claude, "send_continue"))

    def test_native_turn_end_accepts_visible_progress_without_stop_reason(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-native-turn-end", "container_name": "container-native-turn-end"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Implemented the requested workflow and ran verification."}],
            }},
            {"type": "system", "subtype": "turn_duration"},
            {"type": "last-prompt"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["complete"])
        self.assertEqual(state["completion_mode"], "native_turn_end")
        self.assertIn("Implemented", state["result"])

    def test_tool_use_progress_text_is_not_treated_as_completion(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-tool-progress", "container_name": "container-tool-progress"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "Now let me inspect the remaining files."}],
            }},
            {"type": "assistant", "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "app.py"}}],
            }},
            {"type": "last-prompt"},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertFalse(state["complete"])
        self.assertEqual(state["result"], "Now let me inspect the remaining files.")

    def test_task_retired_by_false_completion_is_restored_to_pool(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        baseline = "b" * 40
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='failed',stage='replaced',baseline_sha=? WHERE id=?",
            (baseline, pair["id"]),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'failed',?,?)""",
            ("arm-false-complete", pair["id"], "A", "A", str(self.root), "container", "screen",
             "auto_model/urm", "image", stamp, stamp),
        )
        self.db.audit("claude.arm_completed", "arm_run", "arm-false-complete", {
            "arm": "A", "commit_sha": baseline, "checkpointedDelivery": True,
        })
        self.service._restore_false_completed_tasks()
        task = self.db.one("SELECT status,used_at FROM tasks WHERE id='task-1'")
        self.assertEqual(task, {"status": "ready", "used_at": None})

    def test_completed_mismatched_prompt_is_visible_to_monitor_for_targeted_rerun(self):
        prompt = "Build the requested project\n\nKeep every boundary condition."
        observed = "Keep every boundary condition."
        arm = {"id": "arm-mismatched-prompt", "container_name": "container-mismatched-prompt"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": observed}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Finished"}],
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
        self.assertTrue(state["complete"])
        self.assertFalse(state["prompt_matches"])
        self.assertEqual(state["observed_prompt"], observed)

    def test_prompt_is_sent_as_one_bracketed_paste(self):
        arm = {"id": "arm-paste", "screen_name": "screen-paste"}
        (self.service.claude.runtime_dir / arm["id"]).mkdir(parents=True)
        result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.object(self.service.claude, "_screen_running", return_value=True), \
             patch("pairwise_console.claude_runner.run_command", return_value=result) as command, \
             patch("pairwise_console.claude_runner.time.sleep"):
            self.service.claude.send_prompt(arm, "第一段\n\n第二段")
        calls = [call.args[0] for call in command.call_args_list]
        self.assertIn("\x1b[200~", calls[0])
        self.assertEqual(calls[1][-2:], ["readbuf", str(self.service.claude.runtime_dir / "arm-paste" / "prompt.txt")])
        self.assertEqual(calls[2][-2:], ["paste", "."])
        self.assertIn("\x1b[201~", calls[3])
        self.assertEqual(calls[4][-1], "\r")

    def test_monitor_routes_completed_prompt_mismatch_to_non_counting_trace_repair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-live-mismatch", pair["id"], "A", "A", str(self.root / "A"),
             "container-live-mismatch", "screen-live-mismatch", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": True, "result": "Finished", "api_error": "",
            "prompt_matches": False, "observed_prompt": "truncated prompt",
            "session_id": "session-mismatch", "prompt_id": "prompt-mismatch",
        }
        repaired = {"pairId": pair["id"], "restarted": ["A"]}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service, "_restart_trace_invalid_arms", return_value=repaired) as restart, \
             patch.object(self.service.claude, "export_and_stop") as export:
            result = self.service._monitor_arm(
                pair["id"], "arm-live-mismatch", "Build a hard project with Docker Compose",
            )
        self.assertEqual(result, repaired)
        restart.assert_called_once()
        export.assert_not_called()
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='arm-live-mismatch' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(event["event_type"], "claude.live_prompt_mismatch")

    def test_terminal_api_error_after_visible_progress_keeps_the_session_deliverable(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-api-turn-end", "container_name": "container-api-turn-end"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Implemented the core flow and verified the main path."}],
            }},
            {"type": "assistant", "isApiErrorMessage": True, "message": {
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: 504 Gateway Timeout"}],
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
        self.assertTrue(state["complete"])
        self.assertEqual(state["completion_mode"], "native_turn_end")
        self.assertIn("504", state["api_error"])
        self.assertIn("Implemented", state["result"])

    def test_native_turn_end_after_tool_result_is_complete_without_final_text(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-tool-turn-end", "container_name": "container-tool-turn-end"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "验收链路全部通过，现在更新 README。"},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "README.md"}},
            ]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "done"}]}},
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
        self.assertTrue(state["complete"])
        self.assertEqual(state["completion_mode"], "native_turn_end_after_tool")
        self.assertIn("验收链路全部通过", state["result"])

    def test_api_error_after_tool_progress_is_not_mistaken_for_completion(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-tool-api-error", "container_name": "container-tool-api-error"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "正在写测试。"},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "tests/test_api.py"}},
            ]}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: 504 Gateway Timeout"}],
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
        self.assertFalse(state["complete"])
        self.assertIn("504", state["api_error"])

    def test_monitor_does_not_restart_a_completed_session_that_contains_api_error(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,?,?)""",
            ("arm-api-recovered", pair["id"], "A", "A", str(self.root / "A"),
             "container-api-recovered", "screen-api-recovered", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": True, "result": "Finished after internal retry",
            "api_error": "API Error: 504 Gateway Timeout",
            "session_id": "session-recovered", "prompt_id": "prompt-recovered",
        }
        completed = {"id": "arm-api-recovered", "status": "completed"}
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "export_and_stop", return_value=self.root / "trace"), \
             patch.object(self.service, "_finish_checkpointed_arm", return_value=completed) as finish, \
             patch.object(self.service, "_handle_attempt_failure") as retry:
            result = self.service._monitor_arm(pair["id"], "arm-api-recovered", "Build a hard project with Docker Compose")
        self.assertEqual(result, completed)
        finish.assert_called_once_with(pair["id"], "arm-api-recovered")
        retry.assert_not_called()
        event = self.db.one(
            "SELECT event_type FROM audit_events WHERE entity_id='arm-api-recovered' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(event["event_type"], "claude.api_error_recovered")

    def test_terminal_api_error_is_queued_without_counting_a_development_failure(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        workspace = self.root / "api-error-wait"
        workspace.mkdir()
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,error_retry_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,2,1,?,?)""",
            ("arm-api-error-wait", pair["id"], "A", "A", str(workspace),
             "container-api-wait", "screen-api-wait", "auto_model/urm", "image",
             stamp, stamp, stamp),
        )
        state = {
            "complete": False, "api_error": "API Error: 504 Gateway Timeout",
            "activity_signature": "api-error-signature", "activity_summary": ["API Error: 504"],
        }

        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "has_business_code", return_value=False), \
             patch.object(self.service, "_handle_attempt_failure") as failure:
            self.service._monitor_arm(
                pair["id"], "arm-api-error-wait", "Build a hard project with Docker Compose",
            )
        failure.assert_not_called()
        arm = self.db.one(
            """SELECT status,attempt_no,error_retry_count,api_retry_count,api_retry_after
                 FROM arm_runs WHERE id='arm-api-error-wait'"""
        )
        self.assertEqual(arm["status"], "waiting_api_retry")
        self.assertEqual(arm["attempt_no"], 2)
        self.assertEqual(arm["error_retry_count"], 1)
        self.assertEqual(arm["api_retry_count"], 1)
        self.assertTrue(arm["api_retry_after"])
        self.assertEqual(self.db.one("SELECT status FROM pairs WHERE id=?", (pair["id"],))["status"],
                         "waiting_api_retry")
        event = self.db.one(
            """SELECT event_type,detail_json FROM audit_events
               WHERE entity_id='arm-api-error-wait' ORDER BY id DESC LIMIT 1"""
        )
        self.assertEqual(event["event_type"], "claude.api_retry_queued")
        detail = json.loads(event["detail_json"])
        self.assertFalse(detail["counts_toward_development_attempts"])
        self.assertFalse(detail["counts_toward_error_retries"])

    def test_monitor_replaces_task_after_second_identical_no_code_signature(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.set_setting("first_prompt_stop_minutes", 0)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,attempt_no,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'developing',?,2,?,?)""",
            ("arm-repeat-no-code", pair["id"], "A", "A", str(self.root / "A"),
             "container-repeat", "screen-repeat", "auto_model/urm", "image", stamp, stamp, stamp),
        )
        signature = "same-signature"
        self.db.audit("claude.no_code_timeout_signature", "arm_run", "arm-repeat-no-code", {
            "attempt": 1, "signature": signature, "summary": ["no-assistant-activity"],
        })
        state = {
            "complete": False, "api_error": "", "activity_signature": signature,
            "activity_summary": ["no-assistant-activity"],
        }
        with patch.object(self.service.claude, "trace_state", return_value=state), \
             patch.object(self.service.claude, "runtime_alive", return_value=True), \
             patch.object(self.service.claude, "has_business_code", return_value=False), \
             patch.object(self.service, "_handle_attempt_failure", return_value={"status": "failed"}) as failure:
            self.service._monitor_arm(
                pair["id"], "arm-repeat-no-code", "Build a hard project with Docker Compose",
            )
        self.assertTrue(failure.call_args.kwargs["early_replace"])
        self.assertIn("连续 2 次", failure.call_args.args[3])
        latest = self.db.one(
            "SELECT detail_json FROM audit_events WHERE entity_id='arm-repeat-no-code' ORDER BY id DESC LIMIT 1"
        )
        self.assertTrue(json.loads(latest["detail_json"])["matches_previous_attempt"])

    def test_system_turn_companion_is_not_treated_as_manual_followup(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-companion", "container_name": "container-companion"}
        companion = "[Your previous response had no visible output. Please continue and produce a user-visible response.]"
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "user", "isMeta": True, "turnCompanion": True,
             "message": {"content": companion}},
            {"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "Finished"}],
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
        self.assertTrue(state["complete"])
        self.assertFalse(state["followup_detected"])
        self.assertEqual(state["automatic_companion_count"], 1)
        self.assertEqual(state["automatic_companion_messages"], [companion])

    def test_real_user_followup_still_invalidates_session(self):
        prompt = "Build the requested project"
        arm = {"id": "arm-followup", "container_name": "container-followup"}
        events = [
            {"type": "user", "promptId": "prompt-1", "message": {"content": prompt}},
            {"type": "user", "message": {"content": "Please also change the database schema"}},
        ]

        def fake_copy(command, **_kwargs):
            snapshot = Path(command[-1])
            (snapshot / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8",
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("pairwise_console.claude_runner.run_command", side_effect=fake_copy):
            state = self.service.claude.trace_state(arm, prompt)
        self.assertTrue(state["followup_detected"])
        self.assertEqual(state["followup_text"], "Please also change the database schema")

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
        replace.assert_called_once_with(
            pair["id"], arm["id"], "container exited", "开发连续 3 次失败",
        )

    def test_api_errors_use_the_separate_retry_queue(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-transient-api", "pair_id": pair["id"], "attempt_no": 2, "arm": "A"}
        errors = (
            "API Error: Request rejected (429) · litellm.RateLimitError: max_parallel_requests",
            "API Error: 504 Gateway Timeout",
            "API Error: Unable to connect to API (UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)",
        )
        for error in errors:
            with self.subTest(error=error), \
                 patch.object(self.service, "_queue_api_retry",
                              return_value={**arm, "status": "waiting_api_retry"}) as queue, \
                 patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
                result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", error)
            self.assertEqual(result["id"], arm["id"])
            queue.assert_called_once_with(pair["id"], arm, error)
            replace.assert_not_called()

    def test_third_504_still_does_not_retire_the_pair(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.db.execute("UPDATE pairs SET status='running',stage='development' WHERE id=?", (pair["id"],))
        arm = {"id": "arm-third-504", "pair_id": pair["id"], "attempt_no": 3, "arm": "A"}
        error = "API Error: 504 Gateway Timeout"
        with patch.object(self.service, "_queue_api_retry",
                          return_value={**arm, "status": "waiting_api_retry"}) as queue, \
             patch.object(self.service, "_retire_pair_and_schedule_replacement") as replace:
            result = self.service._handle_attempt_failure(pair["id"], arm, "same prompt", error)
        self.assertEqual(result["id"], arm["id"])
        queue.assert_called_once_with(pair["id"], arm, error)
        replace.assert_not_called()

    def test_api_retry_cooldown_uses_provider_reset_and_bounded_backoff(self):
        current = datetime(2026, 9, 18, 19, 0, 0, tzinfo=timezone.utc)
        error = "API Error: 429 Rate limit. Limit resets at: 2026-09-18 19:00:20 UTC"
        self.assertEqual(self.service._api_retry_delay_seconds(error, 0, current), 60)
        self.assertEqual(self.service._api_retry_delay_seconds(error, 2, current), 240)
        self.assertEqual(
            self.service._api_retry_delay_seconds("API Error: 504 Gateway Timeout", 0, current), 120,
        )
        self.assertEqual(
            self.service._api_retry_delay_seconds("API Error: 504 Gateway Timeout", 4, current), 900,
        )

    def test_waiting_api_pair_reserves_capacity_and_due_retry_is_prioritized(self):
        self.db.set_setting("max_pairs_parallel", 1)
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        stamp = now_iso()
        self.db.execute(
            "UPDATE pairs SET status='waiting_api_retry',stage='development' WHERE id=?", (pair["id"],),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,prompt_sent_at,api_retry_count,api_retry_after,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'waiting_api_retry',NULL,1,?,?,?)""",
            ("arm-api-due", pair["id"], "A", "A", str(self.root / "api-due"),
             "container-api-due", "screen-api-due", "auto_model/urm", "image",
             "2000-01-01T00:00:00+00:00", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO tasks(id,source,task_type,title,prompt,difficulty,difficulty_evidence_json,
               fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("task-spare", "test", "zero_to_one", "spare hard task", "Build another hard project",
             "困难", '["跨模块状态"]', "spare-fingerprint", "ready", stamp, stamp),
        )
        submitted = []
        with patch.object(
            self.service, "_submit_monitor",
            side_effect=lambda operation, fn, *args: submitted.append(operation) or True,
        ), patch.object(self.service, "_submit_auto", return_value=True), \
             patch.object(self.service, "_schedule_refill_once") as refill:
            status = self.service._schedule_auto_pipeline_once()
        self.assertIn("api-retry-arm-api-due", submitted)
        self.assertEqual(status["activePairs"], 1)
        self.assertEqual(status["waitingApiArms"], 1)
        self.assertEqual(len(self.db.all("SELECT id FROM pairs")), 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-spare'")["status"], "ready")
        refill.assert_not_called()

    def test_only_twice_reproduced_hard_bug_converts_to_task(self):
        self.insert_ready_task()
        self.db.execute("UPDATE tasks SET stack='Python 3.13, FastAPI' WHERE id='task-1'")
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
        old_template = (
            "Concurrent commit loses update\n\n前置条件：two clients\n\n复现步骤：send two requests\n\n"
            "实际结果：one update disappears\n\n预期结果：both updates persist\n\n"
            "请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收。"
        )
        natural_prompt = (
            "两个客户端从同一版本同时提交更新时，服务端目前会让后到的提交覆盖先到结果，"
            "最终只能看到一份修改。这个现象已经用 send two requests 在两次独立环境中重复确认，"
            "实际输出都是 one update disappears，而产品约定要求 both updates persist。\n\n"
            "请沿着并发提交时的读取、版本判断和写入链路修正一致性处理，使两个互不冲突的更新都能保存；"
            "发生真实字段冲突时仍需返回现有冲突响应，不能通过串行覆盖来掩盖问题。不要改变当前对外接口和"
            "Docker Compose 启动方式。自动化验收需要在清洁环境中让两个客户端基于同一版本同步提交，"
            "核对两次请求结果与最终持久化内容，并再次运行已有冲突场景，确认 both updates persist 且旧行为没有回退。"
        )
        with patch.object(self.service.codex, "run", side_effect=[
            {"prompt": old_template, "evidenceUsed": ["preconditions", "steps", "actual"]},
            {"prompt": natural_prompt, "evidenceUsed": ["preconditions", "steps", "actual", "expected"]},
        ]) as generated:
            task = self.service.convert_bug_to_task("bug-1")
        self.assertEqual(task["task_type"], "bugfix")
        self.assertEqual(task["parent_pair_id"], pair["id"])
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["stack"], "Python 3.13, FastAPI")
        self.assertNotIn("前置条件：", task["prompt"])
        self.assertNotIn("复现步骤：", task["prompt"])
        self.assertNotIn("请修复该问题，保留现有 Docker Compose", task["prompt"])
        self.assertIn("send two requests", task["prompt"])
        self.assertIn("both updates persist", task["prompt"])
        self.assertEqual(generated.call_count, 2)
        self.assertIn("上一次草稿存在的问题", generated.call_args.args[1])
        self.assertFalse(self.service._retire_outdated_ready_bug_task(task))

        self.db.execute("UPDATE tasks SET prompt=? WHERE id=?", (old_template, task["id"]))
        self.assertEqual(self.service._retire_outdated_ready_bug_tasks(), 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id=?", (task["id"],))["status"], "rejected")
        self.assertEqual(self.db.one("SELECT status FROM bug_candidates WHERE id='bug-1'")["status"], "reproduced")

        with patch.object(self.service.codex, "run", return_value={
            "prompt": natural_prompt,
            "evidenceUsed": ["preconditions", "steps", "actual", "expected"],
        }):
            regenerated = self.service.convert_bug_to_task("bug-1")
        self.assertEqual(regenerated["id"], task["id"])
        self.assertEqual(regenerated["status"], "ready")
        self.assertEqual(regenerated["prompt"], natural_prompt)

    def test_arm_delivery_is_squashed_to_one_commit_on_baseline(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        workspace = self.root / "delivery-A"
        remote = self.root / "delivery.git"
        workspace.mkdir()
        run_command(["git", "init", "-b", "A"], cwd=workspace)
        run_command(["git", "config", "user.name", "Test"], cwd=workspace)
        run_command(["git", "config", "user.email", "test@example.com"], cwd=workspace)
        (workspace / "app.py").write_text("print('baseline')\n", encoding="utf-8")
        run_command(["git", "add", "app.py"], cwd=workspace)
        run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
        baseline = run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        (workspace / "app.py").write_text("print('first')\n", encoding="utf-8")
        run_command(["git", "commit", "-am", "first local commit"], cwd=workspace)
        (workspace / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")
        run_command(["git", "add", "feature.py"], cwd=workspace)
        run_command(["git", "commit", "-m", "second local commit"], cwd=workspace)
        run_command(["git", "init", "--bare", str(remote)])
        run_command(["git", "remote", "add", "origin", str(remote)], cwd=workspace)
        run_command(["git", "push", "origin", "%s:refs/heads/A" % baseline], cwd=workspace)
        stamp = now_iso()
        self.db.execute("UPDATE pairs SET baseline_sha=? WHERE id=?", (baseline, pair["id"]))
        self.db.execute(
            """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,remote_url,
               main_sha,a_sha,b_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?, 'ready',?,?)""",
            ("repo-squash", pair["id"], "owner", "repo", "public", str(self.root), str(remote),
             baseline, baseline, baseline, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
               model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'checkpointing',?,?)""",
            ("arm-squash-A", pair["id"], "A", "A", str(workspace), "container", "screen",
             "auto_model/urm", "image", stamp, stamp),
        )

        def local_git(args, cwd=None, timeout=180, check=True):
            return run_command(["git"] + list(args), cwd=cwd, timeout=timeout, check=check)

        with patch.object(self.service.git, "_github_git", side_effect=local_git):
            delivered = self.service.git.push_arm(pair["id"], "A")
        parent = run_command(["git", "rev-parse", delivered + "^"], cwd=workspace).stdout.strip()
        self.assertEqual(parent, baseline)
        self.assertEqual(run_command(["git", "rev-list", "--count", baseline + ".." + delivered], cwd=workspace).stdout.strip(), "1")
        self.assertEqual((workspace / "app.py").read_text(encoding="utf-8"), "print('first')\n")
        self.assertTrue((workspace / "feature.py").is_file())

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
