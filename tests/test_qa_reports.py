"""Reports, formatting and exports: fixes from the QA review of 0.11.8.

Run:  python3 -m unittest discover -s tests -v
"""

import csv
import io
import json
import time
import unittest
from unittest import mock

try:
    from test_tally import SID, Env, assistant, turn, user
except ImportError:                                   # run as tests.test_qa_reports
    from tests.test_tally import SID, Env, assistant, turn, user


def _local(*ymdhm) -> float:
    return time.mktime(tuple(ymdhm) + (0, 0, 0, -1))


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def task_with_one_prompt(self, model="claude-opus-5-5", name="demo"):
        """A live task with one prompt and one API call; returns (conn, task)."""
        from tally import db, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 50, "old"))
        e.hook("SessionStart", source="startup")
        e.hook("UserPromptSubmit", prompt="/tally:start " + name)
        now = time.time()
        e.hook("UserPromptSubmit", prompt="one", prompt_id="p1")
        e.append(e.main(), user(now, "one", prompt_id="p1"), assistant(now + 1, "m1", "r1", model=model, out=1000))
        e.hook("Stop")
        conn = db.connect()
        return conn, tasks.active_for_session(conn, SID)


class Snapshots(Base):
    def test_snapshot_is_rebuilt_when_calls_are_read_after_the_end(self):
        """tasks.end swallows a failed transcript read and freezes the report anyway: the calls read
        later must show, and the rebuilt report is stored."""
        from tally import db, ingest, report, tasks
        e = self.env
        conn = db.connect()
        e.append(e.main(), user(time.time() - 500, "old"))
        tasks.touch_session(conn, SID, str(e.main()), "/Users/me/proj")
        task, _ = tasks.start(conn, SID, name="A", now=time.time() - 400, transcript_path=str(e.main()))
        e.append(e.main(), user(time.time() - 300, "a1", prompt_id="p1"),
                 assistant(time.time() - 290, "m1", "r1", out=5000))
        with mock.patch.object(ingest, "ingest_sessions", side_effect=RuntimeError("database is locked")):
            tasks.end(conn, task["id"])
        ingest.ingest_session(conn, SID)                   # a later read succeeds
        task = tasks.get(conn, task["id"])
        self.assertEqual(report.summary(conn, task)["totals"]["output"], 5000)
        self.assertEqual(report.summary_for(conn, task)["totals"]["output"], 5000)
        row = conn.execute("SELECT computed_at, report_json, summary_json FROM task_snapshots WHERE task_id = ?",
                           (task["id"],)).fetchone()
        self.assertEqual(json.loads(row["report_json"])["totals"]["output"], 5000)
        self.assertEqual(json.loads(row["summary_json"])["totals"]["output"], 5000)
        report.summary_for(conn, task)
        report.summary(conn, task)
        self.assertEqual(conn.execute("SELECT computed_at FROM task_snapshots WHERE task_id = ?",
                                      (task["id"],)).fetchone()[0], row["computed_at"])   # in step: kept

    def test_snapshot_shows_the_todo_status_of_now(self):
        from tally import db, fmt, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 50, "old"))
        e.hook("SessionStart", source="startup")
        e.hook("UserPromptSubmit", prompt="/tally:todo Write the docs")
        e.hook("UserPromptSubmit", prompt="/tally:take 1")
        e.hook("UserPromptSubmit", prompt="/tally:end")
        e.hook("UserPromptSubmit", prompt="/tally:done 1")
        conn = db.connect()
        task = tasks.get(conn, 1)
        self.assertEqual(task["status"], "ended")
        rep = report.summary_for(conn, task)
        self.assertEqual(rep["task"]["todo"]["status"], "done")
        self.assertIn("todo #1 (done)", fmt.render_report(rep))
        self.assertEqual(report.summary(conn, task)["task"]["todo"]["status"], "done")


