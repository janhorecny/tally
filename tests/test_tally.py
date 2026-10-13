"""End-to-end tests with synthetic Claude Code transcripts.

Run:  python3 -m unittest discover -s tests -v
"""

import atexit
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
sys.path.insert(0, str(PLUGIN))

# Safety net: even a test that forgets Env() must never read or write the real
# ~/.claude, ~/.codex or ~/.pi. Env() points every test at its own folder.
_SAFE = Path(tempfile.mkdtemp(prefix="tc-tests-"))
atexit.register(shutil.rmtree, _SAFE, True)
for _var, _sub in (("TALLY_HOME", "home"), ("TALLY_PROJECTS_DIR", "projects"), ("CLAUDE_CONFIG_DIR", "claude"),
                   ("CODEX_HOME", "codex"), ("PI_CODING_AGENT_DIR", "pi")):
    os.environ[_var] = str(_SAFE / _sub)
os.environ["TALLY_NO_UPDATE_CHECK"] = "1"       # no test asks GitHub; Updates turns it on with a local URL
os.environ.pop("TALLY_UPDATE_URL", None)

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "99999999-8888-7777-6666-555555555555"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def assistant(ts, mid, req, model="claude-opus-5-5", inp=10, out=100, read=1000, w5=200, w1=0,
              agent_id=None, sidechain=False, speed="standard", content=None):
    line = {
        "type": "assistant", "timestamp": iso(ts), "requestId": req, "uuid": "u-%s-%s" % (mid, out),
        "isSidechain": sidechain, "sessionId": SID,
        "message": {
            "id": mid, "model": model, "role": "assistant", "type": "message",
            "content": content or [{"type": "text", "text": "x"}],
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


def user(ts, text="hi", agent_id=None, prompt_id=None, content=None):
    d = {"parentUuid": None, "isSidechain": bool(agent_id)}
    if prompt_id:
        d["promptId"] = prompt_id          # Claude Code writes it as the 3rd key
    d.update({"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": content or text},
              "uuid": "uu%s" % ts})
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
        # never read the real Codex / pi session folders (tally sessions discovers them)
        os.environ["CODEX_HOME"] = str(self.tmp / "codex")
        os.environ["PI_CODING_AGENT_DIR"] = str(self.tmp / "pi-agent")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")      # never the real settings.json

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
        self.assertIn("First time in project proj", e.hook("SessionStart", source="startup")["systemMessage"])

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
        # union of the 0.3 s turn_duration and the real prompt→Stop time the hooks measured
        self.assertGreaterEqual(rep["time"]["active_s"], 0.29)
        self.assertLessEqual(rep["time"]["active_s"], 5.0)      # bounded by the test's own runtime
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
            page = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5).read().decode()
            self.assertIn("<title>Tally</title>", page)
            self.assertIn('<link rel="icon" href="data:image/svg+xml,', page)
            self.assertIn('<svg class="logo"', page)
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


