"""Reconcile with Claude Code's own session totals.

Some API calls Claude Code makes for itself (background-agent progress
summaries, prompt suggestions, titles, memory extraction, compaction) are not
written to any transcript. When a session exits, Claude Code appends a
`cost-state` record with exact per-model totals for the whole session,
including those calls.

unlogged = cost-state totals - everything logged in the session's transcripts

A task gets a share of that, proportional to its share of the session's
logged tokens. It is reported separately as an estimate; the per-agent
numbers stay exact.
"""

import json
import time
from pathlib import Path
from typing import Dict, Optional

from . import db, ingest, paths
from .pricing import Pricer, normalize_model

_MARK = b'"cost-state"'
_FIELDS = ("input", "output", "cache_read", "cache_write")


def _zero():
    return {k: 0 for k in _FIELDS}


def scan_session_totals(main: Path, until_ts: float, pricer: Optional[Pricer] = None) -> Dict[str, dict]:
    """Per-model logged usage of a whole session (main + all agents) up to
    until_ts, with its cost priced per call (so per-call tiers are right)."""
    pricer = pricer or Pricer(paths.load_config().get("pricing"))
    rows = {}
    for path, agent_id, _run in ingest.session_files(main):
        try:
            with open(path, "rb") as fh:
                for line in fh:
                    if b'"usage"' not in line or b'"assistant"' not in line:
                        continue
                    rec = ingest._loads(line)
                    if rec is None:
                        continue
                    r = ingest._usage_row(rec, "", agent_id)
                    if r:
                        rows[r[0]] = r          # last line of a message wins
        except OSError:
            continue
    out: Dict[str, dict] = {"_total": dict(_zero(), cost=0.0)}
    for r in rows.values():
        ts, model = r[3], r[4]
        if ts > until_ts:
            continue
        m = normalize_model(model) or "unknown"
        d = out.setdefault(m, dict(_zero(), cost=0.0))
        vals = {"input": r[6], "output": r[7], "cache_read": r[8], "cache_write": r[9] + r[10]}
        for k in _FIELDS:
            d[k] += vals[k]
            out["_total"][k] += vals[k]
        c = pricer.cost(model, r[6], r[7], r[8], r[9], r[10], r[5]) or 0.0
        d["cost"] += c
        out["_total"]["cost"] += c
    return out


def refresh(conn, session_id: str) -> bool:
    """Look for a new cost-state record; store it with the matching logged totals.

    Cheap when nothing changed (one stat call). Returns True if a new record
    was stored.
    """
    main = ingest.resolve_transcript(conn, session_id)
    if main is None:
        return False
    try:
        size = main.stat().st_size
    except OSError:
        return False
    row = conn.execute("SELECT scan_offset FROM session_costs WHERE session_id = ?", (session_id,)).fetchone()
    off = row["scan_offset"] if row else 0
    if size <= off:
        return False
    with open(main, "rb") as fh:
        fh.seek(off)
        data = fh.read(size - off)
    end = data.rfind(b"\n")
    if end < 0:
        return False
    chunk = data[: end + 1]
    new_off = off + end + 1
    pos = chunk.rfind(_MARK)
    rec = None
    line_abs = None
    while pos >= 0:
        ls = chunk.rfind(b"\n", 0, pos) + 1
        le = chunk.find(b"\n", pos)
        cand = ingest._loads(chunk[ls:le])
        if cand and cand.get("type") == "cost-state" and isinstance(cand.get("modelUsage"), dict):
            rec = cand
            line_abs = off + ls
            break
        pos = chunk.rfind(_MARK, 0, ls)
    if rec is None:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO session_costs(session_id, scan_offset) VALUES(?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET scan_offset = excluded.scan_offset",
                (session_id, new_off),
            )
        return False
    cs_ts = _last_ts_before(main, line_abs)
    if cs_ts is None:
        cs_ts = main.stat().st_mtime
    logged = scan_session_totals(main, cs_ts + 1.0)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO session_costs(session_id, scan_offset, cs_ts, cs_json, logged_json, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET scan_offset = excluded.scan_offset, cs_ts = excluded.cs_ts, "
            "cs_json = excluded.cs_json, logged_json = excluded.logged_json, updated_at = excluded.updated_at",
            (session_id, new_off, cs_ts, json.dumps(rec), json.dumps(logged), time.time()),
        )
    return True


