"""Command line interface: `tally <command>`."""

import argparse
import json
import os
import sys
import time

from . import __version__, commands, db, fmt, paths, report, tasks


def _out(text: str) -> None:
    """Print a Tally message, coloured on a terminal."""
    print(fmt.paint(text) if fmt.use_color(sys.stdout) else text)


def _die(text: str, *hints: str):
    msg = fmt.error(text, *hints)
    sys.exit(fmt.paint(msg) if fmt.use_color(sys.stderr) else msg)


def _session(args) -> str:
    sid = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        _die("No session id", "pass --session <id>; inside Claude Code it is $CLAUDE_CODE_SESSION_ID")
    return sid


def _task_or_active(conn, args):
    if getattr(args, "id", None):
        t = tasks.get(conn, args.id)
        if t is None:
            _die("Task #%s not found" % args.id, "tally list shows your tasks")
        return t
    sid = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    t = tasks.active_for_session(conn, sid) if sid else None
    if t is None:
        act = tasks.active_tasks(conn)
        if len(act) == 1:
            return act[0]
        if not act:
            _die("No active task", "pass a task id; tally list shows your tasks")
        _die("Several active tasks (%s)" % ", ".join("#%d" % a["id"] for a in act), "pass a task id")
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
        _out(res.text)


def cmd_start(args):
    conn = db.connect()
    name = " ".join(args.name).strip()
    res = commands.run(conn, _session(args), "start", name, cwd=os.getcwd())
    _out(res.text)


def cmd_end(args):
    conn = db.connect()
    t = _task_or_active(conn, args)
    if t["status"] != "active":
        _out("\n".join([fmt.render_report(report.summary_for(conn, t)), "",
                        fmt.hints("already ended; tally resume %d --session <id> reopens it" % t["id"])]))
        return
    tasks.end(conn, t["id"], note=args.note)
    _out(commands._ended_text(conn, tasks.get(conn, t["id"])))


def cmd_status(args):
    conn = db.connect()
    t = _task_or_active(conn, args)
    rep = report.summary_for(conn, t)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _out(fmt.render_report(rep, detail=not args.brief))


def cmd_show(args):
    conn = db.connect()
    t = tasks.get(conn, args.id)
    if t is None:
        _die("Task #%s not found" % args.id, "tally list shows your tasks")
    rep = report.summary_for(conn, t)
    if args.json:
        rep["prompts"] = [dict(r) for r in tasks.prompts(conn, t["id"])]
        print(json.dumps(rep, indent=2))
    else:
        lines = [fmt.render_report(rep)]
        if args.prompts:
            lines.append("\n  prompts")
            for r in tasks.prompts(conn, t["id"]):
                lines.append("    %s  %s" % (fmt.when(r["ts"], "%m-%d %H:%M"), fmt._cut(r["prompt"] or "", 100)))
        _out("\n".join(lines))


