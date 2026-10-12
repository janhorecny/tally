"""Overview of whole sessions across agents (Claude Code, Codex, pi).

The main database holds what tasks (and projects in track mode) count:
history before a task start is skipped there on purpose. Whole sessions are
indexed into a separate database, sessions.db, read incrementally from the
agents' own transcripts, so the overview also covers sessions without a task
and sessions from before Tally was installed. Projects set to "off" are left
out.

  Claude Code  ~/.claude/projects/<project>/<session>.jsonl (+ subagents)
  Codex        $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl (root threads;
               subagent threads are read with their root)
  pi           $PI_CODING_AGENT_DIR/sessions/<cwd>/<ts>_<session>.jsonl
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional

from . import db, ingest, paths, projects, settings
from .pricing import Pricer, normalize_model

STABLE_S = 3600          # a transcript untouched this long is not re-read when unchanged
TITLE_CHARS = 80
_HEAD = 256 * 1024
_TAIL = 512 * 1024
_CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_TITLE_RE = re.compile(rb'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
_CMD_RE = re.compile(r"<command-(name|args|message)>(.*?)</command-\1>", re.S)

INDEX_SCHEMA = """CREATE TABLE IF NOT EXISTS indexed (
    session_id   TEXT PRIMARY KEY,
    agent        TEXT,
    path         TEXT,
    cwd          TEXT,
    project      TEXT,
    title        TEXT,
    size         INTEGER,
    mtime        REAL,
    summary_json TEXT,             -- totals, models, time; NULL = no API calls
    indexed_at   REAL,
    prompt_offset INTEGER NOT NULL DEFAULT 0,   -- main transcript bytes scanned for prompts
    prompt_turn  TEXT              -- Codex: turn in effect at prompt_offset
)"""


def index_path() -> Path:
    return paths.home() / "sessions.db"


INDEX_VERSION = "1"        # bump when the index format changes: it is rebuilt from the transcripts
_DERIVED = ("usage", "files", "agents", "workflows", "turns", "prompts", "tool_calls", "sessions", "session_costs")


def connect():
    conn = db.connect(index_path())
    if db.kv_get(conn, "index_version") != INDEX_VERSION:
        with db.transaction(conn):
            if db.kv_get(conn, "index_version") != INDEX_VERSION:     # another process may have won
                conn.execute("DROP TABLE IF EXISTS indexed")
                for table in _DERIVED:
                    conn.execute("DELETE FROM %s" % table)
                conn.execute(INDEX_SCHEMA)
                db.kv_set(conn, "index_version", INDEX_VERSION)
    return conn


# ---------------------------------------------------------------- discovery

def _head(path: Path, n: int = _HEAD) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def _tail(path: Path, size: int, n: int = _TAIL) -> bytes:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - n))
            return fh.read(n)
    except OSError:
        return b""


def _unescape(raw: bytes) -> Optional[str]:
    try:
        return json.loads(b'"' + raw + b'"')
    except ValueError:
        return None


def _title(text) -> Optional[str]:
    if not isinstance(text, str) or settings.get("log.prompts") == "off":
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1] + "…"


def _stat(f: Path, since: float):
    try:
        st = f.stat()
    except OSError:
        return None
    return st if st.st_mtime >= since else None


def _claude_files(since: float) -> Iterator[tuple]:
    root = paths.projects_dir()
    if not root.is_dir():
        return
    for proj in root.iterdir():
        if proj.is_dir():
            for f in proj.glob("*.jsonl"):
                st = None if f.name.startswith("agent-") else _stat(f, since)   # agent-*: old sidechains
                if st is not None:
                    yield "claude", f, st


def _codex_files(since: float) -> Iterator[tuple]:
    from . import codex
    root = codex.codex_home() / "sessions"
    if root.is_dir():
        for f in root.glob("*/*/*/rollout-*.jsonl"):
            st = _stat(f, since)
            if st is not None:
                yield "codex", f, st


def _pi_files(since: float) -> Iterator[tuple]:
    from . import installer
    root = installer.pi_home() / "sessions"
    if root.is_dir():
        for f in root.glob("*/*.jsonl"):
            st = _stat(f, since)
            if st is not None:
                yield "pi", f, st


FILES = {"claude": _claude_files, "codex": _codex_files, "pi": _pi_files}


def candidates(since: float = 0, agents=None) -> Iterator[tuple]:
    """(agent, path, stat) of transcripts modified since `since`. Cheap: no reads."""
    for agent, fn in FILES.items():
        if not agents or agent in agents:
            yield from fn(since)


def describe(agent: str, f: Path, st) -> Optional[dict]:
    """Session id, cwd and title of a transcript; None for files that are not
    a session of their own (Codex subagent threads)."""
    row = {"agent": agent, "path": f, "size": st.st_size, "mtime": st.st_mtime, "cwd": None, "title": None}
    if agent == "claude":
        m = _CWD_RE.search(_head(f))
        titles = _TITLE_RE.findall(_tail(f, st.st_size))
        row.update(session_id=f.stem, cwd=_unescape(m.group(1)) if m else None,
                   title=_title(_unescape(titles[-1])) if titles else None)
        return row
    first = title = None
    for line in _head(f).split(b"\n"):
        rec = ingest._loads(line)
        if not rec:
            continue
        if first is None:
            first = rec
            continue
        if agent == "codex":
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rec.get("type") == "event_msg" and p.get("type") == "user_message":
                title = p.get("message")
                break
        else:
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            if rec.get("type") == "message" and msg.get("role") == "user":
                c = msg.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                title = c
                break
    if first is None:
        return None
    if agent == "codex":
        meta = first.get("payload") if first.get("type") == "session_meta" else None
        if not isinstance(meta, dict) or not meta.get("id"):
            return None
        if (meta.get("session_id") or meta["id"]) != meta["id"]:
            return None                                  # subagent thread: read with its root
        row.update(session_id=meta["id"], cwd=meta.get("cwd"))
    else:
        if first.get("type") != "session" or not first.get("id"):
            return None
        row.update(session_id=first["id"], cwd=first.get("cwd"))
    row["title"] = _title(title)
    return row


def _sniff(f: Path) -> str:
    first = ingest._loads(_head(f, 64 * 1024).split(b"\n", 1)[0]) or {}
    if first.get("type") == "session_meta":
        return "codex"
    if first.get("type") == "session" and "cwd" in first:
        return "pi"
    return "claude"


# ---------------------------------------------------------------- indexing

_lock = threading.Lock()
_progress = {"running": False, "done": 0, "total": 0, "started": None, "finished": None, "error": None,
             "days": 0}                               # period of the last refresh (None = all)


def progress() -> dict:
    return dict(_progress)


def refresh(days: Optional[float] = 30, agents=None, on_progress=None) -> dict:
    """Index new transcript data of sessions active in the last `days` days
    (None = all). Unchanged transcripts that have been quiet for an hour
    cost one stat call."""
    if not _lock.acquire(blocking=False):
        return progress()
    try:
        _progress.update(running=True, done=0, total=0, started=time.time(), error=None, days=days)
        main = db.connect()
        conn = connect()
        try:
            return _refresh(main, conn, days, agents, on_progress)
        finally:
            conn.close()
            main.close()
    except Exception as exc:
        _progress["error"] = str(exc)
        raise
    finally:
        _progress.update(running=False, finished=time.time())
        _lock.release()


def refresh_async(days: Optional[float] = 30, min_interval: float = 20) -> dict:
    """Start indexing in a background thread (dashboard) unless one is
    running or finished less than min_interval seconds ago; returns progress."""
    fin = _progress["finished"]
    done = _progress["days"]
    wider = done is not None and (days is None or days > done)       # asks for more than was read
    if not _progress["running"] and (fin is None or wider or time.time() - fin >= min_interval):
        _progress["running"] = True

        def run():
            try:
                refresh(days)
            except Exception:
                pass
            finally:
                _progress["running"] = _lock.locked()
        threading.Thread(target=run, name="tally-sessions", daemon=True).start()
    return progress()


def _refresh(main, conn, days, agents, on_progress) -> dict:
    now = time.time()
    since = now - days * 86400 if days else 0
    found = list(candidates(since, agents))
    _progress["total"] = len(found)
    known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime FROM indexed")}
    # files read as part of another session (Codex subagent threads)
    inner = {r[0] for r in conn.execute(
        "SELECT f.path FROM files f JOIN indexed i ON i.session_id = f.session_id WHERE f.path != i.path")}
    modes = {r["name"]: r["mode"] for r in main.execute("SELECT name, mode FROM projects")}
    pricer = Pricer(paths.load_config().get("pricing"))
    changed = 0
    for i, (agent, f, st) in enumerate(found):
        _progress["done"] = i
        if on_progress and i % 25 == 0:
            on_progress(i, len(found))
        key = str(f)
        old = known.get(key)
        if key in inner or (old and old["size"] == st.st_size and old["mtime"] == st.st_mtime
                            and now - st.st_mtime > STABLE_S):
            continue
        s = describe(agent, f, st)
        if s is None:
            continue
        project = projects.detect(s["cwd"]) if s["cwd"] else None
        if modes.get(project) == "off":
            continue
        _index_one(conn, s, project, pricer)
        changed += 1
    _progress["done"] = len(found)
    return dict(progress(), sessions=len(found), changed=changed)


def _index_one(conn, s: dict, project: Optional[str], pricer: Pricer) -> None:
    sid = s["session_id"]
    now = time.time()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sessions(session_id, transcript_path, cwd, first_seen, last_seen, project, agent) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET transcript_path = excluded.transcript_path, "
            "cwd = excluded.cwd, last_seen = excluded.last_seen, project = excluded.project, agent = excluded.agent",
            (sid, str(s["path"]), s["cwd"], now, now, project, s["agent"]))
        conn.execute("INSERT INTO indexed(session_id, agent, path) VALUES(?,?,?) ON CONFLICT(session_id) DO NOTHING",
                     (sid, s["agent"], str(s["path"])))
    ingest.ingest_session(conn, sid, str(s["path"]))
    _scan_prompts(conn, sid, s["agent"], s["path"])
    if s["agent"] == "claude":
        from . import reconcile
        reconcile.refresh(conn, sid)
    summ = summarize(conn, sid, s["agent"], pricer)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO indexed(session_id, agent, path, cwd, project, title, size, mtime, summary_json, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET agent = excluded.agent, "
            "path = excluded.path, cwd = excluded.cwd, project = excluded.project, "
            "title = COALESCE(excluded.title, indexed.title), size = excluded.size, mtime = excluded.mtime, "
            "summary_json = excluded.summary_json, indexed_at = excluded.indexed_at",
            (sid, s["agent"], str(s["path"]), s["cwd"], project, s["title"], s["size"], s["mtime"],
             json.dumps(summ) if summ else None, now))


def _claude_prompt(rec: dict):
    """Text of a user prompt line; None for tool results, notifications of
    background agents, meta lines and other turns the user did not type."""
    if rec.get("isMeta") or rec.get("isSidechain") or "toolUseResult" in rec:
        return None
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str):
        return None
    t = c.strip()
    if t.startswith("<command-"):                       # a slash command the user typed
        parts = {k: v.strip() for k, v in _CMD_RE.findall(t)}
        return " ".join(x for x in (parts.get("name") or parts.get("message"), parts.get("args")) if x) or t
    if t.startswith("<"):                               # <task-notification>, <local-command-…>, …
        return None
    return t or "[image]"


def _scan_prompts(conn, sid: str, agent: str, path: Path) -> None:
    """Record the prompts of a whole session (prompts table, task_id NULL),
    read incrementally from the main transcript."""
    row = conn.execute("SELECT prompt_offset, prompt_turn FROM indexed WHERE session_id = ?", (sid,)).fetchone()
    off, turn = (row["prompt_offset"], row["prompt_turn"]) if row else (0, None)
    try:
        size = path.stat().st_size
        if size < off:
            off, turn = 0, None                         # rewritten
        with open(path, "rb") as fh:
            fh.seek(off)
            data = fh.read(size - off)
    except OSError:
        return
    end = data.rfind(b"\n")
    if end < 0:
        return
    found = []
    for line in data[: end + 1].split(b"\n"):
        if agent == "claude":
            if b'"promptId"' not in line or b'"user"' not in line:
                continue
            rec = ingest._loads(line)
            if rec and rec.get("type") == "user" and rec.get("promptId"):
                text = _claude_prompt(rec)
                if text is not None:
                    found.append((rec["promptId"], ingest.parse_ts(rec.get("timestamp")), text))
        elif agent == "codex":
            if not (b'"turn_id"' in line or b'"user_message"' in line):
                continue
            rec = ingest._loads(line)
            p = (rec or {}).get("payload") if isinstance((rec or {}).get("payload"), dict) else {}
            if p.get("turn_id"):
                turn = p["turn_id"]
            if p.get("type") == "user_message" and turn:
                found.append((turn, ingest.parse_ts(rec.get("timestamp")), p.get("message")))
        else:
            if b'"user"' not in line:
                continue
            rec = ingest._loads(line)
            msg = (rec or {}).get("message") if isinstance((rec or {}).get("message"), dict) else {}
            if rec and rec.get("type") == "message" and msg.get("role") == "user" and rec.get("id"):
                c = msg.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                ts = ingest.parse_ts(rec.get("timestamp"))
                if ts is None and isinstance(msg.get("timestamp"), (int, float)):
                    ts = msg["timestamp"] / 1000.0
                found.append((rec["id"], ts, c if isinstance(c, str) else ""))
    with db.transaction(conn):
        have = {r[0] for r in conn.execute(
            "SELECT prompt_id FROM prompts WHERE session_id = ? AND task_id IS NULL", (sid,))}
        for pid, ts, text in found:
            if pid in have or ts is None:
                continue
            have.add(pid)
            conn.execute("INSERT INTO prompts(task_id, session_id, ts, prompt, prompt_id) VALUES(NULL,?,?,?,?)",
                         (sid, ts, settings.store_prompt(text if isinstance(text, str) else ""), pid))
        conn.execute("UPDATE indexed SET prompt_offset = ?, prompt_turn = ? WHERE session_id = ?",
                     (off + end + 1, turn, sid))


def summarize(conn, sid: str, agent: str, pricer: Optional[Pricer] = None) -> Optional[dict]:
    """Totals of one indexed session: tokens, cost, calls, prompts, agents,
    models, wall and active time, and Claude Code's unlogged calls."""
    from . import reconcile, report
    pricer = pricer or Pricer(paths.load_config().get("pricing"))
    rows = conn.execute("SELECT * FROM usage WHERE session_id = ?", (sid,)).fetchall()
    if not rows:
        return None
    tot = report.Tot()
    models = {}
    agents_seen, turns_seen = set(), set()
    first = last = None
    for r in rows:
        c = pricer.row_cost(r)
        tot.add_row(r, c)
        models.setdefault(normalize_model(r["model"]) or "unknown", report.Tot()).add_row(r, c)
        if r["agent_id"]:
            agents_seen.add(r["agent_id"])
        if r["prompt_id"]:
            turns_seen.add(r["prompt_id"])
        first = r["ts"] if first is None or r["ts"] < first else first
        last = r["ts"] if last is None or r["ts"] > last else last
    prompts = set()
    for pid, ts in conn.execute("SELECT prompt_id, ts FROM prompts WHERE session_id = ? AND task_id IS NULL", (sid,)):
        prompts.add(pid)
        first = min(first, ts)                          # the session starts with its first prompt
    turns = report._turn_intervals(conn, sid, first - 3600, last + 1)
    unlogged = None
    if agent == "claude":
        u = reconcile.estimate(conn, [(sid, first - 1, last + 1)], pricer)
        if u["status"] != "pending" and (u["total"] or u["cost"]):
            unlogged = {"total": u["total"], "cost": u["cost"], "status": u["status"]}
    totals = tot.as_dict()
    if unlogged:
        totals["cost_with_unlogged"] = round(totals["cost"] + unlogged["cost"], 6)
    return {
        "first_ts": first, "last_ts": last, "wall_s": last - first,
        "active_s": report._union_length(turns) if turns else None,
        "prompts": len(prompts), "automatic_turns": len(turns_seen - prompts), "agents": len(agents_seen),
        "totals": totals, "unlogged": unlogged,
        "models": sorted(({"model": m, **t.as_dict()} for m, t in models.items()), key=lambda d: -d["total"]),
    }


