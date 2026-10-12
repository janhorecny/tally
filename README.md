# task-counter

Per-task **token** and **time** tracking for Claude Code. It runs entirely locally and stores everything in SQLite.

You start a task inside Claude Code and keep working. Everything that happens while the task is open is counted:

- the main conversation;
- every subagent, including background agents and nested forks;
- every workflow agent, grouped by run and phase.

When you end the task you get totals plus subtotals per agent, subagent, agent type, workflow and model. The task name and initial prompt are stored with it.

```
/task:start Fix login redirect loop
… work as usual …
/task:end
```

```
Task #3 · Fix login redirect loop   [■ ended]
  prompt   The login page loops back to /login after OAuth callback, find out why and fix it
  time     44m 10s wall · 31m 02s active (12 turns) · 2026-10-12 14:03 → 14:47
  tokens   4.21M total · in 12.3k · out 85.1k · cache write 410.2k · cache read 3.70M
  cost     ≈ $3.12 at list price · 171 API calls
  models   opus-5-5 3.90M $2.80 · sonnet-5-5 310.0k $0.32

  breakdown                                        tokens   output      cost      time  calls
  main session (own)                                3.10M    60.2k     $2.10   31m 02s     42
  ├─ general-purpose · Explore auth flow            410.0k     8.1k    $0.300    1m 10s      9
  │  └─ fork · Write regression test                 92.0k     2.0k    $0.071       48s      3
  └─ workflow · review-changes (25 agents)          700.0k    16.0k    $0.720    6m 30s    120
     ├─ phase · Review (20)                         560.0k    12.1k    $0.580    4m 10s     96
     └─ phase · Verify (5)                          140.0k     3.9k    $0.140    2m 20s     24
  TOTAL                                             4.21M    85.1k     $3.12   44m 10s    171
```

## Install

Requirements: Claude Code 2.1+, and `python3` 3.8 or newer. On macOS the Command Line Tools version works. No pip packages are needed.

```bash
git clone <this repo> cc-task-counter && cd cc-task-counter
./install.sh
```

`install.sh` does three things:

1. Registers this folder as a local plugin marketplace (`task-counter`).
2. Installs the plugin `task@task-counter` at user scope, so it is active in every project.
3. Links the CLI to `~/.local/bin/task-counter`.

After installing, restart Claude Code or run `/reload-plugins`. Running `install.sh` again updates the plugin. Run `./uninstall.sh` to remove it; it keeps your history unless you pass `--purge`.

## Commands inside Claude Code

| command | what it does |
|---|---|
| `/task:start [name]` | Start a task in this session. If you leave out the name, your next prompt becomes the name. The next prompt is stored as the initial prompt either way. |
| `/task:new <prompt>` | Start a task and send `<prompt>` to Claude as its first instruction. |
| `/task:status [id]` | Show live totals for the current task, or for task `#id`. |
| `/task:end [id] [note]` | End the task and print the final report, which is then frozen in the database. |
| `/task:list [n\|all]` | List recent tasks with wall time, active time, tokens and cost. |
| `/task:show <id>` | Show the full report of any task. |
| `/task:resume <id>` | Keep counting task `#id` in this session, for example after a restart or in a second terminal. Also reopens an ended task. |
| `/task:rename [id] <name>` | Rename a task. |
| `/task:dashboard` | Open the local web dashboard at http://127.0.0.1:8765. |
| `/task:help` | Show the help text. |

**The commands cost no tokens.** A `UserPromptSubmit` hook answers them and blocks the prompt, so the model is never called. `/task:new` is the exception, because its prompt is meant to reach Claude.

### Rules

- **One task per session.** Starting a new one ends the previous one, and you see its report.
- **`/clear` keeps the task.** Claude Code starts a new session ID on `/clear`, and the task follows it automatically because it is the same Claude Code process.
- **Several sessions can run tasks in parallel,** for example one per terminal. `/task:status`, `/task:end` and `/task:resume` take a task ID when you need to act on a task from another session.
- **Optional auto mode:** run `task-counter config auto_start session`. Every new session then starts a task from its first prompt.

## Dashboard

`/task:dashboard` (or `task-counter dashboard --open`) serves a single-page dashboard on localhost. It shows:

