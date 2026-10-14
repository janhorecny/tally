"""Ingest robustness: records that cannot be stored, fork agents, responses
written over several lines, Codex baselines after a skip, prices.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import sqlite3
import time
import unittest
import unittest.mock

try:
    from test_tally import Env, SID, assistant, codex_line, codex_tokens, iso, user
except ImportError:                                   # run as tests.test_qa_ingest
    from tests.test_tally import Env, SID, assistant, codex_line, codex_tokens, iso, user

LONE = "\ud83d"            # half an emoji, cut by a 220-character limit; JSON.stringify writes it as \ud83d


def workflow_result(ts, run_id, name, summary):
    return {"parentUuid": None, "isSidechain": False, "promptId": "p1", "type": "user", "timestamp": iso(ts),
            "uuid": "uw-%s" % ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_wf", "content": "ok"}]},
            "toolUseResult": {"runId": run_id, "workflowName": name, "status": "completed", "summary": summary}}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        from tally import db, tasks
        self.conn = db.connect()
        tasks.touch_session(self.conn, SID, str(self.env.main()), "/Users/me/proj", agent="claude")

    def tearDown(self):
        self.conn.close()
        self.env.cleanup()

    def ingest(self, **kw):
        from tally import ingest
        return ingest.ingest_session(self.conn, SID, str(self.env.main()), **kw)

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def errors(self) -> str:
        from tally import paths
        p = paths.error_log_path()
        return p.read_text() if p.exists() else ""


class RecordsThatCannotBeStored(Base):
    """One bad record must not stop the counting of a whole session (the offsets
    never moved, every later hook hit the same record)."""

    def test_half_emoji_in_a_workflow_summary(self):
        e = self.env
        t = time.time() - 60
        e.append(e.main(), user(t, "go", prompt_id="p1"), assistant(t + 1, "msg_1", "req_1", out=100),
                 workflow_result(t + 2, "wf_1", "crawl " + LONE, "x" * 219 + LONE),
                 assistant(t + 3, "msg_2", "req_2", out=5000))
        self.ingest()
        e.append(e.main(), assistant(t + 4, "msg_3", "req_3", out=7000))
        self.ingest()
        self.assertEqual(tuple(self.one("SELECT COUNT(*), SUM(output) FROM usage")), (3, 12100))
        self.assertEqual(self.one("SELECT offset FROM files")[0], e.main().stat().st_size)
        wf = self.one("SELECT name, summary FROM workflows WHERE run_id = 'wf_1'")
        self.assertEqual((wf["name"], wf["summary"]), ("crawl ?", "x" * 219 + "?"))
        self.assertEqual(self.errors(), "")                        # cleaned, nothing skipped

    def test_half_emoji_in_agent_meta_and_tool_name(self):
        e = self.env
        t = time.time() - 60
        e.append(e.main(), user(t, "go", prompt_id="p1"), assistant(t + 1, "msg_1", "req_1", out=100))
        self.ingest()
        sub = e.sdir() / "subagents"
        e.meta(sub / "agent-a1.meta.json", {"agentType": "general-purpose", "description": "Fix " + LONE})
        e.append(sub / "agent-a1.jsonl", user(t + 2, "task", agent_id="a1", prompt_id="p1"),
                 assistant(t + 3, "msg_a1", "req_a1", out=300, agent_id="a1", sidechain=True,
                           content=[{"type": "tool_use", "id": "toolu_1", "name": "Bash" + LONE, "input": {}}]))
        e.append(e.main(), assistant(t + 4, "msg_2", "req_2", out=5000))
        self.ingest()
        self.assertEqual(tuple(self.one("SELECT COUNT(*), SUM(output) FROM usage")), (3, 5400))
        self.assertEqual(self.one("SELECT description FROM agents WHERE agent_id = 'a1'")[0], "Fix ?")
        self.assertEqual(self.one("SELECT tool FROM tool_calls WHERE id = 'toolu_1'")[0], "Bash?")

    def test_a_record_that_still_fails_is_skipped_and_logged_once(self):
        e = self.env
        t = time.time() - 60
        too_big = 10 ** 20                                          # no SQLite INTEGER holds it
        e.append(e.main(), user(t, "go", prompt_id="p1"),
                 assistant(t + 1, "msg_1", "req_1", out=100),
                 assistant(t + 2, "msg_bad", "req_bad", inp=too_big, out=1),
                 {"type": "system", "subtype": "turn_duration", "durationMs": 1e30, "timestamp": iso(t + 3),
                  "uuid": "turn-bad", "isSidechain": False},
                 assistant(t + 4, "msg_2", "req_2", out=200))
        stats = self.ingest()
        self.assertEqual(stats.skipped, 2)
        self.assertEqual(tuple(self.one("SELECT COUNT(*), SUM(output) FROM usage")), (2, 300))
        self.assertEqual(self.one("SELECT offset FROM files")[0], e.main().stat().st_size)
        log = self.errors()
        self.assertEqual(log.count("ingest: skipped 2 record(s)"), 1, log)
        self.assertIn(str(e.main()), log)
        # the next one: counted around it, the same problem is not logged again within the hour
        e.append(e.main(), assistant(t + 5, "msg_bad2", "req_bad2", inp=too_big, out=1),
                 assistant(t + 6, "msg_3", "req_3", out=400))
        self.ingest()
        self.assertEqual(tuple(self.one("SELECT COUNT(*), SUM(output) FROM usage")), (3, 700))
        self.assertEqual(self.errors(), log)

    def test_a_failing_file_does_not_hold_back_the_others(self):
        from tally import ingest
        e = self.env
        t = time.time() - 60
        sub = e.sdir() / "subagents"
        e.append(e.main(), user(t, "go", prompt_id="p1"), assistant(t + 1, "msg_1", "req_1", out=100))
        e.append(sub / "agent-bad.jsonl", assistant(t + 2, "msg_b", "req_b", out=20, agent_id="bad", sidechain=True))
        e.append(sub / "agent-good.jsonl", assistant(t + 3, "msg_g", "req_g", out=30, agent_id="good", sidechain=True))
        orig = ingest._process_chunk

        def flaky(conn, session_id, agent_id, *a, **kw):
            if agent_id == "bad":
                raise ValueError("boom")
            return orig(conn, session_id, agent_id, *a, **kw)
        with unittest.mock.patch.object(ingest, "_process_chunk", flaky):
            self.ingest()
        self.assertEqual(self.one("SELECT SUM(output) FROM usage")[0], 130)
        self.assertIsNone(self.one("SELECT offset FROM files WHERE agent_id = 'bad'"))   # rolled back: read again
        self.assertIn("agent-bad.jsonl", self.errors())
        self.ingest()
        self.assertEqual(self.one("SELECT SUM(output) FROM usage")[0], 150)

    def test_a_database_error_is_not_skipped(self):
        """Locked, disk full: the whole read fails and is retried, nothing is lost."""
        from tally import ingest
        e = self.env
        e.append(e.main(), user(time.time() - 9, "go", prompt_id="p1"),
                 assistant(time.time() - 8, "msg_1", "req_1", out=100))

        def broken(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")
        with unittest.mock.patch.object(ingest, "_process_chunk", broken):
            with self.assertRaises(sqlite3.OperationalError):
                self.ingest()
        self.assertIsNone(self.one("SELECT offset FROM files"))
        self.ingest()
        self.assertEqual(self.one("SELECT SUM(output) FROM usage")[0], 100)

    def test_pi_prompt_with_half_an_emoji(self):
        from tally import ingest, tasks
        e = self.env
        sess = e.tmp / "pi" / "2026-10-12T08-00-00-000Z_P1.jsonl"
        e.append(sess, {"type": "session", "version": 3, "id": "P1", "timestamp": iso(time.time() - 60),
                        "cwd": "/Users/me/proj"})
        tasks.touch_session(self.conn, "P1", str(sess), "/Users/me/proj", agent="pi")
        tasks.start(self.conn, "P1", name="pi task", transcript_path=str(sess))
        t = time.time() + 0.01
        e.append(sess, {"type": "message", "id": "u1", "timestamp": iso(t),
                        "message": {"role": "user", "content": "fix the " + LONE}},
                 {"type": "message", "id": "a1", "timestamp": iso(t + 0.01),
                  "message": {"role": "assistant", "provider": "zai", "model": "glm-5.2", "stopReason": "stop",
                              "usage": {"input": 100, "output": 20, "totalTokens": 120}, "content": []}})
        ingest.ingest_session(self.conn, "P1", str(sess))
        self.assertEqual(self.one("SELECT SUM(output) FROM usage WHERE session_id = 'P1'")[0], 20)
        self.assertEqual(self.one("SELECT prompt FROM prompts WHERE prompt_id = 'u1'")[0], "fix the ?")


class ForkAgents(Base):
    """Fork agents copy their parent's history line for line: the copies must not
    take the parent's usage over (totals were right, the agent tree was not)."""

    def test_copied_history_stays_with_the_main_session(self):
        e = self.env
        t = time.time() - 60
        e.append(e.main(), user(t, "start", prompt_id="p1"),
                 assistant(t + 1, "msg_m1", "req_m1", out=5000),
                 assistant(t + 2, "msg_m2", "req_m2", out=4),               # in flight when the fork copied it
                 assistant(t + 2.1, "msg_m2", "req_m2", out=4369))
        self.ingest()
        sub = e.sdir() / "subagents"
        e.meta(sub / "agent-afork.meta.json", {"agentType": "fork", "isFork": True, "spawnDepth": 1})
        e.append(sub / "agent-afork.jsonl", user(t, "start", agent_id="afork", prompt_id="p1"),
                 assistant(t + 1, "msg_m1", "req_m1", out=5000, agent_id="afork", sidechain=True),
                 assistant(t + 2, "msg_m2", "req_m2", out=4, agent_id="afork", sidechain=True),
                 assistant(t + 3, "msg_f1", "req_f1", out=10, agent_id="afork", sidechain=True))
        self.ingest()
        own = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT agent_id, COUNT(*), SUM(output) FROM usage GROUP BY agent_id")}
        self.assertEqual(own, {"": (2, 9369), "afork": (1, 10)})

    def test_fork_read_after_its_parent_whatever_the_file_names(self):
        """A fork of a subagent whose file name sorts first, all read at once."""
        e = self.env
        t = time.time() - 60
        sub = e.sdir() / "subagents"
        e.append(e.main(), user(t, "go", prompt_id="p1"), assistant(t + 1, "msg_m", "req_m", out=1))
        e.meta(sub / "agent-zparent.meta.json", {"agentType": "general-purpose", "spawnDepth": 1})
        e.append(sub / "agent-zparent.jsonl", assistant(t + 2, "msg_p", "req_p", out=700, agent_id="zparent",
                                                         sidechain=True))
        for name, depth in (("afork2", 3), ("afork1", 2)):        # a fork of the fork, and the fork
            e.meta(sub / ("agent-%s.meta.json" % name), {"agentType": "fork", "isFork": True,
                                                          "parentAgentId": "zparent", "spawnDepth": depth})
        e.append(sub / "agent-afork1.jsonl",
                 assistant(t + 2, "msg_p", "req_p", out=700, agent_id="afork1", sidechain=True),
                 assistant(t + 3, "msg_f", "req_f", out=30, agent_id="afork1", sidechain=True))
        e.append(sub / "agent-afork2.jsonl",
                 assistant(t + 2, "msg_p", "req_p", out=700, agent_id="afork2", sidechain=True),
                 assistant(t + 3, "msg_f", "req_f", out=30, agent_id="afork2", sidechain=True),
                 assistant(t + 4, "msg_g", "req_g", out=5, agent_id="afork2", sidechain=True))
        self.ingest()
        owner = {r[0]: r[1] for r in self.conn.execute("SELECT msg_key, agent_id FROM usage")}
        self.assertEqual(owner, {"msg_m:req_m": "", "msg_p:req_p": "zparent", "msg_f:req_f": "afork1",
                                 "msg_g:req_g": "afork2"})
        from tally import ingest
        order = [a for _, a, _ in ingest.session_files(e.main())]
        self.assertEqual(order, ["", "zparent", "afork1", "afork2"])
        self.assertEqual(order, [a for _, a, _ in ingest.session_files(e.main(), self.conn, SID)])