# ---------------------------------------------------------------- queries

def overview(days: Optional[float] = 30, agent: Optional[str] = None, project: Optional[str] = None,
             limit: Optional[int] = None, conn=None) -> dict:
    """Indexed sessions with activity in the last `days` days, newest first,
    with totals per agent and overall."""
    own = conn is None
    conn = conn or connect()
    try:
        main = db.connect()
        try:
            tasks_by_sid = {}
            for r in main.execute("SELECT DISTINCT s.session_id, t.id, t.name FROM task_sessions s "
                                  "JOIN tasks t ON t.id = s.task_id ORDER BY t.id"):
                tasks_by_sid.setdefault(r["session_id"], []).append({"id": r["id"], "name": r["name"]})
            off = {r[0] for r in main.execute("SELECT name FROM projects WHERE mode = 'off'")}
        finally:
            main.close()
        since = time.time() - days * 86400 if days else 0
        out: List[dict] = []
        for r in conn.execute("SELECT * FROM indexed WHERE summary_json IS NOT NULL"):
            if (agent and r["agent"] != agent) or (project and r["project"] != project) or r["project"] in off:
                continue
            s = json.loads(r["summary_json"])
            if s["last_ts"] < since:
                continue
            out.append({"session_id": r["session_id"], "agent": r["agent"], "project": r["project"],
                        "cwd": r["cwd"], "title": r["title"], "tasks": tasks_by_sid.get(r["session_id"], []), **s})
        out.sort(key=lambda d: -d["last_ts"])
        by_agent = {}
        overall = _zero()
        for s in out:
            for d in (by_agent.setdefault(s["agent"], _zero()), overall):
                t = s["totals"]
                d["sessions"] += 1
                for k in ("total", "input", "output", "cache_read", "cache_write", "calls"):
                    d[k] += t[k]
                d["cost"] += t.get("cost_with_unlogged", t["cost"])
                d["prompts"] += s["prompts"]
                d["wall_s"] += s["wall_s"]
                d["active_s"] += s["active_s"] or 0
        for d in [overall, *by_agent.values()]:
            d["cost"] = round(d["cost"], 6)
        return {"sessions": out[:limit] if limit else out, "count": len(out), "totals": overall,
                "by_agent": [{"agent": a, **d} for a, d in sorted(by_agent.items(), key=lambda kv: -kv[1]["total"])],
                "days": days, "indexed_at": conn.execute("SELECT MAX(indexed_at) FROM indexed").fetchone()[0]}
    finally:
        if own:
            conn.close()


