"""Local web dashboard (stdlib http.server, binds to 127.0.0.1 by default)."""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
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
        self.server.last_request = time.time()
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
                from . import updates
                updates.check_in_background()          # an open page checks in every minute
                self._json({"app": "tally", "version": __version__, "pid": os.getpid(), "update": updates.status()})
                return
            if url.path == "/api/export":
                from . import export
                scope, f = arg("scope", "summary"), arg("format")
                days = arg("days", "30")
                try:
                    content, name = export.export(scope, f or export.FORMATS.get(scope, ("md",))[0], ref=arg("ref"),
                                                  project=arg("project") or None, days=float(days) if days else None,
                                                  agent=arg("agent") or None)
                except export.ExportError as exc:
                    self._json({"error": str(exc)}, 404 if "not found" in str(exc) else 400)
                    return
                body = content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", export.MIME[f or export.FORMATS[scope][0]])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if arg("download") == "1":
                    self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
                self.end_headers()
                self.wfile.write(body)
                return
            if url.path == "/api/sessions":
                from . import sessions
                days = arg("days", "30")
                days = float(days) if days else None
                agent = arg("agent") or None
                # progress first: a refresh ending during overview() still reports
                # running, so the page polls once more and gets the final numbers
                ix = sessions.refresh_async(days, min_interval=0 if arg("refresh") == "1" else 20)
                if arg("list") == "not_tracked":         # described only: what can be added
                    rest = sessions.not_tracked(days, agent=agent, project=arg("project") or None)
                    self._json({"sessions": rest[: int(arg("limit") or 500)], "count": len(rest), "days": days,
                                "indexing": ix})
                    return
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
                    from . import projects, sessions
                    self._json({"projects": projects.listing(conn, with_usage=arg("full") == "1"),
                                "indexing": sessions.progress()})
                    return
                m = re.match(r"^/api/projects/([^/]+)$", url.path)
                if m:
                    from urllib.parse import unquote
                    from . import projects
                    self._json(projects.detail(conn, unquote(m.group(1))))
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
        self.server.last_request = time.time()
        if not self._host_allowed() or not self._origin_ok():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        url = urlparse(self.path)
        if url.path == "/api/shutdown":
            self._json({"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        try:
            body = self._body()
            conn = db.connect()
            try:
                from . import settings, todos
                m = re.match(r"^/api/tasks/(\d+)$", url.path)
                if m:
                    self._task_action(conn, int(m.group(1)), body)
                    return
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
                    from . import projects, sessions
                    with db.transaction(conn):
                        row = projects.set_mode(conn, unquote(m.group(1)), str(body.get("mode")))
                    if row["mode"] in ("track", "tasks"):
                        # read its sessions now, with all the history the agents still keep
                        sessions.refresh_async(None, force=True)
                    self._json(dict(row, indexing=sessions.progress()))
                    return
                m = re.match(r"^/api/sessions/([^/]+)$", url.path)
                if m:
                    from urllib.parse import unquote
                    from . import sessions
                    sid, action = unquote(m.group(1)), body.get("action")
                    if action == "add":
                        found = sessions.add(sid)
                        if found is None:
                            self._json({"error": "session %s not found" % sid}, 404)
                            return
                        self._json({"session_id": found["session_id"], "added": True})
                    elif action == "remove":
                        self._json({"session_id": sid, "removed": sessions.remove(sid)})
                    else:
                        raise ValueError("action must be add or remove")
                    return
            finally:
                conn.close()
            self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._json({"error": str(exc)}, 500)


    def _task_action(self, conn, task_id: int, body: dict) -> None:
        """end | rename | delete a task from the dashboard."""
        t = tasks.get(conn, task_id)
        if t is None:
            self._json({"error": "task #%d not found" % task_id}, 404)
            return
        action = body.get("action")
        if action == "end":
            if t["status"] != "active":
                raise ValueError("task #%d has already ended" % task_id)
            tasks.end(conn, task_id, note=str(body.get("note") or "") or None)
        elif action == "rename":
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("a name is required")
            tasks.rename(conn, task_id, name)
        elif action == "delete":
            tasks.delete(conn, task_id)
            self._json({"deleted": task_id})
            return
        else:
            raise ValueError("action must be end, rename or delete")
        self._json(dict(tasks.get(conn, task_id)))


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


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False, idle_minutes: float = 0) -> None:
    """Serve until Ctrl+C, /api/shutdown, SIGTERM, or idle_minutes without a request
    (an open page pings every minute, so this means: nobody has it open)."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.last_request = time.time()
    url = "http://%s:%d/" % (host, port)
    from . import fmt
    print("%s · dashboard on %s (Ctrl+C to stop%s)" % (
        fmt.BRAND, url, ", stops after %g min without an open page" % idle_minutes if idle_minutes else ""), flush=True)
    if idle_minutes:
        threading.Thread(target=_stop_when_idle, args=(httpd, idle_minutes * 60), daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _stop_when_idle(httpd, limit_s: float) -> None:
    while True:
        time.sleep(min(60.0, max(0.1, limit_s / 4)))
        if time.time() - httpd.last_request > limit_s:
            print("dashboard idle for %g min, stopping" % (limit_s / 60), flush=True)
            httpd.shutdown()
            return


def health(host: str, port: int):
    """The running dashboard's {app, version, pid}, or None."""
    try:
        with urllib.request.urlopen("http://%s:%d/api/health" % (host, port), timeout=0.5) as r:
            d = json.loads(r.read())
            return d if d.get("app") == "tally" else None
    except Exception:
        return None


def is_running(host: str, port: int) -> bool:
    return health(host, port) is not None


def _is_dashboard(args: list) -> bool:
    """A Python interpreter running the tally launcher with `dashboard`, and nothing
    else: a shell or an editor whose command line merely mentions both words must
    never match."""
    return (len(args) >= 3 and os.path.basename(args[0]).lower().startswith("python")
            and (args[1] == "tally" or args[1].endswith("/tally")) and args[2] == "dashboard")


def _dashboard_pids(port: int) -> list:
    """Processes started as `tally dashboard ... --port <port>` (versions before
    0.6 do not report their pid in /api/health)."""
    default_port = int(paths.load_config().get("dashboard_port", 8765))
    pids = []
    for pid, args in _processes():
        if not _is_dashboard(args):
            continue
        # without --port it runs on the configured port
        on = args[args.index("--port") + 1] if "--port" in args[:-1] else str(default_port)
        if on == str(port) and pid != os.getpid():
            pids.append(pid)
    return pids


def _processes():
    """(pid, argv) of the running processes: /proc on Linux (exact arguments, no ps
    needed), else ps."""
    if os.path.isdir("/proc/self"):
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    raw = Path("/proc", d, "cmdline").read_bytes()
                except OSError:
                    continue
                if raw:
                    yield int(d), raw.rstrip(b"\0").decode("utf-8", "replace").split("\0")
        return
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            yield int(pid), cmd.split()


def stop_outdated(host: str, port: int) -> bool:
    """Stop a dashboard left running by an older Tally (its API would not match
    the page). Returns True if one was stopped."""
    h = health(host, port)
    if h is None or h.get("version") == __version__:
        return False
    return _terminate(host, port, h)


def stop(host: str, port: int) -> bool:
    """Stop the dashboard on host:port, whatever its version. False if none runs."""
    h = health(host, port)
    return h is not None and _terminate(host, port, h)


def _terminate(host: str, port: int, h: dict) -> bool:
    pids = [int(h["pid"])] if str(h.get("pid", "")).isdigit() else _dashboard_pids(port)
    stopped = False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
    for _ in range(50 if stopped else 0):
        time.sleep(0.1)
        if _port_free(host, port):
            break
    return stopped


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def ensure_running(open_browser: bool = False):
    cfg = paths.load_config()
    host = cfg.get("dashboard_host", "127.0.0.1")
    port = int(cfg.get("dashboard_port", 8765))
    idle = cfg.get("dashboard_idle_minutes", 120)
    url = "http://%s:%d/" % (host, port)
    started = False
    stop_outdated(host, port)
    if not is_running(host, port):
        if not _port_free(host, port):
            raise RuntimeError("port %d is used by another program; set another with "
                               "`tally config dashboard_port 8766`" % port)
        paths.home().mkdir(parents=True, exist_ok=True)
        with open(paths.home() / "dashboard.log", "a") as log:
            subprocess.Popen(
                [sys.executable, str(BIN), "dashboard", "--host", host, "--port", str(port),
                 "--idle-minutes", str(idle or 0)],
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
