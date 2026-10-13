"""Projects: detection (the git repository a directory belongs to), the
per-project tracking mode, and overall per-project statistics."""

import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

MODES = ("track", "tasks", "off")
MODE_HELP = {
    "track": "every session in the project is counted (overall project stats)",
    "tasks": "only tasks you start are counted",
    "off": "Tally ignores the project and does not ask again",
    "ask": "not decided yet",
}


def detect(cwd: Optional[str]) -> Optional[str]:
    """Name of the git repository containing cwd (main repo for worktrees),
    else the directory name. No subprocess: hooks call this."""
    return detect_root(cwd)[0]


def detect_root(cwd: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(name, root path) of the project containing cwd."""
    if not cwd:
        return None, None
    try:
        p = Path(cwd).expanduser()
    except (TypeError, ValueError):
        return None, None
    home = Path.home()
    for d in [p, *p.parents]:
        git = d / ".git"
        try:
            if git.is_dir():
                return d.name, str(d)
            if git.is_file():
                # worktree: "gitdir: /path/main/.git/worktrees/<name>"
                text = git.read_text(errors="ignore").strip()
                if text.startswith("gitdir:"):
                    gd = Path(text.split(":", 1)[1].strip())
                    if not gd.is_absolute():
                        gd = (d / gd).resolve()
                    if gd.parent.name == "worktrees" and gd.parent.parent.name == ".git":
                        main = gd.parent.parent.parent
                        return main.name, str(main)
                return d.name, str(d)
        except OSError:
            break
        if d == home or d == d.parent:
            break
    return (p.name or None), str(p)


# ------------------------------------------------------------------ registry

def get(conn, name: str):
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def register(conn, name: Optional[str], path: Optional[str] = None):
    """Record a project in the mode of setting projects.default_mode (ask by default); returns (row, is_new)."""
    if not name:
        return None, False
    row = get(conn, name)
    if row is not None:
        return row, False
    from . import settings
    mode = settings.get("projects.default_mode")
    conn.execute("INSERT OR IGNORE INTO projects(name, path, mode, first_seen) VALUES(?,?,?,?)",
                 (name, path, mode if mode in MODES else "ask", time.time()))
    return get(conn, name), True


def set_mode(conn, name: str, mode: str):
    if mode not in MODES:
        raise ValueError("mode must be one of: %s" % ", ".join(MODES))
    register(conn, name)
    conn.execute("UPDATE projects SET mode = ?, decided_at = ? WHERE name = ?", (mode, time.time(), name))
    return get(conn, name)


def mode_of(conn, name: Optional[str]) -> str:
    row = get(conn, name) if name else None
    return row["mode"] if row else "ask"


def session_tracked(conn, session_id: str) -> bool:
    row = conn.execute("SELECT p.mode FROM sessions s JOIN projects p ON p.name = s.project "
                       "WHERE s.session_id = ?", (session_id,)).fetchone()
    return bool(row) and row[0] == "track"


def setup_prompt(name: str) -> str:
    from . import fmt
    return "\n".join([
        fmt.head("First time in project %s. Track it?" % name, "👋"),
        "  /tally:project track   count every session here (overall project stats)",
        "  /tally:project tasks   only tasks you start (/tally:start, /tally:take)",
        "  /tally:project off     ignore this project, don't ask again",
        fmt.hints("tally setup sets a default for new projects")])


def default_notice(name: str, mode: str) -> str:
    """New project with a default mode from settings: no question, one line instead."""
    from . import fmt
    return "\n".join([fmt.head("New project %s: %s (%s)" % (name, mode, MODE_HELP[mode]), "👋"),
                      fmt.hints("/tally:project %s change it" % " | ".join(m for m in MODES if m != mode))])


def all_names(conn) -> list:
    rows = conn.execute("SELECT name FROM projects UNION SELECT project FROM tasks WHERE project IS NOT NULL "
                        "UNION SELECT project FROM todos WHERE project IS NOT NULL").fetchall()
    return sorted({r[0] for r in rows if r[0]}, key=str.lower)


# ------------------------------------------------------------------ stats

def stats(conn, name: str, days: Optional[int] = None, series_days: int = 30, usage_conn=None) -> dict:
    """Overall usage of a project.

    track mode: every API call of every session in the project.
    otherwise:  the project's tasks only.
    usage_conn: read the usage of every session from this database instead
    (the sessions index), whatever the mode; tasks and todos still come from conn.
    Also: daily series, by model, tasks, todos, and the share that went
    through tasks.
    """
    from . import paths, report
    from .pricing import Pricer, normalize_model
    pricer = Pricer(paths.load_config().get("pricing"))
    mode = mode_of(conn, name)
    now = time.time()
    since = now - days * 86400 if days else 0
    series_from = now - series_days * 86400

    def zero():
        return {"total": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0, "cost": 0.0}

    def add(d, r, c):
        d["input"] += r["input"]
        d["output"] += r["output"]
        d["cache_read"] += r["cache_read"]
        d["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]
        d["total"] += r["input"] + r["output"] + r["cache_read"] + r["cache_write_5m"] + r["cache_write_1h"]
        d["calls"] += 1
        d["cost"] += c or 0.0

    totals, last7, last30 = zero(), zero(), zero()
    by_model, daily = {}, {}
    sessions_used = set()
    first = last = None
    rows = []
    if usage_conn is not None:
        rows = usage_conn.execute("SELECT u.* FROM usage u JOIN sessions s ON s.session_id = u.session_id "
                                  "WHERE s.project = ? AND u.ts >= ?", (name, since)).fetchall()
    elif mode == "track":
        rows = conn.execute("SELECT u.* FROM usage u JOIN sessions s ON s.session_id = u.session_id "
                            "WHERE s.project = ? AND u.ts >= ?", (name, since)).fetchall()
    else:
        for t in conn.execute("SELECT * FROM tasks WHERE project = ?", (name,)).fetchall():
            from . import tasks as tasks_mod
            for sid, a, b in tasks_mod.task_windows(conn, t):
                rows.extend(conn.execute("SELECT * FROM usage WHERE session_id = ? AND ts >= ? AND ts <= ? "
                                         "AND ts >= ?", (sid, a, b, since)).fetchall())
    seen = set()
    for r in rows:
        if r["msg_key"] in seen:
            continue
        seen.add(r["msg_key"])
        c = pricer.row_cost(r)
        add(totals, r, c)
        if r["ts"] >= now - 7 * 86400:
            add(last7, r, c)
        if r["ts"] >= now - 30 * 86400:
            add(last30, r, c)
        m = normalize_model(r["model"]) or "unknown"
        add(by_model.setdefault(m, zero()), r, c)
        if r["ts"] >= series_from:
            day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
            add(daily.setdefault(day, zero()), r, c)
        sessions_used.add(r["session_id"])
        first = r["ts"] if first is None or r["ts"] < first else first
        last = r["ts"] if last is None or r["ts"] > last else last

    task_rows = conn.execute("SELECT * FROM tasks WHERE project = ? ORDER BY id DESC", (name,)).fetchall()
    task_list = []
    task_tokens = 0
    for t in task_rows:
        s = report.summary(conn, t)
        task_tokens += s["totals"]["total"]
        task_list.append({"id": t["id"], "name": t["name"], "status": t["status"], "started_at": t["started_at"],
                          "total": s["totals"]["total"], "cost": s["totals"]["cost"],
                          "active_s": s["time"]["active_s"]})
    todo_counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM todos WHERE project = ? AND parent_id IS NULL GROUP BY status", (name,))}
    series = []
    for i in range(series_days - 1, -1, -1):
        day = datetime.fromtimestamp(now - i * 86400).strftime("%Y-%m-%d")
        d = daily.get(day, zero())
        series.append({"day": day, "total": d["total"], "output": d["output"], "cost": round(d["cost"], 6)})
    row = get(conn, name)
    for d in (totals, last7, last30):
        d["cost"] = round(d["cost"], 6)
    return {
        "project": name, "path": row["path"] if row else None, "mode": mode,
        "first_seen": row["first_seen"] if row else None,
        "totals": totals, "last_7d": last7, "last_30d": last30,
        "by_model": sorted(({"model": k, **dict(v, cost=round(v["cost"], 6))} for k, v in by_model.items()),
                           key=lambda d: -d["total"]),
        "daily": series,
        "sessions": len(sessions_used), "first_activity": first, "last_activity": last,
        "tasks": {"count": len(task_rows), "active": sum(1 for t in task_rows if t["status"] == "active"),
                  "tokens": task_tokens, "share": round(task_tokens / float(totals["total"]), 4)
                  if mode == "track" and totals["total"] else None, "recent": task_list[:10]},
        "todos": {k: todo_counts.get(k, 0) for k in ("todo", "doing", "blocked", "done", "dropped")},
    }


# ------------------------------------------------------------------ dashboard and exports

def listing(conn, with_usage: bool = False) -> list:
    """Projects Tally knows; with_usage: also every project seen in the sessions
    index in the last 30 days, with that usage (whole sessions, any mode)."""
    usage = {}
    if with_usage:
        from . import sessions
        sessions.refresh_async(30)                 # keep the index current; returns at once
        usage = sessions.project_usage(30)
    names = set(all_names(conn)) | set(usage)
    out = []
    for p in names:
        n_open = conn.execute("SELECT COUNT(*) FROM todos WHERE project = ? AND parent_id IS NULL AND status IN "
                              "('todo','doing','blocked')", (p,)).fetchone()[0]
        n_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE project = ?", (p,)).fetchone()[0]
        d = {"project": p, "open_todos": n_open, "tasks": n_tasks, "mode": mode_of(conn, p),
             "registered": get(conn, p) is not None}
        if with_usage:
            u = usage.get(p) or {"sessions": 0, "total": 0, "output": 0, "calls": 0, "cost": 0.0, "last_activity": None}
            d.update({"last_30d": {k: u[k] for k in ("total", "output", "calls", "cost")}, "sessions": u["sessions"],
                      "last_activity": u["last_activity"] or None})
        out.append(d)
    if with_usage:
        out.sort(key=lambda d: (-d["last_30d"]["total"], d["project"].lower()))
    else:
        out.sort(key=lambda d: d["project"].lower())
    return out


def detail(conn, name: str) -> dict:
    """What Tally counted for the project (tasks, todos, tracked totals) plus the
    last 30 days of every session from the sessions index."""
    from . import sessions
    st = stats(conn, name, series_days=30)
    ix = sessions.connect()
    try:
        recent = stats(conn, name, days=30, series_days=30, usage_conn=ix)
    finally:
        ix.close()
    st["tracked"] = st["totals"]
    for k in ("last_7d", "daily", "by_model", "last_activity", "first_activity"):
        st[k] = recent[k]
    # the same numbers as the projects list: sessions active in the last 30 days
    u = sessions.project_usage(30).get(name) or {"sessions": 0, "total": 0, "output": 0, "calls": 0, "cost": 0.0}
    st["last_30d"] = dict(recent["last_30d"], **{k: u[k] for k in ("total", "output", "calls", "cost")})
    st["sessions"] = u["sessions"]
    st["registered"] = get(conn, name) is not None
    return st
