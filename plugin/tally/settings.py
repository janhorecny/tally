"""User-facing settings: validated get/set of dotted keys in config.json."""

from typing import Any, List, Tuple

from . import paths

# key: (allowed values or type, description)
SCHEMA = {
    "log.depth": (("summary", "prompts", "calls"),
                  "How much detail is logged: summary = totals + agent tree; prompts = + per-prompt "
                  "breakdown; calls = + tool-call statistics"),
    "log.prompts": (("full", "preview", "off"),
                    "How prompt text is stored: full, preview (first 300 characters) or off (never stored)"),
    "projects.default_mode": (("ask", "track", "tasks"),
                              "New projects: ask how to track them, or track every session, or count only tasks"),
    "sessions.scope": (("enabled", "all"),
                       "Sessions overview: only projects set to track or tasks and sessions added one by one "
                       "(enabled), or every session in the agents' transcripts (all)"),
    "auto_start": (("off", "session"), "Start a task automatically with the first prompt of every session"),
    "todos.reminder": ((True, False), "Show the project's open todos when a session starts"),
    "todos.reminder_limit": (int, "How many todos the reminder lists"),
    "updates.check": ((True, False), "Check once a day whether a new version is out and say so "
                                     "(one request to GitHub; nothing about you is sent)"),
    "dashboard_port": (int, "Port of the local dashboard"),
    "dashboard_idle_minutes": (int, "Stop a dashboard started in the background after this many minutes "
                                    "without an open page (0 = never)"),
}

# (group, label): how the dashboard and `tally settings` show each key, in SCHEMA order
LABELS = {
    "log.depth": ("Logging", "Logging detail"),
    "log.prompts": ("Logging", "Prompt text"),
    "projects.default_mode": ("Projects and tasks", "New projects"),
    "sessions.scope": ("Projects and tasks", "Sessions overview reads"),
    "auto_start": ("Projects and tasks", "Start tasks automatically"),
    "todos.reminder": ("Todos", "Reminder at session start"),
    "todos.reminder_limit": ("Todos", "Todos in the reminder"),
    "updates.check": ("Updates", "Tell me about new versions"),
    "dashboard_port": ("Dashboard", "Port"),
    "dashboard_idle_minutes": ("Dashboard", "Stop when unused (minutes)"),
}

PREVIEW_CHARS = 300


_MISSING = object()


def _lookup(node: Any, key: str) -> Any:
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def get(key: str, cfg: dict = None) -> Any:
    """A setting's value (None if not set). A setting of SCHEMA with an
    invalid value (a hand edit: a word where a number belongs, an unknown
    choice) reads as its default, so a typo never breaks a hook."""
    value = _lookup(cfg if cfg is not None else paths.load_config(), key)
    if value is _MISSING:
        return None
    if key not in SCHEMA:
        return value
    try:
        return coerce(key, value)
    except ValueError:
        default = _lookup(paths.DEFAULT_CONFIG, key)
        return None if default is _MISSING else default


def coerce(key: str, value: Any) -> Any:
    if key not in SCHEMA:
        raise ValueError("unknown setting %r; known: %s" % (key, ", ".join(sorted(SCHEMA))))
    allowed, _ = SCHEMA[key]
    if allowed is int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % key) from None
        if key == "dashboard_port" and not 1 <= v <= 65535:
            raise ValueError("%s must be between 1 and 65535" % key)
        if v < 0:
            raise ValueError("%s must be positive" % key)
        return v
    if allowed == (True, False):
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "on", "yes", "y"):
            return True
        if s in ("0", "false", "off", "no", "n"):
            return False
        raise ValueError("%s must be on or off" % key)
    s = str(value).strip().lower()
    if s not in allowed:
        raise ValueError("%s must be one of: %s" % (key, ", ".join(allowed)))
    return s


def set_value(key: str, value: Any) -> Any:
    """Change one setting in config.json, keeping everything else in it.
    Raises ValueError (and changes nothing) for an invalid value, or when
    config.json exists but cannot be read as a JSON object: starting from
    an empty one would throw away the user's other settings."""
    value = coerce(key, value)
    with paths.config_lock():
        raw = _read_raw()
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        paths.save_config(raw)
    return value


def _read_raw() -> dict:
    """config.json as written (no defaults merged in); {} if there is none."""
    import json
    path = paths.config_path()
    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ValueError("cannot read %s (%s); nothing was changed" % (path, e.strerror or e)) from None
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise ValueError("%s is not valid JSON (%s); fix it or move it away, nothing was changed"
                         % (path, e)) from None
    if not isinstance(raw, dict):
        raise ValueError("%s does not hold a JSON object ({...}); fix it or move it away, nothing was changed"
                         % path)
    return raw


def show(value: Any) -> str:
    """A value as typed by the user: on/off for switches."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def choices(key: str) -> List[str]:
    """The values to choose from, as typed (on/off for switches); [] for numbers."""
    allowed = SCHEMA[key][0]
    if allowed is int:
        return []
    return ["on", "off"] if allowed == (True, False) else list(allowed)


def describe() -> List[Tuple[str, Any, str, Any]]:
    """[(key, current value, description, allowed)] for display."""
    cfg = paths.load_config()
    out = []
    for key, (allowed, desc) in SCHEMA.items():
        out.append((key, get(key, cfg), desc, "number" if allowed is int else
                    ("on/off" if allowed == (True, False) else "|".join(allowed))))
    return out


def depth_at_least(level: str, cfg: dict = None) -> bool:
    order = ("summary", "prompts", "calls")
    cur = get("log.depth", cfg) or "calls"
    return order.index(cur if cur in order else "calls") >= order.index(level)


def store_prompt(text: str, cfg: dict = None):
    mode = get("log.prompts", cfg) or "full"
    if text is None or mode == "off":
        return None
    if mode == "preview" and len(text) > PREVIEW_CHARS:
        return text[: PREVIEW_CHARS - 1] + "…"
    return text
