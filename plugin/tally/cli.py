"""Command line interface: `tally <command>`."""

import argparse
import re
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


def _stdin_text() -> str:
    """The arguments of a command fallback, from a quoted heredoc: as typed, never
    seen by the shell. Without its last newline (the heredoc adds one)."""
    stream = getattr(sys.stdin, "buffer", None)
    data = stream.read().decode("utf-8", "replace") if stream is not None else sys.stdin.read()
    return data[:-1] if data.endswith("\n") else data


def cmd_slash(args):
    """Run a /tally:<cmd> exactly as the hook would (used by command fallbacks
    with --stdin, and by pi with the arguments after the command)."""
    now = time.time()
    words = list(args.args)
    if words[:1] == ["--"]:             # the separator, if argparse left it; never the user's own "--x"
        words = words[1:]
    text = (_stdin_text() if args.stdin else " ".join(words)).strip()
    conn = db.connect()
    sid = _session(args)
    cwd = args.cwd or os.getcwd()
    if args.agent or args.transcript:
        from . import projects
        with db.transaction(conn):
            tasks.touch_session(conn, sid, args.transcript, cwd, None, project=projects.detect(cwd),
                                agent=args.agent)
    from . import hooks
    fmt.APP = not args.agent and os.environ.get("CLAUDE_CODE_ENTRYPOINT") in hooks.APP_ENTRYPOINTS
    res = commands.run_once(conn, sid, args.command, text, lambda: commands.run(   # the hook may run it too
        conn, sid, args.command, text, cwd=cwd, transcript_path=args.transcript, prompt_id=args.prompt_id),
        via="agent" if args.agent else "fallback", now=now)
    if args.json:
        print(json.dumps({"text": res.text, "block": res.block, "context": res.context}))
    elif res.text:
        _out(res.text)


def cmd_start(args):
    conn = db.connect()
    name = " ".join(args.name).strip()
    res = commands.run(conn, _session(args), "start", name, cwd=os.getcwd())
    return _result(res.text)


def _cap(msg: str) -> str:
    """Capitalise a message, but not one that starts with a setting key or a command."""
    first = msg.split(" ", 1)[0]
    return msg if any(c in first for c in "._-/") or not msg else msg[0].upper() + msg[1:]


def _result(text: str) -> int:
    """Print an answer of a /tally:* command; 1 if it is an error."""
    _out(text)
    return 1 if text.startswith(fmt.head("", "✗").rstrip()) else 0


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
    return _result(res.text.replace("/tally:list shows", "tally list shows"))


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
        _die(_cap(str(e)), "tally export -h shows the scopes and formats")
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
    if args.port is not None and not 1 <= args.port <= 65535:
        _die("--port %d: the port must be between 1 and 65535" % args.port)
    port = args.port if args.port is not None else int(cfg.get("dashboard_port", 8765))
    if args.stop:
        if server.stop(host, port):
            _out(fmt.head("Dashboard stopped (http://%s:%d/)" % (host, port), "■"))
        else:
            _out(fmt.head("No dashboard running at http://%s:%d/" % (host, port)))
        return
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
    server.serve(host, port, open_browser=args.open, idle_minutes=args.idle_minutes or 0)


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
                          due_at=spec["due_at"], notes=spec["notes"], parent_id=args.parent, source=args.source, once=True)
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
            t, _ = commands.close_todo(conn, args.id, "done", note=args.note)
            _out(fmt.head("Todo #%d done: %s" % (t["id"], t["title"]), "✓"))
        elif a == "mark":
            t, _ = commands.close_todo(conn, args.id, args.status, note=args.note)
            _out(fmt.head("Todo #%d is now %s: %s" % (t["id"], t["status"], t["title"]),
                          fmt.STATUS_MARKS.get(t["status"], "")))
        elif a == "edit":
            if todos.get(conn, args.id) is None:
                _die("Todo #%s not found" % args.id, "tally todo list shows them")
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
        _die(_cap(str(e)), "tally todo list shows them" if "not found" in str(e) else "tally todo %s -h shows the options" % a)
    except OSError as e:
        _die("Cannot read %s: %s" % (getattr(e, "filename", None) or "the file", e.strerror or e))


