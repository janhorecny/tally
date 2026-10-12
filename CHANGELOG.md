# Changelog

## 0.2.0 (2026-10-12)

### Todos

- **Quick capture:** `/tally:todo` takes `@project`, `!`/`!!`/`!!!`, `due:` (English and Slovak days, `+3d`, `20.10.`) and ` -- notes`.
- **Listing:** `/tally:todos` shows open todos per project.
- **Picking up:** `/tally:take` starts measuring a todo. `/tally:do` also hands the todo to Claude as context.
- **Finishing:** `/tally:done` closes a todo and ends its task. `/tally:mark` sets any status.
- **Measured work:** each todo shows the tokens, cost and time of every task that worked on it.
- **Plans:**
  - `tally todo import` reads markdown plans: superpowers `### Task N`, `**Step N**`, and checklists.
  - Re-importing is idempotent.
  - When every step is done, the parent and then the plan are closed automatically.
  - `tally todo next` gives the next item in plan order.
- **Skill:** a `tally:todos` skill lets Claude (and workflows such as superpowers) import plans and update todos itself.
- **Reminders:** open and overdue todos are shown when a session starts (0 tokens) and in the status line.

### Projects

- **Setup question:** the first time Claude Code opens in a project, Tally asks how to track it: `track` (every session), `tasks` or `off`.
- **Overall statistics:** all time, 7 and 30 days, a daily series, by model, tasks and their share, and todos. Available via `/tally:project`, `/tally:projects`, `tally project(s)`, and the dashboard's Projects tab.

### Detailed statistics and settings

- **Per-prompt breakdown:** tokens, cost, duration, calls, subagents and tools for every prompt. It is exact, because each call is attributed to the prompt that caused it, through Claude Code's `promptId`. Automatic turns are listed separately.
- **Summary statistics:** prompts, average and longest turn, cache hit rate, output per active minute, subagents and workflows.
- **Tool calls:** calls and errors per tool, including MCP tools.
- **Settings:** `/tally:settings`, `tally settings` and the dashboard Settings tab.
  - `log.depth`: `summary`, `prompts` or `calls`.
  - `log.prompts`: `full`, `preview` or `off`.
  - `todos.reminder`.

### Dashboard

- **New views:** Todos board (add, status, steps), Projects (modes, 30-day chart), Settings, a project filter, and per-prompt and tool tables in the task detail.
- **Protected writes:** writes require same-origin, through a custom header and an `Origin` check.

### Schema

Schema v5. Older databases migrate automatically.

## 0.1.0 (2026-10-12)

First release.

- **Commands:** zero-token `/tally:*` commands handled in a `UserPromptSubmit` hook: `start`, `new`, `status`, `end`, `list`, `show`, `resume`, `rename`, `dashboard`, `help`.
- **Ingest:** incremental reading of transcripts into SQLite, covering the main session, subagents, nested forks, background agents and workflow agents (by run and phase). Usage is deduplicated per API response, and history before a task start is skipped.
- **Report:** totals, an agent tree with own and rollup subtotals, and groupings by model, agent type and workflow. Wall time, active time (the union of turn intervals from transcript records and hook timing) and per-agent durations.
- **Cost:**
  - Per-call cost at list price, with 5-minute and 1-hour cache writes, fast mode, and Claude Haiku 5.5 tiers.
  - Prices aligned with what Claude Code bills.
- **Reconciliation:** Claude Code's own unlogged calls are estimated from its exit-time `cost-state` record. Validation: 40 of 40 recent sessions within 2% of Claude Code's own cost.
- **Task lifecycle:**
  - Tasks span sessions: the `/clear` carry-over uses the Claude Code PID, with a cwd fallback.
  - `/tally:resume` works across terminals and reopens ended tasks, using interval-based session windows.
  - Snapshots are frozen when a task ends and refreshed when a newer cost record arrives.
- **Interfaces:**
  - A local dashboard: read-only, localhost only, with a `Host` header check.
  - A status line segment and a CLI with CSV/JSON export.
  - `tally session` analyses any past session.
  - `tally doctor` checks the installation.
- **Installer:** `install.sh` and `uninstall.sh`. The installer migrates an earlier `task-counter` install.
