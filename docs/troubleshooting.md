# Troubleshooting & FAQ

Start with `tally doctor`. It shows:

- the version, the data directory and the database;
- the number of tasks;
- the last hook that fired, with its session;
- the sessions recorded per agent;
- whether Claude Code's transcripts are found;
- for each agent, whether it is installed, found but not installed, or not found;
- whether `tally setup` has run, and what Claude Code's status line shows;
- whether any errors were logged.

```
▍Tally 0.6.0 · doctor · python 3.9.6
data dir      /Users/me/.claude/tally
database      /Users/me/.claude/tally/tally.db
tasks         12 (1 active)
last hook     Stop at 2026-10-12 19:31:02 (session c2a7dba3)
sessions      claude 40 · codex 3 · pi 2
transcripts   /Users/me/.claude/projects (ok)
Claude Code   installed
Codex         found, not installed (./install.sh --agents codex)
pi            installed
setup         done 2026-10-13 09:12
status line   Tally
errors        none
```

## `/tally:start` says "Unknown command", or the prompt goes to Claude

The plugin is not loaded in this session.

- Run `/reload-plugins`, or restart Claude Code. Plugins load at session start.
- Check `claude plugin list`. It must show `tally@tally` with status *enabled*.
- Commands are namespaced: write `/tally:start`, not `/start` or `/tally-start`.

## `last hook: never`

The hooks have not fired since installation. Restart Claude Code and send any prompt, then run `tally doctor` again. If it still says never, check `/hooks` inside Claude Code: the `tally` hooks should be listed under UserPromptSubmit, Stop, SubagentStop, SessionStart, SessionEnd and PreCompact.

## A command printed "UserPromptSubmit operation blocked by hook"

That is expected. The `/tally:*` commands are answered by the hook, and blocking the prompt is what keeps them from costing tokens. The text after that line is Tally's output.

## Numbers differ from `/cost` or the status line

- **Scope:** `/cost` covers the whole session. A task covers only its own window, which can include several sessions.
- **Claude Code's own calls:** some calls are never written to transcripts, such as background-agent summaries, prompt suggestions and titles. They appear as the separate `unlogged ≈ +…` line once the session has exited and Claude Code has written its cost record. Until then, the task total is lower than `/cost` by that amount, usually a few percent.
- **Prices:** cost is computed per response from list prices. If Anthropic changes a price, update it in `config.json` (see [configuration](configuration.md#pricing-overrides)).
- **Checking it yourself:** `python3 scripts/validate_against_claude.py` compares Tally with Claude Code's own totals for your recent sessions.

## Active time is 0s

Active time comes from turn intervals. Interactive sessions write `turn_duration` records, and the hooks also time each prompt until `Stop`. A turn that started before the task, or one interrupted before `Stop` fired, is not counted. Wall time is always correct.

## A subagent shows as `subagent` without a description

Its `agent-<id>.meta.json` was not written yet when the agent's first lines were read. Tally retries on later reads, and the next `/tally:status` usually fills it in.

## Background agents were still running when I ended the task

Only usage written before `/tally:end` is counted. To include the rest, use `/tally:resume <id>` before they finish, and end the task again afterwards.

## I used `/clear` and want to keep counting

Nothing to do. The task follows the new session ID automatically, because it is the same Claude Code process. For a different terminal, or after a restart, use `/tally:resume <id>`.

## I don't see the todo reminder or the new-project question

- **Headless mode:** both are `systemMessage`s on session start. They appear in interactive Claude Code, but `claude -p` does not print them. The status line shows the same information (`📋 3 todos`, or `▍Tally · new project: /tally:project …`). The welcome after installing waits for the first interactive session.
- **Reminders switched off:** check that `todos.reminder` is `on` (`/tally:settings`).
- **Project switched off:** check the project is not set to `off` (`/tally:projects`).

## Claude does not update the todo list during a plan

The `tally:todos` skill is loaded on demand. Ask explicitly ("import the plan into tally"), or mention Tally todos in your planning prompt. Also check that `tally` is on PATH (`which tally`). If it isn't, re-run `./install.sh`, or the skill falls back to `python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally"`.

## Codex: `tally:start` goes to the model

- **Hooks not trusted:** Codex runs hooks only after you approve them. Open Codex, run `/hooks`, and trust Tally's entries.
- **Hooks disabled:** run `codex features list`. `hooks` must be `true`, which is the default.
- **Not installed:** check that `~/.codex/hooks.json` contains the Tally hooks. `tally doctor` shows the state, and `./install.sh --agents codex` reinstalls them.

## pi: `/tally` is unknown

- **Not loaded yet:** the extension is `~/.pi/agent/extensions/tally.ts`, or the same path under `$PI_CODING_AGENT_DIR`. Restart pi or run `/reload`.
- **Not auto-discovered:** if you keep extensions somewhere else, add the file to `"extensions"` in `~/.pi/agent/settings.json`.
- **Print mode:** in `pi -p`, `/tally do` cannot hand work to the model, because the process ends first. Use `tally:do <id>` as the prompt instead.

## `/tally:sessions` is slow, or shows old numbers

- **The first run** over a period reads every transcript of it into the sessions index; 30 days of heavy use took about 20 s. Later runs read only new data and take well under a second.
- **Sessions differ from tasks:** a task counts only its own window, so it is usually smaller than the session it ran in.
- **Rebuilding:** the index is a cache. Delete `~/.claude/tally/sessions.db` and it is rebuilt on the next run.

## Colours or odd characters in the terminal

The CLI colours its output only on a terminal, never when piped. `NO_COLOR=1` turns colours off. The banner is plain ASCII; only the `▍` marks need a UTF-8 terminal, and they fall back to `|` elsewhere.

## Errors

Hook errors never interrupt Claude Code. They are written to `~/.claude/tally/errors.log` with a traceback. Please include that file when reporting a problem.

## The dashboard does not open

- `tally dashboard --open` runs it in the foreground and shows any error directly.
- If the port is taken, run `tally config dashboard_port 8766`.
- Background output is logged to `~/.claude/tally/dashboard.log`.
- To stop a background dashboard: `pkill -f "tally dashboard"`.

## The dashboard looks old or shows errors after an update

A dashboard started by an older Tally version keeps running until it is stopped. `/tally:dashboard` and `tally dashboard` replace it automatically. If you open the page directly, run one of them once, or `pkill -f "tally dashboard"`.

## Updating

```bash
cd tally && git pull && ./install.sh
```

Then run `/reload-plugins` in Claude Code (or restart it), `/reload` in pi, and restart Codex. New `/tally:*` commands appear only after this.

## Upgrading from `task-counter`

`./install.sh` removes the old `task@task-counter` plugin and its marketplace, deletes the `task-counter` CLI symlink, and moves `~/.claude/task-counter/tasks.db` to `~/.claude/tally/tally.db`. Your history is kept.

## Uninstall

```bash
./uninstall.sh                  # from every agent; removes the CLI symlink; keeps ~/.claude/tally
./uninstall.sh --agents codex   # from one agent only
./uninstall.sh --purge          # also deletes the data directory
```