class TodosAndStats(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_quick_syntax_and_due_dates(self):
        from datetime import datetime
        from tally import todos
        now = datetime(2026, 10, 12, 10, 0).timestamp()          # a Monday
        eod = lambda y, m, d: datetime(y, m, d, 23, 59, 59).timestamp()
        self.assertEqual(todos.parse_due("dnes", now), eod(2026, 10, 12))
        self.assertEqual(todos.parse_due("zajtra", now), eod(2026, 10, 13))
        self.assertEqual(todos.parse_due("pi", now), eod(2026, 10, 16))
        self.assertEqual(todos.parse_due("fri", now), eod(2026, 10, 16))
        self.assertEqual(todos.parse_due("po", now), eod(2026, 10, 19))      # next Monday, not today
        self.assertEqual(todos.parse_due("+3d", now), eod(2026, 10, 15))
        self.assertEqual(todos.parse_due("20.10.", now), eod(2026, 10, 20))
        self.assertEqual(todos.parse_due("5.1.", now), eod(2027, 1, 5))      # past date -> next year
        self.assertEqual(todos.parse_due("2026-11-02", now), eod(2026, 11, 2))
        with self.assertRaises(ValueError):
            todos.parse_due("someday", now)
        q = todos.parse_quick("Fix login @web !! due:2026-11-02 -- check Safari", "proj")
        self.assertEqual((q["title"], q["project"], q["priority"], q["notes"]),
                         ("Fix login", "web", 2, "check Safari"))
        self.assertEqual(todos.parse_quick("Plain thing", "proj")["project"], "proj")

    def test_todo_take_done_flow(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        r = e.hook("UserPromptSubmit", prompt="/tally:todo Fix the export !!! due:+2d")
        self.assertIn("Todo #1 added to proj: Fix the export", r["reason"])          # project = cwd basename
        r = e.hook("UserPromptSubmit", prompt="/tally:todos")
        self.assertIn("#1 Fix the export !!!", r["reason"])
        r = e.hook("UserPromptSubmit", prompt="/tally:take 1")
        self.assertIn("Todo #1 taken", r["reason"])
        from tally import db, tasks, todos
        conn = db.connect()
        task = tasks.active_for_session(conn, SID)
        self.assertEqual((task["todo_id"], task["name"], task["project"]), (1, "Fix the export", "proj"))
        self.assertEqual(todos.get(conn, 1)["status"], "doing")
        time.sleep(0.01)
        e.append(e.main(), assistant(time.time(), "m1", "r1", out=70))
        e.hook("Stop")
        r = e.hook("UserPromptSubmit", prompt="/tally:done shipped it")
        self.assertIn("✓ Todo #1 done", r["reason"])
        self.assertIn("Task #1 ended", r["reason"])
        self.assertEqual(todos.get(conn, 1)["status"], "done")
        self.assertEqual(tasks.get(conn, 1)["status"], "ended")
        self.assertEqual(todos.linked_stats(conn, 1)["output"], 70)
        # ending a todo's task without /tally:done puts the todo back to "todo"
        e.hook("UserPromptSubmit", prompt="/tally:todo Second")
        e.hook("UserPromptSubmit", prompt="/tally:take 2")
        e.hook("UserPromptSubmit", prompt="/tally:end")
        self.assertEqual(todos.get(conn, 2)["status"], "todo")

    def test_do_hands_the_todo_to_claude(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:todo Write the release notes -- mention the new dashboard")
        r = e.hook("UserPromptSubmit", prompt="/tally:do 1", prompt_id="p-do")
        ctx = r["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(r["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("Tally todo #1", ctx)
        self.assertIn("mention the new dashboard", ctx)
        from tally import db, tasks
        conn = db.connect()
        task = tasks.active_for_session(conn, SID)
        self.assertEqual(task["todo_id"], 1)
        self.assertEqual(conn.execute("SELECT prompt_id FROM prompts WHERE task_id = ?", (task["id"],)).fetchone()[0],
                         "p-do")

    def test_session_start_reminder(self):
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:todo Call the client due:dnes")
        r = e.hook("SessionStart", source="startup")
        self.assertIn("First time in project proj", r["systemMessage"])
        self.assertIn("📋 proj: 1 open todo", r["systemMessage"])
        self.assertIn("Call the client", r["systemMessage"])
        self.assertIsNone(e.hook("SessionStart", source="clear"))
        from tally import settings
        settings.set_value("todos.reminder", "off")
        r = e.hook("SessionStart", source="startup")
        self.assertNotIn("Call the client", r["systemMessage"])      # only the project setup question is left
        e.hook("UserPromptSubmit", prompt="/tally:project tasks")
        self.assertIsNone(e.hook("SessionStart", source="startup"))

    def test_per_prompt_breakdown_is_exact(self):
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start stats")
        e.hook("UserPromptSubmit", prompt="first prompt", prompt_id="P1")
        t = time.time() + 0.01
        main, sub = e.main(), e.sdir() / "subagents"
        e.append(main, user(t, "first prompt", prompt_id="P1"),
                 assistant(t + 0.01, "a1", "ra1", out=10),
                 user(t + 0.02, content=[{"type": "tool_result", "tool_use_id": "x", "content": "ok"}], prompt_id="P1"),
                 assistant(t + 0.03, "a2", "ra2", out=20))
        e.meta(sub / "agent-s1.meta.json", {"agentType": "Explore", "description": "look"})
        e.append(sub / "agent-s1.jsonl", user(t + 0.02, "go", agent_id="s1", prompt_id="P1"),
                 assistant(t + 0.04, "s1m", "rs1", out=5, agent_id="s1", sidechain=True))
        e.hook("Stop")
        e.hook("UserPromptSubmit", prompt="second prompt", prompt_id="P2")
        e.append(main, user(t + 0.1, "second prompt", prompt_id="P2"), assistant(t + 0.11, "b1", "rb1", out=300),
                 user(t + 0.2, "<task-notification>", prompt_id="AUTO"), assistant(t + 0.21, "c1", "rc1", out=1))
        e.hook("Stop")
        from tally import db, report, tasks
        conn = db.connect()
        rep = report.build(conn, tasks.get(conn, 1))
        rows = {p["prompt"]: p for p in rep["prompts"]}
        self.assertEqual(rows["first prompt"]["output"], 35)        # 10 + 20 + subagent 5
        self.assertEqual(rows["first prompt"]["agents"], 1)
        self.assertEqual(rows["second prompt"]["output"], 300)
        self.assertEqual(rep["stats"]["prompts"], 2)
        self.assertEqual(rep["stats"]["automatic"]["output"], 1)
        self.assertEqual(rep["stats"]["automatic_turns"], 1)
        self.assertEqual(rep["stats"]["tokens_per_prompt"], round(rep["totals"]["total"] / 2))

    def test_tool_calls_at_depth_calls(self):
        e = self.env
        from tally import db, report, settings, tasks
        settings.set_value("log.depth", "calls")
        e.append(e.main(), user(time.time() - 10))
        e.hook("UserPromptSubmit", prompt="/tally:start tools")
        t = time.time() + 0.01
        e.append(e.main(),
                 user(t, "do it", prompt_id="P1"),
                 assistant(t + 0.01, "m1", "r1", content=[{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}},
                                                          {"type": "tool_use", "id": "tu2", "name": "Read", "input": {}}]),
                 user(t + 0.02, prompt_id="P1", content=[{"type": "tool_result", "tool_use_id": "tu1", "is_error": True,
                                                         "content": "boom"}]),
                 assistant(t + 0.03, "m2", "r2", content=[{"type": "tool_use", "id": "tu3", "name": "Bash", "input": {}}]))
        e.hook("Stop")
        rep = report.build(db.connect(), tasks.get(db.connect(), 1))
        tools = {x["tool"]: x for x in rep["stats"]["tools"]}
        self.assertEqual((tools["Bash"]["calls"], tools["Bash"]["errors"]), (2, 1))
        self.assertEqual(tools["Read"]["calls"], 1)
        self.assertEqual((rep["stats"]["tool_calls"], rep["stats"]["tool_errors"]), (3, 1))
        settings.set_value("log.depth", "summary")
        rep = report.build(db.connect(), tasks.get(db.connect(), 1))
        self.assertIsNone(rep["prompts"])
        self.assertIsNone(rep["stats"]["tools"])

    def test_prompt_storage_modes(self):
        e = self.env
        from tally import db, settings
        e.append(e.main(), user(time.time() - 10))
        settings.set_value("log.prompts", "preview")
        e.hook("UserPromptSubmit", prompt="/tally:start privacy")
        e.hook("UserPromptSubmit", prompt="x" * 1000)
        settings.set_value("log.prompts", "off")
        e.hook("UserPromptSubmit", prompt="secret prompt")
        rows = db.connect().execute("SELECT prompt FROM prompts ORDER BY id").fetchall()
        self.assertEqual(len(rows[0]["prompt"]), 300)
        self.assertIsNone(rows[1]["prompt"])                       # counted, not stored
        with self.assertRaises(ValueError):
            settings.set_value("log.prompts", "everything")

    def test_plan_import_progress_and_next(self):
        e = self.env
        plan = e.tmp / "plan.md"
        plan.write_text("""# Export plan

### Task 1: CSV writer
- [x] **Step 1: failing test**
- [ ] **Step 2: implement**

### Task 2: CLI flag
**Step 1: add --format**

```
- [ ] not a step (code)
```
## Notes
- [ ] Update README
""")
        from tally import db, todos
        conn = db.connect()
        r = todos.import_plan(conn, str(plan), project="web")
        self.assertEqual((r["created"], r["progress_done"], r["progress_total"]), (7, 1, 4))
        r = todos.import_plan(conn, str(plan), project="web")
        self.assertEqual((r["created"], r["updated"]), (0, 0))      # idempotent
        nxt = todos.next_todo(conn, "web")
        self.assertEqual(nxt["title"], "implement")                 # plan order, not position per parent
        todos.set_status(conn, nxt["id"], "done")
        task1 = conn.execute("SELECT * FROM todos WHERE title = 'CSV writer'").fetchone()
        self.assertEqual(task1["status"], "done")                   # all steps done -> task done
        for title in ("add --format", "Update README"):
            todos.set_status(conn, conn.execute("SELECT id FROM todos WHERE title = ?", (title,)).fetchone()[0], "done")
        root = todos.get(conn, r["plan_id"])
        self.assertEqual(root["status"], "done")                    # -> plan done
        todos.set_status(conn, conn.execute("SELECT id FROM todos WHERE title = 'Update README'").fetchone()[0], "todo")
        self.assertEqual(todos.get(conn, r["plan_id"])["status"], "todo")   # reopening reopens the plan

    def test_dashboard_writes_need_same_origin(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def post(path, body, headers):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers=dict({"Content-Type": "application/json"}, **headers))
            return urllib.request.urlopen(req, timeout=5)
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:      # a plain cross-site form/fetch
                post("/api/todos", {"text": "evil"}, {})
            self.assertEqual(cm.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as cm:      # foreign Origin
                post("/api/todos", {"text": "evil"}, {"X-Tally": "1", "Origin": "https://evil.example"})
            self.assertEqual(cm.exception.code, 403)
            r = post("/api/todos", {"text": "From the dashboard @web !"}, {"X-Tally": "1",
                                                                          "Origin": "http://127.0.0.1:%d" % port})
            todo = json.loads(r.read())
            self.assertEqual((todo["title"], todo["project"], todo["priority"]), ("From the dashboard", "web", 1))
            r = post("/api/todos/%d" % todo["id"], {"status": "blocked"}, {"X-Tally": "1"})
            self.assertEqual(json.loads(r.read())["status"], "blocked")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post("/api/settings", {"key": "log.depth", "value": "everything"}, {"X-Tally": "1"})
            self.assertEqual(cm.exception.code, 400)
            board = json.loads(urllib.request.urlopen(base + "/api/todos?project=web", timeout=5).read())
            self.assertEqual(board["todos"][0]["status"], "blocked")
        finally:
            httpd.shutdown()

    def test_dashboard_task_actions(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import db, server, tasks
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:start First")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def post(path, body, headers=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers=dict({"Content-Type": "application/json"},
                                                      **({"X-Tally": "1"} if headers is None else headers)))
            return json.loads(urllib.request.urlopen(req, timeout=5).read())

        def fails(path, body, code, headers=None):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post(path, body, headers)
            self.assertEqual(cm.exception.code, code)
            cm.exception.close()
        try:
            fails("/api/tasks/1", {"action": "delete"}, 403, {})                    # cross-site: refused
            self.assertEqual(post("/api/tasks/1", {"action": "rename", "name": "  Renamed  "})["name"], "Renamed")
            fails("/api/tasks/1", {"action": "rename", "name": " "}, 400)
            fails("/api/tasks/1", {"action": "explode"}, 400)
            fails("/api/tasks/99", {"action": "end"}, 404)
            self.assertEqual(post("/api/tasks/1", {"action": "end"})["status"], "ended")
            fails("/api/tasks/1", {"action": "end"}, 400)                           # already ended
            self.assertEqual(post("/api/tasks/1", {"action": "delete"}), {"deleted": 1})
            self.assertIsNone(tasks.get(db.connect(), 1))
            health = json.loads(urllib.request.urlopen(base + "/api/health", timeout=5).read())
            self.assertEqual(health["pid"], os.getpid())
        finally:
            httpd.shutdown()
            httpd.server_close()


class DashboardProcess(unittest.TestCase):
    """A dashboard left running by another Tally version is replaced, the current one is kept."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    # run as ".../bin/tally dashboard --port N", like a dashboard the installer started
    FAKE = (
        "import json, os, sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port, version, with_pid = int(sys.argv[3]), sys.argv[4], sys.argv[5] == 'pid'\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "    def do_GET(self):\n"
        "        d = {'app': 'tally', 'version': version}\n"
        "        if with_pid: d['pid'] = os.getpid()\n"
        "        b = json.dumps(d).encode()\n"
        "        self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers()\n"
        "        self.wfile.write(b)\n"
        "srv = HTTPServer(('127.0.0.1', port), H)\n"
        "print('ready', flush=True)\n"
        "srv.serve_forever()\n")

    def fake_dashboard(self, version, with_pid=True):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        script = Path(tempfile.mkdtemp(prefix="tc-fake-")) / "bin" / "tally"
        script.parent.mkdir()
        script.write_text(self.FAKE)
        self.addCleanup(shutil.rmtree, script.parent.parent, True)
        proc = subprocess.Popen([sys.executable, str(script), "dashboard", "--port", str(port), version,
                                 "pid" if with_pid else "nopid"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "ready")           # listening: no timing guesswork
        return proc, port

    def test_outdated_dashboard_is_stopped(self):
        from tally import __version__, server
        old, port = self.fake_dashboard("0.0.1")
        self.assertEqual(server.health("127.0.0.1", port)["version"], "0.0.1")
        self.assertTrue(server.stop_outdated("127.0.0.1", port))
        self.assertIsNotNone(old.wait(timeout=5))
        self.assertFalse(server.is_running("127.0.0.1", port))
        current, port = self.fake_dashboard(__version__)
        self.assertFalse(server.stop_outdated("127.0.0.1", port))
        self.assertIsNone(current.poll())                                         # still running

    @staticmethod
    def free_port():
        import socket
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            return sk.getsockname()[1]

    def test_stop_command_and_slash(self):
        from tally import server, settings
        settings.set_value("dashboard_port", self.free_port())     # never the real dashboard on 8765
        old, port = self.fake_dashboard("0.7.2")
        tally = [sys.executable, str(PLUGIN / "bin" / "tally")]
        r = subprocess.run(tally + ["dashboard", "--stop", "--port", str(port)], capture_output=True, text=True,
                           env=os.environ, timeout=30)
        self.assertIn("■ Dashboard stopped", r.stdout)
        self.assertIsNotNone(old.wait(timeout=5))
        r = subprocess.run(tally + ["dashboard", "--stop", "--port", str(port)], capture_output=True, text=True,
                           env=os.environ, timeout=30)
        self.assertIn("No dashboard running", r.stdout)
        reply = self.env.hook("UserPromptSubmit", prompt="/tally:dashboard stop")["reason"]
        self.assertIn("No dashboard is running", reply)
        self.assertFalse(server.is_running("127.0.0.1", port))

    def test_idle_stop_heartbeat_and_shutdown_api(self):
        import threading
        import urllib.request
        from tally import server
        port = self.free_port()
        t = threading.Thread(target=server.serve, args=("127.0.0.1", port), kwargs={"idle_minutes": 0.02}, daemon=True)
        t.start()                                                    # idle limit 1.2 s
        for _ in range(50):
            if server.is_running("127.0.0.1", port):
                break
            time.sleep(0.05)
        for _ in range(8):                                            # an open page: 2.4 s of pings
            self.assertTrue(server.is_running("127.0.0.1", port))
            time.sleep(0.3)
        t.join(timeout=10)                                            # nobody left: it stops by itself
        self.assertFalse(t.is_alive())
        self.assertFalse(server.is_running("127.0.0.1", port))
        port = self.free_port()
        t = threading.Thread(target=server.serve, args=("127.0.0.1", port), daemon=True)
        t.start()
        for _ in range(50):
            if server.is_running("127.0.0.1", port):
                break
            time.sleep(0.05)
        base = "http://127.0.0.1:%d/api/shutdown" % port
        req = urllib.request.Request(base, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:          # a foreign page cannot stop it
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)
        cm.exception.close()
        req = urllib.request.Request(base, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-Tally": "1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read()), {"stopping": True})
        t.join(timeout=10)
        self.assertFalse(t.is_alive())

    def test_outdated_dashboard_without_pid_is_found_by_its_command_line(self):
        from tally import server
        old, port = self.fake_dashboard("0.5.0", with_pid=False)                 # before 0.6: no pid in health
        self.assertNotIn("pid", server.health("127.0.0.1", port))
        self.assertEqual(server._dashboard_pids(port), [old.pid])
        from tally import settings
        settings.set_value("dashboard_port", port)                                   # `tally dashboard` without --port
        procs = [(4242, ["/usr/bin/python3", "/x/bin/tally", "dashboard", "--open"]), (4243, ["vim", "tally.py"]),
                 (4244, ["/bin/zsh", "-c", "tally dashboard --background; python3 -m unittest"]),
                 (4245, ["/Library/Frameworks/Python.framework/Resources/Python.app/Contents/MacOS/Python",
                         "/Users/me/.local/bin/tally", "dashboard"])]
        with unittest.mock.patch.object(server, "_processes", return_value=procs):
            self.assertEqual(server._dashboard_pids(port), [4242, 4245])         # never the shell (4244)
        if not os.path.isdir("/proc/self"):                                       # macOS: ps, split on spaces
            with unittest.mock.patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="  4242 /usr/bin/python3 /x/bin/tally dashboard --open\n"
                                  " 4244 /bin/zsh -c tally dashboard --background; python3 -m unittest\n")):
                self.assertEqual(server._dashboard_pids(port), [4242])
        self.assertTrue(server.stop_outdated("127.0.0.1", port))
        self.assertIsNotNone(old.wait(timeout=5))


class Projects(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_setup_question_and_modes(self):
        e = self.env
        r = e.hook("SessionStart", source="startup")
        self.assertIn("/tally:project track", r["systemMessage"])
        r = e.hook("UserPromptSubmit", prompt="/tally:project track")
        self.assertIn("✓ proj: track", r["reason"])
        self.assertIsNone(e.hook("SessionStart", source="startup"))     # decided: not asked again
        e.hook("UserPromptSubmit", prompt="/tally:project off")
        e.hook("UserPromptSubmit", prompt="/tally:todo something")
        self.assertIsNone(e.hook("SessionStart", source="startup"))     # off: no reminder either

    def test_default_mode_for_new_projects(self):
        from tally import db, projects, settings
        e = self.env
        settings.set_value("projects.default_mode", "track")
        r = e.hook("SessionStart", source="startup")
        self.assertIn("▍Tally · 👋 New project proj: track", r["systemMessage"])
        self.assertNotIn("Track it?", r["systemMessage"])
        self.assertEqual(projects.mode_of(db.connect(), "proj"), "track")
        self.assertIsNone(e.hook("SessionStart", source="startup"))     # told once
        with self.assertRaises(ValueError):
            settings.set_value("projects.default_mode", "off")          # off would hide new projects silently

    def test_track_mode_counts_every_session(self):
        e = self.env
        e.hook("SessionStart", source="startup")
        e.hook("UserPromptSubmit", prompt="/tally:project track")
        t = time.time() - 1                                               # clearly before the task below
        e.append(e.main(), user(t, "no task here", prompt_id="P0"), assistant(t + 0.01, "free", "rf", out=40))
        e.hook("Stop")                                                    # no task, still ingested
        e.hook("UserPromptSubmit", prompt="/tally:start with task")
        time.sleep(0.01)
        e.append(e.main(), assistant(time.time(), "in_task", "rt", out=60))
        e.hook("Stop")
        from tally import db, projects
        st = projects.stats(db.connect(), "proj")
        self.assertEqual(st["mode"], "track")
        self.assertEqual(st["totals"]["output"], 100)                    # whole project
        self.assertEqual(st["sessions"], 1)
        self.assertEqual(st["tasks"]["count"], 1)
        self.assertAlmostEqual(st["tasks"]["share"], 1270 / 2520.0, places=3)   # the task call / all calls
        self.assertEqual(sum(d["output"] for d in st["daily"]), 100)
        r = e.hook("UserPromptSubmit", prompt="/tally:project")
        self.assertIn("Project proj", r["reason"])
        r = e.hook("UserPromptSubmit", prompt="/tally:projects")
        self.assertIn("proj", r["reason"])

    def test_tasks_mode_counts_only_tasks(self):
        e = self.env
        e.hook("SessionStart", source="startup")
        e.hook("UserPromptSubmit", prompt="/tally:project tasks")
        t = time.time()
        e.append(e.main(), assistant(t, "free", "rf", out=40))
        e.hook("Stop")                                                    # not tracked, no task: skipped
        e.hook("UserPromptSubmit", prompt="/tally:start counted")
        time.sleep(0.01)
        e.append(e.main(), assistant(time.time(), "in_task", "rt", out=60))
        e.hook("Stop")
        from tally import db, projects
        st = projects.stats(db.connect(), "proj")
        self.assertEqual(st["totals"]["output"], 60)
        self.assertIsNone(st["tasks"]["share"])


def codex_line(ts, typ, payload, ordinal=None):
    d = {"timestamp": iso(ts), "type": typ, "payload": payload}
    if ordinal is not None:
        d["ordinal"] = ordinal
    return d


def codex_tokens(ts, inp, cached, out, ordinal=None):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
             "reasoning_output_tokens": out // 3, "total_tokens": inp + out}
    return codex_line(ts, "event_msg", {"type": "token_count", "info": {
        "total_token_usage": usage, "last_token_usage": usage, "model_context_window": 272000}}, ordinal)


class OtherAgents(unittest.TestCase):
    """Codex rollouts and pi sessions through the same hooks, tasks and reports."""

    def setUp(self):
        self.env = Env()
        self.codex_home = self.env.tmp / "codex"
        os.environ["CODEX_HOME"] = str(self.codex_home)

    def tearDown(self):
        os.environ.pop("CODEX_HOME", None)
        self.env.cleanup()

    def rollout(self, thread, day=None, meta=None):
        day = day or time.strftime("%Y/%m/%d")
        folder = self.codex_home / "sessions" / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("rollout-2026-10-12T08-00-00-%s.jsonl" % thread)
        m = {"session_id": thread, "id": thread, "cwd": "/Users/me/proj", "source": "cli"}
        m.update(meta or {})
        self.env.append(path, codex_line(time.time() - 100, "session_meta", m))
        return path

    def codex_hook(self, event, path, thread="T1", **fields):
        from tally import hooks
        payload = {"session_id": thread, "transcript_path": str(path), "cwd": "/Users/me/proj",
                   "hook_event_name": event, "model": "gpt-6.1-sol"}
        payload.update(fields)
        out = io.StringIO()
        hooks.main(event, stdin=io.StringIO(json.dumps(payload)), stdout=out, agent="codex")
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def test_codex_task_with_cumulative_tokens_and_subagent(self):
        e = self.env
        main = self.rollout("T1")
        t0 = time.time() - 50
        # history before the task: 5000 input / 300 output already used
        e.append(main, codex_line(t0, "turn_context", {"turn_id": "old", "model": "gpt-6.1-sol"}),
                 codex_tokens(t0 + 1, 5000, 4000, 300))
        self.codex_hook("SessionStart", main, source="startup")
        r = self.codex_hook("UserPromptSubmit", main, prompt="tally:start codex task", turn_id="x")
        self.assertEqual(r["decision"], "block")
        self.assertIn("Task #1 started", r["reason"])
        self.assertNotIn("/tally:", r["reason"])                      # Codex syntax: tally:<cmd>
        self.codex_hook("UserPromptSubmit", main, prompt="refactor the parser", turn_id="turn-1")
        t = time.time() + 0.01
        e.append(main,
                 codex_line(t, "event_msg", {"type": "turn_started", "turn_id": "turn-1"}),
                 codex_line(t, "turn_context", {"turn_id": "turn-1", "model": "gpt-6.1-sol"}),
                 codex_tokens(t + 0.01, 7000, 5000, 400),            # +2000 in (1000 cached), +100 out
                 codex_tokens(t + 0.02, 7000, 5000, 400),            # repeated total: ignored
                 codex_line(t + 0.03, "response_item", {"type": "function_call", "name": "shell", "call_id": "c1"}),
                 codex_tokens(t + 0.04, 9000, 6500, 650))            # +2000 in (1500 cached), +250 out
        # a subagent thread of this session, in its own rollout
        sub = self.rollout("S1", meta={"session_id": "T1", "id": "S1", "parent_thread_id": "T1",
                                       "agent_role": "explorer", "agent_nickname": "scout",
                                       "source": {"subagent": {"thread_spawn": {"parent_thread_id": "T1", "depth": 1}}}})
        e.append(sub, codex_line(t + 0.05, "turn_context", {"turn_id": "turn-1", "model": "gpt-6-luna"}),
                 codex_tokens(t + 0.06, 1000, 0, 50))
        self.codex_hook("Stop", main, turn_id="turn-1")
        from tally import db, report, tasks
        conn = db.connect()
        rep = report.build(conn, tasks.get(conn, 1))
        tot = rep["totals"]
        self.assertEqual(tot["output"], 100 + 250 + 50)               # history before the task excluded
        self.assertEqual(tot["cache_read"], 1000 + 1500)
        self.assertEqual(tot["input"], 1000 + 500 + 1000)             # input excludes cached tokens
        self.assertEqual(tot["calls"], 3)
        prompts = {p["prompt"]: p for p in rep["prompts"]}
        self.assertEqual(prompts["refactor the parser"]["output"], 400)  # main 350 + subagent 50
        kids = {c["label"]: c for c in rep["tree"][0]["children"]}
        self.assertEqual(kids["scout"]["agent_type"], "explorer")
        self.assertEqual(kids["scout"]["rollup"]["output"], 50)
        models = {m["model"] for m in rep["by_model"]}
        self.assertEqual(models, {"gpt-6.1-sol", "gpt-6-luna"})
        from tally.pricing import Pricer
        self.assertAlmostEqual(tot["cost"], Pricer().cost("gpt-6.1-sol", 1500, 350, 2500)
                               + Pricer().cost("gpt-6-luna", 1000, 50, 0), places=8)

    def test_codex_forked_history_is_skipped(self):
        e = self.env
        main = self.rollout("T2")
        self.codex_hook("SessionStart", main, thread="T2", source="startup")
        self.codex_hook("UserPromptSubmit", main, thread="T2", prompt="tally:start fork test")
        t = time.time() + 0.01
        fork = self.rollout("F1", meta={"session_id": "T2", "id": "F1", "forked_from_id": "T2",
                                        "subagent_history_start_ordinal": 3})
        e.append(fork, codex_tokens(t, 8000, 6000, 900, ordinal=1),       # inherited from the parent
                 codex_tokens(t + 0.01, 8000, 6000, 900, ordinal=2),
                 codex_tokens(t + 0.02, 8500, 6000, 950, ordinal=3))      # the fork's own work: +500 / +50
        self.codex_hook("Stop", main, thread="T2")
        from tally import db, report, tasks
        rep = report.build(db.connect(), tasks.get(db.connect(), 1))
        self.assertEqual(rep["totals"]["output"], 50)
        self.assertEqual(rep["totals"]["input"], 500)

    def test_pi_session(self):
        e = self.env
        sess = e.tmp / "pi" / "2026-10-12T08-00-00-000Z_P1.jsonl"
        e.append(sess, {"type": "session", "version": 3, "id": "P1", "timestamp": iso(time.time() - 60),
                        "cwd": "/Users/me/proj"})

        def pi_hook(event, **fields):
            from tally import hooks
            payload = dict({"session_id": "P1", "transcript_path": str(sess), "cwd": "/Users/me/proj"}, **fields)
            out = io.StringIO()
            hooks.main(event, stdin=io.StringIO(json.dumps(payload)), stdout=out, agent="pi")
            return json.loads(out.getvalue()) if out.getvalue().strip() else None

        def pi_msg(eid, role, ts, **m):
            return {"type": "message", "id": eid, "parentId": None, "timestamp": iso(ts),
                    "message": dict({"role": role, "timestamp": int(ts * 1000)}, **m)}

        pi_hook("SessionStart", source="startup")
        from tally import commands, db, report, tasks
        conn = db.connect()
        commands.run(conn, "P1", "start", "pi task", cwd="/Users/me/proj", transcript_path=str(sess))
        t = time.time() + 0.01
        usage = lambda i, o, c: {"input": i, "output": o, "cacheRead": 0, "cacheWrite": 0, "totalTokens": i + o,
                                 "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": c}}
        e.append(sess,
                 pi_msg("u1", "user", t, content="add a test"),
                 pi_msg("a1", "assistant", t + 0.01, provider="zai", model="glm-5.2", usage=usage(100, 20, 0.01),
                        stopReason="toolUse", content=[{"type": "toolCall", "id": "tc1", "name": "subagent",
                                                        "arguments": {}}]),
                 pi_msg("r1", "toolResult", t + 0.02, toolCallId="tc1", toolName="subagent", isError=False,
                        content=[], details={"agent": "scout"}, usage=usage(500, 50, 0.02)),
                 pi_msg("a2", "assistant", t + 0.03, provider="zai", model="glm-5.2", usage=usage(150, 30, 0.03),
                        stopReason="stop", content=[{"type": "text", "text": "done"}]),
                 pi_msg("a3", "assistant", t + 0.04, provider="zai", model="glm-5.2", usage=usage(0, 0, 0),
                        stopReason="error", content=[]))
        pi_hook("Stop")
        rep = report.build(conn, tasks.get(conn, 1))
        self.assertEqual(rep["totals"]["output"], 100)
        self.assertEqual(rep["totals"]["calls"], 3)                    # the failed empty call is not counted
        self.assertAlmostEqual(rep["totals"]["cost"], 0.06)            # cost reported by pi
        self.assertEqual(rep["prompts"][0]["prompt"], "add a test")    # prompt read from the transcript
        self.assertEqual(rep["prompts"][0]["agents"], 1)
        sub = rep["tree"][0]["children"][0]
        self.assertEqual((sub["agent_type"], sub["rollup"]["output"]), ("subagent", 50))


class Installer(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.home = self.env.tmp / "fakehome"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".pi" / "agent").mkdir(parents=True)
        self.saved = {k: os.environ.get(k) for k in ("HOME", "CODEX_HOME", "PI_CODING_AGENT_DIR")}
        os.environ["HOME"] = str(self.home)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("PI_CODING_AGENT_DIR", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.env.cleanup()

    def test_codex_and_pi_install_uninstall(self):
        from tally import installer
        hooks = self.home / ".codex" / "hooks.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        self.assertEqual(installer.install(["codex", "pi"]), 0)
        data = json.loads(hooks.read_text())
        self.assertEqual(sorted(data["hooks"]), sorted(installer.CODEX_EVENTS))
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "echo mine")     # kept
        self.assertTrue(installer._is_tally(data["hooks"]["Stop"][1]))
        self.assertEqual(data["hooks"]["Stop"][1]["hooks"][0]["statusMessage"], "Tally")
        self.assertTrue(installer.installed()["codex"])
        installer.install(["codex"])                                                       # idempotent
        self.assertEqual(len(json.loads(hooks.read_text())["hooks"]["Stop"]), 2)
        ext = (self.home / ".pi" / "agent" / "extensions" / "tally.ts").read_text()
        self.assertIn(str(installer.BIN), ext)
        self.assertNotIn("__TALLY_BIN__", ext)
        skill = (self.home / ".agents" / "skills" / "tally-todos" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: tally-todos\n"))
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", skill)
        installer.uninstall(["codex", "pi"])
        self.assertEqual(json.loads(hooks.read_text()),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}})
        self.assertFalse((self.home / ".pi" / "agent" / "extensions" / "tally.ts").exists())
        self.assertFalse(installer.installed()["codex"])

    def test_interactive_choice(self):
        from unittest import mock
        from tally import installer
        with mock.patch.object(installer, "detect", return_value={"claude": True, "codex": False, "pi": True}), \
                mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", side_effect=["n", ""]):
            stdin.isatty.return_value = True
            self.assertEqual(installer.choose(None, assume_yes=False), ["pi"])
        with mock.patch.object(installer, "detect", return_value={"claude": True, "codex": False, "pi": True}):
            self.assertEqual(installer.choose(None, assume_yes=True), ["claude", "pi"])
        with self.assertRaises(SystemExit):
            installer.choose(["vim"], assume_yes=True)


class SessionsOverview(unittest.TestCase):
    """Whole sessions of every agent, indexed into sessions.db (sessions.scope all:
    every session is read; SessionsScope covers the default)."""

    def setUp(self):
        self.env = Env()
        self.codex = Path(os.environ["CODEX_HOME"])
        self.pi = Path(os.environ["PI_CODING_AGENT_DIR"])
        from tally import settings
        settings.set_value("sessions.scope", "all")

    def tearDown(self):
        self.env.cleanup()

    def make_sessions(self):
        e = self.env
        t0 = time.time() - 600
        # Claude Code: history, then a task starts (the main DB skips the history)
        e.append(e.main(), dict(user(t0, "build the parser", prompt_id="p1"), cwd="/Users/me/proj"),
                 assistant(t0 + 1, "c1", "r1", out=300),
                 user(t0 + 2, prompt_id="p1", content=[{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
                 user(t0 + 3, "<task-notification>agent done</task-notification>", prompt_id="p0"),
                 assistant(t0 + 4, "c0", "r0", out=7),
                 {"type": "ai-title", "aiTitle": "Parser work", "sessionId": SID})
        e.hook("UserPromptSubmit", prompt="/tally:start parser")
        e.append(e.main(), user(time.time(), "next", prompt_id="p2"), assistant(time.time() + 0.01, "c2", "r2", out=50))
        e.hook("Stop")
        # Codex: a root thread with one subagent thread
        day = self.codex / "sessions" / time.strftime("%Y/%m/%d")
        day.mkdir(parents=True)
        root = day / "rollout-2026-10-12T08-00-00-T1.jsonl"
        e.append(root, codex_line(t0, "session_meta", {"session_id": "T1", "id": "T1", "cwd": "/Users/me/proj",
                                                       "source": "cli"}),
                 codex_line(t0, "event_msg", {"type": "task_started", "turn_id": "k1"}),
                 codex_line(t0, "event_msg", {"type": "user_message", "message": "fix the flaky test"}),
                 codex_line(t0, "turn_context", {"turn_id": "k1", "model": "gpt-6.1-sol"}),
                 codex_tokens(t0 + 1, 2000, 1000, 200))
        sub = day / "rollout-2026-10-12T08-00-01-S1.jsonl"
        e.append(sub, codex_line(t0, "session_meta", {"session_id": "T1", "id": "S1", "parent_thread_id": "T1",
                                                      "cwd": "/Users/me/proj"}),
                 codex_line(t0, "turn_context", {"turn_id": "k1", "model": "gpt-6-luna"}),
                 codex_tokens(t0 + 2, 500, 0, 40))
        # pi: a session with the cost pi reported
        pis = self.pi / "sessions" / "--Users-me-other--" / "2026-10-12T08-00-00-000Z_P1.jsonl"
        usage = {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 120,
                 "cost": {"total": 0.25}}
        e.append(pis, {"type": "session", "version": 3, "id": "P1", "timestamp": iso(t0), "cwd": "/Users/me/other"},
                 {"type": "message", "id": "u1", "timestamp": iso(t0 + 1),
                  "message": {"role": "user", "content": [{"type": "text", "text": "write docs"}]}},
                 {"type": "message", "id": "a1", "timestamp": iso(t0 + 2),
                  "message": {"role": "assistant", "provider": "zai", "model": "glm-5.2", "usage": usage,
                              "stopReason": "stop", "content": []}})
        return root, sub, pis

    def test_overview_covers_whole_sessions_of_every_agent(self):
        self.make_sessions()
        from tally import sessions
        r = sessions.refresh(None)
        self.assertEqual(r["changed"], 3)                       # Claude, Codex root, pi (not the subagent)
        ov = sessions.overview(None)
        rows = {s["agent"]: s for s in ov["sessions"]}
        self.assertEqual(set(rows), {"claude", "codex", "pi"})
        c = rows["claude"]
        self.assertEqual(c["totals"]["output"], 357)            # history before the task included
        self.assertEqual((c["title"], c["project"], c["prompts"]), ("Parser work", "proj", 2))
        self.assertEqual(c["automatic_turns"], 1)               # the background-agent notification
        self.assertEqual([t["name"] for t in c["tasks"]], ["parser"])
        x = rows["codex"]
        self.assertEqual(x["session_id"], "T1")
        self.assertEqual(x["totals"]["output"], 240)            # the subagent thread counts with its root
        self.assertEqual((x["title"], x["agents"]), ("fix the flaky test", 1))
        self.assertEqual({m["model"] for m in x["models"]}, {"gpt-6.1-sol", "gpt-6-luna"})
        p = rows["pi"]
        self.assertAlmostEqual(p["totals"]["cost"], 0.25)       # cost reported by pi
        self.assertEqual((p["title"], p["project"]), ("write docs", "other"))
        self.assertEqual(ov["totals"]["sessions"], 3)
        self.assertEqual(ov["totals"]["output"], 357 + 240 + 20)
        self.assertEqual(ov["totals"]["prompts"], 4)
        rep = sessions.detail(SID)
        self.assertEqual([p["prompt"] for p in rep["prompts"]], ["build the parser", "next"])
        self.assertEqual(rep["prompts"][0]["output"], 300)
        self.assertEqual(rep["stats"]["automatic"]["output"], 7)
        self.assertEqual(len(ov["by_agent"]), 3)
        self.assertEqual(sessions.overview(None, agent="pi")["count"], 1)
        self.assertEqual(sessions.overview(None, project="proj")["count"], 2)
        rep = sessions.detail("T1")
        self.assertEqual(rep["totals"]["output"], 240)
        self.assertEqual(rep["session"]["agent"], "codex")

    def test_refresh_is_incremental_and_skips_off_projects(self):
        root, sub, pis = self.make_sessions()
        from tally import db, projects, sessions
        sessions.refresh(None)
        old = time.time() - 2 * sessions.STABLE_S
        for f in (self.env.main(), root, sub, pis):
            os.utime(f, (old, old))
        self.assertEqual(sessions.refresh(None)["changed"], 3)    # mtime changed once
        self.assertEqual(sessions.refresh(None)["changed"], 0)    # quiet and unchanged: not read again
        self.env.append(pis, {"type": "message", "id": "a2", "timestamp": iso(time.time()),
                              "message": {"role": "assistant", "model": "glm-5.2", "stopReason": "stop",
                                          "usage": {"input": 1, "output": 5, "totalTokens": 6}, "content": []}})
        self.assertEqual(sessions.refresh(None)["changed"], 1)
        rows = {s["agent"]: s for s in sessions.overview(None)["sessions"]}
        self.assertEqual(rows["pi"]["totals"]["output"], 25)
        conn = db.connect()
        with db.transaction(conn):
            projects.set_mode(conn, "proj", "off")
        self.assertEqual({s["agent"] for s in sessions.overview(None)["sessions"]}, {"pi"})

    def test_cli_and_dashboard(self):
        self.make_sessions()
        run = lambda *a: subprocess.run([sys.executable, str(PLUGIN / "bin" / "tally")] + list(a),  # noqa: E731
                                        capture_output=True, text=True, env=os.environ, timeout=60, check=False)
        r = run("sessions", "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Sessions, all time: 3", r.stdout)
        self.assertIn("Parser work", r.stdout)
        data = json.loads(run("sessions", "--json", "--agent", "codex", "--no-refresh").stdout)
        self.assertEqual([s["session_id"] for s in data["sessions"]], ["T1"])
        r = run("session", "T1")                                   # Codex session by id
        self.assertIn("Session T1", r.stdout)
        self.assertIn("[Codex]", r.stdout)
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            get = lambda path: json.loads(urllib.request.urlopen(base + path, timeout=10).read())  # noqa: E731
            ov = get("/api/sessions?days=")
            self.assertEqual(ov["count"], 3)
            rep = get("/api/sessions/P1")
            self.assertEqual(rep["session"]["agent"], "pi")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/sessions/nope", timeout=10)
            cm.exception.close()
        finally:
            from tally import sessions
            deadline = time.time() + 20
            while sessions.progress()["running"] and time.time() < deadline:   # background index of this env
                time.sleep(0.05)
            httpd.shutdown()
            httpd.server_close()


    def test_projects_and_titles_come_from_the_index(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import fmt, server, sessions
        self.make_sessions()
        sessions.refresh(30)
        # a Claude Code session without an AI title is named after its first prompt
        e = self.env
        e.append(e.main(SID2), dict(user(time.time() - 50, "migrate the   billing\nto Stripe", prompt_id="q1"),
                                    cwd="/Users/me/proj"), assistant(time.time() - 40, "z1", "y1", out=10))
        sessions.refresh(30)
        titles = {s["session_id"]: s["title"] for s in sessions.overview(30)["sessions"]}
        self.assertEqual(titles[SID2], "migrate the billing to Stripe")
        self.assertEqual(titles[SID], "Parser work")                            # the agent's own title wins
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            get = lambda path: json.loads(urllib.request.urlopen(base + path, timeout=10).read())  # noqa: E731
            listed = get("/api/projects?full=1")["projects"]
            by = {p["project"]: p for p in listed}
            self.assertFalse(by["other"]["registered"])                         # seen only in the index (pi)
            self.assertEqual((by["other"]["sessions"], by["other"]["last_30d"]["total"]), (1, 120))
            self.assertAlmostEqual(by["other"]["last_30d"]["cost"], 0.25)
            self.assertTrue(by["proj"]["registered"])                           # the task registered it
            self.assertEqual(by["proj"]["sessions"], 3)                         # Claude x2 and Codex
            self.assertEqual([p["project"] for p in listed], ["proj", "other"])   # most used first
            det = get("/api/projects/other")
            self.assertEqual((det["last_30d"]["total"], det["sessions"], det["registered"]), (120, 1, False))
            self.assertEqual(det["tracked"]["total"], 0)                         # nothing kept for an untracked project
            self.assertEqual(sum(d["total"] for d in det["daily"]), 120)
            self.assertEqual(get("/api/projects/proj")["last_30d"]["total"], by["proj"]["last_30d"]["total"])
        finally:
            deadline = time.time() + 20
            while sessions.progress()["running"] and time.time() < deadline:
                time.sleep(0.05)
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(fmt.money({"cost": 9050.11}), "$9,050")
        self.assertEqual(fmt.money({"cost": 12.345}), "$12.35")

class SessionsScope(unittest.TestCase):
    """sessions.scope enabled (the default): only projects set to track or tasks,
    sessions a task counted and sessions added one by one are read; the rest is
    only described and offered in the dashboard."""

    def setUp(self):
        self.env = Env()
        self.codex = Path(os.environ["CODEX_HOME"])
        self.pi = Path(os.environ["PI_CODING_AGENT_DIR"])
        SessionsOverview.make_sessions(self)       # Claude SID (a task, project proj), Codex T1 (proj), pi P1 (other)

    def tearDown(self):
        from tally import sessions
        deadline = time.time() + 20
        while sessions.progress()["running"] and time.time() < deadline:
            time.sleep(0.05)
        self.env.cleanup()

    def read(self):
        from tally import sessions
        ix = sessions.connect()
        try:
            return {r[0] for r in ix.execute("SELECT session_id FROM indexed")}
        finally:
            ix.close()

    def set_mode(self, name, mode):
        from tally import db, projects
        conn = db.connect()
        with db.transaction(conn):
            projects.set_mode(conn, name, mode)
        conn.close()

    def test_only_enabled_projects_are_read(self):
        from tally import fmt, projects, db, sessions
        self.assertEqual(sessions.refresh(None)["changed"], 1)             # the session with a task
        self.assertEqual(self.read(), {SID})
        ov = sessions.overview(None)
        self.assertEqual([s["session_id"] for s in ov["sessions"]], [SID])
        self.assertEqual(ov["not_tracked"], {"sessions": 2, "projects": 2})
        rest = {s["session_id"]: s for s in sessions.not_tracked(None)}
        self.assertEqual(set(rest), {"T1", "P1"})
        self.assertEqual((rest["P1"]["project"], rest["P1"]["title"], rest["P1"]["agent"]), ("other", "write docs", "pi"))
        self.assertIn("+ 2 sessions in 2 projects not tracked", fmt.render_sessions(ov))
        conn = db.connect()
        by = {p["project"]: p for p in projects.listing(conn, with_usage=True)}
        conn.close()
        self.assertFalse(by["other"]["sessions_read"])
        self.assertEqual((by["other"]["sessions"], by["other"]["not_tracked"]["sessions"]), (0, 1))
        self.assertEqual((by["proj"]["sessions"], by["proj"]["not_tracked"]["sessions"]), (1, 1))
        # tracking the project reads its sessions, also files that did not change since they were described
        self.set_mode("proj", "tasks")
        self.assertEqual(sessions.refresh(None)["changed"], 2)
        self.assertEqual(self.read(), {SID, "T1"})
        self.assertEqual(sessions.overview(None)["not_tracked"], {"sessions": 1, "projects": 1})
        sessions.refresh(None)
        self.assertNotIn("P1", self.read())                                 # still not wanted: not read
        # off: nothing is listed, not even as not tracked, and a task does not bring it back
        self.set_mode("proj", "off")
        self.assertEqual(sessions.overview(None)["count"], 0)
        self.assertEqual([s["session_id"] for s in sessions.not_tracked(None)], ["P1"])

    def test_sessions_added_one_by_one(self):
        from tally import db, projects, sessions
        sessions.refresh(None)
        self.assertIsNone(sessions.add("nope"))
        self.assertEqual(sessions.add("P1")["session_id"], "P1")          # read at once
        self.assertIn("P1", self.read())
        rows = {s["session_id"]: s for s in sessions.overview(None)["sessions"]}
        self.assertEqual(set(rows), {SID, "P1"})
        self.assertTrue(rows["P1"]["added"])
        self.assertTrue(sessions.detail("P1")["session"]["added"])
        conn = db.connect()
        det = projects.detail(conn, "other")
        conn.close()
        self.assertEqual((det["last_30d"]["total"], det["sessions_read"], det["not_tracked"]["sessions"]), (120, False, 0))
        self.assertTrue(sessions.remove("P1"))
        self.assertEqual({s["session_id"] for s in sessions.overview(None)["sessions"]}, {SID})
        conn = db.connect()
        det = projects.detail(conn, "other")                                # still in the index, not counted
        conn.close()
        self.assertEqual((det["last_30d"]["total"], det["not_tracked"]["sessions"]), (0, 1))
        self.assertEqual(det["not_tracked"]["list"][0]["session_id"], "P1")

    def test_an_index_from_before_keeps_what_it_read(self):
        from tally import db, settings, sessions
        settings.set_value("sessions.scope", "all")
        sessions.refresh(None)                                             # 0.7: everything read
        ix = sessions.connect()
        with db.transaction(ix):
            ix.execute("DROP TABLE seen")
            ix.execute("DELETE FROM kv WHERE key = 'seen_version'")
        ix.close()
        settings.set_value("sessions.scope", "enabled")
        self.assertEqual({s["session_id"] for s in sessions.not_tracked(None)}, {"T1", "P1"})   # seen, from the index
        self.assertEqual(sessions.overview(None)["count"], 1)
        self.set_mode("other", "track")
        self.assertEqual({s["session_id"] for s in sessions.overview(None)["sessions"]}, {SID, "P1"})

    def test_dashboard_adds_projects_and_sessions(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import server, sessions
        sessions.refresh(None)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        get = lambda path: json.loads(urllib.request.urlopen(base + path, timeout=10).read())  # noqa: E731

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Tally": "1"})
            return json.loads(urllib.request.urlopen(req, timeout=10).read())

        def settle():
            deadline = time.time() + 20
            while sessions.progress()["running"] and time.time() < deadline:
                time.sleep(0.05)
        try:
            rest = get("/api/sessions?days=&list=not_tracked")
            self.assertEqual({s["session_id"] for s in rest["sessions"]}, {"T1", "P1"})
            settle()
            self.assertEqual(get("/api/sessions?days=")["not_tracked"]["sessions"], 2)
            self.assertTrue(post("/api/sessions/T1", {"action": "add"})["added"])
            self.assertEqual({s["session_id"] for s in get("/api/sessions?days=")["sessions"]}, {SID, "T1"})
            self.assertTrue(post("/api/sessions/T1", {"action": "remove"})["removed"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post("/api/sessions/nope", {"action": "add"})
            self.assertEqual(cm.exception.code, 404)
            cm.exception.close()
            settle()
            self.assertEqual(post("/api/projects/other", {"mode": "track"})["mode"], "track")   # reads it in the background
            settle()
            self.assertEqual({s["session_id"] for s in get("/api/sessions?days=")["sessions"]}, {SID, "P1"})
            by = {p["project"]: p for p in get("/api/projects?full=1")["projects"]}
            self.assertTrue(by["other"]["sessions_read"])
            self.assertEqual(by["other"]["last_30d"]["total"], 120)
        finally:
            settle()
            httpd.shutdown()
            httpd.server_close()


class Exports(unittest.TestCase):
    """tally export and /api/export: every scope and format, the time-sheet columns, copy."""

    def setUp(self):
        self.env = Env()
        self.codex = Path(os.environ["CODEX_HOME"])
        self.pi = Path(os.environ["PI_CODING_AGENT_DIR"])
        from tally import settings
        settings.set_value("sessions.scope", "all")
        SessionsOverview.make_sessions(self)
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:end shipped | v2")          # a pipe in the note
        from tally import sessions
        sessions.refresh(30)

    def tearDown(self):
        from tally import sessions
        deadline = time.time() + 20
        while sessions.progress()["running"] and time.time() < deadline:
            time.sleep(0.05)
        self.env.cleanup()

    def test_every_scope_and_format(self):
        import csv as csvmod
        from tally import export
        for scope, formats in export.FORMATS.items():
            ref = {"task": "1", "project": "proj", "session": "T1"}.get(scope)
            for f in formats:
                content, name = export.export(scope, f, ref=ref)
                self.assertTrue(content.strip(), (scope, f))
                self.assertTrue(name.startswith("tally-%s" % scope) and name.endswith("." + export.EXT[f]), name)
                if f == "json":
                    json.loads(content)
                if f == "csv":
                    rows = list(csvmod.reader(io.StringIO(content)))
                    self.assertGreaterEqual(len(rows), 2, (scope, rows))
                    self.assertEqual(len({len(r) for r in rows}), 1, (scope, "ragged CSV"))
                if f == "md":
                    self.assertTrue(content.lstrip().startswith("#"), (scope, content[:40]))

    def test_time_sheet_contents(self):
        import csv as csvmod
        from tally import export
        rows = list(csvmod.DictReader(io.StringIO(export.export("tasks", "csv")[0])))
        self.assertEqual(list(rows[0])[:17], export.TASK_COLUMNS[:17])           # the columns before 0.7 stay first
        self.assertEqual(rows[0]["project"], "proj")
        self.assertRegex(rows[0]["active_hm"], r"^\d+:\d\d$")
        self.assertEqual(rows[0]["note"], "shipped | v2")
        line = export.export("task", "text", ref="1")[0]
        self.assertRegex(line, r"^parser \(#1\) · proj · \d{4}-\d\d-\d\d \d\d:\d\d.+ · active .+ · wall .+ tokens · \$")
        md = export.export("tasks", "md")[0]
        self.assertIn("shipped", export.export("task", "md", ref="1")[0])
        self.assertIn("| **Total** |", md)
        self.assertIn("|---|---|---|", md.replace("---:", "---"))           # a well-formed table
        self.assertNotIn("shipped | v2", export.export("task", "md", ref="1")[0])   # pipes escaped in cells
        summary = json.loads(export.export("summary", "json")[0])
        self.assertEqual(summary["totals"]["sessions"], 3)
        self.assertEqual({a["agent"] for a in summary["by_agent"]}, {"claude", "codex", "pi"})
        self.assertEqual(summary["tasks"]["count"], 1)
        with self.assertRaises(export.ExportError):
            export.export("task", "md", ref="99")
        with self.assertRaises(export.ExportError):
            export.export("projects", "text")
        with self.assertRaises(export.ExportError):
            export.export("project", "md", ref="nope")

    def test_cli_and_api(self):
        tally = [sys.executable, str(PLUGIN / "bin" / "tally")]
        run = lambda *a, **kw: subprocess.run(tally + list(a), capture_output=True, text=True, env=kw.get("env", os.environ),  # noqa: E731
                                              timeout=60, stdin=subprocess.DEVNULL)
        r = run("export", "task", "1", "-f", "text")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("parser (#1) · proj"), r.stdout)
        self.assertIn("id,name,status,started", run("export").stdout)            # the old default: tasks as CSV
        out = self.env.tmp / "p.md"
        r = run("export", "projects", "-f", "md", "-o", str(out))
        self.assertIn("Exported to", r.stdout)
        self.assertTrue(out.read_text().startswith("# Tally projects"))
        r = run("export", "task", "42")
        self.assertEqual(r.returncode, 1)
        self.assertIn("task 42 not found", r.stderr)
        # --copy goes through the platform's clipboard tool
        fakebin = self.env.tmp / "bin"
        fakebin.mkdir()
        clip = self.env.tmp / "clipboard.txt"
        for tool in ("pbcopy", "wl-copy", "xclip", "xsel"):
            (fakebin / tool).write_text("#!/bin/sh\ncat > %s\n" % clip)
            (fakebin / tool).chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (fakebin, os.environ.get("PATH", "")))
        r = run("export", "summary", "--copy", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Copied summary (md)", r.stdout)
        self.assertTrue(clip.read_text().startswith("# Tally summary"))
        # the dashboard serves the same content, as a download when asked
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from tally import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/api/export?" % httpd.server_address[1]
        try:
            with urllib.request.urlopen(base + "scope=task&ref=1&format=text", timeout=10) as resp:
                self.assertTrue(resp.read().decode().startswith("parser (#1)"))
                self.assertIsNone(resp.headers.get("Content-Disposition"))
            with urllib.request.urlopen(base + "scope=projects&format=csv&download=1", timeout=10) as resp:
                self.assertIn('attachment; filename="tally-projects-', resp.headers["Content-Disposition"])
                self.assertEqual(resp.headers["Content-Type"], "text/csv; charset=utf-8")
            for query, code in (("scope=task&ref=9&format=md", 404), ("scope=projects&format=text", 400),
                                ("scope=bogus", 400)):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + query, timeout=10)
                self.assertEqual(cm.exception.code, code, query)
                cm.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

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
        self.assertTrue(r.stdout.startswith("▍Tally "), r.stdout)
        self.assertIn("setup         not run yet (tally setup)", r.stdout)
        self.assertIn("status line   none (tally setup can add it)", r.stdout)


def run_in_pty(argv, answers=(), env=None, timeout=20.0):
    """Run argv in a pseudo-terminal, typing each answer after the next question; returns the output."""
    import pty
    import re
    import select
    pid, fd = pty.fork()
    if pid == 0:                                             # child
        os.environ.update(env or {})
        os.environ["TERM"] = "xterm-256color"
        os.execv(argv[0], argv)
    out, sent, idle = b"", 0, time.time()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                idle = time.time()
            plain = re.sub(r"\x1b\[[0-9;]*m", "", out.decode("utf-8", "replace"))
            if sent < len(answers) and re.search(r"(\] |Y/n |y/N )$", plain):
                os.write(fd, (answers[sent] + "\r").encode())
                sent += 1
                out += b"\n"
            elif sent >= len(answers) and time.time() - idle > 3:
                break
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        os.waitpid(pid, 0)
        os.close(fd)
    return out.decode("utf-8", "replace")


class Branding(unittest.TestCase):
    """Every message has the same head line, in every agent's syntax and in the CLI."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_every_command_output_is_branded(self):
        from tally import fmt
        e = self.env
        e.append(e.main(), user(time.time() - 10))
        ok = ["/tally:help", "/tally:list", "/tally:start Fix login", "/tally:start Second task", "/tally:status",
              "/tally:stats", "/tally:show", "/tally:rename Fix OAuth", "/tally:todo Write notes !! due:fri",
              "/tally:todos", "/tally:todos all", "/tally:take 1", "/tally:mark 1 blocked", "/tally:done 1",
              "/tally:project", "/tally:projects", "/tally:project track", "/tally:settings",
              "/tally:settings log.depth calls", "/tally:sessions", "/tally:end", "/tally:resume 1", "/tally:end 1"]
        errors = ["/tally:status 99", "/tally:resume", "/tally:new", "/tally:mark 1 nonsense", "/tally:todo",
                  "/tally:settings nope x", "/tally:settings nope", "/tally:sessions nonsense",
                  "/tally:project x bogus", "/tally:take 77", "/tally:done 77", "/tally:show 99", "/tally:rename"]
        import re
        for prompt in ok + errors:
            r = e.hook("UserPromptSubmit", prompt=prompt)
            self.assertIsNotNone(r, prompt)
            lines = r["reason"].split("\n")
            self.assertRegex(lines[0], r"^%s( \d+\.\d+\.\d+)? · " % re.escape(fmt.BRAND), prompt)
            self.assertEqual(lines[0].startswith(fmt.BRAND + " · ✗"), prompt in errors, (prompt, lines[0]))
            for line in lines[1:]:
                self.assertTrue(line == "" or line.startswith((fmt.BRAND, "  ")), (prompt, line))
        # starting a task while one runs: the ended report and the start, each with its own head line
        e.hook("UserPromptSubmit", prompt="/tally:start Third")
        r = e.hook("UserPromptSubmit", prompt="/tally:start Fourth")
        heads = [l for l in r["reason"].split("\n") if l.startswith(fmt.BRAND)]
        self.assertEqual(len(heads), 2, r["reason"])
        self.assertRegex(heads[0], r"■ Task #\d+ ended: Third$")
        self.assertRegex(heads[1], r"▶ Task #\d+ started: Fourth$")

    def test_codex_syntax_keeps_the_brand(self):
        from tally import fmt, hooks
        out = io.StringIO()
        hooks.main("UserPromptSubmit", stdin=io.StringIO(json.dumps(
            {"session_id": SID, "cwd": "/Users/me/proj", "prompt": "tally:help"})), stdout=out, agent="codex")
        reason = json.loads(out.getvalue())["reason"]
        self.assertTrue(reason.startswith(fmt.BRAND + " "), reason)
        self.assertIn("tally:start [name]", reason)
        self.assertNotIn("/tally:", reason)

    def test_terminal_colours(self):
        from unittest import mock
        from tally import fmt
        text = "\n".join([fmt.head("Task #1 started", "▶"), "  body", fmt.hints("/tally:end finish")])
        painted = fmt.paint(text)
        self.assertTrue(painted.startswith("\033[1;38;5;202m▍Tally\033[0m\033[1m · ▶ Task #1 started\033[0m"), painted)
        self.assertIn("\n  body\n", painted)
        self.assertTrue(painted.endswith("\033[2m  → /tally:end finish\033[0m"), painted)
        self.assertIn("\033[31m · ✗ nope", fmt.paint(fmt.error("nope")))
        self.assertEqual(fmt.paint("  plain"), "  plain")

        class Tty(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm"}):
            self.assertTrue(fmt.use_color(Tty()))
            self.assertFalse(fmt.use_color(io.StringIO()))              # piped: never
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}):
            self.assertFalse(fmt.use_color(Tty()))
        with mock.patch.dict(os.environ, {"NO_COLOR": "", "TERM": "dumb"}):
            self.assertFalse(fmt.use_color(Tty()))

    def test_banner(self):
        from tally import fmt
        rows = fmt.banner("9.9").split("\n")
        self.assertEqual(rows[1:7], ["  " + line for line in fmt.WORDMARK])
        self.assertEqual(rows[7], "  ▍ " + fmt.TAGLINE)
        self.assertEqual(rows[8], "  9.9")
        fmt.banner("9.9", ascii_only=True).encode("ascii")                    # no block characters at all
        painted = fmt.banner("9.9", color=True)
        for shade in set(fmt.GRADIENT):
            self.assertIn("\033[38;5;%dm" % shade, painted)
        self.assertIn("\033[2m9.9\033[0m", painted)
        self.assertTrue(fmt.can_draw(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")))
        self.assertFalse(fmt.can_draw(io.TextIOWrapper(io.BytesIO(), encoding="ascii")))

    def run_cli(self, *args, **kw):
        return subprocess.run([sys.executable, str(PLUGIN / "bin" / "tally")] + list(args), capture_output=True,
                              text=True, env=os.environ, timeout=60, stdin=subprocess.DEVNULL, **kw)

    def test_cli_overview_errors_and_plain_output_when_piped(self):
        from tally import __version__
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("|_   _|_ _| | |_   _", r.stdout)
        self.assertIn("Per-task token & time tracking", r.stdout)
        self.assertIn("  " + __version__ + "\n", r.stdout)
        self.assertIn("tally setup", r.stdout)
        self.assertNotIn("\033[", r.stdout)
        r = self.run_cli("show", "42")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.strip(), "▍Tally · ✗ Task #42 not found\n  → tally list shows your tasks")
        r = self.run_cli("setup")
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs a terminal", r.stderr)
        r = self.run_cli("settings", "log.depth", "calls")
        self.assertEqual(r.stdout.strip(), "▍Tally · ✓ log.depth = calls")
        r = self.run_cli("settings", "todos.reminder")
        self.assertIn("todos.reminder         on", r.stdout)
        self.assertIn("→ tally settings <key> <value> change one", r.stdout)

    @unittest.skipUnless(hasattr(os, "fork"), "needs a pseudo-terminal")
    def test_cli_in_a_terminal(self):
        tally = str(PLUGIN / "bin" / "tally")
        out = run_in_pty([sys.executable, tally], env={"NO_COLOR": ""})
        self.assertIn("\033[38;5;214m _____     _ _\033[0m", out)                # the gradient wordmark
        self.assertIn("\033[2m  → tally --help all commands", out)
        help_out = run_in_pty([sys.executable, tally, "--help"], env={"NO_COLOR": ""})
        self.assertIn("|_   _|_ _| | |_   _", help_out)                                # banner above the help
        self.assertIn("usage: tally", help_out)
        self.assertNotIn("_____", run_in_pty([sys.executable, tally, "start", "-h"], env={"NO_COLOR": ""}))
        out = run_in_pty([sys.executable, tally], env={"NO_COLOR": "1"})
        self.assertNotIn("\033[", out)
        self.assertIn("|_   _|_ _| | |_   _", out)


class Onboarding(unittest.TestCase):
    """tally setup: questions, the Claude Code status line, the welcome after installing."""

    def setUp(self):
        self.env = Env()
        self.claude = self.env.tmp / "claude"
        self.settings_json = self.claude / "settings.json"

    def tearDown(self):
        self.env.cleanup()

    def run_setup(self, *answers, agents=("claude",)):
        from tally import onboarding
        it = iter(answers)
        self.prompts, self.said = [], []

        def ask(prompt):
            self.prompts.append(prompt)
            value = next(it)
            if value is EOFError:
                raise EOFError()
            return value
        return onboarding.run(True, agents=list(agents), ask=ask, say=self.said.append, banner=False)

    def test_answers_are_saved_and_offered_next_time(self):
        from tally import onboarding, settings
        # 't' fits track and tasks, so the question comes again
        self.assertTrue(self.run_setup("off", "ca", "t", "track", "a", "y", "", "n", ""))
        self.assertEqual(settings.get("log.prompts"), "off")
        self.assertEqual(settings.get("log.depth"), "calls")
        self.assertEqual(settings.get("projects.default_mode"), "track")
        self.assertEqual(settings.get("sessions.scope"), "all")
        self.assertIs(settings.get("updates.check"), False)
        self.assertEqual(settings.get("auto_start"), "session")
        self.assertIs(settings.get("todos.reminder"), True)
        self.assertIn("    choose one of: ask, track, tasks", self.said)
        self.assertTrue(onboarding.done())
        self.assertEqual(json.loads(self.settings_json.read_text())["statusLine"], {
            "type": "command", "command": onboarding.statusline_command(), "refreshInterval": 10})
        self.assertIn('python3 "%s" statusline' % (PLUGIN / "bin" / "tally"), onboarding.statusline_command())
        self.assertTrue(any("status line added" in line for line in self.said[-1].split("\n")))
        # again: the current values are offered, Enter keeps them
        self.assertTrue(self.run_setup("", "", "", "", "", "", ""))
        self.assertIn("[off]", self.prompts[0])
        self.assertIn("[track]", self.prompts[2])
        self.assertIn("[all]", self.prompts[3])
        self.assertIn("Y/n", self.prompts[4])
        self.assertEqual(settings.get("log.depth"), "calls")
        self.assertIn("  Claude Code's status line already shows Tally.", self.said)
        self.assertIn("Saved, nothing changed", self.said[-1])

    def test_ctrl_d_saves_nothing(self):
        from tally import onboarding, settings
        self.assertFalse(self.run_setup("off", "calls", EOFError))
        self.assertEqual(settings.get("log.prompts"), "full")
        self.assertEqual(settings.get("log.depth"), "prompts")
        self.assertFalse(onboarding.done())
        self.assertFalse(self.settings_json.exists())

    def test_three_wrong_answers_keep_the_value(self):
        from tally import settings
        self.assertTrue(self.run_setup("x", "y", "z", "", "", "", "maybe", "", "", "", "n"))
        self.assertEqual(settings.get("log.prompts"), "full")
        self.assertIn("    keeping full", self.said)
        self.assertIn("    answer y or n", self.said)
        self.assertFalse(self.settings_json.exists())                         # status line declined

    def test_existing_or_broken_settings_are_left_alone(self):
        self.claude.mkdir(parents=True)
        mine = {"statusLine": {"type": "command", "command": "npx ccstatusline"}, "model": "opus"}
        self.settings_json.write_text(json.dumps(mine))
        self.run_setup("", "", "", "", "", "", "")
        self.assertEqual(len(self.prompts), 7)                                # no status line question
        self.assertEqual(json.loads(self.settings_json.read_text()), mine)
        self.assertTrue(any("ccstatusline: a Custom Command widget" in line for line in self.said))
        self.settings_json.write_text("{not json")
        self.run_setup("", "", "", "", "", "", "")
        self.assertEqual(len(self.prompts), 7)
        self.assertEqual(self.settings_json.read_text(), "{not json")
        self.assertTrue(any("is not valid JSON" in line for line in self.said))

    def test_status_line_joins_existing_settings_and_uninstall_removes_only_it(self):
        from unittest import mock
        from tally import installer
        self.claude.mkdir(parents=True)
        mine = {"model": "opus", "env": {"A": "1"}}
        self.settings_json.write_text(json.dumps(mine))
        self.run_setup("", "", "", "", "", "", "", "y")
        data = json.loads(self.settings_json.read_text())
        self.assertEqual({k: v for k, v in data.items() if k != "statusLine"}, mine)
        self.assertEqual(json.loads((self.claude / "settings.json.bak-tally").read_text()), mine)
        with mock.patch.object(installer.shutil, "which", return_value=None):
            self.assertIn("status line removed", installer.uninstall_claude())
        self.assertEqual(json.loads(self.settings_json.read_text()), mine)
        # a status line the user changed afterwards stays
        self.settings_json.write_text(json.dumps(dict(mine, statusLine={"type": "command",
                                                                         "command": "tally statusline | lolcat"})))
        with mock.patch.object(installer.shutil, "which", return_value=None):
            self.assertNotIn("status line removed", installer.uninstall_claude())
        self.assertIn("lolcat", self.settings_json.read_text())

    def test_no_status_line_question_without_claude_code(self):
        self.run_setup("", "", "", "", "", "", "", agents=("codex", "pi"))
        self.assertEqual(len(self.prompts), 7)
        self.assertFalse(self.settings_json.exists())

    def test_non_interactive_asks_nothing(self):
        from unittest import mock
        from tally import onboarding
        said = []
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertTrue(onboarding.run(False, agents=["claude"], say=said.append))
        self.assertTrue(onboarding.done())
        self.assertEqual(said, ["✓ Settings     defaults (change any time: tally setup)"])
        self.assertFalse(self.settings_json.exists())

    @unittest.skipUnless(hasattr(os, "fork"), "needs a pseudo-terminal")
    def test_tally_setup_in_a_terminal(self):
        from tally import settings
        fakebin = self.env.tmp / "bin"                     # Claude Code "installed", so the status line is asked
        fakebin.mkdir()
        (fakebin / "claude").write_text("#!/bin/sh\nexit 0\n")
        (fakebin / "claude").chmod(0o755)
        out = run_in_pty([sys.executable, str(PLUGIN / "bin" / "tally"), "setup"],
                         answers=["preview", "", "tasks", "", "", "n", "", "y"],
                         env={"NO_COLOR": "1", "PATH": "%s:%s" % (fakebin, os.environ.get("PATH", ""))})
        self.assertIn("Add Tally to Claude Code's status line? Y/n", out)
        self.assertIn("▍Tally · Setup · Enter keeps the value in [brackets]", out)
        self.assertIn("▍Tally · ✓ Saved", out)
        self.assertEqual(settings.get("log.prompts"), "preview")
        self.assertEqual(settings.get("projects.default_mode"), "tasks")
        self.assertIs(settings.get("todos.reminder"), False)
        self.assertIn("statusLine", self.settings_json.read_text())


class Updates(unittest.TestCase):
    """The daily version check, the notices and tally update (never GitHub: a local URL and git repos)."""

    def setUp(self):
        self.env = Env()
        self.manifest = self.env.tmp / "plugin.json"
        self.patch = unittest.mock.patch.dict(os.environ, {"TALLY_UPDATE_URL": self.manifest.as_uri()})
        self.patch.start()
        os.environ.pop("TALLY_NO_UPDATE_CHECK", None)

    def tearDown(self):
        self.patch.stop()
        self.env.cleanup()

    def publish(self, version):
        self.manifest.write_text(json.dumps({"name": "tally", "version": version}))

    def test_check_and_notices(self):
        from tally import __version__, settings, updates
        self.assertTrue(updates.newer("1.10.0", "1.9.3") and not updates.newer("0.8.0", "0.8.0"))
        self.assertTrue(updates.due())
        self.publish(__version__)
        self.assertEqual(updates.check_now()["latest"], __version__)
        self.assertFalse(updates.due())                                    # once a day
        self.assertIsNone(updates.available())
        self.assertIsNone(updates.notice("cli"))
        self.publish("99.0.0")
        updates.check_now()
        self.assertEqual(updates.available(), "99.0.0")
        text = updates.notice("cli")
        self.assertIn("Tally 99.0.0 is out (you have %s)" % __version__, text)
        self.assertIn("tally update to update", text)                       # tests run from a git checkout
        self.assertIsNone(updates.notice("cli"))                            # not again today
        self.assertIsNotNone(updates.notice("session"))                     # each place once
        settings.set_value("updates.check", "off")
        self.assertIsNone(updates.available())
        settings.set_value("updates.check", "on")
        self.manifest.unlink()                                              # offline: kept, retried sooner
        st = updates.check_now()
        self.assertEqual((st["latest"], bool(st["error"])), ("99.0.0", True))
        self.assertFalse(updates.due())
        self.assertTrue(updates.due(time.time() + updates.RETRY_S + 1))
        self.manifest.write_text(json.dumps({"tag_name": "v100.1.0", "name": "Tally 100.1.0"}))   # a GitHub release
        self.assertEqual(updates.check_now()["latest"], "100.1.0")

    def test_session_start_and_doctor_mention_it(self):
        from tally import hooks, updates
        self.publish("99.0.0")
        updates.check_now()
        out = io.StringIO()
        hooks.main("SessionStart", stdin=io.StringIO(json.dumps(
            {"session_id": SID, "cwd": str(self.env.tmp), "source": "startup"})), stdout=out)
        self.assertIn("▍Tally · ⬆ Tally 99.0.0 is out", json.loads(out.getvalue())["systemMessage"])
        r = subprocess.run([sys.executable, str(PLUGIN / "bin" / "tally"), "doctor"], capture_output=True,
                           text=True, env=os.environ, timeout=60)
        self.assertIn("updates       99.0.0 is out: tally update", r.stdout)

    def test_background_check(self):
        from tally import updates
        self.publish("99.0.0")
        updates.check_in_background()
        deadline = time.time() + 20
        while time.time() < deadline and not updates.state().get("latest"):
            time.sleep(0.1)
        self.assertEqual(updates.state()["latest"], "99.0.0")
        os.environ["TALLY_NO_UPDATE_CHECK"] = "1"
        self.assertFalse(updates.enabled())

    def test_where_updates_come_from(self):
        from tally import updates
        clone = self.env.tmp / "clone"
        (clone / ".git").mkdir(parents=True)
        (clone / "plugin" / "bin").mkdir(parents=True)
        (clone / "plugin" / "bin" / "tally").write_text("")
        known = self.env.tmp / "claude" / "plugins" / "known_marketplaces.json"
        known.parent.mkdir(parents=True, exist_ok=True)
        with unittest.mock.patch("tally.installer.REPO", self.env.tmp / "cache" / "tally"):     # Claude Code's copy
            known.write_text(json.dumps({"tally": {"source": {"source": "directory", "path": str(clone)},
                                                   "installLocation": str(clone)}}))
            self.assertEqual((updates.source_root(), updates.update_command()), (clone, "tally update"))
            known.write_text(json.dumps({"tally": {"source": {"source": "github", "repo": "janhorecny/tally"},
                                                   "installLocation": str(clone)}}))      # its own clone
            self.assertIsNone(updates.source_root())
            self.assertTrue(updates.update_command().startswith("claude plugin marketplace update tally"))

    def test_changelog_between_versions(self):
        from tally import updates
        root = self.env.tmp / "r"
        root.mkdir()
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.10.0 (x)\n\n### Big\n\n- **New thing:** yes\n"
                                           "\n## 0.9.1 (x)\n\n- **Fix:** a\n\n## 0.9.0 (x)\n\n- **Old:** b\n")
        self.assertEqual(updates.changes(root, "0.9.0", "0.10.0"),
                         ["  0.10.0 (x)", "    · New thing", "  0.9.1 (x)", "    · Fix"])

    def test_tally_update_pulls_reinstalls_and_says_what_changed(self):
        import socket
        git = lambda cwd, *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd)]  # noqa: E731
                                             + list(a), capture_output=True, text=True, check=True).stdout
        src = self.env.tmp / "src"
        src.mkdir()
        shutil.copytree(str(PLUGIN), str(src / "plugin"), ignore=shutil.ignore_patterns("__pycache__"))
        init = src / "plugin" / "tally" / "__init__.py"

        def release(version, note):
            init.write_text(re.sub(r'__version__ = "[^"]+"', '__version__ = "%s"' % version, init.read_text()))
            old = (src / "CHANGELOG.md").read_text() if (src / "CHANGELOG.md").exists() else "# Changelog\n"
            (src / "CHANGELOG.md").write_text(old.replace("# Changelog\n", "# Changelog\n\n## %s (today)\n\n- **%s:** x\n"
                                                          % (version, note), 1))
            git(src, "add", "-A")
            git(src, "commit", "-qm", version)
        git(src, "init", "-q", "-b", "main")
        release("0.0.1", "First")
        inst = self.env.tmp / "inst"
        git(self.env.tmp, "clone", "-q", str(src), str(inst))
        release("0.0.2", "Second thing")
        from tally import settings
        with socket.socket() as sock:                                       # never the real dashboard's port
            sock.bind(("127.0.0.1", 0))
            settings.set_value("dashboard_port", sock.getsockname()[1])
        run = lambda *a: subprocess.run([sys.executable, str(inst / "plugin" / "bin" / "tally")] + list(a),  # noqa: E731
                                        capture_output=True, text=True, env=os.environ, timeout=60,
                                        stdin=subprocess.DEVNULL)
        (inst / "plugin" / "tally" / "fmt.py").write_text("# edited\n" + (inst / "plugin" / "tally" / "fmt.py").read_text())
        r = run("update")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has local changes", r.stdout + r.stderr)
        git(inst, "checkout", "--", ".")
        r = run("update")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Updated Tally 0.0.1 → 0.0.2", r.stdout)
        self.assertIn("· Second thing", r.stdout)
        self.assertNotIn("· First", r.stdout)
        self.assertIn('__version__ = "0.0.2"', (inst / "plugin" / "tally" / "__init__.py").read_text())
        self.assertIn("Tally 0.0.2 is up to date", run("update").stdout)
        # Claude Code still runs an older copy (a checkout updated by hand): install the checkout's version
        plugins = self.env.tmp / "claude" / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {"tally@tally": [{"version": "0.0.1"}]}}))
        r = subprocess.run([sys.executable, str(inst / "plugin" / "bin" / "tally"), "update"], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, PATH="/usr/bin:/bin"))              # no claude CLI here
        self.assertIn("Tally 0.0.2: installing it into the agents (Claude Code had 0.0.1)", r.stdout)
        self.assertIn("✗ Claude Code", r.stdout)                             # it tried
        from tally import updates
        self.assertEqual(updates.state()["latest"], "0.0.2")


