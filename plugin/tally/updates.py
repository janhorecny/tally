"""New versions: a daily check in the background and `tally update`.

The check is one GET of the latest GitHub release (the plugin manifest on
main if the API is unavailable; setting updates.check, on by default;
TALLY_NO_UPDATE_CHECK=1 turns it off too). Nothing about the user is sent. It runs in a detached process, so hooks and commands never wait
for the network; they only read the result, ~/.claude/tally/update.json.

A newer version is announced at most once a day per place: the session start
message of every agent, the CLI (on a terminal) and the dashboard header.

`tally update` pulls the git checkout Tally runs from (the one-line installer
clones it to ~/.local/share/tally), installs the new version into the agents
it is installed in, restarts a running dashboard and shows what changed.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__, db, fmt, paths, settings

REPO_URL = "https://github.com/janhorecny/tally"
RELEASE_URL = "https://api.github.com/repos/janhorecny/tally/releases/latest"     # fresh; 60 requests/h per IP
MANIFEST_URL = "https://raw.githubusercontent.com/janhorecny/tally/main/plugin/.claude-plugin/plugin.json"  # cached 5 min
INSTALL_LINE = "curl -fsSL https://raw.githubusercontent.com/janhorecny/tally/main/install.sh | bash"
DAY = 86400
RETRY_S = 6 * 3600                       # after a failed check
NOTICE_EVERY = DAY


def cache_path() -> Path:
    return paths.home() / "update.json"


def enabled() -> bool:
    if os.environ.get("TALLY_NO_UPDATE_CHECK"):
        return False
    return settings.get("updates.check") is not False


def parse(version: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(version or ""))[:3]) or (0,)


def newer(a: Optional[str], b: str = __version__) -> bool:
    """a is a later version than b."""
    return bool(a) and parse(a) > parse(b)


# ------------------------------------------------------------------ the check

def state() -> dict:
    try:
        data = json.loads(cache_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(data))
    os.replace(str(tmp), str(path))


def check_now(timeout: float = 5.0) -> dict:
    """Fetch the latest version now; stores and returns {latest, checked_at, error}."""
    import urllib.request
    urls = [os.environ["TALLY_UPDATE_URL"]] if os.environ.get("TALLY_UPDATE_URL") else [RELEASE_URL, MANIFEST_URL]
    data = dict(state(), attempted_at=time.time())
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tally/" + __version__,
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            latest = str(doc.get("tag_name") or doc["version"]).lstrip("v")      # a release, or the manifest
            data.update(latest=latest, checked_at=time.time(), error=None)
            break
        except Exception as exc:             # offline, rate limited, moved: the next URL, else later
            data.update(error=str(exc)[:200])
    _save(data)
    return data


def due(now: Optional[float] = None) -> bool:
    now = now or time.time()
    st = state()
    last = st.get("attempted_at") or 0
    return now - last >= (RETRY_S if st.get("error") else DAY)


def check_in_background() -> None:
    """Start a detached check when one is due. Returns at once; never raises."""
    try:
        if not enabled() or not due():
            return
        _save(dict(state(), attempted_at=time.time()))      # concurrent hooks start only one
        from .installer import BIN
        subprocess.Popen([sys.executable, str(BIN), "update", "--check", "--quiet"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception:
        pass


def available() -> Optional[str]:
    """The newer version found by the last check, if any (no network)."""
    latest = state().get("latest")
    return latest if enabled() and newer(latest) else None


def status() -> dict:
    """For the dashboard and tally doctor (no network)."""
    st = state()
    return {"current": __version__, "latest": st.get("latest"), "available": available(),
            "checked_at": st.get("checked_at"), "error": st.get("error"), "enabled": enabled(),
            "command": update_command()}


# ------------------------------------------------------------------ notices

def update_command() -> str:
    """What to run to update this installation."""
    if source_root() is not None:
        return "tally update"
    if _marketplace().get("source", {}).get("source") in ("github", "git", "url"):
        return "claude plugin marketplace update tally && claude plugin update tally@tally"
    return INSTALL_LINE


def notice(where: str, conn=None) -> Optional[str]:
    """A two-line message about a newer version, at most once a day per place
    (session, cli); None otherwise."""
    latest = available()
    if not latest:
        return None
    own = conn is None
    conn = conn or db.connect()
    try:
        key = "update_notice:" + where
        try:
            last = json.loads(db.kv_get(conn, key) or "{}")
        except ValueError:
            last = {}
        if last.get("version") == latest and time.time() - float(last.get("ts", 0)) < NOTICE_EVERY:
            return None
        db.kv_set(conn, key, json.dumps({"version": latest, "ts": time.time()}))
    finally:
        if own:
            conn.close()
    return "\n".join([fmt.head("Tally %s is out (you have %s)" % (latest, __version__), "⬆"),
                      fmt.hints("%s to update" % update_command(), "what's new: %s/releases" % REPO_URL)])


# ------------------------------------------------------------------ tally update

def _git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise UpdateError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                          else "git %s failed" % " ".join(args))
    return r.stdout.strip()


class UpdateError(Exception):
    pass


def _marketplace() -> dict:
    try:
        data = json.loads((paths.claude_dir() / "plugins" / "known_marketplaces.json").read_text())
        return data.get("tally") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _is_checkout(p: Optional[Path]) -> bool:
    return bool(p) and (p / ".git").exists() and (p / "plugin" / "bin" / "tally").exists()


def source_root() -> Optional[Path]:
    """The git checkout this installation comes from: the one this code runs
    from, else the folder Claude Code's marketplace was added from (the plugin
    itself runs from a copy in Claude Code's cache)."""
    from .installer import REPO
    if _is_checkout(REPO):
        return REPO
    src = _marketplace().get("source") or {}
    if src.get("source") != "directory":       # GitHub: Claude Code's own clone, updated by Claude Code
        return None
    loc = src.get("path") or _marketplace().get("installLocation")
    p = Path(loc).expanduser() if loc else None
    return p if _is_checkout(p) else None


