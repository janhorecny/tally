"""First-run setup and the welcome after installing.

`tally setup` asks a few questions. In a terminal each one is a choice on one
line: arrow keys move, Enter confirms, and the current setting is chosen to
begin with, so running it again changes nothing by accident. Answers can also
be typed (a unique prefix is enough), which is what happens without arrow-key
support. The installer runs it once, on the first install. Without a
terminal, or with --yes, nothing is asked and the defaults stay.

The welcome is a one-time message at the first session start of each agent
Tally was just installed into (a systemMessage: shown to the user, 0 tokens).
"""

import json
import os
import re
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
    ("projects.default_mode", "New projects?", ("ask", "track", "tasks")),
    ("sessions.scope", "Sessions overview reads?", ("enabled", "all")),
    ("auto_start", "Start a task with each session?", None),
    ("todos.reminder", "Show open todos at start?", None),
    ("updates.check", "Tell me about new versions?", None),
)
STATUSLINE_QUESTION = "Add to Claude Code status line?"
# what each question means, shown under it while it is open
EXPLAIN = {
    "log.prompts": "What Tally stores of your prompts. full: the whole text · preview: the first 300 characters "
                   "· off: nothing (prompts are still counted).",
    "log.depth": "How much a report shows. summary: totals and the tree of agents · prompts: also a breakdown "
                 "per prompt · calls: also which tools ran, how often, and their errors.",
    "projects.default_mode": "When an agent opens in a project Tally has not seen. ask: ask you what to do · "
                             "track: count every session there · tasks: count only the tasks you start.",
    "sessions.scope": "What the sessions overview reads. enabled: only projects set to track or tasks, and "
                      "sessions you add · all: every session of every agent on this machine.",
    "auto_start": "yes: the first prompt of every session starts a task named after it · no: tasks start "
                  "only when you say so (/tally:start).",
    "todos.reminder": "yes: when a session starts, list the project's open todos (shown to you only, "
                      "costs no tokens) · no: stay quiet.",
    "updates.check": "yes: once a day ask GitHub for the latest version and tell you when one is out; nothing "
                     "about you is sent · no: never check.",
    "statusline": "yes: Claude Code's status line shows the active task with its tokens, cost and time · "
                  "no: leave the status line as it is.",
}
LABEL_WIDTH = 31                                  # the longest question: the choices line up after it
YES_NO = {"auto_start": ("session", "off"), "todos.reminder": (True, False),     # setting value for yes, no
          "updates.check": (True, False)}


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
    # a symlink (dotfiles) stays one: the file it points to is written, with its mode (often 600)
    real = Path(os.path.realpath(str(path)))
    real.parent.mkdir(parents=True, exist_ok=True)
    if real.exists():
        shutil.copy2(str(real), str(path.with_name("settings.json.bak-tally")))
    tmp = real.with_name(real.name + ".tmp-tally")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    if real.exists():
        shutil.copymode(str(real), str(tmp))
    os.replace(str(tmp), str(real))


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
    """'' keeps the current value; a unique prefix ('pr' = prompts) or the number of the choice is enough."""
    a = re.sub(r"(\x1b|\^\[)\[?[0-9;]*[A-Za-z~]?", "", answer).strip().lower()     # arrow keys typed by habit
    if a.isdigit() and 1 <= int(a) <= len(choices):
        return choices[int(a) - 1]
    if not a:
        return current
    if a in choices:
        return a
    hits = [c for c in choices if c.startswith(a)]
    return hits[0] if len(hits) == 1 else None


