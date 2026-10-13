"""Plain-text rendering of reports for the terminal / blocked-hook messages.

Every message has the same shape, whichever agent or CLI shows it:

  ▍Tally · ▶ Task #3 started: Fix login             head(): brand, mark, what happened
    Counting this session, its subagents …          body, indented two spaces
    → /tally:status live totals · /tally:end finish hints(): what to do next

paint() colours that shape for a terminal; agents get it as plain text.
"""

import os
import re
import time
from typing import List, Optional

AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "pi": "pi"}

BRAND = "▍Tally"
STATUS_MARKS = {"todo": "○", "doing": "▶", "blocked": "⏸", "done": "✓", "dropped": "✗"}


def head(text: str, mark: str = "") -> str:
    """First line of every message: '▍Tally · ✓ text'."""
    return "%s · %s%s" % (BRAND, mark + " " if mark else "", text)


def hints(*items: str) -> str:
    """Last line of a message: the next commands, '  → /tally:status live totals · /tally:end finish'."""
    return "  → " + " · ".join(i for i in items if i)


def error(text: str, *hint_items: str) -> str:
    out = [head(text, "✗")]
    if hint_items:
        out.append(hints(*hint_items))
    return "\n".join(out)


def usage(syntax: str, what: str = "") -> str:
    return error("Usage: " + syntax, what) if what else error("Usage: " + syntax)


# ------------------------------------------------------------------ terminal colours

_ACCENT = "\033[1;38;5;202m"        # the logo's orange
_BOLD, _DIM, _RED, _RESET = "\033[1m", "\033[2m", "\033[31m", "\033[0m"
_HINT_RE = re.compile(r"^  → ")