def cmd_list(args):
    conn = db.connect()
    rows = commands.list_rows(conn, 0 if args.all else args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _out(fmt.render_list(rows))


def cmd_resume(args):
    conn = db.connect()
    res = commands.run(conn, _session(args), "resume", str(args.id))
    _out(res.text)


def cmd_rename(args):
    conn = db.connect()
    if tasks.get(conn, args.id) is None:
        _die("Task #%s not found" % args.id, "tally list shows your tasks")
    t = tasks.rename(conn, args.id, " ".join(args.name))
    _out(fmt.head("Task #%d renamed: %s" % (t["id"], t["name"]), "✓"))


def cmd_delete(args):
    conn = db.connect()
    t = tasks.get(conn, args.id)
    if t is None:
        _die("Task #%s not found" % args.id, "tally list shows your tasks")
    if not args.yes:
        ans = input("Delete task #%d '%s'? [y/N] " % (t["id"], t["name"]))
        if ans.strip().lower() not in ("y", "yes"):
            return
    tasks.delete(conn, args.id)
    _out(fmt.head("Task #%d deleted: %s" % (t["id"], t["name"]), "✓"))


def cmd_export(args):
    """tally export [scope] [ref] [--format] [-o file | --copy]: see tally/export.py for the scopes."""
    from . import export
    f = args.format or export.FORMATS[args.scope][0]
    try:
        content, name = export.export(args.scope, f, ref=args.ref, project=args.project,
                                      days=None if args.all else args.days, agent=args.agent)
    except export.ExportError as e:
        _die(str(e), "tally export -h shows the scopes and formats")
    if args.copy:
        _copy(content)
        _out(fmt.head("Copied %s (%s) to the clipboard" % (args.scope + (" " + args.ref if args.ref else ""), f), "✓"))
    elif args.output:
        with open(args.output, "w", newline="") as fh:
            fh.write(content)
        _out(fmt.head("Exported to %s" % args.output, "✓"))
    else:
        sys.stdout.write(content)


def _copy(text: str) -> None:
    import shutil
    import subprocess
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
                ["clip"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text, text=True, check=True)
            return
    _die("No clipboard tool found (pbcopy, wl-copy, xclip, xsel)", "print it instead, or write it with -o FILE")


def cmd_session(args):
    """Report of one whole session (no task needed), any agent: id or transcript path.

    Read into the sessions index (sessions.db): the main database only holds
    what tasks counted (history before a task start is skipped there on purpose).
    """
    from . import sessions
    ref = args.session_id
    path = ref if ref.endswith(".jsonl") or os.sep in ref else None
    found = sessions.index_session(ref, path)
    if found is None:
        _die("Session %s not found" % ref, "Claude Code, Codex and pi sessions are searched; tally sessions lists them")
    rep = sessions.detail(found["session_id"])
    if rep is None or rep.get("empty"):
        _die("Session %s has no API calls" % found["session_id"])
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _out(fmt.render_report(rep))


def cmd_sessions(args):
    """All sessions across agents with their tokens, cost, models and time."""
    from . import sessions
    days = None if args.all else args.days
    if not args.no_refresh:
        tty = sys.stderr.isatty()

        def progress(done, total):
            if tty and total > 50:
                sys.stderr.write("\r  indexing sessions %d/%d" % (done, total))
                sys.stderr.flush()
        sessions.refresh(days, [args.agent] if args.agent else None, on_progress=progress)
        if tty:
            sys.stderr.write("\r\033[K")
    ov = sessions.overview(days, agent=args.agent, project=args.project)
    if args.json:
        ov["sessions"] = ov["sessions"][: args.limit] if args.limit else ov["sessions"]
        print(json.dumps(ov, indent=2))
    else:
        _out(fmt.render_sessions(ov, limit=args.limit))


def cmd_dashboard(args):
    from . import server
    cfg = paths.load_config()
    host = args.host or cfg.get("dashboard_host", "127.0.0.1")
    port = args.port or int(cfg.get("dashboard_port", 8765))
    if args.background:
        url, started = server.ensure_running(open_browser=args.open)
        _out(fmt.head("Dashboard %s at %s" % ("started" if started else "already running", url), "✓"))
        return
    if server.stop_outdated(host, port):
        _out(fmt.head("Stopped the dashboard of an older Tally version", "✓"))
    elif server.is_running(host, port):
        url = "http://%s:%d/" % (host, port)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        _out(fmt.head("Dashboard already running at %s" % url, "✓"))
        return
    server.serve(host, port, open_browser=args.open)


def cmd_stats(args):
    """Default task: the active one of this session, else the only active one, else the latest."""
    conn = db.connect()
    if args.id:
        t = tasks.get(conn, args.id)
        if t is None:
            _die("Task #%s not found" % args.id, "tally list shows your tasks")
    else:
        active = tasks.active_tasks(conn)
        t = _task_or_active_soft(conn, args) or (active[0] if len(active) == 1 else None) or _latest(conn)
        if t is None:
            _die("No tasks yet", "tally start <name> --session <id> starts one")
    rep = report.summary_for(conn, t)
    if args.json:
        print(json.dumps({k: rep.get(k) for k in ("task", "time", "totals", "stats", "prompts")}, indent=2))
    else:
        _out(fmt.render_stats(rep))


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
            if args.json:
                print(json.dumps(_todo_json(conn, t)))
            else:
                _out(fmt.head("Todo #%d added to %s: %s" % (t["id"], t["project"] or "(no project)", t["title"]), "＋"))
        elif a in ("list", "ls"):
            st = {"open": todos.OPEN, "done": ("done", "dropped"), "all": None}.get(args.status, (args.status,))
            project = None if args.all else (args.project or here)
            rows = todos.list_todos(conn, project=project, statuses=st)
            if args.json:
                print(json.dumps([_todo_json(conn, r, with_children=args.tree) for r in rows], indent=2))
            else:
                _out(fmt.render_todos(conn, rows, project, show_project=project is None, tree=args.tree))
        elif a == "next":
            r = todos.next_todo(conn, project=None if args.all else (args.project or here))
            if args.json:
                print(json.dumps(_todo_json(conn, r)) if r is not None else "{}")
            elif r is None:
                _out(fmt.head("Nothing open", "📋"))
            else:
                _out(fmt.head("Next: " + todos.line_for(r, show_project=True), "📋"))
        elif a == "show":
            t = todos.get(conn, args.id)
            if t is None:
                _die("Todo #%s not found" % args.id, "tally todo list shows them")
            d = _todo_json(conn, t, with_children=True)
            if args.json:
                print(json.dumps(d, indent=2))
            else:
                lines = [fmt.head(todos.line_for(t, show_project=True), "📋")]
                if t["notes"]:
                    lines.append("  notes  " + t["notes"])
                w = d["work"]
                if w["tasks"]:
                    lines.append("  work   %d task%s · %s tokens · %s · %s active" % (
                        w["tasks"], "" if w["tasks"] == 1 else "s", fmt.tokens(w["total"]),
                        fmt.money({"cost": w["cost"]}), fmt.duration(w["active_s"])))
                for c in todos.children(conn, t["id"]):
                    lines.append("    " + todos.line_for(c))
                _out("\n".join(lines))
        elif a == "take":
            todo, task, _ = todos.take(conn, args.id, _session(args), cwd=os.getcwd())
            _out(fmt.head("Todo #%d taken: %s · task #%d measures it" % (todo["id"], todo["title"], task["id"]), "▶"))
        elif a == "done":
            t = todos.set_status(conn, args.id, "done", note=args.note)
            for task in conn.execute("SELECT * FROM tasks WHERE todo_id = ? AND status = 'active'",
                                     (args.id,)).fetchall():
                tasks.end(conn, task["id"], note=args.note or "todo done")
            _out(fmt.head("Todo #%d done: %s" % (t["id"], t["title"]), "✓"))
        elif a == "mark":
            t = todos.set_status(conn, args.id, args.status, note=args.note)
            _out(fmt.head("Todo #%d is now %s: %s" % (t["id"], t["status"], t["title"]),
                          fmt.STATUS_MARKS.get(t["status"], "")))
        elif a == "edit":
            fields = {}
            for k in ("title", "notes", "project", "priority"):
                if getattr(args, k) is not None:
                    fields[k] = getattr(args, k)
            if args.due is not None:
                fields["due_at"] = todos.parse_due(args.due) if args.due else None
            t = todos.update(conn, args.id, **fields)
            _out(fmt.head("Todo updated: " + todos.line_for(t, show_project=True), "✓"))
        elif a == "rm":
            t = todos.get(conn, args.id)
            if t is None:
                _die("Todo #%s not found" % args.id, "tally todo list shows them")
            if not args.yes and input("Delete todo #%d '%s'? [y/N] " % (t["id"], t["title"])).lower() not in ("y", "yes"):
                return
            todos.delete(conn, args.id)
            _out(fmt.head("Todo #%d deleted: %s" % (t["id"], t["title"]), "✓"))
        elif a == "import":
            r = todos.import_plan(conn, args.file, project=args.project, source=args.source)
            if args.json:
                print(json.dumps(r))
            else:
                _out(fmt.head("Plan #%d %s (%s): %d created, %d updated · %d/%d done" % (
                    r["plan_id"], r["title"], r["project"], r["created"], r["updated"],
                    r["progress_done"], r["progress_total"]), "✓"))
    except ValueError as e:
        _die(str(e))


def cmd_settings(args):
    from . import settings
    if args.key and args.value is not None:
        try:
            _out(fmt.head("%s = %s" % (args.key, settings.show(settings.set_value(args.key, args.value))), "✓"))
        except ValueError as e:
            _die(str(e), "tally settings lists them")
        return
    _out(commands._settings_command(args.key or "").replace("/tally:settings <key> <value>",
                                                            "tally settings <key> <value>"))


def cmd_project(args):
    from . import commands, projects
    conn = db.connect()
    if args.cmd == "projects":
        if args.json:
            print(json.dumps([projects.stats(conn, n, series_days=1) for n in projects.all_names(conn)], indent=2))
        else:
            _out(commands._project_command(conn, "projects", "", os.getcwd()))
        return
    name = args.name or projects.detect(os.getcwd())
    if not name:
        _die("No project here", "run it inside a git repository, or name one: tally project <name>")
    if args.set:
        with db.transaction(conn):
            row = projects.set_mode(conn, name, args.set)
        _out(fmt.head("%s: %s (%s)" % (name, row["mode"], projects.MODE_HELP[row["mode"]]), "✓"))
        return
    st = projects.stats(conn, name, days=args.days, series_days=args.series)
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        _out(fmt.render_project(st))


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
            print("%s · new project: /tally:project track|tasks|off" % fmt.BRAND + (" · " + todo_part if todo_part else ""))
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


def cmd_setup(args):
    from . import onboarding
    if not sys.stdin.isatty():
        _die("tally setup asks questions and needs a terminal", "tally settings <key> <value> sets one without")
    return 0 if onboarding.run(True, color=fmt.use_color(sys.stdout)) else 1


OVERVIEW = (
    ("tally status", "live totals of the active task"),
    ("tally list", "recent tasks"),
    ("tally sessions", "every session of every agent"),
    ("tally todo list", "todos of this project"),
    ("tally dashboard --open", "the local dashboard"),
    ("tally setup", "the onboarding questions again"),
    ("tally doctor", "check the installation"),
)


def overview() -> str:
    """`tally` without a command: the banner and the commands used most."""
    color = fmt.use_color(sys.stdout)
    dim = (lambda s: "\033[2m%s\033[0m" % s) if color else (lambda s: s)
    lines = [fmt.banner(__version__, color, not fmt.can_draw(sys.stdout))]
    lines += ["  %-24s %s" % (cmd, dim(what)) for cmd, what in OVERVIEW]
    lines += ["", fmt.hints("tally --help all commands", "tally <command> -h its options")]
    text = "\n".join(lines)
    return fmt.paint(text) if color else text


class Parser(argparse.ArgumentParser):
    """The root parser shows the banner above its help on a terminal, like `tally` alone."""

    def print_help(self, file=None):
        if file is None and self.prog == "tally" and sys.stdout.isatty():
            print(fmt.banner(__version__, fmt.use_color(sys.stdout), not fmt.can_draw(sys.stdout)))
        super().print_help(file)


def cmd_doctor(args):
    _out("%s %s · doctor · python %s" % (fmt.BRAND, __version__, sys.version.split()[0]))
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
    from . import onboarding
    setup = db.kv_get(conn, "setup")
    print("setup         %s" % ("done %s" % fmt.when(json.loads(setup)["ts"]) if setup
                                else "not run yet (tally setup)"))
    state, _ = onboarding.statusline_state()
    print("status line   %s" % {"ours": "Tally", "other": "another command (tally statusline can be added to it)",
                                "absent": "none (tally setup can add it)",
                                "invalid": "settings.json is not valid JSON"}[state])
    if paths.error_log_path().exists() and paths.error_log_path().stat().st_size:
        print("errors        see %s" % paths.error_log_path())
    else:
        print("errors        none")


def build_parser():
    p = Parser(prog="tally", description="%s · per-task token and time tracking for Claude Code, "
                                                          "Codex and pi." % fmt.BRAND)
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

    s = sub.add_parser("export", help="export a summary, tasks, a task, projects, a project, sessions or a session",
                       description="Scopes and formats (the first is the default): summary md|json|csv · "
                                   "tasks csv|json|md|text · task <id> md|text|json|csv · projects csv|json|md · "
                                   "project <name> md|json|csv · sessions csv|json|md · session <id> md|text|json")
    s.add_argument("scope", nargs="?", default="tasks",
                   choices=["summary", "tasks", "task", "projects", "project", "sessions", "session"])
    s.add_argument("ref", nargs="?", help="task id, project name or session id (prefix)")
    s.add_argument("-f", "--format", choices=["csv", "json", "md", "text"])
    s.add_argument("-o", "--output", help="write to a file")
    s.add_argument("--copy", action="store_true", help="copy to the clipboard")
    s.add_argument("-p", "--project", help="tasks or sessions of this project")
    s.add_argument("--days", type=float, default=30, help="sessions and summary: the last N days (default 30)")
    s.add_argument("--all", action="store_true", help="sessions and summary: all history")
    s.add_argument("--agent", choices=["claude", "codex", "pi"], help="sessions of this agent")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("session", help="report of one whole session (id or transcript path), any agent")
    s.add_argument("session_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_session)

    s = sub.add_parser("sessions", help="all sessions across agents: tokens, cost, models, time")
    s.add_argument("--days", type=float, default=30, help="sessions active in the last N days (default 30)")
    s.add_argument("--all", action="store_true", help="all history")
    s.add_argument("--agent", choices=["claude", "codex", "pi"])
    s.add_argument("-p", "--project")
    s.add_argument("--limit", type=int, default=40, help="rows to show (0 = all)")
    s.add_argument("--no-refresh", action="store_true", help="show the index as is, without reading transcripts")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

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

    s = sub.add_parser("setup", help="the onboarding questions: prompt storage, logging, new projects, status line")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("doctor", help="check installation")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        print(overview())
        return 0
    rc = args.func(args)
    return rc or 0
