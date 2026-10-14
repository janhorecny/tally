"""Codex CLI (openai/codex) rollout reader.

Rollouts: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread-uuid>.jsonl,
one line {"timestamp", "ordinal"?, "type", "payload"} per item:

  session_meta  {session_id (root thread), id (this thread), cwd, parent_thread_id,
                 source: "cli" | {"subagent": {"thread_spawn": {...}}}, agent_nickname, agent_role,
                 subagent_history_start_ordinal?}
  turn_context  {turn_id, model, ...}                      model of the turn
  event_msg     {type: "turn_started", turn_id}            a turn (prompt) begins
  event_msg     {type: "token_count", info: {total_token_usage: {...}, last_token_usage: {...}}}
  response_item {type: "function_call" | "custom_tool_call" | "local_shell_call", call_id, name}

total_token_usage is cumulative per thread and token_count events can repeat,
so usage is taken as the difference to the previous total. input_tokens
includes cached_input_tokens; reasoning_output_tokens is part of output_tokens.
Subagents are separate threads (own rollout files) whose session_meta names
the root thread in session_id.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import db, ingest

_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
_TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def _first_meta(path: Path) -> Optional[dict]:
    try:
        with open(path, "rb") as fh:
            line = fh.readline(4 * 1024 * 1024)
    except OSError:
        return None
    rec = ingest._loads(line)
    if rec and rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
        return rec["payload"]
    return None


def session_files(conn, session_id: str, main: Path) -> list:
    """The thread's own rollout plus the rollouts of its subagent threads."""
    out = [(main, "", None)]
    meta = _first_meta(main) or {}
    root = meta.get("session_id") or meta.get("id") or session_id
    own = meta.get("id") or session_id
    try:
        day = datetime.strptime("/".join(main.parts[-4:-1]), "%Y/%m/%d")
        base = main.parents[3]
    except (ValueError, IndexError):
        return out
    known = {r[0]: r[1] for r in conn.execute(
        "SELECT path, agent_id FROM files WHERE session_id = ?", (session_id,))}
    now = datetime.fromtimestamp(time.time())
    end = now + timedelta(days=1)
    # thread ids of other rollouts, so their first line (which can be large) is not read on every hook
    threads = _load_threads(conn)
    cutoff = (now - timedelta(days=_THREADS_DAYS)).strftime("%Y-%m-%d")
    added = False
    d = day
    while d <= end and (d - day).days <= 7:
        folder = base / d.strftime("%Y/%m/%d")
        if folder.is_dir():
            for f in sorted(folder.glob("rollout-*.jsonl")):
                if f == main:
                    continue
                if str(f) in known:
                    out.append((f, known[str(f)], None))
                    continue
                m = None
                ids = threads.get(f.name)
                if not (isinstance(ids, list) and len(ids) == 2):
                    m = _first_meta(f)
                    if not m:
                        continue                 # first line not complete yet: look again next time
                    ids = [m.get("id"), m.get("session_id") or m.get("id")]
                if ids[0] in (None, own) or ids[1] != root:
                    if f.name not in threads and f.name[8:18] >= cutoff:
                        threads[f.name] = ids
                        added = True
                    continue
                m = m or _first_meta(f)
                if not m:
                    continue
                agent_id = m["id"]
                _register_subagent(conn, session_id, agent_id, m, f, own)
                out.append((f, agent_id, None))
        d += timedelta(days=1)
    if added:
        db.kv_set(conn, _THREADS_KEY, json.dumps(
            {k: v for k, v in threads.items() if k[8:18] >= cutoff}, separators=(",", ":")))
    return out


_THREADS_KEY = "codex_threads"       # rollout file name -> [thread id, root thread id]
_THREADS_DAYS = 15                   # rollouts older than this are not remembered


def _load_threads(conn) -> dict:
    try:
        threads = json.loads(db.kv_get(conn, _THREADS_KEY) or "{}")
    except ValueError:
        return {}
    return threads if isinstance(threads, dict) else {}


