"""First-run setup and the welcome after installing.

`tally setup` asks a few questions; Enter keeps the value in brackets, which
is the current setting, so running it again changes nothing by accident. The
installer runs it once, on the first install. Without a terminal, or with
--yes, nothing is asked and the defaults stay.

The welcome is a one-time message at the first session start of each agent
Tally was just installed into (a systemMessage: shown to the user, 0 tokens).
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import db, fmt, paths, settings

YES = ("y", "yes", "a", "ano", "áno", "j", "ja")
NO = ("n", "no", "nie", "nein")
TRIES = 3

# setting, question, choices (None: a yes/no question)
QUESTIONS = (
    ("log.prompts", "Store prompt text?", ("full", "preview", "off")),
    ("log.depth", "Logging detail?", ("summary", "prompts", "calls")),
    ("projects.default_mode", "New projects:", ("ask", "track", "tasks")),
    ("auto_start", "Start a task automatically with each session?", None),
    ("todos.reminder", "Show open todos at session start?", None),
)
YES_NO = {"auto_start": ("session", "off"), "todos.reminder": (True, False)}   # setting value for yes, no


class Skipped(Exception):
    """The user closed the input (Ctrl-D): keep everything as it is."""


# ------------------------------------------------------------------ state

def done(conn=None) -> bool:
    conn = conn or db.connect()
    return db.kv_get(conn, "setup") is not None


def _mark_done(how: str) -> None:
    from . import __version__
    conn = db.connect()
    db.kv_set(conn, "setup", json.dumps({"ts": time.time(), "how": how, "version": __version__}))


def queue_welcome(agents: List[str]) -> None:
    conn = db.connect()
    for a in agents:
        db.kv_set(conn, "welcome:" + a, "1")


def take_welcome(conn, agent: str) -> Optional[str]:
    """The welcome text once per agent; the DELETE makes concurrent session starts show it only once."""
    cur = conn.execute("DELETE FROM kv WHERE key = ?", ("welcome:" + agent,))
    return welcome_text() if cur.rowcount else None


def welcome_text() -> str:
    return "\n".join([
        fmt.head("Installed. Commands are answered locally and cost no tokens.", "👋"),
        "  /tally:start <name>   measure a task: tokens, cost, time, subagents, workflows",
        "  /tally:sessions       what every session of every agent cost so far",
        "  /tally:todo <text>    add a todo; /tally:take <id> measures the work on it",
        "  /tally:dashboard      the local dashboard",
        fmt.hints("/tally:help all commands"),
    ])


# ------------------------------------------------------------------ Claude Code status line

def claude_settings_path() -> Path:
    return paths.claude_dir() / "settings.json"


def statusline_command() -> str:
    from .installer import BIN
    return 'python3 "%s" statusline' % BIN


def statusline_state():
    """('absent' | 'ours' | 'other' | 'invalid', the current command or None)."""
    path = claude_settings_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "invalid", None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return "invalid", None
    if not isinstance(data, dict):
        return "invalid", None
    line = data.get("statusLine")
    if not line:
        return "absent", None
    cmd = str(line.get("command", "")) if isinstance(line, dict) else str(line)
    if "tally" in cmd and "statusline" in cmd:
        return "ours", cmd
    return "other", cmd


def _write_settings(data: dict) -> None:
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name("settings.json.bak-tally"))
    tmp = path.with_name("settings.json.tmp-tally")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(str(tmp), str(path))


def add_statusline() -> bool:
    """Only when Claude Code has no status line yet; returns whether it was added."""
    state, _ = statusline_state()
    if state != "absent":
        return False
    path = claude_settings_path()
    data = json.loads(path.read_text() or "{}") if path.exists() else {}
    data["statusLine"] = {"type": "command", "command": statusline_command(), "refreshInterval": 10}
    _write_settings(data)
    return True


def remove_statusline() -> bool:
    """Remove the status line only if it is exactly the one setup added."""
    state, cmd = statusline_state()
    if state != "ours" or cmd != statusline_command():
        return False
    data = json.loads(claude_settings_path().read_text())
    del data["statusLine"]
    _write_settings(data)
    return True


# ------------------------------------------------------------------ questions

def _choice(answer: str, choices, current):
    """'' keeps the current value; a unique prefix is enough ('pr' = prompts)."""
    a = answer.strip().lower()
    if not a:
        return current
    if a in choices:
        return a
    hits = [c for c in choices if c.startswith(a)]
    return hits[0] if len(hits) == 1 else None


def _yes_no(answer: str, current: bool):
    a = answer.strip().lower()
    if not a:
        return current
    if a in YES:
        return True
    if a in NO:
        return False
    return None


def _ask(ask: Callable, say: Callable, prompt: str, parse, retry: str, current):
    for _ in range(TRIES):
        try:
            value = parse(ask(prompt))
        except EOFError:
            raise Skipped() from None
        if value is not None:
            return value
        say("    " + retry)
    say("    keeping %s" % settings.show(current))
    return current


def run(interactive: bool, agents: Optional[List[str]] = None, ask: Optional[Callable] = None,
        say: Optional[Callable] = None, color: bool = False, banner: bool = True) -> bool:
    """Ask the setup questions and save the answers. Returns False if the user skipped it."""
    ask, say = ask or input, say or print
    paint = fmt.paint if color else (lambda s: s)
    if not interactive:
        _mark_done("defaults")
        say("✓ %-12s %s" % ("Settings", "defaults (change any time: tally setup)"))
        return True
    if banner:
        from . import __version__
        say(fmt.logo(["Tally " + __version__, "Per-task token & time tracking", "for Claude Code, Codex and pi"],
                     color=color, ascii_only=not fmt.can_draw(sys.stdout)))
        say("")
    say(paint(fmt.head("Setup · Enter keeps the value in [brackets]")))
    answers = []
    try:
        for key, question, choices in QUESTIONS:
            current = settings.get(key)
            if choices:
                prompt = "  %-27s %-26s [%s] " % (question, " / ".join(choices), current)
                value = _ask(ask, say, prompt, lambda a, c=choices, v=current: _choice(a, c, v),
                             "choose one of: " + ", ".join(choices), current)
            else:
                yes_value, no_value = YES_NO[key]
                is_yes = current == yes_value
                prompt = "  %s %s " % (question, "Y/n" if is_yes else "y/N")
                answer = _ask(ask, say, prompt, lambda a, v=is_yes: _yes_no(a, v), "answer y or n", is_yes)
                value = yes_value if answer else no_value
            if value != current:
                answers.append((key, value))
        add_line = _statusline_question(ask, say, agents)
    except Skipped:
        say("")
        say(paint(fmt.error("Setup skipped; nothing changed", "tally setup asks again")))
        return False
    for key, value in answers:                 # saved only once every question is answered
        settings.set_value(key, value)
    changed = ["%s = %s" % (key, settings.show(value)) for key, value in answers]
    if add_line and add_statusline():
        changed.append("status line added to %s (restart Claude Code to see it)" % claude_settings_path())
    _mark_done("asked")
    say("")
    say(paint("\n".join([fmt.head("Saved" if changed else "Saved, nothing changed", "✓")]
                        + ["  " + c for c in changed]
                        + [fmt.hints("change any time: tally setup, /tally:settings or the dashboard")])))
    return True


def _wants_statusline(agents: Optional[List[str]]) -> bool:
    if agents is not None:
        return "claude" in agents
    from . import installer
    return installer.installed()["claude"] or installer.detect()["claude"]


def _statusline_question(ask: Callable, say: Callable, agents: Optional[List[str]]) -> bool:
    """Whether to add Tally to Claude Code's status line (asked only when it has none)."""
    if not _wants_statusline(agents):
        return False
    state, cmd = statusline_state()
    if state == "ours":
        say("  Claude Code's status line already shows Tally.")
        return False
    if state == "other":
        say("  Claude Code already has a status line; it stays as it is. To add Tally to it:")
        say("    ccstatusline: a Custom Command widget running  %s" % statusline_command())
        say("    your own script: print the output of           %s" % statusline_command())
        return False
    if state == "invalid":
        say("  %s is not valid JSON, so it is left alone. To show Tally, add:" % claude_settings_path())
        say('    "statusLine": {"type": "command", "command": %s}' % json.dumps(statusline_command()))
        return False
    return _ask(ask, say, "  Add Tally to Claude Code's status line? Y/n ", lambda a: _yes_no(a, True),
                "answer y or n", True)
