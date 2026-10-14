"""Slash commands (/tally:start, /tally:end, ...).

The UserPromptSubmit hook intercepts these before they reach the model and
blocks them, so running a command costs zero tokens. The same functions back
the CLI.
"""

import json
import re
import time
from typing import Optional

from . import fmt, report, tasks

# "/tally:start x" (Claude Code), "tally:start x" (Codex: its TUI rejects unknown slash commands);
# the text may also start on the next line ("/tally:new" + Shift+Enter)
PREFIX_RE = re.compile(r"^\s*/?tally:([a-z][a-z-]*)(?:\s+(.*))?\s*$", re.S)

HELP = (
    ("Tasks", (
        ("/tally:start [name]", "start a task here (the next prompt is stored as its initial prompt)"),
        ("/tally:new <prompt>", "start a task AND send <prompt> to the agent as its first instruction"),
        ("/tally:status [id]", "live totals for the current task (or task #id)"),
        ("/tally:stats [id]", "detailed stats: per-prompt breakdown, tools, averages"),
        ("/tally:end [id] [note]", "end the task and print the final report"),
        ("/tally:list [n|all]", "recent tasks"),
        ("/tally:show [id]", "full report of any task (default: the current or latest one)"),
        ("/tally:resume <id>", "continue counting task #id in this session (also reopens an ended task)"),
        ("/tally:rename [id] <name>", "rename a task"),
    )),
    ("Todos", (
        ("/tally:todo <text>", 'add a todo: @project, ! !! !!! priority, due:fri|+3d|20.10., " -- notes"'),
        ("/tally:todos [project|all|done]", "open todos of this project (or another, all, done ones)"),
        ("/tally:take <id>", "pick up a todo: status doing + measuring starts (you write the prompt)"),
        ("/tally:do <id>", "pick up a todo and let the agent start working on it right away"),
        ("/tally:done [id] [note]", "mark the todo done (default: the one you are working on) and end its task"),
        ("/tally:mark <id> <status> [note]", "todo | doing | blocked | done | dropped"),
    )),
    ("Projects", (
        ("/tally:project [name] [mode]", "a project's overall stats; mode: track | tasks | off"),
        ("/tally:projects", "all projects with their tracking mode and totals"),
        ("/tally:sessions [days|all] [agent] [here]",
         "all sessions across agents: tokens, cost, models, time (default 7 days)"),
    )),
    ("Other", (
        ("/tally:settings [key [val]]", "show or change settings (log.depth, log.prompts, todos.reminder, …)"),
        ("/tally:dashboard [stop]", "open the local web dashboard, or stop it"),
        ("/tally:help", "this help"),
    )),
)
HELP_FREE = ("Commands are answered locally and cost no tokens, except /tally:new and /tally:do,",
             "which hand work to the agent.")
HELP_MORE = "→ tally setup in a terminal: onboarding questions · tally --help: the CLI · data: ~/.claude/tally/tally.db"

KNOWN = {"start", "new", "end", "stop", "status", "stats", "list", "ls", "show", "report", "resume",
         "attach", "rename", "dashboard", "help", "todo", "todos", "take", "do", "done", "mark", "settings",
         "project", "projects", "sessions"}


class Result:
    def __init__(self, text: Optional[str], block: bool = True, context: Optional[str] = None):
        self.text = text
        self.block = block
        self.context = context      # additionalContext for the model (non-blocking commands)


ONCE_S = 3.0          # a command that finished this soon before: the same prompt, answered once
ONCE_WAIT_S = 55.0    # a command still running: wait this long for it (the prompt hook's timeout is 60 s)


def _once_digest(cmd: str, args: str) -> str:
    """The hook gets the prompt as typed, the command file's fallback gets the
    arguments through the shell ("$ARGUMENTS"): compare them without quotes,
    backslashes and extra spaces."""
    import hashlib
    text = " ".join(re.sub(r"[\"'`\\]", "", args or "").split())
    return hashlib.sha1(("%s %s" % (cmd, text)).encode("utf-8", "replace")).hexdigest()


