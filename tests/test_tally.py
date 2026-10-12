"""End-to-end tests with synthetic Claude Code transcripts.

Run:  python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
sys.path.insert(0, str(PLUGIN))

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "99999999-8888-7777-6666-555555555555"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def assistant(ts, mid, req, model="claude-opus-5-5", inp=10, out=100, read=1000, w5=200, w1=0,
              agent_id=None, sidechain=False, speed="standard"):
    line = {
        "type": "assistant", "timestamp": iso(ts), "requestId": req, "uuid": "u-%s-%s" % (mid, out),
        "isSidechain": sidechain, "sessionId": SID,
        "message": {
            "id": mid, "model": model, "role": "assistant", "type": "message",
            "content": [{"type": "text", "text": "x"}],
            "usage": {
                "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                "cache_creation_input_tokens": w5 + w1,
                "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
                "server_tool_use": {"web_search_requests": 0}, "speed": speed,
            },
        },
    }
    if agent_id:
        line["agentId"] = agent_id
    return line


def user(ts, text="hi", agent_id=None):
    d = {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": text}, "uuid": "uu%s" % ts}
    if agent_id:
        d["agentId"] = agent_id
        d["isSidechain"] = True
    return d


def turn(ts, dur_ms, uid):
    return {"type": "system", "subtype": "turn_duration", "durationMs": dur_ms, "timestamp": iso(ts),
            "uuid": uid, "isSidechain": False}


class Env:
    """Isolated TALLY_HOME + fake ~/.claude/projects."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tc-test-"))
        self.home = self.tmp / "home"
        self.projects = self.tmp / "projects"
        self.proj = self.projects / "-Users-me-proj"
        self.proj.mkdir(parents=True)
        os.environ["TALLY_HOME"] = str(self.home)
        os.environ["TALLY_PROJECTS_DIR"] = str(self.projects)
        os.environ["CLAUDE_PID"] = "4242"

    def main(self, sid=SID) -> Path:
        return self.proj / (sid + ".jsonl")

    def sdir(self, sid=SID) -> Path:
        return self.proj / sid

    def append(self, path: Path, *lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for l in lines:
                fh.write(json.dumps(l) + "\n")

    def meta(self, path: Path, meta: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta))

    def hook(self, event, sid=SID, **fields):
        from tally import hooks
        payload = {"session_id": sid, "transcript_path": str(self.main(sid)), "cwd": "/Users/me/proj",
                   "hook_event_name": event}
        payload.update(fields)
        out = io.StringIO()
        hooks.main(event, stdin=io.StringIO(json.dumps(payload)), stdout=out)
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class CommandParsing(unittest.TestCase):
    def test_parse(self):
        from tally import commands
        self.assertEqual(commands.parse("/tally:start Fix login"), ("start", "Fix login"))
        self.assertEqual(commands.parse("  /tally:end 12 done  "), ("end", "12 done"))
        self.assertEqual(commands.parse("/tally:status"), ("status", ""))
        self.assertEqual(commands.parse("/tally:new line one\nline two"), ("new", "line one\nline two"))
        self.assertIsNone(commands.parse("/tally:unknown x"))
        self.assertIsNone(commands.parse("please /tally:start"))
        self.assertIsNone(commands.parse("/tasks"))


