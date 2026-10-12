"""Transcript readers for agents other than Claude Code.

Each reader turns appended transcript bytes into rows of the shared tables
(usage, tool_calls, agents) so tasks, todos, projects and reports work the
same for every agent. Claude Code's reader lives in ingest.py.

pi (earendil-works/pi-coding-agent), session format v3:
  ~/.pi/agent/sessions/--<cwd>--/<ts>_<session-id>.jsonl
  {"type":"session","id":..,"cwd":..}                         header
  {"type":"message","id":..,"message":{"role":"user",..}}     a prompt
  {"type":"message","id":..,"message":{"role":"assistant","model":..,"usage":{..,"cost":{..}}}}
  {"type":"message","id":..,"message":{"role":"toolResult","toolCallId":..,"usage":{..}}}
      usage on a tool result = nested model work of the tool (subagents)
  {"type":"usage"|"compaction"|"branch_summary",..,"usage":{..}}  other model work
"""

import json
import re
from pathlib import Path
from typing import Optional

from . import ingest

AGENTS = ("claude", "codex", "pi")
_PI_PROMPT_RE = re.compile(rb'"role"\s*:\s*"user"')


def chunk_processor(agent: str):
    return {"pi": process_pi, "codex": process_codex}.get(agent)


def tail_state(agent: str, path: Path, size: int, tail: int = 1024 * 1024):
    """(prompt id, reader state) at the end of a file whose history is skipped.

    Codex token counts are cumulative per thread, so the reader must start
    from the last total, not from zero, or a task started mid-session would
    count everything before it.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail))
            data = fh.read(min(size, tail))
    except OSError:
        return None, None
    if agent == "codex":
        from . import codex
        return codex.tail_state(path, data)
    found = None
    for line in data.split(b"\n"):
        if agent == "pi" and _PI_PROMPT_RE.search(line):
            rec = ingest._loads(line)
            if rec and rec.get("type") == "message" and (rec.get("message") or {}).get("role") == "user":
                found = rec.get("id") or found
    return found, None


# ---------------------------------------------------------------- pi

def _pi_ts(rec: dict, msg: Optional[dict] = None) -> Optional[float]:
    ts = ingest.parse_ts(rec.get("timestamp"))
    if ts is None and msg and isinstance(msg.get("timestamp"), (int, float)):
        ts = msg["timestamp"] / 1000.0
    return ts


def _pi_usage_row(session_id, key, ts, model, u: dict, agent_id: str, prompt_id):
    def n(k):
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    w1h = n("cacheWrite1h")
    cost = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) else None
    return (key, session_id, agent_id, ts, model, None, n("input"), n("output"), n("cacheRead"),
            max(n("cacheWrite") - w1h, 0), w1h, 0, prompt_id,
            float(cost) if isinstance(cost, (int, float)) else None)


def process_pi(conn, session_id: str, file_agent_id: str, chunk: bytes, stats, prompt_id=None,
               want_tools: bool = False, state=None):
    usage_rows, tool_rows = [], []
    for line in chunk.split(b"\n"):
        if not line:
            continue
        is_user = _PI_PROMPT_RE.search(line) is not None
        if not (is_user or b'"usage"' in line or (want_tools and b'"toolCall"' in line)
                or (want_tools and b'"isError":true' in line)):
            continue
        rec = ingest._loads(line)
        if rec is None:
            continue
        typ = rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        if typ == "message" and msg:
            role = msg.get("role")
            if role == "user":
                prompt_id = rec.get("id") or prompt_id
                _pi_record_prompt(conn, session_id, prompt_id, _pi_ts(rec, msg), msg.get("content"))
                continue
            ts = _pi_ts(rec, msg)
            if ts is None:
                continue
            u0 = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            if role == "assistant" and msg.get("stopReason") in ("error", "aborted") and not u0.get("totalTokens"):
                continue                     # failed request without usage: not an API call worth counting
            if role == "assistant" and isinstance(msg.get("usage"), dict):
                model = "%s/%s" % (msg.get("provider"), msg.get("model")) if msg.get("provider") else msg.get("model")
                usage_rows.append(_pi_usage_row(session_id, "pi:%s:%s" % (session_id, rec.get("id")), ts, model,
                                                msg["usage"], "", prompt_id))
                if want_tools and isinstance(msg.get("content"), list):
                    tool_rows.extend((c["id"], session_id, "", prompt_id, ts, str(c.get("name") or "?"))
                                     for c in msg["content"]
                                     if isinstance(c, dict) and c.get("type") == "toolCall" and c.get("id"))
            elif role == "toolResult":
                if want_tools and msg.get("isError") and msg.get("toolCallId"):
                    if tool_rows:
                        ingest.insert_tools(conn, tool_rows)
                        tool_rows = []
                    conn.execute("UPDATE tool_calls SET is_error = 1 WHERE id = ?", (msg["toolCallId"],))
                u = msg.get("usage")
                if isinstance(u, dict) and (u.get("totalTokens") or u.get("input") or u.get("output")):
                    # nested model work of a tool, e.g. a subagent: count it as an agent of its own
                    agent_id = "tool:%s" % (msg.get("toolCallId") or rec.get("id"))
                    usage_rows.append(_pi_usage_row(session_id, "pi:%s:%s" % (session_id, rec.get("id")), ts,
                                                    _pi_nested_model(msg), u, agent_id, prompt_id))
                    _pi_register_tool_agent(conn, session_id, agent_id, msg, ts)
        elif typ in ("usage", "compaction", "branch_summary") and isinstance(rec.get("usage"), dict):
            ts = _pi_ts(rec)
            if ts is None:
                continue
            model = "%s/%s" % (rec.get("provider"), rec.get("model")) if rec.get("provider") else rec.get("model")
            usage_rows.append(_pi_usage_row(session_id, "pi:%s:%s" % (session_id, rec.get("id")), ts, model,
                                            rec["usage"], "", prompt_id))
    if usage_rows:
        conn.executemany(ingest.USAGE_INSERT, usage_rows)
        stats.usage_rows += len(usage_rows)
    if tool_rows:
        ingest.insert_tools(conn, tool_rows)
    return prompt_id


def _pi_record_prompt(conn, session_id, prompt_id, ts, content) -> None:
    """pi prompts are taken from the transcript (their entry id is the id the
    following model calls are attributed to). Recorded for the task that was
    counting this session at that moment."""
    from . import settings
    if not prompt_id or ts is None:
        return
    task = conn.execute(
        "SELECT t.id, t.name, t.initial_prompt FROM tasks t JOIN task_sessions s ON s.task_id = t.id "
        "WHERE s.session_id = ? AND s.attached_at <= ? AND (s.detached_at IS NULL OR s.detached_at >= ?) "
        "AND (t.ended_at IS NULL OR t.ended_at >= ?) ORDER BY t.id DESC LIMIT 1",
        (session_id, ts + 1, ts, ts)).fetchone()
    if task is None or conn.execute("SELECT 1 FROM prompts WHERE task_id = ? AND prompt_id = ?",
                                    (task["id"], prompt_id)).fetchone():
        return
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    stored = settings.store_prompt(content if isinstance(content, str) else None)
    conn.execute("INSERT INTO prompts(task_id, session_id, ts, prompt, prompt_id) VALUES(?,?,?,?,?)",
                 (task["id"], session_id, ts, stored, prompt_id))
    if not task["initial_prompt"] and stored:
        from . import tasks
        conn.execute("UPDATE tasks SET initial_prompt = ? WHERE id = ?", (stored, task["id"]))
        if task["name"] == tasks.UNTITLED:
            conn.execute("UPDATE tasks SET name = ? WHERE id = ?", (tasks.derive_name(stored), task["id"]))


def _pi_nested_model(msg: dict) -> Optional[str]:
    d = msg.get("details") if isinstance(msg.get("details"), dict) else {}
    for k in ("model", "modelId"):
        if isinstance(d.get(k), str):
            return d[k]
    return "via %s" % (msg.get("toolName") or "tool")       # nested usage names no model


def _pi_register_tool_agent(conn, session_id, agent_id, msg: dict, ts) -> None:
    d = msg.get("details") if isinstance(msg.get("details"), dict) else {}
    desc = None
    for k in ("description", "task", "agent", "agentName", "name", "label", "title"):
        if isinstance(d.get(k), str) and d[k].strip():
            desc = d[k].strip()[:120]
            break
    conn.execute(
        "INSERT INTO agents(session_id, agent_id, agent_type, description, tool_use_id, first_ts, last_ts) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id, agent_id) DO UPDATE SET "
        "last_ts = MAX(COALESCE(agents.last_ts, excluded.last_ts), excluded.last_ts)",
        (session_id, agent_id, str(msg.get("toolName") or "tool"), desc, msg.get("toolCallId"), ts, ts))


def process_codex(conn, session_id: str, file_agent_id: str, chunk: bytes, stats, prompt_id=None,
                  want_tools: bool = False, state=None):
    from . import codex
    return codex.process(conn, session_id, file_agent_id, chunk, stats, prompt_id, want_tools,
                         state if state is not None else {})


def describe(agent: str) -> str:
    return {"claude": "Claude Code", "codex": "Codex", "pi": "pi"}.get(agent, agent)


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