def run_once(conn, session_id: str, cmd: str, args: str, run, via: str = "hook", raw: Optional[str] = None,
             now: Optional[float] = None) -> Result:
    """Run a /tally:* command once per prompt. Claude Code can run one prompt
    twice: the hook and also the command file's fallback (tally slash), or two
    hooks after a plugin was reloaded with a new version. Whichever comes
    second waits for the first one (as long as it runs) and gives the same answer.

    via: "hook", "fallback" (a Claude Code command file: its arguments went
    through Claude Code and the shell, so they are not compared) or "agent"
    (pi). raw: the prompt as typed (what 0.10.1-0.11.5 compared). now: when
    the hook started (a run that waited for the database still finds its twin).

    One record per session and command (once:<session>:<cmd>), and for the
    hooks of older versions still registered next to this one, also the record
    they read (once:<session>).
    """
    import hashlib
    from . import db
    now = time.time() if now is None else now
    keys = ("once:%s:%s" % (session_id, cmd), "once:" + session_id, "once_last:" + session_id)
    digest = _once_digest(cmd, args)
    raw_digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest() if raw is not None else None
    mine = None
    with db.transaction(conn):
        twin = _twin(_claims(conn, keys[0]), _kv_json(conn, keys[2]), digest, via, now)
        legacy = None if twin is not None else _legacy_twin(_kv_json(conn, keys[1]), digest, raw_digest, now)
        if twin is None and legacy is None:
            mine = _claim(conn, keys, cmd, digest, via, now)
    if mine is None:
        res = _wait(conn, keys, twin, legacy, now)
        if res is not None:
            return res
        with db.transaction(conn):              # the first run failed or did not finish: run it here
            mine = _claim(conn, keys, cmd, digest, via, now)
    res = None
    try:
        res = run()
    finally:
        _finish(conn, keys, mine, res, cmd)
    return res


