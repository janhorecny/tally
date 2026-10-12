# Roadmap: ideas and improvements

These are proposals, not commitments. Each is grouped by value, with a short note on how it would fit the current design.

## High value, small effort

1. **Budgets and alerts.** `/tally:budget 5$` or `/tally:budget 2M`. The `Stop` / `SubagentStop` hooks already ingest usage. When a task crosses 50%, 80% or 100% of its budget, a hook could emit a `systemMessage`, shown to you at no token cost. At 100% it could optionally inject `additionalContext` asking Claude to wrap up. A per-task budget column and a check in `hooks.py` is all it needs.
2. **Tags.** `/tally:tag bugfix client-x`, plus reports grouped by tag and month: `tally report --by tag --since 2026-10-01`. Projects are done since 0.2.0; tags would add a cross-project dimension.
3. **Git context.** Record the branch and `HEAD` at start and at end, and the lines added and removed (`git diff --shortstat start..end`). Then "cost per merged PR" becomes a single query. It could be filled in by the `start`/`end` commands with a 1-second timeout.
4. **Pause.** `/tally:pause` and `/tally:resume`, for lunch breaks, so that wall time measures working time. This is an interval list on the task, the same mechanism as `task_sessions`.
5. **Markdown report.** `tally show 12 --md` produces a paste-ready summary for a PR description, an invoice note or Notion.
6. **Retention.** `tally prune --older-than 90d` drops `usage` rows of ended tasks; their snapshots keep the totals. This keeps the database small for heavy users.

## High value, medium effort

7. **Time-tracking integration.** Push the task time and the token cost to the time tracker you already use (ActiveCollab, Toggl, Clockify, Jira worklogs) when a task ends. A small `exporters/` module would be configured in `config.json`.
8. **Plan usage instead of dollars.** For Pro and Max plans, show how much of the 5-hour or weekly limit a task consumed. The status line input carries `rate_limits`. A `tally statusline` wrapper could record those numbers with timestamps, and the report would show the delta between the task's start and end.
9. **Tokens per tool.** Tool calls and errors are counted since 0.2.0 (`log.depth calls`). The next step is attributing *tokens* to tools: a tool result's size becomes input on the next request, which would answer "which MCP server makes my sessions expensive".
10. **Dashboard timeline.** A tokens-over-time chart and a Gantt view of agents per task, showing where parallel agents overlapped and where the cache was cold (cache writes against reads).
11. **Cache-efficiency hints.** Flag tasks whose cache-write share is unusually high, which suggests cache misses from long pauses or edits to the prompt prefix. Suggest keeping turns closer together, or the 1-hour cache.
12. **Headless / CI mode.** With `TALLY_TASK="nightly docs refresh"`, a `claude -p` run starts and ends a task automatically. This is useful for scheduled agents and for benchmarking prompts. The hook already sees the session start; it only needs to read the variable.

13. **Recurring todos and snooze.** `due:every-mon`, and `/tally:snooze <id> +2d`.
14. **Todo dependencies.** `blocked by #12` becomes `todo` automatically when #12 is done.

## Larger ideas

15. **Exact capture through OpenTelemetry.** Claude Code can export an `api_request` event for every call, including its own background calls, over OTLP. A local OTLP receiver in Tally, enabled through `env` in settings, would make the unlogged estimate exact. The trade-off is a resident service and the extra settings.
16. **Estimates before you start.** "Tasks like this usually cost ~$2 and take ~25 min." Use similarity over initial prompts and tags across your history, shown on `/tally:start`.
17. **Team mode.** An optional sync of ended-task snapshots to a shared store (a Git repo, S3 or a small server) for a team dashboard, with per-user attribution. Only snapshots would be synced, never prompts.
18. **Self-calibrating prices.** Fit per-model prices from the `cost-state` records Claude Code writes (`scripts/validate_against_claude.py` already does the comparison). When a price changes, the change is detected automatically.
19. **MCP read-only server.** Let Claude answer "what did the last three tasks cost?" from the database. It should be opt-in, since tool definitions cost context tokens in every session.
20. **Windows.** CI already runs on macOS and Ubuntu (Python 3.8 to 3.13). Add a Windows job and use `py -3` in `hooks.json` there.

## Known limitations (today)

- **Unlogged usage is an estimate.** Claude Code's own background calls are attributed proportionally, and only after the session exits.
- **Ending a task stops the count.** Agents still running at `/tally:end` are not counted afterwards; use `/tally:resume`.
- **Turns that span the task start.** Active time ignores a turn already in progress when the task starts.
- **Prices go stale.** The price table is static, so a price change needs a code or `config.json` update.
