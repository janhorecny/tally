"""Build a task report (plain dict, JSON-serialisable) from the database."""

import time
from collections import OrderedDict
from typing import Dict, List, Optional

from . import paths, tasks
from .pricing import Pricer, normalize_model


class Tot:
    __slots__ = ("input", "output", "cache_read", "cache_write", "calls", "cost", "cost_partial", "web_search")

    def __init__(self):
        self.input = self.output = self.cache_read = self.cache_write = 0
        self.calls = self.web_search = 0
        self.cost = 0.0
        self.cost_partial = False

    def add_row(self, r, cost: Optional[float]) -> None:
        self.input += r["input"]
        self.output += r["output"]
        self.cache_read += r["cache_read"]
        self.cache_write += r["cache_write_5m"] + r["cache_write_1h"]
        self.web_search += r["web_search"]
        self.calls += 1
        if cost is None:
            self.cost_partial = True
        else:
            self.cost += cost

    def add(self, other: "Tot") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.web_search += other.web_search
        self.calls += other.calls
        self.cost += other.cost
        self.cost_partial = self.cost_partial or other.cost_partial

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "calls": self.calls,
            "web_search": self.web_search,
            "cost": round(self.cost, 6),
            "cost_partial": self.cost_partial,
        }


def build(conn, task, pricer: Optional[Pricer] = None, windows: Optional[list] = None) -> dict:
    """task: row/dict with id, name, started_at, ended_at, ...

    windows: optional [(session_id, from, to)] override; by default the
    task's attached sessions clipped to its lifetime.
    """
    pricer = pricer or Pricer(paths.load_config().get("pricing"))
    now = time.time()
    t0 = task["started_at"]
    t1 = task["ended_at"] or now
    if windows is None:
        windows = tasks.task_windows(conn, task)
    sids = [w[0] for w in windows]
    multi_session = len(set(sids)) > 1

    totals = Tot()
    by_model: Dict[str, Tot] = OrderedDict()
    per_agent: Dict[tuple, Tot] = {}
    agent_span: Dict[tuple, list] = {}
    agent_models: Dict[tuple, set] = {}
    active_ivals: List[tuple] = []
    turn_count = 0

    for sid, a, b in windows:
        for r in conn.execute(
            "SELECT * FROM usage WHERE session_id = ? AND ts >= ? AND ts <= ? ORDER BY ts", (sid, a, b)
        ):
            c = pricer.cost(r["model"], r["input"], r["output"], r["cache_read"],
                            r["cache_write_5m"], r["cache_write_1h"], r["speed"])
            totals.add_row(r, c)
            mkey = normalize_model(r["model"]) or "unknown"
            by_model.setdefault(mkey, Tot()).add_row(r, c)
            akey = (sid, r["agent_id"])
            per_agent.setdefault(akey, Tot()).add_row(r, c)
            agent_models.setdefault(akey, set()).add(mkey)
            span = agent_span.setdefault(akey, [r["ts"], r["ts"]])
            span[0] = min(span[0], r["ts"])
            span[1] = max(span[1], r["ts"])
        ivals = _turn_intervals(conn, sid, a, b)
        # the same turn may be measured twice (transcript turn_duration + hooks)
        n_hook = sum(1 for iv in ivals if iv[2])
        turn_count += max(n_hook, len(ivals) - n_hook)
        active_ivals.extend(ivals)

    active_s = _union_length(active_ivals)

    window_by_sid = {}
    for sid, a, b in windows:
        lo, hi = window_by_sid.get(sid, (a, b))
        window_by_sid[sid] = (min(lo, a), max(hi, b))

    # ---------------------------------------------------------------- tree
    agent_meta = {}
    workflow_meta = {}
    for sid in set(sids):
        for r in conn.execute("SELECT * FROM agents WHERE session_id = ?", (sid,)):
            agent_meta[(sid, r["agent_id"])] = r
        for r in conn.execute("SELECT * FROM workflows WHERE session_id = ?", (sid,)):
            workflow_meta[(sid, r["run_id"])] = r

    nodes: Dict[str, dict] = {}

    def node(key, kind, label, parent=None, **extra):
        if key in nodes:
            return nodes[key]
        n = {"key": key, "kind": kind, "label": label, "parent": parent, "own": Tot(),
             "children": [], "first_ts": None, "last_ts": None}
        n.update(extra)
        nodes[key] = n
        if parent:
            nodes[parent]["children"].append(key)
        return n

    roots: List[str] = []
    for sid in OrderedDict.fromkeys(sids):
        key = "main:" + sid
        label = "main session" + (" " + sid[:8] if multi_session else "")
        node(key, "main", label, session_id=sid, agent_type="main")
        roots.append(key)

    def ensure_agent(sid, aid):
        key = "agent:%s:%s" % (sid, aid)
        if key in nodes:
            return key
        meta = agent_meta.get((sid, aid))
        parent_aid = meta["parent_agent_id"] if meta else None
        run_id = meta["workflow_run_id"] if meta else None
        if parent_aid and (sid, parent_aid) in agent_meta:
            parent = ensure_agent(sid, parent_aid)
        elif run_id:
            parent = ensure_phase(sid, run_id, meta["workflow_phase"])
        else:
            parent = "main:" + sid
        atype = (meta["agent_type"] if meta else None) or "subagent"
        desc = meta["description"] if meta else None
        kind = "workflow-agent" if run_id else ("fork" if meta and meta["is_fork"] else "subagent")
        node(key, kind, desc or atype, parent, session_id=sid, agent_id=aid, agent_type=atype,
             description=desc, workflow_run_id=run_id,
             workflow_phase=meta["workflow_phase"] if meta else None)
        return key

    def ensure_workflow(sid, run_id):
        key = "workflow:%s:%s" % (sid, run_id)
        if key not in nodes:
            wm = workflow_meta.get((sid, run_id))
            name = (wm["name"] if wm else None) or run_id
            node(key, "workflow", name, "main:" + sid, session_id=sid, run_id=run_id,
                 agent_type="workflow", summary=wm["summary"] if wm else None,
                 status=wm["status"] if wm else None,
                 reported_total_tokens=wm["reported_total_tokens"] if wm else None)
        return key

    def ensure_phase(sid, run_id, phase):
        wkey = ensure_workflow(sid, run_id)
        if not phase:
            return wkey
        key = "phase:%s:%s:%s" % (sid, run_id, phase)
        if key not in nodes:
            node(key, "phase", phase, wkey, agent_type="workflow-phase")
        return key

    for (sid, aid), tot in per_agent.items():
        key = "main:" + sid if aid == "" else ensure_agent(sid, aid)
        n = nodes[key]
        n["own"].add(tot)
        n["models"] = sorted(agent_models.get((sid, aid), ()))
        lo, hi = window_by_sid[sid]
        meta = agent_meta.get((sid, aid))
        first = meta["first_ts"] if meta and meta["first_ts"] else agent_span[(sid, aid)][0]
        last = meta["last_ts"] if meta and meta["last_ts"] else agent_span[(sid, aid)][1]
        n["first_ts"] = max(first, lo)
        n["last_ts"] = min(last, hi)

    def finalize(key) -> Tot:
        n = nodes[key]
        roll = Tot()
        roll.add(n["own"])
        firsts = [n["first_ts"]] if n["first_ts"] is not None else []
        lasts = [n["last_ts"]] if n["last_ts"] is not None else []
        n["children"].sort(key=lambda k: -(_rollup_total_hint(nodes, k)))
        count = 1 if n["kind"] in ("subagent", "workflow-agent", "fork") else 0
        for ck in n["children"]:
            croll = finalize(ck)
            roll.add(croll)
            c = nodes[ck]
            count += c["agent_count"]
            if c["first_ts"] is not None:
                firsts.append(c["first_ts"])
            if c["last_ts"] is not None:
                lasts.append(c["last_ts"])
        n["rollup_tot"] = roll
        n["agent_count"] = count
        if n["kind"] != "main":
            n["first_ts"] = min(firsts) if firsts else None
            n["last_ts"] = max(lasts) if lasts else None
        return roll

    for rk in roots:
        finalize(rk)

    def serialise(key) -> dict:
        n = nodes[key]
        out = {k: v for k, v in n.items() if k not in ("own", "rollup_tot", "children", "parent")}
        out["own"] = n["own"].as_dict()
        out["rollup"] = n["rollup_tot"].as_dict()
        if n["kind"] == "main":
            lo, hi = window_by_sid[n["session_id"]]
            out["duration_s"] = round(_main_active(conn, n["session_id"], lo, hi), 3)
        elif n["first_ts"] is not None and n["last_ts"] is not None:
            out["duration_s"] = round(n["last_ts"] - n["first_ts"], 3)
        else:
            out["duration_s"] = 0
        out["children"] = [serialise(c) for c in n["children"]]
        return out

    tree = [serialise(rk) for rk in roots]

    # ------------------------------------------------------------ groupings
    by_type: Dict[str, dict] = OrderedDict()
    workflows = []
    for n in nodes.values():
        kind = n["kind"]
        if kind in ("main", "subagent", "fork", "workflow-agent"):
            t = n["agent_type"] or kind
            g = by_type.setdefault(t, {"agent_type": t, "count": 0, "tot": Tot()})
            g["tot"].add(n["own"])
            if n["own"].calls:
                g["count"] += 1
        if kind == "workflow":
            phases = []
            for ck in n["children"]:
                c = nodes[ck]
                if c["kind"] == "phase":
                    phases.append({"phase": c["label"], "agents": c["agent_count"],
                                   **c["rollup_tot"].as_dict()})
            dur = (n["last_ts"] - n["first_ts"]) if n["first_ts"] is not None and n["last_ts"] is not None else 0
            workflows.append({
                "run_id": n["run_id"], "name": n["label"], "summary": n.get("summary"),
                "status": n.get("status"), "agents": n["agent_count"],
                "duration_s": round(dur, 3), "reported_total_tokens": n.get("reported_total_tokens"),
                **n["rollup_tot"].as_dict(), "phases": phases,
            })
    by_type_list = sorted(
        ({"agent_type": g["agent_type"], "agents": g["count"], **g["tot"].as_dict()} for g in by_type.values()
         if g["tot"].calls),
        key=lambda d: -d["total"],
    )
    workflows.sort(key=lambda d: -d["total"])

    if task["id"]:
        sessions = [
            {"session_id": r["session_id"], "attached_at": r["attached_at"], "detached_at": r["detached_at"],
             "reason": r["reason"]}
            for r in conn.execute(
                "SELECT * FROM task_sessions WHERE task_id = ? ORDER BY attached_at", (task["id"],))
        ]
        prompt_count = conn.execute("SELECT COUNT(*) FROM prompts WHERE task_id = ?", (task["id"],)).fetchone()[0]
    else:
        sessions = [{"session_id": s, "attached_at": a, "detached_at": b, "reason": "session"} for s, a, b in windows]
        prompt_count = 0

    from . import reconcile
    unlogged = reconcile.estimate(conn, windows, pricer)

    return {
        "task": {
            "id": task["id"], "name": task["name"], "initial_prompt": task["initial_prompt"],
            "status": task["status"], "started_at": t0, "ended_at": task["ended_at"],
            "cwd": task["cwd"], "created_by": task["created_by"], "end_note": task["end_note"],
            "sessions": sessions, "prompt_count": prompt_count,
        },
        "time": {"wall_s": round(t1 - t0, 3), "active_s": round(active_s, 3), "turns": turn_count},
        "totals": totals.as_dict(),
        "unlogged": unlogged,
        "by_model": sorted(({"model": m, **t.as_dict()} for m, t in by_model.items()), key=lambda d: -d["total"]),
        "by_type": by_type_list,
        "workflows": workflows,
        "tree": tree,
        "generated_at": now,
    }