- the task list;
- KPI tiles: total tokens, output, cost, wall and active time, API calls;
- token composition (input, output, cache write, cache read);
- an expandable breakdown tree with each node's share of the task;
- tables by model, by agent type and by workflow, plus the prompts of the task.

Active tasks refresh every 5 seconds. Light and dark themes are both supported.

## CLI

```
task-counter list                 # recent tasks
task-counter status [id] [--json] # live report
task-counter show <id> --prompts  # report and every prompt of the task
task-counter end [id] --note "…"
task-counter export --format csv -o tasks.csv
task-counter session <session-id|transcript.jsonl>   # analyse ANY past session, no task needed
task-counter dashboard --open
task-counter config auto_start session
task-counter doctor               # installation / hook health check
```

## How it works

```
Claude Code ── hooks ──▶ task-counter (python3, stdlib only) ──▶ ~/.claude/task-counter/tasks.db
     │                          ▲
     └── transcripts (.jsonl) ──┘  main session + subagents/agent-*.jsonl + subagents/workflows/<run>/agent-*.jsonl
```

### Hooks

- `UserPromptSubmit` handles the `/task:*` commands and stores the task's prompts.
- `Stop`, `SubagentStop`, `PreCompact` and `SessionEnd` read new transcript lines into the database. This happens only while the session has an active task.
- `SessionStart` follows `/clear`.
- Hooks never break Claude Code. On any error they exit silently and log to `~/.claude/task-counter/errors.log`.

### Tokens

Tokens are read from the `usage` field of every assistant message in Claude Code's own transcripts:

- **Deduplication:** one API response is written as several lines, so lines are deduplicated by message ID plus request ID, keeping the final line.
- **Incremental reads:** reading is incremental, by byte offset. Starting a task skips the history before it, so hooks stay fast. Validation runs read about 60 MB of transcripts in under a second.
- **Attribution:** comes from each subagent's `.meta.json`, which gives its type, description, parent agent (for nested forks) and workflow phase. Workflow names come from Claude Code's workflow run record.

### Time

- **Wall time** runs from start to end.
- **Active time** is the union of the turns Claude spent working. Turns come from the transcript's `turn_duration` records and from prompt→Stop hook timing. Overlapping turns, such as a turn waiting on background agents, are not double counted.
- **Per-agent time** runs from the agent's first to its last transcript entry inside the task window.

### Cost

Cost is an estimate at API list price.

- **Exact cache pricing:** 1-hour and 5-minute cache writes are priced separately, because the transcript records which was used.
- **Fast mode** is billed at 2×.
- **Overrides:** you can change or add prices in `~/.claude/task-counter/config.json` under `"pricing"`.
- **Subscriptions:** Pro and Max plans are not billed per token, but the figure is still useful for comparing tasks.

### Accuracy, and what is not counted

`scripts/validate_against_claude.py` compares the parser against the `cost-state` record Claude Code writes when a session exits. On the 40 most recent sessions on the development machine:

- Output tokens agree within 0–10%. Cost agrees within about 5% for most sessions.
- Sessions with a workflow matched to 0.1%.

The gap always runs the same way, toward under-counting. Claude Code makes some calls of its own that it never writes to any transcript, so no local tool can attribute them to an agent:

- prompt suggestions;
- memory extraction;
- compaction;
- session titles and away summaries.

In long sessions with many background agents this housekeeping can reach roughly 20% of cost. Everything you trigger is counted exactly: the main agent, subagents, forks and workflow agents.

## Data

- **Where it lives:** SQLite at `~/.claude/task-counter/tasks.db`. Set `TASK_COUNTER_HOME` to use a different location.
- **Tables:** `tasks`, `task_sessions`, `usage` (one row per API call), `agents`, `workflows`, `turns`, `prompts`, and `task_snapshots`. `task_snapshots` holds the final report frozen when a task ends, so it survives Claude Code's transcript cleanup.

## Development

```bash
python3 -m unittest discover -s tests -v       # end-to-end tests on synthetic transcripts
python3 scripts/validate_against_claude.py     # compare with Claude Code's own totals (reads files only)
claude --plugin-dir ./plugin                   # try the plugin without installing it
```