def _kv_json(conn, key: str) -> dict:
    from . import db
    try:
        d = json.loads(db.kv_get(conn, key) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _num(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


def _claims(conn, key: str) -> list:
    claims = _kv_json(conn, key).get("claims")
    return [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []


def _fresh(rec: dict, now: float) -> bool:
    """Still running (claimed less than ONCE_WAIT_S ago), or finished less than
    ONCE_S ago (the records of 0.11.8 and older have no done_ts: their claim time)."""
    if rec.get("done"):
        return now - _num(rec, "done_ts", _num(rec, "ts")) < ONCE_S
    return now - _num(rec, "ts") < ONCE_WAIT_S


def _twin(claims: list, latest: dict, digest: str, via: str, now: float) -> Optional[dict]:
    """The claim of the same command this run is a twin of: the same arguments,
    or a Claude Code command fallback on either side (the arguments it gets can
    be expanded or cut off). Still running, or finished a moment ago with no
    other command claimed in the session since (that would be a new prompt)."""
    best = None
    for c in claims:
        if not _fresh(c, now) or not (c.get("hash") == digest or via == "fallback" or c.get("via") == "fallback"):
            continue
        if c.get("done") and _num(latest, "seq") > _num(c, "seq"):
            continue
        if best is None or _num(c, "seq") > _num(best, "seq"):
            best = c
    return best


def _legacy_twin(rec: dict, digest: str, raw_digest: Optional[str], now: float):
    """(record, raw) when once:<session> was written for this prompt by an older
    version: 0.11.6-0.11.8 hash the command as _once_digest does, 0.10.1-0.11.5
    the prompt as typed (raw). The copy this version writes there is not read:
    its own record says more."""
    if not rec or "cmd" in rec or not _fresh(rec, now):
        return None
    if rec.get("hash") == digest:
        return rec, False
    if raw_digest is not None and rec.get("hash") == raw_digest:
        return rec, True
    return None


def _as_result(r, raw: bool = False) -> Optional[Result]:
    """A stored answer: {"text", "block", "context"} (0.11.6 on), or what the
    hook of 0.10.1-0.11.5 printed (raw; None there: the prompt went on to the
    model). None: no answer to reuse (the run failed)."""
    if isinstance(r, dict) and ("decision" in r or "hookSpecificOutput" in r):
        if r.get("decision") == "block":
            return Result(r.get("reason"))
        out = r.get("hookSpecificOutput")
        return Result(None, block=False, context=out.get("additionalContext") if isinstance(out, dict) else None)
    if isinstance(r, dict):
        return Result(r.get("text"), r.get("block", True), r.get("context"))
    return Result(None, block=False) if r is None and raw else None


def _claim(conn, keys, cmd: str, digest: str, via: str, now: float) -> dict:
    import os
    from . import db
    key, legacy_key, latest_key = keys
    seq = int(_num(_kv_json(conn, latest_key), "seq")) + 1
    t = time.time()
    mine = {"id": "%d:%.6f" % (os.getpid(), t), "hash": digest, "via": via, "ts": now, "seq": seq, "done": False}
    db.kv_set(conn, key, json.dumps({"claims": [c for c in _claims(conn, key) if _fresh(c, t)] + [mine]}))
    db.kv_set(conn, latest_key, json.dumps({"seq": seq, "cmd": cmd}))
    # what the hooks of 0.11.6-0.11.8 compare (0.10.1-0.11.5 hash the raw prompt: they cannot match both)
    db.kv_set(conn, legacy_key, json.dumps({"hash": digest, "ts": now, "done": False, "cmd": cmd, "id": mine["id"]}))
    return mine


def _wait(conn, keys, twin: Optional[dict], legacy, now: float) -> Optional[Result]:
    """The twin's answer once it is done (waiting at most ONCE_WAIT_S from this
    run's start); None when it failed, was replaced or did not finish."""
    deadline = now + ONCE_WAIT_S
    while True:
        if twin is not None:
            rec = next((c for c in _claims(conn, keys[0]) if c.get("id") == twin.get("id")), None)
            if rec is None:
                return None
            if rec.get("done"):
                return _as_result(rec.get("result"))
        else:
            first, raw = legacy
            rec = _kv_json(conn, keys[1])
            if rec.get("hash") != first.get("hash") or rec.get("ts") != first.get("ts"):
                return None
            if rec.get("done"):
                return _as_result(rec.get("result"), raw)
        if time.time() >= deadline:
            return None
        time.sleep(0.05)


def _finish(conn, keys, mine: dict, res: Optional[Result], cmd: str) -> None:
    """Store the answer for a twin. Only this run's own claim is replaced: a
    claim made meanwhile (another command, other arguments) stays as it is."""
    from . import db
    key, legacy_key, _ = keys
    saved = {"text": res.text, "block": res.block, "context": res.context} if res is not None else None
    t = time.time()
    done = dict(mine, done=True, done_ts=t, result=saved)
    with db.transaction(conn):
        others = [c for c in _claims(conn, key) if c.get("id") != mine["id"] and _fresh(c, t)]
        db.kv_set(conn, key, json.dumps({"claims": others + [done]}))
        if _kv_json(conn, legacy_key).get("id") == mine["id"]:
            db.kv_set(conn, legacy_key, json.dumps({"hash": mine["hash"], "ts": mine["ts"], "done": True,
                                                    "result": saved, "cmd": cmd, "id": mine["id"], "done_ts": t}))


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


def _end_args(conn, session_id: str, args: str):
    """/tally:end [id] [note]. '#2 …' and a number alone are a task id; '2 more
    text' is one only when this session has no active task or task #2 is (or
    was) counted in this session. Otherwise it is the note of this session's
    task: '/tally:end 1 test still flaky' must not end task #1 of another terminal."""
    m = re.match(r"^(#?)(\d+)(?:\s+(.*))?$", args, re.S)
    if not m:
        return None, args
    task_id, rest = int(m.group(2)), (m.group(3) or "").strip()
    if m.group(1) or not rest or tasks.active_for_session(conn, session_id) is None:
        return task_id, rest
    if conn.execute("SELECT 1 FROM task_sessions WHERE task_id = ? AND session_id = ? LIMIT 1",
                    (task_id, session_id)).fetchone():
        return task_id, rest
    return None, args


def help_text() -> str:
    """Commands in aligned columns; in the Claude app (no fixed-width font)
    one line per command: '/tally:start [name] – start a task here'."""
    from . import __version__
    out = ["%s %s · tokens and time per task, including subagents and workflows, plus a todo list." % (
        fmt.BRAND, __version__)]
    for section, rows in HELP:
        out += ["", section if fmt.APP else "  " + section]
        for cmd, what in rows:
            if fmt.APP:
                out.append("%s – %s" % (cmd, what))
            elif len(cmd) <= 27:
                out.append("    %-27s %s" % (cmd, what))
            else:
                out += ["    " + cmd, " " * 32 + what]
    out += ["", " ".join(HELP_FREE) if fmt.APP else "  " + "\n  ".join(HELP_FREE),
            HELP_MORE if fmt.APP else "  " + HELP_MORE]
    return "\n".join(out)


def _no_task_here(conn, cmd: str = "") -> str:
    act = tasks.active_tasks(conn)
    msg = [fmt.head("No active task in this session")]
    if act:
        msg.append("  Active tasks in other sessions:")
        for t in act[:10]:
            msg.append("    #%d  %s  (started %s, %s)" % (t["id"], t["name"], fmt.when(t["started_at"]), t["cwd"] or ""))
        if len(act) == 1:
            n = act[0]["id"]
            msg.append(fmt.hints("/tally:status %d shows it" % n if cmd == "status" else "",
                                 "/tally:end %d ends it" % n, "/tally:resume %d counts it here" % n))
        else:
            msg.append(fmt.hints("/tally:status <id>", "/tally:end <id>", "/tally:resume <id>"))
    else:
        msg.append(fmt.hints("/tally:start <name> start one", "/tally:new <prompt>"))
    return "\n".join(msg)


def _not_found(task_id) -> str:
    return fmt.error("Task #%d not found" % task_id, "/tally:list shows your tasks")


def _no_tasks() -> str:
    return "\n".join([fmt.head("No tasks yet"), fmt.hints("/tally:start <name> start one", "/tally:new <prompt>")])


def run(conn, session_id: str, cmd: str, args: str, cwd: Optional[str] = None,
        transcript_path: Optional[str] = None, prompt_id: Optional[str] = None) -> Result:
    if cmd == "help":
        return Result(help_text())

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
            return Result(_not_found(task_id) if task_id else _no_tasks())
        return Result(fmt.render_stats(report.summary_for(conn, task)))

    if cmd == "start":
        task, ended = tasks.start(conn, session_id, name=args or None, cwd=cwd,
                                  transcript_path=transcript_path, created_by="command")
        out = []
        if ended is not None:
            out.append(_ended_text(conn, ended))
            out.append("")
        out.append(fmt.head("Task #%d started: %s" % (task["id"], task["name"]), "▶"))
        out.append("  Counting tokens and time for this session, its subagents and workflows.")
        if task["name"] == tasks.UNTITLED:
            out.append("  Your next prompt becomes the task name and initial prompt.")
        else:
            out.append("  Your next prompt is stored as the initial prompt.")
        out.append(fmt.hints("/tally:status live totals", "/tally:end finish"))
        return Result("\n".join(out))

    if cmd == "new":
        if not args:
            return Result(fmt.usage("/tally:new <prompt>", "starts a task and sends the prompt to the agent"))
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
        task_id, note = _end_args(conn, session_id, args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None:
            return Result(_not_found(task_id) if task_id else _no_task_here(conn, "end"))
        if task["status"] != "active":
            return Result("\n".join([fmt.render_report(report.summary_for(conn, task)), "",
                                     fmt.hints("/tally:resume %d reopens it" % task["id"])]))
        tasks.end(conn, task["id"], note=note or None)
        return Result(_ended_text(conn, tasks.get(conn, task["id"])))

    if cmd == "status":
        task_id, _ = _split_id(args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None:
            return Result(_not_found(task_id) if task_id else _no_task_here(conn, "status"))
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
            return Result(_not_found(task_id) if task_id else _no_tasks())
        return Result(fmt.render_report(report.summary_for(conn, task)))

    if cmd in ("list", "ls"):
        limit = 15
        if args == "all":
            limit = 0
        elif re.fullmatch(r"\d+", args):          # not str.isdigit: it takes "²", which int() does not
            limit = int(args)
        return Result(fmt.render_list(list_rows(conn, limit)))

    if cmd in ("resume", "attach"):
        task_id, _ = _split_id(args)
        if not task_id:
            return Result(fmt.usage("/tally:resume <id>", "/tally:list shows your tasks"))
        task = tasks.get(conn, task_id)
        if task is None:
            return Result(_not_found(task_id))
        if task["status"] == "active":
            task, ended = tasks.attach(conn, task_id, session_id, transcript_path)
        else:
            task, ended = tasks.reopen(conn, task_id, session_id)
        out = []
        if ended is not None:
            out.append(_ended_text(conn, ended))
            out.append("")
        out.append(fmt.head("Task #%d resumed in this session: %s" % (task["id"], task["name"]), "▶"))
        out.append("  Wall time keeps running from the original start; usage in this session is added from now on.")
        return Result("\n".join(out))

    if cmd == "rename":
        task_id, name = _split_id(args)
        task = tasks.get(conn, task_id) if task_id else tasks.active_for_session(conn, session_id)
        if task is None or not name:
            return Result(fmt.usage("/tally:rename [id] <new name>"))
        task = tasks.rename(conn, task["id"], name)
        return Result(fmt.head("Task #%d renamed: %s" % (task["id"], task["name"]), "✓"))

    if cmd == "dashboard":
        from . import paths, server
        if args.strip() == "stop":
            cfg = paths.load_config()
            from . import settings
            host, port = cfg.get("dashboard_host", "127.0.0.1"), int(settings.get("dashboard_port", cfg) or 8765)
            if server.stop(host, port):
                return Result(fmt.head("Dashboard stopped", "■") + "\n" + fmt.hints("/tally:dashboard starts it again"))
            return Result(fmt.head("No dashboard is running") + "\n" + fmt.hints("/tally:dashboard starts it"))
        try:
            url, started = server.ensure_running(open_browser=True)
        except (RuntimeError, OSError) as e:
            return Result(fmt.error(str(e), "tally doctor in a terminal"))
        return Result(fmt.head("Dashboard %s at %s" % ("started" if started else "running", url), "✓"))

    return Result(help_text())


def _ended_text(conn, task) -> str:
    rep = report.summary_for(conn, task, live=False)
    return fmt.render_report(rep, title="Task #%d ended: %s" % (task["id"], fmt._cut(task["name"], 70)), mark="■")


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
            return Result(fmt.usage("/tally:todo <text> [@project] [!|!!|!!!] [due:fri|+3d|20.10.] [-- notes]"))
        try:
            spec = todos.parse_quick(args, here)
        except ValueError as e:
            return Result(fmt.error(str(e)))
        t = todos.add(conn, spec["title"], project=spec["project"], priority=spec["priority"],
                      due_at=spec["due_at"], notes=spec["notes"], source="user", once=True)
        return Result("\n".join([
            fmt.head("Todo #%d added to %s: %s%s" % (
                t["id"], t["project"] or "(no project)", t["title"],
                "".join(x for x in (" " + todos.PRIORITY_MARK[t["priority"]] if t["priority"] else "",
                                    " · " + todos.due_label(t["due_at"]) if t["due_at"] else ""))), "＋"),
            fmt.hints("/tally:take %d start measuring" % t["id"], "/tally:todos list")]))

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
            return Result(fmt.usage("/tally:%s <todo id>" % cmd, "/tally:todos lists them"))
        try:
            todo, task, ended = todos.take(conn, todo_id, session_id, cwd=cwd, transcript_path=transcript_path,
                                           prompt=None, prompt_id=None)
        except ValueError as e:
            return Result(fmt.error(str(e), "/tally:todos lists them"))
        if cmd == "do":
            from . import ingest
            if ingest.session_agent(conn, session_id) != "pi":      # pi records the prompt it sends
                tasks.record_prompt(conn, task, session_id, "/tally:do %d · %s" % (todo_id, todo["title"]),
                                    prompt_id=prompt_id)
            return Result(None, block=False, context=todos.context_for(conn, todo))
        out = []
        if ended is not None:
            out += [_ended_text(conn, ended), ""]
        out.append(fmt.head("Todo #%d taken: %s" % (todo["id"], todo["title"]), "▶"))
        out.append("  Task #%d measures it (project %s). Write your prompt." % (task["id"], todo["project"] or "-"))
        out.append(fmt.hints("/tally:done when finished", "/tally:status live totals"))
        return Result("\n".join(out))

    if cmd == "done":
        if not todo_id:
            active = tasks.active_for_session(conn, session_id)
            todo_id = active["todo_id"] if active is not None and active["todo_id"] else None
            if not todo_id:
                return Result(fmt.usage("/tally:done <todo id>", "no todo is being worked on in this session"))
        todo = todos.get(conn, todo_id)
        if todo is None:
            return Result(fmt.error("Todo #%d not found" % todo_id, "/tally:todos lists them"))
        _, ended = close_todo(conn, todo_id, "done", note=rest or None)
        out = [fmt.head("Todo #%d done: %s" % (todo_id, todo["title"]), "✓")]
        for t in ended:
            out += ["", _ended_text(conn, t)]
        st = todos.linked_stats(conn, todo_id)
        if st["tasks"]:
            out.insert(1, "  total over %d task%s: %s tokens · %s · %s active" % (
                st["tasks"], "" if st["tasks"] == 1 else "s", fmt.tokens(st["total"]),
                fmt.money({"cost": st["cost"], "cost_partial": st.get("cost_partial")}), fmt.duration(st["active_s"])))
        return Result("\n".join(out))

    if cmd == "mark":
        parts = rest.split(None, 1)
        if not todo_id or not parts or parts[0] not in todos.STATUSES:
            return Result(fmt.usage("/tally:mark <todo id> <%s> [note]" % "|".join(todos.STATUSES)))
        try:
            t, ended = close_todo(conn, todo_id, parts[0], note=parts[1] if len(parts) > 1 else None)
        except ValueError as e:
            return Result(fmt.error(str(e), "/tally:todos lists them"))
        out = [fmt.head("Todo #%d is now %s: %s" % (t["id"], t["status"], t["title"]),
                        fmt.STATUS_MARKS.get(t["status"], ""))]
        for task in ended:
            out += ["", _ended_text(conn, task)]
        return Result("\n".join(out))
    return Result(help_text())


def close_todo(conn, todo_id: int, status: str, note: Optional[str] = None):
    """Set a todo's status; done or dropped also ends the active tasks measuring
    it (/tally:done, /tally:mark, tally todo done|mark). Returns (todo, ended tasks)."""
    from . import todos
    todo = todos.set_status(conn, todo_id, status, note=note)
    ended = []
    if status in ("done", "dropped"):
        for t in conn.execute("SELECT id FROM tasks WHERE todo_id = ? AND status = 'active'", (todo_id,)).fetchall():
            ended.append(tasks.end(conn, t["id"], note=note or "todo %s" % status))
    return todo, ended


def _settings_command(args: str) -> str:
    from . import settings
    parts = args.split(None, 1)
    if len(parts) == 2:
        try:
            v = settings.set_value(parts[0], parts[1])
        except ValueError as e:
            return fmt.error(str(e), "/tally:settings lists them")
        return fmt.head("%s = %s" % (parts[0], settings.show(v)), "✓")
    if parts and parts[0] not in settings.SCHEMA:
        return fmt.error("Unknown setting %s" % parts[0], "/tally:settings lists them")
    return fmt.render_settings(parts[:1] or None)


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
            return fmt.usage("/tally:sessions [days|all] [claude|codex|pi] [here]")
    sessions.refresh(days, [agent] if agent else None)
    return fmt.render_sessions(sessions.overview(days, agent=agent, project=project), limit=20)


def _project_command(conn, cmd: str, args: str, cwd: Optional[str], cli: bool = False) -> str:
    """/tally:projects and /tally:project [name] [track|tasks|off] (the CLI uses the same views)."""
    from . import db, projects, sessions
    if cmd == "projects":
        sessions.refresh(30)
        return fmt.render_projects(projects.listing(conn, with_usage=True, refresh=False), cli=cli)
    parts = args.split()
    name = projects.detect(cwd)
    if parts and parts[0] not in projects.MODES:
        name, parts = parts[0], parts[1:]
    if not name:
        return fmt.error("No project here", "run it inside a project, or name one: /tally:project <name>")
    if parts:
        if parts[0] not in projects.MODES:
            return fmt.usage("/tally:project [name] [track|tasks|off]")
        with db.transaction(conn):
            row = projects.set_mode(conn, name, parts[0])
        return fmt.head("%s: %s (%s)" % (name, row["mode"], projects.MODE_HELP[row["mode"]]), "✓")
    st = project_view(conn, name)
    if st is None:
        return fmt.error("Project %s not found" % name, "/tally:projects lists them")
    return fmt.render_project(st)


def project_view(conn, name: str, days: Optional[int] = None) -> Optional[dict]:
    """projects.detail after reading new transcript data; None for a project
    nothing is known about."""
    from . import projects, sessions
    sessions.refresh(30)
    st = projects.detail(conn, name, days=days)
    if not (st["registered"] or st["sessions"] or st["not_tracked"]["sessions"] or st["tasks"]["count"]
            or sum(st["todos"].values())):
        return None
    return st
