"""Slash commands (/task:start, /task:end, ...).

The UserPromptSubmit hook intercepts these before they reach the model and
blocks them, so running a command costs zero tokens. The same functions back
the CLI.
"""

import re
from typing import Optional

from . import fmt, report, tasks

PREFIX_RE = re.compile(r"^\s*/task:([a-z][a-z-]*)(?:[ \t]+(.*))?\s*$", re.S)

HELP = """task-counter: tracks tokens and time per task, including subagents and workflows.

  /task:start [name]       start a task here (the next prompt is stored as its initial prompt)
  /task:new <prompt>       start a task AND send <prompt> to Claude as its first instruction
  /task:status [id]        live totals for the current task (or task #id)
  /task:end [id] [note]    end the task and print the final report
  /task:list [n|all]       recent tasks
  /task:show <id>          full report of any task
  /task:resume <id>        continue counting task #id in this session (also reopens an ended task)
  /task:rename [id] <name> rename a task
  /task:dashboard          open the local web dashboard
  /task:help               this help

Commands run inside the hook and never reach the model, so they cost no tokens.
CLI: task-counter --help   Data: ~/.claude/task-counter/tasks.db"""

KNOWN = {"start", "new", "end", "stop", "status", "list", "ls", "show", "report", "resume",
         "attach", "rename", "dashboard", "help"}


class Result:
    def __init__(self, text: Optional[str], block: bool = True):
        self.text = text
        self.block = block


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
    msg = ["No active task in this session. Start one with /task:start <name> or /task:new <prompt>."]
    if act:
        msg.append("")
        msg.append("Active tasks in other sessions:")
        for t in act[:10]:
            msg.append("  #%d  %s  (started %s, %s)" % (t["id"], t["name"], fmt.when(t["started_at"]), t["cwd"] or ""))
        msg.append("Use /task:status <id>, /task:end <id> or /task:resume <id>.")
    return "\n".join(msg)


def run(conn, session_id: str, cmd: str, args: str, cwd: Optional[str] = None,
        transcript_path: Optional[str] = None) -> Result:
    if cmd == "help":
        return Result(HELP)

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
        out.append("  /task:status for live totals · /task:end to finish")
        return Result("\n".join(out))

    if cmd == "new":
        if not args:
            return Result("Usage: /task:new <prompt>  (starts a task and sends the prompt to Claude)")
        tasks.start(conn, session_id, initial_prompt=args, cwd=cwd,
                    transcript_path=transcript_path, created_by="new")
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
            return Result("Usage: /task:resume <id>")
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
            return Result("Usage: /task:rename [id] <new name>")
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


def list_rows(conn, limit: int = 15) -> list:
    rows = []
    for t in tasks.list_tasks(conn, limit=limit):
        rep = report.summary_for(conn, t)
        rows.append({
            "id": t["id"], "name": t["name"], "status": t["status"], "started_at": t["started_at"],
            "ended_at": t["ended_at"], "wall_s": rep["time"]["wall_s"], "active_s": rep["time"]["active_s"],
            **rep["totals"],
        })
    return rows
