---
name: todos
description: The user's Tally todo list (per project, status, priority, due dates, measured tokens/time). Use when the user asks to add, list, pick up or finish todos, after writing an implementation plan (e.g. superpowers writing-plans) to import its tasks, and when you complete a planned task to mark it done.
allowed-tools: Bash(tally:*), Bash(python3:*)
---

# Tally todos

Tally keeps the user's todo list in `~/.claude/tally/tally.db`. Every todo belongs to a project (the git
repository it was created in) and has a status: `todo`, `doing`, `blocked`, `done`, `dropped`.
When a todo is picked up, Tally measures the tokens and time spent on it.

Use the CLI below (if `tally` is not on PATH: `python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally"`).
Output is short; add `--json` when you need to parse it. The project defaults to the current
repository; `-p <project>` or `--all` change that.

| goal | command |
|---|---|
| see open todos | `tally todo list` (`--tree` shows plan steps, `--all` every project) |
| what to do next | `tally todo next` |
| details + steps + measured work | `tally todo show <id>` |
| add | `tally todo add "<title>" [--priority 1-3] [--due fri\|+3d\|2026-10-20] [--notes "..."] --source claude` |
| add a step under a todo | `tally todo add "<title>" --parent <id> --source claude` |
| change status | `tally todo mark <id> doing\|blocked\|todo\|dropped [--note "why"]` |
| finish | `tally todo done <id> [--note "what was done"]` |
| import a plan | `tally todo import <plan.md>` |

## Rules

- Add todos only when the user asks, or when they come from a plan the user approved. Keep titles short and
  actionable; put details into `--notes`.
- Mark a todo `done` only when the work is actually finished and verified; otherwise leave it or mark it `blocked`
  with a note saying why.
- Do not delete or drop the user's todos unless they ask.
- `/tally:*` slash commands are for the user; you use the CLI.

## Working with plans (superpowers and similar)

- After a plan file is written (for example by superpowers `writing-plans` into `docs/plans/…md`), run
  `tally todo import <file>`. Headings like `### Task 3: …` become todos; `**Step N: …**` lines and `- [ ]`
  checklists under them become their steps. Re-running the import is safe: it only adds new items and marks
  checked items done.
- While executing a plan (for example superpowers `executing-plans` or `subagent-driven-development`), before
  each task run `tally todo next` and `tally todo mark <id> doing`; after the task is verified run
  `tally todo done <id>`. When every step of a task is done, the task and finally the plan are closed automatically.
- If you keep the plan file's checkboxes up to date instead, re-run `tally todo import <file>` to sync.
