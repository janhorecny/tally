"""Task lifecycle: start, end, attach, rename, delete."""

import re
import time
from typing import List, Optional, Tuple

from . import db, ingest, paths

UNTITLED = "(untitled)"


def derive_name(prompt: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", (prompt or "").strip())
    if not text:
        return UNTITLED
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def get(conn, task_id: int):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def active_for_session(conn, session_id: str):
    if not session_id:
        return None
    return conn.execute(
        "SELECT t.* FROM tasks t JOIN task_sessions s ON s.task_id = t.id "
        "WHERE s.session_id = ? AND s.detached_at IS NULL AND t.status = 'active' "
        "ORDER BY t.started_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def active_tasks(conn) -> list:
    return conn.execute("SELECT * FROM tasks WHERE status = 'active' ORDER BY started_at DESC").fetchall()


def session_ids(conn, task_id: int) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT session_id FROM task_sessions WHERE task_id = ? GROUP BY session_id ORDER BY MIN(attached_at)",
        (task_id,))]


def task_windows(conn, task) -> List[Tuple[str, float, float]]:
    """(session_id, from, to) intervals during which the task was counting.

    An active task's window is open-ended so usage stamped a moment "in the
    future" (clock skew between processes) is not dropped.
    """
    t0 = task["started_at"]
    t1 = task["ended_at"] or float("inf")
    out = []
    last = {}           # session -> index in out of its latest interval
    for r in conn.execute(
        "SELECT session_id, attached_at, detached_at FROM task_sessions WHERE task_id = ? ORDER BY attached_at",
        (task["id"],),
    ):
        a = max(t0, r["attached_at"])
        b = min(t1, r["detached_at"] or t1)
        if b <= a:
            continue
        i = last.get(r["session_id"])
        if i is not None and a <= out[i][2]:
            # overlaps the session's previous interval (two attaches raced): one window, or usage counts twice
            out[i] = (r["session_id"], out[i][1], max(out[i][2], b))
        else:
            last[r["session_id"]] = len(out)
            out.append((r["session_id"], a, b))
    return out


def touch_session(conn, session_id: str, transcript_path: Optional[str] = None,
                  cwd: Optional[str] = None, claude_pid: Optional[int] = None,
                  project: Optional[str] = None, agent: Optional[str] = None) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(session_id, transcript_path, cwd, claude_pid, first_seen, last_seen, project, agent) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
        "agent = COALESCE(excluded.agent, sessions.agent), "
        "transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path), "
        "cwd = COALESCE(excluded.cwd, sessions.cwd), "
        "claude_pid = COALESCE(excluded.claude_pid, sessions.claude_pid), "
        "project = COALESCE(excluded.project, sessions.project), "
        "last_seen = excluded.last_seen",
        (session_id, transcript_path, cwd, claude_pid, now, now, project, agent),
    )


TWIN_S = 5.0     # the same task started again in the same session this soon: one start run twice (two hooks)
AUTO_ENDED = "auto-ended: new task started"