def _version_in(root: Path) -> str:
    text = (root / "plugin" / "tally" / "__init__.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "?"


def changes(root: Path, old: str, new: str) -> List[str]:
    """CHANGELOG headlines of the versions after `old` up to `new`."""
    try:
        text = (root / "CHANGELOG.md").read_text()
    except OSError:
        return []
    out, keep = [], False
    for line in text.splitlines():
        m = re.match(r"^## (\S+)", line)
        if m:
            keep = newer(m.group(1), old) and not newer(m.group(1), new)
            if keep:
                out.append("  %s" % line[3:])
            continue
        b = re.match(r"^- \*\*(.+?)\*\*", line)
        if keep and b:
            out.append("    · %s" % b.group(1).rstrip(":"))
    return out


def update(say=print) -> int:
    """Bring this installation to the latest version. Returns an exit code."""
    root = source_root()
    if root is None:
        cmd = update_command()
        if cmd.startswith("claude plugin"):
            from .installer import _run
            import shutil
            if shutil.which("claude"):
                _run(["claude", "plugin", "marketplace", "update", "tally"])
                r = _run(["claude", "plugin", "update", "tally@tally"])
                say(fmt.head("Claude Code plugin updated" if r.returncode == 0 else "Claude Code could not update it",
                             "✓" if r.returncode == 0 else "✗"))
                say(fmt.hints(installer_restart()))
                return r.returncode
            say(fmt.head("Update it with: %s" % cmd))
            return 1
        say(fmt.error("This copy of Tally is not a git checkout, so it cannot update itself",
                      "install it again: " + INSTALL_LINE))
        return 1
    old = __version__
    try:
        if _git(root, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("%s has local changes; commit or stash them first" % root)
        if not _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False):
            raise UpdateError("the branch in %s has no upstream to update from" % root)
        say("  checking %s …" % root)
        _git(root, "fetch", "--quiet")
        behind = int(_git(root, "rev-list", "--count", "HEAD..@{u}") or 0)
        if behind:
            _git(root, "merge", "--ff-only", "--quiet", "@{u}")
    except UpdateError as exc:
        say(fmt.error("Update failed: %s" % exc))
        return 1
    new = _version_in(root)
    _save(dict(state(), latest=new, checked_at=time.time(), error=None))
    if not behind:
        had = _claude_plugin_version()
        if had and had != new:                   # Claude Code runs a copy: install the checkout's version
            say(fmt.head("Tally %s: installing it into the agents (Claude Code had %s)" % (new, had), "✓"))
            rc = _reinstall(root, say)
            _restart_dashboard(root, say)
            say(fmt.hints(installer_restart()))
            return rc
        say(fmt.head("Tally %s is up to date" % new, "✓"))
        return 0
    say(fmt.head("Updated Tally %s → %s" % (old, new), "✓"))
    rc = _reinstall(root, say)
    _restart_dashboard(root, say)
    lines = changes(root, old, new)
    if lines:
        say("")
        say("  What's new")
        for line in lines[:30]:
            say(line)
        if len(lines) > 30:
            say("    …")
    say(fmt.hints("full notes: %s/blob/main/CHANGELOG.md" % REPO_URL,
                  installer_restart()))
    return rc


def _claude_plugin_version() -> Optional[str]:
    """The version of the copy Claude Code runs, if the plugin is installed."""
    try:
        data = json.loads((paths.claude_dir() / "plugins" / "installed_plugins.json").read_text())
        entries = (data.get("plugins") or {}).get("tally@tally") or []
        return entries[0].get("version") if entries else None
    except (OSError, ValueError, AttributeError, IndexError):
        return None


def installer_restart() -> str:
    from .installer import RESTART
    return RESTART


def _reinstall(root: Path, say) -> int:
    """Install the new version (its own code, in a new process) where Tally is installed."""
    from .installer import installed
    agents = [a for a, on in installed().items() if on]
    if not agents:
        return 0
    r = subprocess.run([sys.executable, str(root / "plugin" / "bin" / "tally"), "install", "--yes",
                        "--agents", ",".join(agents)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in (r.stdout + r.stderr).splitlines():
        if line.startswith(("✓", "✗")):
            say("  " + line)
    return r.returncode


def _restart_dashboard(root: Path, say) -> None:
    from . import server
    cfg = paths.load_config()
    host, port = cfg.get("dashboard_host", "127.0.0.1"), int(cfg.get("dashboard_port", 8765))
    try:
        if server.health(host, port) is None:
            return
        server.stop(host, port)
        subprocess.run([sys.executable, str(root / "plugin" / "bin" / "tally"), "dashboard", "--background"],
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        say("  ✓ dashboard     restarted (http://%s:%d/)" % (host, port))
    except Exception:
        pass
