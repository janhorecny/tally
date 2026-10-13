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
    "todos.reminder": ((True, False), "Show the project's open todos when a session starts"),
    "todos.reminder_limit": (int, "How many todos the reminder lists"),
    "auto_start": (("off", "session"), "Start a task automatically with the first prompt of every session"),
    "projects.default_mode": (("ask", "track", "tasks"),
                              "New projects: ask how to track them, or track every session, or count only tasks"),
    "dashboard_port": (int, "Port of the local dashboard"),
    "dashboard_idle_minutes": (int, "Stop a dashboard started in the background after this many minutes "
                                    "without an open page (0 = never)"),
}

PREVIEW_CHARS = 300


def get(key: str, cfg: dict = None) -> Any:
    node: Any = cfg if cfg is not None else paths.load_config()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def coerce(key: str, value: Any) -> Any:
    if key not in SCHEMA:
        raise ValueError("unknown setting %r; known: %s" % (key, ", ".join(sorted(SCHEMA))))
    allowed, _ = SCHEMA[key]
    if allowed is int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % key) from None
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
    value = coerce(key, value)
    try:
        import json
        with open(paths.config_path()) as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    node = raw
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    paths.save_config(raw)
    return value


def show(value: Any) -> str:
    """A value as typed by the user: on/off for switches."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


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
    cur = get("log.depth", cfg) or "prompts"
    return order.index(cur if cur in order else "prompts") >= order.index(level)


def store_prompt(text: str, cfg: dict = None):
    mode = get("log.prompts", cfg) or "full"
    if text is None or mode == "off":
        return None
    if mode == "preview" and len(text) > PREVIEW_CHARS:
        return text[: PREVIEW_CHARS - 1] + "…"
    return text
