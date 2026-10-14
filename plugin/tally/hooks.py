"""Claude Code hook entry point: `tally hook <EventName>` reads JSON on stdin.

Rules: never break Claude Code. Any error is logged to
~/.claude/tally/errors.log and the hook exits 0; in Claude Code it also says
so (a systemMessage, at most every 30 minutes per error), never blocking the
prompt. Otherwise only a recognised /tally:* command produces output (a block
decision whose reason is shown to the user, so the command costs no model
tokens) and the session start message.
"""

import json
import os
import sys
import time
import traceback

from . import __version__, paths


def _log_error(event: str) -> None:
    try:
        paths.home().mkdir(parents=True, exist_ok=True)
        with open(paths.error_log_path(), "a") as fh:
            fh.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), event, traceback.format_exc()))
    except Exception:
        pass


WARN_EVERY_S = 30 * 60          # the same hook error is reported again after this long


def _warn(event: str, stdout) -> None:
    """A Claude Code hook failed: tell the user (a systemMessage, never a
    decision), so a failure that repeats on every prompt does not stop the
    counting unnoticed. At most once per WARN_EVERY_S per error (type and
    message), kept in a small file and not the database, which may be what
    fails. Says nothing when that file cannot be written. Never raises."""
    try:
        etype, err = sys.exc_info()[:2]
        if etype is None:
            return
        msg = " ".join(str(err).split())
        sig = "%s: %s" % (etype.__name__, msg[:500])
        now = time.time()
        state = paths.home() / "hook_errors.json"
        try:
            with open(str(state)) as fh:
                seen = json.load(fh)
        except (OSError, ValueError):
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        last = seen.get(sig)
        if isinstance(last, (int, float)) and 0 <= now - last < WARN_EVERY_S:
            return
        seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and 0 <= now - v < WARN_EVERY_S}
        seen[sig] = now
        paths.home().mkdir(parents=True, exist_ok=True)
        tmp = state.with_name("%s.%d.tmp" % (state.name, os.getpid()))
        try:
            with open(str(tmp), "w") as fh:
                json.dump(seen, fh)
            os.replace(str(tmp), str(state))
        except Exception:
            try:
                os.remove(str(tmp))
            except OSError:
                pass
            return
        log = str(paths.error_log_path())
        home = os.path.expanduser("~")
        if log.startswith(home + os.sep):
            log = "~" + log[len(home):]
        text = "%s · ⚠ the %s hook failed (%s: %s). Counting may have stopped; tally doctor shows details (%s)." % (
            BRAND, event, etype.__name__, msg if len(msg) <= 100 else msg[:99].rstrip() + "…", log)
        stdout.write(json.dumps({"systemMessage": text}) + "\n")
        stdout.flush()
    except Exception:
        pass


BRAND = "▍Tally"                # fmt.BRAND; not imported here, in case fmt is what fails


def _pid():
    try:
        return int(os.environ.get("CLAUDE_PID") or 0) or None
    except ValueError:
        return None