def start(conn, session_id: str, name: Optional[str] = None, initial_prompt: Optional[str] = None,
          cwd: Optional[str] = None, transcript_path: Optional[str] = None,
          created_by: str = "command", now: Optional[float] = None, project: Optional[str] = None,
          todo_id: Optional[int] = None, prompt_id: Optional[str] = None):
    """Start a task in a session. Ends a task already running in that session.

    Returns (new_task_row, ended_task_row_or_None). Ending the running task and
    starting the new one is one write transaction, so two starts at once (two
    hook registrations, the hook and the command fallback) never leave two
    active tasks. The same task started again a moment ago is that start run
    twice: it is returned as it is, with the task that start ended.

    created_by="auto" (auto_start: session) starts nothing and returns None
    when the session already has or had a task.
    """
    from . import projects, settings
    now = now or time.time()
    if created_by == "auto" and _had_task(conn, session_id):
        return None
    cfg = paths.load_config()
    stored = settings.store_prompt(initial_prompt, cfg)
    name = (name or "").strip() or (derive_name(stored) if stored else UNTITLED)
    project = project or projects.detect(cwd)
    root = projects.detect_root(cwd)[1] if project and cwd else None
    running = _active_in_session(conn, session_id)
    if _twin(conn, running, name, created_by, todo_id, prompt_id, now) is None:
        _ingest_before_end(conn, running)
    with db.transaction(conn):
        if created_by == "auto" and _had_task(conn, session_id):
            return None                     # the twin hook started it a moment ago
        running = _active_in_session(conn, session_id)
        twin = _twin(conn, running, name, created_by, todo_id, prompt_id, now)
        if twin is None:
            for r in running:
                _close(conn, r, AUTO_ENDED, now)
            if project:
                projects.register(conn, project, root)
            cur = conn.execute(
                "INSERT INTO tasks(name, initial_prompt, status, started_at, cwd, origin_session, created_by, project, "
                "todo_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (name, stored, "active", now, cwd, session_id, created_by, project, todo_id),
            )
            task_id = cur.lastrowid
            conn.execute(
                "INSERT INTO task_sessions(task_id, session_id, attached_at, reason) VALUES(?,?,?,?)",
                (task_id, session_id, now, "start"),
            )
            if initial_prompt is not None:
                conn.execute(
                    "INSERT INTO prompts(task_id, session_id, ts, prompt, prompt_id) VALUES(?,?,?,?,?)",
                    (task_id, session_id, now, stored, prompt_id),
                )
    if twin is not None:
        ended = conn.execute(
            "SELECT t.* FROM tasks t JOIN task_sessions s ON s.task_id = t.id WHERE s.session_id = ? "
            "AND t.ended_at = ? AND t.end_note = ? ORDER BY t.started_at DESC LIMIT 1",
            (session_id, twin["started_at"], AUTO_ENDED)).fetchone()
        return get(conn, twin["id"]), ended
    ended = _freeze_all(conn, running)
    _skip_history(conn, session_id, transcript_path, exclude_task=task_id)
    return get(conn, task_id), ended


