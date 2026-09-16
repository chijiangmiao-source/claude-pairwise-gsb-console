import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .analytics import dashboard
from .artifact import ArtifactChecker
from .claude_runner import ClaudeRunner
from .codex_runner import BUG_DISCOVERY_SCHEMA, CodexRunner, GSB_SCHEMA, TASK_SCHEMA
from .config import Config
from .db import Database, now_iso
from .gitops import GitOps
from .importer import fingerprint, import_historical_tasks
from .prompts import bug_discovery_prompt, feature_generation_prompt, gsb_prompt, task_generation_prompt, task_validation_prompt
from .recording import RecordingManager
from .commands import redact, run_command


VALIDATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["accepted", "difficulty", "difficultyEvidence", "banned", "duplicate", "baselineReady", "reason"],
    "properties": {
        "accepted": {"type": "boolean"},
        "difficulty": {"type": "string", "enum": ["简单", "中等", "困难", "地狱"]},
        "difficultyEvidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "banned": {"type": "boolean"},
        "duplicate": {"type": "boolean"},
        "baselineReady": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 500},
    },
}


class PairwiseService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.codex = CodexRunner(config, db)
        self.claude = ClaudeRunner(config, db)
        self.git = GitOps(config, db)
        self.artifacts = ArtifactChecker(db)
        self.recordings = RecordingManager(config, db)
        self.executor = ThreadPoolExecutor(max_workers=max(8, config.task_generation_max_parallel + 2))
        self._future_lock = threading.Lock()
        self._futures: Dict[str, Any] = {}
        self._scheduler_started = False
        self._seed_settings()
        self.db.execute(
            """UPDATE codex_jobs SET status='failed',error='服务重启时作业仍处于运行态，已安全释放以便重新排队',
               finished_at=?,updated_at=? WHERE status='running'""",
            (now_iso(), now_iso()),
        )

    def _seed_settings(self) -> None:
        defaults = {
            "codex_model": self.config.codex_model,
            "codex_default_effort": self.config.codex_default_effort,
            "codex_bug_effort": self.config.codex_bug_effort,
            "claude_model": self.config.claude_model,
            "claude_image": self.config.claude_image,
            "max_pairs_parallel": self.config.max_pairs_parallel,
            "task_generation_max_parallel": self.config.task_generation_max_parallel,
            "task_pool_min_ready": 6,
            "task_pool_target_ready": 12,
            "auto_refill_enabled": True,
            "auto_refill_interval_seconds": 60,
            "git_author_name": self.config.git_author_name,
            "git_author_email": self.config.git_author_email,
            "github_owner": self.config.github_owner,
            "github_visibility": self.config.github_visibility,
            "repository_prefix": self.config.repository_prefix,
            "first_prompt_warning_minutes": 15,
            "first_prompt_stop_minutes": 25,
            "terminal_idle_seconds": 120,
            "recording_width": 1280,
            "recording_height": 720,
            "recording_max_seconds": 90,
        }
        for key, value in defaults.items():
            if self.db.one("SELECT key FROM settings WHERE key=?", (key,)) is None:
                self.db.set_setting(key, value)

    def start_scheduler(self) -> None:
        if self._scheduler_started:
            return
        self._scheduler_started = True
        self._resume_active_monitors()
        threading.Thread(target=self._scheduler_loop, name="task-pool-refill", daemon=True).start()

    def _resume_active_monitors(self) -> None:
        """Reattach monitoring after the web service restarts.

        Claude runs in independent Docker/screen sessions, so a service update
        must not strand work that already received its prompt.
        """
        rows = self.db.all(
            """SELECT a.id arm_id,a.pair_id,t.prompt FROM arm_runs a
               JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
               WHERE a.prompt_sent_at IS NOT NULL
                 AND a.status IN ('running','developing','waiting_retry')
                 AND p.stage='development'"""
        )
        pair_ids = set()
        for row in rows:
            pair_ids.add(row["pair_id"])
            self._submit("monitor-" + row["arm_id"], self._monitor_arm, row["pair_id"], row["arm_id"], row["prompt"])
        for pair_id in pair_ids:
            self.db.execute("UPDATE pairs SET status='running',error='',updated_at=? WHERE id=?", (now_iso(), pair_id))

    def _scheduler_loop(self) -> None:
        # Let HTTP start first, then maintain the pool independently of A/B
        # development capacity.
        time.sleep(3)
        while True:
            try:
                if bool(self.db.setting("auto_refill_enabled", True)):
                    self._schedule_refill_once()
            except Exception as exc:
                self.db.audit("task.refill_scheduler_error", "scheduler", "task-pool", {"error": str(exc)[-2000:]})
            interval = max(30, int(self.db.setting("auto_refill_interval_seconds", 60)))
            time.sleep(interval)

    def _schedule_refill_once(self) -> None:
        ready = (self.db.one("SELECT COUNT(*) count FROM tasks WHERE status='ready' AND difficulty IN ('困难','地狱')") or {"count": 0})["count"]
        minimum = int(self.db.setting("task_pool_min_ready", 6))
        target = int(self.db.setting("task_pool_target_ready", 12))
        if ready >= minimum:
            return
        with self._future_lock:
            active = sum(1 for key, future in self._futures.items() if key.startswith("validate-") and not future.done())
            generation_active = any(key.startswith("generate-") and not future.done() for key, future in self._futures.items())
        capacity = max(0, int(self.db.setting("task_generation_max_parallel", 6)) - active)
        needed = max(0, target - ready)
        candidates = self.db.all(
            "SELECT id FROM tasks WHERE status='candidate' AND difficulty IN ('困难','地狱') ORDER BY created_at LIMIT ?",
            (min(capacity, needed),),
        )
        for row in candidates:
            self.validate_task_async(row["id"])
        if needed and capacity and not candidates and not generation_active:
            self.generate_tasks_async(min(capacity, needed), "zero_to_one")

    def preflight(self) -> Dict[str, Any]:
        return {
            "git": self.git.preflight(),
            "codex": self.codex.preflight(),
            "claude": self.claude.preflight(),
            "oldDb": {"ok": self.config.old_db_path.exists(), "path": str(self.config.old_db_path)},
        }

    def import_historical(self, limit: int = 500) -> Dict[str, int]:
        return import_historical_tasks(self.db, self.config.old_db_path, limit)

    def validate_task_async(self, task_id: str) -> str:
        operation = "validate-" + task_id
        self._submit(operation, self.validate_task, task_id)
        return operation

    def validate_task(self, task_id: str) -> Dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError("任务不存在")
        titles = self.db.all("SELECT id,title,substr(prompt,1,180) summary FROM tasks WHERE id<>? AND status IN ('ready','used') ORDER BY created_at DESC LIMIT 80", (task_id,))
        payload = dict(task)
        payload["acceptance"] = json.loads(task.get("acceptance_json") or "[]")
        prompt = task_validation_prompt(json.dumps(payload, ensure_ascii=False, indent=2), json.dumps(titles, ensure_ascii=False))
        result = self.codex.run("task_validation", prompt, VALIDATION_SCHEMA, task_id=task_id)
        accepted = bool(result["accepted"] and not result["banned"] and not result["duplicate"] and result["baselineReady"] and result["difficulty"] in ("困难", "地狱"))
        status = "ready" if accepted else "rejected"
        reason = "" if accepted else str(result.get("reason") or "未通过题目准入")
        self.db.execute(
            """UPDATE tasks SET status=?,difficulty=?,difficulty_evidence_json=?,rejection_reason=?,updated_at=? WHERE id=?""",
            (status, result["difficulty"], json.dumps(result["difficultyEvidence"], ensure_ascii=False), reason, now_iso(), task_id),
        )
        self.db.audit("task.validated", "task", task_id, {"status": status, "result": result})
        return {"taskId": task_id, "status": status, "result": result}

    def generate_tasks_async(self, count: int = 1, task_type: str = "zero_to_one") -> str:
        operation = "generate-" + uuid.uuid4().hex[:12]
        self._submit(operation, self.generate_tasks, count, task_type)
        return operation

    def generate_tasks(self, count: int = 1, task_type: str = "zero_to_one") -> Dict[str, Any]:
        batch_id = "batch-" + uuid.uuid4().hex[:16]
        count = min(20, max(1, int(count)))
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO generation_batches(id,status,requested_count,created_at,updated_at) VALUES(?,?,?,?,?)",
            (batch_id, "running", count, stamp, stamp),
        )
        accepted: List[str] = []
        rejected = 0
        try:
            for _ in range(count):
                existing = self.db.all("SELECT title,substr(prompt,1,220) summary FROM tasks ORDER BY created_at DESC LIMIT 100")
                prompt = task_generation_prompt(json.dumps(existing, ensure_ascii=False), task_type)
                result = self.codex.run("task_generation", prompt, TASK_SCHEMA)
                task_id = "task-" + uuid.uuid4().hex[:16]
                key = fingerprint(result["taskType"], result["prompt"], "")
                if self.db.one("SELECT id FROM tasks WHERE fingerprint=?", (key,)):
                    rejected += 1
                    continue
                stamp = now_iso()
                self.db.execute(
                    """INSERT INTO tasks(id,source,task_type,title,prompt,stack,acceptance_json,difficulty,
                       difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, "generated", result["taskType"], result["title"], result["prompt"], result["stack"],
                     json.dumps(result["acceptance"], ensure_ascii=False), result["difficulty"],
                     json.dumps(result["difficultyEvidence"], ensure_ascii=False), key, "candidate", stamp, stamp),
                )
                validation = self.validate_task(task_id)
                if validation["status"] == "ready":
                    accepted.append(task_id)
                else:
                    rejected += 1
            self.db.execute(
                """UPDATE generation_batches SET status='completed',generated_count=?,accepted_count=?,rejected_count=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (len(accepted) + rejected, len(accepted), rejected, now_iso(), now_iso(), batch_id),
            )
            return {"batchId": batch_id, "accepted": accepted, "rejected": rejected}
        except Exception as exc:
            self.db.execute("UPDATE generation_batches SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?", (str(exc)[-3000:], now_iso(), now_iso(), batch_id))
            raise

    def generate_followup_feature_async(self, pair_id: str) -> str:
        operation = "feature-" + pair_id
        self._submit(operation, self.generate_followup_feature, pair_id)
        return operation

    def generate_followup_feature(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["status"] != "completed":
            raise ValueError("只有已完成 GSB 的 Pair 才能生成 Feature 迭代")
        existing_followup = self.db.one(
            "SELECT * FROM tasks WHERE parent_pair_id=? AND task_type='feature' AND status IN ('candidate','ready','used') ORDER BY created_at DESC LIMIT 1",
            (pair_id,),
        )
        if existing_followup:
            return existing_followup
        selected = "B" if pair["winner"] == "B better" else "A"
        arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=? AND status='completed'", (pair_id, selected))
        check = self.db.one("SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND status='passed' ORDER BY created_at DESC LIMIT 1", (pair_id, selected))
        if not arm or not check or not arm.get("commit_sha"):
            raise ValueError("获胜产物缺少固定提交或 Docker 验收证据")
        workspace = Path(arm["workspace_path"])
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        files = [str(path.relative_to(workspace)) for path in sorted(workspace.rglob("*"))
                 if path.is_file() and ".git" not in path.parts and not any(part in (".venv", "node_modules", "__pycache__") for part in path.parts)][:160]
        readme = next((p for p in (workspace / "README.md", workspace / "README") if p.exists()), None)
        readme_text = readme.read_text(encoding="utf-8", errors="ignore")[:10000] if readme else ""
        summary = json.dumps({
            "selectedArm": selected, "commitSha": arm["commit_sha"], "files": files,
            "readme": readme_text, "dockerCheck": json.loads(check.get("checks_json") or "[]"),
        }, ensure_ascii=False)
        known = self.db.all("SELECT title,substr(prompt,1,220) summary FROM tasks ORDER BY created_at DESC LIMIT 100")
        last_error = ""
        for _ in range(3):
            result = self.codex.run(
                "task_generation",
                feature_generation_prompt(task.get("prompt", ""), summary, json.dumps(known, ensure_ascii=False)),
                TASK_SCHEMA, cwd=workspace, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
            if result.get("taskType") != "feature" or result.get("difficulty") not in ("困难", "地狱"):
                last_error = "生成结果不是困难或地狱 Feature"
                continue
            task_id = "task-" + uuid.uuid4().hex[:16]
            key = fingerprint("feature", result["prompt"], arm["commit_sha"])
            if self.db.one("SELECT id FROM tasks WHERE fingerprint=?", (key,)):
                last_error = "生成结果与已有 Feature 重复"
                continue
            stamp = now_iso()
            self.db.execute(
                """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,stack,acceptance_json,difficulty,
                   difficulty_evidence_json,baseline_path,baseline_repo_url,baseline_sha,parent_pair_id,fingerprint,
                   status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "generated_followup", pair_id, "feature", result["title"], result["prompt"], result["stack"],
                 json.dumps(result["acceptance"], ensure_ascii=False), result["difficulty"],
                 json.dumps(result["difficultyEvidence"], ensure_ascii=False), str(workspace),
                 (self.db.one("SELECT remote_url FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}).get("remote_url", ""),
                 arm["commit_sha"], pair_id, key, "candidate", stamp, stamp),
            )
            validation = self.validate_task(task_id)
            if validation["status"] == "ready":
                self.db.audit("feature.followup_ready", "task", task_id, {"source_pair_id": pair_id, "source_arm": selected, "baseline_sha": arm["commit_sha"]})
                return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)) or {}
            last_error = str(validation["result"].get("reason") or "Feature 准入未通过")
        raise RuntimeError("未能生成可进入 A/B 的困难 Feature：%s" % last_error)

    def create_pair(self, task_id: str) -> Dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError("任务不存在")
        if task["status"] != "ready" or task["difficulty"] not in ("困难", "地狱"):
            raise ValueError("只有已通过准入的困难或地狱任务才能创建 Pair")
        active_count = (self.db.one("SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running')") or {"count": 0})["count"]
        if active_count >= int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel)):
            raise ValueError("已达到 Pair 并发上限")
        pair_id = "pair-" + uuid.uuid4().hex[:16]
        if task["task_type"] == "zero_to_one":
            chain_id = "chain-" + uuid.uuid4().hex[:16]
            stamp = now_iso()
            self.db.execute(
                "INSERT INTO project_chains(id,root_task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (chain_id, task_id, "active", stamp, stamp),
            )
        else:
            parent = self.db.one("SELECT chain_id FROM pairs WHERE id=?", (task["parent_pair_id"],)) if task["parent_pair_id"] else None
            if parent:
                chain_id = parent["chain_id"]
            elif task["source"] == "legacy" and task["baseline_path"] and task["baseline_sha"]:
                # A reusable historical Feature has a verified task-time
                # baseline, but no Pair id in this new database. Keep it as a
                # self-contained imported chain rather than inventing lineage.
                chain_id = "chain-" + uuid.uuid4().hex[:16]
                stamp = now_iso()
                self.db.execute(
                    """INSERT INTO project_chains(id,root_task_id,status,followup_required,followup_completed,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (chain_id, task_id, "active", 0, 1, stamp, stamp),
                )
            else:
                raise ValueError("Feature 或 Bug 任务缺少来源项目链")
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO pairs(id,task_id,chain_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (pair_id, task_id, chain_id, "queued", "repository", stamp, stamp),
        )
        self.db.execute("UPDATE tasks SET status='used',locked_by=?,used_at=?,updated_at=? WHERE id=?", (pair_id, stamp, stamp, task_id))
        self.db.audit("pair.created", "pair", pair_id, {"task_id": task_id, "chain_id": chain_id})
        return self.pair_detail(pair_id)

    def prepare_pair_repository_async(self, pair_id: str) -> str:
        operation = "repo-" + pair_id
        self._submit(operation, self.prepare_pair_repository, pair_id)
        return operation

    def prepare_pair_repository(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        repo = self.git.create_pair_repository(pair, task)
        for arm in ("A", "B"):
            self.claude.prepare_arm(pair, arm, Path(repo["local_root"]) / "workspaces" / arm)
        self.db.execute("UPDATE pairs SET stage='ready_to_start',updated_at=? WHERE id=?", (now_iso(), pair_id))
        return self.pair_detail(pair_id)

    def start_pair_async(self, pair_id: str) -> str:
        operation = "start-" + pair_id
        self._submit(operation, self.start_pair, pair_id)
        return operation

    def start_pair(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["stage"] != "ready_to_start":
            raise ValueError("Pair 尚未完成仓库与 A/B 工作区准备")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        if not repo or repo["status"] != "ready":
            raise RuntimeError("Pair 仓库尚未准备完成")
        # Also migrates pre-fix queued rows whose workspace pointed directly at
        # the non-empty canonical clone.
        for arm in ("A", "B"):
            self.claude.prepare_arm(pair, arm, Path(repo["local_root"]) / "workspaces" / arm)
        runs = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        if len(runs) != 2:
            raise RuntimeError("A/B Arm 不完整")
        started: List[Dict[str, Any]] = []
        try:
            for run in runs:
                self.claude.reset_unsent_arm(run)
            runs = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
            for run in runs:
                self.claude.launch(run)
                started.append(run)
            for run in runs:
                self.claude.wait_until_ready(run)
            for run in runs:
                self.claude.materialize_repository(
                    run, Path(repo["local_root"]) / run["arm"], pair["baseline_sha"]
                )
            # Both containers are started before either receives the identical prompt.
            prompt = task["prompt"]
            for run in runs:
                refreshed = self.db.one("SELECT * FROM arm_runs WHERE id=?", (run["id"],)) or run
                self.claude.send_prompt(refreshed, prompt)
            self.db.execute(
                "UPDATE pairs SET status='running',stage='development',error='',started_at=?,updated_at=? WHERE id=?",
                (now_iso(), now_iso(), pair_id),
            )
            for run in runs:
                self._submit("monitor-" + run["id"], self._monitor_arm, pair_id, run["id"], prompt)
            return self.pair_detail(pair_id)
        except Exception as exc:
            self.db.execute("UPDATE pairs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[-3000:], now_iso(), pair_id))
            # Never destroy a successfully started arm here. Its terminal remains available for safe export/recovery.
            raise

    def generate_gsb_async(self, pair_id: str) -> str:
        operation = "gsb-" + pair_id
        self._submit(operation, self.generate_gsb, pair_id)
        return operation

    def discover_bugs_async(self, pair_id: str) -> str:
        operation = "bugs-" + pair_id
        self._submit(operation, self.discover_bugs, pair_id)
        return operation

    def discover_bugs(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["status"] != "completed":
            raise ValueError("只有 GSB 已人工确认的 Pair 才能进入后续 Bug 搜索")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        selected = "B" if pair["winner"] == "B better" else "A"
        arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, selected))
        check = self.db.one("SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND status='passed'", (pair_id, selected))
        if not arm or not check:
            raise ValueError("选定产物缺少已通过的 Docker 验收证据")
        result = self.codex.run(
            "bug_discovery",
            bug_discovery_prompt(task.get("prompt", ""), selected, arm.get("commit_sha", ""), json.dumps(check, ensure_ascii=False)),
            BUG_DISCOVERY_SCHEMA,
            cwd=Path(arm["workspace_path"]), pair_id=pair_id, task_id=pair["task_id"], timeout=2400,
        )
        created = []
        for candidate in result["candidates"]:
            candidate_id = "bug-" + uuid.uuid4().hex[:16]
            difficulty = candidate["difficulty"]
            status = "awaiting_reproduction" if difficulty in ("困难", "地狱") else "difficulty_rejected"
            stamp = now_iso()
            self.db.execute(
                """INSERT INTO bug_candidates(id,source_pair_id,source_arm,source_sha,title,preconditions,
                   reproduction_steps_json,reproduction_commands_json,actual_result,expected_result,difficulty,
                   difficulty_evidence_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, pair_id, selected, arm["commit_sha"], candidate["title"], candidate["preconditions"],
                 json.dumps(candidate["steps"], ensure_ascii=False), json.dumps(candidate["reproductionCommands"], ensure_ascii=False),
                 candidate["actual"], candidate["expected"],
                 difficulty, json.dumps(candidate["difficultyEvidence"], ensure_ascii=False), status, stamp, stamp),
            )
            created.append(candidate_id)
        self.db.audit("bug.discovery_completed", "pair", pair_id, {
            "arm": selected, "searchSummary": result["searchSummary"], "candidateIds": created,
        })
        return {"pairId": pair_id, "arm": selected, "searchSummary": result["searchSummary"], "candidateIds": created}

    def reproduce_bug_async(self, candidate_id: str) -> str:
        operation = "reproduce-" + candidate_id
        self._submit(operation, self.reproduce_bug, candidate_id)
        return operation

    def reproduce_bug(self, candidate_id: str) -> Dict[str, Any]:
        candidate = self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        if not candidate:
            raise KeyError("Bug 候选不存在")
        if candidate["status"] == "difficulty_rejected":
            raise ValueError("简单或中等 Bug 只保留记录，不能进入复现与 Pair")
        arm = self.db.one(
            "SELECT * FROM arm_runs WHERE pair_id=? AND arm=? AND commit_sha=?",
            (candidate["source_pair_id"], candidate["source_arm"], candidate["source_sha"]),
        )
        if not arm:
            raise ValueError("找不到候选对应的固定提交工作区")
        workspace = Path(arm["workspace_path"])
        compose = self.artifacts._compose_path(workspace)
        if not compose:
            raise ValueError("来源产物缺少 Compose 文件")
        commands = json.loads(candidate["reproduction_commands_json"] or "[]")
        if not commands:
            raise ValueError("候选缺少可执行复现命令")
        self.db.execute("UPDATE bug_candidates SET status='reproducing',error='',updated_at=? WHERE id=?", (now_iso(), candidate_id))
        attempts = []
        try:
            for attempt in (1, 2):
                project = "bugrep-%s-%d" % (candidate_id[-8:].lower(), attempt)
                attempt_result = {"attempt": attempt, "commands": [], "passed": True}
                run_command(["docker", "compose", "-p", project, "-f", str(compose), "down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180)
                up = run_command(["docker", "compose", "-p", project, "-f", str(compose), "up", "-d", "--build"], cwd=workspace, check=False, timeout=1200)
                attempt_result["startExitCode"] = up.returncode
                if up.returncode != 0:
                    attempt_result["passed"] = False
                    attempt_result["startOutput"] = redact(up.stderr or up.stdout)
                else:
                    time.sleep(3)
                    for spec in commands:
                        args = spec.get("composeArgs") if isinstance(spec, dict) else None
                        if not isinstance(args, list) or not args or not all(isinstance(x, str) and x for x in args):
                            raise ValueError("复现命令格式无效")
                        if args[0] not in ("exec", "run") or any(x in ("down", "rm", "kill", "stop") for x in args):
                            raise ValueError("复现命令只允许 docker compose exec 或 run")
                        result = run_command(
                            ["docker", "compose", "-p", project, "-f", str(compose)] + args,
                            cwd=workspace, check=False, timeout=600,
                        )
                        combined = (result.stdout + "\n" + result.stderr).strip()
                        expected_code = int(spec.get("expectedExitCode", 0))
                        marker = str(spec.get("expectedOutputContains") or "")
                        matched = result.returncode == expected_code and (not marker or marker in combined)
                        attempt_result["commands"].append({
                            "composeArgs": args, "exitCode": result.returncode, "expectedExitCode": expected_code,
                            "expectedOutputContains": marker, "matched": matched, "output": redact(combined),
                        })
                        attempt_result["passed"] = attempt_result["passed"] and matched
                run_command(["docker", "compose", "-p", project, "-f", str(compose), "down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180)
                attempts.append(attempt_result)
            reproduced = len(attempts) == 2 and all(item["passed"] for item in attempts)
            status = "reproduced" if reproduced else "not_reproduced"
            error = "" if reproduced else "两次清洁环境复现未得到一致的预期结果"
            self.db.execute(
                """UPDATE bug_candidates SET reproduce_count=?,reproduction_results_json=?,status=?,error=?,updated_at=? WHERE id=?""",
                (sum(1 for item in attempts if item["passed"]), json.dumps(attempts, ensure_ascii=False), status, error, now_iso(), candidate_id),
            )
            self.db.audit("bug.reproduction_finished", "bug_candidate", candidate_id, {"status": status, "attempts": attempts})
            return self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,)) or {}
        except Exception as exc:
            self.db.execute(
                "UPDATE bug_candidates SET status='reproduction_failed',reproduction_results_json=?,error=?,updated_at=? WHERE id=?",
                (json.dumps(attempts, ensure_ascii=False), str(exc)[-3000:], now_iso(), candidate_id),
            )
            raise

    def convert_bug_to_task(self, candidate_id: str) -> Dict[str, Any]:
        candidate = self.db.one("SELECT * FROM bug_candidates WHERE id=?", (candidate_id,))
        if not candidate:
            raise KeyError("Bug 候选不存在")
        if candidate["status"] != "reproduced" or candidate["reproduce_count"] < 2 or candidate["difficulty"] not in ("困难", "地狱"):
            raise ValueError("只有双次复现且难度为困难或地狱的 Bug 才能创建任务")
        arm = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (candidate["source_pair_id"], candidate["source_arm"])) or {}
        prompt = "%s\n\n前置条件：%s\n\n复现步骤：\n%s\n\n实际结果：%s\n\n预期结果：%s\n\n请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收。" % (
            candidate["title"], candidate["preconditions"],
            "\n".join("%d. %s" % (i + 1, step) for i, step in enumerate(json.loads(candidate["reproduction_steps_json"]))),
            candidate["actual_result"], candidate["expected_result"],
        )
        task_id = "task-" + uuid.uuid4().hex[:16]
        key = fingerprint("bugfix", prompt, candidate["source_sha"])
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,difficulty,difficulty_evidence_json,
               baseline_path,baseline_sha,parent_pair_id,fingerprint,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "bug_discovery", candidate_id, "bugfix", candidate["title"], prompt, candidate["difficulty"],
             candidate["difficulty_evidence_json"], arm.get("workspace_path", ""), candidate["source_sha"],
             candidate["source_pair_id"], key, "ready", stamp, stamp),
        )
        self.db.execute("UPDATE bug_candidates SET status='converted',updated_at=? WHERE id=?", (stamp, candidate_id))
        self.db.audit("bug.converted_to_task", "bug_candidate", candidate_id, {"task_id": task_id})
        return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)) or {}

    def start_recording(self, pair_id: str, arm: str, x: int = 0, y: int = 0) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["stage"] not in ("recording", "gsb_ready"):
            raise ValueError("Pair 尚未通过 Docker 产物验收")
        return self.recordings.start(pair_id, arm, x, y)

    def stop_recording(self, pair_id: str, arm: str) -> Dict[str, Any]:
        row = self.recordings.stop(pair_id, arm)
        # The process updates validation asynchronously. The UI refresh exposes
        # its recording/passed or recording/failed result.
        return row

    def refresh_recording_stage(self, pair_id: str) -> None:
        pair = self._pair(pair_id)
        if pair["stage"] != "recording":
            return
        rows = self.db.all("SELECT status FROM recordings WHERE pair_id=?", (pair_id,))
        if len(rows) == 2 and all(row["status"] == "passed" for row in rows):
            self.db.execute("UPDATE pairs SET stage='gsb_ready',updated_at=? WHERE id=?", (now_iso(), pair_id))

    def generate_gsb(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        self.refresh_recording_stage(pair_id)
        pair = self._pair(pair_id)
        if pair["stage"] != "gsb_ready":
            raise ValueError("A/B 两侧必须先通过 Docker 验收并完成合格录像")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        checks = self.db.all("SELECT * FROM artifact_checks WHERE pair_id=? ORDER BY arm", (pair_id,))
        recordings = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair_id,))
        by_arm = {arm["arm"]: arm for arm in arms}
        check_by_arm = {item["arm"]: item for item in checks}
        rec_by_arm = {item["arm"]: item for item in recordings}
        evidence = {}
        for arm in ("A", "B"):
            evidence[arm] = {
                "development": by_arm.get(arm, {}),
                "docker": check_by_arm.get(arm, {}),
                "recording": rec_by_arm.get(arm, {}),
            }
        result = self.codex.run(
            "gsb_review",
            gsb_prompt(task.get("prompt", ""), json.dumps(evidence["A"], ensure_ascii=False), json.dumps(evidence["B"], ensure_ascii=False)),
            GSB_SCHEMA, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
        )
        reason = re.sub(r"[`\r\n]+", " ", str(result["reason"])).strip()[:600]
        review_id = "gsb-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,evidence_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'draft',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET verdict=excluded.verdict,reason=excluded.reason,
                 evidence_json=excluded.evidence_json,status='draft',confirmed_by='',confirmed_at=NULL,updated_at=excluded.updated_at""",
            (review_id, pair_id, result["verdict"], reason, json.dumps(result["evidence"], ensure_ascii=False), stamp, stamp),
        )
        self.db.execute("UPDATE pairs SET status='review',stage='gsb_confirmation',updated_at=? WHERE id=?", (stamp, pair_id))
        return self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}

    def confirm_gsb(self, pair_id: str, verdict: str, reason: str, confirmed_by: str) -> Dict[str, Any]:
        if verdict not in ("A better", "Same", "B better"):
            raise ValueError("GSB 结论无效")
        clean = re.sub(r"[`\r\n]+", " ", reason).strip()
        if len(clean) < 20 or len(clean) > 600:
            raise ValueError("GSB 理由需为 20–600 个字符的单段文字")
        stamp = now_iso()
        self.db.execute(
            """UPDATE gsb_reviews SET verdict=?,reason=?,status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=?
               WHERE pair_id=?""",
            (verdict, clean, confirmed_by.strip() or "人工确认", stamp, stamp, pair_id),
        )
        self.db.execute("UPDATE pairs SET status='completed',stage='completed',winner=?,completed_at=?,updated_at=? WHERE id=?", (verdict, stamp, stamp, pair_id))
        pair = self._pair(pair_id)
        task = self.db.one("SELECT task_type FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        if task.get("task_type") in ("feature", "bugfix"):
            self.db.execute("UPDATE project_chains SET followup_completed=1,status='completed',completed_at=?,updated_at=? WHERE id=?", (stamp, stamp, pair["chain_id"]))
        self.db.audit("gsb.confirmed", "pair", pair_id, {"verdict": verdict, "confirmed_by": confirmed_by})
        return self.pair_detail(pair_id)

    def pair_detail(self, pair_id: str) -> Dict[str, Any]:
        self.refresh_recording_stage(pair_id)
        pair = self._pair(pair_id)
        pair["task"] = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],))
        pair["repository"] = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        pair["arms"] = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["checks"] = self.db.all("SELECT * FROM artifact_checks WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["recordings"] = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["gsb"] = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,))
        return pair

    def _monitor_arm(self, pair_id: str, arm_id: str, prompt: str) -> Dict[str, Any]:
        started = time.monotonic()
        pair_baseline = (self.db.one("SELECT baseline_sha FROM pairs WHERE id=?", (pair_id,)) or {}).get("baseline_sha", "")
        initial = self.db.one("SELECT prompt_sent_at FROM arm_runs WHERE id=?", (arm_id,)) or {}
        try:
            sent_at = datetime.fromisoformat(str(initial.get("prompt_sent_at") or ""))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            started -= max(0.0, (datetime.now(timezone.utc) - sent_at).total_seconds())
        except ValueError:
            pass
        warned = False
        last_api_error = ""
        last_resume_at = 0.0
        while True:
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))
            if not arm or arm["status"] not in ("developing", "running", "waiting_retry"):
                return arm or {}
            state = self.claude.trace_state(arm, prompt)
            if state.get("session_id") or state.get("prompt_id"):
                self.db.execute(
                    "UPDATE arm_runs SET session_id=?,prompt_id=?,updated_at=? WHERE id=?",
                    (state.get("session_id", ""), state.get("prompt_id", ""), now_iso(), arm_id),
                )
            error = str(state.get("api_error") or "")
            if error:
                lowered = error.casefold()
                retryable = any(token in lowered for token in ("429", "rate limit", "rate_limit", "certificate", "unable to connect", "connection"))
                if retryable and (error != last_api_error or time.monotonic() - last_resume_at >= 180):
                    self.db.execute("UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?", (error[-2000:], now_iso(), arm_id))
                    self.db.audit("claude.retryable_api_error", "arm_run", arm_id, {"error": error[-1000:]})
                    # Preserve the same terminal and session. A short delay also
                    # avoids immediately colliding with max_parallel_requests.
                    time.sleep(20 if "429" in lowered or "rate" in lowered else 8)
                    current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
                    self.claude.send_continue(current)
                    self.db.execute("UPDATE arm_runs SET status='developing',error='',updated_at=? WHERE id=?", (now_iso(), arm_id))
                    last_api_error, last_resume_at = error, time.monotonic()
                    continue
                if not retryable:
                    self.db.execute("UPDATE arm_runs SET status='failed',error=?,updated_at=? WHERE id=?", (error[-2000:], now_iso(), arm_id))
                    self._refresh_pair_after_arm(pair_id)
                    return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
            if state.get("complete"):
                result = str(state.get("result") or "")
                self.db.execute("UPDATE arm_runs SET status='checkpointing',result=?,updated_at=? WHERE id=?", (result, now_iso(), arm_id))
                trace_dir = self.claude.export_and_stop(arm)
                sha = self.git.push_arm(pair_id, arm["arm"])
                self.db.execute(
                    """UPDATE arm_runs SET status='completed',trace_path=?,commit_sha=?,result=?,finished_at=?,updated_at=? WHERE id=?""",
                    (str(trace_dir), sha, result, now_iso(), now_iso(), arm_id),
                )
                self.db.audit("claude.arm_completed", "arm_run", arm_id, {"arm": arm["arm"], "commit_sha": sha})
                self._refresh_pair_after_arm(pair_id)
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
            elapsed = time.monotonic() - started
            workspace = Path(arm["workspace_path"])
            has_code = self.claude.has_business_code(workspace, pair_baseline)
            if elapsed >= int(self.db.setting("first_prompt_warning_minutes", 15)) * 60 and not has_code and not warned:
                warned = True
                self.db.execute("UPDATE arm_runs SET warning_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), arm_id))
                self.db.audit("claude.no_code_warning", "arm_run", arm_id, {"elapsedSeconds": int(elapsed)})
            if elapsed >= int(self.db.setting("first_prompt_stop_minutes", 25)) * 60 and not has_code:
                # A no-output arm is stopped; the other arm remains independent.
                try:
                    self.claude.export_and_stop(arm)
                except Exception:
                    pass
                self.db.execute("UPDATE arm_runs SET status='failed',error='首轮超时且无代码产出',finished_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), arm_id))
                self._refresh_pair_after_arm(pair_id)
                return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or {}
            time.sleep(5)

    def _refresh_pair_after_arm(self, pair_id: str) -> None:
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        if len(arms) != 2:
            return
        statuses = {arm["status"] for arm in arms}
        if "failed" in statuses:
            self.db.execute("UPDATE pairs SET status='failed',stage='development_failed',error='A/B 至少一侧开发失败',updated_at=? WHERE id=?", (now_iso(), pair_id))
            return
        if statuses == {"completed"}:
            self.db.execute("UPDATE pairs SET status='running',stage='artifact_validation',updated_at=? WHERE id=?", (now_iso(), pair_id))
            self._submit("artifacts-" + pair_id, self._validate_pair_artifacts, pair_id)

    def _validate_pair_artifacts(self, pair_id: str) -> Dict[str, Any]:
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        results = []
        for arm in arms:
            results.append(self.artifacts.validate(pair_id, arm["arm"], Path(arm["workspace_path"]), arm["commit_sha"]))
        if all(item.get("status") == "passed" for item in results):
            self.db.execute("UPDATE pairs SET stage='recording',updated_at=? WHERE id=?", (now_iso(), pair_id))
        else:
            self.db.execute("UPDATE pairs SET status='failed',stage='artifact_failed',error='Docker 产物验收未通过',updated_at=? WHERE id=?", (now_iso(), pair_id))
        return {"pairId": pair_id, "checks": results}

    def operation(self, operation_id: str) -> Dict[str, Any]:
        with self._future_lock:
            future = self._futures.get(operation_id)
        if not future:
            return {"id": operation_id, "status": "unknown"}
        if not future.done():
            return {"id": operation_id, "status": "running"}
        try:
            return {"id": operation_id, "status": "completed", "result": future.result()}
        except Exception as exc:
            return {"id": operation_id, "status": "failed", "error": str(exc)}

    def _submit(self, operation: str, fn, *args) -> None:
        with self._future_lock:
            existing = self._futures.get(operation)
            if existing and not existing.done():
                return
            self._futures[operation] = self.executor.submit(fn, *args)

    def _pair(self, pair_id: str) -> Dict[str, Any]:
        pair = self.db.one("SELECT * FROM pairs WHERE id=?", (pair_id,))
        if not pair:
            raise KeyError("Pair 不存在")
        return pair
