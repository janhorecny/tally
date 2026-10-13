"""Slash commands (/tally:start, /tally:end, ...).

The UserPromptSubmit hook intercepts these before they reach the model and
blocks them, so running a command costs zero tokens. The same functions back
the CLI.
"""

import re
from typing import Optional

from . import fmt, report, tasks

# "/tally:start x" (Claude Code), "tally:start x" (Codex: its TUI rejects unknown slash commands)
PREFIX_RE = re.compile(r"^\s*/?tally:([a-z][a-z-]*)(?:[ \t]+(.*))?\s*$", re.S)

HELP = """Tally: tokens and time per task, including subagents and workflows, plus a todo list.

Tasks
  /tally:start [name]         start a task here (the next prompt is stored as its initial prompt)
  /tally:new <prompt>         start a task AND send <prompt> to the agent as its first instruction
  /tally:status [id]          live totals for the current task (or task #id)
  /tally:stats [id]           detailed stats: per-prompt breakdown, tools, averages
  /tally:end [id] [note]      end the task and print the final report
  /tally:list [n|all]         recent tasks
  /tally:show [id]            full report of any task (default: the current or latest one)
  /tally:resume <id>          continue counting task #id in this session (also reopens an ended task)
  /tally:rename [id] <name>   rename a task

Todos
  /tally:todo <text>          add a todo: @project, ! !! !!! priority, due:fri|+3d|20.10., " -- notes"
  /tally:todos [project|all|done]
                              open todos of this project (or another, all, done ones)
  /tally:take <id>            pick up a todo: status doing + measuring starts (you write the prompt)
  /tally:do <id>              pick up a todo and let the agent start working on it right away
  /tally:done [id] [note]     mark the todo done (default: the one you are working on) and end its task
  /tally:mark <id> <status> [note]
                              todo | doing | blocked | done | dropped

Projects
  /tally:project [name] [mode]
                              a project's overall stats; mode: track | tasks | off
  /tally:projects             all projects with their tracking mode and totals
  /tally:sessions [days|all] [agent] [here]
                              all sessions across agents: tokens, cost, models, time (default 7 days)

Other
  /tally:settings [key [val]] show or change settings (log.depth, log.prompts, todos.reminder, …)
  /tally:dashboard            open the local web dashboard
  /tally:help                 this help

Commands run inside the hook and never reach the model, so they cost no tokens
(except /tally:new and /tally:do, which hand work to the agent).
CLI: tally --help   Data: ~/.claude/tally/tally.db"""

KNOWN = {"start", "new", "end", "stop", "status", "stats", "list", "ls", "show", "report", "resume",
         "attach", "rename", "dashboard", "help", "todo", "todos", "take", "do", "done", "mark", "settings",
         "project", "projects", "sessions"}


class Result:
    def __init__(self, text: Optional[str], block: bool = True, context: Optional[str] = None):
        self.text = text
        self.block = block
        self.context = context      # additionalContext for the model (non-blocking commands)


def parse(prompt: str):
    m = PREFIX_RE.match(prompt or "")
    if not m:
        return None
    cmd = m.group(1)
    if cmd not in KNOWN:
        return None
    return cmd, (m.group(2) or "").strip()


def _split_id(args: str):
    """'12 some text' -> (12, 'some text'); '#12' accepted."""
    m = re.match(r"^#?(\d+)(?:\s+(.*))?$", args, re.S)
    if m:
        return int(m.group(1)), (m.group(2) or "").strip()
    return None, args


def _no_task_here(conn) -> str:
    act = tasks.active_tasks(conn)
    msg = ["No active task in this session. Start one with /tally:start <name> or /tally:new <prompt>."]
    if act:
        msg.append("")
        msg.append("Active tasks in other sessions:")
        for t in act[:10]:
            msg.append("  #%d  %s  (started %s, %s)" % (t["id"], t["name"], fmt.when(t["started_at"]), t["cwd"] or ""))
        msg.append("Use /tally:status <id>, /tally:end <id> or /tally:resume <id>.")
    return "\n".join(msg)


