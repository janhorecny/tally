"""Command line interface: `task-counter <command>`."""

import argparse
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
    return hooks.main(args.event)


def cmd_slash(args):
    """Run a /task:<cmd> exactly as the hook would (used by command fallbacks)."""
    conn = db.connect()
    text = " ".join(args.args).strip()
    if text.startswith("--"):
        text = text[2:].strip()
    res = commands.run(conn, _session(args), args.command, text, cwd=os.getcwd())
    if res.text:
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
    out = open(args.output, "w", newline="") if args.output else sys.stdout
    try:
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
    finally:
        if args.output:
            out.close()


def cmd_session(args):
    """Analyse a whole session (no task needed): ingest everything and report."""
    conn = db.connect()
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


def cmd_doctor(args):
    print("task-counter %s · python %s" % (__version__, sys.version.split()[0]))
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
        print("last hook     never - the plugin hooks have not fired yet (restart Claude Code after install)")
    print("transcripts   %s %s" % (paths.projects_dir(), "(ok)" if paths.projects_dir().is_dir() else "(MISSING)"))
    inst = paths.claude_dir() / "plugins" / "installed_plugins.json"
    try:
        data = json.load(open(inst))
        found = [k for k in json.dumps(data).split('"') if k.startswith("task@")]
        print("plugin        %s" % (", ".join(sorted(set(found))) or "not installed (run ./install.sh)"))
    except Exception:
        print("plugin        unknown (%s not readable)" % inst)
    if paths.error_log_path().exists() and paths.error_log_path().stat().st_size:
        print("errors        see %s" % paths.error_log_path())
    else:
        print("errors        none")


def build_parser():
    p = argparse.ArgumentParser(prog="task-counter", description="Per-task token and time tracking for Claude Code.")
    p.add_argument("--version", action="version", version="task-counter " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("hook", help="(internal) run a Claude Code hook; JSON on stdin")
    s.add_argument("event")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("slash", help="(internal) run a /task:<command> for a session")
    s.add_argument("--session")
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
