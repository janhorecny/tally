#!/usr/bin/env python3
"""Cross-check tally's transcript parsing against Claude Code's own numbers.

Claude Code appends a `cost-state` record (per-model token totals and cost,
including subagents) to the main transcript. This script ingests each such
session into a throw-away database and compares per-model output tokens,
cache reads and the estimated cost.

Reads files only; makes no API calls.

    python3 scripts/validate_against_claude.py [--limit N]
"""

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "plugin"
sys.path.insert(0, str(ROOT))


def last_cost_state(path: Path):
    found = None
    with open(path, "rb") as fh:
        for line in fh:
            if b'"cost-state"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "cost-state":
                    found = d
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    args = ap.parse_args()

    os.environ["TALLY_HOME"] = tempfile.mkdtemp(prefix="tally-validate-")
    from tally import db, ingest, reconcile
    from tally.pricing import Pricer, normalize_model

    conn = db.connect()
    pricer = Pricer()
    files = sorted(Path(args.projects).glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    checked = 0
    rows = []
    for f in files:
        if checked >= args.limit:
            break
        cs = last_cost_state(f)
        if not cs or not cs.get("modelUsage"):
            continue
        sid = f.stem
        ingest.ingest_session(conn, sid, str(f))
        mine = defaultdict(lambda: defaultdict(float))
        for r in conn.execute("SELECT * FROM usage WHERE session_id = ?", (sid,)):
            m = normalize_model(r["model"])
            mine[m]["output"] += r["output"]
            mine[m]["cache_read"] += r["cache_read"]
            mine[m]["cost"] += pricer.cost(r["model"], r["input"], r["output"], r["cache_read"],
                                           r["cache_write_5m"], r["cache_write_1h"], r["speed"]) or 0
        theirs = defaultdict(lambda: defaultdict(float))
        for m, u in cs["modelUsage"].items():
            n = normalize_model(m)
            theirs[n]["output"] += u.get("outputTokens", 0)
            theirs[n]["cache_read"] += u.get("cacheReadInputTokens", 0)
            theirs[n]["cost"] += u.get("costUSD", 0)
        my_cost = sum(v["cost"] for v in mine.values())
        reconcile.refresh(conn, sid)
        span = conn.execute("SELECT MIN(ts), MAX(ts) FROM usage WHERE session_id = ?", (sid,)).fetchone()
        un = reconcile.estimate(conn, [(sid, (span[0] or 0) - 1, (span[1] or 0) + 1)], pricer)
        their_cost = cs.get("totalCostUSD") or sum(v["cost"] for v in theirs.values())
        my_out = sum(v["output"] for v in mine.values())
        their_out = sum(v["output"] for v in theirs.values())
        rows.append((sid[:8], f.parent.name[-28:], their_out, my_out, their_cost, my_cost, un["cost"]))
        checked += 1

    print("%-8s %-24s %11s %11s %6s %9s %9s %6s %9s %6s" % (
        "session", "project", "out(claude)", "out(logged)", "diff", "$claude", "$logged", "diff", "$+unlog", "diff"))
    within = within2 = 0
    for sid, proj, to, mo, tc, mc, uc in rows:
        do = (mo - to) / to * 100 if to else 0.0
        dc = (mc - tc) / tc * 100 if tc else 0.0
        du = (mc + uc - tc) / tc * 100 if tc else 0.0
        within += abs(dc) <= 2
        within2 += abs(du) <= 2
        print("%-8s %-24s %11d %11d %5.1f%% %9.2f %9.2f %5.1f%% %9.2f %5.1f%%" % (
            sid, proj[-24:], to, mo, do, tc, mc, dc, mc + uc, du))
    print("\nlogged only:          %d/%d sessions within 2%% of Claude Code's cost" % (within, len(rows)))
    print("logged + unlogged:    %d/%d sessions within 2%%" % (within2, len(rows)))
    print("(logged = transcripts: main, subagents, workflows; unlogged = Claude Code's own calls, from cost-state. "
          "Remaining gaps: price-table changes over time and sessions resumed after their last cost record.)")


if __name__ == "__main__":
    main()
