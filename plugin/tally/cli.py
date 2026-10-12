"""Command line interface: `tally <command>`."""

import argparse
import contextlib
import csv
import json
import os
import sys
import time

from . import __version__, commands, db, fmt, ingest, paths, report, tasks


def _session(args) -> str:
    sid = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        sys.exit("No session id: pass --session <id> (inside Claude Code it is taken from "
                 "$CLAUDE_CODE_SESSION_ID).")
    return sid


def _task_or_active(conn, args):
    if getattr(args, "id", None):
        t = tasks.get(conn, args.id)
        if t is None:
            sys.exit("Task #%s not found." % args.id)
        return t
    sid = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    t = tasks.active_for_session(conn, sid) if sid else None
    if t is None:
        act = tasks.active_tasks(conn)
        if len(act) == 1:
            return act[0]
        if not act:
            sys.exit("No active task. Pass a task id.")
        sys.exit("Several active tasks (%s). Pass a task id." % ", ".join("#%d" % a["id"] for a in act))
    return t


def cmd_hook(args):
    from . import hooks
    return hooks.main(args.event, agent=args.agent, payload_text=args.payload)


def cmd_slash(args):
    """Run a /tally:<cmd> exactly as the hook would (used by command fallbacks)."""
    conn = db.connect()
    text = " ".join(args.args).strip()
    if text.startswith("--"):
        text = text[2:].strip()
    sid = _session(args)
    cwd = args.cwd or os.getcwd()
    if args.agent or args.transcript:
        from . import projects
        with db.transaction(conn):
            tasks.touch_session(conn, sid, args.transcript, cwd, None, project=projects.detect(cwd),
                                agent=args.agent)
    res = commands.run(conn, sid, args.command, text, cwd=cwd, transcript_path=args.transcript,
                       prompt_id=args.prompt_id)
    if args.json:
        print(json.dumps({"text": res.text, "block": res.block, "context": res.context}))
    elif res.text:
        print(res.text)


def cmd_start(args):
    conn = db.connect()
    name = " ".join(args.name).strip()
    res = commands.run(conn, _session(args), "start", name, cwd=os.getcwd())
    print(res.text)


def cmd_end(args):
    conn = db.connect()
    t = _task_or_active(conn, args)
    if t["status"] != "active":
        print("Task #%d already ended." % t["id"])
    else:
        tasks.end(conn, t["id"], note=args.note)
    print(fmt.render_report(report.summary_for(conn, tasks.get(conn, t["id"]))))


def cmd_status(args):
    conn = db.connect()
    t = _task_or_active(conn, args)
    rep = report.summary_for(conn, t)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(fmt.render_report(rep, detail=not args.brief))


def cmd_show(args):
    conn = db.connect()
    t = tasks.get(conn, args.id)
    if t is None:
        sys.exit("Task #%s not found." % args.id)
    rep = report.summary_for(conn, t)
    if args.json:
        rep["prompts"] = [dict(r) for r in tasks.prompts(conn, t["id"])]
        print(json.dumps(rep, indent=2))
    else:
        print(fmt.render_report(rep))
        if args.prompts:
            print("\n  prompts")
            for r in tasks.prompts(conn, t["id"]):
                print("    %s  %s" % (fmt.when(r["ts"], "%m-%d %H:%M"), fmt._cut(r["prompt"] or "", 100)))


