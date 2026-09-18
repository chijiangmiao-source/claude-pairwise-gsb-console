import hashlib
from html import escape as html_escape
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .commands import redact, run_command
from .config import Config
from .db import Database, now_iso
from .artifact import isolated_compose_environment


class RecordingManager:
    """Runs the selected artifact and records only its 1280x720 browser viewport."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.root = config.data_dir / "recordings"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen] = {}
        self._cancelled = set()
        self._recover_interrupted_attempts()

    def _recover_interrupted_attempts(self) -> None:
        """Release the recorder lock after a service restart.

        Browser and Docker processes are children of the previous service process,
        so an unfinished database row cannot still be controlled safely here.  A
        later attempt reuses the deterministic Compose project name and cleans up
        any remaining containers before it starts.
        """
        rows = self.db.all(
            "SELECT * FROM recording_attempts WHERE status IN ('starting','recording','stopping')"
        )
        for row in rows:
            stamp = now_iso()
            if row["status"] == "stopping":
                result = inspect_recording(Path(row["path"]))
                if result.get("ok"):
                    self.db.execute(
                        """UPDATE recording_attempts SET sha256=?,width=?,height=?,duration_seconds=?,
                           status='passed',error='',finished_at=?,updated_at=? WHERE id=?""",
                        (result.get("sha256", ""), result.get("width", 0), result.get("height", 0),
                         result.get("duration_seconds", 0), stamp, stamp, row["id"]),
                    )
                    self._promote(row["id"])
                    self.db.audit("recording.save_recovered", "recording_attempt", row["id"], {
                        "reason": "service_restarted_after_stop_request",
                    })
                    continue
            message = (
                "服务重启时录像正在保存，但未形成完整文件，请重新录制"
                if row["status"] == "stopping"
                else "服务重启导致本次录像中断，请重新录制"
            )
            self.db.execute(
                """UPDATE recording_attempts SET status='failed',error=?,
                   finished_at=?,updated_at=? WHERE id=?""",
                (message, stamp, stamp, row["id"]),
            )

    def preflight(self) -> Dict[str, Any]:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        script = self.config.web_dir.parent / "scripts" / "browser_recorder.mjs"
        playwright = self.config.web_dir.parent / "node_modules" / "playwright"
        converter = self.config.web_dir.parent / "node_modules" / "ffmpeg-static" / "ffmpeg"
        ffmpeg = list((Path.home() / "Library/Caches/ms-playwright").glob("ffmpeg-*/ffmpeg-mac"))
        node = shutil.which("node")
        ok = bool(node and chrome.is_file() and script.is_file() and playwright.is_dir() and converter.is_file() and ffmpeg)
        return {"ok": ok, "node": node or "", "chrome": str(chrome), "script": str(script),
                "playwright": playwright.is_dir(), "ffmpeg": str(ffmpeg[-1]) if ffmpeg else "",
                "mp4Converter": str(converter) if converter.is_file() else ""}

    def start(self, pair_id: str, arm: str, x: int = 0, y: int = 0, manual: bool = False) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        active = self.db.one(
            "SELECT * FROM recording_attempts WHERE status IN ('starting','recording','stopping') ORDER BY created_at DESC LIMIT 1"
        )
        if active:
            if active["pair_id"] == pair_id and active["arm"] == arm:
                return active
            raise ValueError("当前已有浏览器录像正在启动或录制，请先完成后再录下一条")
        run = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm))
        if not run or not run.get("commit_sha"):
            raise ValueError("该 Arm 尚无最终提交")
        check = self.db.one(
            """SELECT * FROM artifact_checks WHERE pair_id=? AND arm=? AND commit_sha=?
               ORDER BY created_at DESC LIMIT 1""", (pair_id, arm, run["commit_sha"]),
        )
        if not check:
            raise ValueError("最终提交尚未执行 Docker 产物验收")
        if check.get("status") != "passed":
            raise ValueError("Docker 产物验收未通过，不能录制或生成 GSB；请先从该侧已交付提交返工")
        compose_value = str(check.get("compose_file") or "")
        compose = Path(compose_value) if compose_value else None
        if not compose or not compose.is_file():
            raise ValueError("Docker Compose 文件不存在")
        attempt_id = "rec-attempt-" + uuid.uuid4().hex[:16]
        folder = self.root / pair_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s-%s.mp4" % (arm, attempt_id[-8:]))
        stamp = now_iso()
        project = "pairdemo-%s-%s" % (pair_id[-8:].lower(), arm.lower())
        interaction_mode = "manual" if manual else "auto"
        self.db.execute(
            """INSERT INTO recording_attempts(id,pair_id,arm,commit_sha,path,capture_mode,interaction_mode,runtime_project,
               compose_file,status,started_at,created_at,updated_at) VALUES(?,?,?,?,?,'browser',?,?,?, 'starting',?,?,?)""",
            (attempt_id, pair_id, arm, run["commit_sha"], str(path), interaction_mode,
             project, str(compose or ""), stamp, stamp, stamp),
        )
        threading.Thread(
            target=self._launch, args=(attempt_id, Path(run["workspace_path"]), compose, project, path, check), daemon=True
        ).start()
        self.db.audit("recording.start_requested", "recording_attempt", attempt_id, {
            "pair_id": pair_id, "arm": arm, "interaction_mode": interaction_mode,
        })
        return self.db.one("SELECT * FROM recording_attempts WHERE id=?", (attempt_id,)) or {}

    def stop(self, pair_id: str, arm: str) -> Dict[str, Any]:
        row = self.db.one(
            """SELECT * FROM recording_attempts WHERE pair_id=? AND arm=?
               AND status IN ('starting','recording','stopping') ORDER BY created_at DESC LIMIT 1""", (pair_id, arm)
        )
        if not row:
            raise KeyError("没有正在进行的浏览器录像")
        if row["status"] == "stopping":
            return row
        with self._lock:
            process = self._processes.get(row["id"])
            self._cancelled.add(row["id"])
        self.db.execute(
            "UPDATE recording_attempts SET status='stopping',updated_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
        if process and process.poll() is None:
            Path(str(row["path"]) + ".stop").touch()
        self.db.audit("recording.stop_requested", "recording_attempt", row["id"], {
            "pair_id": pair_id, "arm": arm,
        })
        return self.db.one("SELECT * FROM recording_attempts WHERE id=?", (row["id"],)) or row

    def _launch(self, attempt_id: str, workspace: Path, compose: Path, project: str,
                path: Path, check: Dict[str, Any]) -> None:
        if check.get("status") != "passed":
            self._launch_failure_evidence(attempt_id, workspace, path, check)
            return
        env, assigned_ports = isolated_compose_environment(compose)
        port = int(
            assigned_ports.get("WEB_PORT")
            or assigned_ports.get("HTTP_PORT")
            or assigned_ports.get("APP_PORT")
            or assigned_ports.get("API_PORT")
            or next(iter(assigned_ports.values()))
        )
        base = ["docker", "compose", "-p", project, "-f", str(compose)]
        try:
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=env)
            up = run_command(base + ["up", "-d", "--build"], cwd=workspace, check=False, timeout=1200, env=env)
            if up.returncode != 0:
                raise RuntimeError("演示项目启动失败：" + redact(up.stderr or up.stdout))
            discovered = self._published_port(base, workspace, env) or port
            entry_url = self._wait_for_url(discovered)
            with self._lock:
                if attempt_id in self._cancelled:
                    raise RuntimeError("录像启动已取消")
            profile = self.root / "profiles" / attempt_id
            profile.mkdir(parents=True, exist_ok=True)
            command = [
                str(self.config.web_dir.parent / "node_modules" / ".bin" / "node"),
            ]
            if not Path(command[0]).exists():
                command = ["node"]
            command += [str(self.config.web_dir.parent / "scripts" / "browser_recorder.mjs"), entry_url,
                        str(path), str(profile), str(min(88, int(self.db.setting("recording_max_seconds", 90)) - 2)),
                        str(path) + ".stop",
                        str((self.db.one("SELECT interaction_mode FROM recording_attempts WHERE id=?", (attempt_id,)) or {}).get("interaction_mode") or "auto")]
            process = subprocess.Popen(command, cwd=str(self.config.web_dir.parent), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            first = process.stdout.readline().strip() if process.stdout else ""
            if not first or '"event":"ready"' not in first:
                _, error = process.communicate(timeout=15)
                raise RuntimeError("浏览器录像启动失败：" + redact(error or first))
            with self._lock:
                self._processes[attempt_id] = process
            self.db.execute(
                """UPDATE recording_attempts SET status='recording',entry_url=?,runtime_port=?,updated_at=? WHERE id=?""",
                (entry_url, discovered, now_iso(), attempt_id),
            )
            self.db.audit("recording.started", "recording_attempt", attempt_id, {"entry_url": entry_url, "port": discovered})
            self._wait(attempt_id, process, path, base, workspace, env)
        except Exception as exc:
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=env)
            self.db.execute(
                "UPDATE recording_attempts SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (redact(str(exc))[-3000:], now_iso(), now_iso(), attempt_id),
            )
            self.db.audit("recording.finished", "recording_attempt", attempt_id, {"status": "failed", "error": str(exc)[-1000:]})

    def _launch_failure_evidence(self, attempt_id: str, workspace: Path, path: Path,
                                 check: Dict[str, Any]) -> None:
        """Record the real Docker validation output in a browser-only evidence page."""
        try:
            checks = json.loads(check.get("checks_json") or "[]")
        except ValueError:
            checks = []
        blocks = []
        for item in checks:
            state = "通过" if item.get("passed") else "失败"
            blocks.append(
                "<section><h2>%s · %s</h2><pre>%s</pre></section>" % (
                    html_escape(str(item.get("name") or "Docker 检查")), state,
                    html_escape(str(item.get("detail") or "无输出")),
                )
            )
        error = html_escape(str(check.get("error") or "Docker 产物验收未通过"))
        page = path.with_suffix(".failure.html")
        page.write_text("""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">
