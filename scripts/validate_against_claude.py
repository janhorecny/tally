#!/usr/bin/env python3
"""Cross-check task-counter's transcript parsing against Claude Code's own numbers.

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

    os.environ["TASK_COUNTER_HOME"] = tempfile.mkdtemp(prefix="tc-validate-")
    from task_counter import db, ingest
    from task_counter.pricing import Pricer, normalize_model

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
        their_cost = cs.get("totalCostUSD") or sum(v["cost"] for v in theirs.values())
        my_out = sum(v["output"] for v in mine.values())
        their_out = sum(v["output"] for v in theirs.values())
        rows.append((sid[:8], f.parent.name[-28:], their_out, my_out, their_cost, my_cost))
        checked += 1

    print("%-8s %-28s %12s %12s %7s %10s %10s %7s" % ("session", "project", "out(claude)", "out(mine)", "diff",
                                                      "$claude", "$mine", "diff"))
    within = 0
    for sid, proj, to, mo, tc, mc in rows:
        do = (mo - to) / to * 100 if to else 0.0
        dc = (mc - tc) / tc * 100 if tc else 0.0
        if abs(dc) <= 2:
            within += 1
        print("%-8s %-28s %12d %12d %6.1f%% %10.2f %10.2f %6.1f%%" % (sid, proj, to, mo, do, tc, mc, dc))
    print("\n%d/%d sessions within 2%% of Claude Code's own cost figure" % (within, len(rows)))
    print("(differences come from usage not written to transcripts, e.g. title generation, and from sessions "
          "that continued after the last cost-state record)")


if __name__ == "__main__":
    main()
