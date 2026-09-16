import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.transaction() as c:
            c.executescript(SCHEMA)
            columns = {row[1] for row in c.execute("PRAGMA table_info(arm_runs)")}
            for name, definition in (
                ("result", "TEXT NOT NULL DEFAULT ''"),
                ("warning_at", "TEXT"),
            ):
                if name not in columns:
                    c.execute("ALTER TABLE arm_runs ADD COLUMN %s %s" % (name, definition))
            c.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.transaction() as c:
            cur = c.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.connect() as c:
            row = c.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.connect() as c:
            return [dict(row) for row in c.execute(sql, tuple(params)).fetchall()]

    def audit(self, event_type: str, entity_type: str = "", entity_id: str = "", detail: Any = None) -> None:
        self.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?)",
            (event_type, entity_type, entity_id, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
        )

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (key, json.dumps(value, ensure_ascii=False), now_iso()),
        )

    def page(self, table: str, page: int, size: int, where: str = "1=1", params: Iterable[Any] = (), order: str = "created_at DESC") -> Dict[str, Any]:
        allowed = {
            "tasks", "project_chains", "pairs", "codex_jobs", "bug_candidates",
            "artifact_checks", "recordings", "gsb_reviews", "audit_events", "git_repositories",
        }
        if table not in allowed:
            raise ValueError("unknown table")
        page, size = max(1, page), min(100, max(1, size))
        with self.connect() as c:
            total = c.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), tuple(params)).fetchone()[0]
            rows = c.execute(
                "SELECT * FROM %s WHERE %s ORDER BY %s LIMIT ? OFFSET ?" % (table, where, order),
                tuple(params) + (size, (page - 1) * size),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "page": page, "size": size, "total": total}


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  task_type TEXT NOT NULL CHECK(task_type IN ('zero_to_one','feature','bugfix')),
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  stack TEXT NOT NULL DEFAULT '',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  difficulty TEXT NOT NULL,
  difficulty_evidence_json TEXT NOT NULL DEFAULT '[]',
  baseline_path TEXT NOT NULL DEFAULT '',
  baseline_repo_url TEXT NOT NULL DEFAULT '',
  baseline_sha TEXT NOT NULL DEFAULT '',
  parent_pair_id TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'candidate',
  rejection_reason TEXT NOT NULL DEFAULT '',
  locked_by TEXT NOT NULL DEFAULT '',
  used_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_pool ON tasks(status,difficulty,task_type,created_at);
CREATE TABLE IF NOT EXISTS generation_batches (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  requested_count INTEGER NOT NULL,
  generated_count INTEGER NOT NULL DEFAULT 0,
  accepted_count INTEGER NOT NULL DEFAULT 0,
  rejected_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS project_chains (
  id TEXT PRIMARY KEY,
  root_task_id TEXT NOT NULL REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'active',
  followup_required INTEGER NOT NULL DEFAULT 1,
  followup_completed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pairs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  chain_id TEXT NOT NULL REFERENCES project_chains(id),
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT NOT NULL DEFAULT 'queued',
  repo_id TEXT NOT NULL DEFAULT '',
  baseline_sha TEXT NOT NULL DEFAULT '',
  winner TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pairs_status ON pairs(status,stage,created_at);
CREATE TABLE IF NOT EXISTS git_repositories (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL UNIQUE REFERENCES pairs(id),
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  visibility TEXT NOT NULL,
  remote_url TEXT NOT NULL DEFAULT '',
  local_root TEXT NOT NULL,
  main_sha TEXT NOT NULL DEFAULT '',
  a_sha TEXT NOT NULL DEFAULT '',
  b_sha TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planned',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arm_runs (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  branch TEXT NOT NULL CHECK(branch IN ('A','B')),
  workspace_path TEXT NOT NULL,
  container_name TEXT NOT NULL,
  screen_name TEXT NOT NULL,
  model TEXT NOT NULL,
  image TEXT NOT NULL,
  image_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  prompt_sent_at TEXT,
  finished_at TEXT,
  session_id TEXT NOT NULL DEFAULT '',
  prompt_id TEXT NOT NULL DEFAULT '',
  trace_path TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  exit_code INTEGER,
  result TEXT NOT NULL DEFAULT '',
  warning_at TEXT,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pair_id,arm)
);
CREATE TABLE IF NOT EXISTS codex_jobs (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  job_type TEXT NOT NULL,
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  cwd TEXT NOT NULL DEFAULT '',
  input_path TEXT NOT NULL DEFAULT '',
  schema_path TEXT NOT NULL DEFAULT '',
  events_path TEXT NOT NULL DEFAULT '',
  output_path TEXT NOT NULL DEFAULT '',
  exit_code INTEGER,
  result_json TEXT NOT NULL DEFAULT '{}',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codex_jobs_status ON codex_jobs(status,job_type,created_at);
CREATE TABLE IF NOT EXISTS bug_candidates (
  id TEXT PRIMARY KEY,
  source_pair_id TEXT NOT NULL REFERENCES pairs(id),
  source_arm TEXT NOT NULL CHECK(source_arm IN ('A','B')),
  source_sha TEXT NOT NULL,
  title TEXT NOT NULL,
  preconditions TEXT NOT NULL,
  reproduction_steps_json TEXT NOT NULL,
  actual_result TEXT NOT NULL,
  expected_result TEXT NOT NULL,
  reproduce_count INTEGER NOT NULL DEFAULT 0,
  difficulty TEXT NOT NULL DEFAULT 'pending',
  difficulty_evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'candidate',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_checks (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  commit_sha TEXT NOT NULL,
  compose_file TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  checks_json TEXT NOT NULL DEFAULT '[]',
  started_at TEXT,
  finished_at TEXT,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pair_id,arm,commit_sha)
);
CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL REFERENCES pairs(id),
  arm TEXT NOT NULL CHECK(arm IN ('A','B')),
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  duration_seconds REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pair_id,arm)
);
CREATE TABLE IF NOT EXISTS gsb_reviews (
  id TEXT PRIMARY KEY,
  pair_id TEXT NOT NULL UNIQUE REFERENCES pairs(id),
  verdict TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  confirmed_by TEXT NOT NULL DEFAULT '',
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT '',
  entity_id TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""
