import hashlib
import json
import os
import plistlib
import re
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

from .commands import redact, run_command
from .config import Config
from .db import Database, now_iso


class RecordingManager:
    """Records a real 1280x720 screen region with visible click indicators."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.root = config.data_dir / "recordings"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen] = {}

    def start(self, pair_id: str, arm: str, x: int = 0, y: int = 0) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        current = self.db.one("SELECT * FROM recordings WHERE pair_id=? AND arm=?", (pair_id, arm))
        if current and current["status"] == "recording":
            return current
        recording_id = current["id"] if current else "rec-" + uuid.uuid4().hex[:16]
        folder = self.root / pair_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s.mov" % arm)
        path.unlink(missing_ok=True)
        width = int(self.db.setting("recording_width", 1280))
        height = int(self.db.setting("recording_height", 720))
        maximum = min(89, int(self.db.setting("recording_max_seconds", 90)) - 2)
        stamp = now_iso()
        arm_row = self.db.one(
            "SELECT commit_sha FROM arm_runs WHERE pair_id=? AND arm=?", (pair_id, arm)
        ) or {}
        commit_sha = str(arm_row.get("commit_sha") or "")
        if current:
            self.db.execute(
                """UPDATE recordings SET path=?,commit_sha=?,started_at=?,finished_at=NULL,
                   status='recording',error='',commit_match=0,updated_at=? WHERE id=?""",
                (str(path), commit_sha, stamp, stamp, recording_id),
            )
        else:
            self.db.execute(
                """INSERT INTO recordings(id,pair_id,arm,path,commit_sha,started_at,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'recording',?,?)""",
                (recording_id, pair_id, arm, str(path), commit_sha, stamp, stamp, stamp),
            )
        command = [
            "/usr/sbin/screencapture", "-v", "-V%d" % maximum,
            "-R%d,%d,%d,%d" % (x, y, width, height), "-k", "-x", str(path),
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        except Exception as exc:
            self.db.execute("UPDATE recordings SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc), now_iso(), recording_id))
            raise
        with self._lock:
            self._processes[recording_id] = process
        threading.Thread(target=self._wait, args=(recording_id, process, path), daemon=True).start()
        self.db.audit("recording.started", "recording", recording_id, {"pair_id": pair_id, "arm": arm, "rect": [x, y, width, height]})
        return self.db.one("SELECT * FROM recordings WHERE id=?", (recording_id,)) or {}

    def stop(self, pair_id: str, arm: str) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM recordings WHERE pair_id=? AND arm=?", (pair_id, arm))
        if not row:
            raise KeyError("录像不存在")
        with self._lock:
            process = self._processes.get(row["id"])
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        return row

    def _wait(self, recording_id: str, process: subprocess.Popen, path: Path) -> None:
        _, stderr = process.communicate()
        with self._lock:
            self._processes.pop(recording_id, None)
        _normalize_to_720p(path)
        result = inspect_recording(path)
        status = "passed" if process.returncode in (0, 130, -2) and result.get("ok") else "failed"
        error = "" if status == "passed" else (result.get("error") or redact((stderr or b"").decode("utf-8", "ignore")))
        self.db.execute(
            """UPDATE recordings SET sha256=?,width=?,height=?,duration_seconds=?,status=?,error=?,
               finished_at=?,commit_match=CASE WHEN commit_sha<>'' AND commit_sha=(
                 SELECT commit_sha FROM arm_runs a WHERE a.pair_id=recordings.pair_id AND a.arm=recordings.arm
               ) THEN 1 ELSE 0 END,updated_at=? WHERE id=?""",
            (result.get("sha256", ""), result.get("width", 0), result.get("height", 0),
             result.get("duration_seconds", 0), status, error, now_iso(), now_iso(), recording_id),
        )
        self.db.audit("recording.finished", "recording", recording_id, {"status": status, "error": error})


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
    probe = run_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
    ], check=False, timeout=60)
    if probe.returncode != 0:
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
