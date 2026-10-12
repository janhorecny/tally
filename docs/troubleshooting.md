# Troubleshooting & FAQ

Start with `tally doctor`. It shows:

- the data directory and database;
- the number of tasks;
- the last hook that fired, with its session;
- whether transcripts are found;
- whether the plugin is installed;
- whether any errors were logged.

```
tally 0.1.0 · python 3.9.6
data dir      /Users/me/.claude/tally
database      /Users/me/.claude/tally/tally.db
tasks         12 (1 active)
last hook     Stop at 2026-10-12 19:31:02 (session c2a7dba3)
transcripts   /Users/me/.claude/projects (ok)
plugin        tally@tally
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

## Errors

Hook errors never interrupt Claude Code. They are written to `~/.claude/tally/errors.log` with a traceback. Please include that file when reporting a problem.

## The dashboard does not open

- `tally dashboard --open` runs it in the foreground and shows any error directly.
- If the port is taken, run `tally config dashboard_port 8766`.
- Background output is logged to `~/.claude/tally/dashboard.log`.
- To stop a background dashboard: `pkill -f "tally dashboard"`.

## Upgrading from `task-counter`

`./install.sh` removes the old `task@task-counter` plugin and its marketplace, deletes the `task-counter` CLI symlink, and moves `~/.claude/task-counter/tasks.db` to `~/.claude/tally/tally.db`. Your history is kept.

## Uninstall

```bash
./uninstall.sh            # removes the plugin, marketplace and CLI symlink; keeps ~/.claude/tally
./uninstall.sh --purge    # also deletes the database
```
