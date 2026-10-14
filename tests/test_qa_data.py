"""QA findings on the data layer: concurrent starts and attaches, duplicate
prompts, config writes, history skipping, todos, the self-healing schema and
the /clear carry-over.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock

try:
    from test_tally import Env, user, assistant, SID, SID2, PLUGIN
except ImportError:                                   # run from the repository root
    from tests.test_tally import Env, user, assistant, SID, SID2, PLUGIN

CWD = "/Users/me/proj"


def slowed(module, names, delay=0.3):
    """Patch the named functions of a module (those it has) to wait first: widens the window
    between a check and the write that depends on it, as a busy disk or a held lock would."""
    patches = []
    for name in names:
        orig = getattr(module, name, None)
        if orig is None:
            continue

        def slow(*a, _orig=orig, **k):
            time.sleep(delay)
            return _orig(*a, **k)
        patches.append(unittest.mock.patch.object(module, name, slow))
    return patches


def in_threads(*fns):
    errors = []

    def run(fn):
        try:
            fn()
        except Exception as e:                        # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=run, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def one(self, sql, *args):
        from tally import db
        conn = db.connect()
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    def hook_twice(self, event, payload, sid=SID):
        """The same hook run by two processes at once (two registrations of the plugin)."""
        tally = [sys.executable, str(PLUGIN / "bin" / "tally"), "hook", event]
        data = json.dumps(dict({"session_id": sid, "transcript_path": str(self.env.main(sid)), "cwd": CWD,
                                "hook_event_name": event}, **payload))
        procs = [subprocess.Popen(tally, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=os.environ) for _ in range(2)]
        for p in procs:                                # both are up before either gets its payload
            p.stdin.write(data.encode())
            p.stdin.close()
        for p in procs:
            p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()


# ---------------------------------------------------------------- 1. tasks.start is atomic

class AtomicStart(Base):
    def test_auto_start_with_two_hook_registrations_starts_one_task(self):
        """auto_start: session and the hooks registered twice: the first prompt starts one task (the
        twin used to end it and start a second one)."""
        from tally import paths
        paths.save_config({"auto_start": "session", "updates": {"check": False}})
        for i in range(3):
            sid = "auto%d%s" % (i, SID[5:])
            self.env.append(self.env.main(sid), user(time.time() - 5))
            self.hook_twice("UserPromptSubmit", {"prompt": "Build the exporter %d" % i, "prompt_id": "p%d" % i},
                            sid=sid)
            self.assertEqual(self.one("SELECT COUNT(*) FROM tasks WHERE origin_session = ?", sid), 1, "round %d" % i)
            self.assertEqual(self.one("SELECT COUNT(*) FROM prompts p JOIN tasks t ON t.id = p.task_id "
                                      "WHERE t.origin_session = ?", sid), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks WHERE end_note LIKE 'auto-ended%'"), 0)
        self.assertFalse(paths.error_log_path().exists())

    def test_auto_start_returns_none_when_the_session_already_had_a_task(self):
        from tally import db, tasks
        conn = db.connect()
        task, _ = tasks.start(conn, SID, name="by hand", cwd=CWD)
        tasks.end(conn, task["id"])
        self.assertIsNone(tasks.start(conn, SID, initial_prompt="later prompt", cwd=CWD, created_by="auto"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 1)

    def test_two_starts_at_once_leave_one_active_task(self):
        """/tally:start A and /tally:start B arrive together (the hook and the fallback of two versions):
        the session ends with one active task and the other one ended, never two counting."""
        from tally import db, paths, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        patches = slowed(paths, ["load_config"])         # read between "is a task running?" and the insert
        for p in patches:
            p.start()
        try:
            in_threads(lambda: tasks.start(db.connect(), SID, name="A", cwd=CWD),
                       lambda: tasks.start(db.connect(), SID, name="B", cwd=CWD))
        finally:
            for p in patches:
                p.stop()
        conn = db.connect()
        active = conn.execute("SELECT t.name FROM tasks t JOIN task_sessions s ON s.task_id = t.id "
                              "WHERE s.session_id = ? AND s.detached_at IS NULL AND t.status = 'active'",
                              (SID,)).fetchall()
        self.assertEqual(len(active), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks WHERE status = 'ended' AND end_note LIKE 'auto-ended%'"), 1)

    def test_the_same_start_twice_gives_the_same_answer(self):
        """A start run twice a moment apart (no run_once guard: two versions) returns the task the
        first one started, and the task that first one ended."""
        from tally import db, tasks
        conn = db.connect()
        first, _ = tasks.start(conn, SID, name="Parser", cwd=CWD)
        second, ended = tasks.start(conn, SID, name="Lexer", cwd=CWD)
        self.assertEqual(ended["id"], first["id"])
        again, ended_again = tasks.start(conn, SID, name="Lexer", cwd=CWD)
        self.assertEqual(again["id"], second["id"])
        self.assertEqual(ended_again["id"], first["id"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 2)
        self.assertEqual(tasks.get(conn, second["id"])["status"], "active")
        with unittest.mock.patch.object(tasks, "TWIN_S", 0):        # later: a new task of the same name
            third, ended3 = tasks.start(conn, SID, name="Lexer", cwd=CWD)
        self.assertNotEqual(third["id"], second["id"])
        self.assertEqual(ended3["id"], second["id"])

    def test_two_prompts_of_the_same_name_are_two_tasks(self):
        from tally import db, tasks
        conn = db.connect()
        a, _ = tasks.start(conn, SID, initial_prompt="fix it", cwd=CWD, created_by="new", prompt_id="p1")
        b, ended = tasks.start(conn, SID, initial_prompt="fix it", cwd=CWD, created_by="new", prompt_id="p2")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(ended["id"], a["id"])


# ---------------------------------------------------------------- 2. prompts stored once

class PromptsOnce(Base):
    def test_a_prompt_seen_by_two_hooks_is_stored_once(self):
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        e.hook("UserPromptSubmit", prompt="/tally:start dup")
        for i in range(3):
            self.hook_twice("UserPromptSubmit", {"prompt": "prompt %d" % i, "prompt_id": "pid-%d" % i})
        self.assertEqual(self.one("SELECT COUNT(*) FROM prompts WHERE task_id = 1"), 3)

    def test_record_prompt_keeps_prompts_without_an_id(self):
        from tally import db, tasks
        conn = db.connect()
        task, _ = tasks.start(conn, SID, name="t", cwd=CWD)
        tasks.record_prompt(conn, task, SID, "a", prompt_id="x")
        tasks.record_prompt(conn, task, SID, "a", prompt_id="x")
        tasks.record_prompt(conn, task, SID, "b")                   # older agents: no prompt id
        tasks.record_prompt(conn, task, SID, "b")
        self.assertEqual(self.one("SELECT COUNT(*) FROM prompts WHERE task_id = ?", task["id"]), 3)


# ---------------------------------------------------------------- 3. one interval per attach

class OneInterval(Base):
    def test_two_carry_overs_at_once_open_one_interval_and_count_once(self):
        from tally import db, ingest, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        e.append(e.main(SID2), user(time.time() - 5))
        conn = db.connect()
        with db.transaction(conn):
            tasks.touch_session(conn, SID, str(e.main()), CWD, 4242, project="proj")
        task, _ = tasks.start(conn, SID, name="parser", cwd=CWD, transcript_path=str(e.main()))
        patches = slowed(tasks, ["active_for_session", "_active_in_session"])
        for p in patches:
            p.start()
        try:
            in_threads(*[lambda: tasks.carry_over_after_clear(db.connect(), SID2, 4242, CWD, str(e.main(SID2)))
                         for _ in range(2)])
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(self.one("SELECT COUNT(*) FROM task_sessions WHERE session_id = ? AND detached_at IS NULL",
                                  SID2), 1)
        time.sleep(0.01)
        e.append(e.main(SID2), assistant(time.time(), "m1", "r1", out=1000))
        ingest.ingest_session(conn, SID2, str(e.main(SID2)))
        self.assertEqual(report.build(conn, tasks.get(conn, task["id"]))["totals"]["output"], 1000)

    def test_overlapping_intervals_already_stored_count_once(self):
        """Databases written before the fix can hold two open intervals of one session: the report
        merges them instead of counting the usage in both."""
        from tally import db, ingest, report, tasks
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        conn = db.connect()
        task, _ = tasks.start(conn, SID, name="t", cwd=CWD, transcript_path=str(e.main()))
        now = time.time()
        with db.transaction(conn):              # what a raced attach left behind
            conn.execute("INSERT INTO task_sessions(task_id, session_id, attached_at, reason) VALUES(?,?,?,?)",
                         (task["id"], SID, now, "attach"))
            conn.execute("INSERT INTO task_sessions(task_id, session_id, attached_at, detached_at, reason) "
                         "VALUES(?,?,?,?,?)", (task["id"], SID2, now, now + 1, "attach"))
            conn.execute("INSERT INTO task_sessions(task_id, session_id, attached_at, detached_at, reason) "
                         "VALUES(?,?,?,?,?)", (task["id"], SID2, now + 0.5, now + 2, "attach"))
        time.sleep(0.01)
        e.append(e.main(), assistant(time.time(), "m1", "r1", out=7))
        ingest.ingest_session(conn, SID, str(e.main()))
        task = tasks.get(conn, task["id"])
        self.assertEqual(report.build(conn, task)["totals"]["output"], 7)
        windows = tasks.task_windows(conn, task)
        self.assertEqual([w[0] for w in windows].count(SID), 1)
        self.assertEqual([(w[1], w[2]) for w in windows if w[0] == SID2], [(now, now + 2)])


# ---------------------------------------------------------------- 4. config.json

class Config(Base):
    def test_readers_never_see_a_half_written_config(self):
        from tally import paths, settings
        settings.set_value("log.prompts", "off")
        stop = []
        bad = []

        def writer():
            i = 0
            while not stop:
                settings.set_value("log.depth", ("summary", "calls")[i % 2])
                i += 1
        t = threading.Thread(target=writer)
        t.start()
        try:
            end = time.time() + 0.5
            reads = 0
            while time.time() < end:
                reads += 1
                if paths.load_config()["log"]["prompts"] != "off":
                    bad.append(reads)
        finally:
            stop.append(True)
            t.join()
        self.assertEqual(bad, [], "%d of %d reads got the defaults" % (len(bad), reads))

    def test_concurrent_writers_keep_each_others_settings(self):
        from tally import paths, settings
        settings.set_value("log.prompts", "off")
        settings.set_value("dashboard_port", 9999)
        in_threads(lambda: [settings.set_value("log.depth", v) for v in ("summary", "calls") * 40],
                   lambda: [settings.set_value("auto_start", v) for v in ("session", "off") * 40])
        raw = json.loads(paths.config_path().read_text())
        self.assertEqual(raw, {"log": {"prompts": "off", "depth": "calls"}, "dashboard_port": 9999, "auto_start": "off"})

    def test_the_file_mode_is_kept(self):
        from tally import paths, settings
        settings.set_value("todos.reminder", "off")
        os.chmod(str(paths.config_path()), 0o600)
        settings.set_value("todos.reminder", "on")
        self.assertEqual(stat.S_IMODE(os.stat(str(paths.config_path())).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(str(paths.home())) if f.endswith(".tmp")], [])

    def test_an_unreadable_config_is_not_overwritten(self):
        from tally import paths, settings
        paths.home().mkdir(parents=True, exist_ok=True)
        broken = '{"log": {"prompts": "off"}, "pricing": {"my-model": {"input": 3, "output": 15}},}\n'
        paths.config_path().write_text(broken)
        with self.assertRaises(ValueError) as cm:
            settings.set_value("todos.reminder", "off")
        self.assertIn("not valid JSON", str(cm.exception))
        self.assertEqual(paths.config_path().read_text(), broken)
        reply = self.env.hook("UserPromptSubmit", prompt="/tally:settings todos.reminder off")
        self.assertIn("not valid JSON", reply["reason"])
        self.assertEqual(paths.config_path().read_text(), broken)
        paths.config_path().write_text("[1, 2]\n")
        with self.assertRaises(ValueError):
            settings.set_value("todos.reminder", "off")
        self.assertEqual(paths.config_path().read_text(), "[1, 2]\n")
        paths.config_path().write_text("")                        # empty: nothing to lose
        settings.set_value("todos.reminder", "off")
        self.assertEqual(json.loads(paths.config_path().read_text()), {"todos": {"reminder": False}})

    def test_an_invalid_value_reads_as_the_default(self):
        from tally import paths, settings
        paths.save_config({"todos": {"reminder_limit": "five"}, "log": {"prompts": "maybe", "depth": "Calls"},
                           "dashboard_port": 70000, "dashboard_idle_minutes": "30", "updates": {"check": "off"}})
        self.assertEqual(settings.get("todos.reminder_limit"), 5)
        self.assertEqual(settings.get("log.prompts"), "full")
        self.assertEqual(settings.get("log.depth"), "calls")
        self.assertEqual(settings.get("dashboard_port"), 8765)
        self.assertEqual(settings.get("dashboard_idle_minutes"), 30)
        self.assertIs(settings.get("updates.check"), False)
        self.assertIsNone(settings.get("todos.reminder", {}))         # not set at all
        self.assertEqual(settings.get("pricing"), {})                  # not a setting of SCHEMA: as stored

    def test_the_session_reminder_survives_a_word_for_a_number(self):
        from tally import db, paths, todos
        paths.save_config({"todos": {"reminder_limit": "five"}, "updates": {"check": False}})
        conn = db.connect()
        todos.add(conn, "Ship the release", project="proj")
        out = self.env.hook("SessionStart", source="startup")
        self.assertIn("Ship the release", out["systemMessage"])
        self.assertFalse(paths.error_log_path().exists())

    def test_dashboard_port_is_a_port(self):
        from tally import settings
        for bad in (0, 65536, -1):
            with self.assertRaises(ValueError):
                settings.coerce("dashboard_port", bad)
        self.assertEqual(settings.coerce("dashboard_port", "8770"), 8770)
        self.assertEqual(settings.coerce("dashboard_port", 65535), 65535)


# ---------------------------------------------------------------- 5. history before a task is skipped

class SkipHistory(Base):
    def history(self, n=30):
        t = time.time() - 100
        for i in range(n):
            self.env.append(self.env.main(), user(t + i, "old %d" % i, prompt_id="old-%d" % i),
                            assistant(t + i + 0.5, "old_m%d" % i, "old_r%d" % i, out=1000, content=[
                                {"type": "tool_use", "id": "old_tool_%d" % i, "name": "Bash", "input": {}}]))

    def test_a_task_start_does_not_parse_the_history(self):
        from tally import db, report, tasks
        e = self.env
        self.history()
        e.hook("UserPromptSubmit", prompt="/tally:start fresh")
        conn = db.connect()
        self.assertEqual(self.one("SELECT COUNT(*) FROM usage"), 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tool_calls"), 0)
        self.assertEqual(self.one("SELECT offset FROM files WHERE path = ?", str(e.main())),
                         e.main().stat().st_size)
        # what happens after the start counts, with its prompt and its tool calls
        e.hook("UserPromptSubmit", prompt="Now refactor", prompt_id="new-1")
        time.sleep(0.01)
        t = time.time()
        e.append(e.main(), user(t, "Now refactor", prompt_id="new-1"),
                 assistant(t + 0.001, "new_m1", "new_r1", out=5, content=[
                     {"type": "tool_use", "id": "new_tool_1", "name": "Read", "input": {}}]),
                 assistant(t + 0.002, "new_m2", "new_r2", out=6))
        e.hook("Stop")
        rep = report.build(conn, tasks.get(conn, 1))
        self.assertEqual(rep["totals"]["output"], 11)
        self.assertEqual(rep["totals"]["calls"], 2)
        self.assertEqual([r[0] for r in conn.execute("SELECT DISTINCT prompt_id FROM usage")], ["new-1"])
        self.assertEqual([r[0] for r in conn.execute("SELECT tool FROM tool_calls")], ["Read"])
        prompts = {p["prompt"]: p for p in rep["prompts"]}
        self.assertEqual(prompts["Now refactor"]["output"], 11)

    def test_a_tracked_project_still_reads_the_history(self):
        from tally import db, projects
        e = self.env
        self.history(3)
        e.hook("SessionStart", source="startup")
        conn = db.connect()
        with db.transaction(conn):
            projects.set_mode(conn, "proj", "track")
        e.hook("UserPromptSubmit", prompt="/tally:start tracked")
        self.assertEqual(self.one("SELECT COUNT(*) FROM usage"), 3)


# ---------------------------------------------------------------- 6. todos

class Todos(Base):
    def test_deleting_a_plan_unlinks_tasks_of_its_steps(self):
        from tally import commands, db, tasks, todos
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        conn = db.connect()
        plan = todos.add(conn, "Plan", project="proj")
        step = todos.add(conn, "Step 1", project="proj", parent_id=plan["id"])
        sub = todos.add(conn, "Step 1.1", project="proj", parent_id=step["id"])
        _, task, _ = todos.take(conn, sub["id"], SID, cwd=CWD, transcript_path=str(e.main()))
        todos.delete(conn, plan["id"])
        self.assertIsNone(todos.get(conn, sub["id"]))
        self.assertIsNone(tasks.get(conn, task["id"])["todo_id"])
        out = commands.run(conn, SID, "done", "", cwd=CWD).text
        self.assertNotIn("not found", out)

    def test_deleting_an_active_task_releases_its_todo(self):
        from tally import db, tasks, todos
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        conn = db.connect()
        td = todos.add(conn, "Other", project="proj")
        _, task, _ = todos.take(conn, td["id"], SID, cwd=CWD, transcript_path=str(e.main()))
        self.assertEqual(todos.get(conn, td["id"])["status"], "doing")
        tasks.delete(conn, task["id"])
        self.assertEqual(todos.get(conn, td["id"])["status"], "todo")

    def test_a_different_todo_with_the_same_title_is_added(self):
        from tally import commands, db
        conn = db.connect()
        commands.run(conn, SID, "todo", "Fix login !!! due:fri", cwd=CWD)
        commands.run(conn, SID, "todo", "Fix login -- only Safari is broken", cwd=CWD)
        rows = conn.execute("SELECT priority, notes FROM todos WHERE title = 'Fix login' ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(3, None), (0, "only Safari is broken")])

    def test_the_same_todo_twice_is_one_even_with_a_relative_due_date(self):
        from tally import db, todos
        conn = db.connect()
        due = time.time() + 4 * 3600
        a = todos.add(conn, "Call back", project="proj", priority=2, due_at=due, notes="re: invoice", once=True)
        b = todos.add(conn, "Call back", project="proj", priority=2, due_at=due + 0.02, notes="re: invoice", once=True)
        self.assertEqual(a["id"], b["id"])

    def test_priority_is_0_to_3(self):
        from tally import db, todos
        conn = db.connect()
        t = todos.add(conn, "x", project="proj")
        for bad in ("x", 4, -1, 2.5, "high"):
            with self.assertRaises(ValueError):
                todos.update(conn, t["id"], priority=bad)
        self.assertEqual(todos.update(conn, t["id"], priority="2")["priority"], 2)
        self.assertEqual(todos.update(conn, t["id"], priority=3)["priority"], 3)
        with self.assertRaises(ValueError):
            todos.add(conn, "y", project="proj", priority=7)
        self.assertEqual(todos.list_todos(conn, project="proj")[0]["priority"], 3)


# ---------------------------------------------------------------- 7. the schema heals itself

class SelfHealingSchema(Base):
    def make_db(self, schema, version=None):
        from tally import db, paths
        path = paths.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(path))
        raw.executescript(schema)
        raw.execute("PRAGMA user_version = %d" % (db.SCHEMA_VERSION if version is None else version))
        raw.commit()
        raw.close()

    def columns(self, table):
        from tally import db
        return {r[1]: r for r in db.connect().execute("PRAGMA table_info(%s)" % table)}

    def test_a_missing_table_is_created_with_its_indexes(self):
        from tally import db
        conn = db.connect()
        conn.execute("DROP TABLE tool_calls")
        conn.execute("DROP TABLE todos")
        conn.close()
        conn = db.connect()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertTrue({"tool_calls", "tool_calls_session_ts", "todos", "todos_parent", "todos_source_ref"} <= names)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_columns_missing_at_the_current_version_are_added(self):
        """Columns that never were in the hand-written list of added columns: a build of the same version
        without them (or with them under another name) must not break every ingest."""
        import re
        from tally import db, ingest, report, tasks
        schema = db.SCHEMA
        for pattern, repl in ((r"\n    speed +TEXT,", ""),                                # usage
                              (r"\n    input +INTEGER NOT NULL DEFAULT 0,", "\n    inp INTEGER,"),
                              (r"\n    duration_ms +INTEGER NOT NULL,", ""),             # turns
                              (r"\n    created_by +TEXT, +--[^\n]*", "")):              # tasks
            schema, n = re.subn(pattern, repl, schema)
            self.assertEqual(n, 1, pattern)
        self.make_db(schema)
        cols = self.columns("usage")
        self.assertIn("speed", cols)
        self.assertIn("inp", cols)                                    # never dropped or renamed
        self.assertEqual((cols["input"][3], cols["input"][4]), (1, "0"))   # NOT NULL DEFAULT 0
        self.assertEqual(self.columns("turns")["duration_ms"][3], 0)        # NOT NULL without a default: nullable
        self.assertIn("created_by", self.columns("tasks"))
        e = self.env
        e.append(e.main(), user(time.time() - 5))
        conn = db.connect()
        task, _ = tasks.start(conn, SID, name="t", cwd=CWD, transcript_path=str(e.main()))
        time.sleep(0.01)
        e.append(e.main(), assistant(time.time(), "m1", "r1", out=9))
        ingest.ingest_session(conn, SID, str(e.main()))
        self.assertEqual(report.build(conn, tasks.get(conn, task["id"]))["totals"]["output"], 9)

    def test_unknown_tables_and_columns_are_left_alone(self):
        from tally import db, tasks
        conn = db.connect()
        conn.execute("ALTER TABLE tasks ADD COLUMN mystery TEXT DEFAULT 'kept'")
        conn.execute("CREATE TABLE extra (x INTEGER)")
        conn.execute("INSERT INTO extra VALUES (1)")
        conn.execute("PRAGMA user_version = %d" % (db.SCHEMA_VERSION + 5))     # a newer Tally wrote it
        conn.execute("ALTER TABLE files RENAME COLUMN tools_offset TO tools_off")
        conn.close()
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT x FROM extra").fetchone()[0], 1)
        self.assertTrue({"mystery", "name"} <= set(self.columns("tasks")))
        self.assertTrue({"tools_off", "tools_offset"} <= set(self.columns("files")))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION + 5)   # not lowered
        task, _ = tasks.start(conn, SID, name="still works", cwd=CWD)
        self.assertEqual(tasks.get(conn, task["id"])["mystery"], "kept")

    def test_concurrent_connects_heal_once(self):
        from tally import db
        conn = db.connect()
        conn.execute("DROP TABLE kv")
        conn.execute("ALTER TABLE usage RENAME COLUMN cost TO old_cost")
        conn.close()
        in_threads(*[lambda: db.connect().execute("SELECT cost FROM usage").fetchall() for _ in range(6)])
        self.assertEqual([c for c in self.columns("usage") if "cost" in c], ["old_cost", "cost"])
        self.assertIsNone(db.kv_get(db.connect(), "nothing"))

    def test_a_unique_index_over_repeated_values_does_not_block_connect(self):
        from tally import db
        conn = db.connect()
        conn.execute("DROP INDEX todos_source_ref")
        for _ in range(2):
            conn.execute("INSERT INTO todos(title, source_ref, created_at, updated_at) VALUES('x', 'plan::a', 0, 0)")
        conn.close()
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'todos_source_ref'").fetchone()[0], 1)
        self.assertFalse(any(db._schema_gaps(conn)))

    def test_a_healthy_database_is_not_written_on_connect(self):
        from tally import db
        db.connect().close()
        conn = db.connect()
        before = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.close()
        for _ in range(3):
            db.connect().close()
        self.assertEqual(db.connect().execute("PRAGMA schema_version").fetchone()[0], before)


# ---------------------------------------------------------------- 8. /clear carry-over by process id

class ClearCarryOver(Base):
    def test_a_reused_process_id_does_not_pick_up_a_forgotten_task(self):
        from tally import db, tasks
        conn = db.connect()
        with db.transaction(conn):
            tasks.touch_session(conn, SID, None, CWD, 4242, project="proj")
        tasks.start(conn, SID, name="forgotten", cwd=CWD)
        with db.transaction(conn):
            conn.execute("UPDATE sessions SET last_seen = ? WHERE session_id = ?", (time.time() - 7200, SID))
        self.assertIsNone(tasks.carry_over_after_clear(conn, SID2, 4242, CWD, None))
        self.assertIsNone(tasks.active_for_session(conn, SID2))
        # the session SessionEnd(reason=clear) named a moment ago still carries over
        task = tasks.carry_over_after_clear(conn, SID2, 4242, CWD, None, previous_session=SID)
        self.assertEqual(task["name"], "forgotten")

    def test_a_recent_session_of_the_process_carries_over(self):
        from tally import db, tasks
        conn = db.connect()
        with db.transaction(conn):
            tasks.touch_session(conn, SID, None, CWD, 4242, project="proj")
        tasks.start(conn, SID, name="current", cwd=CWD)
        self.assertEqual(tasks.carry_over_after_clear(conn, SID2, 4242, CWD, None)["name"], "current")


if __name__ == "__main__":
    unittest.main()