class ResponseOverSeveralLines(Base):
    def test_response_started_before_the_task_ended_counts_in_full(self):
        """Content blocks of one response carry partial output_tokens, the last line the final number:
        the row keeps the time of its first line and the tokens of its last."""
        from tally import report, tasks
        e = self.env
        now = time.time()
        task, _ = tasks.start(self.conn, SID, name="t", transcript_path=str(e.main()), now=now - 600)
        sub = e.sdir() / "subagents" / "agent-abg.jsonl"
        e.append(e.main(), user(now - 599, "go", prompt_id="p1"), assistant(now - 598, "msg_main", "req_main", out=100))
        e.append(sub, user(now - 597, "task", agent_id="abg", prompt_id="p1"),
                 assistant(now - 30, "msg_bg", "req_bg", out=40, agent_id="abg", sidechain=True))
        tasks.end(self.conn, task["id"], now=now - 20)
        e.append(sub, assistant(now - 5, "msg_bg", "req_bg", out=8000, agent_id="abg", sidechain=True))
        self.ingest()
        row = self.one("SELECT ts, output FROM usage WHERE msg_key = 'msg_bg:req_bg'")
        self.assertEqual(row["output"], 8000)
        self.assertAlmostEqual(row["ts"], now - 30, places=2)
        rep = report.build(self.conn, tasks.get(self.conn, task["id"]))
        self.assertEqual(rep["totals"]["output"], 8100)


