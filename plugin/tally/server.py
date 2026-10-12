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

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
                return
            if url.path == "/api/health":
                self._json({"app": "tally", "version": __version__})
                return
            conn = db.connect()
            try:
                if url.path == "/api/tasks":
                    limit = int((q.get("limit") or ["50"])[0])
                    self._json({"tasks": commands.list_rows(conn, limit), "now": time.time()})
                    return
                m = re.match(r"^/api/tasks/(\d+)$", url.path)
                if m:
                    t = tasks.get(conn, int(m.group(1)))
                    if t is None:
                        self._json({"error": "not found"}, 404)
                        return
                    rep = report.summary_for(conn, t)
                    rep["prompts"] = [dict(r) for r in tasks.prompts(conn, t["id"])]
                    self._json(rep)
                    return
            finally:
                conn.close()
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # pragma: no cover - defensive
            self._json({"error": str(exc)}, 500)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/" % (host, port)
    print("tally dashboard on %s (Ctrl+C to stop)" % url, flush=True)
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
        log = open(paths.home() / "dashboard.log", "a")
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