<title>Docker 失败过程</title><style>
body{margin:0;background:#101915;color:#e8f0ea;font:18px/1.65 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}
main{width:1120px;margin:0 auto;padding:44px 0 100px}header{border:1px solid #8c4a43;background:#2b1818;padding:28px;border-radius:16px}
h1{margin:0 0 10px;font-size:34px}header p{margin:5px 0;color:#f1b7ae}section{margin-top:24px;border:1px solid #365448;background:#17241f;padding:24px;border-radius:14px}
h2{font-size:21px;margin:0 0 12px}pre{white-space:pre-wrap;word-break:break-word;background:#0a100d;padding:18px;border-radius:10px;color:#d6e5db;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
</style><main><header><h1>Docker 清洁验收失败</h1><p>提交：%s</p><p>工作区：%s</p><p>真实错误：%s</p></header>%s</main></html>""" % (
            html_escape(str(check.get("commit_sha") or "未知")), html_escape(str(workspace)), error,
            "".join(blocks) or "<section><h2>未生成检查步骤</h2><pre>%s</pre></section>" % error,
        ), encoding="utf-8")
        entry_url = page.resolve().as_uri()
        profile = self.root / "profiles" / attempt_id
        profile.mkdir(parents=True, exist_ok=True)
        command = [str(self.config.web_dir.parent / "node_modules" / ".bin" / "node")]
        if not Path(command[0]).exists():
            command = ["node"]
        command += [str(self.config.web_dir.parent / "scripts" / "browser_recorder.mjs"), entry_url,
                    str(path), str(profile), "38", str(path) + ".stop", "failure"]
        try:
            process = subprocess.Popen(command, cwd=str(self.config.web_dir.parent), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            first = process.stdout.readline().strip() if process.stdout else ""
            if not first or '"event":"ready"' not in first:
                _, recorder_error = process.communicate(timeout=15)
                raise RuntimeError("失败过程录像启动失败：" + redact(recorder_error or first))
            with self._lock:
                self._processes[attempt_id] = process
            self.db.execute(
                """UPDATE recording_attempts SET status='recording',entry_url=?,updated_at=? WHERE id=?""",
                (entry_url, now_iso(), attempt_id),
            )
            self.db.audit("recording.failure_evidence_started", "recording_attempt", attempt_id, {
                "entry_url": entry_url, "artifact_status": check.get("status"),
            })
            self._wait(attempt_id, process, path, None, workspace, os.environ.copy())
        except Exception as exc:
            self.db.execute(
                "UPDATE recording_attempts SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                (redact(str(exc))[-3000:], now_iso(), now_iso(), attempt_id),
            )
            self.db.audit("recording.finished", "recording_attempt", attempt_id, {
                "status": "failed", "error": str(exc)[-1000:],
            })

    def _wait(self, attempt_id: str, process: subprocess.Popen, path: Path, base, workspace: Path, env) -> None:
        stdout, stderr = process.communicate()
        with self._lock:
            self._processes.pop(attempt_id, None)
            self._cancelled.discard(attempt_id)
        if base:
            run_command(base + ["down", "-v", "--remove-orphans"], cwd=workspace, check=False, timeout=180, env=env)
        result = inspect_recording(path)
        status = "passed" if process.returncode == 0 and result.get("ok") else "failed"
        error = "" if status == "passed" else (result.get("error") or redact(str(stderr or "")))
        self.db.execute(
            """UPDATE recording_attempts SET sha256=?,width=?,height=?,duration_seconds=?,status=?,error=?,
               finished_at=?,updated_at=? WHERE id=?""",
            (result.get("sha256", ""), result.get("width", 0), result.get("height", 0),
             result.get("duration_seconds", 0), status, error, now_iso(), now_iso(), attempt_id),
        )
        if status == "passed":
            self._promote(attempt_id)
        self.db.audit("recording.finished", "recording_attempt", attempt_id, {"status": status, "error": error})

    def _promote(self, attempt_id: str) -> None:
        row = self.db.one("SELECT * FROM recording_attempts WHERE id=?", (attempt_id,)) or {}
        stamp = now_iso()
        reviewer = str(self.db.setting("git_author_name", "刘昱") or "刘昱").strip() + "（按授权默认确认）"
        recording_id = "rec-" + str(row.get("pair_id", ""))[-8:] + str(row.get("arm", "")).lower()
        self.db.execute(
            """INSERT INTO recordings(id,pair_id,arm,path,sha256,width,height,duration_seconds,commit_sha,
               started_at,finished_at,attempt_id,capture_mode,entry_url,commit_match,review_status,reviewed_by,
               reviewed_at,status,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'confirmed',?,?,'passed','',?,?)
               ON CONFLICT(pair_id,arm) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,
               width=excluded.width,height=excluded.height,duration_seconds=excluded.duration_seconds,
               commit_sha=excluded.commit_sha,started_at=excluded.started_at,finished_at=excluded.finished_at,
               attempt_id=excluded.attempt_id,capture_mode='browser',entry_url=excluded.entry_url,
               commit_match=1,review_status='confirmed',reviewed_by=excluded.reviewed_by,
               reviewed_at=excluded.reviewed_at,status='passed',error='',updated_at=excluded.updated_at""",
            (recording_id, row["pair_id"], row["arm"], row["path"], row["sha256"], row["width"], row["height"],
             row["duration_seconds"], row["commit_sha"], row["started_at"], row["finished_at"], attempt_id,
             "browser", row["entry_url"], reviewer, stamp, stamp, stamp),
        )
        both = self.db.one("SELECT COUNT(*) count FROM recordings WHERE pair_id=? AND status='passed' AND commit_match=1", (row["pair_id"],))
        if int((both or {}).get("count") or 0) == 2:
            review = self.db.one("SELECT * FROM gsb_reviews WHERE pair_id=?", (row["pair_id"],))
            review_confirmed = bool(review and review.get("status") == "confirmed")
            if review and not review_confirmed:
                self.db.execute("DELETE FROM gsb_rechecks WHERE pair_id=?", (row["pair_id"],))
                self.db.execute("UPDATE gsb_reviews SET status='draft',confirmed_by='',confirmed_at=NULL,updated_at=? WHERE pair_id=?", (stamp, row["pair_id"]))
            if review_confirmed:
                # Re-recording only replaces the media evidence for the same
                # delivered commits.  It must make the platform submission
                # eligible for resubmission, but it does not invalidate the
                # already confirmed code/trace/Docker comparison.
                self.db.execute(
                    """UPDATE delivery_submissions SET
                         status=CASE WHEN remote_id='' THEN 'ready_to_submit' ELSE status END,
                         error='',updated_at=? WHERE pair_id=?""",
                    (stamp, row["pair_id"]),
                )
                self.db.execute(
                    """UPDATE pairs SET status='completed',stage='completed',winner=?,
                       completed_at=COALESCE(completed_at,?),updated_at=? WHERE id=?""",
                    (review.get("verdict", ""), stamp, stamp, row["pair_id"]),
                )
            else:
                if review:
                    self.db.execute(
                        "UPDATE delivery_submissions SET status='needs_review',updated_at=? WHERE pair_id=?",
                        (stamp, row["pair_id"]),
                    )
                self.db.execute(
                    """UPDATE pairs SET status='running',stage='gsb_ready',winner='',error='',
                       completed_at=NULL,updated_at=? WHERE id=?""",
                    (stamp, row["pair_id"]),
                )
                lineage = self.db.one(
                    """SELECT p.chain_id,t.task_type FROM pairs p JOIN tasks t ON t.id=p.task_id WHERE p.id=?""",
                    (row["pair_id"],),
                ) or {}
                if lineage.get("task_type") in ("feature", "bugfix"):
                    self.db.execute(
                        "UPDATE project_chains SET status='active',followup_completed=0,completed_at=NULL,updated_at=? WHERE id=?",
                        (stamp, lineage.get("chain_id")),
                    )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _published_port(base, workspace: Path, env) -> int:
        result = run_command(base + ["ps", "--format", "json"], cwd=workspace, check=False, timeout=60, env=env)
        try:
            payload = json.loads(result.stdout)
            rows = payload if isinstance(payload, list) else [payload]
        except ValueError:
            rows = []
            for line in result.stdout.splitlines():
                try: rows.append(json.loads(line))
                except ValueError: pass
        preferred = {"web": 0, "frontend": 1, "ui": 2, "client": 3}
        rows.sort(key=lambda row: preferred.get(str(row.get("Service") or "").casefold(), 100))
        for row in rows:
            for item in row.get("Publishers") or []:
                value = int(item.get("PublishedPort") or 0)
                if value: return value
        return 0

    @staticmethod
    def _wait_for_url(port: int) -> str:
        paths = ("/", "/docs", "/index.html")
        deadline = time.monotonic() + 120
        last = ""
        while time.monotonic() < deadline:
            for path in paths:
                url = "http://127.0.0.1:%d%s" % (port, path)
                try:
                    response = urlopen(Request(url, headers={"User-Agent": "PairwiseRecorder/1.0"}), timeout=3)
                    if response.status < 400: return url
                except HTTPError as exc:
                    last = str(exc)
                except (URLError, OSError) as exc:
                    last = str(exc)
            time.sleep(2)
        raise RuntimeError("演示项目没有可用的浏览器入口：%s" % last)


def inspect_recording(path: Path) -> Dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "error": "录像文件不存在或为空"}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = run_command([
        "mdls", "-plist", "-name", "kMDItemDurationSeconds", "-name", "kMDItemPixelWidth",
        "-name", "kMDItemPixelHeight", str(path),
    ], check=False, timeout=60)
    if result.returncode != 0:
        return _inspect_with_ffprobe(path, digest, redact(result.stderr or result.stdout))
    try:
        metadata = plistlib.loads(result.stdout.encode("utf-8"))
        width = int(metadata.get("kMDItemPixelWidth") or 0)
        height = int(metadata.get("kMDItemPixelHeight") or 0)
        duration = float(metadata.get("kMDItemDurationSeconds") or 0)
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        return _inspect_with_ffprobe(path, digest, "无法读取 Spotlight 录像规格：%s" % exc)
    if not width or not height or not duration:
        return _inspect_with_ffprobe(path, digest, "Spotlight 录像元数据尚未生成")
    ok = width == 1280 and height == 720 and 0 < duration < 90
    return {
        "ok": ok, "sha256": digest, "width": width, "height": height,
        "duration_seconds": round(duration, 3),
        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
    }


def _inspect_with_ffprobe(path: Path, digest: str, prior_error: str) -> Dict[str, Any]:
    media_info = run_command(["/usr/bin/avmediainfo", str(path)], check=False, timeout=60)
    if media_info.returncode == 0:
        dimensions = re.search(r"Dimensions:\s*(\d+)\s*x\s*(\d+)", media_info.stdout)
        duration_match = re.search(r"^Duration:\s*([\d.]+)\s+seconds", media_info.stdout, re.MULTILINE)
        if dimensions and duration_match:
            width, height = int(dimensions.group(1)), int(dimensions.group(2))
            duration = float(duration_match.group(1))
            ok = width == 1280 and height == 720 and 0 < duration < 90
            return {
                "ok": ok, "sha256": digest, "width": width, "height": height,
                "duration_seconds": round(duration, 3),
                "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
            }
    try:
        probe = run_command([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
        ], check=False, timeout=60)
    except FileNotFoundError:
        class MissingProbe:
            returncode, stdout, stderr = 127, "", "ffprobe 未安装"
        probe = MissingProbe()
    if probe.returncode != 0:
        candidates = sorted((Path.home() / "Library/Caches/ms-playwright").glob("ffmpeg-*/ffmpeg-mac"), reverse=True)
        if candidates:
            media = run_command([str(candidates[0]), "-i", str(path)], check=False, timeout=60)
            text = media.stderr + "\n" + media.stdout
            dimensions = re.search(r"\b(\d{3,5})x(\d{3,5})\b", text)
            duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
            if dimensions and duration_match:
                width, height = int(dimensions.group(1)), int(dimensions.group(2))
                duration = int(duration_match.group(1)) * 3600 + int(duration_match.group(2)) * 60 + float(duration_match.group(3))
                ok = width == 1280 and height == 720 and 0 < duration < 90
                return {"ok": ok, "sha256": digest, "width": width, "height": height,
                        "duration_seconds": round(duration, 3),
                        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒"}
        return {"ok": False, "sha256": digest, "error": redact(probe.stderr or prior_error)}
    try:
        data = json.loads(probe.stdout)
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "sha256": digest, "error": "无法读取录像规格：%s" % exc}
    ok = width == 1280 and height == 720 and 0 < duration < 90
    return {
        "ok": ok, "sha256": digest, "width": width, "height": height,
        "duration_seconds": round(duration, 3),
        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
    }


def _normalize_to_720p(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    info = run_command(["/usr/bin/avmediainfo", str(path)], check=False, timeout=60)
    dimensions = re.search(r"Dimensions:\s*(\d+)\s*x\s*(\d+)", info.stdout) if info.returncode == 0 else None
    if dimensions and (int(dimensions.group(1)), int(dimensions.group(2))) == (1280, 720):
        return
    converted = path.with_name(path.stem + ".720p" + path.suffix)
    converted.unlink(missing_ok=True)
    result = run_command([
        "/usr/bin/avconvert", "--source", str(path), "--output", str(converted),
        "--preset", "Preset1280x720", "--replace",
    ], check=False, timeout=600)
    if result.returncode == 0 and converted.exists() and converted.stat().st_size:
        os.replace(converted, path)
    else:
        converted.unlink(missing_ok=True)