class SkippedHistory(Base):
    """A task start skips the history (skip_existing): lines read later still need
    the prompt in effect and, for Codex, the thread's running token total."""

    def test_claude_takes_the_prompt_from_the_tail(self):
        e = self.env
        t = time.time() - 60
        e.append(e.main(), user(t, "old", prompt_id="p1"), assistant(t + 1, "msg_1", "req_1", out=100))
        self.ingest(skip_existing=True)
        self.assertEqual(self.one("SELECT COUNT(*) FROM usage")[0], 0)
        self.assertEqual(self.one("SELECT last_prompt_id FROM files")[0], "p1")
        e.append(e.main(), assistant(t + 2, "msg_2", "req_2", out=7))
        self.ingest()
        self.assertEqual(tuple(self.one("SELECT prompt_id, output FROM usage")), ("p1", 7))

    def codex_rollout(self, tid, *lines):
        from tally import tasks
        folder = self.env.tmp / "codex" / "sessions" / time.strftime("%Y/%m/%d")
        path = folder / ("rollout-2026-10-14T10-00-00-%s.jsonl" % tid)
        self.env.append(path, codex_line(time.time() - 3600, "session_meta",
                                         {"id": tid, "session_id": tid, "cwd": "/Users/me/proj"}), *lines)
        tasks.touch_session(self.conn, tid, str(path), "/Users/me/proj", agent="codex")
        return path

    def codex_task_usage(self, tid, roll):
        from tally import ingest
        ingest.ingest_session(self.conn, tid, str(roll), skip_existing=True)
        t = time.time()
        self.env.append(roll, codex_line(t, "turn_context", {"turn_id": "turn2", "model": "gpt-5.5"}),
                        codex_tokens(t + 1, 5_010_000, 4_005_000, 201_000))       # the task's own call
        ingest.ingest_session(self.conn, tid, str(roll))
        return [tuple(r) for r in self.conn.execute("SELECT input, cache_read, output FROM usage")]

    def test_codex_baseline_from_the_tail(self):
        t0 = time.time() - 3600
        tid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
        roll = self.codex_rollout(tid, codex_line(t0, "turn_context", {"turn_id": "turn1", "model": "gpt-5.5"}),
                                  codex_tokens(t0 + 10, 5_000_000, 4_000_000, 200_000))
        self.assertEqual(self.codex_task_usage(tid, roll), [(5000, 5000, 1000)])

    def test_codex_baseline_when_the_tail_has_no_token_count(self):
        t0 = time.time() - 3600
        tid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0002"
        roll = self.codex_rollout(tid, codex_line(t0, "turn_context", {"turn_id": "turn1", "model": "gpt-5.5"}),
                                  codex_tokens(t0 + 10, 5_000_000, 4_000_000, 200_000),
                                  codex_line(t0 + 11, "response_item", {"type": "function_call_output",
                                                                        "call_id": "c1", "output": "x" * 1_200_000}))
        self.assertEqual(self.codex_task_usage(tid, roll), [(5000, 5000, 1000)])