def _rollup_total_hint(nodes, key) -> int:
    n = nodes[key]
    total = n["own"].total
    for ck in n["children"]:
        total += _rollup_total_hint(nodes, ck)
    return total


def _turn_intervals(conn, sid, a, b) -> List[tuple]:
    """Main-session turns overlapping [a, b], clipped to it.

    Turns can overlap: a turn that waits on background agents stays open
    while later turns run, so callers take the union, not the sum.
    """
    out = []
    for t in conn.execute(
        "SELECT uuid, ts_end, duration_ms FROM turns WHERE session_id = ? AND ts_end >= ? "
        "AND ts_end - duration_ms / 1000.0 <= ?", (sid, a, b)
    ):
        lo = max(t["ts_end"] - t["duration_ms"] / 1000.0, a)
        hi = min(t["ts_end"], b)
        if hi > lo:
            out.append((lo, hi, t["uuid"].startswith("hook:")))
    return out


def _union_length(ivals: List[tuple]) -> float:
    total = 0.0
    cur_lo = cur_hi = None
    for lo, hi, *_ in sorted(ivals):
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        elif hi > cur_hi:
            cur_hi = hi
    if cur_hi is not None:
        total += cur_hi - cur_lo
    return total


def _main_active(conn, sid, a, b) -> float:
    return _union_length(_turn_intervals(conn, sid, a, b))


def summary_for(conn, task, live: bool = True) -> dict:
    """Report for a task: frozen snapshot if ended, fresh build if active.

    An ended task's snapshot is rebuilt when Claude Code has since written a
    new session cost record (used for the unlogged-usage estimate).
    """
    import json
    from . import reconcile
    refreshed = False
    for sid in tasks.session_ids(conn, task["id"]):
        try:
            refreshed = reconcile.refresh(conn, sid) or refreshed
        except Exception:
            pass
    if task["status"] != "active":
        row = conn.execute("SELECT report_json FROM task_snapshots WHERE task_id = ?", (task["id"],)).fetchone()
        if row and not refreshed:
            rep = json.loads(row[0])
            rep["task"]["name"] = task["name"]
            return rep
        rep = build(conn, task)
        conn.execute("INSERT OR REPLACE INTO task_snapshots(task_id, computed_at, report_json) VALUES(?,?,?)",
                     (task["id"], time.time(), json.dumps(rep)))
        return rep
    if live and task["status"] == "active":
        from . import ingest
        try:
            ingest.ingest_sessions(conn, tasks.session_ids(conn, task["id"]))
        except Exception:
            pass
    return build(conn, task)
