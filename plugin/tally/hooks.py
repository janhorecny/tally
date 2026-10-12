"""Claude Code hook entry point: `tally hook <EventName>` reads JSON on stdin.

Rules: never break Claude Code. Any error is logged to
~/.claude/tally/errors.log and the hook exits 0 with no output. Only a
recognised /tally:* command produces output (a block decision whose reason is
shown to the user, so the command costs no model tokens).
"""

import json
import os
import sys
import time
import traceback

from . import paths


def _log_error(event: str) -> None:
    try:
        paths.home().mkdir(parents=True, exist_ok=True)
        with open(paths.error_log_path(), "a") as fh:
            fh.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), event, traceback.format_exc()))
    except Exception:
        pass


def _pid():
    try:
        return int(os.environ.get("CLAUDE_PID") or 0) or None
    except ValueError:
        return None


def main(event: str, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        _log_error(event)
        return 0
    try:
        out = handle(event, payload)
        if out is not None:
            stdout.write(json.dumps(out))
            stdout.write("\n")
            stdout.flush()
    except Exception:
        _log_error(event)
    return 0


def handle(event: str, p: dict):
    from . import db, tasks

    sid = p.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    tpath = p.get("transcript_path")
    cwd = p.get("cwd")
    conn = db.connect()
    try:
        now = time.time()
        new_project = None
        with db.transaction(conn):
            db.kv_set(conn, "last_hook", json.dumps({"event": event, "ts": now, "session_id": sid}))
            if event in ("SessionStart", "UserPromptSubmit"):
                from . import projects
                name, root = projects.detect_root(cwd)
                tasks.touch_session(conn, sid, tpath, cwd, _pid(), project=name)
                if event == "SessionStart":
                    row, is_new = projects.register(conn, name, root)
                    new_project = row if row is not None and row["mode"] == "ask" else None

        if event == "SessionStart":
            if p.get("source") == "clear":
                tasks.carry_over_after_clear(conn, sid, _pid(), cwd, tpath,
                                             previous_session=_recently_cleared(conn, cwd, now))
            if p.get("source") in ("startup", "resume", None):
                return _session_message(conn, cwd, new_project)
            return None

        if event == "UserPromptSubmit":
            return _prompt(conn, sid, p, tpath, cwd)

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


def _recently_cleared(conn, cwd, now, window: float = 30.0):
    """Session that SessionEnd(reason=clear) reported for this cwd moments ago."""
    from . import db
    if not cwd:
        return None
    raw = db.kv_get(conn, "cleared:" + cwd)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d.get("session_id") if now - float(d.get("ts", 0)) <= window else None


def _session_message(conn, cwd, new_project):
    """Shown to the user only (systemMessage, 0 tokens): the setup question for
    a project Tally has not been told how to track, and the open todos."""
    from . import projects, settings, todos
    cfg = paths.load_config()
    name = projects.detect(cwd)
    parts = []
    if new_project is not None:
        parts.append(projects.setup_prompt(new_project["name"]))
    if name and projects.mode_of(conn, name) != "off" and settings.get("todos.reminder", cfg):
        text = todos.reminder(conn, name, int(settings.get("todos.reminder_limit", cfg) or 5))
        if text:
            parts.append(text)
    return {"systemMessage": "\n\n".join(parts)} if parts else None


def _prompt(conn, sid, p, tpath, cwd):
    from . import tasks

    prompt = p.get("prompt") or ""
    pid = p.get("prompt_id")
    if prompt.lstrip().startswith("/tally:"):
        from . import commands          # imported only for commands: keeps plain prompts fast
        parsed = commands.parse(prompt)
        if parsed:
            cmd, args = parsed
            res = commands.run(conn, sid, cmd, args, cwd=cwd, transcript_path=tpath, prompt_id=pid)
            if res.block:
                return {"decision": "block", "reason": res.text, "suppressOriginalPrompt": True}
            _open_turn(conn, sid, prompt_id=pid)   # /tally:new, /tally:do: the prompt goes on to the model
            if res.context:
                return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                               "additionalContext": res.context}}
            return None

    task = tasks.active_for_session(conn, sid)
    if task is not None:
        tasks.record_prompt(conn, task, sid, prompt, prompt_id=pid)
        _open_turn(conn, sid, tpath, prompt_id=pid)
        return None

    if paths.load_config().get("auto_start") == "session" and prompt.strip():
        seen = conn.execute("SELECT 1 FROM task_sessions WHERE session_id = ? LIMIT 1", (sid,)).fetchone()
        if seen is None:
            tasks.start(conn, sid, initial_prompt=prompt, cwd=cwd, transcript_path=tpath, created_by="auto",
                        prompt_id=pid)
            _open_turn(conn, sid, prompt_id=pid)
    return None


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