def cmd_settings(args):
    """tally settings [key [value]]: change one; in a terminal without arguments, the editor."""
    from . import prompt, settings
    if args.key and args.key not in settings.SCHEMA:
        _die("Unknown setting %s" % args.key, "tally settings --list shows them all")
    if args.key and args.value is not None:
        try:
            value = settings.set_value(args.key, args.value)
        except ValueError as e:
            _die(_cap(str(e)), "tally settings %s shows what it does" % args.key)
        _out(fmt.head("%s = %s" % (args.key, settings.show(value)), "✓"))
        return 0
    if not args.key and not args.list and prompt.interactive():
        changed = prompt.settings_editor(color=fmt.use_color(sys.stdout))
        _out(fmt.head("Saved: " + ", ".join("%s = %s" % (k, settings.show(v)) for k, v in changed), "✓")
             if changed else fmt.head("Settings · nothing changed"))
        return 0
    _out(fmt.render_settings([args.key] if args.key else None, cli=True, width=prompt.width()))
    return 0


def cmd_project(args):
    from . import commands, projects
    conn = db.connect()
    if args.cmd == "projects":
        if args.json:
            from . import sessions
            sessions.refresh(30)
            print(json.dumps(projects.listing(conn, with_usage=True, refresh=False), indent=2))
        else:
            _out(commands._project_command(conn, "projects", "", os.getcwd(), cli=True))
        return
    name = args.name or projects.detect(os.getcwd())
    if not name:
        _die("No project here", "run it inside a git repository, or name one: tally project <name>")
    if args.set:
        with db.transaction(conn):
            row = projects.set_mode(conn, name, args.set)
        _out(fmt.head("%s: %s (%s)" % (name, row["mode"], projects.MODE_HELP[row["mode"]]), "✓"))
        return
    st = commands.project_view(conn, name, days=args.days)
    if st is None:
        _die("Project %s not found" % name, "tally projects lists them" if args.name else
             "no agent session here yet; tally projects lists the projects Tally knows")
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        _out(fmt.render_project(st, cli=True, series=args.series))


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
        if args.key not in cfg:
            _die("Unknown key %s" % args.key, "tally config lists them; tally settings has the validated ones")
        print(json.dumps(cfg.get(args.key), indent=2))
        return
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    # the file as it is, plus this key: saving load_config() would write every default into it
    try:
        with open(paths.config_path()) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        _die("%s is not valid JSON (%s)" % (paths.config_path(), e), "fix it or remove it, then try again")
    if not isinstance(raw, dict):
        _die("%s is not valid JSON (not an object)" % paths.config_path(), "fix it or remove it, then try again")
    raw[args.key] = value
    paths.save_config(raw)
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
    return 0 if onboarding.run(True, color=fmt.use_color(sys.stdout), offer_agents=True) else 1


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
    """Help and errors in Tally's style: the root help groups the commands,
    a usage error says what is wrong and where to look."""

    def print_help(self, file=None):
        if file is None and self.prog == "tally" and sys.stdout.isatty():
            print(fmt.banner(__version__, fmt.use_color(sys.stdout), not fmt.can_draw(sys.stdout)))
        super().print_help(file)

    def format_help(self):
        if self.prog != "tally":
            return super().format_help()
        names = {}
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                for choice in action._choices_actions:
                    names[choice.dest] = (choice.help, [k for k, v in action.choices.items()
                                                        if v is action.choices[choice.dest] and k != choice.dest])
        lines = ["%s · per-task token and time tracking for Claude Code, Codex and pi" % fmt.BRAND, "",
                 "usage: tally <command> [options]     tally <command> -h shows the options of one"]
        for group, cmds in GROUPS:
            lines += ["", group]
            for c in cmds:
                summary, aliases = names.get(c, ("", []))
                label = ", ".join([c] + aliases)
                lines.append("  %-17s %s" % (label, summary))
        lines += ["", "Options", "  %-17s %s" % ("-h, --help", "this help"), "  %-17s %s" % ("--version", "the version"),
                  "", "Examples"] + ["  %-44s %s" % (cmd, what) for cmd, what in EXAMPLES]
        lines += ["", "Docs: https://github.com/janhorecny/tally/blob/main/docs/cli.md"]
        return "\n".join(lines) + "\n"

    def error(self, message):
        m = re.match(r"argument <command>: invalid choice: '([^']*)'", message)
        if m:                                       # an unknown command: say so, and offer the closest one
            import difflib
            known = [c for _, cmds in GROUPS for c in cmds] + ["ls", "serve"]
            close = difflib.get_close_matches(m.group(1), known, 1, 0.6)
            text = fmt.error("Unknown command %s" % m.group(1), ("did you mean tally %s?" % close[0]) if close else "",
                             "tally --help lists them")
            self.exit(2, (fmt.paint(text) if fmt.use_color(sys.stderr) else text) + "\n")
        hint = "%s -h shows the options" % self.prog
        usage = " ".join(self.format_usage().split())
        text = "\n".join([fmt.error(message[0].upper() + message[1:], hint), "  " + usage])
        self.exit(2, (fmt.paint(text) if fmt.use_color(sys.stderr) else text) + "\n")