class InstallerOnboarding(unittest.TestCase):
    """The first install asks the setup questions once and greets each new agent once."""

    def setUp(self):
        self.env = Env()
        self.home = self.env.tmp / "fakehome"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".pi" / "agent").mkdir(parents=True)
        self.saved = {k: os.environ.get(k) for k in ("HOME", "CODEX_HOME", "PI_CODING_AGENT_DIR")}
        os.environ["HOME"] = str(self.home)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("PI_CODING_AGENT_DIR", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.env.cleanup()

    def session_start(self, agent, sid):
        from tally import hooks
        out = io.StringIO()
        hooks.main("SessionStart", stdin=io.StringIO(json.dumps(
            {"session_id": sid, "cwd": str(self.env.tmp), "source": "startup"})), stdout=out, agent=agent)
        text = out.getvalue().strip()
        return json.loads(text).get("systemMessage", "") if text else ""

    def test_setup_once_and_a_welcome_per_new_agent(self):
        from unittest import mock
        from tally import installer, settings
        with mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", side_effect=["off", "", "", "", "", "", ""]):
            stdin.isatty.return_value = True
            self.assertEqual(installer.install(["codex", "pi"]), 0)
        self.assertEqual(settings.get("log.prompts"), "off")
        with mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", side_effect=AssertionError("asked")):
            stdin.isatty.return_value = True
            self.assertEqual(installer.install(["codex", "pi"]), 0)             # an update asks nothing
        codex = self.session_start("codex", "C1")
        self.assertIn("▍Tally · 👋 Installed.", codex)
        self.assertIn("tally:start <name>", codex)
        self.assertNotIn("/tally:", codex)                                       # Codex syntax
        self.assertIn("/tally:sessions", self.session_start("pi", "P1"))      # pi's extension rewrites it
        self.assertNotIn("Installed.", self.session_start("codex", "C2"))      # once per agent
        self.assertNotIn("Installed.", self.session_start("pi", "P2"))


if __name__ == "__main__":
    unittest.main()
