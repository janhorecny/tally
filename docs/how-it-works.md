# How it works

```
            ┌──────────────────────── Claude Code ────────────────────────┐
            │  you ── prompt ──▶ UserPromptSubmit hook ──▶ model          │
            │                         │  /tally:* answered here,           │
            │                         │  prompt blocked = 0 tokens         │
            │  main agent ─┬─ subagents ─ forks                           │
            │              └─ workflows ─ phases ─ agents                 │
            │        │ every API response is appended to a transcript     │
            └────────┼─────────────────────────────────────────────────────┘
                     ▼
  ~/.claude/projects/<project>/<session>.jsonl                      main session
  ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl (+ .meta.json)
  ~/.claude/projects/<project>/<session>/subagents/workflows/<run>/agent-<id>.jsonl
  ~/.claude/projects/<project>/<session>/workflows/<run>.json         workflow run record
                     │
   Stop / SubagentStop / PreCompact / SessionEnd hooks: read new bytes
                     ▼
          ~/.claude/tally/tally.db  ──▶  /tally:status · CLI · dashboard · status line
```

Tally is a Claude Code **plugin**. It consists of slash commands, hooks, and a Python package that uses only the standard library. The plugin never calls a model. It never contacts the network, except for the dashboard, which listens on localhost. It reads only the transcripts Claude Code already writes.

## Components

| path | role |
|---|---|
| `plugin/.claude-plugin/plugin.json` | Plugin manifest (name `tally`, so commands are `/tally:*`). |
| `plugin/hooks/hooks.json` | Hook registrations. Every hook runs `python3 bin/tally hook <Event>`. |
| `plugin/commands/*.md` | Slash command definitions. The hook answers them. Their bodies are only a fallback that runs the CLI when hooks are disabled. |
| `plugin/bin/tally` | Launcher. `hook` goes straight to `tally.hooks`, without argparse. Everything else goes to `tally.cli`. |
| `tally/hooks.py` | Hook entry point. Never raises: errors go to `errors.log`. |
| `tally/commands.py` | `/tally:*` parsing and handlers, shared by the hook and the CLI. |
| `tally/tasks.py` | Task lifecycle: start, end, attach/resume, reopen, the `/clear` carry-over. |
| `tally/ingest.py` | Incremental transcript reader that writes `usage`, `agents`, `workflows` and `turns`. |
| `tally/report.py` | Builds the report: totals, the agent tree, groupings, time. Also handles snapshots. |
| `tally/reconcile.py` | Unlogged-usage estimate from Claude Code's `cost-state` records. |
| `tally/pricing.py` | Price table and per-call cost. |
| `tally/fmt.py` | Text rendering for the terminal. |
| `tally/server.py`, `dashboard.html` | Local dashboard: a read-only JSON API and a single HTML page. |
| `tally/db.py` | SQLite schema, migrations, transactions. |

## Hooks

| event | what Tally does | typical time |
|---|---|---|
| `UserPromptSubmit` | `/tally:*`: runs the command and returns `{"decision":"block","reason":…}`, so the reason is shown to you and the model is never called. A normal prompt while a task is active is stored, and a turn timer starts. | ~35 ms |
| `Stop` | Reads new transcript bytes of the session (only while a task is active) and closes the turn timer. | ~35 ms plus the new bytes |
| `SubagentStop`, `PreCompact` | Read new transcript bytes. | same |
| `SessionStart` | Records the session (transcript path, cwd, Claude Code PID). On `source=clear`, attaches the new session to the active task. | ~33 ms |
| `SessionEnd` | Reads the rest, closes the turn, and notes a `/clear` for the carry-over. | same |

Times are medians on an M-series Mac. About 18 ms of each is Python interpreter startup. Hooks that have nothing to do (no active task) touch the database once and exit. Only `/tally:*` prompts import the command and report modules.

## Reading transcripts

Each transcript file has a byte offset in the `files` table. A read consumes only complete lines appended since the last read, inside one `BEGIN IMMEDIATE` transaction. Parallel hooks, such as several subagents finishing at once, therefore serialize safely and never double count. A test covers this with 8 concurrent hook processes.

When a task starts, all of the session's files are registered at their current size, so **history before the task is never parsed**. The exception is a session that is still counting towards another active task: then nothing is skipped, so that task loses nothing.

Every line is first checked with cheap substring tests. Only candidate lines are JSON-parsed, and they are then routed by their real `type`:

- `assistant` with `message.usage` becomes a `usage` row. The key is `message.id:requestId`, because one response is written as one line per content block. Those lines have identical usage, except that earlier ones can carry partial `output_tokens`, so the last line wins.
- `<synthetic>` messages, which are local error stubs, are skipped.
- `system/turn_duration` (interactive mode) becomes a `turns` row.
- A `toolUseResult` carrying `runId` and `workflowName` becomes a `workflows` row.
- Every line's `timestamp` updates the first and last activity of its agent.

