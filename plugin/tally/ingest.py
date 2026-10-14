"""Incremental ingest of Claude Code transcripts into SQLite.

Layout of a session on disk (Claude Code 2.1.x):

    ~/.claude/projects/<project>/<session>.jsonl                      main session
    ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl  subagent
    ~/.claude/projects/<project>/<session>/subagents/agent-<id>.meta.json
    ~/.claude/projects/<project>/<session>/subagents/workflows/<run>/agent-<id>.jsonl
    ~/.claude/projects/<project>/<session>/workflows/<run>.json         workflow run record

Every assistant line carries `message.usage`. One API response is written as
several lines (one per content block) with identical usage, so rows are keyed
by message id + request id.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from . import db, paths

_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
# user lines carry the id of the prompt they belong to as their 3rd key
_PROMPT_RE = re.compile(rb'"promptId"\s*:\s*"([^"]+)"')
_PROMPT_SCAN = 600
_TOOL_ERROR_RE = re.compile(rb'"is_error"\s*:\s*true')

USAGE_INSERT = (
    "INSERT OR REPLACE INTO usage(msg_key, session_id, agent_id, ts, model, speed, input, output, cache_read, "
    "cache_write_5m, cache_write_1h, web_search, prompt_id, cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def parse_ts(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    # fromisoformat in 3.9 wants 3 or 6 fractional digits
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", s)
    if m:
        frac = (m.group(2) or "0")[:6].ljust(6, "0")
        try:
            return datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, m.group(3) or "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- locating

def find_transcript(session_id: str) -> Optional[Path]:
    root = paths.projects_dir()
    if not root.is_dir():
        return None
    name = session_id + ".jsonl"
    for proj in root.iterdir():
        cand = proj / name
        if cand.is_file():
            return cand
    return None


def resolve_transcript(conn, session_id: str, hint: Optional[str] = None) -> Optional[Path]:
    if hint:
        p = Path(hint).expanduser()
        if p.is_file():
            return p
    row = conn.execute(
        "SELECT transcript_path FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row and row[0] and Path(row[0]).is_file():
        return Path(row[0])
    found = find_transcript(session_id)
    if found is None and session_agent(conn, session_id) == "codex":
        from . import codex
        found = codex.find_rollout(session_id)
    if found:
        conn.execute(
            "INSERT INTO sessions(session_id, transcript_path, first_seen, last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET transcript_path = excluded.transcript_path",
            (session_id, str(found), time.time(), time.time()),
        )
    return found


def session_files(main: Path) -> List[Tuple[Path, str, Optional[str]]]:
    """Return (path, agent_id, workflow_run_id) for main + every agent transcript."""
    out: List[Tuple[Path, str, Optional[str]]] = []
    if main.is_file():
        out.append((main, "", None))
    sub = main.with_suffix("") / "subagents"
    if sub.is_dir():
        for p in sorted(sub.rglob("agent-*.jsonl")):
            agent_id = p.stem[len("agent-"):]
            run_id = None
            if p.parent.parent.name == "workflows" and p.parent.name.startswith("wf_"):
                run_id = p.parent.name
            out.append((p, agent_id, run_id))
    return out


# ---------------------------------------------------------------- ingest

class Stats:
    def __init__(self):
        self.files = 0
        self.bytes = 0
        self.usage_rows = 0
        self.turns = 0

    def as_dict(self):
        return dict(files=self.files, bytes=self.bytes, usage_rows=self.usage_rows, turns=self.turns)


def ingest_session(conn, session_id: str, transcript_path: Optional[str] = None,
                   skip_existing: bool = False, backfill_tools: bool = True) -> Stats:
    """Read new transcript bytes for a session (main + all agents).

    skip_existing=True moves every file's offset to its current end without
    parsing; used when a task starts so history before the task is not read.
    backfill_tools=False: bytes read before tool calls were wanted stay without
    them (the sessions index, which would otherwise reread every transcript).
    """
    from . import settings
    stats = Stats()
    main = resolve_transcript(conn, session_id, transcript_path)
    if main is None:
        return stats
    agent = session_agent(conn, session_id)
    sdir = main.with_suffix("")
    want_tools = settings.depth_at_least("calls")
    with db.transaction(conn):
        if agent == "claude":
            files = session_files(main)
        elif agent == "codex":
            from . import codex
            files = codex.session_files(conn, session_id, main)
        else:
            files = [(main, "", None)]
        for path, agent_id, run_id in files:
            _ingest_file(conn, session_id, path, agent_id, run_id, skip_existing, stats, want_tools, agent,
                         backfill_tools)
        if agent == "claude":
            _load_workflow_records(conn, session_id, sdir)
    return stats


def session_agent(conn, session_id: str) -> str:
    row = conn.execute("SELECT agent FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return (row[0] if row and row[0] else None) or "claude"


def _ingest_file(conn, session_id, path: Path, agent_id: str, run_id: Optional[str],
                 skip_existing: bool, stats: Stats, want_tools: bool = False, agent: str = "claude",
                 backfill_tools: bool = True) -> None:
    key = str(path)
    row = conn.execute("SELECT offset, meta_loaded, last_prompt_id, state, tools_offset FROM files WHERE path = ?",
                       (key,)).fetchone()
    try:
        size = path.stat().st_size
    except OSError:
        return
    if row is None:
        offset = size if skip_existing else 0
        conn.execute(
            "INSERT INTO files(path, session_id, agent_id, workflow_run_id, offset, tools_offset) VALUES(?,?,?,?,?,?)",
            (key, session_id, agent_id, run_id, 0, 0),
        )
        meta_loaded = 0
        tools_at = 0
    else:
        offset = row["offset"]
        meta_loaded = row["meta_loaded"]
        tools_at = row["tools_offset"] or 0
        if (want_tools and tools_at < offset <= size and not skip_existing and backfill_tools
                and agent == "claude"):
            _backfill_tools(conn, session_id, path, agent_id, tools_at, offset)
            tools_at = offset
        if skip_existing:
            offset = max(offset, size)
    if agent_id and not meta_loaded:
        if _load_agent_meta(conn, session_id, path, agent_id, run_id):
            conn.execute("UPDATE files SET meta_loaded = 1 WHERE path = ?", (key,))
    if size < offset:          # file was rewritten; start over
        offset = tools_at = 0
    if skip_existing and want_tools:
        tools_at = max(tools_at, size)      # skipped on purpose: nothing to fill in later
    if size == offset:
        conn.execute("UPDATE files SET offset = ?, tools_offset = ? WHERE path = ?", (offset, tools_at, key))
        return
    if skip_existing:
        # Skipped bytes are not parsed, but lines appended later still need to
        # know which prompt they belong to: take the last one in the file tail.
        from . import adapters
        if agent == "claude":
            tail_prompt, tail_state = _last_prompt_in_tail(path, size), None
        else:
            tail_prompt, tail_state = adapters.tail_state(agent, path, size)
        conn.execute("UPDATE files SET offset = ?, last_prompt_id = COALESCE(?, last_prompt_id), "
                     "state = COALESCE(?, state), tools_offset = ? WHERE path = ?",
                     (size, tail_prompt, json.dumps(tail_state) if tail_state else None, tools_at, key))
        return
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(size - offset)
    end = data.rfind(b"\n")
    if end < 0:
        return                 # no complete line yet
    chunk = data[: end + 1]
    stats.files += 1
    stats.bytes += len(chunk)
    from . import adapters
    process = adapters.chunk_processor(agent) or _process_chunk
    state = {}
    if row is not None and row["state"]:
        try:
            state = json.loads(row["state"]) or {}
        except ValueError:
            state = {}
    prompt_id = process(conn, session_id, agent_id, chunk, stats,
                        row["last_prompt_id"] if row is not None else None, want_tools, state)
    if want_tools and tools_at >= offset:   # no gap before this chunk: tool calls are complete up to its end
        tools_at = offset + len(chunk)
    conn.execute("UPDATE files SET offset = ?, last_prompt_id = ?, state = ?, tools_offset = ? WHERE path = ?",
                 (offset + len(chunk), prompt_id, json.dumps(state) if state else None, tools_at, key))


def _backfill_tools(conn, session_id: str, path: Path, file_agent_id: str, start: int, upto: int) -> None:
    """Tool calls in bytes start..upto, read without them: while log.depth was
    below calls (the default before 0.11.7), or by an older Tally's hook that
    is still running. Lines before `start` only give the prompt in effect."""
    rows = []
    prompt_id = None
    pos = 0
    with open(path, "rb") as fh:
        for line in fh:
            pos += len(line)
            if pos > upto:
                break
            pm = _PROMPT_RE.search(line, 0, _PROMPT_SCAN)
            if pm:
                prompt_id = pm.group(1).decode("ascii", "ignore")
            if pos <= start:
                continue
            use = b'"tool_use"' in line and b'"usage"' in line
            err = b'"tool_result"' in line and _TOOL_ERROR_RE.search(line) is not None
            if not (use or err):
                continue
            rec = _loads(line)
            if rec is None:
                continue
            if use and rec.get("type") == "assistant":
                row = _usage_row(rec, session_id, file_agent_id, prompt_id)
                if row:
                    rows.extend(_tool_uses(rec, session_id, row[2], prompt_id, row[3]))
            elif err and rec.get("type") == "user":
                if rows:                          # the failed call comes before its result
                    _insert_tools(conn, rows)
                    rows = []
                _mark_tool_errors(conn, rec)
    if rows:
        _insert_tools(conn, rows)


def _last_prompt_in_tail(path: Path, size: int, tail: int = 512 * 1024) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail))
            data = fh.read(min(size, tail))
    except OSError:
        return None
    found = None
    for line in data.split(b"\n"):
        m = _PROMPT_RE.search(line, 0, _PROMPT_SCAN)
        if m:
            found = m.group(1).decode("ascii", "ignore")
    return found


def _process_chunk(conn, session_id: str, file_agent_id: str, chunk: bytes, stats: Stats,
                   prompt_id: Optional[str] = None, want_tools: bool = False,
                   state: Optional[dict] = None) -> Optional[str]:
    """Parse complete lines; returns the prompt id in effect at the end."""
    first_ts = last_ts = None
    usage_rows = []
    tool_rows = []
    for line in chunk.split(b"\n"):
        if not line:
            continue
        pm = _PROMPT_RE.search(line, 0, _PROMPT_SCAN)
        if pm:
            prompt_id = pm.group(1).decode("ascii", "ignore")
        m = _TS_RE.search(line)
        if m:
            ts = parse_ts(m.group(1).decode("ascii", "ignore"))
            if ts is not None:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
        # Cheap substring checks first; only candidate lines are JSON-parsed,
        # then routed by their actual type (a line can match several checks).
        tool_error = want_tools and b'"tool_result"' in line and _TOOL_ERROR_RE.search(line) is not None
        if not ((b'"usage"' in line and b'"assistant"' in line) or b'"turn_duration"' in line
                or (b'"workflowName"' in line and b'"runId"' in line) or tool_error):
            continue
        rec = _loads(line)
        if rec is None:
            continue
        typ = rec.get("type")
        if typ == "assistant":
            row = _usage_row(rec, session_id, file_agent_id, prompt_id)
            if row:
                usage_rows.append(row)
                if want_tools:
                    tool_rows.extend(_tool_uses(rec, session_id, row[2], prompt_id, row[3]))
        elif typ == "user" and tool_error:
            if tool_rows:                     # the failed call may be in this same chunk
                _insert_tools(conn, tool_rows)
                tool_rows = []
            _mark_tool_errors(conn, rec)
        elif typ == "system" and rec.get("subtype") == "turn_duration":
            ts = parse_ts(rec.get("timestamp"))
            dur = rec.get("durationMs")
            if (ts is not None and isinstance(dur, (int, float)) and not file_agent_id
                    and not rec.get("isSidechain")):
                conn.execute(
                    "INSERT OR REPLACE INTO turns(uuid, session_id, ts_end, duration_ms) VALUES(?,?,?,?)",
                    (rec.get("uuid") or "%s:%s" % (session_id, ts), session_id, ts, int(dur)),
                )
                stats.turns += 1
        res = rec.get("toolUseResult")
        if isinstance(res, dict) and res.get("runId") and res.get("workflowName"):
            _upsert_workflow(conn, session_id, res, parse_ts(rec.get("timestamp")))
    if usage_rows:
        conn.executemany(
            USAGE_INSERT, [r + (None,) if len(r) == 13 else r for r in usage_rows],
        )
        stats.usage_rows += len(usage_rows)
    if tool_rows:
        _insert_tools(conn, tool_rows)
    if file_agent_id and first_ts is not None:
        conn.execute(
            "INSERT INTO agents(session_id, agent_id, first_ts, last_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id, agent_id) DO UPDATE SET "
            "first_ts = MIN(COALESCE(agents.first_ts, excluded.first_ts), excluded.first_ts), "
            "last_ts = MAX(COALESCE(agents.last_ts, excluded.last_ts), excluded.last_ts)",
            (session_id, file_agent_id, first_ts, last_ts),
        )
    return prompt_id


def insert_tools(conn, rows: list) -> None:
    _insert_tools(conn, rows)


def _insert_tools(conn, rows: list) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO tool_calls(id, session_id, agent_id, prompt_id, ts, tool) VALUES(?,?,?,?,?,?)", rows)


def _tool_uses(rec: dict, session_id: str, agent_id: str, prompt_id: Optional[str], ts: float) -> list:
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [(c["id"], session_id, agent_id, prompt_id, ts, str(c.get("name") or "?"))
            for c in content
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id")]


def _mark_tool_errors(conn, rec: dict) -> None:
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error") and c.get("tool_use_id"):
            conn.execute("UPDATE tool_calls SET is_error = 1 WHERE id = ?", (c["tool_use_id"],))


def _loads(line: bytes):
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _usage_row(rec: dict, session_id: str, file_agent_id: str, prompt_id: Optional[str] = None):
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    model = msg.get("model")
    if model == "<synthetic>":
        return None
    ts = parse_ts(rec.get("timestamp"))
    if ts is None:
        return None
    mid = msg.get("id") or rec.get("uuid")
    if not mid:
        return None
    key = "%s:%s" % (mid, rec.get("requestId") or "")
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    total_write = _int(u.get("cache_creation_input_tokens"))
    w1h = _int(cc.get("ephemeral_1h_input_tokens"))
    w5m = cc.get("ephemeral_5m_input_tokens")
    w5m = _int(w5m) if w5m is not None else max(total_write - w1h, 0)
    if w5m + w1h < total_write:
        w5m = total_write - w1h
    stu = u.get("server_tool_use") if isinstance(u.get("server_tool_use"), dict) else {}
    agent_id = rec.get("agentId") or file_agent_id or ""
    if agent_id and not rec.get("isSidechain") and not file_agent_id:
        agent_id = ""          # main-session line that merely mentions an agent
    return (
        key, session_id, agent_id, ts, model, u.get("speed"),
        _int(u.get("input_tokens")), _int(u.get("output_tokens")),
        _int(u.get("cache_read_input_tokens")), w5m, w1h,
        _int(stu.get("web_search_requests")), prompt_id,
    )


def _load_agent_meta(conn, session_id, path: Path, agent_id: str, run_id: Optional[str]) -> bool:
    meta_path = path.with_name("agent-%s.meta.json" % agent_id)
    meta = {}
    loaded = False
    if meta_path.is_file():
        try:
            with open(meta_path) as fh:
                meta = json.load(fh) or {}
            loaded = True
        except (OSError, ValueError):
            meta = {}
    conn.execute(
        "INSERT INTO agents(session_id, agent_id, agent_type, description, parent_agent_id, tool_use_id, "
        "workflow_run_id, workflow_phase, model, spawn_depth, is_fork, transcript_path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id, agent_id) DO UPDATE SET "
        "agent_type = COALESCE(excluded.agent_type, agents.agent_type), "
        "description = COALESCE(excluded.description, agents.description), "
        "parent_agent_id = COALESCE(excluded.parent_agent_id, agents.parent_agent_id), "
        "tool_use_id = COALESCE(excluded.tool_use_id, agents.tool_use_id), "
        "workflow_run_id = COALESCE(excluded.workflow_run_id, agents.workflow_run_id), "
        "workflow_phase = COALESCE(excluded.workflow_phase, agents.workflow_phase), "
        "model = COALESCE(excluded.model, agents.model), "
        "spawn_depth = COALESCE(excluded.spawn_depth, agents.spawn_depth), "
        "is_fork = COALESCE(excluded.is_fork, agents.is_fork), "
        "transcript_path = excluded.transcript_path",
        (
            session_id, agent_id,
            meta.get("agentType") or ("workflow-subagent" if run_id else None),
            meta.get("description"),
            meta.get("parentAgentId"),
            meta.get("toolUseId"),
            run_id,
            meta.get("workflowPhase"),
            meta.get("model"),
            meta.get("spawnDepth"),
            1 if meta.get("isFork") else None,
            str(path),
        ),
    )
    if run_id:
        conn.execute(
            "INSERT OR IGNORE INTO workflows(session_id, run_id) VALUES(?, ?)", (session_id, run_id)
        )
    return loaded


def _upsert_workflow(conn, session_id, res: dict, ts: Optional[float]) -> None:
    started = parse_ts(res.get("startTime")) or parse_ts(res.get("timestamp")) or ts
    conn.execute(
        "INSERT INTO workflows(session_id, run_id, name, summary, status, started_at, duration_ms, "
        "reported_total_tokens) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id, run_id) DO UPDATE SET "
        "name = COALESCE(excluded.name, workflows.name), "
        "summary = COALESCE(excluded.summary, workflows.summary), "
        "status = COALESCE(excluded.status, workflows.status), "
        "started_at = COALESCE(workflows.started_at, excluded.started_at), "
        "duration_ms = COALESCE(excluded.duration_ms, workflows.duration_ms), "
        "reported_total_tokens = COALESCE(excluded.reported_total_tokens, workflows.reported_total_tokens)",
        (
            session_id, res.get("runId"), res.get("workflowName"),
            (res.get("summary") or None), res.get("status"), started,
            res.get("durationMs") if isinstance(res.get("durationMs"), int) else None,
            res.get("totalTokens") if isinstance(res.get("totalTokens"), int) else None,
        ),
    )


def _load_workflow_records(conn, session_id: str, sdir: Path) -> None:
    """Pick up workflows/<run>.json records (written by Claude Code) once complete."""
    wdir = sdir / "workflows"
    if not wdir.is_dir():
        return
    known = {
        r["run_id"]: r
        for r in conn.execute(
            "SELECT run_id, name, status, reported_total_tokens FROM workflows WHERE session_id = ?",
            (session_id,),
        )
    }
    for p in wdir.glob("wf_*.json"):
        run_id = p.stem
        k = known.get(run_id)
        if k is not None and k["name"] and k["reported_total_tokens"] is not None:
            continue
        try:
            with open(p) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            rec.setdefault("runId", run_id)
            _upsert_workflow(conn, session_id, rec, None)


def ingest_sessions(conn, session_ids: Iterable[str]) -> Stats:
    total = Stats()
    for sid in session_ids:
        s = ingest_session(conn, sid)
        total.files += s.files
        total.bytes += s.bytes
        total.usage_rows += s.usage_rows
        total.turns += s.turns
    return total
