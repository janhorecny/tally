"""Todo list: per-project items with status, priority, due date and plan import.

A todo can be *taken*: that starts a Tally task linked to it, so its tokens
and time are measured. A todo can be taken several times (pause, resume);
its totals add up over all linked tasks.
"""

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from . import db

STATUSES = ("todo", "doing", "blocked", "done", "dropped")
OPEN = ("todo", "doing", "blocked")
STATUS_ORDER = {"doing": 0, "todo": 1, "blocked": 2, "done": 3, "dropped": 4}
PRIORITY_MARK = {0: "", 1: "!", 2: "!!", 3: "!!!"}

_DAYS = {
    "mon": 0, "monday": 0, "po": 0, "pon": 0, "pondelok": 0,
    "tue": 1, "tuesday": 1, "ut": 1, "uto": 1, "utorok": 1,
    "wed": 2, "wednesday": 2, "st": 2, "str": 2, "streda": 2,
    "thu": 3, "thursday": 3, "stv": 3, "štv": 3, "stvrtok": 3, "štvrtok": 3,
    "fri": 4, "friday": 4, "pi": 4, "pia": 4, "piatok": 4,
    "sat": 5, "saturday": 5, "so": 5, "sob": 5, "sobota": 5,
    "sun": 6, "sunday": 6, "ne": 6, "ned": 6, "nedela": 6, "nedeľa": 6,
}


# ------------------------------------------------------------------ parsing

def _end_of_day(d: datetime) -> float:
    return d.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def parse_due(spec: str, now: Optional[float] = None) -> Optional[float]:
    """today/dnes, tomorrow/zajtra, mon..sun (+ Slovak po..ne), +3d, +2w, +4h,
    2026-10-20, 20.10., 20.10.2026. Dates mean the end of that day."""
    if not spec:
        return None
    s = spec.strip().lower()
    base = datetime.fromtimestamp(now if now is not None else time.time())
    if s in ("today", "dnes", "tod"):
        return _end_of_day(base)
    if s in ("tomorrow", "zajtra", "tmr", "tmrw"):
        return _end_of_day(base + timedelta(days=1))
    if s in _DAYS:
        delta = (_DAYS[s] - base.weekday()) % 7 or 7
        return _end_of_day(base + timedelta(days=delta))
    m = re.match(r"^\+(\d+)([hdw])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            return (base + timedelta(hours=n)).timestamp()
        return _end_of_day(base + timedelta(days=n * (7 if unit == "w" else 1)))
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return _end_of_day(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})?$", s)
    if m:
        year = int(m.group(3)) if m.group(3) else base.year
        d = datetime(year, int(m.group(2)), int(m.group(1)))
        if not m.group(3) and _end_of_day(d) < base.timestamp():
            d = d.replace(year=year + 1)           # "5.1." in December means next January
        return _end_of_day(d)
    raise ValueError("unknown due date %r (try today, tomorrow, fri, +3d, 2026-10-20, 20.10.)" % spec)


def parse_quick(text: str, default_project: Optional[str] = None) -> dict:
    """'Fix login @web !! due:fri -- check Safari too' ->
    {title, project, priority, due_at, notes}."""
    notes = None
    if " -- " in text:
        text, notes = text.split(" -- ", 1)
        notes = notes.strip() or None
    project = default_project
    priority = 0
    due_at = None
    words = []
    for w in text.split():
        if w.startswith("@") and len(w) > 1:
            project = w[1:]
        elif re.fullmatch(r"!{1,3}", w):
            priority = len(w)
        elif w.lower().startswith(("due:", "do:")) and len(w.split(":", 1)[1]) > 0:
            due_at = parse_due(w.split(":", 1)[1])
        else:
            words.append(w)
    title = " ".join(words).strip()
    if not title:
        raise ValueError("the todo needs a title")
    return {"title": title, "project": project, "priority": priority, "due_at": due_at, "notes": notes}


# ------------------------------------------------------------------ storage

def get(conn, todo_id: int):
    return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


