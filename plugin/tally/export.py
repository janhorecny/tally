"""Exports for spreadsheets, time trackers and documents.

The dashboard (Export menus, Copy buttons) and the CLI (`tally export`) use
the same functions, so a file looks the same wherever it was made.

  scope      what                                         formats
  summary    everything at a glance: agents, projects,    md json csv
             models, tasks
  tasks      every task (of one project with project=)    csv json md text
  task       one task (ref = id)                          md text json csv
  projects   projects with their last 30 days             csv json md
  project    one project (ref = name)                     md json csv (daily series)
  sessions   sessions of a period (agent=, project=)      csv json md
  session    one session (ref = id or prefix)             md text json

text is one line per task or session: what a time tracker wants pasted in.
"""

import csv
import io
import json
import re
import time
from typing import Optional, Tuple

from . import db, fmt, projects, report, sessions, tasks

FORMATS = {
    "summary": ("md", "json", "csv"),
    "tasks": ("csv", "json", "md", "text"),
    "task": ("md", "text", "json", "csv"),
    "projects": ("csv", "json", "md"),
    "project": ("md", "json", "csv"),
    "sessions": ("csv", "json", "md"),
    "session": ("md", "text", "json"),
}
MIME = {"csv": "text/csv; charset=utf-8", "json": "application/json; charset=utf-8",
        "md": "text/markdown; charset=utf-8", "text": "text/plain; charset=utf-8"}
EXT = {"csv": "csv", "json": "json", "md": "md", "text": "txt"}

TASK_COLUMNS = ["id", "name", "status", "started", "ended", "wall_s", "active_s", "total_tokens", "input", "output",
                "cache_write", "cache_read", "api_calls", "cost_usd", "subagents", "workflows", "initial_prompt",
                # added in 0.7: what a time sheet needs
                "project", "todo_id", "prompts", "wall_hm", "active_hm", "wall_hours", "active_hours", "note"]


class ExportError(ValueError):
    pass


def export(scope: str, fmt_name: str, ref: Optional[str] = None, project: Optional[str] = None,
           days: Optional[float] = 30, agent: Optional[str] = None, conn=None) -> Tuple[str, str]:
    """(content, file name) of an export. Raises ExportError for an unknown
    scope or format, or a task / project / session that does not exist."""
    if scope not in FORMATS:
        raise ExportError("unknown export %r; choose from %s" % (scope, ", ".join(FORMATS)))
    if fmt_name not in FORMATS[scope]:
        raise ExportError("%s can be exported as %s" % (scope, ", ".join(FORMATS[scope])))
    if scope in ("task", "project", "session") and not ref:
        raise ExportError("which %s? give its %s, e.g. tally export %s %s" % (
            scope, {"task": "number", "project": "name", "session": "id"}[scope], scope,
            {"task": "12", "project": "web-app", "session": "3f9c2a1b"}[scope]))
    conn = conn or db.connect()
    content, label = globals()["_" + scope](conn, fmt_name, ref=ref, project=project, days=days, agent=agent)
    name = "tally-%s%s-%s.%s" % (scope, ("-" + _slug(label)) if label else "", time.strftime("%Y-%m-%d"),
                                 EXT[fmt_name])
    return content, name


# ------------------------------------------------------------------ helpers

def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in str(text).lower())
    return "-".join(p for p in keep.split("-") if p)[:48]


def _when(ts) -> str:
    return fmt.when(ts, "%Y-%m-%d %H:%M") if ts else ""