def _last_ts_before(main: Path, abs_pos: int) -> Optional[float]:
    start = max(0, abs_pos - 2_000_000)
    with open(main, "rb") as fh:
        fh.seek(start)
        data = fh.read(abs_pos - start)
    found = ingest._TS_RE.findall(data)
    for raw in reversed(found):
        ts = ingest.parse_ts(raw.decode("ascii", "ignore"))
        if ts is not None:
            return ts
    return None


def updated_since(conn, session_ids, since: float) -> bool:
    """True if any of the sessions got a newer cost record after `since`."""
    for sid in session_ids:
        row = conn.execute("SELECT updated_at FROM session_costs WHERE session_id = ?", (sid,)).fetchone()
        if row and row["updated_at"] and row["updated_at"] > since:
            return True
    return False


def estimate(conn, windows, pricer) -> dict:
    """Unlogged usage attributable to a task. windows: [(session_id, from, to)]."""
    tokens = _zero()
    cost = 0.0
    sessions_done = 0
    sessions_pending = 0
    partial = False
    for sid, a, b in windows:
        row = conn.execute("SELECT cs_ts, cs_json, logged_json FROM session_costs WHERE session_id = ?",
                           (sid,)).fetchone()
        if row is None or not row["cs_json"]:
            sessions_pending += 1
            continue
        cs_ts = row["cs_ts"]
        if cs_ts < a:
            sessions_pending += 1     # record predates this task's part of the session
            continue
        cs = json.loads(row["cs_json"])
        logged = json.loads(row["logged_json"])
        sess_total = sum(logged["_total"][k] for k in _FIELDS)
        hi = min(b, cs_ts + 1.0)
        mine = conn.execute(
            "SELECT COALESCE(SUM(input + output + cache_read + cache_write_5m + cache_write_1h), 0) "
            "FROM usage WHERE session_id = ? AND ts >= ? AND ts <= ?", (sid, a, hi)).fetchone()[0]
        if not sess_total or not mine:
            sessions_done += 1
            continue
        share = min(1.0, mine / float(sess_total))
        for model, u in cs["modelUsage"].items():
            m = normalize_model(model) or "unknown"
            lg = logged.get(m, _zero())
            delta = {
                "input": max(0, int(u.get("inputTokens", 0)) - lg["input"]),
                "output": max(0, int(u.get("outputTokens", 0)) - lg["output"]),
                "cache_read": max(0, int(u.get("cacheReadInputTokens", 0)) - lg["cache_read"]),
                "cache_write": max(0, int(u.get("cacheCreationInputTokens", 0)) - lg["cache_write"]),
            }
            for k in _FIELDS:
                tokens[k] += int(round(delta[k] * share))
            # logged cost of the model: 0 if it never appears in transcripts
            # (e.g. Haiku used only for Claude Code's own calls); None for
            # records stored before costs were kept.
            lg_cost = lg.get("cost") if m in logged else 0.0
            if isinstance(u.get("costUSD"), (int, float)) and lg_cost is not None:
                # Claude Code's exact cost for the model minus what the transcripts
                # account for: right even when its own calls hit per-call tiers.
                c = max(0.0, float(u["costUSD"]) - lg_cost)
            else:
                c = pricer.cost(model, delta["input"], delta["output"], delta["cache_read"],
                                delta["cache_write"], 0, tiers=False) or 0.0
            cost += c * share
        sessions_done += 1
        if b > cs_ts + 60:
            partial = True            # session kept running after its last cost record
    total = sum(tokens.values())
    if sessions_done == 0:
        status = "pending"
    elif sessions_pending or partial:
        status = "partial"
    else:
        status = "estimated"
    return {"status": status, "total": total, **tokens, "cost": round(cost, 6),
            "sessions_reconciled": sessions_done, "sessions_pending": sessions_pending}
