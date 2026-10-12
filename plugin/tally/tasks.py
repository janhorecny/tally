"""Task lifecycle: start, end, attach, rename, delete."""

import re
import time
from typing import List, Optional, Tuple

from . import db, ingest

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
    for r in conn.execute(
        "SELECT session_id, attached_at, detached_at FROM task_sessions WHERE task_id = ? ORDER BY attached_at",
        (task["id"],),
    ):
        a = max(t0, r["attached_at"])
        b = min(t1, r["detached_at"] or t1)
        if b > a:
            out.append((r["session_id"], a, b))
    return out


def touch_session(conn, session_id: str, transcript_path: Optional[str] = None,
                  cwd: Optional[str] = None, claude_pid: Optional[int] = None) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(session_id, transcript_path, cwd, claude_pid, first_seen, last_seen) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
        "transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path), "
        "cwd = COALESCE(excluded.cwd, sessions.cwd), "
        "claude_pid = COALESCE(excluded.claude_pid, sessions.claude_pid), "
        "last_seen = excluded.last_seen",
        (session_id, transcript_path, cwd, claude_pid, now, now),
    )


def start(conn, session_id: str, name: Optional[str] = None, initial_prompt: Optional[str] = None,
          cwd: Optional[str] = None, transcript_path: Optional[str] = None,
          created_by: str = "command", now: Optional[float] = None):
    """Start a task in a session. Ends a task already running in that session.

    Returns (new_task_row, ended_task_row_or_None).
    """
    now = now or time.time()
    ended = None
    prev = active_for_session(conn, session_id)
    if prev is not None:
        ended = end(conn, prev["id"], note="auto-ended: new task started", now=now)
    name = (name or "").strip() or (derive_name(initial_prompt) if initial_prompt else UNTITLED)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO tasks(name, initial_prompt, status, started_at, cwd, origin_session, created_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (name, initial_prompt, "active", now, cwd, session_id, created_by),
        )
        task_id = cur.lastrowid
        conn.execute(
            "INSERT INTO task_sessions(task_id, session_id, attached_at, reason) VALUES(?,?,?,?)",
            (task_id, session_id, now, "start"),
        )
        if initial_prompt:
            conn.execute(
                "INSERT INTO prompts(task_id, session_id, ts, prompt) VALUES(?,?,?,?)",
                (task_id, session_id, now, initial_prompt),
            )
    _skip_history(conn, session_id, transcript_path, exclude_task=task_id)
    return get(conn, task_id), ended


def _skip_history(conn, session_id: str, transcript_path: Optional[str], exclude_task: int) -> None:
    """Register transcript files at their current size so history before a
    task is never parsed. Only safe when no other active task is counting
    this session; otherwise read normally so nothing of that task is lost.
    """
    other = conn.execute(
        "SELECT 1 FROM task_sessions s JOIN tasks t ON t.id = s.task_id WHERE s.session_id = ? "
        "AND s.detached_at IS NULL AND t.status = 'active' AND t.id != ? LIMIT 1",
        (session_id, exclude_task),
    ).fetchone()
    try:
        ingest.ingest_session(conn, session_id, transcript_path, skip_existing=other is None)
    except Exception:
        pass


def attach(conn, task_id: int, session_id: str, transcript_path: Optional[str] = None,
           reason: str = "attach", now: Optional[float] = None):
    """Make a session count towards an existing active task."""
    now = now or time.time()
    task = get(conn, task_id)
    if task is None:
        raise ValueError("task #%s does not exist" % task_id)
    if task["status"] != "active":
        raise ValueError("task #%s has already ended" % task_id)
    counting = conn.execute(
        "SELECT 1 FROM task_sessions WHERE task_id = ? AND session_id = ? AND detached_at IS NULL",
        (task_id, session_id),
    ).fetchone()
    if counting:
        return task, None          # already counting here; nothing to skip or re-attach
    ended = None
    prev = active_for_session(conn, session_id)
    if prev is not None and prev["id"] != task_id:
        ended = end(conn, prev["id"], note="auto-ended: session attached to #%d" % task_id, now=now)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO task_sessions(task_id, session_id, attached_at, reason) VALUES(?,?,?,?)",
            (task_id, session_id, now, reason),
        )
    _skip_history(conn, session_id, transcript_path, exclude_task=task_id)
    return get(conn, task_id), ended


def record_prompt(conn, task, session_id: str, prompt: str, now: Optional[float] = None) -> None:
    now = now or time.time()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO prompts(task_id, session_id, ts, prompt) VALUES(?,?,?,?)",
            (task["id"], session_id, now, prompt),
        )
        if not task["initial_prompt"]:
            conn.execute("UPDATE tasks SET initial_prompt = ? WHERE id = ?", (prompt, task["id"]))
            if task["name"] == UNTITLED:
                conn.execute("UPDATE tasks SET name = ? WHERE id = ?", (derive_name(prompt), task["id"]))


def end(conn, task_id: int, note: Optional[str] = None, now: Optional[float] = None):
    """Ingest everything outstanding, close the task and freeze its report."""
    from . import report  # local import: report imports tasks

    task = get(conn, task_id)
    if task is None:
        raise ValueError("task #%s does not exist" % task_id)
    if task["status"] != "active":
        return task
    now = now or time.time()
    try:
        ingest.ingest_sessions(conn, session_ids(conn, task_id))
    except Exception:
        pass
    with db.transaction(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ended', ended_at = ?, end_note = COALESCE(?, end_note) WHERE id = ?",
            (now, note, task_id),
        )
        conn.execute(
            "UPDATE task_sessions SET detached_at = ? WHERE task_id = ? AND detached_at IS NULL",
            (now, task_id),
        )
    task = get(conn, task_id)
    report.save_snapshot(conn, task_id, report.build(conn, task))
    return task


def reopen(conn, task_id: int, session_id: str):
    """Undo an end: the task counts again from now in the given session."""
    task = get(conn, task_id)
    if task is None:
        raise ValueError("task #%s does not exist" % task_id)
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
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cur.rowcount > 0


def carry_over_after_clear(conn, new_session_id: str, claude_pid: Optional[int],
                           cwd: Optional[str], transcript_path: Optional[str],
                           previous_session: Optional[str] = None):
    """/clear starts a new session id in the same Claude Code process.

    If the previous session had an active task, keep counting it in the new
    session. The previous session is found by Claude Code's process id, or
    else by the session that SessionEnd(reason=clear) reported moments ago.
    """
    row = None
    if claude_pid:
        row = conn.execute(
            "SELECT t.id FROM tasks t "
            "JOIN task_sessions ts ON ts.task_id = t.id "
            "JOIN sessions s ON s.session_id = ts.session_id "
            "WHERE t.status = 'active' AND ts.detached_at IS NULL AND s.claude_pid = ? "
            "AND s.session_id != ? ORDER BY s.last_seen DESC LIMIT 1",
            (claude_pid, new_session_id),
        ).fetchone()
    if row is None and previous_session and previous_session != new_session_id:
        row = active_for_session(conn, previous_session)
    if row is None:
        return None
    task, _ = attach(conn, row["id"], new_session_id, transcript_path, reason="clear")
    return task


def list_tasks(conn, limit: int = 20, status: Optional[str] = None) -> list:
    sql = "SELECT * FROM tasks"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def prompts(conn, task_id: int) -> list:
    return conn.execute(
        "SELECT session_id, ts, prompt FROM prompts WHERE task_id = ? ORDER BY ts", (task_id,)
    ).fetchall()
