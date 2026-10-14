"""Build a task report (plain dict, JSON-serialisable) from the database."""

import json
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from . import db, paths, tasks
from .pricing import Pricer, normalize_model

OPEN_TURN_IDLE_S = 600      # a turn still running counts until 10 minutes after the session's last API call


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


def build(conn, task, pricer: Optional[Pricer] = None, windows: Optional[list] = None,
          detail: bool = True) -> dict:
    """task: row/dict with id, name, started_at, ended_at, ...

    windows: optional [(session_id, from, to)] override; by default the
    task's attached sessions clipped to its lifetime.
    detail=False returns only task/time/totals (lists, status line): no agent
    tree, no groupings, no reconciliation.
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
    ivals_by_sid: Dict[str, List[tuple]] = {}
    turn_count = 0
    per_prompt: Dict[Optional[str], Tot] = {}
    prompt_agents: Dict[Optional[str], set] = {}
    prompt_span: Dict[Optional[str], list] = {}

    for sid, a, b in windows:
        for r in conn.execute(
            "SELECT * FROM usage WHERE session_id = ? AND ts >= ? AND ts <= ? ORDER BY ts", (sid, a, b)
        ):
            c = pricer.row_cost(r)
            totals.add_row(r, c)
            mkey = normalize_model(r["model"]) or "unknown"
            by_model.setdefault(mkey, Tot()).add_row(r, c)
            akey = (sid, r["agent_id"])
            per_agent.setdefault(akey, Tot()).add_row(r, c)
            agent_models.setdefault(akey, set()).add(mkey)
            span = agent_span.setdefault(akey, [r["ts"], r["ts"]])
            span[0] = min(span[0], r["ts"])
            span[1] = max(span[1], r["ts"])
            pid = r["prompt_id"]
            per_prompt.setdefault(pid, Tot()).add_row(r, c)
            if r["agent_id"]:
                prompt_agents.setdefault(pid, set()).add((sid, r["agent_id"]))
            pspan = prompt_span.setdefault(pid, [r["ts"], r["ts"]])
            pspan[0] = min(pspan[0], r["ts"])
            pspan[1] = max(pspan[1], r["ts"])
        ivals = _turn_intervals(conn, sid, a, b)
        running = _open_turn(conn, sid, a, b, now)
        if running:
            ivals.append(running)
        # the same turn may be measured twice (transcript turn_duration + hooks)
        n_hook = sum(1 for iv in ivals if iv[2])
        turn_count += max(n_hook, len(ivals) - n_hook)
        active_ivals.extend(ivals)
        ivals_by_sid.setdefault(sid, []).extend(ivals)

    active_s = _union_length(active_ivals)
    head = _task_head(conn, task)
    if not detail:
        return {
            "task": dict(head, initial_prompt=task["initial_prompt"], status=task["status"], started_at=t0,
                         ended_at=task["ended_at"], cwd=task["cwd"]),
            "time": {"wall_s": round(t1 - t0, 3), "active_s": round(active_s, 3), "turns": turn_count},
            "totals": totals.as_dict(),
            "unlogged": None,
        }

    # a session's own windows: another task may have counted it in the gaps between them
    windows_by_sid: Dict[str, List[tuple]] = {}
    for sid, a, b in windows:
        windows_by_sid.setdefault(sid, []).append((a, b))

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
        meta = agent_meta.get((sid, aid))
        first = meta["first_ts"] if meta and meta["first_ts"] else agent_span[(sid, aid)][0]
        last = meta["last_ts"] if meta and meta["last_ts"] else agent_span[(sid, aid)][1]
        n["first_ts"], n["last_ts"] = _clip_span(first, last, windows_by_sid[sid], agent_span[(sid, aid)])

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
            out["duration_s"] = round(_union_length(ivals_by_sid.get(n["session_id"], [])), 3)
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
        sessions = []
        latest = {}         # session -> its last listed interval: two attaches that raced are listed once
        for r in conn.execute("SELECT * FROM task_sessions WHERE task_id = ? ORDER BY attached_at", (task["id"],)):
            prev = latest.get(r["session_id"])
            if prev is not None and (prev["detached_at"] is None or r["attached_at"] <= prev["detached_at"]):
                if prev["detached_at"] is not None:
                    prev["detached_at"] = None if r["detached_at"] is None else max(prev["detached_at"],
                                                                                    r["detached_at"])
                continue
            latest[r["session_id"]] = {"session_id": r["session_id"], "attached_at": r["attached_at"],
                                       "detached_at": r["detached_at"], "reason": r["reason"]}
            sessions.append(latest[r["session_id"]])
        prompt_count = _prompt_count(conn, task["id"])
    else:
        sessions = [{"session_id": s, "attached_at": a, "detached_at": b, "reason": "session"} for s, a, b in windows]
        prompt_count = None             # set from the session's prompts below

    from . import reconcile, settings
    unlogged = reconcile.estimate(conn, windows, pricer)

    cfg = paths.load_config()
    tools = _tool_stats(conn, windows) if settings.depth_at_least("calls", cfg) else None
    prompt_rows, automatic = _prompt_rows(conn, task, windows, per_prompt, prompt_agents, prompt_span,
                                          tools, active_ivals)
    n_prompts = len(prompt_rows)
    if prompt_count is None:
        prompt_count = n_prompts
    tot_d = totals.as_dict()
    durations = [p["duration_s"] for p in prompt_rows if p["duration_s"]]
    stats = {
        "prompts": n_prompts,
        "automatic_turns": automatic["turns"] if automatic else 0,
        "tokens_per_prompt": round(tot_d["total"] / n_prompts) if n_prompts else None,
        "output_per_prompt": round(tot_d["output"] / n_prompts) if n_prompts else None,
        "cost_per_prompt": round(tot_d["cost"] / n_prompts, 6) if n_prompts else None,
        "avg_turn_s": round(sum(durations) / len(durations), 1) if durations else None,
        "max_turn_s": round(max(durations), 1) if durations else None,
        "cache_hit_ratio": (round(tot_d["cache_read"] / float(tot_d["input"] + tot_d["cache_read"]
                                                             + tot_d["cache_write"]), 4)
                            if (tot_d["input"] + tot_d["cache_read"] + tot_d["cache_write"]) else None),
        "output_per_active_min": round(tot_d["output"] / (active_s / 60.0)) if active_s >= 1 else None,
        "subagents": sum(1 for n in nodes.values() if n["kind"] in ("subagent", "fork", "workflow-agent")
                         and n["own"].calls),
        "workflows": len(workflows),
        "models": len(by_model),
        "tool_calls": sum(t["calls"] for t in tools) if tools is not None else None,
        "tool_errors": sum(t["errors"] for t in tools) if tools is not None else None,
        "tools": tools,
        "automatic": automatic,
        "depth": settings.get("log.depth", cfg) or "calls",
    }
    if not settings.depth_at_least("prompts", cfg):
        prompt_rows = None

    return {
        "task": dict(head, **{
            "initial_prompt": task["initial_prompt"],
            "status": task["status"], "started_at": t0, "ended_at": task["ended_at"],
            "cwd": task["cwd"], "created_by": task["created_by"], "end_note": task["end_note"],
            "sessions": sessions, "prompt_count": prompt_count,
        }),
        "prompts": prompt_rows,
        "stats": stats,
        "time": {"wall_s": round(t1 - t0, 3), "active_s": round(active_s, 3), "turns": turn_count},
        "totals": totals.as_dict(),
        "unlogged": unlogged,
        "by_model": sorted(({"model": m, **t.as_dict()} for m, t in by_model.items()), key=lambda d: -d["total"]),
        "by_type": by_type_list,
        "workflows": workflows,
        "tree": tree,
        "generated_at": now,
    }


def _task_head(conn, task) -> dict:
    head = {"id": task["id"], "name": task["name"], "project": None, "todo": _todo_head(conn, task)}
    keys = task.keys() if hasattr(task, "keys") else []
    if "project" in keys:
        head["project"] = task["project"]
    if task["id"]:
        head["prompt_count"] = _prompt_count(conn, task["id"])
    return head


def _todo_head(conn, task) -> Optional[dict]:
    """The todo a task works on, with its current status."""
    keys = task.keys() if hasattr(task, "keys") else []
    if "todo_id" not in keys or not task["todo_id"]:
        return None
    t = conn.execute("SELECT id, title, status FROM todos WHERE id = ?", (task["todo_id"],)).fetchone()
    return {"id": t["id"], "title": t["title"], "status": t["status"]} if t else None


def _prompt_count(conn, task_id: int) -> int:
    """Prompts of a task; one stored twice (the hook registered twice) counts once."""
    return conn.execute("SELECT COUNT(DISTINCT COALESCE(prompt_id, '#' || id)) FROM prompts WHERE task_id = ?",
                        (task_id,)).fetchone()[0]


def _tool_stats(conn, windows) -> list:
    tools: Dict[str, list] = {}
    for sid, a, b in windows:
        for r in conn.execute("SELECT tool, COUNT(*) AS n, SUM(is_error) AS e FROM tool_calls "
                              "WHERE session_id = ? AND ts >= ? AND ts <= ? GROUP BY tool", (sid, a, b)):
            t = tools.setdefault(r["tool"], [0, 0])
            t[0] += r["n"]
            t[1] += r["e"] or 0
    return sorted(({"tool": k, "calls": v[0], "errors": v[1]} for k, v in tools.items()),
                  key=lambda d: (-d["calls"], d["tool"]))


def _prompt_rows(conn, task, windows, per_prompt, prompt_agents, prompt_span, tools, active_ivals):
    """Per-prompt breakdown. Every API call carries the id of the user prompt
    that caused it (subagents inherit the prompt that spawned them).
    Calls of turns Claude started on its own (background-agent notifications)
    are grouped as 'automatic'."""
    rows = []
    known = set()
    tool_counts: Dict[Optional[str], int] = {}
    if tools is not None:
        for sid, a, b in windows:
            for r in conn.execute("SELECT prompt_id, COUNT(*) AS n FROM tool_calls WHERE session_id = ? "
                                  "AND ts >= ? AND ts <= ? GROUP BY prompt_id", (sid, a, b)):
                tool_counts[r["prompt_id"]] = tool_counts.get(r["prompt_id"], 0) + r["n"]
    by_prompt_turns: Dict[str, list] = {}
    for iv in active_ivals:
        if len(iv) > 3 and iv[3]:
            by_prompt_turns.setdefault(iv[3], []).append(iv)
    if task["id"]:
        prompt_list = conn.execute("SELECT * FROM prompts WHERE task_id = ? ORDER BY ts, id", (task["id"],)).fetchall()
    else:                       # a whole session (sessions index): its own prompts
        prompt_list = []
        for sid, a, b in windows:
            prompt_list.extend(conn.execute("SELECT * FROM prompts WHERE task_id IS NULL AND session_id = ? "
                                            "AND ts >= ? AND ts <= ? ORDER BY ts, id", (sid, a, b)).fetchall())
    i = 0
    for p in prompt_list:
        pid = p["prompt_id"]
        if pid:
            if pid in known:
                continue            # stored twice (the hook registered twice): one prompt
            known.add(pid)
        i += 1
        tot = per_prompt.get(pid) if pid else None
        if pid in by_prompt_turns:
            dur = _union_length(by_prompt_turns[pid])
        elif pid in prompt_span:
            dur = max(0.0, prompt_span[pid][1] - p["ts"])
        else:
            dur = 0.0
        rows.append(dict(
            (tot or Tot()).as_dict(), index=i, ts=p["ts"], prompt=p["prompt"], prompt_id=pid,
            agents=len(prompt_agents.get(pid, ())) if pid else 0, duration_s=round(dur, 1),
            tool_calls=tool_counts.get(pid) if tools is not None and pid else None))
    auto = Tot()
    auto_ids = [pid for pid in per_prompt if pid and pid not in known]
    for pid in auto_ids:
        auto.add(per_prompt[pid])
    if None in per_prompt:
        auto.add(per_prompt[None])
    automatic = None
    if auto.calls:
        # calls without a prompt id belong to no turn: with no automatic turn, say so instead of '(0)'
        automatic = dict(auto.as_dict(), turns=len(auto_ids),
                         label=("automatic turns (%d)" % len(auto_ids)) if auto_ids else "unattributed")
    return rows, automatic


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
        "SELECT uuid, ts_end, duration_ms, prompt_id FROM turns WHERE session_id = ? AND ts_end >= ? "
        "AND ts_end - duration_ms / 1000.0 <= ?", (sid, a, b)
    ):
        lo = max(t["ts_end"] - t["duration_ms"] / 1000.0, a)
        hi = min(t["ts_end"], b)
        if hi > lo:
            out.append((lo, hi, t["uuid"].startswith("hook:"), t["prompt_id"]))
    return out


def _open_turn(conn, sid, a, b, now) -> Optional[tuple]:
    """The turn running now (the hooks saw its prompt, no Stop yet) as a hook
    turn interval, clipped to [a, b] and to now. It counts until 10 minutes
    after the session's last API call at most, so a turn whose Stop never
    comes does not grow forever."""
    raw = db.kv_get(conn, "open_turn:" + sid)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        t0, pid = (float(d["t0"]), d.get("prompt_id")) if isinstance(d, dict) else (float(d), None)
    except (ValueError, TypeError, KeyError):
        return None
    last = conn.execute("SELECT MAX(ts) FROM usage WHERE session_id = ?", (sid,)).fetchone()[0]
    lo = max(t0, a)
    hi = min(b, now, max(t0, last or t0) + OPEN_TURN_IDLE_S)
    return (lo, hi, True, pid) if hi > lo else None


def _clip_span(first, last, wins, fallback) -> tuple:
    """[first, last] cut to the windows of its session that it overlaps (not
    to one span over all of them: another task may have run in between)."""
    hit = [(a, b) for a, b in wins if b >= first and a <= last]
    if not hit:
        hit = [(a, b) for a, b in wins if b >= fallback[0] and a <= fallback[1]]
        first, last = fallback
    if not hit:
        return first, last
    return max(first, min(a for a, _ in hit)), min(last, max(b for _, b in hit))


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


def save_snapshot(conn, task_id: int, rep: dict) -> None:
    summary = {k: rep[k] for k in ("task", "time", "totals", "unlogged")}
    conn.execute(
        "INSERT OR REPLACE INTO task_snapshots(task_id, computed_at, report_json, summary_json) VALUES(?,?,?,?)",
        (task_id, time.time(), json.dumps(rep), json.dumps(summary)),
    )


def _refresh_costs(conn, task) -> List[str]:
    """Pick up new session cost records; returns the task's session ids."""
    from . import reconcile
    sids = tasks.session_ids(conn, task["id"])
    for sid in sids:
        try:
            reconcile.refresh(conn, sid)
        except Exception:
            pass
    return sids


