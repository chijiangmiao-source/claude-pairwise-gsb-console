import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pairwise_console.commands import run_command
from pairwise_console.config import load_config
from pairwise_console.db import Database, now_iso
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
        result = self.service.confirm_gsb(pair["id"], "A better", "A 的真实验收覆盖更完整，B 的异常路径仍有失败，因此 A 的交付更可靠。", "刘昱")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["gsb"]["confirmed_by"], "刘昱")

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