def _zero() -> dict:
    return {"sessions": 0, "total": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "calls": 0, "cost": 0.0, "prompts": 0, "wall_s": 0.0, "active_s": 0.0}


def detail(session_id: str, conn=None) -> Optional[dict]:
    """Full report of one indexed session (same shape as a task report)."""
    from . import report
    own = conn is None
    conn = conn or connect()
    try:
        r = conn.execute("SELECT * FROM indexed WHERE session_id = ?", (session_id,)).fetchone()
        if r is None:
            return None
        if r["summary_json"] is None:
            return {"session": dict(r), "empty": True}
        s = json.loads(r["summary_json"])
        a, b = s["first_ts"], s["last_ts"]
        whole = {"id": 0, "name": r["title"] or "session " + session_id, "initial_prompt": None, "status": "ended",
                 "started_at": a - 1, "ended_at": b + 1, "cwd": r["cwd"], "created_by": "session",
                 "end_note": None, "project": r["project"]}
        rep = report.build(conn, whole, windows=[(session_id, a - 1, b + 1)])
        rep["session"] = {k: r[k] for k in ("session_id", "agent", "project", "cwd", "title", "path")}
        return rep
    finally:
        if own:
            conn.close()


def locate(session_id: str) -> Optional[Path]:
    """Transcript of a session id, for any agent (read-only lookups)."""
    from . import codex, installer
    main = db.connect()
    try:
        row = main.execute("SELECT transcript_path FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        main.close()
    if row and row[0] and Path(row[0]).is_file():
        return Path(row[0])
    p = ingest.find_transcript(session_id) or codex.find_rollout(session_id)
    if p is None:
        root = installer.pi_home() / "sessions"
        p = next(iter(root.glob("*/*_%s.jsonl" % session_id)), None) if root.is_dir() else None
    return p


def index_session(session_id: str, path: Optional[str] = None) -> Optional[dict]:
    """Index one session now (tally session <id>); returns its description.
    An id prefix of an indexed session works too."""
    p = Path(path).expanduser() if path else locate(session_id)
    if p is None and not path:
        conn = connect()
        try:
            hits = conn.execute("SELECT path FROM indexed WHERE session_id LIKE ? || '%' LIMIT 2",
                                (session_id,)).fetchall()
        finally:
            conn.close()
        p = Path(hits[0][0]) if len(hits) == 1 else None
    if p is None or not p.is_file():
        return None
    s = describe(_sniff(p), p, p.stat())
    if s is None:
        return None
    conn = connect()
    try:
        _index_one(conn, s, projects.detect(s["cwd"]) if s["cwd"] else None,
                   Pricer(paths.load_config().get("pricing")))
    finally:
        conn.close()
    return s