def use_color(stream) -> bool:
    """Colours only on a terminal, and never with NO_COLOR (https://no-color.org) or TERM=dumb."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def paint(text: str) -> str:
    """Brand in orange, the head line bold (red for errors), hint lines dim."""
    out = []
    for line in text.split("\n"):
        if line.startswith(BRAND):
            rest = line[len(BRAND):]
            out.append(_ACCENT + BRAND + _RESET + _BOLD + (_RED if rest.startswith(" · ✗") else "") + rest + _RESET)
        elif _HINT_RE.match(line):
            out.append(_DIM + line + _RESET)
        else:
            out.append(line)
    return "\n".join(out)


TAGLINE = "Per-task token & time tracking for Claude Code, Codex and pi"
# figlet font "standard"
WORDMARK = (
    " _____     _ _",
    "|_   _|_ _| | |_   _",
    "  | |/ _` | | | | | |",
    "  | | (_| | | | |_| |",
    "  |_|\\__,_|_|_|\\__, |",
    "               |___/",
)
GRADIENT = (214, 208, 208, 202, 202, 166)    # gold to burnt orange, one shade per line (256-colour palette)


def banner(subtitle: str = "", color: bool = False, ascii_only: bool = False) -> str:
    """The wordmark, the tagline and a dim subtitle such as the version or "setup · 0.5.0"."""
    def fg(code: int, text: str) -> str:
        return "\033[38;5;%dm%s%s" % (code, text, _RESET) if color else text

    out = [""]
    out += ["  " + fg(shade, line) for shade, line in zip(GRADIENT, WORDMARK)]
    mark = fg(208, "|" if ascii_only else "▍")
    out.append("  %s %s" % (mark, _BOLD + TAGLINE + _RESET if color else TAGLINE))
    if subtitle:
        out.append("  " + (_DIM + subtitle + _RESET if color else subtitle))
    out.append("")
    return "\n".join(out)


def can_draw(stream) -> bool:
    """Block characters need a UTF-8 terminal; otherwise plain ASCII is used."""
    return "utf" in (getattr(stream, "encoding", None) or "").lower()


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
    s = "${:,.0f}".format(c) if c >= 999.5 else "$%.2f" % c if c >= 0.995 or c == 0 else "$%.3f" % c
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


def render_report(rep: dict, detail: bool = True, title: Optional[str] = None, mark: str = "") -> str:
    """title/mark replace the default head line ('Task #3 · name   [● active]')."""
    t = rep["task"]
    tm = rep["time"]
    tot = rep["totals"]
    out = []
    sess = rep.get("session")
    if title is not None:
        out.append(head(title, mark))
    elif sess:
        out.append(head("Session %s · %s   [%s]" % (sess["session_id"], _cut(sess.get("title") or "", 60),
                                                   AGENT_NAMES.get(sess["agent"], sess["agent"]))))
    else:
        out.append(head("Task #%d · %s   [%s]" % (t["id"], _cut(t["name"], 70), status_mark(t["status"]))))
    where = []
    if t.get("project"):
        where.append("project " + t["project"])
    if t.get("todo"):
        where.append("todo #%d (%s)" % (t["todo"]["id"], t["todo"]["status"]))
    if where:
        out.append("  " + " · ".join(where))
    if t.get("initial_prompt") and t["initial_prompt"].strip() != t["name"].rstrip("…").strip():
        out.append("  prompt   " + _cut(t["initial_prompt"], 90))
    started = when(t["started_at"])
    ended = (" → " + when(t["ended_at"], "%H:%M" if when(t["ended_at"], "%Y-%m-%d") == when(t["started_at"], "%Y-%m-%d") else "%Y-%m-%d %H:%M")) if t.get("ended_at") else ""
    out.append("  time     %s wall · %s active (%d turns) · %s%s" % (
        duration(tm["wall_s"]), duration(tm["active_s"]), tm["turns"], started, ended))
    out.append("  tokens   " + token_line(tot))
    st = rep.get("stats") or {}
    if st.get("prompts"):
        out.append("  prompts  %d · avg %s tokens · %s per prompt%s" % (
            st["prompts"], tokens(st["tokens_per_prompt"]), money({"cost": st["cost_per_prompt"]}),
            (" · +%d automatic turns" % st["automatic_turns"]) if st.get("automatic_turns") else ""))
    out.append("  cost     ≈ %s at list price · %d API calls%s" % (
        money(tot), tot["calls"], (" · %d web searches" % tot["web_search"]) if tot.get("web_search") else ""))
    un = rep.get("unlogged") or {}
    if un.get("status") in ("estimated", "partial") and un.get("total"):
        out.append("  unlogged ≈ +%s tokens · +%s  Claude Code's own calls (agent summaries, suggestions, titles), "
                   "estimated from the session cost record%s" % (
                       tokens(un["total"]), money(un), " (partial)" if un["status"] == "partial" else ""))
    if rep["by_model"]:
        out.append("  models   " + " · ".join(
            "%s %s %s" % (m["model"].replace("claude-", ""), tokens(m["total"]), money(m)) for m in rep["by_model"]))
    if not detail:
        return "\n".join(out)
    if tot["calls"]:
        out.append("")
        out.extend("  " + line for line in render_tree(rep["tree"]))
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
        return "\n".join([head("No tasks yet"), hints("/tally:start <name> start one", "/tally:new <prompt>")])
    out = [head("Tasks (%d)" % len(rows)), "",
           "  %4s  %-8s %-16s %9s %9s %9s %8s  %s" % ("#", "status", "started", "wall", "active", "tokens", "cost",
                                                     "name")]
    for r in rows:
        out.append("  %4d  %-8s %-16s %9s %9s %9s %8s  %s" % (
            r["id"], "active" if r["status"] == "active" else "ended", when(r["started_at"]),
            duration(r["wall_s"]), duration(r["active_s"]), tokens(r["total"]), money(r),
            _cut(r["name"], 48)))
    return "\n".join(out)


def pct(x: Optional[float]) -> str:
    return "-" if x is None else "%.0f%%" % (x * 100)


def render_stats(rep: dict) -> str:
    """Detailed stats: header, averages, per-prompt table, tools."""
    out = [render_report(rep, detail=False), ""]
    st = rep.get("stats") or {}
    out.append("  stats    %s prompts · %s automatic turns · %s subagents · %s workflows · %s models" % (
        st.get("prompts", 0), st.get("automatic_turns", 0), st.get("subagents", 0), st.get("workflows", 0),
        st.get("models", 0)))
    out.append("           avg turn %s · longest %s · cache hit %s · %s output tokens / active min" % (
        duration(st.get("avg_turn_s")), duration(st.get("max_turn_s")), pct(st.get("cache_hit_ratio")),
        tokens(st.get("output_per_active_min"))))
    if st.get("tool_calls") is not None:
        out.append("           %d tool calls · %d errors" % (st["tool_calls"], st["tool_errors"] or 0))
    prompts = rep.get("prompts")
    if prompts:
        out.append("")
        out.append("  %3s  %-5s %-34s %8s %8s %8s %8s %5s %6s" % (
            "#", "time", "prompt", "tokens", "output", "cost", "took", "calls", "agents"))
        for p in prompts:
            text = p["prompt"] if p["prompt"] is not None else "(not stored)"
            out.append("  %3d  %-5s %-34s %8s %8s %8s %8s %5s %6s" % (
                p["index"], when(p["ts"], "%H:%M"), _cut(text, 34), tokens(p["total"]), tokens(p["output"]),
                money(p), duration(p["duration_s"]), p["calls"], p["agents"] or ""))
        auto = st.get("automatic")
        if auto:
            out.append("  %3s  %-5s %-34s %8s %8s %8s %8s %5s" % (
                "", "", "automatic turns (%d)" % auto["turns"], tokens(auto["total"]), tokens(auto["output"]),
                money(auto), "", auto["calls"]))
    elif prompts is None:
        out.append("")
        out.append("  per-prompt breakdown is off (log.depth = summary); /tally:settings log.depth prompts")
    tools = st.get("tools")
    if tools:
        out.append("")
        out.append("  tools")
        width = max(t["calls"] for t in tools)
        for t in tools[:12]:
            bar = "█" * max(1, int(round(16 * t["calls"] / float(width))))
            out.append("    %-34s %5d  %-16s%s" % (_cut(t["tool"], 34), t["calls"], bar,
                                                  ("  %d errors" % t["errors"]) if t["errors"] else ""))
        if len(tools) > 12:
            out.append("    … %d more tools" % (len(tools) - 12))
    elif tools is None and st:
        out.append("")
        out.append("  tool statistics are off (log.depth = %s); /tally:settings log.depth calls" % st.get("depth"))
    return "\n".join(out)


def render_todos(conn, rows: list, project: Optional[str], show_project: bool = False,
                 tree: bool = False) -> str:
    from . import todos
    where = ("project " + project) if project else "all projects"
    if not rows:
        return "\n".join([head("No open todos in %s" % where, "📋"),
                          hints("/tally:todo <text> [@project] [!!] [due:fri] add one")])
    now = time.time()
    out = [head("Todos · %s (%d)" % (where, len(rows)), "📋")]

    def emit(r, depth):
        line = "  " + "   " * depth + todos.line_for(r, now, show_project=show_project and depth == 0)
        done, total = todos.progress(conn, r["id"])
        if total:
            line += "  [%d/%d]" % (done, total)
        out.append(line)
        if tree:
            for c in todos.children(conn, r["id"]):
                emit(c, depth + 1)

    for r in rows:
        emit(r, 0)
    out.append("")
    out.append(hints("/tally:take <id> start measuring", "/tally:do <id> let the agent work on it", "/tally:done"))
    return "\n".join(out)


def render_project(st: dict) -> str:
    from .projects import MODE_HELP
    t, w, m = st["totals"], st["last_7d"], st["last_30d"]
    out = [head("Project %s   [%s: %s]" % (st["project"], st["mode"], MODE_HELP.get(st["mode"], "")))]
    if st["mode"] == "ask":
        out.append(hints("/tally:project track | tasks | off  set it up"))
    out.append("  all time %s tokens · %s · %d API calls · %d sessions" % (
        tokens(t["total"]), money(t), t["calls"], st["sessions"]))
    out.append("  last 7d  %s tokens · %s      last 30d %s tokens · %s" % (
        tokens(w["total"]), money(w), tokens(m["total"]), money(m)))
    if st["by_model"]:
        out.append("  models   " + " · ".join("%s %s %s" % (x["model"].replace("claude-", ""), tokens(x["total"]),
                                                            money(x)) for x in st["by_model"][:4]))
    tk = st["tasks"]
    share = (" · %s of project tokens" % pct(tk["share"])) if tk["share"] is not None else ""
    out.append("  tasks    %d (%d active) · %s tokens%s" % (tk["count"], tk["active"], tokens(tk["tokens"]), share))
    td = st["todos"]
    out.append("  todos    %d open (%d doing, %d blocked) · %d done" % (
        td["todo"] + td["doing"] + td["blocked"], td["doing"], td["blocked"], td["done"]))
    days = st["daily"]
    if any(d["total"] for d in days):
        peak = max(d["total"] for d in days)
        blocks = " ▁▂▃▄▅▆▇█"
        spark = "".join(blocks[0 if not d["total"] else max(1, int(round(8 * d["total"] / float(peak))))]
                        for d in days)
        out.append("  %d days %s  (peak %s)" % (len(days), spark, tokens(peak)))
    if tk["recent"]:
        out.append("")
        for x in tk["recent"][:5]:
            out.append("    #%-4d %-40s %9s %8s %s" % (x["id"], _cut(x["name"], 40), tokens(x["total"]),
                                                    money(x), "● active" if x["status"] == "active" else ""))
    return "\n".join(out)



def _short_model(m: str) -> str:
    return m.replace("claude-", "").split("/")[-1]


def render_sessions(ov: dict, limit: Optional[int] = 40) -> str:
    t = ov["totals"]
    span = ("last %g day%s" % (ov["days"], "" if ov["days"] == 1 else "s")) if ov["days"] else "all time"
    out = [head("Sessions, %s: %d · %s tokens · %s · %d prompts · active %s" % (
        span, t["sessions"], tokens(t["total"]), money(t), t["prompts"], duration(t["active_s"])))]
    if len(ov["by_agent"]) > 1:
        out.append("  " + "   ".join("%s %d · %s · %s" % (AGENT_NAMES.get(a["agent"], a["agent"]), a["sessions"],
                                                         tokens(a["total"]), money(a)) for a in ov["by_agent"]))
    rows = ov["sessions"][:limit] if limit else ov["sessions"]
    rest = ov.get("not_tracked") or {}
    not_tracked = ("  + %d session%s in %d project%s not tracked, so not listed: /tally:project track in a "
                   "project, or the dashboard's Projects and Sessions" % (
                       rest["sessions"], "" if rest["sessions"] == 1 else "s",
                       rest["projects"], "" if rest["projects"] == 1 else "s")) if rest.get("sessions") else None
    if not rows:
        out.append("  no sessions with API calls in this period")
        if not_tracked:
            out.append(not_tracked)
        return "\n".join(out)
    out.append("")
    out.append("  %-8s %-16s %-6s %-18s %8s %8s %5s %8s %9s  %-24s %s" % (
        "id", "last activity", "agent", "project", "span", "active", "prm", "tokens", "cost", "models", "title / tasks"))
    for s in rows:
        st = s["totals"]
        cost = dict(st, cost=st.get("cost_with_unlogged", st["cost"]))
        models = ", ".join(_short_model(m["model"]) for m in s["models"][:2])
        if len(s["models"]) > 2:
            models += " +%d" % (len(s["models"]) - 2)
        label = s["title"] or ""
        if s["tasks"]:
            label = ("#" + " #".join(str(x["id"]) for x in s["tasks"]) + " " + label).strip()
        out.append("  %-8s %-16s %-6s %-18s %8s %8s %5d %8s %9s  %-24s %s" % (
            s["session_id"][:8], when(s["last_ts"]), s["agent"], _cut(s["project"] or "-", 18), duration(s["wall_s"]),
            duration(s["active_s"]), s["prompts"], tokens(st["total"]), money(cost), _cut(models, 24),
            _cut(label, 48)))
    if limit and len(ov["sessions"]) > limit:
        out.append("  … %d more (--limit 0 for all)" % (len(ov["sessions"]) - limit))
    out.append("")
    if not_tracked:
        out.append(not_tracked)
    out.append(hints("tally session <id or id prefix> for the full report of one session"))
    return "\n".join(out)
