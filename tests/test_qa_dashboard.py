"""Dashboard server, installer and release-safety regressions (QA review of 0.11.8).

Run:  python3 -m unittest discover -s tests -q

Every server here listens on a free port, never on 8765 (the user's own dashboard),
and every path points at a temporary folder (test_tally's Env and safety net).
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    import test_tally as base
except ImportError:                      # run as tests.test_qa_dashboard
    from tests import test_tally as base

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
POSIX = os.name != "nt"


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in (0, 8765):        # never the user's dashboard
            return port


class TestServer:
    """server.Handler on a free port, in a thread."""

    def __enter__(self):
        from tally import server
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        assert self.port not in (0, 8765)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.port
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, body=None):
        """(status, headers, parsed JSON or text)."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json", "X-Tally": "1"})
        try:
            resp = NO_PROXY.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            resp = exc
        with resp:
            raw = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type") or ""
            return resp.getcode(), resp.headers, json.loads(raw) if ctype.startswith("application/json") else raw


class DashboardServer(unittest.TestCase):
    def setUp(self):
        self.env = base.Env()

    def tearDown(self):
        self.env.cleanup()

    def test_health_ignores_http_proxy(self):
        """urllib sends 127.0.0.1 through http_proxy unless no_proxy says otherwise: the
        dashboard looked like "another program" on the port, and was never restarted."""
        from tally import server
        with TestServer() as s, unittest.mock.patch.dict(os.environ), \
                unittest.mock.patch.object(urllib.request, "_opener", None):     # urlopen builds a new one
            for k in ("no_proxy", "NO_PROXY"):
                os.environ.pop(k, None)
            os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"   # nothing listens there
            h = server.health("127.0.0.1", s.port)
        self.assertEqual((h or {}).get("app"), "tally")

    def test_a_newer_dashboard_is_kept(self):
        """0.11.8 stopped a 0.12.0 dashboard: a session not restarted after the update put the old one back."""
        from tally import server
        port = free_port()
        script = Path(tempfile.mkdtemp(prefix="tc-qa-fake-")) / "bin" / "tally"
        script.parent.mkdir()
        script.write_text(base.DashboardProcess.FAKE)
        self.addCleanup(shutil.rmtree, str(script.parent.parent), True)
        proc = subprocess.Popen([sys.executable, str(script), "dashboard", "--port", str(port), "99.0.0", "pid"],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        self.assertEqual(server.health("127.0.0.1", port)["version"], "99.0.0")
        self.assertFalse(server.stop_outdated("127.0.0.1", port))
        self.assertIsNone(proc.poll())                                   # still running

    def test_todo_priority_is_checked(self):
        """"x" was stored, and every todo list (dashboard, CLI, session start) failed on it."""
        from tally import db, todos
        t = todos.add(db.connect(), "pick a priority")
        with TestServer() as s:
            for bad in ("x", 7, -1, None, [2]):
                code, _, body = s.request("/api/todos/%d" % t["id"], {"priority": bad})
                self.assertEqual(code, 400, (bad, body))
            code, _, body = s.request("/api/todos/%d" % t["id"], {"priority": "2"})
            self.assertEqual((code, body["priority"]), (200, 2))
            code, _, body = s.request("/api/todos")
            self.assertEqual((code, body["todos"][0]["priority"]), (200, 2))

    def test_no_page_may_frame_the_dashboard(self):
        with TestServer() as s:
            for path in ("/", "/api/health", "/api/tasks", "/api/nothing", "/api/export?scope=projects&format=csv&download=1"):
                _, headers, _ = s.request(path)
                self.assertEqual(headers.get("X-Frame-Options"), "DENY", path)
                self.assertEqual(headers.get("Content-Security-Policy"), "frame-ancestors 'none'", path)
            _, _, page = s.request("/")
            self.assertIn("<script>", page)                               # the inline code is not blocked by a CSP

    def test_download_names_beyond_latin1(self):
        """send_header encodes Latin-1: a name with other characters broke the response
        after its status line was sent. Now an ASCII name plus filename* (RFC 6266)."""
        from tally import export
        with TestServer() as s, unittest.mock.patch.object(export, "export", return_value=("# x\n", "tally-čaj—report.md")):
            code, headers, body = s.request("/api/export?scope=summary&format=md&download=1")
        self.assertEqual((code, body), (200, "# x\n"))
        cd = headers["Content-Disposition"]
        self.assertTrue(cd.startswith('attachment; filename="tally-_aj_report.md"'), cd)
        self.assertIn("filename*=UTF-8''tally-%C4%8Daj%E2%80%94report.md", cd)

    def test_task_list_total_and_filter(self):
        """The dashboard lists the newest 500 tasks: it shows "500 of 602" and filters
        on the server, so an older task can still be found."""
        from tally import db, tasks
        conn = db.connect()
        for i, name in enumerate(("Fix login", "Write docs", "Ship release")):
            t, _ = tasks.start(conn, "sess-qa-%d" % i, name=name, project="web")
            if i < 2:
                tasks.end(conn, t["id"])
        conn.commit()
        with TestServer() as s:
            def get(query):
                code, _, d = s.request("/api/tasks?" + query)
                self.assertEqual(code, 200, d)
                return d
            d = get("limit=2")
            self.assertEqual((len(d["tasks"]), d["total"], d["matched"]), (2, 3, 3))
            d = get("limit=2&q=LOGIN")
            self.assertEqual(([t["name"] for t in d["tasks"]], d["total"], d["matched"]), (["Fix login"], 3, 1))
            self.assertEqual([t["id"] for t in get("limit=5&q=%232")["tasks"]], [2])            # "#2"
            d = get("limit=5&status=ended")
            self.assertEqual(([t["name"] for t in d["tasks"]], d["matched"]), (["Write docs", "Fix login"], 2))
            self.assertEqual([t["name"] for t in get("limit=5&status=active&project=web")["tasks"]], ["Ship release"])
            self.assertEqual(get("limit=5&project=other")["total"], 0)
            # a filtered row is the row of the plain list (the running task's times move: the fields)
            row, plain = get("limit=5&q=ship")["tasks"][0], get("limit=1")["tasks"][0]
            self.assertEqual((sorted(row), row["id"], row["total"]), (sorted(plain), plain["id"], plain["total"]))


class DashboardStart(unittest.TestCase):
    """ensure_running (/tally:dashboard, tally dashboard --background) on a free port."""

    SERVE = (
        "import json, os, sys, threading\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "args = sys.argv[1:]\n"
        "port = int(args[args.index('--port') + 1])\n"
        "assert port not in (0, 8765), port\n"
        "threading.Timer(60, os._exit, (0,)).start()\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "    def do_GET(self):\n"
        "        b = json.dumps({'app': 'tally', 'version': %r, 'pid': os.getpid()}).encode()\n"
        "        self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers()\n"
        "        self.wfile.write(b)\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n")

    def setUp(self):
        self.env = base.Env()
        from tally import paths, server, settings
        self.port = free_port()
        settings.set_value("dashboard_port", self.port)                 # never the real dashboard on 8765
        self.assertEqual(int(paths.load_config()["dashboard_port"]), self.port)
        self.addCleanup(server.stop, "127.0.0.1", self.port)

    def tearDown(self):
        self.env.cleanup()

    def fake_bin(self, code: str) -> Path:
        d = Path(tempfile.mkdtemp(prefix="tc-qa-bin-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / "tally").write_text(code)
        return d / "tally"

    def test_a_dashboard_that_does_not_start_is_an_error(self):
        """It said "✓ Dashboard started" and opened a URL nobody answered."""
        from tally import paths, server
        broken = self.fake_bin("import sys\nsys.stderr.write('Traceback\\nSyntaxError: broken working tree\\n')\n"
                               "sys.exit(1)\n")
        with unittest.mock.patch.object(server, "BIN", broken), \
                unittest.mock.patch.object(server.webbrowser, "open") as browser:
            with self.assertRaises(RuntimeError) as cm:
                server.ensure_running(open_browser=True)
        self.assertIn(str(paths.home() / "dashboard.log"), str(cm.exception))
        self.assertIn("SyntaxError: broken working tree", str(cm.exception))
        browser.assert_not_called()

    def test_started_when_it_answers(self):
        from tally import __version__, server
        with unittest.mock.patch.object(server, "BIN", self.fake_bin(self.SERVE % __version__)):
            self.assertEqual(server.ensure_running(), ("http://127.0.0.1:%d/" % self.port, True))
            self.assertTrue(server.is_running("127.0.0.1", self.port))
            self.assertEqual(server.ensure_running(), ("http://127.0.0.1:%d/" % self.port, False))   # already running


class InstallerFiles(unittest.TestCase):
    def setUp(self):
        self.env = base.Env()

    def tearDown(self):
        self.env.cleanup()

    @unittest.skipUnless(POSIX, "python3 from PATH is the POSIX command")
    def test_codex_hooks_run_python3_from_path(self):
        """The absolute interpreter path (a Homebrew Python) broke every Codex hook once it was removed."""
        from tally import installer
        with unittest.mock.patch.object(installer.shutil, "which", return_value="/usr/bin/python3"):
            cmd = installer._hook_cmd("Stop", "codex")
        self.assertEqual(cmd, 'python3 "%s" hook Stop --agent codex' % installer.BIN)
        self.assertTrue(installer._is_tally({"hooks": [{"command": cmd}]}))
        with unittest.mock.patch.object(installer.shutil, "which", return_value=None):      # no python3 command
            self.assertTrue(installer._hook_cmd("Stop", "codex").startswith('"%s" "' % sys.executable))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlinked_claude_settings_stay_a_symlink(self):
        """os.replace put a regular file (mode 644) in place of a dotfiles symlink."""
        from tally import onboarding
        claude = Path(os.environ["CLAUDE_CONFIG_DIR"])
        claude.mkdir(parents=True, exist_ok=True)
        dot = self.env.tmp / "dotfiles"
        dot.mkdir()
        real = dot / "settings.json"
        real.write_text(json.dumps({"theme": "dark"}))
        real.chmod(0o600)
        link = claude / "settings.json"
        os.symlink(str(real), str(link))
        self.assertTrue(onboarding.add_statusline())
        self.assertTrue(link.is_symlink())
        data = json.loads(real.read_text())
        self.assertEqual((data["theme"], data["statusLine"]["command"]), ("dark", onboarding.statusline_command()))
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)
        self.assertEqual(json.loads((claude / "settings.json.bak-tally").read_text()), {"theme": "dark"})
        self.assertTrue(onboarding.remove_statusline())
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"theme": "dark"})
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in dot.iterdir()], ["settings.json"])           # no temporary file left