def _yes_no(answer: str, current: bool):
    a = re.sub(r"(\x1b|\^\[)\[?[0-9;]*[A-Za-z~]?", "", answer).strip().lower()
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
        say: Optional[Callable] = None, color: bool = False, banner: bool = True, offer_agents: bool = False) -> bool:
    """Ask the setup questions and save the answers. Returns False if the user skipped it."""
    from . import prompt
    picker = ask is None and interactive and prompt.interactive()
    ask, say = ask or input, say or print
    paint = fmt.paint if color else (lambda s: s)
    if not interactive:
        _mark_done("defaults")
        say("✓ %-12s %s" % ("Settings", "defaults (change any time: tally setup)"))
        return True
    if banner:
        from . import __version__
        say(fmt.banner("setup · " + __version__, color, not fmt.can_draw(sys.stdout)))
    say(paint(fmt.head("Setup · ← → choose · Enter confirms · Esc skips setup" if picker
                       else "Setup · Enter keeps the value in [brackets]")))

    def style(text: str, code: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if color else text

    answers = []
    try:
        for key, question, choices in QUESTIONS:
            current = settings.get(key)
            if choices and picker:
                value = prompt.pick(question, list(choices), current, color, LABEL_WIDTH, EXPLAIN[key])
            elif choices:
                say(style("    " + EXPLAIN[key], "2"))
                text = "  %-31s %s [%s] " % (question, style("%-26s" % " / ".join(choices), "2"),
                                              style(str(current), "38;5;208"))
                value = _ask(ask, say, text, lambda a, c=choices, v=current: _choice(a, c, v),
                             "choose one of: " + ", ".join(choices), current)
            else:
                yes_value, no_value = YES_NO[key]
                is_yes = current == yes_value
                if picker:
                    answer = prompt.yes_no(question, is_yes, color, LABEL_WIDTH, EXPLAIN[key])
                else:
                    say(style("    " + EXPLAIN[key], "2"))
                    text = "  %-31s %s " % (question, style("Y/n" if is_yes else "y/N", "38;5;208"))
                    answer = _ask(ask, say, text, lambda a, v=is_yes: _yes_no(a, v), "answer y or n", is_yes)
                value = yes_value if answer else no_value
            if value != current:
                answers.append((key, value))
        install = _agents_question(ask, say, style, picker, color) if offer_agents else []
        add_line = _statusline_question(ask, say, agents or (install or None), style, picker, color)
    except (Skipped, prompt.Cancelled, KeyboardInterrupt):
        say("")
        say(paint(fmt.error("Setup skipped; nothing changed", "tally setup asks again")))
        return False
    for key, value in answers:                 # saved only once every question is answered
        settings.set_value(key, value)
    changed = ["%s = %s" % (key, settings.show(value)) for key, value in answers]
    if install:
        changed += _install(install)
    if add_line and add_statusline():
        changed.append("status line added to %s (restart Claude Code to see it)" % claude_settings_path())
    _mark_done("asked")
    say("")
    say(paint("\n".join([fmt.head("Saved" if changed else "Saved, nothing changed", "✓")]
                        + ["  " + c for c in changed]
                        + [fmt.hints("change any time: tally setup, /tally:settings or the dashboard")])))
    return True


EXPLAIN["agents"] = ("yes: install Tally into it now, so its sessions, tasks and todos are counted · "
                     "no: leave it (tally install --agents <name> does it later).")


def _agents_question(ask: Callable, say: Callable, style, picker: bool, color: bool) -> List[str]:
    """tally setup: the agents found on this machine that Tally is not installed in yet; returns the chosen."""
    from . import installer
    found, inst = installer.detect(), installer.installed()
    missing = [a for a in installer.AGENTS if found[a] and not inst[a]]
    say("  Agents      %s" % " · ".join("%s %s" % (installer.NAMES[a], "installed" if inst[a] else
                                                    ("found" if found[a] else "not found")) for a in installer.AGENTS))
    chosen = []
    for a in missing:
        question = "Install into %s?" % installer.NAMES[a]
        if picker:
            from . import prompt
            yes = prompt.yes_no(question, True, color, LABEL_WIDTH, EXPLAIN["agents"])
        else:
            say(style("    " + EXPLAIN["agents"], "2"))
            yes = _ask(ask, say, "  %-31s %s " % (question, style("Y/n", "38;5;208")),
                       lambda a_: _yes_no(a_, True), "answer y or n", True)
        if yes:
            chosen.append(a)
    return chosen


def _install(agents: List[str]) -> List[str]:
    """Install into the agents chosen in setup; one line each for the summary."""
    from . import installer
    before = installer.installed()
    lines = []
    for a in agents:
        try:
            lines.append("installed into %s: %s" % (installer.NAMES[a], installer.INSTALL[a]()))
        except Exception as exc:
            lines.append("✗ %s: %s" % (installer.NAMES[a], exc))
    after = installer.installed()
    queue_welcome([a for a in agents if after[a] and not before[a]])
    installer._record([a for a, on in after.items() if on])
    return lines


def _wants_statusline(agents: Optional[List[str]]) -> bool:
    if agents is not None:
        return "claude" in agents
    from . import installer
    return installer.installed()["claude"] or installer.detect()["claude"]


def _statusline_question(ask: Callable, say: Callable, agents: Optional[List[str]], style=lambda t, c: t,
                         picker: bool = False, color: bool = False) -> bool:
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
    if picker:
        from . import prompt
        return prompt.yes_no(STATUSLINE_QUESTION, True, color, LABEL_WIDTH, EXPLAIN["statusline"])
    return _ask(ask, say, "  %-31s %s " % (STATUSLINE_QUESTION, style("Y/n", "38;5;208")),
                lambda a: _yes_no(a, True), "answer y or n", True)