def main(event: str, stdin=None, stdout=None, agent=None, payload_text=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = payload_text if payload_text is not None else stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if agent and not payload.get("agent"):
            payload["agent"] = agent
    except Exception:               # not a payload: logged only (whose agent it is is not known)
        _log_error(event)
        return 0
    from . import fmt
    fmt.APP = not payload.get("agent") and os.environ.get("CLAUDE_CODE_ENTRYPOINT") in APP_ENTRYPOINTS
    try:
        out = handle(event, payload)
        if out is not None and payload.get("agent") == "codex":
            out = _codex_syntax(out)
        elif out is not None and fmt.APP:
            out = _app_syntax(out)
        if out is not None:
            stdout.write(json.dumps(out))
            stdout.write("\n")
            stdout.flush()
    except Exception:
        _log_error(event)
        if not payload.get("agent"):           # Claude Code; Codex and pi read other output
            _warn(event, stdout)
    finally:
        fmt.APP = False
    return 0


def _codex_syntax(out: dict) -> dict:
    """Codex: commands are typed as tally:<cmd> (its TUI rejects unknown slash
    commands), and its hook output schema has no suppressOriginalPrompt."""
    out.pop("suppressOriginalPrompt", None)
    for k in ("reason", "systemMessage"):
        if isinstance(out.get(k), str):
            out[k] = out[k].replace("/tally:", "tally:")
    return out


APP_ENTRYPOINTS = {"claude-desktop", "claude-desktop-3p"}       # Claude Code in the Claude app (Code tab)


def _app_syntax(out: dict) -> dict:
    from . import fmt
    for k in ("reason", "systemMessage"):
        if isinstance(out.get(k), str):
            out[k] = fmt.for_app(out[k])
    return out


def handle(event: str, p: dict):
    from . import db, tasks

    now = time.time()               # before the first database access, which can wait for a lock
    sid = p.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    tpath = p.get("transcript_path")
    cwd = p.get("cwd")
    if tpath and not os.path.isabs(tpath) and cwd:
        tpath = os.path.normpath(os.path.join(cwd, tpath))
    conn = db.connect()
    try:
        new_project, is_new = None, False
        with db.transaction(conn):
            before = _last_hook(conn)
            db.kv_set(conn, "last_hook", json.dumps({"event": event, "ts": now, "session_id": sid,
                                                     "version": __version__,
                                                     "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT")}))
            if event in ("SessionStart", "UserPromptSubmit"):
                from . import projects
                name, root = projects.detect_root(cwd)
                tasks.touch_session(conn, sid, tpath, cwd, _pid(), project=name, agent=p.get("agent") or "claude")
                if event == "SessionStart":
                    row, is_new = projects.register(conn, name, root)
                    if row is not None and (is_new or row["mode"] == "ask"):
                        new_project = row

        if event == "SessionStart":
            pid = _pid() if not p.get("agent") else None
            if p.get("source") in ("clear", "new"):
                tasks.carry_over_after_clear(conn, sid, pid, cwd, tpath,
                                             previous_session=p.get("previous_session_id")
                                             or _recently_cleared(conn, cwd, now, pid))
            if p.get("source") in ("startup", "resume", None):
                return _session_message(conn, cwd, new_project, p.get("agent") or "claude", sid=sid, is_new=is_new)
            return None

        if event == "UserPromptSubmit":
            out = _prompt(conn, sid, p, tpath, cwd, now)
            if out and out.get("decision") == "block" and (_older(before, event, sid, now)
                                                            or _older(_last_hook(conn), event, sid, now)):
                out = dict(out, reason=out["reason"] + "\n" + OLDER_HOOK)
            return out

        if event in ("Stop", "SubagentStop", "SessionEnd", "PreCompact"):
            if event == "SessionEnd" and p.get("reason") == "clear" and cwd:
                db.kv_set(conn, "cleared:" + cwd, json.dumps({"session_id": sid, "ts": now}))
            from . import projects
            if tasks.active_for_session(conn, sid) is not None or projects.session_tracked(conn, sid):
                from . import ingest
                ingest.ingest_session(conn, sid, tpath)
            if event in ("Stop", "SessionEnd"):
                _close_turn(conn, sid, now)
            return None
        return None
    finally:
        conn.close()


def _recently_cleared(conn, cwd, now, pid=None, window: float = 30.0):
    """Session that SessionEnd(reason=clear) reported for this cwd moments ago,
    if it ran in this process (pid; a session without one is accepted): the
    /clear of another terminal in the same repository is not this one's."""
    from . import db
    if not cwd:
        return None
    raw = db.kv_get(conn, "cleared:" + cwd)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if not isinstance(d, dict) or now - float(d.get("ts", 0)) > window:
            return None
    except (ValueError, TypeError):
        return None
    row = conn.execute("SELECT claude_pid FROM sessions WHERE session_id = ?", (d.get("session_id"),)).fetchone()
    if row is not None and row[0] is not None and row[0] != pid:
        return None
    return d.get("session_id")


def _session_message(conn, cwd, new_project, agent="claude", sid=None, is_new=True):
    """Shown to the user only (systemMessage, 0 tokens): a welcome after
    installing, the setup question for a project Tally has not been told how
    to track (or a notice of its default mode), the project's tasks still
    running in other sessions, the open todos, and a new version (at most
    once a day)."""
    from . import onboarding, projects, settings, todos
    cfg = paths.load_config()
    name = projects.detect(cwd)
    parts = []
    welcome = onboarding.take_welcome(conn, agent)
    if welcome:
        parts.append(welcome)
    if new_project is not None:
        if new_project["mode"] == "ask":
            parts.append(projects.setup_prompt(new_project["name"], is_new=is_new))
        else:
            parts.append(projects.default_notice(new_project["name"], new_project["mode"]))
    text = _elsewhere(conn, sid, name)
    if text:
        parts.append(text)
    if name and projects.mode_of(conn, name) != "off" and settings.get("todos.reminder", cfg):
        try:
            limit = int(settings.get("todos.reminder_limit", cfg) or 5)
        except (TypeError, ValueError):            # a hand-edited config.json
            limit = 5
        text = todos.reminder(conn, name, limit)
        if text:
            parts.append(text)
    from . import updates
    updates.check_in_background()
    text = updates.notice("session", conn)
    if text:
        parts.append(text)
    return {"systemMessage": "\n\n".join(parts)} if parts else None


def _elsewhere(conn, sid, project, limit=3):
    """Active tasks of this project counted only in other sessions (a new
    session would otherwise not know they still run): one line each."""
    if not project:
        return None
    rows = conn.execute(
        "SELECT t.id, t.name FROM tasks t WHERE t.status = 'active' AND t.project = ? "
        "AND EXISTS (SELECT 1 FROM task_sessions s WHERE s.task_id = t.id AND s.detached_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM task_sessions s WHERE s.task_id = t.id AND s.session_id = ? "
        "AND s.detached_at IS NULL) ORDER BY t.started_at DESC LIMIT ?", (project, sid, limit)).fetchall()
    if not rows:
        return None
    from . import fmt
    return "\n".join(fmt.head("Task #%d %s is still active in another session → /tally:resume %d counts it here · "
                              "/tally:end %d ends it" % (r["id"], fmt._cut(r["name"], 60), r["id"], r["id"]), "▶")
                     for r in rows)