def add(conn, title: str, project: Optional[str] = None, priority: int = 0, due_at: Optional[float] = None,
        notes: Optional[str] = None, parent_id: Optional[int] = None, source: str = "user",
        source_ref: Optional[str] = None, status: str = "todo", position: Optional[int] = None):
    now = time.time()
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO todos(project, title, notes, status, priority, due_at, parent_id, position, source, "
            "source_ref, created_at, updated_at, done_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, title.strip(), notes, status, int(priority or 0), due_at, parent_id, position, source,
             source_ref, now, now, now if status == "done" else None),
        )
    return get(conn, cur.lastrowid)


def update(conn, todo_id: int, **fields):
    allowed = {"title", "notes", "project", "priority", "due_at", "status", "parent_id", "position"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "status" in sets and sets["status"] not in STATUSES:
        raise ValueError("status must be one of: %s" % ", ".join(STATUSES))
    if not sets:
        return get(conn, todo_id)
    now = time.time()
    cols = ", ".join("%s = ?" % k for k in sets)
    args = list(sets.values())
    if "status" in sets:
        cols += ", done_at = ?"
        args.append(now if sets["status"] == "done" else None)
    with db.transaction(conn):
        conn.execute("UPDATE todos SET %s, updated_at = ? WHERE id = ?" % cols, args + [now, todo_id])
        if "status" in sets:
            _propagate(conn, todo_id, now)
    return get(conn, todo_id)


def _propagate(conn, todo_id: int, now: float) -> None:
    """All steps done -> parent done (up the plan); a reopened step reopens
    a parent that was done."""
    row = conn.execute("SELECT parent_id, status FROM todos WHERE id = ?", (todo_id,)).fetchone()
    while row is not None and row["parent_id"]:
        pid = row["parent_id"]
        parent = conn.execute("SELECT status FROM todos WHERE id = ?", (pid,)).fetchone()
        open_kids = conn.execute("SELECT COUNT(*) FROM todos WHERE parent_id = ? AND status IN ('todo','doing',"
                                 "'blocked')", (pid,)).fetchone()[0]
        if open_kids == 0 and parent["status"] in OPEN:
            conn.execute("UPDATE todos SET status = 'done', done_at = ?, updated_at = ? WHERE id = ?", (now, now, pid))
        elif open_kids and parent["status"] == "done":
            conn.execute("UPDATE todos SET status = 'todo', done_at = NULL, updated_at = ? WHERE id = ?", (now, pid))
        else:
            break
        row = conn.execute("SELECT parent_id, status FROM todos WHERE id = ?", (pid,)).fetchone()


def set_status(conn, todo_id: int, status: str, note: Optional[str] = None):
    todo = get(conn, todo_id)
    if todo is None:
        raise ValueError("todo #%s does not exist" % todo_id)
    fields = {"status": status}
    if note:
        fields["notes"] = (todo["notes"] + "\n" if todo["notes"] else "") + note
    return update(conn, todo_id, **fields)


def delete(conn, todo_id: int) -> bool:
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET todo_id = NULL WHERE todo_id = ?", (todo_id,))
        cur = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    return cur.rowcount > 0


def list_todos(conn, project: Optional[str] = None, statuses=OPEN, top_level: bool = True,
               limit: int = 0, parent_id: Optional[int] = None) -> list:
    sql = "SELECT * FROM todos WHERE 1=1"
    args: list = []
    if project:
        sql += " AND project = ?"
        args.append(project)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args.extend(statuses)
    if parent_id is not None:
        sql += " AND parent_id = ?"
        args.append(parent_id)
    elif top_level:
        sql += " AND parent_id IS NULL"
    rows = conn.execute(sql, args).fetchall()
    now = time.time()
    rows.sort(key=lambda r: (
        STATUS_ORDER.get(r["status"], 9),
        0 if (r["due_at"] and r["due_at"] < now and r["status"] in OPEN) else 1,
        -(r["priority"] or 0),
        r["due_at"] or 1e18,
        r["position"] if r["position"] is not None else 1e9,
        r["id"],
    ))
    return rows[:limit] if limit else rows


def children(conn, todo_id: int) -> list:
    return conn.execute("SELECT * FROM todos WHERE parent_id = ? ORDER BY COALESCE(position, 1e9), id",
                        (todo_id,)).fetchall()


def progress(conn, todo_id: int) -> Tuple[int, int]:
    """(done, total) over all descendants that are leaves of the plan tree."""
    done = total = 0
    stack = [todo_id]
    while stack:
        tid = stack.pop()
        kids = children(conn, tid)
        for k in kids:
            grand = conn.execute("SELECT COUNT(*) FROM todos WHERE parent_id = ?", (k["id"],)).fetchone()[0]
            if grand:
                stack.append(k["id"])
            elif k["status"] != "dropped":
                total += 1
                done += k["status"] == "done"
    return done, total


def projects(conn) -> List[str]:
    rows = conn.execute(
        "SELECT project FROM todos WHERE project IS NOT NULL UNION SELECT project FROM tasks WHERE project IS NOT NULL"
    ).fetchall()
    return sorted({r[0] for r in rows}, key=str.lower)


def linked_stats(conn, todo_id: int) -> dict:
    """Tokens, cost and time of all tasks that worked on this todo."""
    from . import report
    out = {"tasks": 0, "total": 0, "output": 0, "cost": 0.0, "active_s": 0.0, "wall_s": 0.0, "active_task": None}
    for t in conn.execute("SELECT * FROM tasks WHERE todo_id = ? ORDER BY id", (todo_id,)).fetchall():
        s = report.summary(conn, t)
        out["tasks"] += 1
        out["total"] += s["totals"]["total"]
        out["output"] += s["totals"]["output"]
        out["cost"] += s["totals"]["cost"]
        out["active_s"] += s["time"]["active_s"]
        out["wall_s"] += s["time"]["wall_s"]
        if t["status"] == "active":
            out["active_task"] = t["id"]
    return out


def next_todo(conn, project: Optional[str] = None):
    """What to work on next: an open leaf in plan order. Something already
    being worked on (doing) wins, then the first open step of the most
    important top-level todo."""
    def leaves(row):
        kids = [k for k in children(conn, row["id"]) if k["status"] in ("todo", "doing")]
        if not kids:
            yield row
            return
        for k in kids:
            yield from leaves(k)

    tops = list_todos(conn, project=project, statuses=("doing", "todo"))
    ordered = [leaf for top in tops for leaf in leaves(top)]
    for leaf in ordered:
        if leaf["status"] == "doing":
            return leaf
    return ordered[0] if ordered else None


# ------------------------------------------------------------------ taking

def take(conn, todo_id: int, session_id: str, cwd: Optional[str] = None, transcript_path: Optional[str] = None,
         prompt: Optional[str] = None, prompt_id: Optional[str] = None):
    """Start a Tally task for a todo. Returns (todo, task, ended_task)."""
    from . import tasks
    todo = get(conn, todo_id)
    if todo is None:
        raise ValueError("todo #%s does not exist" % todo_id)
    if todo["status"] in ("done", "dropped"):
        update(conn, todo_id, status="todo")
    running = conn.execute(
        "SELECT t.* FROM tasks t JOIN task_sessions s ON s.task_id = t.id WHERE t.todo_id = ? AND "
        "t.status = 'active' AND s.session_id = ? AND s.detached_at IS NULL", (todo_id, session_id)).fetchone()
    ended = None
    if running is not None:
        task = running
    else:
        task, ended = tasks.start(conn, session_id, name=todo["title"], cwd=cwd, transcript_path=transcript_path,
                                  created_by="todo", project=todo["project"], todo_id=todo_id,
                                  initial_prompt=prompt, prompt_id=prompt_id)
    todo = update(conn, todo_id, status="doing")
    return todo, task, ended


def context_for(conn, todo) -> str:
    """Text handed to Claude by /tally:do (additionalContext)."""
    lines = ["Tally todo #%d (project %s): %s" % (todo["id"], todo["project"] or "-", todo["title"])]
    if todo["priority"]:
        lines.append("Priority: %s" % ("low", "medium", "high")[todo["priority"] - 1])
    if todo["due_at"]:
        lines.append("Due: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(todo["due_at"])))
    if todo["notes"]:
        lines.append("Notes: %s" % todo["notes"])
    kids = children(conn, todo["id"])
    if kids:
        lines.append("Steps:")
        for k in kids:
            lines.append("  [%s] #%d %s" % ("x" if k["status"] == "done" else " ", k["id"], k["title"]))
    lines.append("Work on this todo now. Tally is measuring tokens and time for it. When it is finished, "
                 "tell the user so they can close it with /tally:done (or run: tally todo done %d)." % todo["id"])
    return "\n".join(lines)


# ------------------------------------------------------------------ reminder

def reminder(conn, project: Optional[str], limit: int = 5) -> Optional[str]:
    if not project:
        return None
    rows = list_todos(conn, project=project, statuses=OPEN)
    if not rows:
        return None
    now = time.time()
    doing = sum(1 for r in rows if r["status"] == "doing")
    overdue = sum(1 for r in rows if r["due_at"] and r["due_at"] < now)
    head = "📋 %s: %d open todo%s" % (project, len(rows), "" if len(rows) == 1 else "s")
    extra = [x for x in ("%d doing" % doing if doing else "", "%d overdue" % overdue if overdue else "") if x]
    if extra:
        head += " (" + ", ".join(extra) + ")"
    lines = [head]
    for r in rows[:limit]:
        lines.append("  %s" % line_for(r, now))
    if len(rows) > limit:
        lines.append("  … %d more" % (len(rows) - limit))
    lines.append("  /tally:todos · /tally:take <id> · /tally:do <id>")
    return "\n".join(lines)


def due_label(due_at: Optional[float], now: Optional[float] = None) -> str:
    if not due_at:
        return ""
    now = now or time.time()
    d = datetime.fromtimestamp(due_at).date()
    today = datetime.fromtimestamp(now).date()
    if due_at < now:
        return "overdue %s" % d.strftime("%d.%m.")
    if d == today:
        return "due today"
    if d == today + timedelta(days=1):
        return "due tomorrow"
    return "due %s" % d.strftime("%a %d.%m.")


def line_for(r, now: Optional[float] = None, show_project: bool = False) -> str:
    mark = {"doing": "▶", "todo": "○", "blocked": "⏸", "done": "✓", "dropped": "✗"}.get(r["status"], "·")
    parts = ["%s #%d" % (mark, r["id"]), r["title"]]
    if PRIORITY_MARK.get(r["priority"] or 0):
        parts.append(PRIORITY_MARK[r["priority"]])
    if show_project and r["project"]:
        parts.append("@" + r["project"])
    label = due_label(r["due_at"], now) if r["status"] in OPEN else ""
    if label:
        parts.append("· " + label)
    return " ".join(parts)


# ------------------------------------------------------------------ plan import

_FENCE = re.compile(r"^\s*(```|~~~)")
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_TASK_HEAD = re.compile(
    r"^(#{2,4})\s+(?:(?:task|úloha|uloha|phase|fáza|faza|milestone)\s*(\d+)\s*[:.)\-–]?\s*|(\d+)[.)]\s+)(.+?)\s*#*\s*$",
    re.I)
_CHECK = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.+?)\s*$")
_BOLD_STEP = re.compile(r"^\s*(?:[-*+]\s+(?:\[( |x|X)\]\s+)?)?\*\*(?:step|krok)\s*\d+\s*[:.]\s*(.+?)\*\*\s*$", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


def _clean(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text)
    text = text.replace("✅", "").strip()
    return re.sub(r"\s+", " ", text)


def parse_plan(text: str, fallback_title: str) -> dict:
    """Markdown plan -> {title, items: [{title, done, steps: [{title, done}]}]}.

    Recognised: superpowers-style "### Task N: ..." headings with
    "**Step N: ...**" lines, numbered "## 1. ..." headings, and "- [ ] / - [x]"
    checklists (under a task heading they become its steps). Code blocks are
    ignored.
    """
    title = None
    items: List[dict] = []
    current = None
    in_code = False
    for raw in text.splitlines():
        if _FENCE.match(raw):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _H1.match(raw)
        if m and title is None and not raw.startswith("##"):
            title = _clean(m.group(1))
            continue
        m = _TASK_HEAD.match(raw)
        if m:
            name = m.group(4)
            done = "✅" in name or "[x]" in name.lower()
            current = {"title": _clean(name.replace("[x]", "").replace("[X]", "")), "done": done, "steps": []}
            items.append(current)
            continue
        if re.match(r"^#{1,6}\s", raw):
            current = None              # any other heading ends the current task
        m = _BOLD_STEP.match(raw)
        if m:
            step = {"title": _clean(m.group(2)), "done": (m.group(1) or "").lower() == "x"}
            (current["steps"] if current else items).append(step if current else dict(step, steps=[]))
            continue
        m = _CHECK.match(raw)
        if m:
            step = {"title": _clean(m.group(2)), "done": m.group(1).lower() == "x"}
            if current is not None:
                current["steps"].append(step)
            else:
                items.append(dict(step, steps=[]))
    for it in items:
        if it["steps"] and all(s["done"] for s in it["steps"]):
            it["done"] = True
    return {"title": title or fallback_title, "items": items}


def import_plan(conn, path: str, project: Optional[str] = None, source: str = "import") -> dict:
    """Create or update todos from a markdown plan. Re-importing the same
    file updates the existing todos (by file + item title) and only ever
    moves them forward to done, never back."""
    from . import projects as proj
    p = Path(path).expanduser().resolve()
    text = p.read_text(encoding="utf-8", errors="replace")
    plan = parse_plan(text, p.stem.replace("-", " ").replace("_", " "))
    if not plan["items"]:
        raise ValueError("no tasks or checklist items found in %s" % p)
    project = project or proj.detect(str(p.parent))
    ref_base = str(p)
    stats = {"created": 0, "updated": 0, "done": 0, "total": 0}

    def upsert(title, ref, parent_id, position, done, status_default="todo"):
        row = conn.execute("SELECT * FROM todos WHERE source_ref = ?", (ref,)).fetchone()
        if row is None:
            row = add(conn, title, project=project, parent_id=parent_id, source=source, source_ref=ref,
                      status="done" if done else status_default, position=position)
            stats["created"] += 1
            return row
        fields = {}
        if row["title"] != title:
            fields["title"] = title
        if row["parent_id"] != parent_id:
            fields["parent_id"] = parent_id
        if row["position"] != position:
            fields["position"] = position
        if done and row["status"] != "done":
            fields["status"] = "done"
        if fields:
            row = update(conn, row["id"], **fields)
            stats["updated"] += 1
        return row

    root = upsert(plan["title"], ref_base + "::plan", None, None, False)
    seen = {}
    for i, it in enumerate(plan["items"]):
        key = _slug(it["title"])
        seen[key] = seen.get(key, 0) + 1
        key = key if seen[key] == 1 else "%s-%d" % (key, seen[key])
        row = upsert(it["title"], "%s::%s" % (ref_base, key), root["id"], i, it["done"])
        stats["total"] += 1
        stats["done"] += it["done"]
        sseen = {}
        for j, st in enumerate(it["steps"]):
            skey = _slug(st["title"])
            sseen[skey] = sseen.get(skey, 0) + 1
            skey = skey if sseen[skey] == 1 else "%s-%d" % (skey, sseen[skey])
            upsert(st["title"], "%s::%s/%s" % (ref_base, key, skey), row["id"], j, st["done"])
    done, total = progress(conn, root["id"])
    if total and done == total and root["status"] != "done":
        update(conn, root["id"], status="done")
    stats.update({"plan_id": root["id"], "title": plan["title"], "project": project,
                  "progress_done": done, "progress_total": total, "file": os.path.relpath(str(p))})
    return stats