def _had_task(conn, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM task_sessions WHERE session_id = ? LIMIT 1", (session_id,)).fetchone() is not None


def _active_in_session(conn, session_id: str) -> list:
    """Active tasks counting the session, the latest first (normally at most one)."""
    return conn.execute(
        "SELECT DISTINCT t.* FROM tasks t JOIN task_sessions s ON s.task_id = t.id "
        "WHERE s.session_id = ? AND s.detached_at IS NULL AND t.status = 'active' ORDER BY t.started_at DESC",
        (session_id,),
    ).fetchall()


def _twin(conn, running, name: str, created_by: str, todo_id: Optional[int], prompt_id: Optional[str], now: float):
    """The running task this start would create, started a moment ago (a start run twice)."""
    for r in running:
        if (r["name"] == name and r["created_by"] == created_by and r["todo_id"] == todo_id
                and abs(now - r["started_at"]) < TWIN_S):
            if prompt_id is not None and conn.execute(
                    "SELECT 1 FROM prompts WHERE task_id = ? AND prompt_id IS NOT NULL AND prompt_id != ? LIMIT 1",
                    (r["id"], prompt_id)).fetchone():
                continue                    # started by another prompt: not a twin
            return r
    return None


def _ingest_before_end(conn, rows) -> None:
    """Read what is outstanding for tasks about to end (outside the write
    transaction that ends them: reading a transcript can take a while)."""
    for r in rows:
        try:
            ingest.ingest_sessions(conn, session_ids(conn, r["id"]))
        except Exception:
            pass


def _close(conn, task, note: Optional[str], now: float) -> None:
    """End a task (inside a write transaction)."""
    conn.execute(
        "UPDATE tasks SET status = 'ended', ended_at = ?, end_note = COALESCE(?, end_note) WHERE id = ?",
        (now, note, task["id"]),
    )
    conn.execute(
        "UPDATE task_sessions SET detached_at = ? WHERE task_id = ? AND detached_at IS NULL",
        (now, task["id"]),
    )
    if task["todo_id"]:
        _release_todo(conn, task, now)


def _release_todo(conn, task, now: float) -> None:
    """Nobody works on the task's todo any more: back to todo (/tally:done marks it done instead)."""
    conn.execute("UPDATE todos SET status = 'todo', updated_at = ? WHERE id = ? AND status = 'doing' "
                 "AND NOT EXISTS (SELECT 1 FROM tasks WHERE todo_id = ? AND status = 'active' AND id != ?)",
                 (now, task["todo_id"], task["todo_id"], task["id"]))


def _freeze(conn, task_id: int):
    """Freeze the final report of an ended task; returns the task."""
    from . import report  # local import: report imports tasks
    task = get(conn, task_id)
    report.save_snapshot(conn, task_id, report.build(conn, task))
    return task


def _freeze_all(conn, rows):
    """Freeze the tasks just ended; returns the latest of them (or None)."""
    ended = None
    for r in rows:
        task = _freeze(conn, r["id"])
        ended = ended or task
    return ended


def _skip_history(conn, session_id: str, transcript_path: Optional[str], exclude_task: int) -> None:
    """Register transcript files at their current size so history before a
    task is never parsed. Only safe when no other active task is counting
    this session and its project is not tracked; otherwise read normally so
    nothing of theirs is lost.
    """
    from . import projects
    other = conn.execute(
        "SELECT 1 FROM task_sessions s JOIN tasks t ON t.id = s.task_id WHERE s.session_id = ? "
        "AND s.detached_at IS NULL AND t.status = 'active' AND t.id != ? LIMIT 1",
        (session_id, exclude_task),
    ).fetchone() or projects.session_tracked(conn, session_id)
    # Codex token counts are cumulative: its history may only be skipped once ingest keeps the
    # last total of the skipped bytes (files.state); until then it is read, as before.
    skip = not other and ingest.session_agent(conn, session_id) != "codex"
    try:
        ingest.ingest_session(conn, session_id, transcript_path, skip_existing=skip)
    except Exception:
        pass


def _counting(conn, task_id: int, session_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_sessions WHERE task_id = ? AND session_id = ? AND detached_at IS NULL",
        (task_id, session_id),
    ).fetchone() is not None


def attach(conn, task_id: int, session_id: str, transcript_path: Optional[str] = None,
           reason: str = "attach", now: Optional[float] = None):
    """Make a session count towards an existing active task.

    The "already counting here?" check, ending the session's other task and
    the new interval are one write transaction: two attaches at once (two
    hooks, /clear seen twice) open one interval, not two.
    """
    now = now or time.time()
    task = get(conn, task_id)
    if task is None:
        raise ValueError("Task #%s not found" % task_id)
    if task["status"] != "active":
        raise ValueError("task #%s has already ended" % task_id)
    if _counting(conn, task_id, session_id):
        return task, None          # already counting here; nothing to skip or re-attach
    _ingest_before_end(conn, [r for r in _active_in_session(conn, session_id) if r["id"] != task_id])
    note = "auto-ended: session attached to #%d" % task_id
    with db.transaction(conn):
        task = get(conn, task_id)
        if task is None:
            raise ValueError("Task #%s not found" % task_id)
        if task["status"] != "active":
            raise ValueError("task #%s has already ended" % task_id)
        if _counting(conn, task_id, session_id):
            return task, None      # the twin attached it a moment ago
        prev = [r for r in _active_in_session(conn, session_id) if r["id"] != task_id]
        for r in prev:
            _close(conn, r, note, now)
        conn.execute(
            "INSERT INTO task_sessions(task_id, session_id, attached_at, reason) VALUES(?,?,?,?)",
            (task_id, session_id, now, reason),
        )
    ended = _freeze_all(conn, prev)
    _skip_history(conn, session_id, transcript_path, exclude_task=task_id)
    return get(conn, task_id), ended


def record_prompt(conn, task, session_id: str, prompt: str, now: Optional[float] = None,
                  prompt_id: Optional[str] = None) -> None:
    """Store a prompt of the task (text per the log.prompts setting; the row
    is kept even when the text is not, so prompts are always counted). A
    prompt already stored (the same prompt id: two hook registrations) is
    stored once."""
    from . import settings
    now = now or time.time()
    stored = settings.store_prompt(prompt)
    with db.transaction(conn):
        if prompt_id is not None and conn.execute(
                "SELECT 1 FROM prompts WHERE task_id = ? AND prompt_id = ? LIMIT 1", (task["id"], prompt_id)).fetchone():
            return
        conn.execute(
            "INSERT INTO prompts(task_id, session_id, ts, prompt, prompt_id) VALUES(?,?,?,?,?)",
            (task["id"], session_id, now, stored, prompt_id),
        )
        if not task["initial_prompt"] and stored:
            conn.execute("UPDATE tasks SET initial_prompt = ? WHERE id = ?", (stored, task["id"]))
            if task["name"] == UNTITLED:
                conn.execute("UPDATE tasks SET name = ? WHERE id = ?", (derive_name(stored), task["id"]))


def end(conn, task_id: int, note: Optional[str] = None, now: Optional[float] = None):
    """Ingest everything outstanding, close the task and freeze its report."""
    task = get(conn, task_id)
    if task is None:
        raise ValueError("Task #%s not found" % task_id)
    if task["status"] != "active":
        return task
    now = now or time.time()
    _ingest_before_end(conn, [task])
    with db.transaction(conn):
        _close(conn, task, note, now)
    return _freeze(conn, task_id)


def reopen(conn, task_id: int, session_id: str):
    """Undo an end: the task counts again from now in the given session."""
    task = get(conn, task_id)
    if task is None:
        raise ValueError("Task #%s not found" % task_id)
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET status = 'active', ended_at = NULL WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM task_snapshots WHERE task_id = ?", (task_id,))
    return attach(conn, task_id, session_id, reason="reopen")


def rename(conn, task_id: int, name: str):
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET name = ? WHERE id = ?", (name.strip() or UNTITLED, task_id))
    return get(conn, task_id)


def delete(conn, task_id: int) -> bool:
    with db.transaction(conn):
        task = get(conn, task_id)
        if task is not None and task["status"] == "active" and task["todo_id"]:
            _release_todo(conn, task, time.time())      # as end() does: nobody works on its todo any more
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cur.rowcount > 0


CLEAR_PID_S = 3600.0     # /clear finds the previous session by process id only if it was seen this recently


def carry_over_after_clear(conn, new_session_id: str, claude_pid: Optional[int],
                           cwd: Optional[str], transcript_path: Optional[str],
                           previous_session: Optional[str] = None):
    """/clear starts a new session id in the same Claude Code process.

    If the previous session had an active task, keep counting it in the new
    session. The previous session is found by Claude Code's process id (seen
    within the last hour: process ids are reused), or else by the session that
    SessionEnd(reason=clear) reported moments ago.
    """
    row = None
    if claude_pid:
        row = conn.execute(
            "SELECT t.id FROM tasks t "
            "JOIN task_sessions ts ON ts.task_id = t.id "
            "JOIN sessions s ON s.session_id = ts.session_id "
            "WHERE t.status = 'active' AND ts.detached_at IS NULL AND s.claude_pid = ? "
            "AND s.session_id != ? AND s.last_seen >= ? ORDER BY s.last_seen DESC LIMIT 1",
            (claude_pid, new_session_id, time.time() - CLEAR_PID_S),
        ).fetchone()
    if row is None and previous_session and previous_session != new_session_id:
        row = active_for_session(conn, previous_session)
    if row is None:
        return None
    task, _ = attach(conn, row["id"], new_session_id, transcript_path, reason="clear")
    return task


def list_tasks(conn, limit: int = 20, status: Optional[str] = None, project: Optional[str] = None) -> list:
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if project:
        sql += " AND project = ?"
        args.append(project)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def prompts(conn, task_id: int) -> list:
    return conn.execute(
        "SELECT session_id, ts, prompt FROM prompts WHERE task_id = ? ORDER BY ts", (task_id,)
    ).fetchall()
