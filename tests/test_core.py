import json
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pairwise_console.commands import run_command
from pairwise_console.config import load_config
from pairwise_console.db import Database, now_iso
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

    def test_pair_requires_ready_hard_task_and_creates_chain(self):
        self.insert_ready_task()
        pair = self.service.create_pair("task-1")
        self.assertEqual(pair["status"], "queued")
        self.assertEqual(pair["stage"], "repository")
        chain = self.db.one("SELECT * FROM project_chains WHERE id=?", (pair["chain_id"],))
        self.assertEqual(chain["followup_required"], 1)
        self.assertEqual(self.db.one("SELECT status FROM tasks WHERE id='task-1'")["status"], "used")

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
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,width,height,duration_seconds,status,created_at,updated_at)
                   VALUES(?,?,?,?,1280,720,30,'passed',?,?)""",
                ("rec-" + arm, pair["id"], arm, str(self.root / (arm + ".mov")), stamp, stamp),
            )
        result = self.service.confirm_gsb(pair["id"], "A better", "A 的真实验收覆盖更完整，B 的异常路径仍有失败，因此 A 的交付更可靠。", "刘昱")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(result["gsb"]["confirmed_by"], "刘昱")
        self.assertEqual(result["gsb"]["draft_verdict"], "Same")
        self.assertEqual(result["gsb"]["final_verdict"], "A better")
        self.assertEqual(result["delivery"]["status"], "ready_to_submit")

    def test_new_evidence_review_and_delivery_schema_is_available(self):
        recording_columns = {row["name"] for row in self.db.all("PRAGMA table_info(recordings)")}
        self.assertTrue({"commit_sha", "commit_match", "steps_json", "direct_url", "attempt_id", "capture_mode", "entry_url"} <= recording_columns)
        gsb_columns = {row["name"] for row in self.db.all("PRAGMA table_info(gsb_reviews)")}
        self.assertTrue({"draft_verdict", "final_verdict", "evidence_version"} <= gsb_columns)
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='gsb_rechecks'"))
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_submissions'"))
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
        self.service.confirm_gsb(pair["id"], "Same", "A 和 B 均完成主要要求，验收结果一致，最终交付没有影响使用的差异。", "刘昱")
        check = self.service.delivery_preflight(pair["id"])
        self.assertTrue(check["eligible"])
        self.assertTrue(check["warnings"])
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
            "verdict": "A better", "reason": "A 的实际交付更完整，B 的主流程存在可复现问题。",
            "readiness": "ready", "submission_status": "ready_to_submit",
        }])
        self.assertTrue(filename.endswith(".xlsx"))
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("项目编号", sheet)
        self.assertIn("pair-1", sheet)

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
