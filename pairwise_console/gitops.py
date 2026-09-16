import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .commands import CommandError, redact, run_command
from .config import Config
from .db import Database, now_iso


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return value[:70] or "pair-project"


class GitOps:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def preflight(self) -> Dict[str, Any]:
        author_name = str(self.db.setting("git_author_name", self.config.git_author_name))
        author_email = str(self.db.setting("git_author_email", self.config.git_author_email))
        result: Dict[str, Any] = {
            "ok": True,
            "git": {},
            "gh": {},
            "author": {"name": author_name, "email": author_email},
        }
        for binary in ("git", "gh"):
            path = shutil.which(binary)
            item = {"binary": path or "", "ok": bool(path), "version": "", "error": ""}
            if path:
                try:
                    probe = run_command([path, "--version"], check=False, timeout=15)
                    item.update(ok=probe.returncode == 0, version=(probe.stdout or probe.stderr).splitlines()[0])
                except Exception as exc:
                    item.update(ok=False, error=str(exc))
            result[binary] = item
            result["ok"] = result["ok"] and item["ok"]
        if result["gh"]["ok"]:
            auth = run_command(["gh", "auth", "status"], check=False, timeout=20)
            result["gh"]["authenticated"] = auth.returncode == 0
            result["gh"]["error"] = "" if auth.returncode == 0 else redact(auth.stderr or auth.stdout)
            account = run_command(["gh", "api", "user", "--jq", ".login"], check=False, timeout=20)
            result["gh"]["account"] = account.stdout.strip() if account.returncode == 0 else ""
            result["ok"] = result["ok"] and auth.returncode == 0
        if not author_email:
            result["ok"] = False
            result["author"]["error"] = "尚未配置 Git 提交邮箱"
        return result

    def create_pair_repository(self, pair: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
        pair_id = pair["id"]
        existing = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        if existing and existing["status"] == "ready":
            return existing
        owner = str(self.db.setting("github_owner", self.config.github_owner)) or self.preflight().get("gh", {}).get("account", "")
        if not owner:
            raise RuntimeError("无法确定 GitHub Owner")
        suffix = {"zero_to_one": "", "feature": "-feature", "bugfix": "-bugfix"}[task["task_type"]]
        # A Pair always gets a new repository. The short Pair id keeps names
        # deterministic while avoiding collisions with imported source repos.
        prefix = str(self.db.setting("repository_prefix", self.config.repository_prefix))
        visibility = str(self.db.setting("github_visibility", self.config.github_visibility))
        author_name = str(self.db.setting("git_author_name", self.config.git_author_name))
        author_email = str(self.db.setting("git_author_email", self.config.git_author_email))
        name = slugify("%sab-%s-%s%s" % (
            prefix, pair_id[-8:], task["title"], suffix
        ))
        local_root = self.config.projects_dir / pair_id
        baseline = local_root / "baseline"
        local_root.mkdir(parents=True, exist_ok=True)
        repo_id = existing["id"] if existing else "repo-" + uuid.uuid4().hex[:16]
        stamp = now_iso()
        if not existing:
            self.db.execute(
                """INSERT INTO git_repositories(id,pair_id,owner,name,visibility,local_root,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (repo_id, pair_id, owner, name, visibility, str(local_root), "creating", stamp, stamp),
            )
        try:
            if baseline.exists():
                shutil.rmtree(baseline)
            source = Path(task["baseline_path"]) if task.get("baseline_path") else None
            if source and source.exists():
                # Feature/Bug tasks must use the exact recorded task-time
                # commit, never whatever happens to be in the source working
                # tree today.
                base_sha = str(task.get("baseline_sha") or "")
                is_commit = run_command(
                    ["git", "-C", str(source), "cat-file", "-e", "%s^{commit}" % base_sha],
                    check=False, timeout=30,
                ) if base_sha else None
                if is_commit and is_commit.returncode == 0:
                    run_command(["git", "clone", "--no-hardlinks", str(source), str(baseline)], timeout=180)
                    run_command(["git", "checkout", "--detach", base_sha], cwd=baseline, timeout=60)
                    shutil.rmtree(baseline / ".git")
                    for ignored in ("node_modules", ".venv", "__pycache__"):
                        for path in baseline.glob("**/%s" % ignored):
                            if path.is_dir():
                                shutil.rmtree(path, ignore_errors=True)
                else:
                    raise RuntimeError("来源目录中找不到记录的基线提交：%s" % base_sha)
            else:
                baseline.mkdir()
                (baseline / ".gitignore").write_text(".DS_Store\n.env\nnode_modules/\n__pycache__/\n", encoding="utf-8")
            run_command(["git", "init", "-b", "main"], cwd=baseline)
            run_command(["git", "config", "user.name", author_name], cwd=baseline)
            run_command(["git", "config", "user.email", author_email], cwd=baseline)
            run_command(["git", "add", "-A"], cwd=baseline)
            run_command(["git", "commit", "-m", "Initialize A/B baseline"], cwd=baseline)
            main_sha = run_command(["git", "rev-parse", "HEAD"], cwd=baseline).stdout.strip()
            remote_slug = "%s/%s" % (owner, name)
            exists = run_command(["gh", "repo", "view", remote_slug, "--json", "url", "--jq", ".url"], check=False, timeout=30)
            if exists.returncode == 0:
                raise RuntimeError("目标仓库已经存在，不能覆盖：%s" % remote_slug)
            visibility_flag = "--private" if visibility == "private" else "--public"
            created = run_command(
                ["gh", "repo", "create", remote_slug, visibility_flag, "--source", str(baseline), "--remote", "origin", "--push"],
                cwd=baseline, timeout=180,
            )
            remote_url = run_command(["git", "remote", "get-url", "origin"], cwd=baseline).stdout.strip()
            for arm in ("A", "B"):
                run_command(["git", "branch", arm, main_sha], cwd=baseline)
                run_command(["git", "push", "origin", "%s:%s" % (arm, arm)], cwd=baseline, timeout=120)
                arm_dir = local_root / arm
                run_command(["git", "clone", "--branch", arm, "--single-branch", remote_url, str(arm_dir)], timeout=180)
                run_command(["git", "config", "user.name", author_name], cwd=arm_dir)
                run_command(["git", "config", "user.email", author_email], cwd=arm_dir)
            self.db.execute(
                """UPDATE git_repositories SET remote_url=?,main_sha=?,a_sha=?,b_sha=?,status='ready',error='',updated_at=?
                   WHERE id=?""",
                (remote_url, main_sha, main_sha, main_sha, now_iso(), repo_id),
            )
            self.db.execute("UPDATE pairs SET repo_id=?,baseline_sha=?,updated_at=? WHERE id=?", (repo_id, main_sha, now_iso(), pair_id))
            self.db.audit("git.repository_ready", "pair", pair_id, {"repo": remote_slug, "main_sha": main_sha})
            return self.db.one("SELECT * FROM git_repositories WHERE id=?", (repo_id,)) or {}
        except Exception as exc:
            error = redact(str(exc))
            self.db.execute("UPDATE git_repositories SET status='failed',error=?,updated_at=? WHERE id=?", (error, now_iso(), repo_id))
            self.db.audit("git.repository_failed", "pair", pair_id, {"error": error})
            raise

    def push_arm(self, pair_id: str, arm: str) -> str:
        if arm not in ("A", "B"):
            raise ValueError("arm must be A or B")
        repo = self.db.one("SELECT * FROM git_repositories WHERE pair_id=?", (pair_id,))
        if not repo:
            raise RuntimeError("Pair 尚未创建仓库")
        path = Path(repo["local_root"]) / arm
        status = run_command(["git", "status", "--porcelain"], cwd=path).stdout.strip()
        if status:
            run_command(["git", "add", "-A"], cwd=path)
            run_command(["git", "commit", "-m", "Deliver %s implementation" % arm], cwd=path)
        sha = run_command(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
        run_command(["git", "push", "origin", "HEAD:%s" % arm], cwd=path, timeout=180)
        remote_sha = run_command(["git", "ls-remote", "origin", "refs/heads/%s" % arm], cwd=path).stdout.split()[0]
        if sha != remote_sha:
            raise RuntimeError("%s 远端 SHA 校验失败" % arm)
        column = "a_sha" if arm == "A" else "b_sha"
        self.db.execute("UPDATE git_repositories SET %s=?,updated_at=? WHERE id=?" % column, (sha, now_iso(), repo["id"]))
        return sha
