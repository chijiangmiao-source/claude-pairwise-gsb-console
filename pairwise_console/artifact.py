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


class ArtifactChecker:
    def __init__(self, db: Database):
        self.db = db

    def validate(self, pair_id: str, arm: str, workspace: Path, commit_sha: str) -> Dict[str, Any]:
        check_id = "check-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        self.db.execute(
            """INSERT OR REPLACE INTO artifact_checks(id,pair_id,arm,commit_sha,status,started_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (check_id, pair_id, arm, commit_sha, "running", stamp, stamp, stamp),
        )
        checks: List[Dict[str, Any]] = []
        compose = self._compose_path(workspace)
        try:
            self._record(checks, "compose_file", bool(compose), str(compose or "未找到 Compose 文件"))
            dockerfile = next(iter(workspace.glob("**/Dockerfile")), None)
            self._record(checks, "dockerfile", bool(dockerfile), str(dockerfile or "未找到 Dockerfile"))
            if not compose or not dockerfile:
                raise RuntimeError("缺少 Docker Compose 或 Dockerfile")
            compose_env = os.environ.copy()
            compose_env["API_PORT"] = str(self._free_port())
            self._record(checks, "isolated_host_port", True, compose_env["API_PORT"])
            config = run_command(
                ["docker", "compose", "-f", str(compose), "--profile", "*", "config"],
                cwd=workspace, check=False, timeout=60, env=compose_env,
            )
            self._record(checks, "compose_config", config.returncode == 0, redact(config.stderr or config.stdout))
            if config.returncode != 0:
                raise RuntimeError("Compose 配置无效")
            project = "paircheck-%s-%s" % (pair_id[-8:].lower(), arm.lower())
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
            self.db.execute(
                """UPDATE artifact_checks SET compose_file=?,status=?,checks_json=?,finished_at=?,updated_at=? WHERE id=?""",
                (str(compose), status, json.dumps(checks, ensure_ascii=False), now_iso(), now_iso(), check_id),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,)) or {}
        except Exception as exc:
            if compose and "project" in locals() and "compose_env" in locals():
                run_command(
                    ["docker", "compose", "-p", project, "-f", str(compose), "down", "-v", "--remove-orphans"],
                    cwd=workspace, check=False, timeout=180, env=compose_env,
                )
            self.db.execute(
                """UPDATE artifact_checks SET compose_file=?,status='failed',checks_json=?,error=?,finished_at=?,updated_at=? WHERE id=?""",
                (str(compose or ""), json.dumps(checks, ensure_ascii=False), redact(str(exc)), now_iso(), now_iso(), check_id),
            )
            return self.db.one("SELECT * FROM artifact_checks WHERE id=?", (check_id,)) or {}

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