class ActiveTime(Base):
    def test_a_running_turn_counts_live_and_when_the_task_ends_during_it(self):
        from tally import db, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 5000, "old"))
        e.hook("SessionStart", source="startup")
        T = time.time()
        with mock.patch("time.time", return_value=T - 3000):
            e.hook("UserPromptSubmit", prompt="/tally:start long goal")
        with mock.patch("time.time", return_value=T - 2900):
            e.hook("UserPromptSubmit", prompt="/goal migrate everything", prompt_id="p1")     # the turn opens
        e.append(e.main(), user(T - 2900, "/goal migrate everything", prompt_id="p1"),
                 assistant(T - 2000, "m1", "r1", out=4000), assistant(T - 100, "m2", "r2", out=4000))
        conn = db.connect()
        task = tasks.active_for_session(conn, SID)
        live = report.summary_for(conn, task)
        self.assertGreaterEqual(live["time"]["active_s"], 2899)
        self.assertEqual(live["time"]["turns"], 1)
        self.assertGreaterEqual(live["tree"][0]["duration_s"], 2899)
        tasks.end(conn, task["id"], now=T - 50)                  # ended while the turn runs
        e.hook("Stop")                                           # the turn finishes later
        rep = report.summary_for(conn, tasks.get(conn, task["id"]))
        self.assertAlmostEqual(rep["time"]["active_s"], 2850, delta=1)
        self.assertEqual(rep["time"]["turns"], 1)
        self.assertAlmostEqual(rep["prompts"][0]["duration_s"], 2850, delta=1)

    def test_a_turn_without_stop_counts_ten_minutes_past_the_last_call(self):
        from tally import db, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 9000, "old"))
        e.hook("SessionStart", source="startup")
        T = time.time()
        with mock.patch("time.time", return_value=T - 8000):
            e.hook("UserPromptSubmit", prompt="/tally:start hung")
        e.append(e.main(), assistant(T - 6900, "m1", "r1", out=10))
        conn = db.connect()
        db.kv_set(conn, "open_turn:" + SID, repr(T - 7000))      # an older Tally stored a bare float
        task = tasks.active_for_session(conn, SID)
        rep = report.summary_for(conn, task)
        self.assertAlmostEqual(rep["time"]["active_s"], 100 + report.OPEN_TURN_IDLE_S, delta=1)
        self.assertEqual(rep["time"]["turns"], 1)

    def test_main_session_time_is_the_union_of_its_own_windows(self):
        """A task counted a session twice; another task ran in the gap. Neither the main session's
        time nor a subagent's span may include the gap."""
        from tally import db, report, tasks
        e = self.env
        conn = db.connect()
        T = time.time() - 20000
        m = e.main()
        e.append(m, user(T - 50, "old"))
        tasks.touch_session(conn, SID, str(m), "/Users/me/proj")
        a, _ = tasks.start(conn, SID, name="A", now=T, transcript_path=str(m))
        e.append(m, user(T + 1, "a1", prompt_id="pA1"), assistant(T + 10, "m1", "r1"), turn(T + 20, 19000, "t1"))
        sub = e.sdir() / "subagents"
        e.meta(sub / "agent-bg.meta.json", {"agentType": "general-purpose", "description": "bg"})
        e.append(sub / "agent-bg.jsonl", user(T + 5, "go", agent_id="bg", prompt_id="pA1"),
                 assistant(T + 30, "s1", "rs1", agent_id="bg", sidechain=True),
                 user(T + 500, "still here", agent_id="bg"))           # written while task B ran
        tasks.record_prompt(conn, a, SID, "a1", now=T + 1, prompt_id="pA1")
        tasks.end(conn, a["id"], now=T + 100)
        b, _ = tasks.start(conn, SID, name="B", now=T + 200, transcript_path=str(m))
        e.append(m, user(T + 210, "b1", prompt_id="pB1"), assistant(T + 790, "m2", "r2"), turn(T + 800, 590000, "t2"))
        tasks.end(conn, b["id"], now=T + 900)
        with db.transaction(conn):                                     # A counts the session again
            conn.execute("UPDATE tasks SET status = 'active', ended_at = NULL WHERE id = ?", (a["id"],))
            conn.execute("DELETE FROM task_snapshots WHERE task_id = ?", (a["id"],))
            conn.execute("INSERT INTO task_sessions(task_id, session_id, attached_at, reason) VALUES(?,?,?,?)",
                         (a["id"], SID, T + 1000, "reopen"))
        e.append(m, user(T + 1001, "a2", prompt_id="pA2"), assistant(T + 1010, "m3", "r3"), turn(T + 1020, 19000, "t3"))
        tasks.end(conn, a["id"], now=T + 1100)
        rep = report.summary_for(conn, tasks.get(conn, a["id"]))
        self.assertAlmostEqual(rep["time"]["active_s"], 38, delta=0.1)
        main = rep["tree"][0]
        self.assertAlmostEqual(main["duration_s"], 38, delta=0.1)
        bg = [c for c in main["children"] if c.get("agent_id") == "bg"][0]
        self.assertAlmostEqual(bg["duration_s"], 95, delta=0.1)           # T+5 .. the window's end at T+100


