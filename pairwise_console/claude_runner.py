import hashlib
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

    @staticmethod
    def canonical_prompt(prompt: str) -> str:
        """Return the prompt representation recorded by Claude's native TUI.

        The TUI normalizes line endings and removes blank paragraph rows before
        it writes the first user event.  Persisting and sending the same form
        keeps the database prompt byte-for-byte comparable with that event.
        """
        text = str(prompt).replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n[ \t]*\n+", "\n", text)
        return text.rstrip("\n")

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

    def archive_failed_attempt(self, arm_run: Dict[str, Any], error: str,
                               prepare_retry: bool = True,
                               count_development_failure: bool = True,
                               count_error_retry: bool = True) -> Dict[str, Any]:
        """Preserve one failed attempt and optionally prepare a fresh session.

        The old container is removed only after its trace copy has been checked.
        If export fails the stopped container and its workspace are retained as
        evidence, while a retry receives new names and a new empty workspace.
        """
        arm_run = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_run["id"],)) or arm_run
        attempt_no = max(1, int(arm_run.get("attempt_no") or 1))
        archive = self.config.data_dir / "claude-attempts" / (
            "%s-attempt-%d-%s" % (arm_run["id"], attempt_no, uuid.uuid4().hex[:8])
        )
        archive.mkdir(parents=True, exist_ok=True)
        container = arm_run["container_name"]
        root = self.runtime_dir / arm_run["id"]
        stop_error = ""
        try:
            self._graceful_stop(arm_run)
        except Exception as exc:
            stop_error = redact(str(exc))
        container_exists = self._container_exists(container)
        trace_exported = False
        trace_error = ""
        if container_exists:
            traces = archive / "traces"
            traces.mkdir(parents=True, exist_ok=True)
            copied = self._copy_traces(container, traces)
            if copied.returncode == 0:
                try:
                    self._verify_trace_export(traces, arm_run, require_complete=False)
                    trace_exported = True
                except RuntimeError as exc:
                    trace_error = str(exc)
            else:
                trace_error = redact(copied.stderr or copied.stdout or "轨迹导出失败")
            if trace_exported:
                removed = run_command(["docker", "rm", container], check=False, timeout=60)
                if removed.returncode != 0:
                    trace_error = redact(removed.stderr or removed.stdout or "容器删除失败")
        else:
            previous = root / "traces"
            if previous.is_dir():
                shutil.copytree(previous, archive / "traces", dirs_exist_ok=True)
                try:
                    self._verify_trace_export(archive / "traces", arm_run, require_complete=False)
                    trace_exported = True
                except RuntimeError as exc:
                    trace_error = str(exc)
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        for name in ("terminal.log", "exit-status", "permission-status", "prompt.txt"):
            source = root / name
            if source.is_file():
                shutil.copy2(source, archive / name)
            source.unlink(missing_ok=True)
        (archive / "error.txt").write_text(redact(error)[-4000:] + "\n", encoding="utf-8")
        workspace = Path(arm_run["workspace_path"]).resolve()
        archived_workspace = archive / "workspace"
        workspace_archived = False
        if workspace.is_dir():
            try:
                shutil.copytree(workspace, archived_workspace, dirs_exist_ok=True, symlinks=True)
                workspace_archived = True
            except OSError as exc:
                (archive / "workspace-export-error.txt").write_text(redact(str(exc)), encoding="utf-8")
        next_attempt = attempt_no + 1 if count_development_failure else attempt_no
        next_workspace = workspace
        next_container = container
        next_screen = arm_run["screen_name"]
        if prepare_retry:
            suffix = "r%d-%s" % (next_attempt, uuid.uuid4().hex[:6])
            next_workspace = workspace.parent / ("%s-%s" % (arm_run["arm"], suffix))
            next_workspace.mkdir(parents=True, exist_ok=False)
            base = "pairwise-%s-%s" % (arm_run["pair_id"].replace("pair-", "")[:12], arm_run["arm"].lower())
            next_container = "%s-%s" % (base, suffix)
            next_screen = "%s-%s" % (base, suffix)
        status = "queued" if prepare_retry else "failed"
        finished_at = None if prepare_retry else now_iso()
        error_retry_increment = 1 if count_error_retry else 0
        self.db.execute(
            """UPDATE arm_runs SET status=?,workspace_path=?,container_name=?,screen_name=?,
               image_id='',session_id='',prompt_id='',trace_path='',
               commit_sha='',result='',warning_at=NULL,error='',prompt_sent_at=NULL,finished_at=NULL,
               attempt_no=?,error_retry_count=error_retry_count+?,updated_at=? WHERE id=?""",
            (status, str(next_workspace), next_container, next_screen,
             next_attempt if prepare_retry else attempt_no, error_retry_increment, now_iso(), arm_run["id"]),
        )
        if not prepare_retry:
            self.db.execute(
                "UPDATE arm_runs SET finished_at=?,error=? WHERE id=?",
                (finished_at, redact(error)[-3000:], arm_run["id"]),
            )
        self.db.audit("claude.failed_attempt_archived", "arm_run", arm_run["id"], {
            "attempt": attempt_no, "archive": str(archive), "error": redact(error)[-1000:],
            "trace_exported": trace_exported, "trace_error": trace_error, "stop_error": stop_error,
            "container_retained": bool(container_exists and self._container_exists(container)),
            "workspace_archived": workspace_archived, "retry_prepared": prepare_retry,
            "counts_toward_development_attempts": count_development_failure,
            "counts_toward_error_retries": count_error_retry,
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
        # Claude's TUI must receive the task as one bracketed paste. Without
        # these markers, embedded blank lines can be normalized and long
        # multi-byte prompts can arrive as only their trailing fragment.
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", "\x1b[200~"])
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "readbuf", str(prompt_path)])
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "paste", "."])
        run_command(["screen", "-S", arm_run["screen_name"], "-p", "0", "-X", "stuff", "\x1b[201~"])
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
        arm_run = self.db.one("SELECT * FROM arm_runs WHERE id=?", (arm_run["id"],)) or arm_run
        root = self.runtime_dir / arm_run["id"]
        trace_dir = root / "traces"
        staging = root / (".traces-export-%s" % uuid.uuid4().hex[:8])
        staging.mkdir(parents=True, exist_ok=False)
        self._graceful_stop(arm_run)
        copied = self._copy_traces(arm_run["container_name"], staging)
        if copied.returncode != 0:
            raise RuntimeError(redact(copied.stderr or copied.stdout or "轨迹导出失败"))
        self._verify_trace_export(staging, arm_run, require_complete=True)
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
        staging.rename(trace_dir)
        removed = run_command(["docker", "rm", arm_run["container_name"]], check=False, timeout=60)
        if removed.returncode != 0:
            raise RuntimeError(redact(removed.stderr or removed.stdout or "轨迹已校验，但容器删除失败"))
        if self._screen_running(arm_run["screen_name"]):
            run_command(["screen", "-S", arm_run["screen_name"], "-X", "quit"], check=False, timeout=20)
        self._close_terminal_window(root / "terminal-window.json", arm_run["screen_name"])
        self.db.execute(
            "UPDATE arm_runs SET status='exported',trace_path=?,finished_at=?,updated_at=? WHERE id=?",
            (str(trace_dir), now_iso(), now_iso(), arm_run["id"]),
        )
        return trace_dir

    def runtime_alive(self, arm_run: Dict[str, Any]) -> bool:
        return self._container_running(arm_run["container_name"]) or self._screen_running(arm_run["screen_name"])

    def _graceful_stop(self, arm_run: Dict[str, Any]) -> None:
        container = arm_run["container_name"]
        if not self._container_exists(container) or not self._container_running(container):
            return
        screen = arm_run["screen_name"]
        if self._screen_running(screen):
            for _ in range(2):
                run_command(["screen", "-S", screen, "-p", "0", "-X", "stuff", "\x04"], check=False, timeout=20)
                time.sleep(0.5)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and self._container_running(container):
                time.sleep(0.5)
        if self._container_running(container):
            stopped = run_command(["docker", "stop", "--time", "15", container], check=False, timeout=30)
            if stopped.returncode != 0:
                raise RuntimeError(redact(stopped.stderr or stopped.stdout or "容器无法正常停止"))

    @staticmethod
    def _copy_traces(container: str, destination: Path):
        result = None
        for _ in range(3):
            result = run_command(
                ["docker", "cp", "%s:%s/." % (container, CONTAINER_TRACE_PATH), str(destination)],
                check=False, timeout=180,
            )
            if result.returncode == 0:
                return result
            time.sleep(1)
        return result

    @staticmethod
    def _verify_trace_export(trace_dir: Path, arm_run: Dict[str, Any], require_complete: bool) -> Path:
        expected_session = str(arm_run.get("session_id") or "")
        expected_prompt = str(arm_run.get("prompt_id") or "")
        prompt_file = trace_dir.parent / "prompt.txt"
        expected_text = ""
        try:
            expected_text = prompt_file.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            pass
        candidates = [path for path in trace_dir.rglob("*.jsonl") if path.is_file() and path.stat().st_size > 0]
        if not candidates:
            raise RuntimeError("轨迹导出后没有非空 JSONL，已保留容器")
        for path in candidates:
            if expected_session and path.stem != expected_session:
                continue
            matched_prompt = not expected_prompt and not expected_text
            completed = False
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "user":
                        message = event.get("message") if isinstance(event.get("message"), dict) else {}
                        content = message.get("content")
                        if ((expected_prompt and str(event.get("promptId") or "") == expected_prompt) or
                                (expected_text and isinstance(content, str) and content.rstrip("\r\n") == expected_text)):
                            matched_prompt = True
                    if event.get("type") == "last-prompt" or (
                            event.get("type") == "system" and event.get("subtype") == "turn_duration"):
                        completed = True
            except OSError:
                continue
            if matched_prompt and (completed or not require_complete):
                return path
        raise RuntimeError("轨迹与当前 SessionID/PromptID 不匹配或尚未完整收尾，已保留容器")

    def trace_state(self, arm_run: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        root = self.runtime_dir / arm_run["id"]
        # A recovered scheduler can briefly have two monitor workers for the
        # same Arm.  A shared snapshot directory let one worker remove the
        # JSONL while the other was reading it, which falsely consumed a
        # development attempt.  Each inspection therefore owns an immutable
        # snapshot and removes only its own copy.
        snapshot = root / (".trace-snapshot-" + uuid.uuid4().hex)
        snapshot.mkdir(parents=True)

        def finish(value: Dict[str, Any]) -> Dict[str, Any]:
            shutil.rmtree(snapshot, ignore_errors=True)
            return value

        copied = run_command(
            ["docker", "cp", "%s:%s/." % (arm_run["container_name"], CONTAINER_TRACE_PATH), str(snapshot)],
            check=False, timeout=120,
        )
        if copied.returncode != 0:
            return finish({"complete": False, "api_error": "", "path": ""})
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
            fallback_start_index = None
            observed_prompt = ""
            prompt_id = ""
            for index, event in enumerate(events):
                if event.get("type") != "user":
                    continue
                if event.get("isMeta") is True or event.get("turnCompanion") is True:
                    continue
                content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
                if fallback_start_index is None and isinstance(content, str) and content.strip():
                    fallback_start_index = index
                    observed_prompt = content.rstrip("\r\n")
                if start_index is None and isinstance(content, str) and content.rstrip("\r\n") == prompt.rstrip("\r\n"):
                    start_index, prompt_id = index, str(event.get("promptId") or "")
                    observed_prompt = content.rstrip("\r\n")
            prompt_matches = start_index is not None
            if start_index is None and fallback_start_index is not None:
                start_index = fallback_start_index
                prompt_id = str(events[start_index].get("promptId") or "")
            if start_index is None:
                continue
            final_text, final_index, visible_text, visible_index = "", None, "", None
            native_text, native_index = "", None
            api_error, api_index = "", None
            extra_user_message = ""
            automatic_companion_messages = []
            activity = []
            last_tool_activity_at = ""
            last_tool_activity_epoch = 0.0
            for index in range(start_index + 1, len(events)):
                event = events[index]
                if event.get("type") == "user":
                    content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else ""
                    if isinstance(content, str) and content.strip():
                        if event.get("isMeta") is True or event.get("turnCompanion") is True:
                            automatic_companion_messages.append(content.strip()[:300])
                            continue
                        extra_user_message = content.strip()[:300]
                        break
                if event.get("type") != "assistant":
                    continue
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                content = message.get("content")
                blocks = content if isinstance(content, list) else []
                used_tool = False
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        used_tool = True
                        name = str(block.get("name") or "tool")
                        value = block.get("input") if isinstance(block.get("input"), dict) else {}
                        shape = " ".join(sorted(str(key) for key in value))
                        activity.append("tool:%s:%s" % (name, shape))
                    elif block.get("type") == "text" and str(block.get("text") or "").strip():
                        normalized = re.sub(
                            r"[0-9a-f]{8,}|\d+", "#",
                            re.sub(r"\s+", " ", str(block.get("text") or "").casefold()),
                        ).strip()
                        activity.append("text:" + normalized[:180])
                if used_tool:
                    timestamp = str(event.get("timestamp") or "")
                    try:
                        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        epoch = parsed.timestamp()
                    except ValueError:
                        epoch = 0.0
                    if epoch >= last_tool_activity_epoch:
                        last_tool_activity_epoch = epoch
                        last_tool_activity_at = timestamp
                text = "\n".join(str(x.get("text") or "") for x in blocks if isinstance(x, dict) and x.get("type") == "text").strip()
                if event.get("isApiErrorMessage") or text.startswith("API Error:"):
                    api_error, api_index = text or "API Error", index
                elif text:
                    visible_text, visible_index = text, index
                    stop_reason = message.get("stop_reason")
                    if stop_reason in ("end_turn", "stop_sequence"):
                        final_text, final_index = text, index
                    elif not stop_reason and not any(
                            isinstance(block, dict) and block.get("type") == "tool_use"
                            for block in blocks):
                        native_text, native_index = text, index
            # A text block attached to stop_reason=tool_use is progress before
            # another command, not the final answer.  Treating it as complete
            # used to checkpoint an unchanged baseline as the delivered code.
            completion_index = final_index if final_index is not None else native_index
            completion_mode = "explicit_stop" if final_index is not None else ""
            if native_index is not None:
                completion_mode = "native_turn_end"
            finished = bool(completion_index is not None and any(
                e.get("type") == "last-prompt" or (e.get("type") == "system" and e.get("subtype") == "turn_duration")
                for e in events[completion_index + 1:]
            ))
            # Claude can finish a successful native turn immediately after a
            # tool result, leaving only progress text whose stop_reason is
            # tool_use. The TUI is back at its input prompt and records a
            # turn_duration event, but there is no separate final text block.
            # Accept that native end only when the same turn has no API error;
            # otherwise a partially written artifact could be mistaken for a
            # completed delivery after exhausted gateway retries.
            if (not finished and api_index is None and visible_index is not None
                    and any(
                        e.get("type") == "system" and e.get("subtype") == "turn_duration"
                        for e in events[visible_index + 1:]
                    )):
                completion_index = visible_index
                completion_mode = "native_turn_end_after_tool"
                finished = True
            activity_payload = activity[:80] or ["no-assistant-activity"]
            return finish({
                "complete": finished,
                "result": final_text or native_text or visible_text,
                "completion_mode": completion_mode if finished else "",
                # Keep API errors as visible evidence, but do not use them as
                # a completion veto. Claude can recover inside the same native
                # session and later emit a valid final response.
                "api_error": api_error if api_index is not None else "",
                "session_id": path.stem,
                "prompt_id": prompt_id,
                "prompt_matches": prompt_matches,
                "observed_prompt": observed_prompt,
                "path": str(path),
                "followup_detected": bool(extra_user_message),
                "followup_text": extra_user_message,
                "automatic_companion_count": len(automatic_companion_messages),
                "automatic_companion_messages": automatic_companion_messages[:10],
                "activity_signature": hashlib.sha256(
                    json.dumps(activity_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "activity_summary": activity_payload[:12],
                # Only real tool calls extend the business-progress window.
                # Free-form thinking text alone must not keep an otherwise
                # stalled development session alive forever.
                "last_tool_activity_at": last_tool_activity_at,
            })
        empty_signature = hashlib.sha256(b'["no-trace-activity"]').hexdigest()
        return finish({"complete": False, "api_error": "", "path": "",
                       "activity_signature": empty_signature, "activity_summary": ["no-trace-activity"]})

    @staticmethod
    def business_progress(workspace: Path, baseline_sha: str = "") -> Dict[str, Any]:
        """Return whether delivery code exists and when it last changed.

        Dependency trees and generated build output are intentionally ignored.
        The timestamp covers changed delivery files and a commit made after the
        shared baseline, so a monitor restart does not reset the idle clock.
        """
        status = run_command(["git", "status", "--porcelain"], cwd=workspace, check=False, timeout=30)
        if status.returncode != 0:
            return {"has_code": False, "last_modified": 0.0, "paths": []}
        extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".vue", ".svelte", ".html", ".css", ".sql", ".sh"}
        ignored_parts = {
            ".venv", "venv", "env", "node_modules", ".pnpm-store",
            "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
            "dist", "build", "coverage", ".next", ".nuxt", ".turbo",
            "playwright-report", "test-results",
        }

        def ignored(item: Path) -> bool:
            try:
                parts = item.relative_to(workspace).parts
            except ValueError:
                parts = item.parts
            return any(part.casefold() in ignored_parts for part in parts)

        paths = [line[3:].split(" -> ")[-1].strip() for line in status.stdout.splitlines()]
        committed_after_baseline = False
        if baseline_sha:
            committed = run_command(
                ["git", "diff", "--name-only", "%s..HEAD" % baseline_sha],
                cwd=workspace, check=False, timeout=30,
            )
            if committed.returncode == 0:
                committed_paths = [line.strip() for line in committed.stdout.splitlines() if line.strip()]
                paths.extend(committed_paths)
                committed_after_baseline = bool(committed_paths)
        business_paths = []
        latest = 0.0

        def record(item: Path) -> None:
            nonlocal latest
            business_paths.append(str(item.relative_to(workspace)))
            try:
                latest = max(latest, item.stat().st_mtime)
            except OSError:
                try:
                    latest = max(latest, item.parent.stat().st_mtime)
                except OSError:
                    pass

        for path in paths:
            item = workspace / path
            if ignored(item):
                continue
            if item.name in ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml") or item.suffix.casefold() in extensions:
                record(item)
                continue
            if item.is_dir():
                for child in item.rglob("*"):
                    if child.is_file() and not ignored(child) and (
                        child.name in ("Dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml")
                        or child.suffix.casefold() in extensions
                    ):
                        record(child)
        if committed_after_baseline and business_paths:
            committed_at = run_command(
                ["git", "show", "-s", "--format=%ct", "HEAD"],
                cwd=workspace, check=False, timeout=30,
            )
            if committed_at.returncode == 0 and committed_at.stdout.strip().isdigit():
                latest = max(latest, float(committed_at.stdout.strip()))
        return {
            "has_code": bool(business_paths),
            "last_modified": latest,
            "paths": sorted(set(business_paths))[:20],
        }

    @staticmethod
    def has_business_code(workspace: Path, baseline_sha: str = "") -> bool:
        return bool(ClaudeRunner.business_progress(workspace, baseline_sha)["has_code"])

    @staticmethod
    def _screen_running(name: str) -> bool:
        probe = run_command(["screen", "-ls"], check=False, timeout=20)
        return probe.returncode in (0, 1) and (".%s" % name) in probe.stdout

    @staticmethod
    def _container_running(name: str) -> bool:
        probe = run_command(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False, timeout=20)
        return probe.returncode == 0 and probe.stdout.strip().casefold() == "true"

    @staticmethod
    def _container_exists(name: str) -> bool:
        return run_command(["docker", "inspect", name], check=False, timeout=20).returncode == 0

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
