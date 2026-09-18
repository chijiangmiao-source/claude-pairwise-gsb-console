import hashlib
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .commands import redact, run_command
from .db import Database, now_iso


def isolated_compose_environment(compose: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return a Compose environment with every declared host port isolated.

    Generated projects have historically used both API_PORT and WEB_PORT.  A
    validator that sets only one of them appears isolated in its audit output
    while Compose still binds the other's default (usually 8080).  Discover
    PORT variables from the actual Compose file and give each one a separate
    free host port; keep the conventional aliases for older projects.
    """
    env = os.environ.copy()
    text = compose.read_text(encoding="utf-8", errors="replace")
    variables = {
        name for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text)
        if "PORT" in name.upper()
    }
    variables.update(("API_PORT", "WEB_PORT", "APP_PORT", "HOST_PORT", "HTTP_PORT"))
    assigned: Dict[str, str] = {}
    for name in sorted(variables):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            assigned[name] = str(int(probe.getsockname()[1]))
        env[name] = assigned[name]
    return env, assigned


class ArtifactChecker:
    def __init__(self, db: Database):
        self.db = db

    def preflight(self, workspace: Path, project_key: str) -> Dict[str, Any]:
        """Validate an existing task baseline without creating delivery evidence."""
        return self._probe(workspace, "baseline-%s" % project_key[-16:].lower())

    def validate(self, pair_id: str, arm: str, workspace: Path, commit_sha: str) -> Dict[str, Any]:
        check_id = "check-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        self.db.execute(
            """INSERT OR REPLACE INTO artifact_checks(id,pair_id,arm,commit_sha,status,started_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (check_id, pair_id, arm, commit_sha, "running", stamp, stamp, stamp),
        )
        result = self._probe(
            workspace, "paircheck-%s-%s" % (pair_id[-8:].lower(), arm.lower()),
        )
        try:
            self.db.execute(
                """UPDATE artifact_checks SET compose_file=?,status=?,checks_json=?,error=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (result["compose_file"], result["status"],
                 json.dumps(result["checks"], ensure_ascii=False), result["error"],
                 now_iso(), now_iso(), check_id),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,)) or {}
        except Exception:
            self.db.execute(
                """UPDATE artifact_checks SET compose_file=?,status='failed',checks_json=?,error=?,finished_at=?,updated_at=? WHERE id=?""",
                (result["compose_file"], json.dumps(result["checks"], ensure_ascii=False),
                 result["error"], now_iso(), now_iso(), check_id),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,)) or {}

    def _probe(self, workspace: Path, project: str) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        compose = self._compose_path(workspace)
        compose_env: Dict[str, str] = {}
        try:
            self._record(checks, "compose_file", bool(compose), str(compose or "未找到 Compose 文件"))
            dockerfile = next(iter(workspace.glob("**/Dockerfile")), None)
            self._record(checks, "dockerfile", bool(dockerfile), str(dockerfile or "未找到 Dockerfile"))
            if not compose or not dockerfile:
                raise RuntimeError("缺少 Docker Compose 或 Dockerfile")
            compose_env, assigned_ports = isolated_compose_environment(compose)
            self._record(
                checks, "isolated_host_port", True,
                json.dumps(assigned_ports, ensure_ascii=False, sort_keys=True),
            )
            config = run_command(
                ["docker", "compose", "-f", str(compose), "--profile", "*", "config"],
                cwd=workspace, check=False, timeout=60, env=compose_env,
            )
            self._record(checks, "compose_config", config.returncode == 0, redact(config.stderr or config.stdout))
            if config.returncode != 0:
                raise RuntimeError("Compose 配置无效")
            base = ["docker", "compose", "-p", project, "-f", str(compose)]
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=compose_env)
            up = run_command(base + ["up", "-d", "--build"], cwd=workspace, check=False, timeout=1200, env=compose_env)
            self._record(checks, "clean_start", up.returncode == 0, redact(up.stderr or up.stdout))
            if up.returncode != 0:
                raise RuntimeError("Docker Compose 清洁启动失败")
            time.sleep(3)
            ps = run_command(base + ["ps", "--format", "json"], cwd=workspace, check=False, timeout=60, env=compose_env)
            running = ps.returncode == 0 and ("running" in ps.stdout.casefold() or "healthy" in ps.stdout.casefold())
            self._record(checks, "containers_running", running, redact(ps.stdout or ps.stderr))
            services = run_command(
                base + ["--profile", "*", "config", "--services"],
                cwd=workspace, check=False, timeout=60, env=compose_env,
            )
            service_names = {line.strip() for line in services.stdout.splitlines() if line.strip()}
            has_verify = "verify" in service_names
            self._record(checks, "verify_service_present", has_verify, ", ".join(sorted(service_names)))
            if has_verify:
                verify = run_command(
                    base + ["run", "--rm", "verify"],
                    cwd=workspace, check=False, timeout=1200, env=compose_env,
                )
                self._record(checks, "verify_service", verify.returncode == 0, redact(verify.stdout + "\n" + verify.stderr))
            down = run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=compose_env)
            self._record(checks, "cleanup", down.returncode == 0, redact(down.stderr or down.stdout))
            status = "passed" if all(item["passed"] for item in checks) else "failed"
            return {"status": status, "compose_file": str(compose), "checks": checks,
                    "error": "" if status == "passed" else "Docker 基线验收未全部通过"}
        except Exception as exc:
            if compose and compose_env:
                run_command(
                    ["docker", "compose", "-p", project, "-f", str(compose), "down", "-v", "--remove-orphans"],
                    cwd=workspace, check=False, timeout=180, env=compose_env,
                )
            return {"status": "failed", "compose_file": str(compose or ""),
                    "checks": checks, "error": redact(str(exc))}

    @staticmethod
    def _compose_path(workspace: Path):
        for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
            path = workspace / name
            if path.exists():
                return path
        return None

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _record(checks: List[Dict[str, Any]], name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail[-1200:]})


def validate_recording(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "录像文件不存在"}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    probe = run_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
    ], check=False, timeout=60)
    if probe.returncode != 0:
        return {"ok": False, "sha256": digest, "error": redact(probe.stderr)}
    data = json.loads(probe.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    return {
        "ok": width == 1280 and height == 720 and 0 < duration < 90,
        "sha256": digest, "width": width, "height": height, "duration_seconds": duration,
        "error": "" if width == 1280 and height == 720 and 0 < duration < 90 else "录像必须为 1280×720 且少于 90 秒",
    }