def hm(seconds) -> str:
    """Hours and minutes, as time sheets write them: 1:05."""
    if seconds is None:
        return ""
    m = int(round(seconds / 60.0))
    return "%d:%02d" % (m // 60, m % 60)


def _num(x) -> str:
    """A count with thousands separators: 76,781."""
    return "{:,}".format(int(x or 0))


def hours(seconds) -> str:
    return "" if seconds is None else "%.2f" % (seconds / 3600.0)


def _csv(header, rows) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue()


def _cell(text) -> str:
    """A Markdown table cell: one line, pipes escaped."""
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")


_NUMERIC = re.compile(r"^[\d$.,:%+\-–kMB ]+$")


def _md_table(header, rows) -> str:
    """Numbers right-aligned, text left: decided per column from its cells."""
    def numeric(i):
        cells = [str(r[i]) for r in rows if str(r[i]).strip() and not str(r[i]).startswith("**")]
        return bool(cells) and all(_NUMERIC.match(c) for c in cells)
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---:" if numeric(i) else "---" for i in range(len(header))) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _cost(d) -> str:
    return fmt.money(d)


# ------------------------------------------------------------------ tasks

def _task_rows(conn, project=None):
    for t in tasks.list_tasks(conn, limit=0, project=project):
        yield t, report.summary_for(conn, t)


def _task_csv_row(t, rep) -> list:
    task, tm, tot = rep["task"], rep["time"], rep["totals"]
    n_agents = sum(x["agents"] for x in rep.get("by_type", []) if x["agent_type"] != "main")
    return [task["id"], task["name"], task["status"], fmt.when(task["started_at"], "%Y-%m-%dT%H:%M:%S"),
            fmt.when(task["ended_at"], "%Y-%m-%dT%H:%M:%S") if task.get("ended_at") else "",
            tm["wall_s"], tm["active_s"], tot["total"], tot["input"], tot["output"], tot["cache_write"],
            tot["cache_read"], tot["calls"], round(tot["cost"], 4), n_agents, len(rep.get("workflows", [])),
            task.get("initial_prompt") or "",
            t["project"] or "", t["todo_id"] or "", task.get("prompt_count") or 0,
            hm(tm["wall_s"]), hm(tm["active_s"]), hours(tm["wall_s"]), hours(tm["active_s"]), task.get("end_note") or ""]


def task_line(rep) -> str:
    """One line for a time tracker: name, project, when, active and wall time, tokens, cost."""
    t, tm, tot = rep["task"], rep["time"], rep["totals"]
    start = fmt.when(t["started_at"], "%Y-%m-%d %H:%M")
    end = ""
    if t.get("ended_at"):
        same = fmt.when(t["ended_at"], "%Y-%m-%d") == fmt.when(t["started_at"], "%Y-%m-%d")
        end = "–" + fmt.when(t["ended_at"], "%H:%M" if same else "%Y-%m-%d %H:%M")
    parts = ["%s (#%s)" % (t["name"], t["id"]) if t.get("id") else t["name"]]
    if t.get("project"):
        parts.append(t["project"])
    parts += [start + end, "active %s" % fmt.duration(tm["active_s"]), "wall %s" % fmt.duration(tm["wall_s"]),
              "%s tokens" % fmt.tokens(tot["total"]), fmt.money(tot)]
    if t.get("end_note"):
        parts.append(t["end_note"])
    return " · ".join(parts)


def _tasks(conn, f, project=None, **_):
    rows = list(_task_rows(conn, project))
    if f == "json":
        return _json([rep for _, rep in rows]), project
    if f == "csv":
        return _csv(TASK_COLUMNS, [_task_csv_row(t, rep) for t, rep in rows]), project
    if f == "text":
        return "\n".join(task_line(rep) for _, rep in rows) + ("\n" if rows else ""), project
    body = [[rep["task"]["id"], rep["task"]["name"], t["project"] or "", fmt.when(rep["task"]["started_at"]),
             hm(rep["time"]["wall_s"]), hm(rep["time"]["active_s"]), fmt.tokens(rep["totals"]["total"]),
             _cost(rep["totals"])] for t, rep in rows]
    wall = sum(rep["time"]["wall_s"] or 0 for _, rep in rows)
    active = sum(rep["time"]["active_s"] or 0 for _, rep in rows)
    tok = sum(rep["totals"]["total"] for _, rep in rows)
    cost = sum(rep["totals"]["cost"] or 0 for _, rep in rows)
    body.append(["", "**Total**", "", "", hm(wall), hm(active), fmt.tokens(tok), fmt.money({"cost": cost})])
    title = "# Tally tasks" + (" · " + project if project else "")
    return "\n".join([title, "", "%s tasks · exported %s" % (_num(len(rows)), _when(time.time())), "",
                      _md_table(["#", "Task", "Project", "Started", "Wall (h:mm)", "Active (h:mm)", "Tokens", "Cost"],
                                body), ""]), project


def _get_task(conn, ref):
    try:
        t = tasks.get(conn, int(str(ref).lstrip("#")))
    except (TypeError, ValueError):
        t = None
    if t is None:
        raise ExportError("task %s not found" % ref)
    return t


def _task(conn, f, ref=None, **_):
    t = _get_task(conn, ref)
    rep = report.summary_for(conn, t)
    label = "%s-%s" % (t["id"], t["name"])
    if f == "json":
        rep["prompt_list"] = [dict(r) for r in tasks.prompts(conn, t["id"])]
        return _json(rep), label
    if f == "csv":
        return _csv(TASK_COLUMNS, [_task_csv_row(t, rep)]), label
    if f == "text":
        return task_line(rep) + "\n", label
    return report_md(rep), label


def report_md(rep) -> str:
    """A task or session report as Markdown: facts, agents, models, prompts."""
    t, tm, tot, sess = rep["task"], rep["time"], rep["totals"], rep.get("session")
    title = (sess.get("title") or "Session " + sess["session_id"]) if sess else "%s (#%s)" % (t["name"], t["id"])
    same_day = t.get("ended_at") and fmt.when(t["ended_at"], "%Y-%m-%d") == fmt.when(t["started_at"], "%Y-%m-%d")
    end = (" → " + fmt.when(t["ended_at"], "%H:%M" if same_day else "%Y-%m-%d %H:%M")) if t.get("ended_at") \
        else " (running)"
    facts = []
    if sess:
        facts.append(("Agent", fmt.AGENT_NAMES.get(sess["agent"], sess["agent"])))
        facts.append(("Session", sess["session_id"]))
    if t.get("project"):
        facts.append(("Project", t["project"]))
    if t.get("todo"):
        facts.append(("Todo", "#%d %s (%s)" % (t["todo"]["id"], t["todo"]["title"], t["todo"]["status"])))
    facts += [("When", _when(t["started_at"]) + end),
              ("Active time", "%s (%s, %d turns)" % (fmt.duration(tm["active_s"]), hm(tm["active_s"]), tm["turns"])),
              ("Wall time", "%s (%s)" % (fmt.duration(tm["wall_s"]), hm(tm["wall_s"]))),
              ("Tokens", "%s · in %s · out %s · cache write %s · cache read %s" % (
                  fmt.tokens(tot["total"]), fmt.tokens(tot["input"]), fmt.tokens(tot["output"]),
                  fmt.tokens(tot["cache_write"]), fmt.tokens(tot["cache_read"]))),
              ("Cost", "%s at API list price · %s API calls" % (fmt.money(tot), _num(tot["calls"])))]
    un = rep.get("unlogged") or {}
    if un.get("status") in ("estimated", "partial") and un.get("cost"):
        facts.append(("Unlogged", "≈ +%s tokens · +%s (Claude Code's own calls, estimated)" % (
            fmt.tokens(un["total"]), fmt.money(un))))
    if t.get("initial_prompt"):
        facts.append(("Prompt", t["initial_prompt"]))
    if t.get("end_note"):
        facts.append(("Note", t["end_note"]))
    out = ["## " + _cell(title), "", "| Item | Value |", "|---|---|"] + ["| %s | %s |" % (k, _cell(v)) for k, v in facts]

    agents = []
    for n in rep.get("tree", []):
        vals = n["own"] if n["kind"] == "main" else n["rollup"]
        agents.append([fmt.node_label(n), fmt.tokens(vals["total"]), fmt.tokens(vals["output"]), fmt.money(vals),
                       fmt.duration(n.get("duration_s")), vals["calls"]])
        for c in (n.get("children") or [])[:20]:
            agents.append(["  └ " + fmt.node_label(c), fmt.tokens(c["rollup"]["total"]), fmt.tokens(c["rollup"]["output"]),
                           fmt.money(c["rollup"]), fmt.duration(c.get("duration_s")), c["rollup"]["calls"]])
    if agents:
        out += ["", "### Agents", "", _md_table(["Agent", "Tokens", "Output", "Cost", "Time", "Calls"], agents)]
    if rep.get("by_model"):
        out += ["", "### Models", "", _md_table(["Model", "Tokens", "Output", "Cost"], [
            [m["model"], fmt.tokens(m["total"]), fmt.tokens(m["output"]), fmt.money(m)] for m in rep["by_model"]])]
    if rep.get("prompts"):
        out += ["", "### Prompts", "", _md_table(["#", "Prompt", "Tokens", "Cost", "Took", "Calls"], [
            [p["index"], (p["prompt"] or "(not stored)")[:160], fmt.tokens(p["total"]), fmt.money(p),
             fmt.duration(p["duration_s"]), p["calls"]] for p in rep["prompts"]])]
    out += ["", "_Exported from Tally %s. Cost is an estimate at API list price._" % _when(time.time()), ""]
    return "\n".join(out)


# ------------------------------------------------------------------ projects

def _projects(conn, f, **_):
    rows = projects.listing(conn, with_usage=True)
    if f == "json":
        return _json(rows), None
    if f == "csv":
        return _csv(["project", "mode", "set_up", "sessions_30d", "tokens_30d", "output_30d", "api_calls_30d",
                     "cost_30d_usd", "tasks", "open_todos", "last_activity", "sessions_read",
                     "not_tracked_sessions_30d"],
                    [[p["project"], p["mode"], "yes" if p["registered"] else "no", p["sessions"],
                      p["last_30d"]["total"], p["last_30d"]["output"], p["last_30d"]["calls"],
                      round(p["last_30d"]["cost"], 4), p["tasks"], p["open_todos"], _when(p["last_activity"]),
                      "yes" if p["sessions_read"] else "no", p["not_tracked"]["sessions"]]
                     for p in rows]), None
    body = [[p["project"], p["mode"] if p["registered"] else "not set up", p["sessions"],
             fmt.tokens(p["last_30d"]["total"]), fmt.money(p["last_30d"]), p["not_tracked"]["sessions"] or "",
             p["tasks"], p["open_todos"], _when(p["last_activity"])] for p in rows]
    return "\n".join(["# Tally projects · last 30 days", "",
                      "Usage of the sessions Tally reads in the last 30 days, from the agents' transcripts: "
                      "projects set to track or tasks, and sessions added one by one. Not tracked: sessions "
                      "of projects that are not set up, counted but not read. Exported %s."
                      % _when(time.time()), "",
                      _md_table(["Project", "Tracking", "Sessions", "Tokens", "Cost", "Not tracked", "Tasks",
                                 "Open todos", "Last active"], body), ""]), None


def _project(conn, f, ref=None, **_):
    if not ref:
        raise ExportError("which project? give its name")
    if ref not in projects.all_names(conn) and ref not in sessions.project_usage(30) \
            and ref not in sessions.not_tracked_projects(30):
        raise ExportError("project %s not found" % ref)
    st = projects.detail(conn, ref)
    if f == "json":
        return _json(st), ref
    if f == "csv":
        return _csv(["day", "tokens", "output", "cost_usd"],
                    [[d["day"], d["total"], d["output"], d["cost"]] for d in st["daily"]]), ref
    m, tk, td = st["last_30d"], st["tasks"], st["todos"]
    facts = [("Tracking", st["mode"] if st.get("registered") else "not set up"),
             ("Last 30 days", "%s tokens · %s · %s API calls · %s sessions" % (
                 fmt.tokens(m["total"]), fmt.money(m), _num(m.get("calls", 0)), _num(st["sessions"]))),
             ("Last 7 days", "%s tokens · %s" % (fmt.tokens(st["last_7d"]["total"]), fmt.money(st["last_7d"]))),
             ("Kept by Tally", "%s tokens · %s" % (fmt.tokens(st["tracked"]["total"]), fmt.money(st["tracked"]))),
             ("Tasks", "%d (%d active)" % (tk["count"], tk["active"])),
             ("Todos", "%d open · %d done" % (td["todo"] + td["doing"] + td["blocked"], td["done"]))]
    if st["not_tracked"]["sessions"]:
        facts.insert(2, ("Not tracked", "%s sessions, not read (set the project to track or tasks)"
                         % _num(st["not_tracked"]["sessions"])))
    if st.get("path"):
        facts.insert(0, ("Path", st["path"]))
    out = ["# %s" % _cell(st["project"]), "", "| Item | Value |", "|---|---|"] + ["| %s | %s |" % (k, _cell(v)) for k, v in facts]
    if st["by_model"]:
        out += ["", "## Models · last 30 days", "", _md_table(["Model", "Tokens", "Output", "Cost"], [
            [x["model"], fmt.tokens(x["total"]), fmt.tokens(x["output"]), fmt.money(x)] for x in st["by_model"]])]
    if tk["recent"]:
        out += ["", "## Tasks", "", _md_table(["#", "Task", "Tokens", "Cost", "Active (h:mm)"], [
            [x["id"], x["name"], fmt.tokens(x["total"]), fmt.money(x), hm(x["active_s"])] for x in tk["recent"]])]
    days = [d for d in st["daily"] if d["total"]]
    if days:
        out += ["", "## Tokens per day", "", _md_table(["Day", "Tokens", "Cost"], [
            [d["day"], fmt.tokens(d["total"]), fmt.money(d)] for d in days])]
    out += ["", "_Exported from Tally %s. Cost is an estimate at API list price._" % _when(time.time()), ""]
    return "\n".join(out), ref


# ------------------------------------------------------------------ sessions

def _sessions(conn, f, project=None, days=30, agent=None, **_):
    ov = sessions.overview(days, agent=agent, project=project)
    label = "-".join(x for x in (project, agent) if x) or None
    if f == "json":
        return _json(ov), label
    if f == "csv":
        return _csv(["session_id", "agent", "project", "title", "first_activity", "last_activity", "wall_s",
                     "active_s", "active_hm", "prompts", "api_calls", "total_tokens", "input", "output",
                     "cache_write", "cache_read", "cost_usd", "models", "tasks"],
                    [[s["session_id"], s["agent"], s["project"] or "", s["title"] or "", _when(s["first_ts"]),
                      _when(s["last_ts"]), s["wall_s"], s["active_s"] if s["active_s"] is not None else "",
                      hm(s["active_s"]), s["prompts"], s["totals"]["calls"], s["totals"]["total"],
                      s["totals"]["input"], s["totals"]["output"], s["totals"]["cache_write"],
                      s["totals"]["cache_read"], round(s["totals"].get("cost_with_unlogged", s["totals"]["cost"]), 4),
                      " ".join(m["model"] for m in s["models"]), " ".join("#%d" % t["id"] for t in s["tasks"])]
                     for s in ov["sessions"]]), label
    t = ov["totals"]
    span = ("last %g days" % days) if days else "all time"
    body = [[_when(s["last_ts"]), fmt.AGENT_NAMES.get(s["agent"], s["agent"]), s["project"] or "", s["title"] or s["session_id"],
             hm(s["active_s"]), s["prompts"], fmt.tokens(s["totals"]["total"]),
             fmt.money(dict(s["totals"], cost=s["totals"].get("cost_with_unlogged", s["totals"]["cost"])))]
            for s in ov["sessions"]]
    head = "# Tally sessions · %s%s" % (span, (" · " + project) if project else "")
    rest = ov["not_tracked"]["sessions"]
    return "\n".join([head, "", "%s sessions · %s tokens · %s · %s prompts · active %s.%s Exported %s." % (
        _num(t["sessions"]), fmt.tokens(t["total"]), fmt.money(t), _num(t["prompts"]), fmt.duration(t["active_s"]),
        (" Not listed: %s sessions of projects that are not tracked." % _num(rest)) if rest else "",
        _when(time.time())), "",
        _md_table(["Last activity", "Agent", "Project", "Session", "Active (h:mm)", "Prompts", "Tokens", "Cost"],
                  body), ""]), label


def session_line(rep) -> str:
    s, tm, tot = rep["session"], rep["time"], rep["totals"]
    un = rep.get("unlogged") or {}
    cost = tot["cost"] + (un.get("cost") or 0 if un.get("status") in ("estimated", "partial") else 0)
    parts = [s.get("title") or "Session " + s["session_id"], fmt.AGENT_NAMES.get(s["agent"], s["agent"])]
    if s.get("project"):
        parts.append(s["project"])
    parts += [_when(rep["task"]["started_at"]) + "–" + fmt.when(rep["task"]["ended_at"], "%H:%M"),
              "active %s" % fmt.duration(tm["active_s"]), "%s tokens" % fmt.tokens(tot["total"]),
              fmt.money({"cost": cost})]
    return " · ".join(parts)


def _session(conn, f, ref=None, **_):
    found = sessions.index_session(str(ref or ""))
    rep = sessions.detail(found["session_id"]) if found else None
    if rep is None or rep.get("empty"):
        raise ExportError("session %s not found or without API calls" % ref)
    label = rep["session"]["session_id"][:8]
    if f == "json":
        return _json(rep), label
    if f == "text":
        return session_line(rep) + "\n", label
    return report_md(rep), label


# ------------------------------------------------------------------ summary

def _summary(conn, f, days=30, **_):
    ov = sessions.overview(days)
    by_project = sessions.project_usage(days) if days else {}
    if not days:
        for s in ov["sessions"]:
            if s["project"]:
                d = by_project.setdefault(s["project"], {"sessions": 0, "total": 0, "cost": 0.0})
                d["sessions"] += 1
                d["total"] += s["totals"]["total"]
                d["cost"] += s["totals"].get("cost_with_unlogged", s["totals"]["cost"])
    models = {}
    for s in ov["sessions"]:
        for m in s["models"]:
            d = models.setdefault(m["model"], {"total": 0, "cost": 0.0, "sessions": 0})
            d["total"] += m["total"]
            d["cost"] += m.get("cost") or 0
            d["sessions"] += 1
    task_rows = commands_rows(conn)
    since = time.time() - days * 86400 if days else 0
    recent_tasks = [t for t in task_rows if (t["ended_at"] or time.time()) >= since]
    t = ov["totals"]
    data = {
        "period_days": days, "exported_at": time.time(),
        "totals": t, "not_tracked": ov["not_tracked"],
        "by_agent": ov["by_agent"],
        "by_project": [dict(project=k, **v) for k, v in sorted(by_project.items(), key=lambda kv: -kv[1]["total"])],
        "by_model": [dict(model=k, **v) for k, v in sorted(models.items(), key=lambda kv: -kv[1]["total"])],
        "tasks": {"count": len(recent_tasks), "active": sum(1 for x in recent_tasks if x["status"] == "active"),
                  "total": sum(x["total"] for x in recent_tasks), "cost": round(sum(x["cost"] or 0 for x in recent_tasks), 6),
                  "active_s": sum(x["active_s"] or 0 for x in recent_tasks)},
    }
    if f == "json":
        return _json(data), None
    if f == "csv":
        rows = [["total", "all", t["sessions"], t["total"], round(t["cost"], 4)]]
        rows += [["agent", a["agent"], a["sessions"], a["total"], round(a["cost"], 4)] for a in ov["by_agent"]]
        rows += [["project", p["project"], p["sessions"], p["total"], round(p["cost"], 4)] for p in data["by_project"]]
        rows += [["model", m["model"], m["sessions"], m["total"], round(m["cost"], 4)] for m in data["by_model"]]
        return _csv(["group", "name", "sessions", "tokens", "cost_usd"], rows), None
    span = ("last %g days" % days) if days else "all time"
    tk = data["tasks"]
    rest = ov["not_tracked"]
    out = ["# Tally summary · %s" % span, "", "Exported %s. Cost is an estimate at API list price." % _when(time.time()), "",
           "| Item | Value |", "|---|---|",
           "| Sessions | %s |" % _num(t["sessions"]),
           "| Tokens | %s (%s output) |" % (fmt.tokens(t["total"]), fmt.tokens(t["output"])),
           "| Cost | %s |" % fmt.money(t),
           "| Prompts | %s · %s API calls |" % (_num(t["prompts"]), _num(t["calls"])),
           "| Active time | %s (%s) |" % (fmt.duration(t["active_s"]), hm(t["active_s"])),
           "| Tasks | %s · %s tokens · %s · active %s |" % (_num(tk["count"]), fmt.tokens(tk["total"]),
                                                        fmt.money({"cost": tk["cost"]}), hm(tk["active_s"])),
           *(["| Not tracked | %s sessions in %s projects, not counted above |" % (
               _num(rest["sessions"]), _num(rest["projects"]))] if rest["sessions"] else []),
           "", "## By agent", "",
           _md_table(["Agent", "Sessions", "Tokens", "Cost"], [
               [fmt.AGENT_NAMES.get(a["agent"], a["agent"]), _num(a["sessions"]), fmt.tokens(a["total"]), fmt.money(a)]
               for a in ov["by_agent"]]),
           "", "## By project", "",
           _md_table(["Project", "Sessions", "Tokens", "Cost"], [
               [p["project"], p["sessions"], fmt.tokens(p["total"]), fmt.money(p)] for p in data["by_project"]]),
           "", "## By model", "",
           _md_table(["Model", "Sessions", "Tokens", "Cost"], [
               [m["model"], m["sessions"], fmt.tokens(m["total"]), fmt.money(m)] for m in data["by_model"]]), ""]
    return "\n".join(out), None


def commands_rows(conn) -> list:
    from . import commands
    return commands.list_rows(conn, 0)