def _fresh_snapshot(conn, task, sids, column: str):
    """Stored snapshot (report_json or summary_json) unless a session got a
    newer cost record after it was computed, or API calls of the task were
    read after it (reading the transcripts failed when the task ended)."""
    from . import reconcile
    row = conn.execute("SELECT computed_at, %s FROM task_snapshots WHERE task_id = ?" % column,
                       (task["id"],)).fetchone()
    if row is None or row[1] is None or reconcile.updated_since(conn, sids, row["computed_at"]):
        return None
    data = json.loads(row[1])
    if _usage_calls(conn, task) != (data.get("totals") or {}).get("calls"):
        return None
    data["task"]["name"] = task["name"]
    data["task"]["todo"] = _todo_head(conn, task)
    return data


def _usage_calls(conn, task) -> int:
    """API calls stored in the task's windows now (what build() would count)."""
    return sum(conn.execute("SELECT COUNT(*) FROM usage WHERE session_id = ? AND ts >= ? AND ts <= ?",
                            (sid, a, b)).fetchone()[0] for sid, a, b in tasks.task_windows(conn, task))


def summary_for(conn, task, live: bool = True) -> dict:
    """Full report: frozen snapshot if ended (rebuilt when Claude Code has
    since written a newer session cost record, or API calls of the task were
    read after it was frozen), fresh build if active."""
    sids = _refresh_costs(conn, task)
    if task["status"] != "active":
        _reread(conn, task)
        rep = _fresh_snapshot(conn, task, sids, "report_json")
        if rep is None:
            rep = build(conn, task)
            save_snapshot(conn, task["id"], rep)
        return rep
    if live:
        _ingest(conn, sids)
    return build(conn, task)