Subagent metadata comes from `agent-<id>.meta.json`: `agentType`, `description`, `parentAgentId`, `workflowPhase`, `model`, `isFork`. If the meta file is not there yet, Tally retries on later reads.

## The task window

A task counts usage whose timestamp lies inside one of its `task_sessions` intervals:

- **Start:** `/tally:start` opens an interval for the current session.
- **`/clear`:** opens an interval for the new session ID. Claude Code keeps the same process, recognized by `CLAUDE_PID`. As a fallback, Tally uses the session that `SessionEnd(reason=clear)` reported for the same cwd within 30 s.
- **`/tally:resume <id>`:** opens an interval in another session, or reopens an ended task. The time between ending and reopening is not counted.
- **End:** `/tally:end` closes all intervals.

An active task's interval is open-ended, so a few milliseconds of clock skew between processes cannot drop usage.

## The report

- **Totals:** a sum over all `usage` rows in the window. Total tokens = input + output + cache read + cache write.
- **Tree:**
  - one root per session (`main session`);
  - subagents under the main session;
  - forks under the agent that spawned them (`parentAgentId`);
  - workflow agents under `workflow → phase`.
- **Own and rollup values:** every node has *own* values and *rollup* values (itself plus its descendants). The main-session row shows its own values, every other row shows rollups. The top-level rows therefore add up to the total.
- **Groupings:** by model, by agent type, and by workflow with its phases.
- **Wall time:** start to end.
- **Active time:** the union of turn intervals clipped to the window. The union matters because a turn waiting on background agents overlaps later turns. Turns come from `turn_duration` records and from hook timing (prompt → Stop). A turn interrupted without a `Stop` is closed at its last API response when the next prompt arrives, so idle time is not counted.
- **Agent time:** from the agent's first to its last transcript entry, clipped to the window.
- **Snapshots:**
  - When a task ends, the full report is frozen in `task_snapshots`, together with a small summary used by lists and the status line. It therefore survives Claude Code deleting old transcripts.
  - A snapshot is rebuilt only when Claude Code later writes a newer cost record for one of the task's sessions (see below).

## Cost

Cost is computed **per API call**, by `pricing.Pricer.cost`. The rates:

- input, output and cache-read prices for the model;
- cache writes at 1.25× input for the 5-minute TTL and 2× for the 1-hour TTL. The transcript records which TTL each write used.
- 2× in fast mode.
- Claude Haiku 5.5's higher tier above 100K prompt tokens.

The table follows what Claude Code itself bills. The `[1m]` suffix, date suffixes and Bedrock/Vertex prefixes are normalized away.

## Claude Code's own calls (reconciliation)

Some calls never reach a transcript:

- background-agent progress summaries;
- prompt suggestions;
- titles;
- memory extraction;
- compaction.

When a session exits, Claude Code appends `{"type":"cost-state", "modelUsage": {...}}`, which holds exact per-model tokens and `costUSD` for the whole session, including those calls. `reconcile.py` uses it as follows:

1. **Find the record.** The main transcript is scanned for a newer `cost-state`, starting from the offset where the last scan stopped.
2. **Measure what was logged.** All of the session's transcripts are read up to that moment, per model, with cost priced per call.
3. **Compute the difference.** Unlogged tokens are `cost-state` tokens minus logged tokens. Unlogged cost is Claude Code's exact `costUSD` minus the logged cost. Using Claude Code's figure keeps per-call tiers correct for calls Tally never sees.
4. **Give the task its share.** Each task gets a share in proportion to its fraction of the session's logged tokens.

The result is the separate `unlogged ≈ +…` line. Its status is `estimated`, `partial` (the session ran on after its last record) or `pending` (no record yet). The exact per-agent numbers never change.

Validation: `python3 scripts/validate_against_claude.py`. It reads files only. On the 40 most recent sessions of the development machine:

| | within 2% of Claude Code's own session cost |
|---|---|
| transcripts only | 12 / 40 |
| transcripts + unlogged estimate | **40 / 40** |

## Performance

The heaviest session on the development machine had 527 transcript files, 298 MB, 10.6k API calls and a 465-agent workflow:

| operation | time |
|---|---|
| hook with nothing to do | ~33 ms (Python startup ~18 ms) |
| incremental read with no new bytes | 8 ms |
| building the full report (526 agents) | 75 ms |
| ending a task (read + report + snapshot) | 90 ms |
| `/tally:list` and the dashboard list (from snapshot summaries) | 2 ms |
| reading a whole session from scratch (`tally session`) | 1.3 s |
| one-off rescan when a new `cost-state` appears | ~0.7 s |

## Security and privacy

- **Local only:** all data stays in `~/.claude/tally`, and nothing is sent anywhere.
- **Prompts are stored** in the database (`prompts` table), so treat the file like your transcripts.
- **Dashboard:**
  - It binds to `127.0.0.1` and is read-only.
  - It rejects requests whose `Host` header is not localhost, which blocks DNS-rebinding pages from reading your prompts.
- **Hooks never break Claude Code:** every error is caught and logged, and the hook exits 0.
