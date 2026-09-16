import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .commands import redact
from .config import Config
from .db import Database, now_iso


TRANSIENT_MARKERS = (
    "rate limit", "rate_limit", "too many requests", "429", "at capacity",
    "certificate", "unable to connect", "connection reset", "timed out", "timeout",
)

JOB_EFFORTS = {
    "bug_discovery": "high",
    "bug_reproduction_review": "high",
    "task_generation": "medium",
    "task_validation": "medium",
    "baseline_review": "medium",
    "artifact_comparison": "medium",
    "gsb_review": "medium",
    "gsb_language_check": "medium",
    "next_step": "medium",
}


class CodexRunner:
    """Runs every non-development model decision through isolated Codex CLI jobs."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.jobs_dir = config.data_dir / "codex-jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def preflight(self) -> Dict[str, Any]:
        binary = shutil.which("codex")
        if not binary:
            return {"ok": False, "binary": "", "error": "找不到 codex CLI"}
        try:
            result = subprocess.run([binary, "--version"], text=True, capture_output=True, timeout=15)
            return {
                "ok": result.returncode == 0,
                "binary": binary,
                "version": (result.stdout or result.stderr).strip(),
                "error": "" if result.returncode == 0 else redact(result.stderr),
            }
        except Exception as exc:
            return {"ok": False, "binary": binary, "error": str(exc)}

    def run(
        self,
        job_type: str,
        prompt: str,
        schema: Dict[str, Any],
        cwd: Optional[Path] = None,
        pair_id: str = "",
        task_id: str = "",
        timeout: int = 1200,
        retries: int = 2,
    ) -> Dict[str, Any]:
        job_id = "codex-" + uuid.uuid4().hex[:16]
        model = str(self.db.setting("codex_model", self.config.codex_model))
        default_effort = str(self.db.setting("codex_default_effort", self.config.codex_default_effort))
        bug_effort = str(self.db.setting("codex_bug_effort", self.config.codex_bug_effort))
        effort = JOB_EFFORTS.get(job_type, default_effort)
        if job_type.startswith("bug_"):
            effort = bug_effort
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True)
        schema_path = job_dir / "schema.json"
        input_path = job_dir / "prompt.txt"
        output_path = job_dir / "result.json"
        events_path = job_dir / "events.jsonl"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
        input_path.write_text(prompt, encoding="utf-8")
        stamp = now_iso()
        self.db.execute(
            """INSERT INTO codex_jobs(id,pair_id,task_id,job_type,model,reasoning_effort,status,cwd,
               input_path,schema_path,events_path,output_path,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, pair_id, task_id, job_type, model, effort, "running",
             str(cwd or ""), str(input_path), str(schema_path), str(events_path), str(output_path), stamp, stamp),
        )
        command = [
            "codex", "exec", "--model", model,
            "--sandbox", "read-only", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--json", "--config", 'model_reasoning_effort="%s"' % effort,
            "--output-schema", str(schema_path), "--output-last-message", str(output_path),
            "--cd", str((cwd or self.config.data_dir).resolve()), "-",
        ]
        last_error = ""
        for attempt in range(1, retries + 2):
            self.db.execute(
                "UPDATE codex_jobs SET attempt_count=?,updated_at=? WHERE id=?",
                (attempt, now_iso(), job_id),
            )
            try:
                with events_path.open("a", encoding="utf-8") as events:
                    process = subprocess.run(
                        command, input=prompt, text=True, stdout=events, stderr=subprocess.PIPE,
                        timeout=timeout,
                    )
                if process.returncode != 0:
                    last_error = redact(process.stderr or "Codex CLI 执行失败")
                    transient = any(marker in last_error.casefold() for marker in TRANSIENT_MARKERS)
                    if transient and attempt <= retries:
                        time.sleep(min(20, 3 * attempt))
                        continue
                    raise RuntimeError(last_error)
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("Codex CLI 结果不是 JSON 对象")
                self.db.execute(
                    """UPDATE codex_jobs SET status='completed',exit_code=0,result_json=?,error='',
                       finished_at=?,updated_at=? WHERE id=?""",
                    (json.dumps(payload, ensure_ascii=False), now_iso(), now_iso(), job_id),
                )
                self.db.audit("codex.completed", "codex_job", job_id, {"job_type": job_type, "effort": effort})
                return payload
            except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as exc:
                last_error = redact(str(exc))
                transient = isinstance(exc, subprocess.TimeoutExpired) or any(
                    marker in last_error.casefold() for marker in TRANSIENT_MARKERS
                )
                if transient and attempt <= retries:
                    time.sleep(min(20, 3 * attempt))
                    continue
                self.db.execute(
                    """UPDATE codex_jobs SET status='failed',exit_code=?,error=?,finished_at=?,updated_at=?
                       WHERE id=?""",
                    (-1, last_error, now_iso(), now_iso(), job_id),
                )
                self.db.audit("codex.failed", "codex_job", job_id, {"job_type": job_type, "error": last_error})
                raise
        raise RuntimeError(last_error or "Codex CLI 执行失败")


GSB_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason", "evidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["A better", "Same", "B better"]},
        "reason": {"type": "string", "minLength": 20, "maxLength": 600},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
    },
}

TASK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "prompt", "taskType", "difficulty", "difficultyEvidence", "stack", "acceptance"],
    "properties": {
        "title": {"type": "string"},
        "prompt": {"type": "string"},
        "taskType": {"type": "string", "enum": ["zero_to_one", "feature"]},
        "difficulty": {"type": "string", "enum": ["困难", "地狱"]},
        "difficultyEvidence": {"type": "array", "items": {"type": "string"}, "minItems": 2},
        "stack": {"type": "string"},
        "acceptance": {"type": "array", "items": {"type": "string"}, "minItems": 3},
    },
}