class Prompts(Base):
    def test_a_prompt_stored_twice_is_listed_and_counted_once(self):
        from tally import db, fmt, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 300, "old"))
        e.hook("SessionStart", source="startup")
        e.hook("UserPromptSubmit", prompt="/tally:start demo")
        e.hook("UserPromptSubmit", prompt="fix the login bug", prompt_id="p1")
        conn = db.connect()
        task = tasks.active_for_session(conn, SID)
        tasks.record_prompt(conn, task, SID, "fix the login bug", prompt_id="p1")   # the second registration
        now = time.time()
        e.append(e.main(), user(now, "fix the login bug", prompt_id="p1"), assistant(now + 1, "m1", "r1", out=1000))
        e.hook("Stop")
        rep = report.summary_for(conn, tasks.get(conn, task["id"]))
        self.assertEqual(len(rep["prompts"]), 1)
        self.assertEqual(sum(p["total"] for p in rep["prompts"]), rep["totals"]["total"])
        self.assertEqual(rep["stats"]["prompts"], 1)
        self.assertEqual(rep["task"]["prompt_count"], 1)
        self.assertIn("prompts  1 ·", fmt.render_report(rep))

    def test_calls_without_a_prompt_id_are_unattributed_not_zero_turns(self):
        from tally import db, fmt, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 300, "old"))
        e.hook("SessionStart", source="startup")
        e.hook("UserPromptSubmit", prompt="/tally:start demo")
        t = time.time()
        e.hook("UserPromptSubmit", prompt="look around", prompt_id="p1")
        e.append(e.main(), user(t, "look around", prompt_id="p1"), assistant(t + 1, "a1", "r1"))
        sub = e.sdir() / "subagents"
        e.meta(sub / "agent-ag1.meta.json", {"agentType": "Explore", "description": "look"})
        e.append(sub / "agent-ag1.jsonl", assistant(t + 2, "s1", "rs1", agent_id="ag1", sidechain=True, out=7))
        e.hook("Stop")
        conn = db.connect()
        rep = report.build(conn, tasks.active_for_session(conn, SID))
        auto = rep["stats"]["automatic"]
        self.assertEqual((auto["turns"], auto["output"], auto["label"]), (0, 7, "unattributed"))
        text = fmt.render_stats(rep)
        self.assertIn("unattributed", text)
        self.assertNotIn("automatic turns (0)", text)
        with mock.patch.object(fmt, "APP", True):
            app = fmt.for_app(fmt.render_stats(rep))
        self.assertRegex(app, r"\nunattributed · [\d.k]+ tokens · 7 output ·")
        self.assertEqual(fmt.auto_label({"turns": 0}), "unattributed")              # snapshots before the label
        self.assertEqual(fmt.auto_label({"turns": 2}), "automatic turns (2)")


