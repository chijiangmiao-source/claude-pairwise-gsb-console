import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .commands import redact, run_command
from .config import Config, OLD_APP_DIR
from .db import Database, now_iso


CONTAINER_TRACE_PATH = "/home/node/.claude/projects"


class ClaudeRunner:
    """Claude is used only for the two A/B development arms."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.runtime_dir = config.data_dir / "claude-runs"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def preflight(self) -> Dict[str, Any]:
        image_name = str(self.db.setting("claude_image", self.config.claude_image))
        model = str(self.db.setting("claude_model", self.config.claude_model))
        checks: Dict[str, Any] = {"ok": True, "image": image_name, "model": model}
        for binary in ("docker", "screen", "osascript"):
            path = shutil.which(binary)
            checks[binary] = {"ok": bool(path), "path": path or ""}
            checks["ok"] = checks["ok"] and bool(path)
        if checks["docker"]["ok"]:
            image = run_command([checks["docker"]["path"], "image", "inspect", image_name, "--format", "{{.Id}}"], check=False, timeout=30)
            checks["image_id"] = image.stdout.strip() if image.returncode == 0 else ""
            checks["image_ok"] = image.returncode == 0
            checks["ok"] = checks["ok"] and image.returncode == 0
        else:
            checks["image_id"] = ""
            checks["image_ok"] = False
        return checks

    def prepare_arm(self, pair: Dict[str, Any], arm: str, workspace: Path) -> Dict[str, Any]:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        arm_id = "%s-%s" % (pair["id"], arm.lower())
        container = "pairwise-%s-%s" % (pair["id"].replace("pair-", "")[:16], arm.lower())
        screen = "pairwise-%s-%s" % (pair["id"].replace("pair-", "")[:16], arm)
        stamp = now_iso()
        current = self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair["id"], arm))
        model = str(self.db.setting("claude_model", self.config.claude_model))
        image = str(self.db.setting("claude_image", self.config.claude_image))
        values = (arm_id, pair["id"], arm, arm, str(workspace), container, screen, model,
                  image, "queued", stamp, stamp)
        if not current:
            self.db.execute(
                """INSERT INTO arm_runs(id,pair_id,arm,branch,workspace_path,container_name,screen_name,
                   model,image,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values,
            )
        elif not current.get("prompt_sent_at"):
            # Repository preparation can be retried before the prompt is sent.
            # Keep the canonical A/B clones separate and point Claude at a
            # disposable workspace that is empty when the container starts.
            self.db.execute(
                """UPDATE arm_runs SET workspace_path=?,container_name=?,screen_name=?,model=?,image=?,
                   status='queued',error='',updated_at=? WHERE id=?""",
                (str(workspace), container, screen, model, image, stamp, current["id"]),
            )
        return self.db.one("SELECT * FROM arm_runs WHERE pair_id=? AND arm=?", (pair["id"], arm)) or {}

    def reset_unsent_arm(self, arm_run: Dict[str, Any]) -> None:
        """Reset launch debris only when no task prompt has entered the session."""
        if arm_run.get("prompt_sent_at"):
            raise RuntimeError("该 Arm 已发送题面，不能按未启动任务重置")
        container = arm_run["container_name"]
        if run_command(["docker", "inspect", container], check=False, timeout=20).returncode == 0:
            run_command(["docker", "rm", "-f", container], check=False, timeout=60)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        root = self.runtime_dir / arm_run["id"]
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        workspace = Path(arm_run["workspace_path"]).resolve()
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        for stale in (root / "terminal.log", root / "exit-status", root / "permission-status"):
            stale.unlink(missing_ok=True)
        self.db.execute(
            """UPDATE arm_runs SET status='queued',image_id='',session_id='',prompt_id='',trace_path='',
               commit_sha='',result='',warning_at=NULL,error='',updated_at=? WHERE id=?""",
            (now_iso(), arm_run["id"]),
        )

    def restart_after_api_error(self, arm_run: Dict[str, Any], error: str) -> Dict[str, Any]:
        """Archive an invalid attempt and return the arm to a clean, unsent state.

        An API failure invalidates that session.  The retry must therefore use a
        new container, empty workspace and new SessionID; sending a follow-up to
        the failed session would turn the sample into a multi-turn run.
        """
        attempt_no = max(1, int(arm_run.get("attempt_no") or 1))
        archive = self.config.data_dir / "claude-attempts" / (
            "%s-attempt-%d-%s" % (arm_run["id"], attempt_no, uuid.uuid4().hex[:8])
        )
        archive.mkdir(parents=True, exist_ok=True)
        container = arm_run["container_name"]
        container_exists = run_command(["docker", "inspect", container], check=False, timeout=20).returncode == 0
        if container_exists:
            traces = archive / "traces"
            traces.mkdir(parents=True, exist_ok=True)
            run_command(
                ["docker", "cp", "%s:%s/." % (container, CONTAINER_TRACE_PATH), str(traces)],
                check=False, timeout=180,
            )
            run_command(["docker", "rm", "-f", container], check=False, timeout=60)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        root = self.runtime_dir / arm_run["id"]
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        for name in ("terminal.log", "exit-status", "permission-status", "prompt.txt"):
            source = root / name
            if source.is_file():
                shutil.copy2(source, archive / name)
            source.unlink(missing_ok=True)
        (archive / "error.txt").write_text(redact(error)[-4000:] + "\n", encoding="utf-8")
        workspace = Path(arm_run["workspace_path"]).resolve()
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        self.db.execute(
            """UPDATE arm_runs SET status='queued',image_id='',session_id='',prompt_id='',trace_path='',
               commit_sha='',result='',warning_at=NULL,error='',prompt_sent_at=NULL,finished_at=NULL,
               attempt_no=?,error_retry_count=error_retry_count+1,updated_at=? WHERE id=?""",
            (attempt_no + 1, now_iso(), arm_run["id"]),
        )
        self.db.audit("claude.api_error_attempt_archived", "arm_run", arm_run["id"], {
            "attempt": attempt_no, "archive": str(archive), "error": redact(error)[-1000:],
        })
        return self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_run["id"],)) or {}

    def materialize_repository(self, arm_run: Dict[str, Any], source: Path, expected_sha: str) -> None:
        """Import an exact branch snapshot after Claude accepts the empty mount."""
        destination = Path(arm_run["workspace_path"]).resolve()
        source = source.resolve()
        if not self._container_running(arm_run["container_name"]):
            raise RuntimeError("Claude 容器尚未运行，不能导入仓库")
        if not source.is_dir() or not (source / ".git").is_dir():
            raise RuntimeError("A/B 源仓库不存在：%s" % source)
        if any(destination.iterdir()):
            raise RuntimeError("Claude 运行工作区在仓库导入前不是空目录")
        source_sha = run_command(["git", "rev-parse", "HEAD"], cwd=source, timeout=30).stdout.strip()
        source_branch = run_command(["git", "branch", "--show-current"], cwd=source, timeout=30).stdout.strip()
        source_status = run_command(["git", "status", "--porcelain"], cwd=source, timeout=30).stdout.strip()
        if source_sha != expected_sha or source_branch != arm_run["arm"] or source_status:
            raise RuntimeError("A/B 源仓库未保持指定分支的清洁基线")
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        imported_sha = run_command(["git", "rev-parse", "HEAD"], cwd=destination, timeout=30).stdout.strip()
        imported_branch = run_command(["git", "branch", "--show-current"], cwd=destination, timeout=30).stdout.strip()
        imported_status = run_command(["git", "status", "--porcelain"], cwd=destination, timeout=30).stdout.strip()
        if imported_sha != expected_sha or imported_branch != arm_run["arm"] or imported_status:
            raise RuntimeError("导入后的 A/B 工作区未通过分支与基线校验")
        self.db.audit("claude.repository_materialized", "arm_run", arm_run["id"], {
            "arm": arm_run["arm"], "baseline_sha": imported_sha,
        })

    def launch(self, arm_run: Dict[str, Any]) -> None:
        arm_id = arm_run["id"]
        root = self.runtime_dir / arm_id
        root.mkdir(parents=True, exist_ok=True)
        launcher = root / "launch-container.command"
        screenrc = root / "screenrc"
        log = root / "terminal.log"
        exit_status = root / "exit-status"
        terminal_meta = root / "terminal-window.json"
        workspace = Path(arm_run["workspace_path"]).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        if any(workspace.iterdir()):
            raise RuntimeError("Claude 首次启动工作区必须为空")
        settings = Path.home() / ".claude" / "settings.json"
        image_id = run_command(["docker", "image", "inspect", arm_run["image"], "--format", "{{.Id}}"], timeout=30).stdout.strip()
        if run_command(["docker", "inspect", arm_run["container_name"]], check=False, timeout=20).returncode == 0:
            raise RuntimeError("容器名称已被占用：%s" % arm_run["container_name"])
        if self._screen_running(arm_run["screen_name"]):
            raise RuntimeError("Screen 会话名称已被占用：%s" % arm_run["screen_name"])
        script = """#!/bin/zsh
set -u
container_name=%s
workspace=%s
image=%s
model=%s
settings_file=%s
exit_status=%s
api_key="${apikey:-${ANTHROPIC_AUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}}"
unset apikey ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
if [[ -z "$api_key" && -f "$settings_file" ]]; then
  api_key="$(/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); e=d.get("env", {}) if isinstance(d,dict) else {}; print(e.get("ANTHROPIC_AUTH_TOKEN") or e.get("ANTHROPIC_API_KEY") or "", end="")' "$settings_file" 2>/dev/null)"
fi
if [[ -z "$api_key" ]]; then
  read -r -s "api_key?请输入本次任务的 API Key（输入不显示）："
  printf '\n'
fi
docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges --name "$container_name" --mount "type=bind,src=$workspace,dst=/workspace" -e "apikey=$api_key" -e "ANTHROPIC_MODEL=$model" "$image"
code=$?
unset api_key
printf '%%s\n' "$code" > "$exit_status"
exit "$code"
""" % tuple(shlex.quote(str(v)) for v in (
            arm_run["container_name"], workspace, arm_run["image"], arm_run["model"], settings, exit_status
        ))
        launcher.write_text(script, encoding="utf-8")
        launcher.chmod(0o700)
        screenrc.write_text('deflog on\nlogfile "%s"\nlogfile flush 1\ndefscrollback 10000\n' % str(log).replace('"', '\\"'), encoding="utf-8")
        run_command(["screen", "-c", str(screenrc), "-dmS", arm_run["screen_name"], "/bin/zsh", str(launcher)], timeout=30)
        self._open_terminal(arm_run["screen_name"], terminal_meta)
        self.db.execute(
            "UPDATE arm_runs SET status='running',image_id=?,updated_at=? WHERE id=?",
            (image_id, now_iso(), arm_id),
        )
        self.db.audit("claude.arm_started", "arm_run", arm_id, {"arm": arm_run["arm"], "image_id": image_id})

    def send_prompt(self, arm_run: Dict[str, Any], prompt: str) -> None:
        if not self._screen_running(arm_run["screen_name"]):
            raise RuntimeError("Claude 开发终端已经关闭")
        prompt_path = self.runtime_dir / arm_run["id"] / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "readbuf", str(prompt_path)])
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "paste", "."])
        time.sleep(0.4)
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", "\r"])
        self.db.execute("UPDATE arm_runs SET status='developing',prompt_sent_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), arm_run["id"]))

    def wait_until_ready(self, arm_run: Dict[str, Any], timeout: int = 3600) -> None:
        """Wait for Docker and accept Claude's one-time bypass permission prompt."""
        root = self.runtime_dir / arm_run["id"]
        log = root / "terminal.log"
        permission = root / "permission-status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._container_running(arm_run["container_name"]):
                break
            if not self._screen_running(arm_run["screen_name"]):
                raise RuntimeError("终端启动已结束，但 Claude 容器没有运行")
            time.sleep(2)
        else:
            raise RuntimeError("等待 Claude 容器启动超时")
        deadline = time.monotonic() + 60
        accepted_once = False
        while time.monotonic() < deadline:
            if not self._container_running(arm_run["container_name"]):
                raise RuntimeError("Claude 容器在权限确认前已停止")
            try:
                output = log.read_text(encoding="utf-8", errors="ignore")[-30000:]
            except OSError:
                output = ""
            visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).casefold()
            compact = re.sub(r"\s+", "", visible)
            if "bypasspermissionson" in compact:
                permission.write_text("accepted\n", encoding="utf-8")
                return
            if not accepted_once and all(token.casefold() in visible for token in ("Bypass", "Permissions", "Yes,", "accept")):
                run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", "\x1b[B\r"])
                accepted_once = True
            time.sleep(0.5)
        raise RuntimeError("Claude 权限确认后未进入对话主界面")

    def export_and_stop(self, arm_run: Dict[str, Any]) -> Path:
        root = self.runtime_dir / arm_run["id"]
        trace_dir = root / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        copied = run_command(["docker", "cp", "%s:%s/." % (arm_run["container_name"], CONTAINER_TRACE_PATH), str(trace_dir)], check=False, timeout=180)
        if copied.returncode != 0:
            raise RuntimeError(redact(copied.stderr or copied.stdout or "轨迹导出失败"))
        run_command(["docker", "rm", "-f", arm_run["container_name"]], check=False, timeout=60)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        self.db.execute(
            "UPDATE arm_runs SET status='exported',trace_path=?,finished_at=?,updated_at=? WHERE id=?",
            (str(trace_dir), now_iso(), now_iso(), arm_run["id"]),
        )
        return trace_dir

    def trace_state(self, arm_run: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        root = self.runtime_dir / arm_run["id"]
        snapshot = root / ".trace-snapshot"
        if snapshot.exists():
            shutil.rmtree(snapshot)
        snapshot.mkdir(parents=True)
        copied = run_command(
            ["docker", "cp", "%s:%s/." % (arm_run["container_name"], CONTAINER_TRACE_PATH), str(snapshot)],
            check=False, timeout=120,
        )
        if copied.returncode != 0:
            return {"complete": False, "api_error": "", "path": ""}
        for path in sorted(snapshot.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            events = []
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
            except OSError:
                continue
            start_index = None
            prompt_id = ""
            for index, event in enumerate(events):
                if event.get("type") != "user":
                    continue
                content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
                if start_index is None and isinstance(content, str) and content.rstrip("\r\n") == prompt.rstrip("\r\n"):
                    start_index, prompt_id = index, str(event.get("promptId") or "")
            if start_index is None:
                continue
            final_text, final_index, api_error, api_index = "", None, "", None
            extra_user_message = ""
            for index in range(start_index + 1, len(events)):
                event = events[index]
                if event.get("type") == "user":
                    content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else ""
                    if isinstance(content, str) and content.strip():
                        extra_user_message = content.strip()[:300]
                        break
                if event.get("type") != "assistant":
                    continue
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                content = message.get("content")
                blocks = content if isinstance(content, list) else []
                text = "\n".join(str(x.get("text") or "") for x in blocks if isinstance(x, dict) and x.get("type") == "text").strip()
                if event.get("isApiErrorMessage") or text.startswith("API Error:"):
                    api_error, api_index = text or "API Error", index
                elif text and message.get("stop_reason") in ("end_turn", "stop_sequence"):
                    final_text, final_index = text, index
            finished = bool(final_index is not None and any(
                e.get("type") == "last-prompt" or (e.get("type") == "system" and e.get("subtype") == "turn_duration")
                for e in events[final_index + 1:]
            ))
            # Any API error makes this attempt ineligible, even when the CLI
            # later emits a final message by retrying internally. The service
            # archives it and replays the original prompt in a new session.
            unresolved_error = api_index is not None
            return {
                "complete": finished,
                "result": final_text,
                "api_error": api_error if unresolved_error else "",
                "session_id": path.stem,
                "prompt_id": prompt_id,
                "path": str(path),
                "followup_detected": bool(extra_user_message),
                "followup_text": extra_user_message,
            }
        return {"complete": False, "api_error": "", "path": ""}

    @staticmethod
    def has_business_code(workspace: Path, baseline_sha: str = "") -> bool:
        status = run_command(["git", "status", "--porcelain"], cwd=workspace, check=False, timeout=30)
        if status.returncode != 0:
            return False
        extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".vue", ".svelte", ".html", ".css", ".sql", ".sh"}
        paths = [line[3:].split(" -> ")[-1].strip() for line in status.stdout.splitlines()]
        if baseline_sha:
            committed = run_command(
                ["git", "diff", "--name-only", "%s..HEAD" % baseline_sha],
                cwd=workspace, check=False, timeout=30,
            )
            if committed.returncode == 0:
                paths.extend(line.strip() for line in committed.stdout.splitlines() if line.strip())
        for path in paths:
            item = workspace / path
            if item.name in ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml") or item.suffix.casefold() in extensions:
                return True
            if item.is_dir():
                for child in item.rglob("*"):
                    if child.is_file() and (
                        child.name in ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml")
                        or child.suffix.casefold() in extensions
                    ):
                        return True
        return False

    @staticmethod
    def _screen_running(name: str) -> bool:
        probe = run_command(["screen", "-ls"], check=False, timeout=20)
        return probe.returncode in (0, 1) and (".%s" % name) in probe.stdout

    @staticmethod
    def _container_running(name: str) -> bool:
        probe = run_command(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False, timeout=20)
        return probe.returncode == 0 and probe.stdout.strip().casefold() == "true"

    @staticmethod
    def _open_terminal(screen_name: str, metadata_path: Path) -> None:
        command = "/usr/bin/screen -r %s" % shlex.quote(screen_name)
        script = (
            'tell application "Terminal"\n'
            'set launchedTab to do script %s\n' % json.dumps(command) +
            'set custom title of launchedTab to %s\n' % json.dumps(screen_name) +
            'set title displays custom title of launchedTab to true\nactivate\n'
            'return ((id of front window) as text) & "|" & (tty of launchedTab as text)\nend tell'
        )
        result = run_command(["osascript", "-e", script], check=False, timeout=30)
        if result.returncode == 0:
            window, _, tty = result.stdout.strip().partition("|")
            metadata_path.write_text(json.dumps({"window_id": window, "tty": tty, "title": screen_name}), encoding="utf-8")

    @staticmethod
    def _close_terminal_window(metadata_path: Path, screen_name: str) -> None:
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            window_id, tty, title = str(data["window_id"]), str(data["tty"]), str(data["title"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if title != screen_name or not window_id.isdigit() or not tty.startswith("/dev/"):
            return
        script = (
            'tell application "Terminal"\nrepeat with w in windows\n'
            'if (id of w as text) is %s then\n' % json.dumps(window_id) +
            'if (count of tabs of w) is 1 then\nrepeat with t in tabs of w\n'
            'if (tty of t as text) is %s and (custom title of t as text) is %s and not busy of t then close w\n' % (json.dumps(tty), json.dumps(title)) +
            'end repeat\nend if\nend if\nend repeat\nend tell'
        )
        run_command(["osascript", "-e", script], check=False, timeout=30)
        metadata_path.unlink(missing_ok=True)