def _prompt(conn, sid, p, tpath, cwd, now=None):
    from . import tasks

    prompt = p.get("prompt") or ""
    pid = p.get("prompt_id") or p.get("turn_id")          # Codex names it turn_id
    if prompt.lstrip().startswith(("/tally:", "tally:")):
        from . import commands          # imported only for commands: keeps plain prompts fast
        parsed = commands.parse(prompt)
        if parsed:
            cmd, args = parsed
            res = commands.run_once(conn, sid, cmd, args, lambda: commands.run(
                conn, sid, cmd, args, cwd=cwd, transcript_path=tpath, prompt_id=pid), via="hook", raw=prompt, now=now)
            if res.block:
                return {"decision": "block", "reason": res.text, "suppressOriginalPrompt": True}
            _open_turn(conn, sid, prompt_id=pid)       # /tally:new, /tally:do: the prompt goes on to the model
            if res.context:
                return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                               "additionalContext": res.context}}
            return None

    task = tasks.active_for_session(conn, sid)
    if task is not None:
        if p.get("record_prompt", True):      # pi records prompts from its transcript instead
            tasks.record_prompt(conn, task, sid, prompt, prompt_id=pid)
        _open_turn(conn, sid, tpath, prompt_id=pid)
        return None

    if paths.load_config().get("auto_start") == "session" and prompt.strip():
        from . import projects
        seen = conn.execute("SELECT 1 FROM task_sessions WHERE session_id = ? LIMIT 1", (sid,)).fetchone()
        if seen is None and projects.mode_of(conn, projects.detect(cwd)) != "off":     # off: Tally ignores it
            tasks.start(conn, sid, initial_prompt=prompt, cwd=cwd, transcript_path=tpath, created_by="auto",
                        prompt_id=pid)
            _open_turn(conn, sid, prompt_id=pid)
    return None


OLDER_HOOK = ("  ⚠ Tally ran twice: the agent still has the hooks of an older Tally version (from before "
              "/reload-plugins). Restart Claude Code fully (claude --continue resumes this conversation).")


def _last_hook(conn) -> dict:
    from . import db
    try:
        d = json.loads(db.kv_get(conn, "last_hook") or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _older(hook: dict, event: str, sid: str, now: float) -> bool:
    """The same event of the same session, a moment apart, from an older version
    (before 0.11.2 the record had no version): its hooks are registered too."""
    return (hook.get("session_id") == sid and hook.get("event") == event and abs(float(hook.get("ts", 0)) - now) < 2
            and hook.get("version") != __version__)


# Turn timing from hooks (prompt submitted -> Stop). Interactive sessions also
# write turn_duration records to the transcript; the report takes the union of
# both, so overlapping measurements are not double counted.

def _open_turn(conn, sid, tpath=None, prompt_id=None) -> None:
    """Mark a turn as started now.

    A turn still open here never got its Stop (the user interrupted it, or
    the prompt arrived while Claude was working). It is closed at its last
    recorded API response instead of stretching over the idle time since.
    """
    from . import db
    key = "open_turn:" + sid
    now = time.time()
    start = db.kv_get(conn, key)
    if start is not None:
        t0, old_pid = _parse_open(start, now)
        if tpath is not None:
            try:
                from . import ingest
                ingest.ingest_session(conn, sid, tpath)
            except Exception:
                pass
        last = conn.execute("SELECT MAX(ts) FROM usage WHERE session_id = ? AND ts >= ? AND ts <= ?",
                            (sid, t0, now)).fetchone()[0]
        _record_turn(conn, sid, t0, last if last else t0, old_pid)
    db.kv_set(conn, key, json.dumps({"t0": now, "prompt_id": prompt_id}))


def _parse_open(raw: str, now: float):
    """open_turn value: {"t0", "prompt_id"} (older versions stored a bare float)."""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return float(d.get("t0", now)), d.get("prompt_id")
        return float(d), None
    except (ValueError, TypeError):
        return now, None


def _close_turn(conn, sid, now=None) -> None:
    from . import db
    key = "open_turn:" + sid
    start = db.kv_get(conn, key)
    if start is None:
        return
    now = now or time.time()
    t0, pid = _parse_open(start, now)
    _record_turn(conn, sid, t0, now, pid)


def _record_turn(conn, sid, t0, t1, prompt_id=None) -> None:
    from . import db
    with db.transaction(conn):
        if t1 > t0:
            conn.execute(
                "INSERT OR REPLACE INTO turns(uuid, session_id, ts_end, duration_ms, prompt_id) VALUES(?,?,?,?,?)",
                ("hook:%s:%.3f" % (sid, t0), sid, t1, int((t1 - t0) * 1000), prompt_id),
            )
        conn.execute("DELETE FROM kv WHERE key = ?", ("open_turn:" + sid,))