def run(conn, session_id: str, cmd: str, args: str, cwd: Optional[str] = None,
        transcript_path: Optional[str] = None, prompt_id: Optional[str] = None) -> Result:
    if cmd == "help":
        return Result(HELP)

    if cmd in ("todo", "todos", "take", "do", "done", "mark"):
        return _todo_command(conn, session_id, cmd, args, cwd, transcript_path, prompt_id)

    if cmd == "settings":
        return Result(_settings_command(args))

    if cmd in ("project", "projects"):
        return Result(_project_command(conn, cmd, args, cwd))

    if cmd == "sessions":
        return Result(_sessions_command(args, cwd))

    if cmd == "stats":
        task_id, _ = _split_id(args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None and not task_id:
            rows = tasks.list_tasks(conn, limit=1)
            task = rows[0] if rows else None
        if task is None:
            return Result("Task #%d not found." % task_id if task_id else "No tasks yet.")
        return Result(fmt.render_stats(report.summary_for(conn, task)))

    if cmd == "start":
        task, ended = tasks.start(conn, session_id, name=args or None, cwd=cwd,
                                  transcript_path=transcript_path, created_by="command")
        out = []
        if ended is not None:
            out.append(_ended_text(conn, ended))
            out.append("")
        out.append("▶ Task #%d started: %s" % (task["id"], task["name"]))
        out.append("  Counting tokens and time for this session, its subagents and workflows.")
        if task["name"] == tasks.UNTITLED:
            out.append("  Your next prompt becomes the task name and initial prompt.")
        else:
            out.append("  Your next prompt is stored as the initial prompt.")
        out.append("  /tally:status for live totals · /tally:end to finish")
        return Result("\n".join(out))

    if cmd == "new":
        if not args:
            return Result("Usage: /tally:new <prompt>  (starts a task and sends the prompt to the agent)")
        from . import ingest
        if ingest.session_agent(conn, session_id) == "pi":
            # pi sends the prompt itself (sendUserMessage); it is recorded when the turn starts
            tasks.start(conn, session_id, name=tasks.derive_name(args), cwd=cwd,
                        transcript_path=transcript_path, created_by="new")
            return Result(None, block=False, context=args)
        tasks.start(conn, session_id, initial_prompt=args, cwd=cwd,
                    transcript_path=transcript_path, created_by="new", prompt_id=prompt_id)
        return Result(None, block=False)

    if cmd in ("end", "stop"):
        task_id, note = _split_id(args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None:
            return Result("Task #%d not found." % task_id if task_id else _no_task_here(conn))
        if task["status"] != "active":
            return Result("Task #%d already ended.\n\n%s" % (
                task["id"], fmt.render_report(report.summary_for(conn, task))))
        tasks.end(conn, task["id"], note=note or None)
        return Result(_ended_text(conn, tasks.get(conn, task["id"])))

    if cmd == "status":
        task_id, _ = _split_id(args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None:
            return Result("Task #%d not found." % task_id if task_id else _no_task_here(conn))
        return Result(fmt.render_report(report.summary_for(conn, task)))

    if cmd in ("show", "report"):
        task_id, _ = _split_id(args)
        if not task_id:
            task = tasks.active_for_session(conn, session_id)
            if task is None:
                rows = tasks.list_tasks(conn, limit=1)
                task = rows[0] if rows else None
        else:
            task = tasks.get(conn, task_id)
        if task is None:
            return Result("No such task." if task_id else "No tasks yet.")
        return Result(fmt.render_report(report.summary_for(conn, task)))

    if cmd in ("list", "ls"):
        limit = 15
        if args == "all":
            limit = 0
        elif args.isdigit():
            limit = int(args)
        return Result(fmt.render_list(list_rows(conn, limit)))

    if cmd in ("resume", "attach"):
        task_id, _ = _split_id(args)
        if not task_id:
            return Result("Usage: /tally:resume <id>")
        task = tasks.get(conn, task_id)
        if task is None:
            return Result("Task #%d not found." % task_id)
        if task["status"] == "active":
            task, ended = tasks.attach(conn, task_id, session_id, transcript_path)
        else:
            task, ended = tasks.reopen(conn, task_id, session_id)
        out = []
        if ended is not None:
            out.append(_ended_text(conn, ended))
            out.append("")
        out.append("▶ Task #%d resumed in this session: %s" % (task["id"], task["name"]))
        out.append("  Wall time keeps running from the original start; usage in this session is added from now on.")
        return Result("\n".join(out))

    if cmd == "rename":
        task_id, name = _split_id(args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None or not name:
            return Result("Usage: /tally:rename [id] <new name>")
        task = tasks.rename(conn, task["id"], name)
        return Result("Task #%d renamed to: %s" % (task["id"], task["name"]))

    if cmd == "dashboard":
        from . import server
        url, started = server.ensure_running(open_browser=True)
        return Result("Dashboard %s at %s" % ("started" if started else "running", url))

    return Result(HELP)


def _ended_text(conn, task) -> str:
    rep = report.summary_for(conn, task, live=False)
    return "■ Task #%d ended.\n\n%s" % (task["id"], fmt.render_report(rep))


def list_rows(conn, limit: int = 15, project: Optional[str] = None) -> list:
    rows = []
    for t in tasks.list_tasks(conn, limit=limit, project=project):
        rep = report.summary(conn, t)
        rows.append({
            "id": t["id"], "name": t["name"], "status": t["status"], "started_at": t["started_at"],
            "project": t["project"], "todo_id": t["todo_id"], "prompts": rep["task"].get("prompt_count"),
            "ended_at": t["ended_at"], "wall_s": rep["time"]["wall_s"], "active_s": rep["time"]["active_s"],
            **rep["totals"],
        })
    return rows


# ---------------------------------------------------------------- todos

def _todo_command(conn, session_id, cmd, args, cwd, transcript_path, prompt_id) -> Result:
    from . import projects, todos
    here = projects.detect(cwd)

    if cmd == "todo":
        if not args:
            return Result('Usage: /tally:todo <text> [@project] [!|!!|!!!] [due:fri|+3d|20.10.] [-- notes]')
        try:
            spec = todos.parse_quick(args, here)
        except ValueError as e:
            return Result(str(e))
        t = todos.add(conn, spec["title"], project=spec["project"], priority=spec["priority"],
                      due_at=spec["due_at"], notes=spec["notes"], source="user")
        return Result("＋ Todo #%d in %s: %s%s" % (
            t["id"], t["project"] or "(no project)", t["title"],
            "".join(x for x in (" " + todos.PRIORITY_MARK[t["priority"]] if t["priority"] else "",
                                " · " + todos.due_label(t["due_at"]) if t["due_at"] else ""))))

    if cmd == "todos":
        a = args.strip()
        statuses = todos.OPEN
        project = here
        if a in ("all", "*"):
            project = None
        elif a in ("done", "closed"):
            statuses = ("done", "dropped")
        elif a.startswith("@") or a:
            project = a.lstrip("@") or here
        return Result(fmt.render_todos(conn, todos.list_todos(conn, project=project, statuses=statuses),
                                       project, show_project=project is None))

    todo_id, rest = _split_id(args)

    if cmd in ("take", "do"):
        if not todo_id:
            return Result("Usage: /tally:%s <todo id>" % cmd)
        try:
            todo, task, ended = todos.take(conn, todo_id, session_id, cwd=cwd, transcript_path=transcript_path,
                                           prompt=None, prompt_id=None)
        except ValueError as e:
            return Result(str(e))
        if cmd == "do":
            from . import ingest
            if ingest.session_agent(conn, session_id) != "pi":      # pi records the prompt it sends
                tasks.record_prompt(conn, task, session_id, "/tally:do %d · %s" % (todo_id, todo["title"]),
                                    prompt_id=prompt_id)
            return Result(None, block=False, context=todos.context_for(conn, todo))
        out = []
        if ended is not None:
            out += [_ended_text(conn, ended), ""]
        out.append("▶ Todo #%d taken: %s" % (todo["id"], todo["title"]))
        out.append("  Task #%d measures it (project %s). Write your prompt; /tally:done when finished."
                   % (task["id"], todo["project"] or "-"))
        return Result("\n".join(out))

    if cmd == "done":
        if not todo_id:
            active = tasks.active_for_session(conn, session_id)
            todo_id = active["todo_id"] if active is not None and active["todo_id"] else None
            if not todo_id:
                return Result("Usage: /tally:done <todo id>  (no todo is being worked on in this session)")
        todo = todos.get(conn, todo_id)
        if todo is None:
            return Result("Todo #%d not found." % todo_id)
        todos.set_status(conn, todo_id, "done", note=rest or None)
        out = ["✓ Todo #%d done: %s" % (todo_id, todo["title"])]
        for t in conn.execute("SELECT * FROM tasks WHERE todo_id = ? AND status = 'active'", (todo_id,)).fetchall():
            tasks.end(conn, t["id"], note=rest or "todo done")
            out += ["", _ended_text(conn, tasks.get(conn, t["id"]))]
        st = todos.linked_stats(conn, todo_id)
        if st["tasks"]:
            out.insert(1, "  total over %d task%s: %s tokens · %s · %s active" % (
                st["tasks"], "" if st["tasks"] == 1 else "s", fmt.tokens(st["total"]),
                fmt.money({"cost": st["cost"]}), fmt.duration(st["active_s"])))
        return Result("\n".join(out))

    if cmd == "mark":
        parts = rest.split(None, 1)
        if not todo_id or not parts or parts[0] not in todos.STATUSES:
            return Result("Usage: /tally:mark <todo id> <%s> [note]" % "|".join(todos.STATUSES))
        try:
            t = todos.set_status(conn, todo_id, parts[0], note=parts[1] if len(parts) > 1 else None)
        except ValueError as e:
            return Result(str(e))
        return Result("Todo #%d is now %s: %s" % (t["id"], t["status"], t["title"]))
    return Result(HELP)


def _settings_command(args: str) -> str:
    from . import settings
    parts = args.split(None, 1)
    if len(parts) == 2:
        try:
            v = settings.set_value(parts[0], parts[1])
        except ValueError as e:
            return str(e)
        return "✓ %s = %s" % (parts[0], v)
    lines = ["Tally settings (change: /tally:settings <key> <value>)", ""]
    for key, value, desc, allowed in settings.describe():
        if parts and parts[0] != key:
            continue
        lines.append("  %-22s %-10s [%s]" % (key, value, allowed))
        lines.append("  %-22s %s" % ("", desc))
    return "\n".join(lines)


def _sessions_command(args: str, cwd: Optional[str]) -> str:
    """/tally:sessions [days|all] [claude|codex|pi] [here]"""
    from . import projects, sessions
    days, agent, project = 7.0, None, None
    for word in args.lower().split():
        if word == "all":
            days = None
        elif re.match(r"^\d+(\.\d+)?d?$", word):
            days = float(word.rstrip("d"))
        elif word in ("claude", "codex", "pi"):
            agent = word
        elif word == "here":
            project = projects.detect(cwd)
        else:
            return "Usage: /tally:sessions [days|all] [claude|codex|pi] [here]"
    sessions.refresh(days, [agent] if agent else None)
    return fmt.render_sessions(sessions.overview(days, agent=agent, project=project), limit=20)


def _project_command(conn, cmd: str, args: str, cwd: Optional[str]) -> str:
    from . import db, projects
    if cmd == "projects":
        names = projects.all_names(conn)
        if not names:
            return "No projects yet."
        lines = ["%-28s %-6s %9s %9s %9s %6s %6s" % ("project", "mode", "30d tok", "30d cost", "all tok", "tasks",
                                                       "todos")]
        for n in names:
            st = projects.stats(conn, n, series_days=1)
            open_todos = st["todos"]["todo"] + st["todos"]["doing"] + st["todos"]["blocked"]
            lines.append("%-28s %-6s %9s %9s %9s %6d %6d" % (
                fmt._cut(n, 28), st["mode"], fmt.tokens(st["last_30d"]["total"]), fmt.money(st["last_30d"]),
                fmt.tokens(st["totals"]["total"]), st["tasks"]["count"], open_todos))
        return "\n".join(lines)
    parts = args.split()
    name = projects.detect(cwd)
    if parts and parts[0] not in projects.MODES:
        name, parts = parts[0], parts[1:]
    if not name:
        return "No project here."
    if parts:
        with db.transaction(conn):
            row = projects.set_mode(conn, name, parts[0])
        return "✓ %s: %s (%s)" % (name, row["mode"], projects.MODE_HELP[row["mode"]])
    return fmt.render_project(projects.stats(conn, name, series_days=14))
