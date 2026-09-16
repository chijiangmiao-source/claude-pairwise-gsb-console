import json
import mimetypes
import re
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .analytics import dashboard
from .config import APP_NAME, Config
from .db import Database, now_iso
from .service import PairwiseService


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, config: Config, db: Database, service: PairwiseService):
        super().__init__(address, handler)
        self.config = config
        self.db = db
        self.service = service


class Handler(BaseHTTPRequestHandler):
    server_version = "PairwiseGSB/0.1"

    @property
    def app(self) -> AppServer:
        return self.server  # type: ignore

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        try:
            path, query = self._path_query()
            if path == "/api/health":
                return self._json(200, {"ok": True, "name": APP_NAME, "time": now_iso()})
            if path == "/api/preflight":
                return self._json(200, self.app.service.preflight())
            if path == "/api/dashboard":
                return self._json(200, dashboard(self.app.db))
            if path == "/api/settings":
                rows = self.app.db.all("SELECT key,value_json,updated_at FROM settings ORDER BY key")
                return self._json(200, {row["key"]: json.loads(row["value_json"]) for row in rows})
            if path == "/api/tasks":
                return self._json(200, self._page("tasks", query, self._filter(query, ("status", "task_type", "difficulty"))))
            if path == "/api/pairs":
                return self._json(200, self._pairs_page(query))
            match = re.fullmatch(r"/api/pairs/([^/]+)", path)
            if match:
                return self._json(200, self.app.service.pair_detail(match.group(1)))
            if path == "/api/codex-jobs":
                return self._json(200, self._page("codex_jobs", query, self._filter(query, ("status", "job_type"))))
            if path == "/api/bug-candidates":
                return self._json(200, self._page("bug_candidates", query, self._filter(query, ("status", "difficulty"))))
            if path == "/api/artifact-checks":
                return self._json(200, self._page("artifact_checks", query, self._filter(query, ("status", "arm"))))
            if path == "/api/recordings":
                return self._json(200, self._page("recordings", query, self._filter(query, ("status", "arm"))))
            if path == "/api/gsb-reviews":
                return self._json(200, self._page("gsb_reviews", query, self._filter(query, ("status", "verdict"))))
            if path == "/api/audit":
                return self._json(200, self._page("audit_events", query, self._filter(query, ("event_type", "entity_type")), order="id DESC"))
            match = re.fullmatch(r"/api/operations/([^/]+)", path)
            if match:
                return self._json(200, self.app.service.operation(match.group(1)))
            return self._static(path)
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, _ = self._path_query()
            body = self._body()
            if path == "/api/tasks/import-historical":
                return self._json(200, self.app.service.import_historical(int(body.get("limit", 500))))
            if path == "/api/tasks/generate":
                operation = self.app.service.generate_tasks_async(int(body.get("count", 1)), str(body.get("taskType", "zero_to_one")))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/tasks/([^/]+)/validate", path)
            if match:
                operation = self.app.service.validate_task_async(match.group(1))
                return self._json(202, {"operationId": operation})
            if path == "/api/pairs":
                return self._json(201, self.app.service.create_pair(str(body.get("taskId", ""))))
            match = re.fullmatch(r"/api/pairs/([^/]+)/prepare", path)
            if match:
                operation = self.app.service.prepare_pair_repository_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/start", path)
            if match:
                operation = self.app.service.start_pair_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb", path)
            if match:
                operation = self.app.service.generate_gsb_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/bugs/discover", path)
            if match:
                operation = self.app.service.discover_bugs_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/features/generate", path)
            if match:
                operation = self.app.service.generate_followup_feature_async(match.group(1))
                return self._json(202, {"operationId": operation})
            match = re.fullmatch(r"/api/pairs/([^/]+)/recordings/([AB])/(start|stop)", path)
            if match:
                pair_id, arm, action = match.groups()
                if action == "start":
                    result = self.app.service.start_recording(pair_id, arm, int(body.get("x", 0)), int(body.get("y", 0)))
                else:
                    result = self.app.service.stop_recording(pair_id, arm)
                return self._json(200, result)
            match = re.fullmatch(r"/api/pairs/([^/]+)/gsb/confirm", path)
            if match:
                result = self.app.service.confirm_gsb(match.group(1), str(body.get("verdict", "")), str(body.get("reason", "")), str(body.get("confirmedBy", "人工确认")))
                return self._json(200, result)
            if path == "/api/settings":
                for key, value in body.items():
                    self.app.db.set_setting(str(key), value)
                self.app.db.audit("settings.updated", "settings", "", {"keys": list(body)})
                return self._json(200, {"ok": True})
            match = re.fullmatch(r"/api/bug-candidates/([^/]+)/(reproduce|convert)", path)
            if match:
                candidate_id, action = match.groups()
                if action == "reproduce":
                    operation = self.app.service.reproduce_bug_async(candidate_id)
                    return self._json(202, {"operationId": operation})
                return self._json(201, self.app.service.convert_bug_to_task(candidate_id))
            self._json(404, {"error": "接口不存在"})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def _path_query(self) -> Tuple[str, Dict[str, list]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return data

    def _json(self, status: int, value: Any) -> None:
        payload = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.app.config.web_dir / relative).resolve()
        root = self.app.config.web_dir.resolve()
        if root not in target.parents and target != root:
            return self._json(403, {"error": "拒绝访问"})
        if not target.exists() or not target.is_file():
            # SPA fallback for client-side routes.
            target = root / "index.html"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, table: str, query: Dict[str, list], condition, order: str = "created_at DESC") -> Dict[str, Any]:
        page = int((query.get("page") or ["1"])[0])
        size = int((query.get("size") or ["20"])[0])
        where, params = condition
        return self.app.db.page(table, page, size, where, params, order)

    @staticmethod
    def _filter(query: Dict[str, list], fields) -> Tuple[str, tuple]:
        clauses, params = [], []
        for field in fields:
            value = (query.get(field) or [""])[0].strip()
            if value:
                clauses.append("%s=?" % field)
                params.append(value)
        return " AND ".join(clauses) or "1=1", tuple(params)

    def _pairs_page(self, query: Dict[str, list]) -> Dict[str, Any]:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = min(100, max(1, int((query.get("size") or ["20"])[0])))
        clauses, params = [], []
        status = (query.get("status") or [""])[0].strip()
        if status:
            clauses.append("p.status=?")
            params.append(status)
        where = " AND ".join(clauses) or "1=1"
        count = self.app.db.one("SELECT COUNT(*) count FROM pairs p WHERE " + where, params) or {"count": 0}
        rows = self.app.db.all(
            """SELECT p.*,t.title,t.task_type,t.difficulty,g.verdict,g.reason,g.status gsb_status
               FROM pairs p JOIN tasks t ON t.id=p.task_id
               LEFT JOIN gsb_reviews g ON g.pair_id=p.id
               WHERE %s ORDER BY p.created_at DESC LIMIT ? OFFSET ?""" % where,
            tuple(params) + (size, (page - 1) * size),
        )
        return {"items": rows, "page": page, "size": size, "total": count["count"]}


def serve(config: Config, db: Database, service: PairwiseService) -> None:
    server = AppServer((config.host, config.port), Handler, config, db, service)
    print("%s running at http://%s:%s" % (APP_NAME, config.host, config.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
