"""Local web dashboard (stdlib http.server, binds to 127.0.0.1 by default)."""

import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, commands, db, paths, report, tasks

HTML = Path(__file__).with_name("dashboard.html")
BIN = Path(__file__).resolve().parent.parent / "bin" / "tally"


class Handler(BaseHTTPRequestHandler):
    server_version = "tally/" + __version__

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _host_allowed(self) -> bool:
        """Reject foreign Host headers (DNS rebinding): a web page whose name
        resolves to 127.0.0.1 must not be able to read the task data."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = {"127.0.0.1", "localhost", "[::1]", str(self.server.server_address[0]).lower()}
        return host in allowed or host in {"%s:%d" % (h, port) for h in allowed}

    def _origin_ok(self) -> bool:
        """Writes need same-origin: a custom header (forms cannot send one,
        cross-site fetch would need a CORS preflight we never answer) and, if
        the browser sends an Origin, our own."""
        if self.headers.get("X-Tally") != "1":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = origin.split("://", 1)[-1].lower()
        port = self.server.server_address[1]
        return host in {"%s:%d" % (h, port) for h in ("127.0.0.1", "localhost", "[::1]")}

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def do_GET(self):
        if not self._host_allowed():
            self._send(403, b"forbidden host", "text/plain; charset=utf-8")
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        arg = lambda k, d=None: (q.get(k) or [d])[0]   # noqa: E731
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
                return
            if url.path == "/api/health":
                self._json({"app": "tally", "version": __version__})
                return
            if url.path == "/api/sessions":
                from . import sessions
                days = arg("days", "30")
                days = float(days) if days else None
                agent = arg("agent") or None
                # progress first: a refresh ending during overview() still reports
                # running, so the page polls once more and gets the final numbers
                ix = sessions.refresh_async(days, min_interval=0 if arg("refresh") == "1" else 20)
                ov = sessions.overview(days, agent=agent, project=arg("project") or None,
                                       limit=int(arg("limit") or 500))
                ov["indexing"] = ix
                self._json(ov)
                return
            m = re.match(r"^/api/sessions/([^/]+)$", url.path)
            if m:
                from urllib.parse import unquote
                from . import sessions
                rep = sessions.detail(unquote(m.group(1)))
                if rep is None or rep.get("empty"):
                    self._json({"error": "not found"}, 404)
                    return
                self._json(rep)
                return
            conn = db.connect()
            try:
                if url.path == "/api/tasks":
                    rows = commands.list_rows(conn, int(arg("limit", "100")), project=arg("project") or None)
                    self._json({"tasks": rows, "now": time.time()})
                    return
                m = re.match(r"^/api/tasks/(\d+)$", url.path)
                if m:
                    t = tasks.get(conn, int(m.group(1)))
                    if t is None:
                        self._json({"error": "not found"}, 404)
                        return
                    rep = report.summary_for(conn, t)
                    rep["prompt_list"] = [dict(r) for r in tasks.prompts(conn, t["id"])]
                    self._json(rep)
                    return
                if url.path == "/api/projects":
                    self._json({"projects": _projects(conn, full=arg("full") == "1")})
                    return
                m = re.match(r"^/api/projects/([^/]+)$", url.path)
                if m:
                    from urllib.parse import unquote
                    from . import projects
                    days = arg("days")
                    self._json(projects.stats(conn, unquote(m.group(1)), days=int(days) if days else None,
                                              series_days=int(arg("series") or 30)))
                    return
                if url.path == "/api/todos":
                    self._json({"todos": _todos(conn, arg("project") or None, arg("status", "board")),
                                "now": time.time()})
                    return
                m = re.match(r"^/api/todos/(\d+)$", url.path)
                if m:
                    from . import cli, todos
                    t = todos.get(conn, int(m.group(1)))
                    if t is None:
                        self._json({"error": "not found"}, 404)
                        return
                    self._json(cli._todo_json(conn, t, with_children=True))
                    return
                if url.path == "/api/settings":
                    from . import settings
                    self._json({"settings": [{"key": k, "value": v, "description": d, "allowed": a}
                                             for k, v, d, a in settings.describe()]})
                    return
            finally:
                conn.close()
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # pragma: no cover - defensive
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        if not self._host_allowed() or not self._origin_ok():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        url = urlparse(self.path)
        try:
            body = self._body()
            conn = db.connect()
            try:
                from . import settings, todos
                if url.path == "/api/todos":
                    spec = todos.parse_quick(str(body.get("text") or body.get("title") or ""),
                                             body.get("project") or None)
                    t = todos.add(conn, spec["title"], project=spec["project"], priority=spec["priority"],
                                  due_at=spec["due_at"], notes=spec["notes"] or body.get("notes"),
                                  source="dashboard")
                    self._json(dict(t), 201)
                    return
                m = re.match(r"^/api/todos/(\d+)$", url.path)
                if m:
                    tid = int(m.group(1))
                    if body.get("delete"):
                        todos.delete(conn, tid)
                        self._json({"deleted": tid})
                        return
                    fields = {k: body[k] for k in ("title", "notes", "project", "priority", "status") if k in body}
                    if "due" in body:
                        fields["due_at"] = todos.parse_due(body["due"]) if body["due"] else None
                    if fields.get("status") == "done":
                        for task in conn.execute("SELECT * FROM tasks WHERE todo_id = ? AND status = 'active'",
                                                 (tid,)).fetchall():
                            tasks.end(conn, task["id"], note="todo done (dashboard)")
                    self._json(dict(todos.update(conn, tid, **fields)))
                    return
                if url.path == "/api/settings":
                    self._json({"key": body.get("key"), "value": settings.set_value(str(body.get("key")),
                                                                                    body.get("value"))})
                    return
                m = re.match(r"^/api/projects/([^/]+)$", url.path)
                if m:
                    from urllib.parse import unquote
                    from . import projects
                    with db.transaction(conn):
                        row = projects.set_mode(conn, unquote(m.group(1)), str(body.get("mode")))
                    self._json(dict(row))
                    return
            finally:
                conn.close()
            self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._json({"error": str(exc)}, 500)


def _projects(conn, full: bool = False) -> list:
    from . import projects
    out = []
    for p in projects.all_names(conn):
        n_open = conn.execute("SELECT COUNT(*) FROM todos WHERE project = ? AND parent_id IS NULL AND status IN "
                              "('todo','doing','blocked')", (p,)).fetchone()[0]
        n_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE project = ?", (p,)).fetchone()[0]
        d = {"project": p, "open_todos": n_open, "tasks": n_tasks, "mode": projects.mode_of(conn, p)}
        if full:
            st = projects.stats(conn, p, series_days=1)
            d.update({"last_30d": st["last_30d"], "totals": st["totals"], "sessions": st["sessions"],
                      "last_activity": st["last_activity"], "task_share": st["tasks"]["share"]})
        out.append(d)
    return out


def _todos(conn, project, status) -> list:
    """Top-level todos with progress and measured work. status: board (open +
    done in the last 14 days), open, done, all."""
    from . import todos
    now = time.time()
    st = {"open": todos.OPEN, "done": ("done", "dropped"), "all": None}.get(status, None)
    rows = todos.list_todos(conn, project=project, statuses=st)
    if status == "board":
        rows = [r for r in rows if r["status"] in todos.OPEN or (r["done_at"] or r["updated_at"]) > now - 14 * 86400]
    out = []
    for r in rows:
        d = dict(r)
        d["progress"] = list(todos.progress(conn, r["id"]))
        d["work"] = todos.linked_stats(conn, r["id"])
        d["steps"] = [dict(c) for c in todos.children(conn, r["id"])]
        out.append(d)
    return out


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/" % (host, port)
    from . import fmt
    print("%s · dashboard on %s (Ctrl+C to stop)" % (fmt.BRAND, url), flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def is_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen("http://%s:%d/api/health" % (host, port), timeout=0.5) as r:
            return json.loads(r.read()).get("app") == "tally"
    except Exception:
        return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def ensure_running(open_browser: bool = False):
    cfg = paths.load_config()
    host = cfg.get("dashboard_host", "127.0.0.1")
    port = int(cfg.get("dashboard_port", 8765))
    url = "http://%s:%d/" % (host, port)
    started = False
    if not is_running(host, port):
        if not _port_free(host, port):
            raise RuntimeError("port %d is used by another program; set another with "
                               "`tally config dashboard_port 8766`" % port)
        paths.home().mkdir(parents=True, exist_ok=True)
        with open(paths.home() / "dashboard.log", "a") as log:
            subprocess.Popen(
                [sys.executable, str(BIN), "dashboard", "--host", host, "--port", str(port)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
            )
        for _ in range(30):
            time.sleep(0.1)
            if is_running(host, port):
                break
        started = True
    if open_browser:
        webbrowser.open(url)
    return url, started