class ToolBackfill(Base):
    def test_tools_offset_is_kept_when_no_line_is_complete_yet(self):
        from tally import ingest
        e = self.env
        t = time.time() - 60
        e.append(e.main(), user(t, "go", prompt_id="p1"),
                 assistant(t + 1, "msg_1", "req_1", out=10,
                           content=[{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]))
        self.ingest()
        self.conn.execute("UPDATE files SET tools_offset = 0")         # read by an older hook without tool calls
        self.conn.execute("DELETE FROM tool_calls")
        with open(e.main(), "a") as fh:
            fh.write('{"type": "assistant", "timest')                   # a line being written
        self.ingest()
        self.assertEqual(self.one("SELECT tool FROM tool_calls WHERE id = 'toolu_1'")[0], "Bash")
        f = self.one("SELECT offset, tools_offset FROM files")
        self.assertEqual(f["tools_offset"], f["offset"])
        with unittest.mock.patch.object(ingest, "_backfill_tools") as backfill:
            self.ingest()
        backfill.assert_not_called()                                    # not scanned again from the start


class Prices(Base):
    def test_web_searches_are_priced(self):
        from tally import reconcile, report, tasks
        from tally.pricing import Pricer
        p = Pricer()
        base = p.cost("claude-opus-5-5", 10, 100, 1000, 200)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 10, 100, 1000, 200, web_search=3), base + 0.03)
        e = self.env
        t = time.time()
        task, _ = tasks.start(self.conn, SID, name="t", transcript_path=str(e.main()), now=t - 60)
        line = assistant(t - 50, "msg_1", "req_1")
        line["message"]["usage"]["server_tool_use"]["web_search_requests"] = 2
        e.append(e.main(), user(t - 55, "search", prompt_id="p1"), line)
        self.ingest()
        rep = report.build(self.conn, tasks.get(self.conn, task["id"]))
        self.assertEqual(rep["totals"]["web_search"], 2)
        self.assertAlmostEqual(rep["totals"]["cost"], base + 0.02, places=6)
        # what reconcile counts as logged includes them too, so unlogged does not count them again
        logged = reconcile.scan_session_totals(e.main(), t)
        self.assertAlmostEqual(logged["_total"]["cost"], base + 0.02, places=6)

    def test_invalid_price_overrides_are_skipped(self):
        from tally import paths, report, tasks
        from tally.pricing import DEFAULT_PRICES, Pricer
        for bad in ("junk", ["x"], 3, {"claude-opus-5-5": "cheap"}, {"claude-opus-5-5": {"input": None}}):
            self.assertEqual(Pricer(bad).prices["claude-opus-5-5"], DEFAULT_PRICES["claude-opus-5-5"], bad)
        p = Pricer({"claude-opus-5-5": {"input": "4$", "output": 30, "cache_read": True},
                    "my-model": {"input": "x", "output": 1},
                    "ok-model": {"input": "1", "output": 2}})
        self.assertEqual(p.prices["claude-opus-5-5"]["input"], DEFAULT_PRICES["claude-opus-5-5"]["input"])
        self.assertEqual(p.prices["claude-opus-5-5"]["output"], 30.0)
        self.assertEqual(p.prices["claude-opus-5-5"]["cache_read"], DEFAULT_PRICES["claude-opus-5-5"]["cache_read"])
        self.assertIsNone(p.resolve("my-model"))
        self.assertAlmostEqual(p.cost("ok-model", 1_000_000, 1_000_000), 3.0)
        # a report (and so /tally:end, /tally:start and the dashboard) still works
        paths.save_config({"pricing": {"claude-opus-5-5": {"input": "4$"}}})
        e = self.env
        task, _ = tasks.start(self.conn, SID, name="t", transcript_path=str(e.main()), now=time.time() - 60)
        e.append(e.main(), assistant(time.time() - 50, "msg_1", "req_1"))
        self.ingest()
        self.assertGreater(report.build(self.conn, tasks.get(self.conn, task["id"]))["totals"]["cost"], 0)