class Pricing(unittest.TestCase):
    def test_models(self):
        from tally.pricing import Pricer, normalize_model
        self.assertEqual(normalize_model("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(normalize_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")
        self.assertEqual(normalize_model("us.anthropic.claude-sonnet-4-5-20250929-v1:0"), "claude-sonnet-4-5")
        p = Pricer()
        self.assertEqual(p.resolve("claude-opus-5-5"), "claude-opus-5-5")
        self.assertEqual(p.resolve("claude-opus-5"), "claude-opus-5")
        # 1M input on Opus 5.5 = $4, 1M output = $20, 1M cache read = $0.20
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000), 24.2)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 0, 1_000_000, speed="fast"), 40.0)
        self.assertAlmostEqual(p.cost("claude-sonnet-5-5", 0, 0, 0, 1_000_000, 1_000_000), 2.5 + 4.0)
        self.assertAlmostEqual(p.cost("claude-sonnet-5-5", 0, 0, 1_000_000), 0.10)
        self.assertIsNone(p.cost("some-other-model", 1, 1))
        p2 = Pricer({"my-model": {"input": 1, "output": 2}})
        self.assertAlmostEqual(p2.cost("my-model", 1_000_000, 1_000_000), 3.0)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_full_lifecycle(self):
        e = self.env
        now = time.time()
        # history before the task: must not be counted
        e.append(e.main(), user(now - 300), assistant(now - 290, "msg_old", "req_old", out=5000))
        self.assertIsNone(e.hook("SessionStart", source="startup"))

        res = e.hook("UserPromptSubmit", prompt="/tally:start Fix login bug")
        self.assertEqual(res["decision"], "block")
        self.assertIn("Task #1 started: Fix login bug", res["reason"])

        self.assertIsNone(e.hook("UserPromptSubmit", prompt="Please fix the redirect loop in login"))

        t = time.time()
        S = 0.01                     # compress the timeline: 1 unit = 10 ms
        main = e.main()
        # one response written as 2 lines: partial output first, final last
        e.append(main, user(t + 1 * S, "Please fix"),
                 assistant(t + 2 * S, "msg_a", "req_a", out=7),
                 assistant(t + 2.1 * S, "msg_a", "req_a", out=120),
                 assistant(t + 30 * S, "msg_b", "req_b", inp=5, out=80, read=2000, w5=0, w1=300))
        # subagent (general-purpose) with a nested fork
        sub = e.sdir() / "subagents"
        e.meta(sub / "agent-aaa111.meta.json", {"agentType": "general-purpose", "description": "Explore auth",
                                                "toolUseId": "toolu_1", "spawnDepth": 1})
        e.append(sub / "agent-aaa111.jsonl", user(t + 3 * S, agent_id="aaa111"),
                 assistant(t + 4 * S, "msg_s1", "req_s1", model="claude-sonnet-5-5", out=50, read=500, w5=100,
                           agent_id="aaa111", sidechain=True),
                 assistant(t + 20 * S, "msg_s2", "req_s2", model="claude-sonnet-5-5", out=60, read=600, w5=0,
                           agent_id="aaa111", sidechain=True))
        e.meta(sub / "agent-bbb222.meta.json", {"agentType": "fork", "isFork": True, "description": "Write tests",
                                                "parentAgentId": "aaa111", "spawnDepth": 2})
        e.append(sub / "agent-bbb222.jsonl",
                 assistant(t + 10 * S, "msg_f1", "req_f1", out=40, read=400, w5=0, agent_id="bbb222", sidechain=True))
        # workflow with two phases
        wf = sub / "workflows" / "wf_abc-123"
        for i, (aid, phase) in enumerate([("ccc1", "Research"), ("ccc2", "Research"), ("ccc3", "Verify")]):
            e.meta(wf / ("agent-%s.meta.json" % aid), {"agentType": "workflow-subagent", "description": "item %d" % i,
                                                       "workflowPhase": phase, "spawnDepth": 1, "model": "sonnet"})
            e.append(wf / ("agent-%s.jsonl" % aid),
                     assistant(t + (5 + i) * S, "msg_w%d" % i, "req_w%d" % i, model="claude-sonnet-5-5", out=10 * (i + 1),
                               read=100, w5=10, agent_id=aid, sidechain=True))
        (e.sdir() / "workflows").mkdir(parents=True, exist_ok=True)
        (e.sdir() / "workflows" / "wf_abc-123.json").write_text(json.dumps(
            {"runId": "wf_abc-123", "workflowName": "review-changes", "summary": "Review changed files",
             "status": "completed", "totalTokens": 999, "script": "x" * 1000}))
        e.append(main, turn(t + 31 * S, 300, "turn-1"))

        self.assertIsNone(e.hook("Stop"))
        self.assertIsNone(e.hook("SubagentStop", agent_id="aaa111", agent_type="general-purpose"))

        status = e.hook("UserPromptSubmit", prompt="/tally:status")
        self.assertEqual(status["decision"], "block")
        self.assertIn("Fix login bug", status["reason"])
        self.assertIn("workflow · review-changes", status["reason"])

        from tally import db, report, tasks
        conn = db.connect()
        task = tasks.get(conn, 1)
        self.assertEqual(task["initial_prompt"], "Please fix the redirect loop in login")
        rep = report.build(conn, task)
        tot = rep["totals"]
        # main: 120 + 80; sub: 50 + 60; fork: 40; workflow: 10 + 20 + 30
        self.assertEqual(tot["output"], 120 + 80 + 50 + 60 + 40 + 60)
        self.assertEqual(tot["calls"], 2 + 2 + 1 + 3)
        self.assertEqual(tot["cache_read"], 1000 + 2000 + 500 + 600 + 400 + 300)
        self.assertEqual(tot["cache_write"], 200 + 300 + 100 + 30)
        self.assertEqual(tot["input"], 10 + 5 + 10 + 10 + 10 + 30)
        # cost: opus-5-5 main + fork; sonnet-5-5 subagent + workflow
        from tally.pricing import Pricer
        p = Pricer()
        expected = (p.cost("claude-opus-5-5", 10, 120, 1000, 200) + p.cost("claude-opus-5-5", 5, 80, 2000, 0, 300)
                    + p.cost("claude-opus-5-5", 10, 40, 400, 0)
                    + p.cost("claude-sonnet-5-5", 10, 50, 500, 100) + p.cost("claude-sonnet-5-5", 10, 60, 600, 0)
                    + sum(p.cost("claude-sonnet-5-5", 10, 10 * (i + 1), 100, 10) for i in range(3)))
        self.assertAlmostEqual(tot["cost"], expected, places=6)

        main_node = rep["tree"][0]
        self.assertEqual(main_node["own"]["output"], 200)
        self.assertEqual(main_node["rollup"]["output"], tot["output"])
        kids = {c["label"]: c for c in main_node["children"]}
        self.assertIn("Explore auth", kids)
        explore = kids["Explore auth"]
        self.assertEqual(explore["own"]["output"], 110)
        self.assertEqual(explore["rollup"]["output"], 150)          # includes nested fork
        self.assertEqual(explore["children"][0]["kind"], "fork")
        wf_node = kids["review-changes"]
        self.assertEqual(wf_node["kind"], "workflow")
        self.assertEqual(wf_node["agent_count"], 3)
        self.assertEqual(wf_node["rollup"]["output"], 60)
        phases = {c["label"]: c for c in wf_node["children"]}
        self.assertEqual(phases["Research"]["rollup"]["output"], 30)
        self.assertEqual(phases["Verify"]["rollup"]["output"], 30)
        self.assertEqual(rep["workflows"][0]["reported_total_tokens"], 999)
        types = {x["agent_type"]: x for x in rep["by_type"]}
        self.assertEqual(types["workflow-subagent"]["agents"], 3)
        self.assertEqual(types["general-purpose"]["agents"], 1)
        self.assertAlmostEqual(rep["time"]["active_s"], 0.3, delta=0.05)
        self.assertEqual(rep["time"]["turns"], 1)

        # end the task; usage written afterwards is not counted
        time.sleep(max(0.0, t + 0.5 - time.time()))   # all synthetic usage is now in the past
        ended = e.hook("UserPromptSubmit", prompt="/tally:end shipped")
        self.assertIn("Task #1 ended", ended["reason"])
        time.sleep(0.05)
        e.append(main, assistant(time.time() + 5, "msg_after", "req_after", out=9999))
        self.assertIsNone(e.hook("Stop"))           # no active task: nothing ingested
        task = tasks.get(conn, 1)
        self.assertEqual(task["status"], "ended")
        self.assertEqual(task["end_note"], "shipped")
        snap = report.summary_for(conn, task)
        self.assertEqual(snap["totals"]["output"], tot["output"])

        # list shows it
        lst = e.hook("UserPromptSubmit", prompt="/tally:list")
        self.assertIn("Fix login bug", lst["reason"])

    def test_task_new_passes_prompt_through(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        res = e.hook("UserPromptSubmit", prompt="/tally:new Implement the export feature")
        self.assertIsNone(res)                       # not blocked: the model gets the prompt
        from tally import db, tasks
        conn = db.connect()
        t = tasks.active_for_session(conn, SID)
        self.assertEqual(t["name"], "Implement the export feature")
        self.assertEqual(t["initial_prompt"], "Implement the export feature")
        self.assertEqual(t["created_by"], "new")

    def test_untitled_start_takes_name_from_next_prompt(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start")
        e.hook("UserPromptSubmit", prompt="Refactor the billing module\nand add tests")
        from tally import db, tasks
        t = tasks.active_for_session(db.connect(), SID)
        self.assertEqual(t["name"], "Refactor the billing module and add tests")

    def test_start_while_active_ends_previous(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start one")
        res = e.hook("UserPromptSubmit", prompt="/tally:start two")
        self.assertIn("Task #1 ended", res["reason"])
        self.assertIn("Task #2 started: two", res["reason"])

    def test_clear_carries_task_to_new_session(self):
        e = self.env
        e.append(e.main(SID), user(time.time() - 10))
        e.hook("SessionStart", sid=SID, source="startup")
        e.hook("UserPromptSubmit", sid=SID, prompt="/tally:start long task")
        e.hook("SessionEnd", sid=SID, reason="clear")
        e.append(e.main(SID2), user(time.time()))
        e.hook("SessionStart", sid=SID2, source="clear")
        t = time.time()
        e.append(e.main(SID2), assistant(t + 1, "msg_after_clear", "r1", out=77))
        e.hook("Stop", sid=SID2)
        from tally import db, report, tasks
        conn = db.connect()
        task = tasks.active_for_session(conn, SID2)
        self.assertIsNotNone(task)
        self.assertEqual(task["id"], 1)
        self.assertEqual(report.build(conn, task)["totals"]["output"], 77)

    def test_resume_in_other_session_and_reopen(self):
        e = self.env
        e.append(e.main(SID), user(time.time() - 10))
        e.append(e.main(SID2), user(time.time() - 10))
        e.hook("UserPromptSubmit", sid=SID, prompt="/tally:start shared")
        res = e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:resume 1")
        self.assertIn("resumed", res["reason"])
        e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:end")
        res = e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:resume 1")
        self.assertIn("resumed", res["reason"])
        from tally import db, tasks
        self.assertEqual(tasks.get(db.connect(), 1)["status"], "active")

    def test_no_task_messages(self):
        e = self.env
        res = e.hook("UserPromptSubmit", prompt="/tally:status")
        self.assertIn("No active task", res["reason"])
        res = e.hook("UserPromptSubmit", prompt="/tally:end")
        self.assertIn("No active task", res["reason"])
        res = e.hook("UserPromptSubmit", prompt="/tally:help")
        self.assertIn("/tally:start", res["reason"])
        self.assertIsNone(e.hook("UserPromptSubmit", prompt="normal prompt"))

    def test_auto_start(self):
        e = self.env
        from tally import paths
        paths.save_config({"auto_start": "session"})
        e.append(e.main(), user(time.time() - 10))
        self.assertIsNone(e.hook("UserPromptSubmit", prompt="Build a CSV exporter"))
        self.assertIsNone(e.hook("UserPromptSubmit", prompt="second prompt"))
        from tally import db, tasks
        conn = db.connect()
        t = tasks.active_for_session(conn, SID)
        self.assertEqual(t["name"], "Build a CSV exporter")
        self.assertEqual(t["created_by"], "auto")
        self.assertEqual(len(tasks.list_tasks(conn)), 1)

    def test_unlogged_reconciliation(self):
        """cost-state at session exit: the unlogged part is pro-rated to the task."""
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start reconcile me")
        t = time.time()
        e.append(e.main(), assistant(t + 0.01, "m1", "r1", inp=0, out=100, read=900, w5=0))
        e.hook("Stop")
        time.sleep(max(0.0, t + 0.05 - time.time()))
        e.hook("UserPromptSubmit", prompt="/tally:end")
        # history before the task: same size as the task's usage -> task share = 50%
        hist = [assistant(t - 100, "h1", "rh", inp=0, out=100, read=900, w5=0)]
        content = e.main().read_text()
        e.main().write_text("".join(json.dumps(l) + "\n" for l in hist) + content)
        # Claude Code writes its own totals at exit: logged 2000 output-ish tokens + 2000 unlogged cache reads
        e.append(e.main(), {"type": "cost-state", "sessionId": SID, "totalCostUSD": 1.0, "modelUsage": {
            "claude-opus-5-5[1m]": {"inputTokens": 0, "outputTokens": 200, "cacheReadInputTokens": 3800,
                                    "cacheCreationInputTokens": 0, "costUSD": 1.0}}})
        from tally import db, report, tasks
        conn = db.connect()
        rep = report.summary_for(conn, tasks.get(conn, 1))
        un = rep["unlogged"]
        self.assertEqual(un["status"], "estimated")
        self.assertEqual(un["cache_read"], 1000)     # (3800 - 1800) * 50%
        self.assertEqual(un["output"], 0)
        from tally.pricing import Pricer
        logged_cost = 2 * Pricer().cost("claude-opus-5-5", 0, 100, 900)
        self.assertAlmostEqual(un["cost"], (1.0 - logged_cost) * 0.5)   # Claude's exact cost minus logged
        self.assertEqual(rep["totals"]["output"], 100)  # exact numbers unchanged

    def test_hook_never_fails(self):
        from tally import hooks
        out = io.StringIO()
        self.assertEqual(hooks.main("Stop", stdin=io.StringIO("not json"), stdout=out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_concurrent_hooks(self):
        """Parallel SubagentStop hooks (real processes) must not double count or error."""
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start parallel")
        t = time.time()
        sub = e.sdir() / "subagents"
        for i in range(8):
            e.meta(sub / ("agent-p%d.meta.json" % i), {"agentType": "general-purpose", "description": "p%d" % i})
            e.append(sub / ("agent-p%d.jsonl" % i),
                     *[assistant(t + 1 + j, "m%d_%d" % (i, j), "r%d_%d" % (i, j), out=1, agent_id="p%d" % i,
                                 sidechain=True) for j in range(50)])
        procs = []
        payload = json.dumps({"session_id": SID, "transcript_path": str(e.main()), "hook_event_name": "SubagentStop"})
        for _ in range(8):
            p = subprocess.Popen([sys.executable, str(PLUGIN / "bin" / "tally"), "hook", "SubagentStop"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
            p.stdin.write(payload.encode())
            p.stdin.close()
            procs.append(p)
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
            self.assertEqual(p.stdout.read(), b"")
        from tally import db, paths, report, tasks
        self.assertFalse(paths.error_log_path().exists(), paths.error_log_path().read_text()
                         if paths.error_log_path().exists() else "")
        conn = db.connect()
        rep = report.build(conn, tasks.get(conn, 1))
        self.assertEqual(rep["totals"]["calls"], 400)
        self.assertEqual(rep["totals"]["output"], 400)


class ReviewRegressions(unittest.TestCase):
    """One test per finding of the code review."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def _usage(self, out, mid):
        time.sleep(0.01)    # transcript timestamps have ms precision: stay clearly after the task start
        self.env.append(self.env.main(), assistant(time.time(), mid, "r" + mid, out=out))
        self.env.hook("Stop")
        time.sleep(0.02)

    def test_resume_ended_task_excludes_the_gap(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start A")
        self._usage(1, "a1")
        e.hook("UserPromptSubmit", prompt="/tally:start B")        # A auto-ends
        self._usage(10, "b1")
        e.hook("UserPromptSubmit", prompt="/tally:resume 1")       # B auto-ends, A reopens
        self._usage(100, "a2")
        e.hook("UserPromptSubmit", prompt="/tally:end")
        from tally import db, report, tasks
        conn = db.connect()
        self.assertEqual(report.summary_for(conn, tasks.get(conn, 1))["totals"]["output"], 101)
        self.assertEqual(report.summary_for(conn, tasks.get(conn, 2))["totals"]["output"], 10)

    def test_resume_same_task_keeps_unread_usage(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start bg")
        sub = e.sdir() / "subagents"
        e.meta(sub / "agent-bg1.meta.json", {"agentType": "general-purpose", "description": "bg"})
        time.sleep(0.01)
        e.append(sub / "agent-bg1.jsonl", assistant(time.time(), "bg_m1", "bg_r1", out=33, agent_id="bg1",
                                                    sidechain=True))
        e.hook("UserPromptSubmit", prompt="/tally:resume 1")       # no Stop since the agent wrote
        from tally import db, report, tasks
        conn = db.connect()
        self.assertEqual(report.summary_for(conn, tasks.get(conn, 1))["totals"]["output"], 33)

    def test_unlogged_estimate_ignores_per_call_tiers(self):
        from tally.pricing import Pricer
        p = Pricer()
        self.assertAlmostEqual(p.cost("claude-haiku-5-5", 600_000, 0), 0.30)            # one huge call: tier
        self.assertAlmostEqual(p.cost("claude-haiku-5-5", 600_000, 0, tiers=False), 0.06)  # sum of small calls

    def test_new_cost_record_refreshes_every_ended_task(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start one")
        self._usage(100, "o1")
        e.hook("UserPromptSubmit", prompt="/tally:start two")
        self._usage(100, "t1")
        e.hook("UserPromptSubmit", prompt="/tally:end")
        from tally import db, report, tasks
        conn = db.connect()
        for tid in (1, 2):                                          # snapshots: still pending
            self.assertEqual(report.summary_for(conn, tasks.get(conn, tid))["unlogged"]["status"], "pending")
        e.append(e.main(), {"type": "cost-state", "sessionId": SID, "totalCostUSD": 1, "modelUsage": {
            "claude-opus-5-5": {"inputTokens": 0, "outputTokens": 200, "cacheReadInputTokens": 2000 + 4000,
                                "cacheCreationInputTokens": 400}}})
        lst = e.hook("UserPromptSubmit", prompt="/tally:list")      # the list consumes the new record first
        self.assertIn("one", lst["reason"])
        for tid in (2, 1):
            un = report.summary_for(conn, tasks.get(conn, tid))["unlogged"]
            self.assertEqual(un["status"], "estimated", "task %d" % tid)
            self.assertEqual(un["cache_read"], 2000)

    def test_interrupted_turn_does_not_count_idle_time(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start t")
        from tally import db
        conn = db.connect()
        t0 = time.time() - 3600
        db.kv_set(conn, "open_turn:" + SID, repr(t0))             # turn opened an hour ago, never stopped
        e.append(e.main(), assistant(t0 + 5, "late", "rl", out=1))
        e.hook("UserPromptSubmit", prompt="next prompt")
        e.hook("Stop")
        rows = conn.execute("SELECT duration_ms FROM turns WHERE session_id = ? ORDER BY ts_end", (SID,)).fetchall()
        self.assertAlmostEqual(rows[0]["duration_ms"], 5000, delta=5)   # closed at its last API response
        self.assertLess(rows[-1]["duration_ms"], 5000)

    def test_session_command_sees_history_skipped_by_a_task(self):
        e = self.env
        e.append(e.main(), assistant(time.time() - 100, "hist", "rh", out=500))
        e.hook("UserPromptSubmit", prompt="/tally:start later")    # main DB skips the history
        r = subprocess.run([sys.executable, str(PLUGIN / "bin" / "tally"), "session", SID, "--json"],
                           capture_output=True, text=True, env=os.environ, timeout=60, check=False)
        self.assertEqual(json.loads(r.stdout)["totals"]["output"], 500)

    def test_workflow_record_on_a_line_mentioning_usage(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start wf")
        e.append(e.main(), {"type": "user", "timestamp": iso(time.time()),
                            "message": {"role": "user", "content": "the assistant reported its usage"},
                            "toolUseResult": {"runId": "wf_x-1", "workflowName": "named-flow", "status": "done"}})
        e.hook("Stop")
        from tally import db
        row = db.connect().execute("SELECT name FROM workflows WHERE run_id = 'wf_x-1'").fetchone()
        self.assertEqual(row["name"], "named-flow")

    def test_clear_carry_over_without_pid(self):
        e = self.env
        os.environ.pop("CLAUDE_PID", None)
        e.append(e.main(SID), user(time.time() - 10))
        e.hook("UserPromptSubmit", sid=SID, prompt="/tally:start keep me")
        e.hook("SessionEnd", sid=SID, reason="clear")
        e.hook("SessionStart", sid=SID2, source="clear")
        from tally import db, tasks
        t = tasks.active_for_session(db.connect(), SID2)
        self.assertIsNotNone(t)
        self.assertEqual(t["name"], "keep me")

    def test_dashboard_rejects_foreign_host(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            ok = urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=5)
            self.assertEqual(ok.status, 200)
            req = urllib.request.Request("http://127.0.0.1:%d/api/tasks" % port, headers={"Host": "evil.example:80"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 403)
        finally:
            httpd.shutdown()

    def test_migrates_v2_database(self):
        import sqlite3
        from tally import db, paths
        path = paths.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(str(path))
        old.executescript(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, initial_prompt TEXT, "
            "status TEXT NOT NULL DEFAULT 'active', started_at REAL NOT NULL, ended_at REAL, cwd TEXT, "
            "origin_session TEXT, created_by TEXT, end_note TEXT);"
            "CREATE TABLE task_sessions (task_id INTEGER NOT NULL, session_id TEXT NOT NULL, attached_at REAL NOT NULL, "
            "detached_at REAL, reason TEXT, PRIMARY KEY (task_id, session_id));"
            "CREATE TABLE task_snapshots (task_id INTEGER PRIMARY KEY, computed_at REAL NOT NULL, report_json TEXT NOT NULL);"
            "CREATE TABLE session_costs (session_id TEXT PRIMARY KEY, scan_offset INTEGER NOT NULL DEFAULT 0, "
            "cs_ts REAL, cs_json TEXT, logged_json TEXT);"
            "INSERT INTO tasks(name, status, started_at) VALUES ('old task', 'active', 1);"
            "INSERT INTO task_sessions VALUES (1, 'sess-old', 1, NULL, 'start');"
            "PRAGMA user_version = 2;")
        old.commit()
        old.close()
        conn = db.connect()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        row = conn.execute("SELECT id, task_id, session_id FROM task_sessions").fetchone()
        self.assertEqual((row["task_id"], row["session_id"]), (1, "sess-old"))
        self.assertIn("summary_json", db._columns(conn, "task_snapshots"))
        self.assertIn("updated_at", db._columns(conn, "session_costs"))
        from tally import tasks
        self.assertEqual(tasks.active_for_session(conn, "sess-old")["name"], "old task")


class CliSmoke(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(PLUGIN / "bin" / "tally")] + list(args),
                              capture_output=True, text=True, env=os.environ, timeout=60)

    def test_cli(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        r = self.run_cli("start", "--session", SID, "CLI", "task")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task #1 started: CLI task", r.stdout)
        e.append(e.main(), assistant(time.time() + 1, "m1", "r1", out=42))
        r = self.run_cli("status", "1", "--json")
        self.assertEqual(json.loads(r.stdout)["totals"]["output"], 42)
        r = subprocess.run([sys.executable, str(PLUGIN / "bin" / "tally"), "statusline"],
                           input=json.dumps({"session_id": SID}), capture_output=True, text=True, env=os.environ)
        self.assertIn("▶ #1 CLI task · 1.3k tok", r.stdout)
        r = self.run_cli("slash", "--session", SID, "end", "--", "1 finished via fallback")
        self.assertIn("Task #1 ended", r.stdout)
        r = self.run_cli("list")
        self.assertIn("CLI task", r.stdout)
        r = self.run_cli("export", "--format", "csv")
        self.assertIn("CLI task", r.stdout)
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