class Formatting(Base):
    def test_sparkline_keeps_empty_days_in_the_app(self):
        from tally import fmt
        z = {"total": 0, "cost": 0.0, "calls": 0}
        daily = [{"day": "d%d" % i, "total": t, "cost": 0.0, "output": 0}
                 for i, t in enumerate([0, 0, 0, 5000, 0, 0, 0, 0, 9000, 0, 0, 100, 0, 0])]
        st = {"project": "web", "mode": "track", "sessions_read": True, "last_30d": dict(z, total=14100),
              "last_7d": z, "tracked": dict(z, total=14100), "sessions": 3, "by_model": [],
              "tasks": {"count": 0, "active": 0, "tokens": 0, "recent": []},
              "todos": {"todo": 0, "doing": 0, "blocked": 0, "done": 0}, "daily": daily,
              "not_tracked": {"sessions": 0, "size": 0, "last_activity": None}}
        self.assertIn("\n  14 days      ▄    █  ▁    (peak 9.0k)\n", fmt.render_project(st))     # terminal: as before
        with mock.patch.object(fmt, "APP", True):
            app = fmt.for_app(fmt.render_project(st))
        self.assertIn("\n14 days: ···▄····█··▁·· (peak 9.0k)\n", app)

    def test_partial_cost_is_kept_in_aggregates(self):
        from tally import db, export, fmt, report, sessions
        conn, task = self.task_with_one_prompt(model="mystery-model-9")
        rep = report.summary_for(conn, task)
        self.assertTrue(rep["totals"]["cost_partial"])
        self.assertIn("n/a per prompt", fmt.render_report(rep))
        # the hidden rows of a long tree
        d = {"total": 100, "output": 10, "cost": 0.5, "cost_partial": True, "calls": 1}
        kids = [{"kind": "subagent", "label": "x%d" % i, "agent_type": "Explore", "description": "x%d" % i,
                 "own": d, "rollup": d, "duration_s": 1, "children": []} for i in range(10)]
        lines = fmt.render_tree([{"kind": "main", "label": "main session", "own": d, "rollup": d, "duration_s": 1,
                                  "children": kids}])
        self.assertTrue(lines[-1].rstrip().endswith("$1.00+"), lines[-1])
        with mock.patch.object(fmt, "APP", True):
            self.assertTrue(fmt.render_tree([{"kind": "main", "label": "main session", "own": d, "rollup": d,
                                              "duration_s": 1, "children": kids}])[-1].endswith("· $1.00+"))
        # the markdown total of the tasks
        total = [line for line in export.export("tasks", "md", conn=conn)[0].split("\n") if "**Total**" in line][0]
        self.assertTrue(total.endswith("| n/a |"), total)
        # the sessions header
        now = time.time()
        with db.transaction(conn):
            conn.execute("INSERT OR IGNORE INTO added_sessions(session_id, added_at) VALUES(?,?)", ("s-partial", now))
        idx = sessions.connect()
        try:
            summ = {"first_ts": now - 100, "last_ts": now - 10, "wall_s": 90.0, "active_s": 10.0, "prompts": 1,
                    "automatic_turns": 0, "agents": 0, "unlogged": None, "models": [],
                    "totals": {"total": 100, "input": 10, "output": 10, "cache_read": 80, "cache_write": 0,
                               "calls": 1, "web_search": 0, "cost": 0.5, "cost_partial": True}}
            with db.transaction(idx):
                idx.execute("INSERT INTO indexed(session_id, agent, path, cwd, project, title, size, mtime, "
                            "summary_json, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            ("s-partial", "claude", "/nowhere.jsonl", "/Users/me/proj", None, "t", 1, now,
                             json.dumps(summ), now))
            ov = sessions.overview(30, conn=idx)
        finally:
            idx.close()
        self.assertTrue(ov["totals"]["cost_partial"])
        self.assertIn("· $0.500+ ·", fmt.render_sessions(ov).split("\n")[0])

    def test_depth_note_of_a_report_recorded_at_a_lower_depth(self):
        from tally import db, fmt, report, settings, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 500, "old"))
        e.hook("SessionStart", source="startup")
        settings.set_value("log.depth", "summary")                      # the default before 0.11.7
        e.hook("UserPromptSubmit", prompt="/tally:start demo")
        t = time.time()
        e.hook("UserPromptSubmit", prompt="one", prompt_id="p1")
        e.append(e.main(), user(t, "one", prompt_id="p1"),
                 assistant(t + 1, "a1", "r1", content=[{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}}]))
        e.hook("Stop")
        conn = db.connect()
        live = fmt.render_stats(report.summary_for(conn, tasks.active_for_session(conn, SID)))
        self.assertIn("tool statistics are off (log.depth = summary); /tally:settings log.depth calls", live)
        self.assertIn("per-prompt breakdown is off (log.depth = summary); /tally:settings log.depth prompts", live)
        e.hook("UserPromptSubmit", prompt="/tally:end")
        settings.set_value("log.depth", "calls")
        text = fmt.render_stats(report.summary_for(conn, tasks.get(conn, 1)))
        self.assertIn("tool statistics are off (recorded at log.depth = summary while the task ran)", text)
        self.assertIn("per-prompt breakdown is off (recorded at log.depth = summary while the task ran)", text)
        self.assertNotIn("/tally:settings", text)

    def test_plurals(self):
        from tally import export, fmt, report
        conn, task = self.task_with_one_prompt()
        rep = report.summary_for(conn, task)
        rep["totals"]["web_search"] = 1
        rep["stats"]["automatic_turns"] = 1
        rep["stats"]["tool_calls"], rep["stats"]["tool_errors"] = 1, 1
        rep["stats"]["tools"] = [{"tool": "Bash", "calls": 1, "errors": 1}] + [
            {"tool": "T%02d" % i, "calls": 1, "errors": 0} for i in range(12)]
        text = fmt.render_report(rep) + "\n" + fmt.render_stats(rep)
        for good in ("(1 turn)", "· 1 API call ·", "· 1 web search", "· +1 automatic turn\n", "1 prompt · 1 automatic turn",
                     "· 1 model\n", "1 tool call · 1 error\n", "  1 error", "… 1 more tool\n"):
            self.assertIn(good, text + "\n")
        for bad in ("1 turns", "1 API calls", "1 web searches", "1 automatic turns", "1 prompts", "1 models",
                    "1 subagents", "1 tool calls", "1 errors", "1 more tools"):
            self.assertNotIn(bad, text)
        self.assertEqual(fmt.node_label({"kind": "workflow", "label": "w", "agent_count": 1}), "workflow · w (1 agent)")
        rep["by_type"] = [{"agent_type": "main", "agents": 1, "total": 10, "cost": 0.0},
                          {"agent_type": "Explore", "agents": 1, "total": 10, "cost": 0.0}]
        self.assertIn("\n    Explore                       1 agent         10     $0.00", fmt.render_report(rep))  # same columns
        md = export.report_md(rep)
        self.assertIn(", 1 turn) |", md)
        self.assertIn("· 1 API call |", md)
        ov = {"totals": {"sessions": 1, "total": 10, "cost": 0.0, "prompts": 1, "active_s": 1}, "days": 7,
              "by_agent": [], "sessions": [], "not_tracked": {}}
        self.assertIn(" · 1 prompt · ", fmt.render_sessions(ov))

    def test_token_counts_round_into_the_next_unit(self):
        from tally import fmt
        self.assertEqual(fmt.tokens(999_949), "999.9k")
        self.assertEqual(fmt.tokens(999_950), "1.00M")
        self.assertEqual(fmt.tokens(1_234_567), "1.23M")
        self.assertEqual(fmt.tokens(999_994_999), "999.99M")
        self.assertEqual(fmt.tokens(999_995_000), "1.00B")

    def test_app_tool_line_has_one_colon(self):
        from tally import fmt, report
        conn, task = self.task_with_one_prompt()
        rep = report.summary_for(conn, task)
        rep["stats"]["tools"] = [{"tool": "mcp__claude_ai_Firecrawl__firecrawl_scrape", "calls": 1, "errors": 0}]
        with mock.patch.object(fmt, "APP", True):
            app = fmt.for_app(fmt.render_stats(rep))
        self.assertIn("\nFirecrawl: firecrawl_scrape · 1× ", app)
        self.assertIn("    Firecrawl: firecrawl_scrape ", fmt.render_stats(rep))       # terminal: a column


