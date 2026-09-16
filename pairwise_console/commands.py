import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class CommandResult:
    args: List[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        detail = (result.stderr or result.stdout or "command failed").strip()
        super().__init__(detail[-4000:])
        self.result = result


def run_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = 120,
    check: bool = True,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> CommandResult:
    process = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env or os.environ.copy(),
    )
    result = CommandResult(args, str(cwd or ""), process.returncode, process.stdout, process.stderr)
    if check and process.returncode != 0:
        raise CommandError(result)
    return result


def redact(text: str) -> str:
    value = text or ""
    for marker in ("ghp_", "github_pat_", "sk-ant-", "Bearer "):
        start = 0
        while True:
            index = value.find(marker, start)
            if index < 0:
                break
            end = index + len(marker)
            while end < len(value) and not value[end].isspace() and value[end] not in "'\"":
                end += 1
            value = value[:index] + marker + "***" + value[end:]
            start = index + len(marker) + 3
    return value[-4000:]

