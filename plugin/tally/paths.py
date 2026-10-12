"""Filesystem locations and user configuration."""

import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    # "off": tasks only start via /tally:start or /tally:new.
    # "session": the first prompt of every session starts a task automatically.
    "auto_start": "off",
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8765,
    # Per-model price overrides, $ per million tokens, e.g.
    # {"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2,
    #                      "cache_write_5m": 5, "cache_write_1h": 8}}
    "pricing": {},
    "log": {
        # summary: task totals and the agent tree
        # prompts: + per-prompt breakdown (tokens, cost, time, agents per prompt)
        # calls:   + tool-call statistics (which tools, how often, errors)
        "depth": "prompts",
        # full | preview (first 300 characters) | off (prompt text never stored)
        "prompts": "full",
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
    home().mkdir(parents=True, exist_ok=True)
    with open(config_path(), "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
