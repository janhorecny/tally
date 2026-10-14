"""Filesystem locations and user configuration."""

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

DEFAULT_CONFIG = {
    # "off": tasks only start via /tally:start or /tally:new.
    # "session": the first prompt of every session starts a task automatically.
    "auto_start": "off",
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8765,
    # a background dashboard stops after this many minutes without an open page; 0 = never
    "dashboard_idle_minutes": 120,
    # Per-model price overrides, $ per million tokens, e.g.
    # {"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2,
    #                      "cache_write_5m": 5, "cache_write_1h": 8}}
    "pricing": {},
    "log": {
        # summary: task totals and the agent tree
        # prompts: + per-prompt breakdown (tokens, cost, time, agents per prompt)
        # calls:   + tool-call statistics (which tools, how often, errors)
        "depth": "calls",
        # full | preview (first 300 characters) | off (prompt text never stored)
        "prompts": "full",
    },
    "projects": {
        # how a project Tally has not seen yet is tracked: ask | track | tasks
        "default_mode": "ask",
    },
    "sessions": {
        # which sessions the overview reads from the agents' transcripts:
        # enabled: projects set to track or tasks, sessions with a task, sessions added one by one
        # all:     every session, except projects set to off
        "scope": "enabled",
    },
    "updates": {
        # once a day, ask GitHub for the latest version (one request, nothing sent) and say when one is out
        "check": True,
    },
    "todos": {
        # show open todos of the project when a Claude Code session starts
        "reminder": True,
        "reminder_limit": 5,
    },
}


def home() -> Path:
    override = os.environ.get("TALLY_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "tally"


def db_path() -> Path:
    return home() / "tally.db"


def config_path() -> Path:
    return home() / "config.json"


def error_log_path() -> Path:
    return home() / "errors.log"


def claude_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def projects_dir() -> Path:
    override = os.environ.get("TALLY_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return claude_dir() / "projects"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config() -> dict:
    cfg = _merge(DEFAULT_CONFIG, {})
    try:
        with open(config_path()) as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg = _merge(cfg, user)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    """Write config.json atomically: a temporary file in the same folder
    replaces it, so a hook reading it meanwhile sees the old settings or the
    new ones, never an empty or half-written file (which reads as defaults).
    The file keeps its mode; a symlinked config.json stays a symlink."""
    home().mkdir(parents=True, exist_ok=True)
    target = os.path.realpath(str(config_path()))
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        mode = None
    tmp = "%s.%d.%s.tmp" % (target, os.getpid(), os.urandom(4).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)      # a new file: as open() would (umask)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def config_lock():
    """Serialise read-modify-write of config.json between processes (two
    writers would otherwise each save their own change over the other's)."""
    home().mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:            # not POSIX: the atomic write still protects readers
        yield
        return
    with open(str(home() / "config.lock"), "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
