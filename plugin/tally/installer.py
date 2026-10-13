"""Install / uninstall Tally into coding agents: Claude Code, Codex, pi.

  tally install [--agents claude,codex,pi | --all] [--yes]
  tally uninstall [--agents ...] [--purge]

Claude Code  plugin tally@tally from this repository (local marketplace)
Codex        hooks in $CODEX_HOME/hooks.json (merged, other hooks kept) and the
             tally-todos skill in ~/.agents/skills; trust the hooks once via /hooks
pi           extension $PI_CODING_AGENT_DIR/extensions/tally.ts and the
             tally-todos skill in $PI_CODING_AGENT_DIR/skills
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

PLUGIN = Path(__file__).resolve().parent.parent          # .../plugin
REPO = PLUGIN.parent
BIN = PLUGIN / "bin" / "tally"
AGENTS = ("claude", "codex", "pi")
NAMES = {"claude": "Claude Code", "codex": "Codex", "pi": "pi"}
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "PreCompact", "SessionEnd")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def pi_home() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or (Path.home() / ".pi" / "agent")).expanduser()


def agents_skills_home() -> Path:
    return Path.home() / ".agents" / "skills"


def detect() -> Dict[str, bool]:
    return {
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None or (codex_home() / "config.toml").exists(),
        "pi": shutil.which("pi") is not None or pi_home().is_dir(),
    }


def installed() -> Dict[str, bool]:
    out = {"claude": False, "codex": False, "pi": False}
    try:
        data = json.loads((Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
                           / "plugins" / "installed_plugins.json").read_text())
        out["claude"] = "tally@tally" in json.dumps(data)
    except (OSError, ValueError):
        pass
    try:
        data = json.loads((codex_home() / "hooks.json").read_text() or "{}")
        out["codex"] = any(_is_tally(e) for entries in data.get("hooks", {}).values() for e in entries)
    except (OSError, ValueError, AttributeError):
        pass
    out["pi"] = (pi_home() / "extensions" / "tally.ts").exists()
    return out


def _say(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _hook_cmd(event: str, agent: str) -> str:
    return '"%s" "%s" hook %s --agent %s' % (sys.executable if os.path.isabs(sys.executable) else "python3",
                                              BIN, event, agent)


def _skill_text() -> str:
    """The Claude plugin skill, made agent-neutral (absolute CLI path)."""
    src = (PLUGIN / "skills" / "todos" / "SKILL.md").read_text()
    head, body = src.split("---", 2)[1:]
    lines = [l for l in head.strip().splitlines() if not l.startswith(("allowed-tools", "name:"))]
    body = body.replace('python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally"', '"%s"' % BIN)
    body = body.replace("`/tally:*` slash commands are for the user; you use the CLI.",
                        "The user's own commands (`/tally ...`, `tally:<cmd>`) are for them; you use the CLI.")
    return "---\nname: tally-todos\n%s\n---%s" % ("\n".join(lines), body)


# ------------------------------------------------------------------ Claude Code

def install_claude() -> str:
    if not shutil.which("claude"):
        raise RuntimeError("the claude CLI is not on PATH")
    # migrate an install made under the previous name
    if "task@task-counter" in _run(["claude", "plugin", "list"]).stdout:
        _run(["claude", "plugin", "uninstall", "task@task-counter"])
    if "task-counter" in _run(["claude", "plugin", "marketplace", "list"]).stdout:
        _run(["claude", "plugin", "marketplace", "remove", "task-counter"])
    for target in (str(REPO), str(PLUGIN)):
        r = _run(["claude", "plugin", "validate", target])
        if r.returncode != 0:
            raise RuntimeError("plugin validation failed: %s" % (r.stdout + r.stderr).strip()[-400:])
    if " tally" in _run(["claude", "plugin", "marketplace", "list"]).stdout:
        _run(["claude", "plugin", "marketplace", "update", "tally"])
    else:
        r = _run(["claude", "plugin", "marketplace", "add", str(REPO)])
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[-400:])
    if "tally@tally" in _run(["claude", "plugin", "list"]).stdout:
        _run(["claude", "plugin", "update", "tally@tally"])
    else:
        r = _run(["claude", "plugin", "install", "tally@tally", "--scope", "user"])
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[-400:])
    return "plugin tally@tally (restart Claude Code or /reload-plugins); commands /tally:start …"


def uninstall_claude() -> str:
    from . import onboarding
    line = "; status line removed" if onboarding.remove_statusline() else ""
    if not shutil.which("claude"):
        return "claude CLI not found" + line
    _run(["claude", "plugin", "uninstall", "tally@tally"])
    _run(["claude", "plugin", "marketplace", "remove", "tally"])
    return "plugin removed" + line


# ------------------------------------------------------------------ Codex

def _is_tally(entry: dict) -> bool:
    return isinstance(entry, dict) and any(
        "bin/tally" in str(h.get("command", "")) and " hook " in str(h.get("command", ""))
        for h in entry.get("hooks", []) if isinstance(h, dict))


def install_codex() -> str:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "hooks.json"
    data = {"hooks": {}}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except ValueError:
            raise RuntimeError("%s is not valid JSON; fix or move it first" % path) from None
        shutil.copy2(path, path.with_name("hooks.json.bak-tally"))
    hooks = data.setdefault("hooks", {})
    for event in CODEX_EVENTS:
        kept = [e for e in hooks.get(event, []) if not _is_tally(e)]
        kept.append({"hooks": [{"type": "command", "command": _hook_cmd(event, "codex"),
                                "timeout": 3 if event == "SessionEnd" else 60,
                                "statusMessage": "Tally"}]})
        hooks[event] = kept
    path.write_text(json.dumps(data, indent=2) + "\n")
    skill = agents_skills_home() / "tally-todos" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(_skill_text())
    return ("hooks in %s, skill in %s. Open Codex and approve the new hooks once in /hooks; "
            "then type commands as prompts: tally:start …, tally:status, tally:todo …" % (path, skill.parent))


def uninstall_codex() -> str:
    path = codex_home() / "hooks.json"
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
            hooks = data.get("hooks", {})
            for event in list(hooks):
                hooks[event] = [e for e in hooks[event] if not _is_tally(e)]
                if not hooks[event]:
                    del hooks[event]
            path.write_text(json.dumps(data, indent=2) + "\n")
        except ValueError:
            pass
    shutil.rmtree(agents_skills_home() / "tally-todos", ignore_errors=True)
    return "hooks and skill removed"


# ------------------------------------------------------------------ pi

def install_pi() -> str:
    home = pi_home()
    ext = home / "extensions" / "tally.ts"
    ext.parent.mkdir(parents=True, exist_ok=True)
    src = (PLUGIN / "agents" / "pi" / "tally.ts").read_text()
    ext.write_text(src.replace("__TALLY_BIN__", str(BIN)))
    skill = home / "skills" / "tally-todos" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(_skill_text())
    return "extension %s, skill %s (restart pi or /reload); commands /tally start …, /tally status" % (
        ext, skill.parent)


def uninstall_pi() -> str:
    home = pi_home()
    try:
        (home / "extensions" / "tally.ts").unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(home / "skills" / "tally-todos", ignore_errors=True)
    return "extension and skill removed"


# ------------------------------------------------------------------ CLI glue

INSTALL = {"claude": install_claude, "codex": install_codex, "pi": install_pi}
UNINSTALL = {"claude": uninstall_claude, "codex": uninstall_codex, "pi": uninstall_pi}


def choose(requested: Optional[List[str]], assume_yes: bool) -> List[str]:
    found = detect()
    if requested:
        unknown = [a for a in requested if a not in AGENTS]
        if unknown:
            raise SystemExit("unknown agent(s): %s (choose from %s)" % (", ".join(unknown), ", ".join(AGENTS)))
        return requested
    detected = [a for a in AGENTS if found[a]]
    if not detected:
        raise SystemExit("No supported agent found (Claude Code, Codex, pi). Use --agents to force one.")
    if assume_yes or not sys.stdin.isatty():
        return detected
    _say("Which agents should Tally be installed into?")
    picked = []
    for a in AGENTS:
        if not found[a]:
            _say("  %-12s not found" % NAMES[a])
            continue
        ans = input("  %-12s install? [Y/n] " % NAMES[a]).strip().lower()
        if ans in ("", "y", "yes", "a", "ano", "j", "ja"):
            picked.append(a)
    return picked


def install(requested: Optional[List[str]] = None, assume_yes: bool = False) -> int:
    """Install into the chosen agents; on the first install also ask the setup questions."""
    from . import __version__, fmt, onboarding
    interactive = not assume_yes and sys.stdin.isatty()
    color = fmt.use_color(sys.stdout)
    if sys.stdout.isatty():
        _say(fmt.banner("install · " + __version__, color, not fmt.can_draw(sys.stdout)))
    agents = choose(requested, assume_yes)
    if not agents:
        _say("Nothing selected.")
        return 0
    before = installed()
    rc = 0
    for a in agents:
        try:
            _say("✓ %-12s %s" % (NAMES[a], INSTALL[a]()))
        except Exception as e:              # keep going with the other agents
            rc = 1
            _say("✗ %-12s %s" % (NAMES[a], e))
    _record(agents)
    after = installed()
    onboarding.queue_welcome([a for a in agents if after[a] and not before[a]])
    if not onboarding.done():
        _say("")
        onboarding.run(interactive, agents=agents, color=color, banner=False)
    return rc


def uninstall(requested: Optional[List[str]] = None) -> int:
    agents = requested or [a for a, on in installed().items() if on] or list(AGENTS)
    for a in agents:
        try:
            _say("✓ %-12s %s" % (NAMES[a], UNINSTALL[a]()))
        except Exception as e:
            _say("✗ %-12s %s" % (NAMES[a], e))
    return 0


def _record(agents: List[str]) -> None:
    from . import db
    try:
        conn = db.connect()
        db.kv_set(conn, "installed", json.dumps({"agents": agents, "ts": time.time(), "repo": str(REPO)}))
    except Exception:
        pass