class Exports(Base):
    def test_file_names_are_ascii(self):
        from tally import db, export, tasks
        conn = db.connect()
        tasks.start(conn, SID, name="Oprava účtovníctva: ťažké ľahké ňuňo", now=time.time() - 100)
        name = export.export("task", "md", ref="1", conn=conn)[1]
        self.assertTrue(name.startswith("tally-task-1-oprava-uctovnictva-tazke-lahke-nuno-"), name)
        name.encode("latin-1")                                                  # fits an HTTP header
        self.assertEqual(export._slug("Fix login #2 (API)"), "fix-login-2-api")    # ASCII: as before
        self.assertEqual(export._slug("修正"), "")
        with db.transaction(conn):
            conn.execute("UPDATE tasks SET name = '修正' WHERE id = 1")
        self.assertRegex(export.export("task", "md", ref="1", conn=conn)[1], r"^tally-task-1-\d{4}-\d\d-\d\d\.md$")

    def test_text_lines_stay_one_line(self):
        from tally import export
        rep = {"task": {"id": 3, "name": "Fix\nlogin", "project": "web", "started_at": _local(2026, 10, 1, 10, 0),
                        "ended_at": _local(2026, 10, 1, 11, 30), "end_note": "wrapped up\nsecond  line"},
               "time": {"active_s": 60, "wall_s": 5400}, "totals": {"total": 10, "cost": 0.1}}
        self.assertEqual(export.task_line(rep), "Fix login (#3) · web · 2026-10-01 10:00–11:30 · active 1m 00s · "
                                                "wall 1h 30m · 10 tokens · $0.100 · wrapped up second line")
        srep = {"session": {"title": "night\nshift", "agent": "claude", "session_id": "abc", "project": "web"},
                "task": {"started_at": _local(2026, 10, 1, 10, 0), "ended_at": _local(2026, 10, 2, 9, 1)},
                "time": {"active_s": 60, "wall_s": 0}, "totals": {"total": 10, "cost": 0.1}, "unlogged": None}
        self.assertEqual(export.session_line(srep), "night shift · Claude Code · web · 2026-10-01 10:00–2026-10-02 09:01"
                                                    " · active 1m 00s · 10 tokens · $0.100")
        srep["task"]["ended_at"] = _local(2026, 10, 1, 12, 0)
        self.assertIn("· 2026-10-01 10:00–12:00 ·", export.session_line(srep))

    def test_csv_text_cells_are_not_formulas(self):
        from tally import db, export, tasks
        conn = db.connect()
        task, _ = tasks.start(conn, SID, name="=HYPERLINK(\"http://x\")", now=time.time() - 100)
        tasks.end(conn, task["id"], note="-rf everything")
        rows = list(csv.DictReader(io.StringIO(export.export("tasks", "csv", conn=conn)[0])))
        self.assertEqual(rows[0]["name"], "'=HYPERLINK(\"http://x\")")
        self.assertEqual(rows[0]["note"], "'-rf everything")
        self.assertEqual(rows[0]["total_tokens"], "0")                          # numbers stay numbers
        self.assertEqual(export._csv(["a"], [["@x"], ["+1"], [-1], ["ok"]]), "a\r\n'@x\r\n'+1\r\n-1\r\nok\r\n")


if __name__ == "__main__":
    unittest.main()
