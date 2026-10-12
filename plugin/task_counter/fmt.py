"""Plain-text rendering of reports for the terminal / blocked-hook messages."""

import time
from typing import List, Optional


def tokens(n: Optional[int]) -> str:
    if n is None:
        return "-"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return "%.1fk" % (n / 1000.0)
    if n < 1_000_000_000:
        return "%.2fM" % (n / 1_000_000.0)
    return "%.2fB" % (n / 1_000_000_000.0)


def duration(s: Optional[float]) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh %02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd %02dh" % (s // 86400, (s % 86400) // 3600)


def money(d: dict) -> str:
    c = d.get("cost")
    if c is None:
        return "-"
    if c == 0 and d.get("cost_partial"):
        return "n/a"
    s = "$%.2f" % c if c >= 0.995 or c == 0 else "$%.3f" % c
    return s + ("+" if d.get("cost_partial") else "")


def when(ts: Optional[float], fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not ts:
        return "-"
    return time.strftime(fmt, time.localtime(ts))


def _cut(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def status_mark(status: str) -> str:
    return "● active" if status == "active" else "■ ended"


def token_line(t: dict) -> str:
    return "%s total · in %s · out %s · cache write %s · cache read %s" % (
        tokens(t["total"]), tokens(t["input"]), tokens(t["output"]),
        tokens(t["cache_write"]), tokens(t["cache_read"]),
    )


def node_label(n: dict) -> str:
    kind = n["kind"]
    if kind == "main":
        return n["label"] + " (own)"
    if kind == "workflow":
        return "workflow · %s (%d agents)" % (n["label"], n.get("agent_count", 0))
    if kind == "phase":
        return "phase · %s (%d)" % (n["label"], n.get("agent_count", 0))
    atype = n.get("agent_type") or "agent"
    desc = n.get("description")
    if kind == "fork":
        atype = "fork"
    return "%s · %s" % (atype, desc) if desc and desc != atype else atype


COLS = "%-46s %8s %8s %9s %9s %6s"


def _row(label: str, d: dict, dur: Optional[float], prefix: str = "") -> str:
    return COLS % (prefix + _cut(label, 46 - len(prefix)), tokens(d["total"]), tokens(d["output"]), money(d),
                   duration(dur) if dur is not None else "", d["calls"])


def render_tree(tree: List[dict], max_children: int = 8, max_depth: int = 4) -> List[str]:
    lines = [COLS % ("breakdown", "tokens", "output", "cost", "time", "calls")]

    def walk(n: dict, prefix: str, is_last: bool, depth: int, top: bool):
        if top:
            branch, child_prefix = "", ""
        else:
            branch = "└─ " if is_last else "├─ "
            child_prefix = prefix + ("   " if is_last else "│  ")
        values = n["own"] if n["kind"] == "main" else n["rollup"]
        lines.append(_row(node_label(n), values, n.get("duration_s"), prefix + branch))
        kids = n.get("children") or []
        if depth >= max_depth and kids:
            lines.append(child_prefix + "└─ … %d nested" % len(kids))
            return
        shown = kids[:max_children]
        hidden = kids[max_children:]
        for i, c in enumerate(shown):
            walk(c, child_prefix, i == len(shown) - 1 and not hidden, depth + 1, False)
        if hidden:
            tot = sum(c["rollup"]["total"] for c in hidden)
            cost = sum(c["rollup"]["cost"] for c in hidden)
            lines.append(COLS % (child_prefix + "└─ … %d more" % len(hidden), tokens(tot), "", "$%.2f" % cost, "", ""))

    for n in tree:
        walk(n, "", True, 0, True)
    return lines


def render_report(rep: dict, detail: bool = True) -> str:
    t = rep["task"]
    tm = rep["time"]
    tot = rep["totals"]
    out = []
    out.append("Task #%d · %s   [%s]" % (t["id"], _cut(t["name"], 70), status_mark(t["status"])))
    if t.get("initial_prompt") and t["initial_prompt"].strip() != t["name"].rstrip("…").strip():
        out.append("  prompt   " + _cut(t["initial_prompt"], 90))
    started = when(t["started_at"])
    ended = (" → " + when(t["ended_at"], "%H:%M" if when(t["ended_at"], "%Y-%m-%d") == when(t["started_at"], "%Y-%m-%d") else "%Y-%m-%d %H:%M")) if t.get("ended_at") else ""
    out.append("  time     %s wall · %s active (%d turns) · %s%s" % (
        duration(tm["wall_s"]), duration(tm["active_s"]), tm["turns"], started, ended))
    out.append("  tokens   " + token_line(tot))
    out.append("  cost     ≈ %s at list price · %d API calls%s" % (
        money(tot), tot["calls"], (" · %d web searches" % tot["web_search"]) if tot.get("web_search") else ""))
    if rep["by_model"]:
        out.append("  models   " + " · ".join(
            "%s %s %s" % (m["model"].replace("claude-", ""), tokens(m["total"]), money(m)) for m in rep["by_model"]))
    if not detail:
        return "\n".join(out)
    if tot["calls"]:
        out.append("")
        out.extend("  " + l for l in render_tree(rep["tree"]))
        out.append("  " + _row("TOTAL", tot, tm["wall_s"]))
        types = [x for x in rep["by_type"] if x["agent_type"] != "main"]
        if types:
            out.append("")
            out.append("  by agent type")
            for x in rep["by_type"]:
                out.append("    %-26s %4s agents %9s %9s" % (
                    _cut(x["agent_type"], 26), x["agents"], tokens(x["total"]), money(x)))
    else:
        out.append("")
        out.append("  No API usage recorded yet.")
    if t.get("end_note"):
        out.append("  note     " + _cut(t["end_note"], 90))
    return "\n".join(out)


def render_list(rows: List[dict]) -> str:
    if not rows:
        return "No tasks yet. Start one with /task:start <name> or /task:new <prompt>."
    out = ["%4s  %-8s %-16s %9s %9s %9s %8s  %s" % ("#", "status", "started", "wall", "active", "tokens", "cost", "name")]
    for r in rows:
        out.append("%4d  %-8s %-16s %9s %9s %9s %8s  %s" % (
            r["id"], "active" if r["status"] == "active" else "ended", when(r["started_at"]),
            duration(r["wall_s"]), duration(r["active_s"]), tokens(r["total"]), money(r),
            _cut(r["name"], 48)))
    return "\n".join(out)
