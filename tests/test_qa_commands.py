"""Regression tests for the QA findings on commands, hooks and the command fallbacks.

Run:  python3 -m unittest discover -s tests -v
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock

try:
    from test_tally import PLUGIN, SID, SID2, Env, user
except ImportError:                                     # run as a package: python3 -m unittest tests.test_qa_commands
    from tests.test_tally import PLUGIN, SID, SID2, Env, user

SID3 = "33333333-4444-5555-6666-777777777777"
TALLY = [sys.executable, str(PLUGIN / "bin" / "tally")]


def cli(*args, stdin=None, cwd=None):
    return subprocess.run(TALLY + list(args), input=stdin, capture_output=True, text=True, env=os.environ,
                          timeout=60, cwd=cwd)


def fallback_block(cmd: str) -> str:
    """The `!` block of plugin/commands/<cmd>.md (it may span lines)."""
    text = (PLUGIN / "commands" / (cmd + ".md")).read_text()
    m = re.search(r"^!`(.*?)`$", text, re.S | re.M)
    assert m, cmd
    return m.group(1)


def run_fallback(cmd: str, arguments: str, cwd: str) -> str:
    """What Claude Code does with a command file: substitute the variables and $ARGUMENTS as text,
    end the `!` block at the first backtick, run it with bash."""
    text = (PLUGIN / "commands" / (cmd + ".md")).read_text()
    start = text.index("!`") + 2
    text = text[start:].replace("$ARGUMENTS", arguments)
    script = text[: text.index("`")]                    # a backtick in the arguments ends the block early
    script = script.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)).replace("${CLAUDE_SESSION_ID}", SID)
    script = script.replace("python3 ", sys.executable + " ", 1)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=os.environ, cwd=cwd, timeout=60)
    return r.stdout + r.stderr


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.append(self.env.main(), user(time.time() - 10, "hi", prompt_id="p0"))

    def tearDown(self):
        self.env.cleanup()

    def conn(self):
        from tally import db
        return db.connect()

    def one(self, sql, *args):
        c = self.conn()
        try:
            return c.execute(sql, args).fetchone()[0]
        finally:
            c.close()


# ------------------------------------------------------------------ 1. the command fallbacks

class Fallbacks(Base):
    def test_every_fallback_reads_its_arguments_from_a_quoted_heredoc(self):
        files = sorted(p.stem for p in (PLUGIN / "commands").glob("*.md") if "!`" in p.read_text())
        self.assertGreater(len(files), 10)
        for cmd in files:
            block = fallback_block(cmd)
            self.assertNotIn('"$ARGUMENTS"', block, cmd)
            self.assertIn(" --stdin %s 2>&1 <<'TALLY_ARGS' || true\n$ARGUMENTS\nTALLY_ARGS" % cmd, block, cmd)

    def test_the_shell_neither_expands_nor_runs_the_users_text(self):
        work = self.env.tmp / "proj"
        (work / ".git").mkdir(parents=True)
        marker = self.env.tmp / "PWNED"
        text = 'pay $HOME and $(touch %s) for a 27" monitor @proj' % marker
        out = run_fallback("todo", text, str(work))
        self.assertIn("Todo #1 added", out)
        self.assertFalse(marker.exists())
        self.assertEqual(self.one("SELECT title FROM todos WHERE id = 1"),
                         'pay $HOME and $(touch %s) for a 27" monitor' % marker)

    def test_a_backtick_truncates_the_text_but_runs_nothing(self):
        work = self.env.tmp / "proj"
        (work / ".git").mkdir(parents=True)
        marker = self.env.tmp / "PWNED2"
        out = run_fallback("todo", "remember `touch %s` before the release" % marker, str(work))
        self.assertIn("Todo #1 added", out)
        self.assertFalse(marker.exists())
        self.assertEqual(self.one("SELECT title FROM todos WHERE id = 1"), "remember")

    def test_slash_stdin(self):
        r = cli("slash", "--session", SID, "--cwd", "/Users/me/proj", "--stdin", "start", stdin="Fix  $PATH handling\n")
        self.assertIn("Task #1 started: Fix  $PATH handling", r.stdout)
        r = cli("slash", "--session", SID, "--cwd", "/Users/me/proj", "--stdin", "status", stdin="\n")
        self.assertIn("Task #1", r.stdout)
        self.assertNotIn("✗", r.stdout)

    def test_the_users_own_double_dash_is_kept(self):
        r = cli("slash", "--session", SID, "--cwd", "/Users/me/proj", "todo", "--", "--force flag ignores the lock")
        self.assertIn("Todo #1 added", r.stdout)
        self.assertEqual(self.one("SELECT title FROM todos WHERE id = 1"), "--force flag ignores the lock")
        cli("slash", "--session", SID2, "--cwd", "/Users/me/proj", "--stdin", "todo", stdin="--dry-run mode\n")
        self.assertEqual(self.one("SELECT title FROM todos WHERE id = 2"), "--dry-run mode")

    def test_a_fallback_with_shell_mangled_arguments_is_the_hooks_twin(self):
        e = self.env
        reply = e.hook("UserPromptSubmit", prompt="/tally:todo pay $20 invoice @proj")
        self.assertIn("Todo #1 added", reply["reason"])
        out = cli("slash", "--session", SID, "--cwd", "/Users/me/proj", "todo", "--", "pay 0 invoice @proj").stdout
        self.assertIn("Todo #1 added", out)
        self.assertEqual(self.one("SELECT COUNT(*) FROM todos"), 1)
        # the fallback first (its text cut at a backtick), then the hook
        out = cli("slash", "--session", SID, "--cwd", "/Users/me/proj", "start", "--", "check the").stdout
        self.assertIn("Task #1 started: check the", out)
        reply = e.hook("UserPromptSubmit", prompt="/tally:start check the `ls` output")
        self.assertIn("Task #1 started", reply["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 1)

    def test_pi_compares_the_arguments(self):
        r = cli("slash", "--json", "--agent", "pi", "--session", SID, "--cwd", "/Users/me/proj", "todo", "--", "A @proj")
        self.assertIn("Todo #1 added", json.loads(r.stdout)["text"])
        r = cli("slash", "--json", "--agent", "pi", "--session", SID, "--cwd", "/Users/me/proj", "todo", "--", "B @proj")
        self.assertIn("Todo #2 added", json.loads(r.stdout)["text"])

    def test_another_command_in_between_is_a_new_prompt(self):
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:start one")
        self.assertIn("ended", e.hook("UserPromptSubmit", prompt="/tally:end")["reason"])
        self.assertIn("resumed", e.hook("UserPromptSubmit", prompt="/tally:resume 1")["reason"])
        self.assertIn("ended", e.hook("UserPromptSubmit", prompt="/tally:end")["reason"])      # within 3 s
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id = 1"), "ended")


# ------------------------------------------------------------------ 2. run_once

class RunOnce(Base):
    def _run(self, cmd, args, result, delay=0.0, via="hook", raw=None, calls=None, out=None, key=None):
        from tally import commands, db

        def go():
            conn = db.connect()
            try:
                def run():
                    if calls is not None:
                        calls.append(key or cmd)
                    time.sleep(delay)
                    return commands.Result(result)
                extra = dict(k for k in (("via", via), ("raw", raw)) if k[1] not in (None, "hook"))
                res = commands.run_once(conn, SID, cmd, args, run, **extra)
                if out is not None:
                    out[key or cmd] = res.text
            finally:
                conn.close()
        t = threading.Thread(target=go)
        t.start()
        return t

    def test_a_slow_first_run_is_waited_for(self):
        calls, out = [], {}
        with unittest.mock.patch("tally.commands.ONCE_S", 0.3):
            a = self._run("start", "x", "first", delay=0.8, calls=calls, out=out, key="a")
            time.sleep(0.5)                                     # older than ONCE_S, still running
            b = self._run("start", "x", "second", calls=calls, out=out, key="b")
            a.join()
            b.join()
        self.assertEqual(calls, ["a"])
        self.assertEqual(out, {"a": "first", "b": "first"})

    def test_each_command_has_its_own_slot(self):
        calls, out = [], {}
        a = self._run("todo", "X", "todo X", delay=0.5, calls=calls, out=out, key="a")
        time.sleep(0.1)
        b = self._run("status", "", "status", delay=0.8, calls=calls, out=out, key="b")
        time.sleep(0.1)
        a2 = self._run("todo", "X", "todo X again", calls=calls, out=out, key="a2")
        a.join()
        a2.join()
        b2 = self._run("status", "", "status again", calls=calls, out=out, key="b2")    # after a finished
        b.join()
        b2.join()
        self.assertEqual(sorted(calls), ["a", "b"])
        self.assertEqual(out, {"a": "todo X", "a2": "todo X", "b": "status", "b2": "status"})

    def test_now_is_taken_when_the_hook_starts(self):
        from tally import commands, db
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:start X")
        real = db.connect

        def slow_connect(*a, **k):
            time.sleep(0.4)
            return real(*a, **k)
        with unittest.mock.patch("tally.commands.ONCE_S", 0.3), unittest.mock.patch("tally.db.connect", slow_connect):
            conn = real()
            with db.transaction(conn):                          # a twin that finished just before this prompt
                rec = json.loads(db.kv_get(conn, "once:%s:start" % SID))
                for c in rec["claims"]:
                    c["done_ts"] = time.time()
                db.kv_set(conn, "once:%s:start" % SID, json.dumps(rec))
            conn.close()
            self.assertIn("Task #1 started", e.hook("UserPromptSubmit", prompt="/tally:start X")["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 1)
        self.assertTrue(commands.ONCE_WAIT_S >= 50)


class OlderVersions(Base):
    """Records of the once-guard written by 0.10.1-0.11.5 (hash of the prompt, the hook's output) and by
    0.11.6-0.11.8 (normalised hash, a Result), both under once:<session>."""

    def put(self, value):
        from tally import db
        conn = self.conn()
        with db.transaction(conn):
            db.kv_set(conn, "once:" + SID, json.dumps(value))
        conn.close()

    def test_a_0_11_5_hook_ran_first(self):
        prompt = "/tally:todo Write the notes @proj"
        self.put({"hash": hashlib.sha1(prompt.encode()).hexdigest(), "ts": time.time(), "done": True,
                  "result": {"decision": "block", "reason": "OLD ANSWER", "suppressOriginalPrompt": True}})
        self.assertEqual(self.env.hook("UserPromptSubmit", prompt=prompt)["reason"], "OLD ANSWER")
        self.assertEqual(self.one("SELECT COUNT(*) FROM todos"), 0)

    def test_a_0_11_5_hook_handed_new_to_the_model(self):
        prompt = "/tally:new Refactor the reader"
        self.put({"hash": hashlib.sha1(prompt.encode()).hexdigest(), "ts": time.time(), "done": True, "result": None})
        self.assertIsNone(self.env.hook("UserPromptSubmit", prompt=prompt))
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 0)
        prompt = "/tally:do 1"
        self.put({"hash": hashlib.sha1(prompt.encode()).hexdigest(), "ts": time.time(), "done": True,
                  "result": {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "CTX"}}})
        self.assertEqual(self.env.hook("UserPromptSubmit", prompt=prompt)["hookSpecificOutput"]["additionalContext"],
                         "CTX")

    def test_a_0_11_8_hook_is_still_running(self):
        from tally import commands, db
        claimed = time.time()
        self.put({"hash": commands._once_digest("todo", "Write the notes @proj"), "ts": claimed, "done": False})

        def finish():                                   # 0.11.8 keeps the claim time in the finished record
            time.sleep(0.5)
            conn = db.connect()
            with db.transaction(conn):
                db.kv_set(conn, "once:" + SID, json.dumps({
                    "hash": commands._once_digest("todo", "Write the notes @proj"), "ts": claimed, "done": True,
                    "result": {"text": "0.11.8 ANSWER", "block": True, "context": None}}))
            conn.close()
        t = threading.Thread(target=finish)
        t.start()
        out = cli("slash", "--session", SID, "--cwd", "/Users/me/proj", "todo", "--", "Write the notes @proj").stdout
        t.join()
        self.assertIn("0.11.8 ANSWER", out)
        self.assertEqual(self.one("SELECT COUNT(*) FROM todos"), 0)

    def test_an_old_record_from_an_earlier_prompt_is_not_a_twin(self):
        from tally import commands
        self.put({"hash": commands._once_digest("todo", "Write the notes @proj"), "ts": time.time() - 5, "done": True,
                  "result": {"text": "OLD", "block": True, "context": None}})
        self.assertIn("Todo #1 added", self.env.hook("UserPromptSubmit", prompt="/tally:todo Write the notes @proj")["reason"])

    def test_0_11_8_sees_the_new_run(self):
        """What 0.11.6-0.11.8 read: once:<session> with their hash, the claim time and the Result."""
        from tally import commands, db
        self.env.hook("UserPromptSubmit", prompt="/tally:todo Write the notes @proj")
        conn = self.conn()
        prev = json.loads(db.kv_get(conn, "once:" + SID))
        conn.close()
        self.assertEqual(prev["hash"], commands._once_digest("todo", "Write the notes @proj"))
        self.assertTrue(prev["done"])
        self.assertLess(time.time() - float(prev["ts"]), 3)
        self.assertIn("Todo #1 added", prev["result"]["text"])
        self.assertTrue(prev["result"]["block"])


# ------------------------------------------------------------------ 3, 4, 5. parsing

class Parsing(Base):
    def test_a_newline_after_the_command(self):
        from tally import commands
        self.assertEqual(commands.parse("/tally:new\nRefactor the reader:\n- split parse()\n- add tests"),
                         ("new", "Refactor the reader:\n- split parse()\n- add tests"))
        self.assertEqual(commands.parse("/tally:todo\nBuy milk"), ("todo", "Buy milk"))
        self.assertEqual(commands.parse("/tally:status\n"), ("status", ""))
        self.assertIsNone(commands.parse("/tally:startx"))
        self.env.hook("UserPromptSubmit", prompt="/tally:start old task")
        r = self.env.hook("UserPromptSubmit", prompt="/tally:new\nRefactor the reader:\n- split parse()", prompt_id="p9")
        self.assertIsNone(r)
        self.assertEqual(self.one("SELECT initial_prompt FROM tasks WHERE id = 2"), "Refactor the reader:\n- split parse()")
        self.assertEqual(self.one("SELECT COUNT(*) FROM prompts WHERE task_id = 1"), 0)
        self.assertIn("Todo #1 added", self.env.hook("UserPromptSubmit", prompt="/tally:todo\nBuy milk")["reason"])

    def test_list_with_a_superscript(self):
        r = self.env.hook("UserPromptSubmit", prompt="/tally:list ²")
        self.assertEqual(r["decision"], "block")

    def test_end_with_a_number_and_a_note(self):
        e = self.env
        e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start other terminal work")          # #1 elsewhere
        e.hook("UserPromptSubmit", prompt="/tally:start my task")                                # #2 here
        r = e.hook("UserPromptSubmit", prompt="/tally:end 1 test still flaky, will revisit")
        self.assertIn("Task #2 ended", r["reason"])
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id = 1"), "active")
        self.assertEqual(self.one("SELECT end_note FROM tasks WHERE id = 2"), "1 test still flaky, will revisit")
        e.hook("UserPromptSubmit", prompt="/tally:start third")                                  # #3 here
        self.assertIn("Task #3 ended", e.hook("UserPromptSubmit", prompt="/tally:end 3 shipped")["reason"])
        self.assertEqual(self.one("SELECT end_note FROM tasks WHERE id = 3"), "shipped")
        e.hook("UserPromptSubmit", prompt="/tally:start fourth")                                 # #4 here
        self.assertIn("Task #1 ended", e.hook("UserPromptSubmit", prompt="/tally:end #1 moved on")["reason"])
        self.assertEqual(self.one("SELECT end_note FROM tasks WHERE id = 1"), "moved on")
        e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start fifth")                        # #5 elsewhere
        self.assertIn("Task #5 ended", e.hook("UserPromptSubmit", prompt="/tally:end 5")["reason"])
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id = 4"), "active")
        e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start sixth")                        # #6 elsewhere
        r = e.hook("UserPromptSubmit", sid=SID3, prompt="/tally:end 6 from a third terminal")    # nothing active there
        self.assertIn("Task #6 ended", r["reason"])


# ------------------------------------------------------------------ 6. pi

class Pi(unittest.TestCase):
    def test_print_mode_refuses_new_and_do_before_running_them(self):
        ts = (PLUGIN / "agents" / "pi" / "tally.ts").read_text()
        handler = ts[ts.index("handler: async (args, ctx)"):]
        refuse = handler.index('ctx.mode !== "tui"')
        self.assertLess(refuse, handler.index("await slash(ctx, cmd, rest)"))
        self.assertIn('cmd === "new" || cmd === "do"', handler[:refuse + 200])


# ------------------------------------------------------------------ 7, 8, 9. hooks

class Hooks(Base):
    def test_a_clear_in_another_terminal_is_not_joined(self):
        e = self.env
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_PID": "5555"}):                     # the other terminal
            e.hook("SessionStart", sid=SID2, source="startup")
            e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start their task")
            e.hook("SessionEnd", sid=SID2, reason="clear")
        e.hook("SessionStart", sid=SID3, source="clear")                                       # this one: pid 4242
        self.assertEqual(self.one("SELECT COUNT(*) FROM task_sessions WHERE session_id = ?", SID3), 0)
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_PID": "5555"}):                     # its own /clear: joins
            e.hook("SessionStart", sid=SID, source="clear")
        self.assertEqual(self.one("SELECT task_id FROM task_sessions WHERE session_id = ?", SID), 1)

    def test_a_clear_of_a_session_without_a_pid_is_joined(self):
        e = self.env
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_PID": ""}):
            e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start their task")
            e.hook("SessionEnd", sid=SID2, reason="clear")
        e.hook("SessionStart", sid=SID3, source="clear")
        self.assertEqual(self.one("SELECT task_id FROM task_sessions WHERE session_id = ?", SID3), 1)

    def test_auto_start_skips_a_project_set_to_off(self):
        from tally import paths
        e = self.env
        paths.save_config({"auto_start": "session"})
        e.hook("UserPromptSubmit", prompt="/tally:project off")
        self.assertIsNone(e.hook("UserPromptSubmit", prompt="secret plans for the off project"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM tasks"), 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM prompts"), 0)
        e.hook("UserPromptSubmit", prompt="/tally:project tasks")
        self.assertIsNone(e.hook("UserPromptSubmit", sid=SID2, prompt="now count it"))
        self.assertEqual(self.one("SELECT created_by FROM tasks WHERE id = 1"), "auto")

    def test_a_hand_edited_reminder_limit(self):
        from tally import paths
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:todo Call the client @proj")
        paths.home().mkdir(parents=True, exist_ok=True)
        paths.config_path().write_text(json.dumps({"todos": {"reminder_limit": "five"}}))
        r = e.hook("SessionStart", source="startup")
        self.assertIn("Call the client", r["systemMessage"])


# ------------------------------------------------------------------ 10, 11, 12. todos and the CLI

class TodosAndCli(Base):
    def test_mark_done_or_dropped_ends_the_linked_task(self):
        e = self.env
        e.hook("UserPromptSubmit", prompt="/tally:todo Fix export @proj")
        e.hook("UserPromptSubmit", prompt="/tally:take 1")
        r = e.hook("UserPromptSubmit", prompt="/tally:mark 1 done shipped")
        self.assertIn("Todo #1 is now done", r["reason"])
        self.assertIn("Task #1 ended", r["reason"])
        self.assertEqual((self.one("SELECT status FROM tasks WHERE id = 1"),
                          self.one("SELECT end_note FROM tasks WHERE id = 1")), ("ended", "shipped"))
        e.hook("UserPromptSubmit", prompt="/tally:todo Write docs @proj")
        e.hook("UserPromptSubmit", prompt="/tally:take 2")
        e.hook("UserPromptSubmit", prompt="/tally:mark 2 dropped")
        self.assertEqual((self.one("SELECT status FROM tasks WHERE id = 2"),
                          self.one("SELECT end_note FROM tasks WHERE id = 2")), ("ended", "todo dropped"))
        self.assertEqual(self.one("SELECT status FROM todos WHERE id = 2"), "dropped")
        e.hook("UserPromptSubmit", prompt="/tally:todo Third @proj")
        e.hook("UserPromptSubmit", prompt="/tally:take 3")
        e.hook("UserPromptSubmit", prompt="/tally:mark 3 blocked")
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id = 3"), "active")      # blocked: keeps counting
        r = cli("todo", "mark", "3", "done", "--note", "via the cli")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.one("SELECT status FROM tasks WHERE id = 3"),
                          self.one("SELECT end_note FROM tasks WHERE id = 3")), ("ended", "via the cli"))
        e.hook("UserPromptSubmit", prompt="/tally:take 3")
        r = cli("todo", "done", "3")
        self.assertEqual((self.one("SELECT status FROM tasks WHERE id = 4"),
                          self.one("SELECT end_note FROM tasks WHERE id = 4")), ("ended", "todo done"))

    def test_config_sets_only_its_key(self):
        from tally import paths
        r = cli("config", "dashboard_port", "8770")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(paths.config_path().read_text()), {"dashboard_port": 8770})
        paths.config_path().write_text(json.dumps({"log": {"prompts": "off"}}))
        cli("config", "pricing", '{"my-model": {"input": 1}}')
        self.assertEqual(json.loads(paths.config_path().read_text()),
                         {"log": {"prompts": "off"}, "pricing": {"my-model": {"input": 1}}})
        paths.config_path().write_text('{"log": {"prompts": "off"},}')            # a stray comma
        r = cli("config", "auto_start", "session")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid JSON", r.stderr)
        self.assertEqual(paths.config_path().read_text(), '{"log": {"prompts": "off"},}')

    def test_dashboard_port_out_of_range(self):
        from tally import paths
        paths.home().mkdir(parents=True, exist_ok=True)
        paths.config_path().write_text(json.dumps({"dashboard_port": 1}))         # never the real dashboard
        for port in ("0", "65536", "-1"):
            r = cli("dashboard", "--stop", "--port", port)
            self.assertNotEqual(r.returncode, 0, port)
            self.assertIn("between 1 and 65535", r.stderr, port)


# ------------------------------------------------------------------ 13. other sessions' tasks

class OtherSessions(Base):
    def test_one_task_elsewhere_names_the_commands(self):
        e = self.env
        e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start video vrana")
        r = e.hook("UserPromptSubmit", prompt="/tally:end")
        self.assertIn("No active task in this session", r["reason"])
        self.assertIn("→ /tally:end 1 ends it · /tally:resume 1 counts it here", r["reason"])
        r = e.hook("UserPromptSubmit", prompt="/tally:status")
        self.assertIn("/tally:status 1 shows it · /tally:end 1 ends it · /tally:resume 1 counts it here", r["reason"])
        e.hook("UserPromptSubmit", sid=SID3, prompt="/tally:start another")
        r = e.hook("UserPromptSubmit", prompt="/tally:end")
        self.assertIn("/tally:end <id>", r["reason"])

    def test_session_start_mentions_a_task_of_this_project_elsewhere(self):
        e = self.env
        e.hook("UserPromptSubmit", sid=SID2, prompt="/tally:start video vrana")
        e.hook("UserPromptSubmit", sid=SID3, prompt="/tally:start elsewhere", cwd="/Users/me/other")
        msg = e.hook("SessionStart", source="startup")["systemMessage"]
        self.assertIn("▶ Task #1 video vrana is still active in another session → /tally:resume 1 counts it here · "
                      "/tally:end 1 ends it", msg)
        self.assertNotIn("elsewhere", msg)                                                 # another project
        msg = e.hook("SessionStart", sid=SID2, source="resume")
        self.assertNotIn("still active", (msg or {}).get("systemMessage", ""))            # it is attached here

    def test_the_setup_question_says_first_time_once(self):
        e = self.env
        self.assertIn("First time in project proj. Track it?", e.hook("SessionStart", source="startup")["systemMessage"])
        msg = e.hook("SessionStart", sid=SID2, source="startup")["systemMessage"]
        self.assertIn("Project proj is not set up yet. Track it?", msg)
        self.assertNotIn("First time", msg)


# ------------------------------------------------------------------ 14. a failing hook says so

class FailingHook(Base):
    def call(self, event="Stop", **fields):
        from tally import hooks
        payload = dict({"session_id": SID, "cwd": "/Users/me/proj", "hook_event_name": event}, **fields)
        out = io.StringIO()
        self.assertEqual(hooks.main(event, stdin=io.StringIO(json.dumps(payload)), stdout=out), 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_once_per_error_and_half_hour(self):
        from tally import paths
        with unittest.mock.patch("tally.hooks.handle", side_effect=RuntimeError("no such column: x " * 20)):
            r = self.call("UserPromptSubmit", prompt="hello")
            self.assertEqual(list(r), ["systemMessage"])
            msg = r["systemMessage"]
            self.assertTrue(msg.startswith("▍Tally · ⚠ the UserPromptSubmit hook failed (RuntimeError: no such column"))
            self.assertIn("Counting may have stopped; tally doctor shows details", msg)
            self.assertLess(len(msg), 330)
            self.assertIsNone(self.call("Stop"))                                            # the same error: quiet
            self.assertIsNone(self.call("Stop", agent="codex"))
        with unittest.mock.patch("tally.hooks.handle", side_effect=ValueError("bad record")):
            self.assertIn("(ValueError: bad record)", self.call("Stop")["systemMessage"])
        self.assertEqual(paths.error_log_path().read_text().count("Traceback"), 4)
        state = paths.home() / "hook_errors.json"
        data = json.loads(state.read_text())
        state.write_text(json.dumps({k: v - 1801 for k, v in data.items()}))               # half an hour later
        with unittest.mock.patch("tally.hooks.handle", side_effect=ValueError("bad record")):
            self.assertIsNotNone(self.call("Stop"))

    def test_quiet_when_the_state_file_cannot_be_written(self):
        with unittest.mock.patch("tally.hooks.handle", side_effect=RuntimeError("boom")), \
                unittest.mock.patch("tally.hooks.os.replace", side_effect=OSError("read-only")):
            self.assertIsNone(self.call("Stop"))

    def test_a_payload_that_is_not_json_is_only_logged(self):
        from tally import hooks, paths
        out = io.StringIO()
        hooks.main("SessionStart", stdin=io.StringIO("{not json"), stdout=out)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("JSONDecodeError", paths.error_log_path().read_text())

    def test_a_failing_command_still_reaches_the_model_and_says_why(self):
        e = self.env
        with unittest.mock.patch("tally.commands.run", side_effect=KeyError("column")):
            r = e.hook("UserPromptSubmit", prompt="/tally:status")
        self.assertNotIn("decision", r)
        self.assertIn("the UserPromptSubmit hook failed (KeyError: 'column')", r["systemMessage"])


if __name__ == "__main__":
    unittest.main()