@unittest.skipUnless(POSIX and shutil.which("bash") and shutil.which("git"), "bash and git")
class Scripts(unittest.TestCase):
    """install.sh, the pre-push hook and release.sh, run on temporary folders only."""

    def setUp(self):
        self.env = base.Env()
        self.tmp = self.env.tmp
        self.genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.genv.update(HOME=str(self.tmp), GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        self.env.cleanup()

    def git(self, repo, *args):
        r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"] + list(args),
                           capture_output=True, text=True, env=self.genv, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def new_repo(self, name):
        repo = self.tmp / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        return repo

    def test_install_sh_finishes_after_a_failed_agent(self):
        """With set -e one failing agent ended the script: no ~/.local/bin/tally, no doctor."""
        home, fakebin, calls = self.tmp / "inst-home", self.tmp / "fakebin", self.tmp / "calls.log"
        home.mkdir()
        fakebin.mkdir()
        python = fakebin / "python3"             # the version check runs Python, the tally commands are stubs
        python.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exec "%s" "$@"; fi\necho "$2" >> "%s"\n'
                          'case "$2" in install) echo "x Codex  failed"; exit 3 ;; doctor) echo "doctor ran"; exit 1 ;; esac\n'
                          % (sys.executable, calls))
        python.chmod(0o755)
        env = {"HOME": str(home), "PATH": "%s:/usr/bin:/bin" % fakebin, "TALLY_HOME": str(self.tmp / "inst-tally")}
        r = subprocess.run(["bash", str(base.ROOT / "install.sh"), "--yes"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(calls.read_text().split(), ["install", "doctor"])
        self.assertEqual(os.readlink(str(home / ".local" / "bin" / "tally")), str(base.ROOT / "plugin" / "bin" / "tally"))
        self.assertIn("doctor ran", r.stdout)
        self.assertIn("Not every agent was installed", r.stdout)
        self.assertIn("Update any time", r.stdout)

    def test_pre_push_checks_the_pushed_commit(self):
        """It ran the checks on the working tree: an uncommitted fix hid a broken push."""
        repo = self.new_repo("push")
        (repo / ".githooks").mkdir()
        (repo / "scripts").mkdir()
        hook = repo / ".githooks" / "pre-push"
        shutil.copy2(str(base.ROOT / ".githooks" / "pre-push"), str(hook))
        check = repo / "scripts" / "check.sh"
        stub = '#!/bin/sh\necho "checks in $(cd "$(dirname "$0")/.." && pwd -P)"; exit %d\n'
        check.write_text(stub % 1)                                                   # broken in the commit
        check.chmod(0o755)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "broken")
        broken = self.git(repo, "rev-parse", "HEAD")
        check.write_text(stub % 0)                                                   # fixed in the working tree only

        def push(sha, local="refs/heads/main"):
            return subprocess.run(["sh", str(hook), "origin", "unused"], cwd=str(repo), env=self.genv, timeout=60,
                                  input="%s %s refs/heads/main %s\n" % (local, sha, "0" * 40),
                                  capture_output=True, text=True)
        r = push(broken)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the pushed commit, not the working tree", r.stdout)
        self.assertNotIn("checks in %s\n" % repo.resolve(), r.stdout)              # a temporary worktree
        self.git(repo, "commit", "-q", "-am", "fixed")
        fixed = self.git(repo, "rev-parse", "HEAD")
        r = push(fixed)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks in %s" % repo.resolve(), r.stdout)                    # clean HEAD: checked in place
        self.assertNotEqual(push(broken, "refs/heads/old").returncode, 0)           # another, broken branch
        self.assertEqual(push("0" * 40, "(delete)").returncode, 0)                  # a deleted ref: nothing to run
        self.assertEqual(len(self.git(repo, "worktree", "list").splitlines()), 1)   # the temporary worktrees are gone

    def test_release_refuses_a_version_not_above_the_latest_tag(self):
        from tally import __version__, updates
        repo = self.new_repo("release")
        shutil.copytree(str(base.PLUGIN / "tally"), str(repo / "plugin" / "tally"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        v = __version__
        (repo / "plugin" / ".claude-plugin").mkdir(parents=True)
        (repo / "plugin" / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": v}))
        (repo / ".claude-plugin").mkdir()
        (repo / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"metadata": {"version": v}, "plugins": [{"version": v}]}))
        (repo / "CHANGELOG.md").write_text("## %s (test)\n\n- **Test**\n" % v)
        (repo / ".gitignore").write_text("__pycache__/\n")
        (repo / "scripts").mkdir()
        shutil.copy2(str(base.ROOT / "scripts" / "release.sh"), str(repo / "scripts" / "release.sh"))
        (repo / "scripts" / "check.sh").write_text("#!/bin/sh\necho checks ran\n")
        (repo / "scripts" / "check.sh").chmod(0o755)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "release")

        def release():
            return subprocess.run(["bash", str(repo / "scripts" / "release.sh"), "--dry-run"], cwd=str(repo),
                                  env=self.genv, capture_output=True, text=True, timeout=60)
        major, minor, patch = updates.parse(v)
        higher = "v%d.%d.%d" % (major, minor, patch + 2)               # 0.11.10 after 0.11.8: numbers, not text
        self.git(repo, "tag", higher)
        r = release()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not higher than the latest tag %s" % higher, r.stderr)
        self.assertNotIn("checks ran", r.stdout)
        self.git(repo, "tag", "-d", higher)
        self.git(repo, "tag", "v0.0.1")
        r = release()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks ran", r.stdout)
        self.assertIn("Tally %s: ready" % v, r.stdout)


if __name__ == "__main__":
    unittest.main()