def _register_subagent(conn, session_id, agent_id, m: dict, path: Path, own: str) -> None:
    src = m.get("source") if isinstance(m.get("source"), dict) else {}
    spawn = ((src.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(src.get("subagent"), dict) else {}
    if not isinstance(spawn, dict):
        spawn = {}
    role = m.get("agent_role") or spawn.get("agent_role")
    nick = m.get("agent_nickname") or spawn.get("agent_nickname") or m.get("agent_path") or spawn.get("agent_path")
    parent = m.get("parent_thread_id") or spawn.get("parent_thread_id")
    conn.execute(
        "INSERT INTO agents(session_id, agent_id, agent_type, description, parent_agent_id, spawn_depth, "
        "transcript_path) VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id, agent_id) DO UPDATE SET "
        "agent_type = excluded.agent_type, description = excluded.description",
        ingest._bind((session_id, agent_id, role or (m.get("thread_source") or "subagent"), nick,
                      parent if parent and parent != own else None, spawn.get("depth"), str(path))))


def _usage(info: dict) -> Optional[dict]:
    tot = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(tot, dict):
        return None
    out = {}
    for f in _FIELDS:
        try:
            out[f] = int(tot.get(f) or 0)
        except (TypeError, ValueError):
            out[f] = 0
    return out


def tail_state(path: Path, data: bytes, size: Optional[int] = None):
    """Baseline at the end of a file: last cumulative total, model, turn.

    data is the file's tail (the whole file up to `size` if shorter). A tail
    without a token_count (a long tool output at the end) has no baseline:
    the file is then scanned from its start, or the next call would count
    the thread's whole history."""
    state = {}
    meta = _first_meta(path) or {}
    if meta.get("subagent_history_start_ordinal") is not None:
        state["hist_start"] = meta["subagent_history_start_ordinal"]
    prompt_id = _scan_baseline(data.split(b"\n"), state)
    if "prev" not in state and size is not None and len(data) < size:
        prompt_id = _scan_baseline(_lines_upto(path, size), state)
    return prompt_id, state


def _lines_upto(path: Path, size: int):
    """Complete lines of the first `size` bytes of a file."""
    pos = 0
    try:
        with open(path, "rb") as fh:
            for line in fh:
                pos += len(line)
                if pos > size:
                    return
                yield line
    except OSError:
        return


def _scan_baseline(lines, state: dict) -> Optional[str]:
    prompt_id = None
    for line in lines:
        if not (b'"token_count"' in line or b'"turn_context"' in line or b'"turn_started"' in line):
            continue
        rec = ingest._loads(line)
        if not rec:
            continue
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("type") == "turn_context":
            state["model"] = p.get("model") or state.get("model")
            prompt_id = p.get("turn_id") or prompt_id
        elif p.get("type") == "turn_started":
            prompt_id = p.get("turn_id") or prompt_id
        elif p.get("type") == "token_count":
            u = _usage(p.get("info"))
            if u:
                state["prev"] = u
    return prompt_id


def process(conn, session_id: str, file_agent_id: str, chunk: bytes, stats, prompt_id=None,
            want_tools: bool = False, state: Optional[dict] = None):
    state = state if state is not None else {}
    usage_rows, tool_rows = [], []
    for line in chunk.split(b"\n"):
        if not line:
            continue
        if not (b'"token_count"' in line or b'"turn_context"' in line or b'"turn_started"' in line
                or b'"session_meta"' in line or (want_tools and b'"response_item"' in line)):
            continue
        try:
            rec = ingest._loads(line)
            if rec is None:
                continue
            typ = rec.get("type")
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if typ == "session_meta":
                if p.get("subagent_history_start_ordinal") is not None:
                    state["hist_start"] = p["subagent_history_start_ordinal"]
                continue
            if typ == "turn_context":
                state["model"] = p.get("model") or state.get("model")
                prompt_id = p.get("turn_id") or prompt_id
                continue
            if typ == "event_msg" and p.get("type") == "turn_started":
                prompt_id = p.get("turn_id") or prompt_id
                continue
            ts = ingest.parse_ts(rec.get("timestamp"))
            if typ == "event_msg" and p.get("type") == "token_count":
                u = _usage(p.get("info"))
                if u is None:
                    continue                                 # rate-limit-only event
                prev = state.get("prev") or {f: 0 for f in _FIELDS}
                inherited = (state.get("hist_start") is not None and isinstance(rec.get("ordinal"), int)
                             and rec["ordinal"] < state["hist_start"])
                if any(u[f] < prev.get(f, 0) for f in _FIELDS):
                    prev = {f: 0 for f in _FIELDS}           # counter reset (new thread state)
                delta = {f: u[f] - prev.get(f, 0) for f in _FIELDS}
                state["prev"] = u
                if inherited or ts is None or not any(delta.values()):
                    continue                                 # forked history, or a repeated total
                cached = delta["cached_input_tokens"]
                write = delta["cache_write_input_tokens"]
                fresh = max(delta["input_tokens"] - cached - write, 0)
                key = "codex:%s:%s:%d:%d" % (session_id, file_agent_id or "main", u["input_tokens"],
                                             u["output_tokens"])
                usage_rows.append((key, session_id, file_agent_id, ts, state.get("model"), None, fresh,
                                   delta["output_tokens"], cached, write, 0, 0, prompt_id, None))
            elif want_tools and typ == "response_item" and p.get("type") in _TOOL_TYPES and ts is not None:
                call_id = p.get("call_id") or p.get("id")
                if call_id:
                    name = p.get("name") or {"local_shell_call": "shell", "web_search_call": "web_search"}.get(
                        p.get("type"), p.get("type"))
                    tool_rows.append((call_id, session_id, file_agent_id, prompt_id, ts, str(name)))
        except Exception as exc:
            if not ingest._skippable(exc):
                raise
            stats.skip()
    if usage_rows:
        stats.usage_rows += ingest.insert_usage(conn, usage_rows, stats)
    if tool_rows:
        ingest.insert_tools(conn, tool_rows, stats)
    if file_agent_id and usage_rows:
        lo = min(r[3] for r in usage_rows)
        hi = max(r[3] for r in usage_rows)
        conn.execute("UPDATE agents SET first_ts = MIN(COALESCE(first_ts, ?), ?), last_ts = MAX(COALESCE(last_ts, ?), ?) "
                     "WHERE session_id = ? AND agent_id = ?",
                     ingest._bind((lo, lo, hi, hi, session_id, file_agent_id)))
    return prompt_id


def find_rollout(session_id: str) -> Optional[Path]:
    """Rollout file of a thread id (newest first, last 14 days)."""
    root = codex_home() / "sessions"
    if not root.is_dir():
        return None
    today = datetime.fromtimestamp(time.time())
    for i in range(0, 15):
        folder = root / (today - timedelta(days=i)).strftime("%Y/%m/%d")
        if folder.is_dir():
            for f in folder.glob("rollout-*%s*.jsonl" % session_id):
                return f
    return None


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
