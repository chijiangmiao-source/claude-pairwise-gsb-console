import hashlib
import os
import plistlib
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
        if current:
            self.db.execute("UPDATE recordings SET path=?,status='recording',error='',updated_at=? WHERE id=?", (str(path), stamp, recording_id))
        else:
            self.db.execute(
                "INSERT INTO recordings(id,pair_id,arm,path,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (recording_id, pair_id, arm, str(path), "recording", stamp, stamp),
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
        result = inspect_recording(path)
        status = "passed" if process.returncode in (0, 130, -2) and result.get("ok") else "failed"
        error = "" if status == "passed" else (result.get("error") or redact((stderr or b"").decode("utf-8", "ignore")))
        self.db.execute(
            """UPDATE recordings SET sha256=?,width=?,height=?,duration_seconds=?,status=?,error=?,updated_at=? WHERE id=?""",
            (result.get("sha256", ""), result.get("width", 0), result.get("height", 0),
             result.get("duration_seconds", 0), status, error, now_iso(), recording_id),
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
        return {"ok": False, "sha256": digest, "error": redact(result.stderr or result.stdout)}
    try:
        metadata = plistlib.loads(result.stdout.encode("utf-8"))
        width = int(metadata.get("kMDItemPixelWidth") or 0)
        height = int(metadata.get("kMDItemPixelHeight") or 0)
        duration = float(metadata.get("kMDItemDurationSeconds") or 0)
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        return {"ok": False, "sha256": digest, "error": "无法读取录像规格：%s" % exc}
    ok = width == 1280 and height == 720 and 0 < duration < 90
    return {
        "ok": ok, "sha256": digest, "width": width, "height": height,
        "duration_seconds": round(duration, 3),
        "error": "" if ok else "录像必须为 1280×720 且少于 90 秒",
    }

