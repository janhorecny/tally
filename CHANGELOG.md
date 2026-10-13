# Changelog

## 0.4.1 (2026-10-13)

### Documentation

- **Command reference** (`docs/commands.md`): every `/tally:*` command with its syntax, arguments, behaviour, examples and CLI equivalent.
- **CLI reference** (`docs/cli.md`): every `tally` command and option, including `todo` subcommands, `install`, `uninstall` and `doctor`.
- The usage guide now links to both references instead of repeating them. Troubleshooting has current `tally doctor` output, updating steps and a sessions FAQ. How it works and Configuration cover Codex, pi and the sessions index, and the dashboard's write protection.
- The README banner names Claude Code, Codex and pi.

### Fixes

- `/tally:help` and the command hints were out of date: `show [id]`, `todos … done`, `mark … [note]` and `project [name] [mode]`. Agent-neutral wording where a command works in every agent.

## 0.4.0 (2026-10-12)

### Sessions overview

- **Every session, every agent:** `/tally:sessions [days|all] [agent] [here]`, `tally sessions` and the dashboard's new *Sessions* tab list every session of Claude Code, Codex and pi, with or without a task. Each row shows agent, project, title, span, active time, prompts, API calls, tokens, cost and models, with totals per agent.
- **Sessions index:** whole sessions are read from the agents' own transcripts into a separate cache, `~/.claude/tally/sessions.db`, so the task database keeps skipping history before a task start. The index is incremental: a first run over 30 days (1,138 transcripts, 4.1 GB) took 19 s, and a refresh with nothing new takes 0.05 s. Projects set to `off` are left out.
- **Prompts of whole sessions:** the prompts you typed are recorded per session. Background-agent notifications and tool results are shown as automatic turns.
- **`tally session <id>`** now works for Codex and pi too, accepts an id prefix and uses the index instead of a throw-away database.
- **Dashboard:** period and agent filters, a scrollable table and a full report per session (per prompt, agents, models).

### Fixes

- `projects.detect_root` returned a single `None` instead of `(None, None)` for an unusable path.

## 0.3.0 (2026-10-12)

### Codex and pi support

- **Installer:** `./install.sh` (or `tally install`) detects Claude Code, Codex and pi and asks which ones to install into. Non-interactive: `--agents claude,codex,pi`, `--all` or `--yes`. `./uninstall.sh --agents …` removes Tally from specific agents only. `tally doctor` shows the state per agent.
- **Codex:**
  - **Hooks:** merged into `~/.codex/hooks.json`; other hooks are kept, and uninstall removes only Tally's.
  - **Commands:** `tally:<cmd>` prompts are blocked by `UserPromptSubmit`, at zero tokens.
  - **Rollout reader:** usage comes from deltas of the cumulative `token_count` totals, including the baseline when a task starts mid-session.
  - **Attribution:** calls are attributed to turns; subagent threads, with their roles, are discovered and counted; history inherited by a fork is skipped.
  - **Prices:** OpenAI list prices.
  - **Skill:** `tally-todos` in `~/.agents/skills`.
- **pi:**
  - **Extension:** `/tally <cmd>` with completion, plus `tally:<cmd>` typed as a prompt.
  - **Display:** a report widget, notifications and a live footer status.
  - **What is counted:** prompts are read from the session file; pi's own reported cost is used; nested subagent usage on tool results counts as agents (`pi-subagents`, `@tintinweb/pi-subagents`).
  - **Handing work to the model:** `/tally do` and `/tally new` use `sendUserMessage` in the TUI; in print mode use the `tally:` form.
  - **Skill:** `tally-todos`.
- **Shared core:**
  - `sessions.agent` and the agent-reported `usage.cost` (schema v6), plus per-file reader state.
  - The command syntax in messages is adjusted per agent.
- **Testing:** verified against real Codex 0.162.1 (hooks, blocking, rollouts) and pi 0.86.0 (print and RPC modes, using pi's faux provider for model runs).

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