def cmd_update(args):
    """Update to the latest version, or only check (--check)."""
    from . import updates
    if args.check:
        st = updates.check_now()
        if args.quiet:
            return 0
        if st.get("error") and not st.get("latest"):
            _die("Could not check for a new version: %s" % st["error"])
        if updates.newer(st.get("latest")):
            _out(fmt.head("Tally %s is out (you have %s)" % (st["latest"], __version__), "⬆") + "\n"
                 + fmt.hints("%s to update" % updates.update_command()))
        else:
            _out(fmt.head("Tally %s is the latest version" % __version__, "✓"))
        return 0
    return updates.update(say=_out)


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
                 ("found, not installed (tally install --agents %s, or tally setup)" % a if found[a] else
                  "not found"))
        print("%-13s %s" % (installer.NAMES[a], state))
    from . import onboarding
    setup = db.kv_get(conn, "setup")
    print("setup         %s" % ("done %s" % fmt.when(json.loads(setup)["ts"]) if setup
                                else "not run yet (tally setup)"))
    state, _ = onboarding.statusline_state()
    print("status line   %s" % {"ours": "Tally", "other": "another command (tally statusline can be added to it)",
                                "absent": "none (tally setup can add it)",
                                "invalid": "settings.json is not valid JSON"}[state])
    from . import updates
    st, root = updates.status(), updates.source_root()
    print("installed at  %s" % (root or installer.REPO))
    if not st["enabled"]:
        print("updates       check off (tally settings updates.check on)")
    elif st["available"]:
        print("updates       %s is out: %s" % (st["available"], st["command"]))
    elif st["checked_at"]:
        print("updates       up to date (checked %s)" % fmt.when(st["checked_at"]))
    else:
        print("updates       not checked yet (tally update --check)")
    if paths.error_log_path().exists() and paths.error_log_path().stat().st_size:
        print("errors        see %s" % paths.error_log_path())
    else:
        print("errors        none")


SESSION_HELP = "the agent's session id (default: $CLAUDE_CODE_SESSION_ID, set inside Claude Code)"
AGENTS = ["claude", "codex", "pi"]

# the root help: groups of commands, in this order (internal commands are left out)
GROUPS = (
    ("Tasks", ("status", "list", "show", "stats", "end", "start", "resume", "rename", "delete")),
    ("Todos", ("todo",)),
    ("Projects and sessions", ("projects", "project", "sessions", "session", "export", "dashboard")),
    ("Settings and maintenance", ("settings", "setup", "update", "doctor", "install", "uninstall", "statusline",
                                  "config")),
)
EXAMPLES = (
    ("tally status", "live totals of the active task"),
    ("tally sessions --days 7", "what every tracked session cost this week"),
    ('tally todo add "Fix login" @web !! due:fri', "a todo with project, priority and due date"),
    ("tally export tasks -f csv -o timesheet.csv", "a time sheet"),
    ("tally settings", "change settings (arrow keys in a terminal)"),
)