def summary(conn, task) -> dict:
    """Totals only (task/time/totals/unlogged), cheap enough for lists and
    the status line."""
    if task["status"] == "active":
        _ingest(conn, tasks.session_ids(conn, task["id"]))
        return build(conn, task, detail=False)
    sids = _refresh_costs(conn, task)
    _reread(conn, task)
    data = _fresh_snapshot(conn, task, sids, "summary_json")
    if data is None:
        rep = summary_for(conn, task)
        data = {k: rep.get(k) for k in ("task", "time", "totals", "unlogged")}
        conn.execute("UPDATE task_snapshots SET summary_json = ? WHERE task_id = ? AND summary_json IS NULL",
                     (json.dumps(data), task["id"]))      # backfill snapshots made before schema v3
    return data


def _reread(conn, task) -> None:
    """Reading the transcripts failed when the task ended (tasks._ingest_before_end):
    read them now; the snapshot is then rebuilt, as its API calls differ."""
    from . import ingest
    key = "reread:%d" % task["id"]
    if not db.kv_get(conn, key):
        return
    try:
        ingest.ingest_sessions(conn, tasks.session_ids(conn, task["id"]))
    except Exception:
        return
    with db.transaction(conn):
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def _ingest(conn, sids) -> None:
    from . import ingest
    try:
        ingest.ingest_sessions(conn, sids)
    except Exception:
        pass
