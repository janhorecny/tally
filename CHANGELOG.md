# Changelog

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
