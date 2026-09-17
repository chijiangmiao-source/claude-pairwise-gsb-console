import hashlib
import json
import mimetypes
import re
import shutil
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
from .classification import normalize_project_category, normalize_stack
from .codex_runner import (
    ACTUAL_DIFFICULTY_SCHEMA, BUG_DISCOVERY_SCHEMA, CodexRunner,
    GSB_RECHECK_SCHEMA, GSB_SCHEMA, TASK_SCHEMA,
)
from .config import Config, MAX_PAIR_PROJECTS
from .db import Database, now_iso
from .gitops import GitOps
from .importer import fingerprint, import_historical_tasks
from .prompts import (
    actual_difficulty_review_prompt, bug_discovery_prompt, feature_generation_prompt,
    gsb_prompt, gsb_recheck_prompt, task_generation_prompt, task_validation_prompt,
)
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

GSB_STEP_REFERENCE = re.compile(
    r"第\s*[一二三四五六七八九十百千万零〇\d]+"
    r"(?:\s*[、，,及和与]\s*[一二三四五六七八九十百千万零〇\d]+)*\s*步"
)


class PairwiseService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.codex = CodexRunner(config, db)
        self.claude = ClaudeRunner(config, db)
        self.git = GitOps(config, db)
        self.artifacts = ArtifactChecker(db)
        self.recordings = RecordingManager(config, db)
        # Arm monitors are intentionally long lived.  Keeping them in the same
        # pool as user actions can starve GSB rechecks (and every other short
        # operation) whenever all A/B slots are occupied.
        self.executor = ThreadPoolExecutor(
            max_workers=max(8, config.task_generation_max_parallel + 2),
            thread_name_prefix="pairwise-operation",
        )
        self.monitor_executor = ThreadPoolExecutor(
            max_workers=max(8, MAX_PAIR_PROJECTS * 2 + 2),
            thread_name_prefix="pairwise-monitor",
        )
        self._future_lock = threading.Lock()
        self._futures: Dict[str, Any] = {}
        self._scheduler_started = False
        self._automation_lock = threading.Lock()
        self._pair_creation_lock = threading.Lock()
        self._repository_locks_lock = threading.Lock()
        self._repository_locks: Dict[str, threading.Lock] = {}
        self._start_locks_lock = threading.Lock()
        self._start_locks: Dict[str, threading.Lock] = {}
        self._prompt_locks_lock = threading.Lock()
        self._prompt_locks: Dict[str, threading.Lock] = {}
        self._pair_completion_lock = threading.RLock()
        self._auto_retry_after: Dict[str, float] = {}
        self._artifact_retry_after: Dict[str, float] = {}
        self._seed_settings()
        self._quarantine_invalid_completed_pairs()
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
            "gsb_recheck_model": "gpt-6-astra",
            "gsb_recheck_effort": "high",
            "claude_model": self.config.claude_model,
            "claude_image": self.config.claude_image,
            "max_pairs_parallel": self.config.max_pairs_parallel,
            "ab_prompt_stagger_seconds": 30,
            "task_generation_max_parallel": self.config.task_generation_max_parallel,
            "task_pool_min_ready": 6,
            "task_pool_target_ready": 12,
            "auto_refill_enabled": True,
            "auto_refill_interval_seconds": 60,
            "auto_pipeline_enabled": False,
            "git_author_name": self.config.git_author_name,
            "git_author_email": self.config.git_author_email,
            "github_owner": self.config.github_owner,
            "github_visibility": self.config.github_visibility,
            "repository_prefix": self.config.repository_prefix,
            "first_prompt_warning_minutes": 15,
            "first_prompt_stop_minutes": 40,
            "development_max_attempts": 3,
            "terminal_idle_seconds": 120,
            "recording_width": 1280,
            "recording_height": 720,
            "recording_max_seconds": 90,
        }
        for key, value in defaults.items():
            if self.db.one("SELECT key FROM settings WHERE key=?", (key,)) is None:
                self.db.set_setting(key, value)
        # Upgrade the original shipped timeout while preserving any later
        # explicit customization made by an operator.
        if int(self.db.setting("first_prompt_stop_minutes", 40)) == 25:
            self.db.set_setting("first_prompt_stop_minutes", 40)
        if (str(self.db.setting("claude_image", self.config.claude_image))
                == "claude-eval-runtime:claude-2.1.269"
                and self.config.claude_image == "claude-eval-runtime:prepared-2.1.269"):
            self.db.set_setting("claude_image", self.config.claude_image)
        configured = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
        if configured > MAX_PAIR_PROJECTS or configured < 1:
            self.db.set_setting("max_pairs_parallel", MAX_PAIR_PROJECTS)

    def _quarantine_invalid_completed_pairs(self) -> None:
        """Stop historical Docker failures from being presented as deliveries."""
        rows = self.db.all(
            """SELECT DISTINCT p.id,p.chain_id FROM pairs p
                 JOIN arm_runs a ON a.pair_id=p.id
            LEFT JOIN artifact_checks c ON c.pair_id=p.id AND c.arm=a.arm AND c.commit_sha=a.commit_sha
                WHERE p.status='completed' AND COALESCE(c.status,'missing')<>'passed'"""
        )
        for row in rows:
            stamp = now_iso()
            error = "A/B Docker 产物验收未全部通过，原完成记录已拦截；失败侧需要从已交付提交返工"
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE pairs SET status='failed',stage='artifact_failed',winner='',completed_at=NULL,
                       error=?,updated_at=? WHERE id=?""",
                    (error, stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,updated_at=?
                       WHERE pair_id=?""",
                    (stamp, row["id"]),
                )
                conn.execute(
                    """UPDATE delivery_submissions SET status='blocked',error=?,updated_at=? WHERE pair_id=?""",
                    (error, stamp, row["id"]),
                )
                if row.get("chain_id"):
                    conn.execute(
                        """UPDATE project_chains SET status='active',followup_completed=0,completed_at=NULL,
                           updated_at=? WHERE id=?""",
                        (stamp, row["chain_id"]),
                    )
            self._invalidate_recordings(row["id"], reason=error)
            self.db.audit("artifact.invalid_delivery_quarantined", "pair", row["id"], {"error": error})

    def _invalidate_recordings(self, pair_id: str, arms=None, reason: str = "") -> int:
        """Remove current-delivery pointers while preserving attempt history and files."""
        selected = [str(arm) for arm in (arms or []) if str(arm) in ("A", "B")]
        where = "pair_id=?"
        params = [pair_id]
        if selected:
            where += " AND arm IN (%s)" % ",".join("?" for _ in selected)
            params.extend(selected)
        rows = self.db.all("SELECT id,arm,attempt_id FROM recordings WHERE " + where, tuple(params))
        if not rows:
            return 0
        self.db.execute("DELETE FROM recordings WHERE " + where, tuple(params))
        self.db.audit("recording.current_invalidated", "pair", pair_id, {
            "arms": [row["arm"] for row in rows],
            "attemptIds": [row.get("attempt_id", "") for row in rows],
            "reason": redact(reason)[-1000:],
            "historyPreserved": True,
        })
        return len(rows)

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
            self._submit_monitor(
                "monitor-" + row["arm_id"], self._monitor_arm,
                row["pair_id"], row["arm_id"], row["prompt"],
            )
        pending_retries = self.db.all(
            """SELECT a.id arm_id,a.pair_id,t.prompt FROM arm_runs a
               JOIN pairs p ON p.id=a.pair_id JOIN tasks t ON t.id=p.task_id
               WHERE a.prompt_sent_at IS NULL
                 AND a.status IN ('queued','waiting_retry','running')
                 AND p.stage='development'"""
        )
        for row in pending_retries:
            pair_ids.add(row["pair_id"])
            self._submit_monitor(
                "retry-recover-" + row["arm_id"], self._recover_pending_retry,
                row["pair_id"], row["arm_id"], row["prompt"],
            )
        for pair_id in pair_ids:
            self.db.execute("UPDATE pairs SET status='running',error='',updated_at=? WHERE id=?", (now_iso(), pair_id))

    def _recover_pending_retry(self, pair_id: str, arm_id: str, prompt: str) -> Dict[str, Any]:
        """Finish a fresh-session retry interrupted by a service restart."""
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))
        if not arm:
            return {}
        if arm.get("prompt_sent_at"):
            return self._monitor_arm(pair_id, arm_id, prompt)
        pair = self._pair(pair_id)
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        canonical = Path(str(repo.get("local_root") or "")) / str(arm["arm"])
        try:
            self.claude.reset_unsent_arm(arm)
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self.claude.launch(arm)
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self.claude.wait_until_ready(arm)
            self.claude.materialize_repository(arm, canonical, pair["baseline_sha"])
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            self._send_prompt_with_pair_stagger(pair_id, arm, prompt)
            self.db.audit("claude.pending_retry_recovered", "arm_run", arm_id, {
                "attempt": int(arm.get("attempt_no") or 1),
                "prompt_mode": "same_original_prompt_once",
            })
            return self._monitor_arm(pair_id, arm_id, prompt)
        except Exception as exc:
            failure = "恢复全新 Session 失败：%s" % redact(str(exc))
            current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
            return self._handle_attempt_failure(pair_id, current, prompt, failure)

    def _send_prompt_with_pair_stagger(self, pair_id: str, arm: Dict[str, Any], prompt: str) -> None:
        """Send one original prompt while keeping the two Arm sends apart.

        The per-Pair lock also covers concurrent A/B recovery threads. The
        persisted timestamp keeps the spacing after a service restart; one
        extra second compensates for the database timestamp's second-level
        precision so the real interval never becomes shorter than configured.
        """
        with self._prompt_locks_lock:
            prompt_lock = self._prompt_locks.setdefault(pair_id, threading.Lock())
        with prompt_lock:
            interval = max(0, min(300, int(self.db.setting("ab_prompt_stagger_seconds", 30))))
            other = self.db.one(
                """SELECT arm,prompt_sent_at FROM arm_runs
                   WHERE pair_id=? AND arm<>? AND prompt_sent_at IS NOT NULL
                   ORDER BY prompt_sent_at DESC LIMIT 1""",
                (pair_id, arm["arm"]),
            )
            waited = 0.0
            if interval and other and other.get("prompt_sent_at"):
                try:
                    sent_at = datetime.fromisoformat(str(other["prompt_sent_at"]).replace("Z", "+00:00"))
                    if sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=timezone.utc)
                    elapsed = max(0.0, (datetime.now(timezone.utc) - sent_at).total_seconds())
                    waited = max(0.0, interval + 1.0 - elapsed)
                except (TypeError, ValueError):
                    waited = float(interval)
                if waited:
                    time.sleep(waited)
            refreshed = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
            if refreshed.get("prompt_sent_at"):
                return
            self.claude.send_prompt(refreshed, prompt)
            self.db.audit("claude.original_prompt_sent", "arm_run", arm["id"], {
                "arm": arm["arm"],
                "configured_stagger_seconds": interval,
                "waited_seconds": round(waited, 3),
            })

    def _scheduler_loop(self) -> None:
        # Let HTTP start first. Task-pool refill has its own slower cadence;
        # the full-pipeline driver reacts quickly when a Pair finishes.
        time.sleep(3)
        next_refill = 0.0
        while True:
            try:
                current = time.monotonic()
                if bool(self.db.setting("auto_refill_enabled", True)) and current >= next_refill:
                    self._schedule_refill_once()
                    interval = max(30, int(self.db.setting("auto_refill_interval_seconds", 60)))
                    next_refill = current + interval
                if bool(self.db.setting("auto_pipeline_enabled", False)):
                    self._schedule_auto_pipeline_once()
            except Exception as exc:
                self.db.audit("automation.scheduler_error", "scheduler", "full-pipeline", {"error": str(exc)[-2000:]})
            time.sleep(5)

    def automation_status(self) -> Dict[str, Any]:
        target = MAX_PAIR_PROJECTS
        active = int((self.db.one(
            "SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')"
        ) or {"count": 0})["count"])
        ready = int((self.db.one(
            "SELECT COUNT(*) count FROM tasks WHERE status='ready' AND difficulty IN ('困难','地狱')"
        ) or {"count": 0})["count"])
        generating = int((self.db.one(
            "SELECT COUNT(*) count FROM generation_batches WHERE status='running'"
        ) or {"count": 0})["count"])
        stages = self.db.all(
            """SELECT stage,COUNT(*) count FROM pairs
               WHERE status IN ('queued','running','review') GROUP BY stage ORDER BY stage"""
        )
        return {
            "enabled": bool(self.db.setting("auto_pipeline_enabled", False)),
            "targetPairs": target,
            "activePairs": active,
            "readyTasks": ready,
            "generatingBatches": generating,
            "stages": stages,
        }

    def set_auto_pipeline(self, enabled: bool) -> Dict[str, Any]:
        self.db.set_setting("auto_pipeline_enabled", bool(enabled))
        if enabled:
            # The one-click mode has a fixed capacity: three Pair projects,
            # each using two independent Claude terminals.
            self.db.set_setting("max_pairs_parallel", MAX_PAIR_PROJECTS)
        self.db.audit(
            "automation.started" if enabled else "automation.stopped",
            "scheduler", "full-pipeline", {"targetPairs": MAX_PAIR_PROJECTS},
        )
        if enabled:
            self._schedule_auto_pipeline_once()
        return self.automation_status()

    def _submit_auto(self, operation: str, fn, *args) -> bool:
        """Submit an idempotent pipeline action with a small failure backoff."""
        with self._future_lock:
            existing = self._futures.get(operation)
            if existing and not existing.done():
                return False
            if time.monotonic() < self._auto_retry_after.get(operation, 0.0):
                return False

            def run_action():
                try:
                    result = fn(*args)
                    with self._future_lock:
                        self._auto_retry_after.pop(operation, None)
                    return result
                except Exception as exc:
                    with self._future_lock:
                        self._auto_retry_after[operation] = time.monotonic() + 30
                    self.db.audit("automation.action_failed", "operation", operation, {
                        "error": redact(str(exc))[-2000:],
                    })
                    raise

            self._futures[operation] = self.executor.submit(run_action)
            return True

    def _schedule_auto_pipeline_once(self) -> Dict[str, Any]:
        """Advance every active Pair and refill empty Pair slots up to three."""
        if not self._automation_lock.acquire(blocking=False):
            return self.automation_status()
        try:
            active_pairs = self.db.all(
                """SELECT * FROM pairs WHERE status IN ('queued','running','review')
                   ORDER BY created_at,id"""
            )
            recording_pairs: List[Dict[str, Any]] = []
            for pair in active_pairs:
                pair_id, stage = pair["id"], pair["stage"]
                if stage == "repository":
                    self._submit_auto("repo-" + pair_id, self.prepare_pair_repository, pair_id)
                elif stage == "ready_to_start":
                    self._submit_auto("start-" + pair_id, self.start_pair, pair_id)
                elif stage in ("development", "artifact_validation"):
                    self._schedule_completed_arm_validations(pair_id)
                elif stage == "difficulty_review":
                    self._submit_auto(
                        "difficulty-" + pair_id,
                        self.reassess_actual_difficulty,
                        pair_id,
                    )
                elif stage == "recording":
                    recording_pairs.append(pair)
                elif stage == "gsb_ready":
                    self._submit_auto("gsb-" + pair_id, self.generate_gsb, pair_id)
                elif stage == "gsb_confirmation":
                    review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}
                    if review.get("status") == "draft" and review.get("a_reason") and review.get("b_reason"):
                        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
                        self._submit_auto(
                            "confirm-gsb-" + pair_id, self.confirm_gsb, pair_id,
                            review.get("verdict", ""), review.get("a_reason", ""),
                            review.get("b_reason", ""), reviewer,
                        )

            self._schedule_next_automatic_recording(recording_pairs)

            active_count = int((self.db.one(
                "SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"])
            if active_count < MAX_PAIR_PROJECTS and self._resume_one_reusable_pair():
                active_count += 1
            while active_count < MAX_PAIR_PROJECTS:
                task = self.db.one(
                    """SELECT * FROM tasks WHERE status='ready' AND difficulty IN ('困难','地狱')
                       ORDER BY created_at,id LIMIT 1"""
                )
                if not task:
                    break
                pair = self.create_pair(task["id"])
                self._submit_auto("repo-" + pair["id"], self.prepare_pair_repository, pair["id"])
                active_count += 1

            # Existing approved questions are consumed first. Refill begins
            # only when no additional approved question can fill the target.
            if active_count < MAX_PAIR_PROJECTS:
                self._schedule_refill_once()
            return self.automation_status()
        finally:
            self._automation_lock.release()

    def _schedule_next_automatic_recording(self, pairs: List[Dict[str, Any]]) -> None:
        if self.db.one(
            "SELECT id FROM recording_attempts WHERE status IN ('starting','recording') LIMIT 1"
        ):
            return
        for pair in pairs:
            pair_id = pair["id"]
            for arm in ("A", "B"):
                run = self.db.one("SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm)) or {}
                commit_sha = str(run.get("commit_sha") or "")
                if not commit_sha:
                    continue
                recording = self.db.one(
                    """SELECT id FROM recordings WHERE pair_id=? AND arm=? AND status='passed'
                       AND commit_match=1 AND commit_sha=?""", (pair_id, arm, commit_sha),
                )
                if recording:
                    continue
                retry_window = self.db.one(
                    """SELECT created_at FROM audit_events
                       WHERE event_type='recording.retry_window_started' AND entity_id=?
                       ORDER BY id DESC LIMIT 1""", (pair_id,),
                ) or {}
                cutoff = str(retry_window.get("created_at") or "")
                failures = int((self.db.one(
                    """SELECT COUNT(*) count FROM recording_attempts WHERE pair_id=? AND arm=?
                       AND commit_sha=? AND interaction_mode<>'manual' AND status='failed'
                       AND (?='' OR created_at>=?)""",
                    (pair_id, arm, commit_sha, cutoff, cutoff),
                ) or {"count": 0})["count"])
                if failures >= 3:
                    stamp = now_iso()
                    self.db.execute(
                        """UPDATE pairs SET status='failed',stage='recording_failed',
                           error=?,updated_at=? WHERE id=?""",
                        ("自动录像连续 3 次失败，请人工检查后重新录制", stamp, pair_id),
                    )
                    self.db.audit("automation.recording_exhausted", "pair", pair_id, {"arm": arm})
                    break
                try:
                    self.start_recording(pair_id, arm, manual=False)
                except Exception as exc:
                    self.db.audit("automation.recording_start_failed", "pair", pair_id, {
                        "arm": arm, "error": redact(str(exc))[-2000:],
                    })
                return

    def _resume_one_reusable_pair(self) -> bool:
        # Reopening a preserved delivery consumes the same Pair slot as
        # creating a new Pair. Serialize both paths so the scheduler and the
        # replacement worker cannot claim the last free slot together.
        with self._pair_creation_lock:
            active_count = (self.db.one(
                "SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')"
            ) or {"count": 0})["count"]
            configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
            pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
            if active_count >= pair_limit:
                return False
            return self._resume_one_reusable_pair_locked()

    def _resume_one_reusable_pair_locked(self) -> bool:
        """Prefer finished code over consuming another task-pool entry.

        An artifact failure does not erase either Git commit or trace, so it
        can start a targeted repair from the delivered commit.  A recording
        failure is deliberately excluded: after three failed automatic
        attempts it must remain stopped until an operator starts a manual
        re-recording.  Reopening it here would create endless three-attempt
        retry windows.
        """
        row = self.db.one(
            """SELECT p.id,p.chain_id,p.stage FROM pairs p
               WHERE p.status='failed' AND p.stage='artifact_failed'
                 AND (SELECT COUNT(*) FROM arm_runs a
                      WHERE a.pair_id=p.id AND a.status='completed' AND a.commit_sha<>'')=2
               ORDER BY p.updated_at,p.created_at LIMIT 1"""
        )
        if not row:
            return False
        stamp = now_iso()
        next_stage = "artifact_validation"
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE pairs SET status='running',stage=?,error='',
                   winner='',completed_at=NULL,updated_at=? WHERE id=?""",
                (next_stage, stamp, row["id"]),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status='needs_review',error='',updated_at=?
                   WHERE pair_id=?""", (stamp, row["id"]),
            )
            if row.get("chain_id"):
                conn.execute(
                    """UPDATE project_chains SET status='active',followup_completed=0,
                       completed_at=NULL,updated_at=? WHERE id=?""",
                    (stamp, row["chain_id"]),
                )
        self.db.audit("artifact.revalidation_started", "pair", row["id"], {
            "reason": "reuse_existing_commits_before_targeted_repair",
            "preserved": ["A_commit", "B_commit", "traces"],
        })
        return True

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
            "browserRecording": self.recordings.preflight(),
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
                    """INSERT INTO tasks(id,source,task_type,title,prompt,stack,project_category,acceptance_json,difficulty,
                       difficulty_evidence_json,fingerprint,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, "generated", result["taskType"], result["title"], result["prompt"], normalize_stack(result["stack"]),
                     normalize_project_category(result.get("projectCategory"), result["stack"], result["prompt"]),
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
                feature_generation_prompt(task.get("prompt", ""), summary, json.dumps(known, ensure_ascii=False),
                                          task.get("project_category", "")),
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
                """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,stack,project_category,acceptance_json,difficulty,
                   difficulty_evidence_json,baseline_path,baseline_repo_url,baseline_sha,parent_pair_id,fingerprint,
                   status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "generated_followup", pair_id, "feature", result["title"], result["prompt"], normalize_stack(result["stack"]),
                 normalize_project_category(task.get("project_category"), result["stack"], result["prompt"]),
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
        # Capacity checks and insertion must be one operation.  The scheduler
        # and failed-task replacement path can otherwise both observe the same
        # free slot and create a fourth Pair concurrently.
        with self._pair_creation_lock:
            return self._create_pair_locked(task_id)

    def _create_pair_locked(self, task_id: str) -> Dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError("任务不存在")
        if task["status"] != "ready" or task["difficulty"] not in ("困难", "地狱"):
            raise ValueError("只有已通过准入的困难或地狱任务才能创建 Pair")
        active_count = (self.db.one("SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')") or {"count": 0})["count"]
        configured_limit = int(self.db.setting("max_pairs_parallel", self.config.max_pairs_parallel))
        pair_limit = max(1, min(MAX_PAIR_PROJECTS, configured_limit))
        if active_count >= pair_limit:
            raise ValueError("已达到 Pair 并发上限：最多 3 个 Pair（6 个 A/B 终端）")
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
        # Replacement and scheduler paths may discover the same repository
        # stage at once. Serialize work for this Pair so a second caller sees
        # the first caller's ready repository instead of running `git remote
        # add origin` against the same baseline concurrently.
        with self._repository_locks_lock:
            repository_lock = self._repository_locks.setdefault(pair_id, threading.Lock())
        with repository_lock:
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
        # Replacement workers and the automatic scheduler can both observe a
        # freshly prepared Pair. Only one of them may launch its two Arms.
        with self._start_locks_lock:
            start_lock = self._start_locks.setdefault(pair_id, threading.Lock())
        with start_lock:
            return self._start_pair_locked(pair_id)

    def _start_pair_locked(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["status"] not in ("queued", "running", "review"):
            raise ValueError("Pair 已停止，不能重新启动 A/B")
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
            # Both containers are ready before A receives the original prompt.
            # Mark development first so a service restart during the configured
            # A/B gap can recover the still-unsent Arm.
            self.db.execute(
                "UPDATE pairs SET status='running',stage='development',error='',started_at=?,updated_at=? WHERE id=?",
                (now_iso(), now_iso(), pair_id),
            )
            prompt = task["prompt"]
            for run in runs:
                self._send_prompt_with_pair_stagger(pair_id, run, prompt)
            for run in runs:
                self._submit_monitor("monitor-" + run["id"], self._monitor_arm, pair_id, run["id"], prompt)
            return self.pair_detail(pair_id)
        except Exception as exc:
            self.db.execute("UPDATE pairs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[-3000:], now_iso(), pair_id))
            # Never destroy a successfully started arm here. Its terminal remains available for safe export/recovery.
            raise

    def generate_gsb_async(self, pair_id: str) -> str:
        operation = "gsb-" + pair_id
        self._submit(operation, self.generate_gsb, pair_id)
        return operation

    def reassess_actual_difficulty_async(self, pair_id: str) -> str:
        operation = "difficulty-" + pair_id
        self._submit(operation, self.reassess_actual_difficulty, pair_id)
        return operation

    def repair_trace_prompt_async(self, pair_id: str, arm: str) -> str:
        operation = "trace-repair-%s-%s" % (pair_id, arm.lower())
        self._submit(operation, self.repair_trace_prompt, pair_id, arm)
        return operation

    def repair_trace_prompt(self, pair_id: str, arm: str) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        pair = self._pair(pair_id)
        task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        run = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm))
        if not run or run.get("status") != "completed":
            raise ValueError("只有已完成且轨迹不合格的 Arm 才能按原题面重跑")
        _, _, issues = self._inspect_trace(run, str(task.get("prompt") or ""))
        if not any("首轮 User Prompt" in issue for issue in issues):
            raise ValueError("该 Arm 没有首轮题面逐字不一致问题")
        return self._restart_trace_invalid_arms(
            pair_id, [run], str(task.get("prompt") or ""), issues,
        )

    def discover_bugs_async(self, pair_id: str) -> str:
        operation = "bugs-" + pair_id
        self._submit(operation, self.discover_bugs, pair_id)
        return operation

    def discover_bugs(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair["status"] != "completed":
            raise ValueError("只有 GSB 已确认的 Pair 才能进入后续 Bug 搜索")
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
        source_task = self.db.one(
            """SELECT t.* FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=?""",
            (candidate["source_pair_id"],),
        ) or {}
        prompt = "%s\n\n前置条件：%s\n\n复现步骤：\n%s\n\n实际结果：%s\n\n预期结果：%s\n\n请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收。" % (
            candidate["title"], candidate["preconditions"],
            "\n".join("%d. %s" % (i + 1, step) for i, step in enumerate(json.loads(candidate["reproduction_steps_json"]))),
            candidate["actual_result"], candidate["expected_result"],
        )
        task_id = "task-" + uuid.uuid4().hex[:16]
        key = fingerprint("bugfix", prompt, candidate["source_sha"])
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO tasks(id,source,source_id,task_type,title,prompt,project_category,difficulty,difficulty_evidence_json,
               baseline_path,baseline_sha,parent_pair_id,fingerprint,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "bug_discovery", candidate_id, "bugfix", candidate["title"], prompt,
             normalize_project_category(source_task.get("project_category"), source_task.get("stack"), source_task.get("prompt")),
             candidate["difficulty"],
             candidate["difficulty_evidence_json"], arm.get("workspace_path", ""), candidate["source_sha"],
             candidate["source_pair_id"], key, "ready", stamp, stamp),
        )
        self.db.execute("UPDATE bug_candidates SET status='converted',updated_at=? WHERE id=?", (stamp, candidate_id))
        self.db.audit("bug.converted_to_task", "bug_candidate", candidate_id, {"task_id": task_id})
        return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)) or {}

    def start_recording(self, pair_id: str, arm: str, x: int = 0, y: int = 0, manual: bool = False) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair.get("stage") in ("difficulty_review", "difficulty_rejected"):
            raise ValueError("实际难度复评通过后才能录制")
        return self.recordings.start(pair_id, arm, x, y, manual=manual)

    def stop_recording(self, pair_id: str, arm: str) -> Dict[str, Any]:
        row = self.recordings.stop(pair_id, arm)
        # The process updates validation asynchronously. The UI refresh exposes
        # its recording/passed or recording/failed result.
        return row

    def refresh_recording_stage(self, pair_id: str) -> None:
        pair = self._pair(pair_id)
        if pair["stage"] != "recording":
            return
        checks = self._current_artifact_checks(pair_id)
        if len(checks) != 2 or any(row.get("status") != "passed" for row in checks):
            return
        rows = self.db.all(
            """SELECT r.status FROM recordings r
                 JOIN arm_runs a ON a.pair_id=r.pair_id AND a.arm=r.arm AND a.commit_sha=r.commit_sha
                WHERE r.pair_id=? AND r.commit_match=1""",
            (pair_id,),
        )
        if len(rows) == 2 and all(row["status"] == "passed" for row in rows):
            self.db.execute("UPDATE pairs SET stage='gsb_ready',updated_at=? WHERE id=?", (now_iso(), pair_id))

    def _current_artifact_checks(self, pair_id: str) -> List[Dict[str, Any]]:
        return self.db.all(
            """SELECT c.* FROM artifact_checks c
                 JOIN arm_runs a ON a.pair_id=c.pair_id AND a.arm=c.arm AND a.commit_sha=c.commit_sha
                WHERE c.pair_id=? ORDER BY c.arm""",
            (pair_id,),
        )

    def _require_passed_artifacts(self, pair_id: str) -> List[Dict[str, Any]]:
        checks = self._current_artifact_checks(pair_id)
        by_arm = {row.get("arm"): row for row in checks}
        failed = [arm for arm in ("A", "B") if (by_arm.get(arm) or {}).get("status") != "passed"]
        if failed:
            raise ValueError("A/B 必须先通过 Docker 产物验收；未通过：" + "、".join(failed))
        return checks

    def _difficulty_arm_evidence(self, pair: Dict[str, Any], arm: Dict[str, Any],
                                 check: Dict[str, Any]) -> Dict[str, Any]:
        workspace = Path(str(arm.get("workspace_path") or ""))
        baseline = str(pair.get("baseline_sha") or "")
        commit = str(arm.get("commit_sha") or "")
        diff_range = "%s..%s" % (baseline, commit) if baseline and commit else commit
        code: Dict[str, Any] = {"range": diff_range, "stat": "", "files": []}
        if workspace.is_dir() and diff_range:
            stat = run_command(
                ["git", "diff", "--stat", "--find-renames", diff_range],
                cwd=workspace, timeout=60, check=False,
            )
            names = run_command(
                ["git", "diff", "--name-status", "--find-renames", diff_range],
                cwd=workspace, timeout=60, check=False,
            )
            code["stat"] = self._trace_value_text(stat.stdout or stat.stderr, 1800)
            code["files"] = [line[:300] for line in (names.stdout or "").splitlines()[:120]]
            if stat.returncode or names.returncode:
                code["error"] = self._trace_value_text(stat.stderr or names.stderr, 500)
        try:
            check_items = json.loads(str(check.get("checks_json") or "[]"))
        except ValueError:
            check_items = []
        compact_checks = []
        for item in check_items[:20]:
            compact_checks.append({
                "name": str(item.get("name") or ""),
                "passed": bool(item.get("passed")),
                "detail": self._trace_value_text(item.get("detail"), 500),
            })
        trace = self._trace_action_evidence(arm)
        trace_events = list(trace.get("events") or [])
        if len(trace_events) > 100:
            trace["events"] = trace_events[:30] + trace_events[-70:]
            trace["omittedForDifficultyReview"] = len(trace_events) - 100
        return {
            "arm": arm.get("arm"),
            "commit": commit,
            "developmentResult": self._trace_value_text(arm.get("result"), 1600),
            "codeChange": code,
            "docker": {
                "status": check.get("status"),
                "checks": compact_checks,
                "error": self._trace_value_text(check.get("error"), 600),
            },
            "traceEvidence": trace,
        }

    def reassess_actual_difficulty(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        if pair.get("status") == "completed" or pair.get("stage") == "completed":
            raise ValueError("已完成或已质检的数据不执行开发后难度回写")
        if pair.get("stage") != "difficulty_review":
            raise ValueError("只有 A/B 开发和 Docker 验收完成后才能复评实际难度")
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        if len(arms) != 2 or any(arm.get("status") != "completed" or not arm.get("commit_sha") for arm in arms):
            raise ValueError("A/B 两侧必须都已完成并形成提交")
        checks = self._require_passed_artifacts(pair_id)
        arm_by_name = {str(arm["arm"]): arm for arm in arms}
        check_by_name = {str(check["arm"]): check for check in checks}
        commits = {name: str(arm_by_name[name].get("commit_sha") or "") for name in ("A", "B")}
        existing = self.db.one("SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,))
        if existing and existing.get("a_commit_sha") == commits["A"] and existing.get("b_commit_sha") == commits["B"]:
            if existing.get("status") == "passed":
                self.db.execute(
                    "UPDATE pairs SET status='running',stage='recording',error='',updated_at=? WHERE id=?",
                    (now_iso(), pair_id),
                )
                return existing
            if existing.get("status") == "rejected":
                return existing
        review_id = str(existing.get("id") if existing else "") or "difficulty-" + uuid.uuid4().hex[:16]
        original = str(existing.get("original_difficulty") if existing else task.get("difficulty") or "")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO difficulty_reviews(
                 id,pair_id,original_difficulty,a_commit_sha,b_commit_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,'running',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET a_difficulty='',b_difficulty='',assessed_difficulty='',
                 reason='',evidence_json='[]',a_commit_sha=excluded.a_commit_sha,
                 b_commit_sha=excluded.b_commit_sha,status='running',error='',reviewed_at=NULL,
                 updated_at=excluded.updated_at""",
            (review_id, pair_id, original, commits["A"], commits["B"], stamp, stamp),
        )
        a_evidence = self._difficulty_arm_evidence(pair, arm_by_name["A"], check_by_name["A"])
        b_evidence = self._difficulty_arm_evidence(pair, arm_by_name["B"], check_by_name["B"])
        prompt = actual_difficulty_review_prompt(
            str(task.get("prompt") or ""), original,
            json.dumps(a_evidence, ensure_ascii=False),
            json.dumps(b_evidence, ensure_ascii=False),
        )
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        cwd = Path(str(repo.get("local_root") or arm_by_name["A"].get("workspace_path") or self.config.data_dir))
        try:
            result = self.codex.run(
                "difficulty_reassessment", prompt, ACTUAL_DIFFICULTY_SCHEMA,
                cwd=cwd, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
        except Exception as exc:
            error = redact(str(exc))[-2000:]
            self.db.execute(
                "UPDATE difficulty_reviews SET status='failed',error=?,updated_at=? WHERE pair_id=?",
                (error, now_iso(), pair_id),
            )
            self.db.execute(
                "UPDATE pairs SET status='running',stage='difficulty_review',error=?,updated_at=? WHERE id=?",
                ("实际难度复评失败，将自动重试：" + error, now_iso(), pair_id),
            )
            raise
        assessed = str(result.get("difficulty") or "")
        accepted = assessed in ("困难", "地狱")
        status = "passed" if accepted else "rejected"
        reason = str(result.get("reason") or "").strip()[:800]
        evidence = [str(value)[:300] for value in list(result.get("evidence") or [])[:10]]
        stamp = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE difficulty_reviews SET a_difficulty=?,b_difficulty=?,assessed_difficulty=?,
                   reason=?,evidence_json=?,status=?,error='',reviewed_at=?,updated_at=? WHERE pair_id=?""",
                (str(result.get("aDifficulty") or ""), str(result.get("bDifficulty") or ""),
                 assessed, reason, json.dumps(evidence, ensure_ascii=False), status, stamp, stamp, pair_id),
            )
            if accepted:
                conn.execute(
                    "UPDATE tasks SET difficulty=?,difficulty_evidence_json=?,updated_at=? WHERE id=?",
                    (assessed, json.dumps(evidence, ensure_ascii=False), stamp, pair["task_id"]),
                )
                conn.execute(
                    "UPDATE pairs SET status='running',stage='recording',error='',updated_at=? WHERE id=?",
                    (stamp, pair_id),
                )
            else:
                message = "实际难度复评为%s，低于困难/地狱准入线，已停止当前 Pair 并等待自动补位：%s" % (
                    assessed or "未知", reason,
                )
                conn.execute(
                    "UPDATE pairs SET status='failed',stage='difficulty_rejected',error=?,updated_at=? WHERE id=?",
                    (message[-3000:], stamp, pair_id),
                )
                conn.execute(
                    """INSERT INTO delivery_submissions(id,pair_id,status,error,created_at,updated_at)
                       VALUES(?,?,'discarded',?,?,?) ON CONFLICT(pair_id) DO UPDATE SET
                         status='discarded',error=excluded.error,updated_at=excluded.updated_at""",
                    ("delivery-" + uuid.uuid4().hex[:16], pair_id, message[-2000:], stamp, stamp),
                )
        self.db.audit(
            "difficulty.passed" if accepted else "difficulty.rejected",
            "pair", pair_id,
            {"original": original, "assessed": assessed, "a": result.get("aDifficulty"),
             "b": result.get("bDifficulty"), "commits": commits},
        )
        return self.db.one("SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,)) or {}

    def _current_process_events(self, pair_id: str) -> List[Dict[str, Any]]:
        arms = self.db.all(
            "SELECT id,prompt_sent_at FROM arm_runs WHERE pair_id=?", (pair_id,),
        )
        cutoffs = {str(arm["id"]): str(arm.get("prompt_sent_at") or "") for arm in arms}
        pair_cutoff = max(cutoffs.values(), default="")
        rows = self.db.all(
            """SELECT event_type,entity_id,detail_json,created_at FROM audit_events
               WHERE (entity_id=? OR entity_id LIKE ? OR detail_json LIKE ?)
                 AND (event_type LIKE 'claude.%' OR event_type LIKE 'artifact.%' OR event_type LIKE 'recording.%')
               ORDER BY id""",
            (pair_id, pair_id + "-%", "%" + pair_id + "%"),
        )
        current = []
        for row in rows:
            cutoff = cutoffs.get(str(row.get("entity_id") or ""), pair_cutoff)
            if not cutoff or str(row.get("created_at") or "") >= cutoff:
                current.append(row)
        return current

    @staticmethod
    def _trace_value_text(value: Any, limit: int = 700) -> str:
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif not isinstance(item, dict):
                    parts.append(str(item))
            text = " ".join(parts)
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value or "")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return text
        head = max(120, limit // 3)
        return text[:head] + " … " + text[-(limit - head - 3):]

    def _trace_action_evidence(self, arm: Dict[str, Any]) -> Dict[str, Any]:
        """Extract visible actions/results without exposing assistant reasoning."""
        trace_root = Path(str(arm.get("trace_path") or ""))
        if not trace_root.is_dir():
            return {"available": False, "events": []}
        files = sorted(trace_root.rglob("*.jsonl"))
        if len(files) != 1:
            return {"available": False, "events": [], "error": "轨迹文件数量不是 1"}
        events: List[Dict[str, Any]] = []
        try:
            lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return {"available": False, "events": [], "error": redact(str(exc))}
        for step, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    name = str(item.get("name") or "")
                    inputs = item.get("input") if isinstance(item.get("input"), dict) else {}
                    if name == "Bash":
                        detail = inputs.get("command") or ""
                    elif name in ("Read", "Write", "Edit"):
                        detail = inputs.get("file_path") or inputs.get("path") or ""
                    elif name in ("Glob", "Grep"):
                        detail = "%s %s" % (inputs.get("pattern") or "", inputs.get("path") or "")
                    else:
                        detail = inputs
                    events.append({
                        "step": step, "kind": "tool", "tool": name,
                        "detail": self._trace_value_text(detail, 600),
                    })
                elif item.get("type") == "tool_result":
                    result_text = self._trace_value_text(item.get("content"), 800)
                    if result_text:
                        events.append({
                            "step": step, "kind": "tool_result",
                            "isError": bool(item.get("is_error")), "detail": result_text,
                        })
        omitted = max(0, len(events) - 200)
        if omitted:
            events = events[:60] + events[-140:]
        return {
            "available": True, "traceFile": files[0].name,
            "events": events, "omittedEvents": omitted,
            "stepRule": "step 是 JSONL 内部记录号，只用于定位证据，公开评价不输出第几步",
        }

    def _bug_evidence(self, pair_id: str, arm: str) -> List[Dict[str, Any]]:
        return self.db.all(
            """SELECT title,preconditions,reproduction_steps_json,reproduction_commands_json,
                      reproduction_results_json,actual_result,expected_result,reproduce_count,
                      difficulty,status,error
                 FROM bug_candidates WHERE source_pair_id=? AND source_arm=? ORDER BY created_at""",
            (pair_id, arm),
        )

    @staticmethod
    def _clean_gsb_part(value: Any, limit: int) -> str:
        text = re.sub(r"[`\r\n]+", " ", str(value or "")).strip()
        # Keep the underlying action and result while removing internal JSONL
        # line numbers from public prose. Handle the common "failed at step X,
        # fixed at step Y" form first so the sentence remains natural.
        paired = re.compile(
            GSB_STEP_REFERENCE.pattern
            + r"(?P<middle>[^。；]{0,100}?)已(?:在|于)\s*"
            + GSB_STEP_REFERENCE.pattern
            + r"(?P<verb>修正|修复|修好|解决|通过|完成)"
        )

        def replace_pair(match: re.Match) -> str:
            middle = str(match.group("middle") or "").lstrip("的")
            return middle + "后来已" + str(match.group("verb") or "")

        text = paired.sub(replace_pair, text)
        text = re.sub(GSB_STEP_REFERENCE.pattern + r"\s*(?:及|和|与)\s*", "", text)
        text = GSB_STEP_REFERENCE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    @staticmethod
    def _compose_gsb_reason(a_reason: str, b_reason: str) -> str:
        return "A：%s B：%s" % (a_reason, b_reason)

    @staticmethod
    def _gsb_has_locator(value: str) -> bool:
        """Return whether a public reason contains one reviewable evidence locator."""
        text = str(value or "")
        patterns = (
            r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+",
            r"\b[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|sh|yml|yaml|json|toml|md)\b",
            r"\b(?:docker\s+compose|pytest|npm\s+(?:test|run)|pnpm\s+(?:test|run)|yarn\s+(?:test|run)|python3?\s+|curl\s+|git\s+)[^，。；]*",
            r"\b(?:[1-5]\d\d|[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b",
            r"(?:函数|方法|接口)\s*[A-Za-z_][A-Za-z0-9_]*",
            r"(?:报错|错误|冲突|失败)",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    @classmethod
    def _gsb_locator_issues(cls, a_reason: str, b_reason: str) -> List[str]:
        issues = []
        if not cls._gsb_has_locator(a_reason):
            issues.append("A 评价缺少可核对的具体证据（文件/函数、命令、接口状态或报错）")
        if not cls._gsb_has_locator(b_reason):
            issues.append("B 评价缺少可核对的具体证据（文件/函数、命令、接口状态或报错）")
        return issues

    @staticmethod
    def _gsb_conversational_issues(a_reason: str, b_reason: str) -> List[str]:
        """Flag narrow, mechanical patterns without penalizing useful detail."""
        issues: List[str] = []
        numbered_cases = re.compile(
            r"第\s*\d+\s*(?:[、，,]\s*\d+){2,}(?:\s*(?:至|到|-)\s*\d+)?\s*(?:项|条|次)?"
        )
        test_count_pile = re.compile(
            r"\d+\s*个(?:单元测试|单测|端到端测试|e2e)[^。；]{0,80}"
            r"\d+\s*个(?:单元测试|单测|端到端测试|e2e)",
            flags=re.IGNORECASE,
        )
        recording_seconds = re.compile(r"\d+(?:\.\d+)?\s*秒(?:钟)?(?:的)?录像")
        for label, value in (("A", a_reason), ("B", b_reason)):
            text = str(value or "")
            if GSB_STEP_REFERENCE.search(text):
                issues.append(label + " 评价包含轨迹步骤号，应改写为实际操作或验证场景")
            if numbered_cases.search(text):
                issues.append(label + " 评价机械罗列测试编号，应改写为实际验证的业务场景")
            if test_count_pile.search(text):
                issues.append(label + " 评价堆叠测试数量，应说明这些测试验证了什么")
            if recording_seconds.search(text):
                issues.append(label + " 评价使用录像时长支撑功能判断，录像时长只能说明文件合规")
            if "未见已发生的功能缺陷" in text:
                issues.append(label + " 评价使用生硬的无缺陷套话，应改成有证据支撑的自然判断")
        return issues

    def generate_gsb(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        self.refresh_recording_stage(pair_id)
        pair = self._pair(pair_id)
        if pair["stage"] != "gsb_ready":
            raise ValueError("A/B 两侧必须先通过 Docker 验收并完成合格录像")
        checks = self._require_passed_artifacts(pair_id)
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
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
                "traceEvidence": self._trace_action_evidence(by_arm.get(arm, {})),
                "discoveredBugs": self._bug_evidence(pair_id, arm),
            }
        evidence["processEvents"] = self._current_process_events(pair_id)
        review_prompt = gsb_prompt(
            task.get("prompt", ""),
            json.dumps(evidence["A"], ensure_ascii=False),
            json.dumps(evidence["B"], ensure_ascii=False),
            json.dumps(evidence["processEvents"], ensure_ascii=False),
        )
        result = self.codex.run(
            "gsb_review",
            review_prompt,
            GSB_SCHEMA, pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
        )
        a_reason = self._clean_gsb_part(result["aReason"], 300)
        b_reason = self._clean_gsb_part(result["bReason"], 300)
        locator_issues = self._gsb_locator_issues(a_reason, b_reason)
        style_issues = self._gsb_conversational_issues(a_reason, b_reason)
        if locator_issues or style_issues:
            correction = (
                review_prompt + "\n\n上一次输出需要修正：" + "；".join(locator_issues + style_issues)
                + "\n上一次 A 理由：" + a_reason + "\n上一次 B 理由：" + b_reason
                + "\n请只依据上面的真实证据重新生成。保留能支撑结论的证据，把轨迹步骤号和机械数字改写成业务场景；每段仍要有文件/函数、命令、接口状态或报错等真实定位。"
            )
            result = self.codex.run(
                "gsb_review", correction, GSB_SCHEMA,
                pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
            )
            a_reason = self._clean_gsb_part(result["aReason"], 300)
            b_reason = self._clean_gsb_part(result["bReason"], 300)
            locator_issues = self._gsb_locator_issues(a_reason, b_reason)
            if locator_issues:
                raise ValueError("GSB 自动纠正后仍缺少具体定位：" + "；".join(locator_issues))
        reason = self._compose_gsb_reason(a_reason, b_reason)
        review_id = "gsb-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        evidence_version = self.gsb_evidence_version(pair_id, result["verdict"], reason)
        self.db.execute(
            """INSERT INTO gsb_reviews(id,pair_id,verdict,reason,a_reason,b_reason,preference_reason,
               evidence_json,draft_verdict,draft_reason,evidence_version,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET verdict=excluded.verdict,reason=excluded.reason,
                 a_reason=excluded.a_reason,b_reason=excluded.b_reason,preference_reason=excluded.preference_reason,
                 evidence_json=excluded.evidence_json,draft_verdict=excluded.draft_verdict,
                 draft_reason=excluded.draft_reason,final_verdict='',final_reason='',
                 evidence_version=excluded.evidence_version,status='draft',confirmed_by='',confirmed_at=NULL,
                 updated_at=excluded.updated_at""",
            (review_id, pair_id, result["verdict"], reason, a_reason, b_reason, "",
             json.dumps(result["evidence"], ensure_ascii=False), result["verdict"], reason,
             evidence_version, stamp, stamp),
        )
        self.db.execute("UPDATE pairs SET status='review',stage='gsb_confirmation',updated_at=? WHERE id=?", (stamp, pair_id))
        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
        detail = self.confirm_gsb(pair_id, result["verdict"], a_reason, b_reason, reviewer)
        return detail.get("gsb") or {}

    def confirm_gsb(self, pair_id: str, verdict: str, a_reason: str, b_reason: str,
                    confirmed_by: str) -> Dict[str, Any]:
        if verdict not in ("A better", "Same", "B better"):
            raise ValueError("GSB 结论无效")
        self._require_passed_artifacts(pair_id)
        clean_a = self._clean_gsb_part(a_reason, 300)
        clean_b = self._clean_gsb_part(b_reason, 300)
        if len(clean_a) < 20 or len(clean_b) < 20:
            raise ValueError("A、B 评价均至少 20 个字符，并在两段中说明支持结论的依据")
        locator_issues = self._gsb_locator_issues(clean_a, clean_b)
        if locator_issues:
            raise ValueError("；".join(locator_issues))
        clean = self._compose_gsb_reason(clean_a, clean_b)
        stamp = now_iso()
        evidence_version = self.gsb_evidence_version(pair_id, verdict, clean)
        self.db.execute(
            """UPDATE gsb_reviews SET draft_verdict=CASE WHEN draft_verdict='' THEN verdict ELSE draft_verdict END,
               draft_reason=CASE WHEN draft_reason='' THEN reason ELSE draft_reason END,
               verdict=?,reason=?,a_reason=?,b_reason=?,preference_reason=?,
               final_verdict=?,final_reason=?,evidence_version=?,
               status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=?
               WHERE pair_id=?""",
            (verdict, clean, clean_a, clean_b, "", verdict, clean, evidence_version,
             confirmed_by.strip() or "人工确认", stamp, stamp, pair_id),
        )
        self.db.execute("UPDATE pairs SET status='completed',stage='completed',winner=?,completed_at=?,updated_at=? WHERE id=?", (verdict, stamp, stamp, pair_id))
        pair = self._pair(pair_id)
        task = self.db.one("SELECT task_type FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        if task.get("task_type") in ("feature", "bugfix"):
            self.db.execute("UPDATE project_chains SET followup_completed=1,status='completed',completed_at=?,updated_at=? WHERE id=?", (stamp, stamp, pair["chain_id"]))
        submission_id = "delivery-" + uuid.uuid4().hex[:16]
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,created_at,updated_at)
               VALUES(?,?,'ready_to_submit',?,?)
               ON CONFLICT(pair_id) DO UPDATE SET status='ready_to_submit',error='',updated_at=excluded.updated_at""",
            (submission_id, pair_id, stamp, stamp),
        )
        self.db.audit("gsb.confirmed", "pair", pair_id, {"verdict": verdict, "confirmed_by": confirmed_by})
        return self.pair_detail(pair_id)

    def _gsb_evidence_bundle(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        arms = self.db.all(
            """SELECT arm,model,image_id,status,session_id,prompt_id,trace_path,commit_sha,result,
               warning_at,error,prompt_sent_at,finished_at FROM arm_runs WHERE pair_id=? ORDER BY arm""",
            (pair_id,),
        )
        for arm in arms:
            arm["traceEvidence"] = self._trace_action_evidence(arm)
            arm["discoveredBugs"] = self._bug_evidence(pair_id, str(arm.get("arm") or ""))
        return {
            "pair": {key: pair.get(key) for key in ("id", "task_id", "chain_id", "baseline_sha")},
            "task": {key: task.get(key) for key in ("title", "task_type", "difficulty", "prompt", "acceptance_json")},
            "arms": arms,
            "checks": self.db.all(
                """SELECT arm,commit_sha,status,checks_json,error,started_at,finished_at
                   FROM artifact_checks WHERE pair_id=? ORDER BY arm""",
                (pair_id,),
            ),
            "recordings": self.db.all(
                """SELECT id,arm,commit_sha,sha256,width,height,duration_seconds,status,commit_match,error,
                   started_at,finished_at FROM recordings WHERE pair_id=? ORDER BY arm""",
                (pair_id,),
            ),
            "processEvents": self._current_process_events(pair_id),
        }

    def gsb_evidence_version(self, pair_id: str, verdict: str = "", reason: str = "") -> str:
        payload = self._gsb_evidence_bundle(pair_id)
        payload["publicVerdict"] = verdict
        payload["publicReason"] = reason
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def recheck_gsb_async(self, pair_id: str) -> str:
        operation = "gsb-recheck-" + pair_id
        self._submit(operation, self._recheck_gsb, pair_id)
        return operation

    def _recheck_gsb(self, pair_id: str) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,))
        if not review:
            raise ValueError("尚未生成 GSB 草稿")
        verdict = str(review.get("verdict") or "")
        a_reason = str(review.get("a_reason") or "")
        b_reason = str(review.get("b_reason") or "")
        reason = self._compose_gsb_reason(a_reason, b_reason)
        evidence = self._gsb_evidence_bundle(pair_id)
        version = self.gsb_evidence_version(pair_id, verdict, reason)
        model = str(self.db.setting("gsb_recheck_model", "gpt-6-astra"))
        effort = str(self.db.setting("gsb_recheck_effort", "high"))
        recheck_prompt = gsb_recheck_prompt(
            str(evidence["task"].get("prompt") or ""), verdict, a_reason, b_reason,
            json.dumps(evidence, ensure_ascii=False),
        )
        result = self.codex.run(
            "gsb_recheck",
            recheck_prompt,
            GSB_RECHECK_SCHEMA,
            pair_id=pair_id,
            task_id=pair["task_id"],
            timeout=1800,
            model_override=model,
            effort_override=effort,
        )
        suggested_a = self._clean_gsb_part(result["suggestedAReason"], 300)
        suggested_b = self._clean_gsb_part(result["suggestedBReason"], 300)
        source_style_issues = self._gsb_conversational_issues(a_reason, b_reason)
        locator_issues = self._gsb_locator_issues(suggested_a, suggested_b)
        suggestion_style_issues = self._gsb_conversational_issues(suggested_a, suggested_b)
        if locator_issues or source_style_issues or suggestion_style_issues:
            correction = (
                recheck_prompt + "\n\n当前原评价或上一次建议需要修正："
                + "；".join(source_style_issues + locator_issues + suggestion_style_issues)
                + "\n上一次建议 A 理由：" + suggested_a + "\n上一次建议 B 理由：" + suggested_b
                + "\n请重新复检。保留所有影响结论的证据，把轨迹步骤号和无意义数字改写成实际操作或业务场景，并确保两段各自包含文件/函数、命令、接口状态或报错等可核对证据。"
            )
            result = self.codex.run(
                "gsb_recheck", correction, GSB_RECHECK_SCHEMA,
                pair_id=pair_id, task_id=pair["task_id"], timeout=1800,
                model_override=model, effort_override=effort,
            )
            suggested_a = self._clean_gsb_part(result["suggestedAReason"], 300)
            suggested_b = self._clean_gsb_part(result["suggestedBReason"], 300)
            locator_issues = self._gsb_locator_issues(suggested_a, suggested_b)
            suggestion_style_issues = self._gsb_conversational_issues(suggested_a, suggested_b)
        result_status = str(result["status"])
        result_issues = [str(item) for item in result.get("issues", [])]
        if result_status != "fact_conflict" and (source_style_issues or locator_issues or suggestion_style_issues):
            result_status = "suggested_revision"
        for issue in source_style_issues + locator_issues + suggestion_style_issues:
            if issue not in result_issues:
                result_issues.append(issue)
        suggested_reason = self._compose_gsb_reason(suggested_a, suggested_b)
        latest_job = self.db.one(
            "SELECT id FROM codex_jobs WHERE pair_id=? AND job_type='gsb_recheck' ORDER BY created_at DESC LIMIT 1",
            (pair_id,),
        ) or {}
        recheck_id = "recheck-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO gsb_rechecks(id,pair_id,evidence_version,input_verdict,input_reason,result_status,
               suggested_verdict,suggested_reason,suggested_a_reason,suggested_b_reason,
               suggested_preference_reason,issues_json,evidence_refs_json,model,reasoning_effort,
               codex_job_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (recheck_id, pair_id, version, verdict, reason, result_status, result["suggestedVerdict"],
             suggested_reason, suggested_a, suggested_b, "",
             json.dumps(result_issues, ensure_ascii=False),
             json.dumps(result["evidenceRefs"], ensure_ascii=False), model, effort,
             str(latest_job.get("id") or ""), stamp),
        )
        self.db.audit("gsb.rechecked", "pair", pair_id, {"recheck_id": recheck_id, "status": result_status, "model": model, "effort": effort})
        recheck = self.db.one("SELECT * FROM gsb_rechecks WHERE id=?", (recheck_id,)) or {}
        recheck.pop("suggested_preference_reason", None)
        return recheck

    def apply_gsb_recheck(self, pair_id: str, recheck_id: str) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM gsb_rechecks WHERE id=? AND pair_id=?", (recheck_id, pair_id))
        if not row:
            raise KeyError("复检记录不存在")
        review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,)) or {}
        current_reason = self._compose_gsb_reason(
            str(review.get("a_reason") or ""), str(review.get("b_reason") or "")
        )
        current_version = self.gsb_evidence_version(
            pair_id, str(review.get("verdict") or ""), current_reason
        )
        if row["evidence_version"] != current_version:
            same_input = (
                str(row.get("input_verdict") or "") == str(review.get("verdict") or "")
                and str(row.get("input_reason") or "") == current_reason
            )
            if not same_input or self._recheck_source_changed_since(row):
                raise ValueError("公开理由或证据已经变化，请重新复检")
        verdict = row["suggested_verdict"]
        a_reason = row.get("suggested_a_reason") or ""
        b_reason = row.get("suggested_b_reason") or ""
        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
        self.confirm_gsb(pair_id, verdict, a_reason, b_reason, reviewer)
        stamp = now_iso()
        applied_reason = self._compose_gsb_reason(a_reason, b_reason)
        self.db.execute(
            """UPDATE gsb_rechecks SET evidence_version=?,applied_at=?,applied_by=? WHERE id=?""",
            (self.gsb_evidence_version(pair_id, verdict, applied_reason), stamp, reviewer, recheck_id),
        )
        self.db.audit("gsb.recheck_applied", "pair", pair_id, {
            "recheck_id": recheck_id, "applied_by": reviewer,
        })
        return self.pair_detail(pair_id)

    def _recheck_source_changed_since(self, recheck: Dict[str, Any]) -> bool:
        """Support pre-fix rechecks without accepting genuinely stale evidence."""
        job = self.db.one("SELECT created_at FROM codex_jobs WHERE id=?", (recheck.get("codex_job_id") or "",)) or {}
        since = str(job.get("created_at") or recheck.get("created_at") or "")
        if not since:
            return True
        pair_id = str(recheck.get("pair_id") or "")
        source_queries = (
            ("SELECT 1 FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=? AND t.updated_at>? LIMIT 1", (pair_id, since)),
            ("SELECT 1 FROM arm_runs WHERE pair_id=? AND updated_at>? LIMIT 1", (pair_id, since)),
            ("SELECT 1 FROM artifact_checks WHERE pair_id=? AND updated_at>? LIMIT 1", (pair_id, since)),
            ("SELECT 1 FROM recordings WHERE pair_id=? AND updated_at>? LIMIT 1", (pair_id, since)),
            ("""SELECT 1 FROM audit_events WHERE created_at>?
                  AND (entity_id=? OR entity_id LIKE ? OR detail_json LIKE ?)
                  AND (event_type LIKE 'claude.%' OR event_type LIKE 'artifact.%' OR event_type LIKE 'recording.%')
                  LIMIT 1""", (since, pair_id, pair_id + "-%", "%" + pair_id + "%")),
        )
        return any(self.db.one(sql, params) is not None for sql, params in source_queries)

    def apply_latest_gsb_rechecks(self, pair_ids: List[str]) -> Dict[str, Any]:
        results = []
        for pair_id in dict.fromkeys(pair_ids):
            latest = self.db.one(
                "SELECT * FROM gsb_rechecks WHERE pair_id=? ORDER BY created_at DESC LIMIT 1", (pair_id,)
            )
            if not latest:
                results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "尚未完成复检"})
                continue
            if latest.get("applied_at"):
                results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "最新建议已经应用"})
                continue
            if latest.get("result_status") == "passed":
                results.append({"pair_id": pair_id, "outcome": "skipped", "reason": "复检已通过，无需应用"})
                continue
            try:
                self.apply_gsb_recheck(pair_id, latest["id"])
                results.append({"pair_id": pair_id, "outcome": "applied", "recheck_id": latest["id"]})
            except Exception as exc:
                results.append({"pair_id": pair_id, "outcome": "failed", "error": redact(str(exc))})
        return {
            "results": results,
            "applied": sum(item["outcome"] == "applied" for item in results),
            "skipped": sum(item["outcome"] == "skipped" for item in results),
            "failed": sum(item["outcome"] == "failed" for item in results),
        }

    def delivery_preflight(self, pair_id: str, include_platform: bool = False) -> Dict[str, Any]:
        detail = self.pair_detail(pair_id)
        blockers: List[str] = []
        warnings: List[str] = []
        repository = detail.get("repository") or {}
        if repository and str(repository.get("visibility") or "").casefold() != "public":
            blockers.append("GitHub 仓库不是公开仓库，SOLO-QA 无法核验分支与提交")
        arms = {row["arm"]: row for row in detail.get("arms", [])}
        checks = {row["arm"]: row for row in detail.get("checks", [])}
        recs = {row["arm"]: row for row in detail.get("recordings", [])}
        for arm in ("A", "B"):
            item = arms.get(arm) or {}
            if not item.get("session_id"): blockers.append(arm + " 缺少 SessionID")
            if not item.get("prompt_id"): blockers.append(arm + " 缺少 PromptID")
            if not item.get("commit_sha"): blockers.append(arm + " 缺少最终提交")
            check_status = (checks.get(arm) or {}).get("status")
            if check_status != "passed": blockers.append(arm + " Docker 产物验收未通过")
            rec = recs.get(arm) or {}
            if rec.get("status") != "passed": blockers.append(arm + " 录像未通过")
            if not int(rec.get("commit_match") or 0): blockers.append(arm + " 录像与最终提交不匹配")
            if rec.get("review_status") != "confirmed": blockers.append(arm + " 录像尚未审核通过")
        review = detail.get("gsb") or {}
        if review.get("status") != "confirmed": blockers.append("GSB 尚未确认")
        blockers.extend(
            issue for issue in self._gsb_locator_issues(
                str(review.get("a_reason") or ""), str(review.get("b_reason") or "")
            ) if issue not in blockers
        )
        verdict, reason = str(review.get("verdict") or ""), str(review.get("reason") or "")
        version = self.gsb_evidence_version(pair_id, verdict, reason) if review else ""
        latest = self.db.one("SELECT * FROM gsb_rechecks WHERE pair_id=? ORDER BY created_at DESC LIMIT 1", (pair_id,))
        if not latest or latest.get("evidence_version") != version:
            warnings.append("尚未基于当前公开理由完成模型复检")
        elif latest.get("result_status") == "fact_conflict" and not latest.get("applied_at"):
            blockers.append("模型复检发现公开理由存在事实冲突")
        elif latest.get("result_status") == "suggested_revision" and not latest.get("applied_at"):
            warnings.append("模型复检给出了措辞修改建议")
        if include_platform:
            _, _, platform_blockers = self._solo_qa_material(detail)
            blockers.extend(issue for issue in platform_blockers if issue not in blockers)
        return {"pair_id": pair_id, "eligible": not blockers, "blockers": blockers, "warnings": warnings,
                "evidence_version": version, "checked_at": now_iso()}

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _trace_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return ""

    def _restore_archived_trace(self, arm: Dict[str, Any]) -> Optional[Path]:
        """Restore a completed Arm's trace when a repair archived its runtime.

        Artifact repair archives the old container before opening a new session.
        If that repair is interrupted and the preserved commit is later resumed,
        the database can still point at the old runtime directory even though the
        exact trace now lives under ``claude-attempts``. Recovering that immutable
        trace avoids misclassifying a storage move as a prompt mismatch.
        """
        if str(arm.get("status") or "") != "completed":
            return None
        arm_id = str(arm.get("id") or "").strip()
        session_id = str(arm.get("session_id") or "").strip()
        if not arm_id or not session_id:
            return None
        archive_root = self.config.data_dir / "claude-attempts"
        candidates: List[Path] = []
        for attempt in archive_root.glob(arm_id + "-attempt-*"):
            candidates.extend((attempt / "traces").rglob(session_id + ".jsonl"))
        if not candidates:
            return None
        # A session id is immutable. Duplicate archive copies are harmless; use
        # the newest complete copy and restore its whole trace tree.
        source_file = max(candidates, key=lambda item: item.stat().st_mtime)
        source_root = source_file
        while source_root.name != "traces" and source_root != source_root.parent:
            source_root = source_root.parent
        if source_root.name != "traces":
            return None
        target_root = self.config.data_dir / "claude-runs" / arm_id / "traces"
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, target_root, dirs_exist_ok=True)
        self.db.execute(
            "UPDATE arm_runs SET trace_path=?,updated_at=? WHERE id=? AND status='completed'",
            (str(target_root), now_iso(), arm_id),
        )
        self.db.audit("claude.archived_trace_restored", "arm_run", arm_id, {
            "session_id": session_id, "archive": str(source_root),
            "restored_to": str(target_root),
        })
        return target_root

    def _inspect_trace(self, arm: Dict[str, Any], prompt: str) -> tuple:
        issues: List[str] = []
        session_id = str(arm.get("session_id") or "").strip()
        root = Path(str(arm.get("trace_path") or "")).expanduser().resolve()
        allowed_root = (self.config.data_dir / "claude-runs").resolve()
        if session_id and (allowed_root not in root.parents or not root.is_dir()):
            restored = self._restore_archived_trace(arm)
            if restored:
                root = restored.resolve()
        if not session_id or allowed_root not in root.parents or not root.is_dir():
            return None, "", [str(arm.get("arm") or "?") + " 轨迹目录无效"]
        matches = list(root.rglob(session_id + ".jsonl"))
        if len(matches) != 1:
            return None, "", [str(arm.get("arm") or "?") + " 未找到唯一的 SessionID 轨迹文件"]
        path = matches[0].resolve()
        if path.stat().st_size > 27 * 1024 * 1024:
            issues.append(str(arm.get("arm") or "?") + " 轨迹文件超过本期 27 MB 上限")
        versions, sessions = set(), set()
        exact_prompt = False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("version"):
                        versions.add(str(event["version"]))
                    if event.get("sessionId"):
                        sessions.add(str(event["sessionId"]))
                    message = event.get("message") if isinstance(event.get("message"), dict) else {}
                    if (event.get("type") == "user" or message.get("role") == "user") and self._trace_text(message.get("content")) == prompt:
                        exact_prompt = True
        except OSError as exc:
            issues.append(str(arm.get("arm") or "?") + " 轨迹读取失败：" + str(exc))
        if sessions and session_id not in sessions:
            issues.append(str(arm.get("arm") or "?") + " SessionID 与轨迹内容不一致")
        if not exact_prompt:
            issues.append(str(arm.get("arm") or "?") + " 轨迹中没有与题面逐字一致的首轮 User Prompt")
        version = next(iter(versions)) if len(versions) == 1 else ""
        if not version:
            issues.append(str(arm.get("arm") or "?") + " 轨迹无法确定唯一 Harness 版本")
        return path, version, issues

    def _solo_qa_material(self, detail: Dict[str, Any]) -> tuple:
        issues: List[str] = []
        task = detail.get("task") or {}
        repo = detail.get("repository") or {}
        review = detail.get("gsb") or {}
        arms = {row["arm"]: row for row in detail.get("arms", [])}
        recs = {row["arm"]: row for row in detail.get("recordings", [])}
        task_types = {"zero_to_one": "0-1代码生成", "feature": "feature迭代", "bugfix": "Bug修复"}
        verdicts = {"A better": "A 更好", "Same": "Same", "B better": "B 更好"}
        task_type = task_types.get(str(task.get("task_type") or ""), "")
        if not task_type:
            issues.append("任务类型无法映射到本期 GSB 表单")
        difficulty = str(task.get("difficulty") or "")
        if difficulty not in ("困难", "地狱"):
            issues.append("本期只允许提交困难或地狱题目")
        prompt = str(task.get("prompt") or "")
        if not prompt:
            issues.append("缺少完整 User Prompt")
        remote = str(repo.get("remote_url") or "").removesuffix(".git")
        main_sha = str(repo.get("main_sha") or detail.get("baseline_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
            issues.append("初始环境快照不是 40 位完整 SHA")
        if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+", remote):
            issues.append("缺少有效的 GitHub 仓库地址")
        files: Dict[str, Dict[str, Any]] = {}
        versions: Dict[str, str] = {}
        for arm_name in ("A", "B"):
            arm = arms.get(arm_name) or {"arm": arm_name}
            trace, version, trace_issues = self._inspect_trace(arm, prompt)
            issues.extend(trace_issues)
            versions[arm_name] = version
            if trace:
                files[arm_name.lower() + "_trace_file"] = {
                    "name": trace.name, "path": str(trace), "size": trace.stat().st_size,
                    "sha256": self._sha256_file(trace), "content_type": "application/x-ndjson",
                }
            commit_sha = str(arm.get("commit_sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
                issues.append(arm_name + " 产物快照不是 40 位完整 SHA")
            workspace = Path(str(arm.get("workspace_path") or "")).expanduser().resolve()
            if commit_sha and workspace.is_dir():
                parent = run_command(["git", "rev-parse", commit_sha + "^"], cwd=workspace, check=False, timeout=15)
                if parent.returncode != 0 or parent.stdout.strip() != main_sha:
                    issues.append(arm_name + " 产物快照的父提交不是初始环境快照")
            rec = recs.get(arm_name) or {}
            video = Path(str(rec.get("path") or "")).expanduser().resolve()
            recording_root = (self.config.data_dir / "recordings").resolve()
            if recording_root not in video.parents or not video.is_file():
                issues.append(arm_name + " 录像文件不存在")
            elif video.suffix.lower() not in (".mp4", ".mov", ".webm", ".m4v"):
                issues.append(arm_name + " 录像格式不受本期平台支持")
            elif video.stat().st_size > 500 * 1024 * 1024:
                issues.append(arm_name + " 录像超过本期 500 MB 上限")
            else:
                files[arm_name.lower() + "_video"] = {
                    "name": video.name, "path": str(video), "size": video.stat().st_size,
                    "sha256": str(rec.get("sha256") or self._sha256_file(video)),
                    "content_type": mimetypes.guess_type(str(video))[0] or "video/mp4",
                }
        if versions.get("A") and versions.get("B") and versions["A"] != versions["B"]:
            issues.append("A/B Harness 版本不一致")
        a_session = str((arms.get("A") or {}).get("session_id") or "")
        b_session = str((arms.get("B") or {}).get("session_id") or "")
        if a_session and a_session == b_session:
            issues.append("A/B 必须使用不同 SessionID")
        verdict = verdicts.get(str(review.get("verdict") or ""), "")
        if not verdict:
            issues.append("GSB 结论无法映射到本期表单")
        reason = str(review.get("reason") or "").replace("`", "").strip()
        if len(reason) < 60:
            issues.append("GSB 理由不足 60 字")
        if "A：" not in reason or "B：" not in reason:
            issues.append("GSB 理由必须分别包含 A、B 评价")
        issues.extend(
            issue for issue in self._gsb_locator_issues(
                str(review.get("a_reason") or ""), str(review.get("b_reason") or "")
            ) if issue not in issues
        )
        values = {
            "user_prompt": prompt,
            "question_type": task_type,
            "difficulty": difficulty,
            "languages": normalize_stack(task.get("stack")),
            "harness": "Claude Code",
            "harness_version": versions.get("A") or versions.get("B") or "",
            "os_platform": "MacOS/Linux",
            "repro_level": "已容器化，可一键起环境",
            "env_snapshot": remote + "/commit/" + main_sha if remote and main_sha else "",
            "a_session_id": a_session,
            "a_artifact_snapshot": remote + "/commit/" + str((arms.get("A") or {}).get("commit_sha") or "") if remote else "",
            "b_session_id": b_session,
            "b_artifact_snapshot": remote + "/commit/" + str((arms.get("B") or {}).get("commit_sha") or "") if remote else "",
            "gsb_verdict": verdict,
            "gsb_reason": reason,
            "validity": "有效",
            "remark": "",
        }
        return values, files, issues

    def solo_qa_payload(self, pair_id: str) -> Dict[str, Any]:
        detail = self.pair_detail(pair_id)
        check = self.delivery_preflight(pair_id, include_platform=True)
        values, files, platform_issues = self._solo_qa_material(detail)
        for key, meta in files.items():
            meta["url"] = "/api/solo-qa/pairs/%s/files/%s" % (pair_id, key)
        payload_identity = {
            "pair_id": pair_id,
            "values": values,
            "files": {key: {k: v for k, v in meta.items() if k != "path"} for key, meta in files.items()},
        }
        payload_sha256 = hashlib.sha256(json.dumps(payload_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            **payload_identity,
            "ready": bool(check["eligible"]),
            "issues": list(dict.fromkeys(check["blockers"] + platform_issues)),
            "warnings": check["warnings"],
            "payload_sha256": payload_sha256,
            "solo_qa": detail.get("delivery") or {},
        }

    def solo_qa_file(self, pair_id: str, field_key: str) -> Dict[str, Any]:
        detail = self.pair_detail(pair_id)
        _, files, _ = self._solo_qa_material(detail)
        item = files.get(field_key)
        if not item:
            raise KeyError("提交文件不存在")
        return item

    def update_solo_qa_state(self, values: Dict[str, Any]) -> Dict[str, Any]:
        pair_id = str(values.get("pair_id") or "")
        self._pair(pair_id)
        allowed = {"ready_to_submit", "submitting", "qc_pending", "qc_passed", "needs_fix", "discarded", "failed"}
        status = str(values.get("status") or "")
        if status not in allowed:
            raise ValueError("提交状态无效")
        stamp = now_iso()
        submission_id = "delivery-" + uuid.uuid4().hex[:16]
        cleaned = lambda key, limit: str(values.get(key) or "")[:limit]
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,remote_id,remote_url,payload_sha256,
                 remote_status,qc_summary,remote_updated_at,error,submitted_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pair_id) DO UPDATE SET
                 status=excluded.status,remote_id=excluded.remote_id,remote_url=excluded.remote_url,
                 payload_sha256=excluded.payload_sha256,remote_status=excluded.remote_status,
                 qc_summary=excluded.qc_summary,remote_updated_at=excluded.remote_updated_at,
                 error=excluded.error,submitted_at=CASE WHEN excluded.submitted_at IS NOT NULL
                   THEN excluded.submitted_at ELSE delivery_submissions.submitted_at END,
                 updated_at=excluded.updated_at""",
            (submission_id, pair_id, status, cleaned("remote_id", 128), cleaned("remote_url", 1000),
             cleaned("payload_sha256", 64), cleaned("remote_status", 64), cleaned("qc_summary", 2000),
             cleaned("remote_updated_at", 128), cleaned("error", 2000),
             cleaned("submitted_at", 128) or None, stamp, stamp),
        )
        self.db.audit("solo_qa.state", "pair", pair_id, {"status": status, "remote_id": cleaned("remote_id", 128)})
        return self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,)) or {}

    def set_delivery_hidden(self, pair_id: str, hidden: bool) -> Dict[str, Any]:
        self._pair(pair_id)
        stamp = now_iso()
        submission_id = "delivery-" + uuid.uuid4().hex[:16]
        self.db.execute(
            """INSERT INTO delivery_submissions(id,pair_id,status,hidden_at,created_at,updated_at)
               VALUES(?,?,'not_submitted',?,?,?) ON CONFLICT(pair_id) DO UPDATE SET
               hidden_at=excluded.hidden_at,updated_at=excluded.updated_at""",
            (submission_id, pair_id, stamp if hidden else None, stamp, stamp),
        )
        self.db.audit("delivery.hidden" if hidden else "delivery.restored", "pair", pair_id, {})
        return self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,)) or {}

    def submit_delivery(self, pair_id: str) -> Dict[str, Any]:
        raise ValueError("正式提交必须通过 Chrome 提交小助手上传到 SOLO-QA，不能只在本地登记")

    def pair_detail(self, pair_id: str) -> Dict[str, Any]:
        self.refresh_recording_stage(pair_id)
        pair = self._pair(pair_id)
        pair["task"] = self.db.one("SELECT * FROM tasks WHERE id=?", (pair["task_id"],))
        pair["repository"] = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        pair["arms"] = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["checks"] = self._current_artifact_checks(pair_id)
        pair["difficulty_review"] = self.db.one(
            "SELECT * FROM difficulty_reviews WHERE pair_id=?", (pair_id,)
        )
        pair["recordings"] = self.db.all("SELECT * FROM recordings WHERE pair_id=? ORDER BY arm", (pair_id,))
        pair["recording_attempts"] = self.db.all(
            "SELECT * FROM recording_attempts WHERE pair_id=? ORDER BY created_at DESC", (pair_id,)
        )
        pair["gsb"] = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (pair_id,))
        if pair["gsb"]:
            pair["gsb"].pop("preference_reason", None)
        pair["gsb_rechecks"] = self.db.all("SELECT * FROM gsb_rechecks WHERE pair_id=? ORDER BY created_at DESC", (pair_id,))
        for recheck in pair["gsb_rechecks"]:
            recheck.pop("suggested_preference_reason", None)
        pair["delivery"] = self.db.one("SELECT * FROM delivery_submissions WHERE pair_id=?", (pair_id,))
        return pair

    @staticmethod
    def _transient_claude_api_error_kind(error: str) -> str:
        text = str(error or "").casefold()
        api_context = "api error" in text or "litellm" in text
        rate_limited = any(token in text for token in (
            "rate limit", "rate_limit", "ratelimiterror", "too many requests",
            "max_parallel_requests",
        )) or bool(re.search(r"(?:^|\D)429(?:\D|$)", text))
        gateway_timeout = "gateway timeout" in text or "gateway_timeout" in text
        gateway_timeout = gateway_timeout or bool(re.search(r"(?:^|\D)504(?:\D|$)", text))
        if not api_context:
            return ""
        if rate_limited:
            return "rate_limit"
        if gateway_timeout:
            return "gateway_timeout"
        return ""

    @classmethod
    def _is_transient_claude_api_error(cls, error: str) -> bool:
        return bool(cls._transient_claude_api_error_kind(error))

    def _restart_arm_from_baseline(self, pair_id: str, arm: Dict[str, Any], prompt: str,
                                   error: str, count_development_failure: bool = True,
                                   count_error_retry: bool = True) -> Dict[str, Any]:
        pair = self._pair(pair_id)
        self._invalidate_recordings(pair_id, [arm.get("arm")], error)
        restarted = self.claude.archive_failed_attempt(
            arm, error, prepare_retry=True,
            count_development_failure=count_development_failure,
            count_error_retry=count_error_retry,
        )
        canonical = self.git.reset_arm_to_baseline(pair_id, str(arm["arm"]))
        lowered = error.casefold()
        delay = 20 if any(token in lowered for token in ("429", "504", "rate limit", "rate_limit")) else 8
        self.db.execute(
            "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
            (redact(error)[-2000:], now_iso(), arm["id"]),
        )
        time.sleep(delay)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.launch(restarted)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.wait_until_ready(restarted)
        self.claude.materialize_repository(restarted, canonical, pair["baseline_sha"])
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self._send_prompt_with_pair_stagger(pair_id, restarted, prompt)
        self.db.execute("UPDATE pairs SET status='running',stage='development',error='',updated_at=? WHERE id=?", (now_iso(), pair_id))
        self.db.audit("claude.arm_restarted_after_error", "arm_run", arm["id"], {
            "attempt": int(restarted.get("attempt_no") or 1), "baseline_sha": pair["baseline_sha"],
            "reason": redact(error)[-1000:], "prompt_mode": "same_original_prompt_once",
            "counts_toward_development_attempts": count_development_failure,
            "counts_toward_error_retries": count_error_retry,
        })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted

    def _restart_arm_from_delivered_commit(self, pair_id: str, arm: Dict[str, Any],
                                            prompt: str, error: str) -> Dict[str, Any]:
        """Repair a real artifact defect without discarding delivered code."""
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        attempt = max(1, int(arm.get("attempt_no") or 1))
        maximum = max(1, int(self.db.setting("development_max_attempts", 3)))
        if attempt >= maximum:
            archived = self.claude.archive_failed_attempt(arm, error, prepare_retry=False)
            self._retire_pair_and_schedule_replacement(pair_id, arm["id"], error)
            return archived
        source_sha = str(arm.get("commit_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise RuntimeError("缺少可复用的 %s 已交付提交" % arm.get("arm", "Arm"))
        self._invalidate_recordings(pair_id, [arm.get("arm")], error)
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,)) or {}
        canonical = Path(str(repo.get("local_root") or "")) / str(arm["arm"])
        restarted = self.claude.archive_failed_attempt(
            arm, error, prepare_retry=True,
            count_development_failure=True, count_error_retry=True,
        )
        self.db.execute(
            "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
            (redact(error)[-2000:], now_iso(), arm["id"]),
        )
        time.sleep(8)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.launch(restarted)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self.claude.wait_until_ready(restarted)
        self.claude.materialize_repository(restarted, canonical, source_sha)
        restarted = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted
        self._send_prompt_with_pair_stagger(pair_id, restarted, prompt)
        self.db.execute(
            "UPDATE pairs SET status='running',stage='development',error='',updated_at=? WHERE id=?",
            (now_iso(), pair_id),
        )
        self.db.audit("artifact.repair_started_from_commit", "arm_run", arm["id"], {
            "arm": arm["arm"], "source_commit": source_sha,
            "attempt": int(restarted.get("attempt_no") or attempt + 1),
            "prompt_mode": "exact_database_prompt_once",
        })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or restarted

    def _handle_attempt_failure(self, pair_id: str, arm: Dict[str, Any], prompt: str,
                                error: str) -> Dict[str, Any]:
        """Retry every failed development attempt in a new session.

        The initial run counts as attempt one. After the third failed attempt the
        whole Pair is retired and a different ready task is started automatically.
        """
        arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
        attempt = max(1, int(arm.get("attempt_no") or 1))
        maximum = max(1, int(self.db.setting("development_max_attempts", 3)))
        transient_api_error_kind = self._transient_claude_api_error_kind(error)
        rate_limit_error = transient_api_error_kind == "rate_limit"
        count_development_failure = not rate_limit_error
        count_error_retry = not rate_limit_error
        self.db.audit("claude.attempt_failed", "arm_run", arm["id"], {
            "attempt": attempt, "maximum": maximum, "error": redact(error)[-1000:],
            "action": "fresh_session_from_baseline_without_failure_count" if rate_limit_error
                      else ("replace_task" if attempt >= maximum else "fresh_session_from_baseline"),
            "counts_toward_development_attempts": count_development_failure,
            "counts_toward_error_retries": count_error_retry,
        })
        if count_development_failure and attempt >= maximum:
            archived = self.claude.archive_failed_attempt(arm, error, prepare_retry=False)
            self._retire_pair_and_schedule_replacement(pair_id, arm["id"], error)
            return archived
        try:
            return self._restart_arm_from_baseline(
                pair_id, arm, prompt, error,
                count_development_failure=count_development_failure,
                count_error_retry=count_error_retry,
            )
        except Exception as exc:
            current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm["id"],)) or arm
            return self._handle_attempt_failure(
                pair_id, current, prompt,
                "第 %d 次失败后启动全新 Session 仍失败：%s" % (attempt, redact(str(exc))),
            )

    def _retire_pair_and_schedule_replacement(self, pair_id: str, failed_arm_id: str,
                                              error: str) -> None:
        stamp = now_iso()
        with self.db.transaction() as conn:
            pair = conn.execute("SELECT status,stage FROM pairs WHERE id=?", (pair_id,)).fetchone()
            if not pair or pair["stage"] in ("task_replacement", "replaced", "replacement_failed"):
                return
            conn.execute(
                "UPDATE pairs SET status='failed',stage='task_replacement',error=?,updated_at=? WHERE id=?",
                (("开发连续 3 次失败，正在自动换题：" + redact(error))[-3000:], stamp, pair_id),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                   WHERE pair_id=?""",
                ("原 Pair 开发连续 3 次失败，已停止交付并正在自动换题", stamp, pair_id),
            )
        self._invalidate_recordings(pair_id, reason="当前 Pair 已连续失败并换题：" + error)
        for other in self.db.all("SELECT * FROM arm_runs WHERE pair_id=? AND id<>?", (pair_id, failed_arm_id)):
            if other["status"] in ("queued", "running", "developing", "waiting_retry", "checkpointing"):
                try:
                    self.claude.archive_failed_attempt(
                        other, "同一 Pair 的另一侧连续 3 次失败，当前 Pair 已换题", prepare_retry=False,
                    )
                except Exception as exc:
                    self.db.audit("claude.peer_retire_failed", "arm_run", other["id"], {
                        "error": redact(str(exc))[-1000:],
                    })
        self.db.audit("pair.task_replacement_scheduled", "pair", pair_id, {
            "failed_arm_id": failed_arm_id, "reason": redact(error)[-1000:],
        })
        self._submit("replace-task-" + pair_id, self._start_replacement_pair, pair_id)

    def _start_replacement_pair(self, retired_pair_id: str) -> Dict[str, Any]:
        retired = self._pair(retired_pair_id)
        old_task = self.db.one("SELECT task_type FROM tasks WHERE id=?", (retired["task_id"],)) or {}
        task_type = str(old_task.get("task_type") or "zero_to_one")
        try:
            candidate = self.db.one(
                """SELECT * FROM tasks WHERE status='ready' AND difficulty IN ('困难','地狱')
                   ORDER BY CASE WHEN task_type=? THEN 0 ELSE 1 END,created_at LIMIT 1""",
                (task_type,),
            )
            if not candidate:
                for _ in range(3):
                    generated = self.generate_tasks(1, "zero_to_one")
                    if generated.get("accepted"):
                        candidate = self.db.one("SELECT * FROM tasks WHERE id=?", (generated["accepted"][0],))
                        break
            if not candidate:
                raise RuntimeError("连续生成 3 次仍没有通过准入的困难或地狱新题")
            try:
                replacement = self.create_pair(candidate["id"])
            except ValueError as exc:
                if "已达到 Pair 并发上限" not in str(exc):
                    raise
                stamp = now_iso()
                self.db.execute(
                    "UPDATE pairs SET stage='replaced',error=?,updated_at=? WHERE id=?",
                    ("开发连续 3 次失败；并发空位已由自动补位使用，无需重复创建替换 Pair", stamp, retired_pair_id),
                )
                self.db.execute(
                    """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                       WHERE pair_id=?""",
                    ("原 Pair 已废弃；并发空位已由自动补位使用", stamp, retired_pair_id),
                )
                self.db.audit("pair.task_replacement_skipped_capacity", "pair", retired_pair_id, {
                    "reason": "capacity_filled_by_scheduler",
                })
                return {"retiredPairId": retired_pair_id, "replacementPairId": "",
                        "replacementTaskId": "", "outcome": "capacity_filled"}
            replacement_id = replacement["id"]
            self.prepare_pair_repository(replacement_id)
            self.start_pair(replacement_id)
            self.db.execute(
                "UPDATE pairs SET stage='replaced',error=?,updated_at=? WHERE id=?",
                ("开发连续 3 次失败，已自动换题为 %s" % replacement_id, now_iso(), retired_pair_id),
            )
            self.db.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                   WHERE pair_id=?""",
                ("原 Pair 已废弃，已自动换题为 %s" % replacement_id,
                 now_iso(), retired_pair_id),
            )
            self.db.audit("pair.task_replaced", "pair", retired_pair_id, {
                "replacement_pair_id": replacement_id, "replacement_task_id": candidate["id"],
            })
            return {"retiredPairId": retired_pair_id, "replacementPairId": replacement_id,
                    "replacementTaskId": candidate["id"]}
        except Exception as exc:
            self.db.execute(
                "UPDATE pairs SET stage='replacement_failed',error=?,updated_at=? WHERE id=?",
                (("自动换题失败：" + redact(str(exc)))[-3000:], now_iso(), retired_pair_id),
            )
            self.db.execute(
                """UPDATE delivery_submissions SET status='discarded',error=?,updated_at=?
                   WHERE pair_id=?""",
                (("原 Pair 已废弃；自动换题失败：" + redact(str(exc)))[-2000:],
                 now_iso(), retired_pair_id),
            )
            self.db.audit("pair.task_replacement_failed", "pair", retired_pair_id, {
                "error": redact(str(exc))[-1000:],
            })
            raise

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
        while True:
            arm = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,))
            if not arm or arm["status"] not in ("developing", "running", "waiting_retry"):
                return arm or {}
            pair_state = self.db.one("SELECT stage FROM pairs WHERE id=?", (pair_id,)) or {}
            if pair_state.get("stage") != "development":
                return arm
            try:
                state = self.claude.trace_state(arm, prompt)
            except Exception as exc:
                state = {"complete": False, "api_error": "轨迹监控失败：%s" % redact(str(exc))}
            if state.get("session_id") or state.get("prompt_id"):
                self.db.execute(
                    "UPDATE arm_runs SET session_id=?,prompt_id=?,updated_at=? WHERE id=?",
                    (state.get("session_id", ""), state.get("prompt_id", ""), now_iso(), arm_id),
                )
            error = str(state.get("api_error") or "")
            if state.get("followup_detected"):
                error = "检测到首轮后的追加消息，当前 Session 作废并从共同基线重跑：%s" % state.get("followup_text", "")
            if not error and not state.get("complete") and not self.claude.runtime_alive(arm):
                error = "Claude 容器或终端意外结束，当前 Session 没有形成完整结果"
            if error:
                self.db.audit("claude.session_invalidated", "arm_run", arm_id, {
                    "error": redact(error)[-1000:], "action": "fresh_session_from_baseline",
                })
                retried = self._handle_attempt_failure(pair_id, arm, prompt, error)
                if retried.get("status") == "failed":
                    return retried
                started = time.monotonic()
                warned = False
                continue
            if state.get("complete"):
                result = str(state.get("result") or "")
                try:
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
                except Exception as exc:
                    failure = "完成后导出轨迹或推送代码失败：%s" % redact(str(exc))
                    current = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_id,)) or arm
                    retried = self._handle_attempt_failure(pair_id, current, prompt, failure)
                    if retried.get("status") == "failed":
                        return retried
                    started = time.monotonic()
                    warned = False
                    continue
            elapsed = time.monotonic() - started
            workspace = Path(arm["workspace_path"])
            has_code = self.claude.has_business_code(workspace, pair_baseline)
            if elapsed >= int(self.db.setting("first_prompt_warning_minutes", 15)) * 60 and not has_code and not warned:
                warned = True
                self.db.execute("UPDATE arm_runs SET warning_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), arm_id))
                self.db.audit("claude.no_code_warning", "arm_run", arm_id, {"elapsedSeconds": int(elapsed)})
            if elapsed >= int(self.db.setting("first_prompt_stop_minutes", 40)) * 60 and not has_code:
                retried = self._handle_attempt_failure(pair_id, arm, prompt, "首轮超时且无代码产出")
                if retried.get("status") == "failed":
                    return retried
                started = time.monotonic()
                warned = False
                continue
            time.sleep(5)

    def _restart_trace_invalid_arms(self, pair_id: str, arms: List[Dict[str, Any]],
                                    prompt: str, issues: List[str]) -> Dict[str, Any]:
        active_arms = int((self.db.one(
            """SELECT COUNT(*) count FROM arm_runs
               WHERE status IN ('queued','running','developing','waiting_retry','checkpointing')"""
        ) or {"count": 0})["count"])
        if active_arms + len(arms) > MAX_PAIR_PROJECTS * 2:
            raise RuntimeError("当前 6 个开发终端均在运行，轨迹返工需等待一个终端空位")
        stamp = now_iso()
        reason = "；".join(dict.fromkeys(str(issue) for issue in issues))[-2500:]
        prompt_mismatch = any("首轮 User Prompt" in str(issue) for issue in issues)
        problem = "轨迹题面不一致" if prompt_mismatch else "轨迹文件校验未通过"
        retry_reason = (
            "轨迹首轮题面不一致，按数据库原题面重新运行"
            if prompt_mismatch else
            "轨迹文件不可用，按数据库原题面重新运行"
        )
        pair = self._pair(pair_id)
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE pairs SET status='running',stage='development',winner='',completed_at=NULL,
                   error=?,updated_at=? WHERE id=?""",
                ((problem + "，正在按原题面用新 Session 重跑：" + reason)[-3000:], stamp, pair_id),
            )
            for arm in arms:
                conn.execute(
                    "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
                    (reason, stamp, arm["id"]),
                )
            conn.execute("DELETE FROM gsb_rechecks WHERE pair_id=?", (pair_id,))
            conn.execute(
                """UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,
                   final_verdict='',final_reason='',updated_at=? WHERE pair_id=?""",
                (stamp, pair_id),
            )
            conn.execute(
                """UPDATE delivery_submissions SET status='needs_review',error=?,updated_at=?
                   WHERE pair_id=?""",
                (problem + "，等待受影响侧重跑和重新验收", stamp, pair_id),
            )
            if pair.get("chain_id"):
                conn.execute(
                    """UPDATE project_chains SET status='active',followup_completed=0,
                       completed_at=NULL,updated_at=? WHERE id=?""",
                    (stamp, pair["chain_id"]),
                )
        restarted = []
        for arm in arms:
            current = self._restart_arm_from_baseline(
                pair_id, arm, prompt,
                retry_reason,
                count_development_failure=False,
            )
            restarted.append(str(arm["arm"]))
            self._submit_monitor(
                "monitor-" + current["id"], self._monitor_arm,
                pair_id, current["id"], prompt,
            )
        self.db.execute(
            "UPDATE pairs SET error=?,updated_at=? WHERE id=? AND stage='development'",
            ((problem + "，正在按原题面用新 Session 重跑：" + reason)[-3000:], now_iso(), pair_id),
        )
        self.db.audit("claude.trace_prompt_repair_started", "pair", pair_id, {
            "arms": restarted, "issues": list(dict.fromkeys(issues)),
            "prompt_mode": "exact_database_prompt_new_session",
            "counts_toward_development_attempts": False,
        })
        return {"pairId": pair_id, "restarted": restarted, "issues": list(dict.fromkeys(issues))}

    def _refresh_pair_after_arm(self, pair_id: str) -> None:
        with self._pair_completion_lock:
            arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
            if len(arms) != 2:
                return
            statuses = {arm["status"] for arm in arms}
            if "failed" in statuses:
                self.db.execute("UPDATE pairs SET status='failed',stage='development_failed',error='A/B 至少一侧开发失败',updated_at=? WHERE id=?", (now_iso(), pair_id))
                return
            pair = self._pair(pair_id)
            task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
            prompt = str(task.get("prompt") or "")
            invalid = []
            issues = []
            completed = [arm for arm in arms if arm["status"] == "completed"]
            for arm in completed:
                _, _, arm_issues = self._inspect_trace(arm, prompt)
                if arm_issues:
                    invalid.append(arm)
                    issues.extend(arm_issues)
            if invalid:
                self._restart_trace_invalid_arms(pair_id, invalid, prompt, issues)
                return
            stage = "artifact_validation" if statuses == {"completed"} else "development"
            self.db.execute(
                "UPDATE pairs SET status='running',stage=?,updated_at=? WHERE id=?",
                (stage, now_iso(), pair_id),
            )
            self._schedule_completed_arm_validations(pair_id)

    @staticmethod
    def _artifact_retry_key(pair_id: str, arm: str) -> str:
        return pair_id + ":" + arm

    def _schedule_completed_arm_validations(self, pair_id: str) -> None:
        """Validate each delivered Arm immediately and reuse checks by commit."""
        for arm in self.db.all(
            "SELECT * FROM arm_runs WHERE pair_id=? AND status='completed' ORDER BY arm",
            (pair_id,),
        ):
            commit_sha = str(arm.get("commit_sha") or "")
            if not commit_sha:
                continue
            passed = self.db.one(
                """SELECT id FROM artifact_checks
                   WHERE pair_id=? AND arm=? AND commit_sha=? AND status='passed'""",
                (pair_id, arm["arm"], commit_sha),
            )
            if passed:
                continue
            retry_key = self._artifact_retry_key(pair_id, str(arm["arm"]))
            if time.monotonic() < self._artifact_retry_after.get(retry_key, 0.0):
                continue
            operation = "artifact-%s-%s-%s" % (pair_id, arm["arm"], commit_sha[:12])
            self._submit_auto(
                operation, self._validate_completed_arm, pair_id, str(arm["arm"]),
            )

    def _validate_completed_arm(self, pair_id: str, arm_name: str) -> Dict[str, Any]:
        """Check the exact first prompt and artifact for one completed Arm."""
        arm = self.db.one(
            "SELECT * FROM arm_runs WHERE pair_id=? AND arm=?",
            (pair_id, arm_name),
        )
        if not arm or arm["status"] != "completed":
            return {"pairId": pair_id, "arm": arm_name, "skipped": True}
        pair = self._pair(pair_id)
        task = self.db.one("SELECT prompt FROM tasks WHERE id=?", (pair["task_id"],)) or {}
        prompt = str(task.get("prompt") or "")
        _, _, issues = self._inspect_trace(arm, prompt)
        if issues:
            return self._restart_trace_invalid_arms(pair_id, [arm], prompt, issues)
        return self._validate_pair_artifacts(pair_id, [arm_name])

    def _validate_pair_artifacts(self, pair_id: str,
                                 arm_names: Optional[List[str]] = None) -> Dict[str, Any]:
        arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        selected = [arm for arm in arms if arm["status"] == "completed"
                    and (arm_names is None or arm["arm"] in arm_names)]
        results = []
        reused_by_arm: Dict[str, bool] = {}
        for arm in selected:
            current = self.db.one(
                """SELECT * FROM artifact_checks
                   WHERE pair_id=? AND arm=? AND commit_sha=? AND status='passed'""",
                (pair_id, arm["arm"], arm["commit_sha"]),
            )
            reused_by_arm[str(arm["arm"])] = bool(current)
            results.append(current or self.artifacts.validate(
                pair_id, arm["arm"], Path(arm["workspace_path"]), arm["commit_sha"],
            ))
        failed = [item for item in results if item.get("status") != "passed"]
        if failed:
            environment_failures = [item for item in failed if self._artifact_environment_failure(item)]
            product_failures = [item for item in failed if item not in environment_failures]
            for item in environment_failures:
                retry_key = self._artifact_retry_key(pair_id, str(item.get("arm") or ""))
                self._artifact_retry_after[retry_key] = time.monotonic() + 30
            if environment_failures and not product_failures:
                names = [str(item.get("arm") or "") for item in environment_failures]
                all_completed = len(arms) == 2 and all(arm["status"] == "completed" for arm in arms)
                self.db.execute(
                    """UPDATE pairs SET status='running',stage=?,error=?,updated_at=?
                       WHERE id=?""",
                    ("artifact_validation" if all_completed else "development",
                     "Docker 验收环境冲突，已保留已完成提交并将在 30 秒后重验：" + "、".join(names),
                     now_iso(), pair_id),
                )
                self.db.audit("artifact.environment_retry_scheduled", "pair", pair_id, {
                    "arms": names, "preserved_commits": True, "retry_after_seconds": 30,
                })
                return {"pairId": pair_id, "checks": results, "reused": names, "retryScheduled": True}
            task = self.db.one(
                """SELECT t.prompt FROM tasks t JOIN pairs p ON p.task_id=t.id WHERE p.id=?""",
                (pair_id,),
            ) or {}
            prompt = str(task.get("prompt") or "")
            by_arm = {arm["arm"]: arm for arm in selected}
            names = [str(item.get("arm") or "") for item in product_failures]
            self.db.execute(
                """UPDATE pairs SET status='running',stage='development',error=?,updated_at=? WHERE id=?""",
                ("Docker 产物验收未通过，正在从失败侧已交付提交用新会话返工：" + "、".join(names), now_iso(), pair_id),
            )
            for item in product_failures:
                arm = by_arm.get(str(item.get("arm") or ""))
                if arm:
                    self.db.execute(
                        "UPDATE arm_runs SET status='waiting_retry',error=?,updated_at=? WHERE id=?",
                        ("Docker 产物验收失败，等待从该侧已交付提交返工", now_iso(), arm["id"]),
                    )
            restarted = []
            for item in product_failures:
                arm_name = str(item.get("arm") or "")
                arm = by_arm.get(arm_name)
                if not arm:
                    continue
                reason = "Docker 产物验收失败：" + str(item.get("error") or "未通过清洁 Compose 验收")
                current = self._restart_arm_from_delivered_commit(pair_id, arm, prompt, reason)
                pair = self._pair(pair_id)
                if pair.get("stage") in ("task_replacement", "replaced", "replacement_failed"):
                    return {"pairId": pair_id, "checks": results, "restarted": restarted, "retired": True}
                if current.get("status") != "failed":
                    restarted.append(arm_name)
                    self._submit_monitor(
                        "monitor-" + current["id"], self._monitor_arm,
                        pair_id, current["id"], prompt,
                    )
            self.db.audit("artifact.failed_arms_restarted", "pair", pair_id, {
                "arms": restarted, "rule": "fresh_session_from_delivered_commit",
            })
            return {"pairId": pair_id, "checks": results, "restarted": restarted}
        for arm in selected:
            self._artifact_retry_after.pop(
                self._artifact_retry_key(pair_id, str(arm["arm"])), None,
            )
            self.db.audit("artifact.arm_passed", "arm_run", arm["id"], {
                "pair_id": pair_id, "arm": arm["arm"], "commit_sha": arm["commit_sha"],
                "reused": reused_by_arm.get(str(arm["arm"]), False),
            })
        current_arms = self.db.all("SELECT * FROM arm_runs WHERE pair_id=? ORDER BY arm", (pair_id,))
        passed_count = 0
        for arm in current_arms:
            if arm["status"] != "completed":
                continue
            if self.db.one(
                """SELECT id FROM artifact_checks WHERE pair_id=? AND arm=?
                   AND commit_sha=? AND status='passed'""",
                (pair_id, arm["arm"], arm["commit_sha"]),
            ):
                passed_count += 1
        if len(current_arms) == 2 and all(arm["status"] == "completed" for arm in current_arms) and passed_count == 2:
            self.db.execute(
                "UPDATE pairs SET status='running',stage='difficulty_review',error='',updated_at=? WHERE id=?",
                (now_iso(), pair_id),
            )
            self.db.audit("artifact.pair_passed", "pair", pair_id, {
                "rule": "both_current_commits_passed_then_actual_difficulty_review",
            })
            self._submit_auto(
                "difficulty-" + pair_id,
                self.reassess_actual_difficulty,
                pair_id,
            )
        else:
            all_completed = len(current_arms) == 2 and all(
                arm["status"] == "completed" for arm in current_arms
            )
            self.db.execute(
                "UPDATE pairs SET status='running',stage=?,error='',updated_at=? WHERE id=?",
                ("artifact_validation" if all_completed else "development", now_iso(), pair_id),
            )
            if all_completed:
                self._schedule_completed_arm_validations(pair_id)
        return {"pairId": pair_id, "checks": results}

    @staticmethod
    def _artifact_environment_failure(check: Dict[str, Any]) -> bool:
        """Identify host/runtime collisions that do not invalidate a commit."""
        text = str(check.get("error") or "")
        try:
            items = json.loads(check.get("checks_json") or "[]")
        except ValueError:
            items = []
        text += "\n" + "\n".join(str(item.get("detail") or "") for item in items if not item.get("passed"))
        lowered = text.casefold()
        return any(marker in lowered for marker in (
            "port is already allocated", "address already in use",
            "failed programming external connectivity", "network is still in use",
        ))

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

    def _submit_monitor(self, operation: str, fn, *args) -> None:
        """Run long-lived Claude monitoring without blocking user actions."""
        with self._future_lock:
            existing = self._futures.get(operation)
            if existing and not existing.done():
                return
            self._futures[operation] = self.monitor_executor.submit(fn, *args)

    def _pair(self, pair_id: str) -> Dict[str, Any]:
        pair = self.db.one("SELECT * FROM pairs WHERE id=?", (pair_id,))
        if not pair:
            raise KeyError("Pair 不存在")
        return pair