def _examples(*lines: str) -> str:
    return "examples:\n" + "\n".join("  " + x for x in lines)


def build_parser():
    p = Parser(prog="tally", description="%s · per-task token and time tracking for Claude Code, "
                                         "Codex and pi." % fmt.BRAND)
    p.add_argument("--version", action="version", version="tally " + __version__)
    sub = p.add_subparsers(dest="cmd", metavar="<command>", parser_class=Parser)

    def add(name, summary, func=None, aliases=(), examples=(), details=""):
        s = sub.add_parser(name, aliases=list(aliases), help=summary,
                           description=summary[0].upper() + summary[1:] + "." + ("\n\n" + details if details else ""),
                           epilog=_examples(*examples) if examples else None,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        if func:
            s.set_defaults(func=func)
        return s

    def session_opt(s):
        s.add_argument("--session", metavar="ID", help=SESSION_HELP)

    def json_opt(s, what="the same data as JSON"):
        s.add_argument("--json", action="store_true", help=what)

    # ---- internal: run by the agent integrations
    s = add("hook", "(internal) run an agent hook; JSON payload on stdin or --payload", cmd_hook)
    s.add_argument("event")
    s.add_argument("--agent", choices=AGENTS)
    s.add_argument("--payload")
    s = add("slash", "(internal) run a /tally:<command> for a session", cmd_slash)
    session_opt(s)
    s.add_argument("--agent", choices=AGENTS)
    s.add_argument("--transcript")
    s.add_argument("--cwd")
    s.add_argument("--prompt-id")
    s.add_argument("--json", action="store_true")
    s.add_argument("--stdin", action="store_true", help="read the arguments from stdin (the command files' heredoc)")
    s.add_argument("command")
    s.add_argument("args", nargs=argparse.REMAINDER)

    # ---- tasks
    s = add("start", "start a task in an agent session", cmd_start,
            examples=['tally start "Fix login bug" --session 3f9c2a1b-…'],
            details="In an agent, /tally:start is simpler; from a terminal you need the session id.")
    s.add_argument("name", nargs="*", help="the task name (default: the next prompt)")
    session_opt(s)
    s = add("end", "end a task and print its report", cmd_end,
            examples=["tally end", "tally end 12 --note shipped"])
    s.add_argument("id", nargs="?", type=int, help="the task number (default: the active task)")
    s.add_argument("--note", help="a note kept with the task, e.g. what was done")
    session_opt(s)
    s = add("status", "live report of a task", cmd_status, examples=["tally status", "tally status 12 --brief"])
    s.add_argument("id", nargs="?", type=int, help="the task number (default: the active task)")
    session_opt(s)
    json_opt(s)
    s.add_argument("--brief", action="store_true", help="totals only, no breakdown")
    s = add("show", "the report of any task", cmd_show, examples=["tally show 12", "tally show 12 --prompts"])
    s.add_argument("id", type=int, help="the task number (tally list shows them)")
    json_opt(s)
    s.add_argument("--prompts", action="store_true", help="also list the prompts of the task")
    s = add("list", "list tasks, newest first", cmd_list, aliases=["ls"], examples=["tally list", "tally ls -n 50"])
    s.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="how many (default 20)")
    s.add_argument("--all", action="store_true", help="every task")
    json_opt(s)
    s = add("resume", "continue a task in a session (reopens an ended task)", cmd_resume,
            examples=["tally resume 12 --session 3f9c2a1b-…"])
    s.add_argument("id", type=int, help="the task number")
    session_opt(s)
    s = add("rename", "rename a task", cmd_rename, examples=['tally rename 12 "Fix login redirect"'])
    s.add_argument("id", type=int, help="the task number")
    s.add_argument("name", nargs="+", help="the new name")
    s = add("delete", "delete a task and what it counted", cmd_delete, examples=["tally delete 12"])
    s.add_argument("id", type=int, help="the task number")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    s = add("stats", "detailed statistics of a task: per prompt, tools, averages", cmd_stats,
            examples=["tally stats", "tally stats 12"])
    s.add_argument("id", nargs="?", type=int, help="the task number (default: the active or last task)")
    session_opt(s)
    json_opt(s)

    # ---- todos
    s = add("todo", "the todo list: add, list, next, show, take, done, mark, edit, rm, import",
            examples=['tally todo add "Fix login" @web !! due:fri', "tally todo list", "tally todo done 3"])
    tsub = s.add_subparsers(dest="todo_cmd", metavar="<action>", parser_class=Parser)
    s.set_defaults(func=lambda a, parser=s: (cmd_todo(a) if a.todo_cmd else parser.print_help()))

    def tadd(name, summary, aliases=(), examples=()):
        return tsub.add_parser(name, aliases=list(aliases), help=summary,
                               description=summary[0].upper() + summary[1:] + ".",
                               epilog=_examples(*examples) if examples else None,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    t = tadd("add", "add a todo", examples=['tally todo add "Fix login" @web !! due:fri -- check Safari',
                                            "tally todo add Write tests --priority 2 --due +3d"])
    t.add_argument("text", nargs="+", help="the title; @project, ! !! !!!, due:<when> and -- notes are recognised")
    t.add_argument("-p", "--project", help="the project (default: the one of the current directory)")
    t.add_argument("--priority", type=int, choices=[0, 1, 2, 3], help="0 none, 1 low, 2 medium, 3 high")
    t.add_argument("--due", metavar="WHEN", help="today, tomorrow, fri, +3d, 2026-10-20, 20.10.")
    t.add_argument("--notes", help="longer notes")
    t.add_argument("--parent", type=int, metavar="ID", help="make it a step of another todo")
    t.add_argument("--source", default="user", help="user, claude or import (default user)")
    json_opt(t)
    t = tadd("list", "list todos (default: the open ones of this project)", aliases=["ls"],
             examples=["tally todo list", "tally todo list --all --status done"])
    t.add_argument("-p", "--project", help="this project instead of the current one")
    t.add_argument("--all", action="store_true", help="every project")
    t.add_argument("--status", default="open", help="open, done, all, todo, doing, blocked or dropped (default open)")
    t.add_argument("--tree", action="store_true", help="show plan steps under their todos")
    json_opt(t)
    t = tadd("next", "the next todo to work on: doing first, then by priority and due date")
    t.add_argument("-p", "--project", help="this project instead of the current one")
    t.add_argument("--all", action="store_true", help="every project")
    json_opt(t)
    t = tadd("show", "a todo with its steps and the work measured on it", examples=["tally todo show 3"])
    t.add_argument("id", type=int, help="the todo number")
    json_opt(t)
    t = tadd("take", "pick up a todo in an agent session: it becomes doing and a task measures it")
    t.add_argument("id", type=int, help="the todo number")
    session_opt(t)
    t = tadd("done", "mark a todo done (ends its active task)", examples=["tally todo done 3"])
    t.add_argument("id", type=int, help="the todo number")
    t.add_argument("--note", help="a note for the task it ends")
    t = tadd("mark", "set the status of a todo", examples=["tally todo mark 3 blocked"])
    t.add_argument("id", type=int, help="the todo number")
    t.add_argument("status", choices=["todo", "doing", "blocked", "done", "dropped"], help="the new status")
    t.add_argument("--note", help="a note for the task it ends (done, dropped)")
    t = tadd("edit", "change the title, notes, project, priority or due date of a todo",
             examples=["tally todo edit 3 --due fri --priority 3"])
    t.add_argument("id", type=int, help="the todo number")
    t.add_argument("--title", help="the new title")
    t.add_argument("--notes", help="the new notes")
    t.add_argument("--project", help="move it to this project")
    t.add_argument("--priority", type=int, choices=[0, 1, 2, 3], help="0 none, 1 low, 2 medium, 3 high")
    t.add_argument("--due", metavar="WHEN", help="the new due date, or '' to clear it")
    t = tadd("rm", "delete a todo and its steps", examples=["tally todo rm 3"])
    t.add_argument("id", type=int, help="the todo number")
    t.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    t = tadd("import", "create or update todos from a markdown plan (superpowers, checklists)",
             examples=["tally todo import docs/plan.md"])
    t.add_argument("file", help="the markdown file")
    t.add_argument("-p", "--project", help="the project (default: the one of the current directory)")
    t.add_argument("--source", default="import", help="recorded as the source (default import)")
    json_opt(t)

    # ---- projects and sessions
    s = add("project", "a project's numbers, or set how it is tracked", cmd_project,
            examples=["tally project", "tally project web-app", "tally project --set track"],
            details="Modes: track (every session is read and kept), tasks (only the tasks you start are kept),\n"
                    "off (ignored). Track and tasks also read the project's sessions into the sessions overview.")
    s.add_argument("name", nargs="?", help="the project (default: the one of the current directory)")
    s.add_argument("--set", choices=["track", "tasks", "off"], metavar="MODE", help="track, tasks or off")
    s.add_argument("--days", type=int, metavar="N", help="what Tally kept: only the last N days")
    s.add_argument("--series", type=int, default=14, metavar="N", help="days in the daily chart (default 14)")
    json_opt(s)
    s = add("projects", "every project: tracking, sessions, tokens and cost of the last 30 days", cmd_project,
            examples=["tally projects"])
    json_opt(s)
    s = add("sessions", "the tracked sessions of every agent: tokens, cost, models, time", cmd_sessions,
            examples=["tally sessions", "tally sessions --days 7 --agent codex", "tally sessions --all -p web-app"],
            details="Tracked: projects set to track or tasks, sessions a task counted, sessions added in the\n"
                    "dashboard (setting sessions.scope). The last line says how many are not tracked.")
    s.add_argument("--days", type=float, default=30, metavar="N", help="sessions active in the last N days (default 30)")
    s.add_argument("--all", action="store_true", help="all history")
    s.add_argument("--agent", choices=AGENTS, help="only this agent")
    s.add_argument("-p", "--project", help="only this project")
    s.add_argument("--limit", type=int, default=40, metavar="N", help="rows to show (default 40, 0 = all)")
    s.add_argument("--no-refresh", action="store_true", help="show the index as it is, without reading transcripts")
    json_opt(s)
    s = add("session", "the full report of one session, any agent", cmd_session,
            examples=["tally session 3f9c2a1b", "tally session ~/.codex/sessions/2026/10/12/rollout-….jsonl"])
    s.add_argument("session_id", metavar="session", help="a session id, its first characters, or a transcript path")
    json_opt(s)
    s = add("export", "export a summary, tasks, a task, projects, a project, sessions or a session", cmd_export,
            examples=["tally export tasks -f csv -o timesheet.csv", "tally export task 12 --copy",
                      "tally export summary --days 7", "tally export sessions -p web-app -f md"],
            details="Scopes and their formats (the first is the default):\n"
                    "  summary         md, json, csv\n"
                    "  tasks           csv, json, md, text\n"
                    "  task <id>       md, text, json, csv\n"
                    "  projects        csv, json, md\n"
                    "  project <name>  md, json, csv\n"
                    "  sessions        csv, json, md\n"
                    "  session <id>    md, text, json")
    s.add_argument("scope", nargs="?", default="tasks",
                   choices=["summary", "tasks", "task", "projects", "project", "sessions", "session"],
                   metavar="scope", help="summary, tasks (default), task, projects, project, sessions or session")
    s.add_argument("ref", nargs="?", help="task, project or session: its number, name or id")
    s.add_argument("-f", "--format", choices=["csv", "json", "md", "text"], help="csv, json, md or text")
    s.add_argument("-o", "--output", metavar="FILE", help="write to a file instead of the screen")
    s.add_argument("--copy", action="store_true", help="copy to the clipboard instead")
    s.add_argument("-p", "--project", help="tasks or sessions of this project")
    s.add_argument("--days", type=float, default=30, metavar="N", help="sessions and summary: the last N days (default 30)")
    s.add_argument("--all", action="store_true", help="sessions and summary: all history")
    s.add_argument("--agent", choices=AGENTS, help="sessions of this agent")
    s = add("dashboard", "the local web dashboard", cmd_dashboard, aliases=["serve"],
            examples=["tally dashboard --open", "tally dashboard --background", "tally dashboard --stop"])
    s.add_argument("--open", action="store_true", help="open it in the browser")
    s.add_argument("--background", action="store_true", help="start it in the background and return")
    s.add_argument("--stop", action="store_true", help="stop the running dashboard")
    s.add_argument("--host", help="the address to listen on (default 127.0.0.1; keep it local)")
    s.add_argument("--port", type=int, help="the port (default: setting dashboard_port, 8765)")
    s.add_argument("--idle-minutes", type=float, metavar="N", help="stop after N minutes without an open page")

    # ---- settings and maintenance
    s = add("settings", "show or change settings", cmd_settings,
            examples=["tally settings", "tally settings log.prompts preview", "tally settings sessions.scope"],
            details="In a terminal, tally settings alone lets you change them with the arrow keys.")
    s.add_argument("key", nargs="?", help="one setting, e.g. log.prompts")
    s.add_argument("value", nargs="?", help="its new value")
    s.add_argument("--list", action="store_true", help="only list them, also in a terminal")
    s = add("setup", "the setup questions: prompts, logging, new projects, sessions, updates, status line",
            cmd_setup)
    s = add("update", "update Tally to the latest version", cmd_update, examples=["tally update", "tally update --check"],
            details="Pulls the latest version, installs it into the agents Tally is in, restarts a running\n"
                    "dashboard and lists what changed.")
    s.add_argument("--check", action="store_true", help="only say whether a new version is out")
    s.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    s = add("doctor", "check the installation: version, data, agents, hooks, updates", cmd_doctor)
    s = add("install", "install into agents: Claude Code, Codex, pi", cmd_install,
            examples=["tally install", "tally install --agents claude,pi"])
    s.add_argument("--agents", metavar="LIST", help="comma separated: claude,codex,pi")
    s.add_argument("--all", action="store_true", help="every supported agent")
    s.add_argument("-y", "--yes", action="store_true", help="no questions: every detected agent")
    s = add("uninstall", "remove Tally from agents (your history stays)", cmd_uninstall,
            examples=["tally uninstall", "tally uninstall --agents codex"])
    s.add_argument("--agents", metavar="LIST", help="comma separated (default: every agent it is installed in)")
    s = add("statusline", "the one-line segment for a status line (reads the statusLine JSON on stdin)",
            cmd_statusline)
    session_opt(s)
    s.add_argument("--width", type=int, default=28, metavar="N", help="longest task name shown (default 28)")
    s = add("config", "raw configuration keys (advanced; tally settings is validated)", cmd_config,
            examples=["tally config", 'tally config pricing \'{"claude-opus-5-5": {"input": 4}}\''])
    s.add_argument("key", nargs="?", help="a top-level key of config.json")
    s.add_argument("value", nargs="?", help="its new value (JSON, or plain text)")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] in (["todo", "add"],) and "--" in argv[2:]:
        # `tally todo add Fix login -- check Safari`: argparse would swallow the --, so keep it as text
        i = argv.index("--", 2)
        argv = argv[:i] + [" -- " + " ".join(argv[i + 1:])] if argv[i + 1:] else argv[:i]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        print(overview())
        return 0
    try:
        rc = args.func(args)
    except KeyboardInterrupt:                  # Ctrl-C: stop quietly, the shell's convention for the exit code
        sys.stderr.write("\n")
        return 130
    if args.cmd in NOTIFY:
        _update_notice()
    return rc or 0


# commands a person runs and reads: they mention a new version (once a day, on a terminal)
NOTIFY = {"status", "show", "list", "ls", "sessions", "session", "stats", "todo", "project", "projects",
          "settings", "doctor", "dashboard", "serve", "export", "setup"}


def _update_notice() -> None:
    try:
        from . import updates
        updates.check_in_background()
        if sys.stderr.isatty():
            text = updates.notice("cli")
            if text:
                sys.stderr.write("\n" + (fmt.paint(text) if fmt.use_color(sys.stderr) else text) + "\n")
    except Exception:
        pass