class CodexOtherRollouts(Base):
    def test_rollouts_of_other_sessions_are_not_read_again(self):
        from tally import codex, ingest, tasks
        folder = self.env.tmp / "codex" / "sessions" / time.strftime("%Y/%m/%d")
        t = time.time()

        def rollout(tid, meta):
            path = folder / ("rollout-2026-10-14T10-00-00-%s.jsonl" % tid)
            self.env.append(path, codex_line(t - 100, "session_meta", dict({"cwd": "/Users/me/proj"}, **meta)))
            return path
        main = rollout("T1", {"id": "T1", "session_id": "T1"})
        other = rollout("O1", {"id": "O1", "session_id": "O1"})
        tasks.touch_session(self.conn, "T1", str(main), "/Users/me/proj", agent="codex")
        half = folder / "rollout-2026-10-14T10-00-00-S2.jsonl"
        with open(half, "w") as fh:
            fh.write('{"timestamp": "%s", "type": "session_meta", "payload": {"id": "S2", "sess' % iso(t))
        ingest.ingest_session(self.conn, "T1", str(main))
        real = codex._first_meta
        with unittest.mock.patch.object(codex, "_first_meta", side_effect=real) as first_meta:
            ingest.ingest_session(self.conn, "T1", str(main))
        self.assertNotIn(other, [c.args[0] for c in first_meta.call_args_list])
        self.assertIn(half, [c.args[0] for c in first_meta.call_args_list])   # not complete: looked at again
        # subagents still found: a new one, and the one whose first line is complete now
        sub = rollout("S1", {"id": "S1", "session_id": "T1", "agent_nickname": "scout " + LONE})
        self.env.append(sub, codex_tokens(t + 1, 1000, 0, 50))
        half.write_text(json.dumps(codex_line(t, "session_meta", {"id": "S2", "session_id": "T1"})) + "\n"
                        + json.dumps(codex_tokens(t + 2, 2000, 0, 70)) + "\n")
        ingest.ingest_session(self.conn, "T1", str(main))
        self.assertEqual({r[0]: r[1] for r in self.conn.execute("SELECT agent_id, SUM(output) FROM usage "
                                                                 "GROUP BY agent_id")}, {"S1": 50, "S2": 70})
        self.assertEqual(self.one("SELECT description FROM agents WHERE agent_id = 'S1'")[0], "scout ?")


if __name__ == "__main__":
    unittest.main()