def cmd_list(args):
    conn = db.connect()
    rows = commands.list_rows(conn, 0 if args.all else args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(fmt.render_list(rows))


def cmd_resume(args):
    conn = db.connect()
    res = commands.run(conn, _session(args), "resume", str(args.id))
    print(res.text)


def cmd_rename(args):
    conn = db.connect()
    t = tasks.rename(conn, args.id, " ".join(args.name))
    print("Task #%d renamed to: %s" % (t["id"], t["name"]))


def cmd_delete(args):
    conn = db.connect()
    if not args.yes:
        t = tasks.get(conn, args.id)
        if t is None:
            sys.exit("Task #%s not found." % args.id)
        ans = input("Delete task #%d '%s'? [y/N] " % (t["id"], t["name"]))
        if ans.strip().lower() not in ("y", "yes"):
            return
    print("deleted" if tasks.delete(conn, args.id) else "not found")


def cmd_export(args):
    conn = db.connect()
    rows = []
    for t in tasks.list_tasks(conn, limit=0):
        rep = report.summary_for(conn, t)
        rows.append(rep)
    with contextlib.ExitStack() as stack:
        out = stack.enter_context(open(args.output, "w", newline="")) if args.output else sys.stdout
        if args.format == "json":
            json.dump(rows, out, indent=2)
            out.write("\n")
        else:
            w = csv.writer(out)
            w.writerow(["id", "name", "status", "started", "ended", "wall_s", "active_s", "total_tokens",
                        "input", "output", "cache_write", "cache_read", "api_calls", "cost_usd",
                        "subagents", "workflows", "initial_prompt"])
            for rep in rows:
                t, tm, tot = rep["task"], rep["time"], rep["totals"]
                n_agents = sum(x["agents"] for x in rep["by_type"] if x["agent_type"] != "main")
                w.writerow([t["id"], t["name"], t["status"], fmt.when(t["started_at"], "%Y-%m-%dT%H:%M:%S"),
                            fmt.when(t["ended_at"], "%Y-%m-%dT%H:%M:%S") if t["ended_at"] else "",
                            tm["wall_s"], tm["active_s"], tot["total"], tot["input"], tot["output"],
                            tot["cache_write"], tot["cache_read"], tot["calls"], round(tot["cost"], 4),
                            n_agents, len(rep["workflows"]), t["initial_prompt"] or ""])


def cmd_session(args):
    """Analyse a whole session (no task needed): ingest everything and report.

    Uses a throw-away database: the main one only holds what tasks counted
    (history before a task start is skipped there on purpose).
    """
    import shutil
    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="tally-session-")
    try:
        _session_report(args, db.connect(Path(tmp) / "session.db"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _session_report(args, conn):
    sid = args.session_id
    if sid.endswith(".jsonl"):
        path = sid
        sid = os.path.basename(sid)[:-6]
    else:
        path = None
    st = ingest.ingest_session(conn, sid, path)
    main = ingest.resolve_transcript(conn, sid, path)
    if main is None:
        sys.exit("Transcript for session %s not found under %s" % (sid, paths.projects_dir()))
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM usage WHERE session_id = ?", (sid,)).fetchone()
    t0 = conn.execute("SELECT MIN(first_ts) FROM agents WHERE session_id = ?", (sid,)).fetchone()[0]
    start = min(x for x in (span[0], t0) if x) if (span[0] or t0) else time.time()
    end = span[1] or start
    whole = {"id": 0, "name": "session " + sid, "initial_prompt": None, "status": "ended",
             "started_at": start - 1, "ended_at": end + 1, "cwd": None, "created_by": "session",
             "end_note": None}
    from . import reconcile
    reconcile.refresh(conn, sid)
    rep = report.build(conn, whole, windows=[(sid, start - 1, end + 1)])
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(fmt.render_report(rep))
        print("\n  ingested %s" % st.as_dict())


def cmd_dashboard(args):
    from . import server
    cfg = paths.load_config()
    host = args.host or cfg.get("dashboard_host", "127.0.0.1")
    port = args.port or int(cfg.get("dashboard_port", 8765))
    if args.background:
        url, started = server.ensure_running(open_browser=args.open)
        print("Dashboard %s at %s" % ("started" if started else "already running", url))
        return
    server.serve(host, port, open_browser=args.open)


def cmd_stats(args):
    """Default task: the active one of this session, else the only active one, else the latest."""
    conn = db.connect()
    if args.id:
        t = tasks.get(conn, args.id)
        if t is None:
            sys.exit("Task #%s not found." % args.id)
    else:
        active = tasks.active_tasks(conn)
        t = _task_or_active_soft(conn, args) or (active[0] if len(active) == 1 else None) or _latest(conn)
        if t is None:
            sys.exit("No tasks yet.")
    rep = report.summary_for(conn, t)
    print(json.dumps({k: rep.get(k) for k in ("task", "time", "totals", "stats", "prompts")}, indent=2)
          if args.json else fmt.render_stats(rep))


def _latest(conn):
    rows = tasks.list_tasks(conn, limit=1)
    return rows[0] if rows else None


def _task_or_active_soft(conn, args):
    sid = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    return tasks.active_for_session(conn, sid) if sid else None


def _todo_json(conn, r, with_children=False) -> dict:
    from . import todos
    d = dict(r)
    d["progress"] = list(todos.progress(conn, r["id"]))
    if with_children:
        d["children"] = [_todo_json(conn, c, True) for c in todos.children(conn, r["id"])]
        d["work"] = todos.linked_stats(conn, r["id"])
    return d


def cmd_todo(args):
    from . import projects, todos
    conn = db.connect()
    here = projects.detect(os.getcwd())
    a = args.todo_cmd
    try:
        if a == "add":
            spec = todos.parse_quick(" ".join(args.text), args.project or here)
            if args.priority is not None:
                spec["priority"] = args.priority
            if args.due:
                spec["due_at"] = todos.parse_due(args.due)
            if args.notes:
                spec["notes"] = args.notes
            t = todos.add(conn, spec["title"], project=spec["project"], priority=spec["priority"],
                          due_at=spec["due_at"], notes=spec["notes"], parent_id=args.parent, source=args.source)
            print(json.dumps(_todo_json(conn, t)) if args.json else "#%d %s (%s)" % (t["id"], t["title"], t["project"]))
        elif a in ("list", "ls"):
            st = {"open": todos.OPEN, "done": ("done", "dropped"), "all": None}.get(args.status, (args.status,))
            project = None if args.all else (args.project or here)
            rows = todos.list_todos(conn, project=project, statuses=st)
            if args.json:
                print(json.dumps([_todo_json(conn, r, with_children=args.tree) for r in rows], indent=2))
            else:
                print(fmt.render_todos(conn, rows, project, show_project=project is None, tree=args.tree))
        elif a == "next":
            r = todos.next_todo(conn, project=None if args.all else (args.project or here))
            if r is None:
                print("{}" if args.json else "Nothing open.")
            else:
                print(json.dumps(_todo_json(conn, r)) if args.json else todos.line_for(r, show_project=True))
        elif a == "show":
            t = todos.get(conn, args.id)
            if t is None:
                sys.exit("Todo #%s not found." % args.id)
            d = _todo_json(conn, t, with_children=True)
            if args.json:
                print(json.dumps(d, indent=2))
            else:
                print(todos.line_for(t, show_project=True))
                if t["notes"]:
                    print("  notes  " + t["notes"])
                w = d["work"]
                if w["tasks"]:
                    print("  work   %d task%s · %s tokens · %s · %s active" % (
                        w["tasks"], "" if w["tasks"] == 1 else "s", fmt.tokens(w["total"]),
                        fmt.money({"cost": w["cost"]}), fmt.duration(w["active_s"])))
                for c in todos.children(conn, t["id"]):
                    print("    " + todos.line_for(c))
        elif a == "take":
            todo, task, _ = todos.take(conn, args.id, _session(args), cwd=os.getcwd())
            print("Todo #%d taken; task #%d measures it." % (todo["id"], task["id"]))
        elif a == "done":
            t = todos.set_status(conn, args.id, "done", note=args.note)
            for task in conn.execute("SELECT * FROM tasks WHERE todo_id = ? AND status = 'active'",
                                     (args.id,)).fetchall():
                tasks.end(conn, task["id"], note=args.note or "todo done")
            print("✓ #%d %s" % (t["id"], t["title"]))
        elif a == "mark":
            t = todos.set_status(conn, args.id, args.status, note=args.note)
            print("#%d is now %s" % (t["id"], t["status"]))
        elif a == "edit":
            fields = {}
            for k in ("title", "notes", "project", "priority"):
                if getattr(args, k) is not None:
                    fields[k] = getattr(args, k)
            if args.due is not None:
                fields["due_at"] = todos.parse_due(args.due) if args.due else None
            t = todos.update(conn, args.id, **fields)
            print(todos.line_for(t, show_project=True))
        elif a == "rm":
            t = todos.get(conn, args.id)
            if t is None:
                sys.exit("Todo #%s not found." % args.id)
            if not args.yes and input("Delete todo #%d '%s'? [y/N] " % (t["id"], t["title"])).lower() not in ("y", "yes"):
                return
            todos.delete(conn, args.id)
            print("deleted")
        elif a == "import":
            r = todos.import_plan(conn, args.file, project=args.project, source=args.source)
            if args.json:
                print(json.dumps(r))
            else:
                print("Plan #%d %s (%s): %d created, %d updated · %d/%d done" % (
                    r["plan_id"], r["title"], r["project"], r["created"], r["updated"],
                    r["progress_done"], r["progress_total"]))
    except ValueError as e:
        sys.exit(str(e))


def cmd_settings(args):
    from . import settings
    if args.key and args.value is not None:
        try:
            print("%s = %s" % (args.key, json.dumps(settings.set_value(args.key, args.value))))
        except ValueError as e:
            sys.exit(str(e))
        return
    for key, value, desc, allowed in settings.describe():
        if args.key and key != args.key:
            continue
        print("%-22s %-10s [%s]\n%-22s %s" % (key, json.dumps(value), allowed, "", desc))


def cmd_project(args):
    from . import commands, projects
    conn = db.connect()
    if args.cmd == "projects":
        if args.json:
            print(json.dumps([projects.stats(conn, n, series_days=1) for n in projects.all_names(conn)], indent=2))
        else:
            print(commands._project_command(conn, "projects", "", os.getcwd()))
        return
    name = args.name or projects.detect(os.getcwd())
    if args.set:
        with db.transaction(conn):
            row = projects.set_mode(conn, name, args.set)
        print("%s: %s (%s)" % (name, row["mode"], projects.MODE_HELP[row["mode"]]))
        return
    st = projects.stats(conn, name, days=args.days, series_days=args.series)
    print(json.dumps(st, indent=2) if args.json else fmt.render_project(st))


def cmd_statusline(args):
    """One-line segment for a status line (stdin: Claude Code statusLine JSON).

    Active task: "▶ #3 name · 4.21M tok · $3.12 · 44m 10s" (+ "· 📋 2" open todos).
    No task: "📋 3 todos · 1 overdue" for the project, or nothing. Never fails loudly.
    """
    try:
        from . import projects, todos
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        sid = payload.get("session_id") or args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
        cwd = payload.get("cwd") or (payload.get("workspace") or {}).get("current_dir") or os.getcwd()
        conn = db.connect()
        project = projects.detect(cwd)
        open_rows = todos.list_todos(conn, project=project, statuses=todos.OPEN) if project else []
        now = time.time()
        overdue = sum(1 for r in open_rows if r["due_at"] and r["due_at"] < now)
        todo_part = ("📋 %d%s" % (len(open_rows), (" · %d overdue" % overdue) if overdue else "")) if open_rows else ""
        task = tasks.active_for_session(conn, sid) if sid else None
        if task is None and project and projects.mode_of(conn, project) == "ask":
            print("Tally: new project · /tally:project track|tasks|off" + (" · " + todo_part if todo_part else ""))
            return
        if task is None:
            if todo_part:
                print(todo_part.replace("📋 %d" % len(open_rows),
                                        "📋 %d todo%s" % (len(open_rows), "" if len(open_rows) == 1 else "s"), 1))
            return
        rep = report.summary(conn, task)
        tot = rep["totals"]
        name = task["name"] if len(task["name"]) <= args.width else task["name"][: args.width - 1] + "…"
        line = "▶ #%d %s · %s tok · %s · %s" % (
            task["id"], name, fmt.tokens(tot["total"]), fmt.money(tot), fmt.duration(rep["time"]["wall_s"]))
        print(line + (" · " + todo_part if todo_part else ""))
    except Exception:
        pass


def cmd_config(args):
    cfg = paths.load_config()
    if args.key is None:
        print(json.dumps(cfg, indent=2))
        print("\nfile: %s" % paths.config_path())
        return
    if args.value is None:
        print(json.dumps(cfg.get(args.key)))
        return
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    cfg[args.key] = value
    paths.save_config(cfg)
    print("%s = %s" % (args.key, json.dumps(value)))


def cmd_install(args):
    from . import installer
    agents = None if not args.agents else [a.strip() for a in args.agents.split(",") if a.strip()]
    if args.all:
        agents = list(installer.AGENTS)
    return installer.install(agents, assume_yes=args.yes)


def cmd_uninstall(args):
    from . import installer
    agents = [a.strip() for a in args.agents.split(",")] if args.agents else None
    return installer.uninstall(agents)


def cmd_doctor(args):
    print("tally %s · python %s" % (__version__, sys.version.split()[0]))
    print("data dir      %s" % paths.home())
    print("database      %s" % paths.db_path())
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    a = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='active'").fetchone()[0]
    print("tasks         %d (%d active)" % (n, a))
    last = db.kv_get(conn, "last_hook")
    if last:
        d = json.loads(last)
        print("last hook     %s at %s (session %s)" % (d["event"], fmt.when(d["ts"], "%Y-%m-%d %H:%M:%S"),
                                                       d.get("session_id", "")[:8]))
    else:
        print("last hook     never - the hooks have not fired yet (restart the agent after installing)")
    agents = dict(conn.execute("SELECT COALESCE(agent, 'claude'), COUNT(*) FROM sessions GROUP BY 1").fetchall())
    if agents:
        print("sessions      %s" % " · ".join("%s %d" % (k, v) for k, v in sorted(agents.items())))
    print("transcripts   %s %s" % (paths.projects_dir(), "(ok)" if paths.projects_dir().is_dir() else "(MISSING)"))
    from . import installer
    found, inst = installer.detect(), installer.installed()
    for a in installer.AGENTS:
        state = ("installed" if inst[a] else
                 ("found, not installed (./install.sh --agents %s)" % a if found[a] else "not found"))
        print("%-13s %s" % (installer.NAMES[a], state))
    if paths.error_log_path().exists() and paths.error_log_path().stat().st_size:
        print("errors        see %s" % paths.error_log_path())
    else:
        print("errors        none")


def build_parser():
    p = argparse.ArgumentParser(prog="tally", description="Per-task token and time tracking for Claude Code.")
    p.add_argument("--version", action="version", version="tally " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("hook", help="(internal) run an agent hook; JSON payload on stdin or --payload")
    s.add_argument("event")
    s.add_argument("--agent", choices=["claude", "codex", "pi"])
    s.add_argument("--payload")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("slash", help="(internal) run a /tally:<command> for a session")
    s.add_argument("--session")
    s.add_argument("--agent", choices=["claude", "codex", "pi"])
    s.add_argument("--transcript")
    s.add_argument("--cwd")
    s.add_argument("--prompt-id")
    s.add_argument("--json", action="store_true")
    s.add_argument("command")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_slash)

    s = sub.add_parser("start", help="start a task in a session")
    s.add_argument("name", nargs="*")
    s.add_argument("--session")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("end", help="end a task (default: the active one)")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--note")
    s.add_argument("--session")
    s.set_defaults(func=cmd_end)

    s = sub.add_parser("status", help="live report of a task (default: the active one)")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--session")
    s.add_argument("--json", action="store_true")
    s.add_argument("--brief", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("show", help="report of a task")
    s.add_argument("id", type=int)
    s.add_argument("--json", action="store_true")
    s.add_argument("--prompts", action="store_true", help="also list the prompts of the task")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("list", aliases=["ls"], help="list tasks")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("resume", help="continue a task in a session (reopens ended tasks)")
    s.add_argument("id", type=int)
    s.add_argument("--session")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("rename", help="rename a task")
    s.add_argument("id", type=int)
    s.add_argument("name", nargs="+")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("delete", help="delete a task")
    s.add_argument("id", type=int)
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("export", help="export all tasks as CSV or JSON")
    s.add_argument("--format", choices=["csv", "json"], default="csv")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("session", help="analyse any session transcript (id or path), no task needed")
    s.add_argument("session_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_session)

    s = sub.add_parser("dashboard", aliases=["serve"], help="local web dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.add_argument("--background", action="store_true", help="start detached and return")
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("config", help="show or set configuration (auto_start, dashboard_port, pricing)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("stats", help="detailed stats of a task: per-prompt breakdown, tools, averages")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--session")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("todo", help="todo list: add, list, next, show, take, done, mark, edit, rm, import")
    tsub = s.add_subparsers(dest="todo_cmd")
    s.set_defaults(func=lambda a: (cmd_todo(a) if a.todo_cmd else s.print_help()))
    t = tsub.add_parser("add", help="add a todo (quick syntax: @project ! !! !!! due:fri -- notes)")
    t.add_argument("text", nargs="+", help="title; @project, ! !! !!!, due:<when> are recognised")
    t.add_argument("-p", "--project")
    t.add_argument("--priority", type=int, choices=[0, 1, 2, 3])
    t.add_argument("--due", help="today, tomorrow, fri, +3d, 2026-10-20, 20.10.")
    t.add_argument("--notes")
    t.add_argument("--parent", type=int, help="make it a step of another todo")
    t.add_argument("--source", default="user", help="user | claude | import")
    t.add_argument("--json", action="store_true")
    t = tsub.add_parser("list", aliases=["ls"], help="list todos (default: open, this project)")
    t.add_argument("-p", "--project")
    t.add_argument("--all", action="store_true", help="all projects")
    t.add_argument("--status", default="open", help="open | done | all | todo | doing | blocked | dropped")
    t.add_argument("--tree", action="store_true", help="show plan steps under their todos")
    t.add_argument("--json", action="store_true")
    t = tsub.add_parser("next", help="the next todo to work on (doing first, then by priority/due)")
    t.add_argument("-p", "--project")
    t.add_argument("--all", action="store_true")
    t.add_argument("--json", action="store_true")
    t = tsub.add_parser("show", help="a todo with its steps and measured work")
    t.add_argument("id", type=int)
    t.add_argument("--json", action="store_true")
    t = tsub.add_parser("take", help="pick up a todo in a Claude Code session (starts measuring)")
    t.add_argument("id", type=int)
    t.add_argument("--session")
    t = tsub.add_parser("done", help="mark done (ends its active task)")
    t.add_argument("id", type=int)
    t.add_argument("--note")
    t = tsub.add_parser("mark", help="set status")
    t.add_argument("id", type=int)
    t.add_argument("status", choices=["todo", "doing", "blocked", "done", "dropped"])
    t.add_argument("--note")
    t = tsub.add_parser("edit", help="change title, notes, project, priority or due date")
    t.add_argument("id", type=int)
    t.add_argument("--title")
    t.add_argument("--notes")
    t.add_argument("--project")
    t.add_argument("--priority", type=int, choices=[0, 1, 2, 3])
    t.add_argument("--due", help="new due date, or '' to clear")
    t = tsub.add_parser("rm", help="delete a todo (and its steps)")
    t.add_argument("id", type=int)
    t.add_argument("-y", "--yes", action="store_true")
    t = tsub.add_parser("import", help="create/update todos from a markdown plan (superpowers, checklists)")
    t.add_argument("file")
    t.add_argument("-p", "--project")
    t.add_argument("--source", default="import")
    t.add_argument("--json", action="store_true")

    s = sub.add_parser("project", help="overall stats of a project; --set track|tasks|off")
    s.add_argument("name", nargs="?", help="default: the project of the current directory")
    s.add_argument("--set", choices=["track", "tasks", "off"])
    s.add_argument("--days", type=int, help="only the last N days")
    s.add_argument("--series", type=int, default=14, help="days in the daily series")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_project)

    s = sub.add_parser("projects", help="all projects with mode and totals")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_project)

    s = sub.add_parser("settings", help="show or change settings (validated)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_settings)

    s = sub.add_parser("statusline", help="one-line active-task segment for a status line (reads statusLine JSON)")
    s.add_argument("--session")
    s.add_argument("--width", type=int, default=28, help="max task name length")
    s.set_defaults(func=cmd_statusline)

    s = sub.add_parser("install", help="install into agents: Claude Code, Codex, pi (asks which)")
    s.add_argument("--agents", help="comma separated: claude,codex,pi")
    s.add_argument("--all", action="store_true", help="every supported agent")
    s.add_argument("-y", "--yes", action="store_true", help="no questions: every detected agent")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("uninstall", help="remove from agents (default: wherever it is installed)")
    s.add_argument("--agents")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("doctor", help="check installation")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    rc = args.func(args)
    return rc or 0
