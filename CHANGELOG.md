# Changelog

## 0.11.3 (2026-10-13)

- **After an update, restart Claude Code:** `tally update`, the installer and the docs said "restart Claude Code or /reload-plugins". `/reload-plugins` keeps the hooks of the old version next to the new ones, so both run; they now ask for a full restart (`claude --continue` resumes the conversation). `/reload-plugins` stays fine for a first install.

## 0.11.2 (2026-10-13)

- **Tally says when it runs twice:** if a prompt's hook was also handled by an older Tally version in the same session (its hooks stay registered after `/reload-plugins` until Claude Code restarts), the answer ends with `⚠ Tally ran twice … Restart Claude Code fully`. Two hooks of the same version stay quiet: they already run each command once.
- **Checked for every command that changes something** (start, new, end, resume, rename, todo, take, do, done, mark, project, settings): with the hook run twice at once, each has one effect and both runs give the same answer.

## 0.11.1 (2026-10-13)

- **No second todo from an old hook:** a Claude Code session started before 0.10.1 keeps its old hooks after `/reload-plugins`, and they add the todo without the once-per-prompt guard. The same todo (title, project, parent) added again within 5 seconds is now the same todo, from `/tally:todo`, the CLI and the dashboard (a double click too). Plan imports are not affected. A full restart of Claude Code removes the old hooks.

## 0.11.0 (2026-10-13)

### A kanban board

- **Todo → Doing → Blocked → Done:** the *Todos* tab is a kanban board in the order work flows. Drag a card to another column to change its status, or within a column to change its place; the order is kept (`todos.rank`, schema v8).
- **Cards** show the priority as a coloured edge, the due date (red when overdue), the project, steps, notes and what the work cost, and stay compact.
- **Click a card** to open it: edit everything, tick or add steps, see what it cost, copy `/tally:do`, delete. A finished todo offers neither `/tally:do` nor the hint to measure it.
- **⋯ on a card** moves, copies `/tally:do` or deletes without dragging; **＋ Add a card** adds to a column; **a filter** above the board (`/`) narrows the cards.
- **API:** `POST /api/todos` takes `status` and `parent` (a step); `POST /api/todos/<id>` takes `order`, the ids of the column after a drop, and answers 404 for a missing todo.

### Fixed

- **Two hooks opening a new database at once:** switching it to WAL failed with "database is locked" (SQLite does not wait there); it is tried again for a moment.
- **`tally todo add Fix login -- check Safari`:** the shell's `--` was swallowed and the notes became part of the title.

## 0.10.1 (2026-10-13)

- **A command runs once per prompt:** when an agent calls the prompt hook twice for one prompt (hooks registered twice, for example right after `/reload-plugins` with a new version), `/tally:todo` added the todo twice. The second call now waits for the first and gives the same answer. The same command typed again after 3 seconds runs again.

## 0.10.0 (2026-10-13)

### Terminal

- **Arrow keys in `tally setup`:** each question is a choice on one line (← → or ↑ ↓, Enter confirms, Esc skips setup). A line under the open question explains what the choices mean. Typing still works: first letters, the number of a choice, `y` / `n`. Typed answers no longer break on an arrow key pressed by habit.
- **Setup offers the agents:** `tally setup` lists Claude Code, Codex and pi and asks about each one that is found but not installed; the installer's own agent question uses the arrow keys too.
- **`tally settings` in a terminal** is an editor: ↑ ↓ choose a setting, ← → change it (saved at once), type a number, q quits with a summary. Without a terminal, or with `--list`, a list by group with each setting's label, value, other values and what it does; `/tally:settings` shows the same list.
- **Help:** `tally --help` groups the commands (tasks, todos, projects and sessions, settings and maintenance) and leaves the internal ones out; every command has a description, every option an explanation, and the main ones have examples.
- **Errors:** wrong arguments are explained in Tally's style with a hint (`tally sessions -h shows the options`); an unknown command suggests the closest one (`did you mean tally sessions?`); Ctrl-C stops any command quietly (exit 130) instead of a traceback.
- **Fixed:** `tally todo` alone printed the help of `tally update`; `tally uninstall --agents <unknown>`, `tally todo edit` of a missing todo and `tally todo import` of a missing file ended with a traceback; `tally resume`, `tally settings <unknown>` and `tally config <unknown>` exited 0 on errors; `tally export task` without a number said "task None not found"; `tally doctor` pointed at `./install.sh` for an agent that is found but not installed.
- **`tally project` and `tally projects`** (and `/tally:project`, `/tally:projects`) show the dashboard's numbers: the last 30 days of the tracked sessions, what Tally keeps, and the projects that are not tracked with how many sessions they have. A project Tally knows nothing about is an error. `--json` gives the dashboard's data.

### Dashboard

- **Projects:** *Tracked* and *Not tracked* are two separate cards; the second shows how many sessions and how much transcript each project has, and its mode. A search box above them filters by name (`/` jumps to it, Enter opens the only match).
- **A project has its own page** (`#projects/<name>`, with *← All projects* back) instead of a detail under the list; the project selector in the header opens it on the *Projects* tab.
- **Settings** take their groups and labels from the server, the same as `tally settings`.

### Docs

- **Every table has a header**, no code block scrolls sideways, commands to copy are in code blocks, and the long sections of the README are split into steps and subsections.
- **Exports:** the Markdown key-value tables of a task, a project and the summary have a header (`Item | Value`).

## 0.9.3 (2026-10-13)

- **`tally update` in a checkout that is already current** (one you pulled or committed to by hand) installs its version into the agents when Claude Code still runs an older copy, instead of only saying it is up to date.

## 0.9.2 (2026-10-13)

- **Version check:** asks GitHub for the latest release instead of reading the manifest on `main`, which GitHub's CDN serves up to five minutes old; the manifest stays the fallback when the API is unavailable. You hear about published releases only, right after they are out.

## 0.9.1 (2026-10-13)

- **Installed from Claude Code's plugin manager:** `tally update` and the new-version notice no longer point at Claude Code's own clone of the marketplace; they say `claude plugin marketplace update tally && claude plugin update tally@tally`, and `tally update` runs exactly that.

## 0.9.0 (2026-10-13)

The repository is public.

### Install and update

- **One-line install:** `curl -fsSL https://raw.githubusercontent.com/janhorecny/tally/main/install.sh | bash` downloads Tally to `~/.local/share/tally` (`TALLY_DIR`) and runs the installer from there; questions still work, options go after `bash -s --`. `install.sh` in a clone works as before. A missing `~/.local/bin` on `PATH` comes with the line to add it.
- **`tally update`:** pulls the checkout Tally runs from (fast-forward only, never over local changes), installs the new version into every agent Tally is in, restarts a running dashboard and lists the changes since your version from the changelog. Installed from Claude Code's plugin manager, it updates the plugin there.
- **New versions are announced:** once a day a background process fetches the version of the plugin manifest on GitHub (nothing about you is sent). A newer one is mentioned in the session start message of every agent and below CLI commands, each at most once a day, and as a ⬆ chip in the dashboard header that copies `tally update`. `tally update --check` checks now; `tally doctor` shows the result and where Tally is installed.
- **`updates.check`:** on by default; `tally setup` asks about it. `TALLY_NO_UPDATE_CHECK=1` turns it off too.
- **Claude Code plugin manager:** `/plugin marketplace add janhorecny/tally` and `/plugin install tally@tally` install the Claude Code part without the installer.
- **Uninstall** of a one-line install also deletes `~/.local/share/tally`, and `uninstall.sh` stops only the dashboard on the configured port (`tally dashboard --stop`), not every process named like one.

### Development

- **`scripts/release.sh`:** checks that the versions and the changelog agree, runs the checks, pushes `main`, tags `vX.Y.Z` and creates the GitHub release with the changelog section.

## 0.8.0 (2026-10-13)

### Only the sessions you choose

- **Opt-in sessions overview:** the overview (`/tally:sessions`, `tally sessions`, the dashboard's *Sessions* and *Projects*, exports) reads only projects set to `track` or `tasks`, sessions a task counted, and sessions added one by one. A fresh install starts empty instead of reading every transcript on the machine; helper agents (claude-mem observers, temporary directories) stay out until you add them.
- **Not tracked, but offered:** the other transcripts are only described (session, project, title, from the start and end of the file) and counted. `/tally:sessions` ends with `+ N sessions in M projects not tracked`; the summary and session exports say so too.
- **`sessions.scope`:** `enabled` (default) or `all` (every session except `off` projects, as before). `tally setup` asks about it.
- **An index from 0.7** keeps what it read: switching a project to `track` shows its sessions at once, without reading them again.

### Dashboard

- **Projects in two groups:** *Tracked* with sessions, tokens and cost; *Not tracked* with how many sessions each has. Choosing `track` or `tasks` reads the project's sessions in the background, with all the history the agents still keep, and the table follows the progress.
- **Project detail of an untracked project:** *Track every session*, *Only tasks* or *Off*, and its sessions with an **Add** button each.
- **Sessions → Not tracked:** every untracked session of the period with an **Add** button; an added session is read at once and gets the chip *added*. **Remove** in its detail takes it out again. `POST /api/sessions/<id>` with `{"action": "add" | "remove"}`.
- **Welcome:** before anything is tracked, the Tasks tab says how many sessions your agents ran and where to choose.

## 0.7.4 (2026-10-13)

- **Stopping the dashboard:** `/tally:dashboard stop`, `tally dashboard --stop`, and a *Stop the dashboard* button in *Settings → Dashboard* (`POST /api/shutdown`, same-origin like every write).
- **Idle stop:** a dashboard started in the background stops by itself after `dashboard_idle_minutes` (new setting, default 120, `0` = never) without an open page; an open page checks in every minute. `/tally:dashboard` starts it again.

## 0.7.3 (2026-10-13)

- **Dashboard tables:** the selected and hovered rows had no inner padding at their edges, so the highlight started right at the text. Edge cells now keep 10px inside the row, and tables reach into the section padding so their text stays aligned with the titles.
- **One inset:** card headers, stat tiles, the task list and the sessions table use the same 20px from the card edge as the sections, so titles, tiles, table headers and rows line up.

## 0.7.2 (2026-10-13)

- **Dashboard processes on Linux** are found through `/proc` (exact arguments, no `ps` needed), so a dashboard of an older version is replaced there too.

### Development

- **Checks run locally:** `scripts/check.sh` runs the tests, lint, byte-compile, the plugin manifests and the links in the docs in about 10 s; `--all` adds Python 3.8, 3.11 and 3.13 (uv) and Linux in Docker, about a minute. `.githooks/pre-push` runs the quick checks before every push (`git config core.hooksPath .githooks`).
- **GitHub Actions off:** the workflow is disabled and runs only by hand, on Linux. Its macOS jobs were billed at ten times the minutes: about 1,170 of the 1,227 minutes this repository used.

## 0.7.1 (2026-10-13)

- **Exports:** counts in Markdown exports have thousands separators (`2,433 prompts · 76,786 API calls`).

## 0.7.0 (2026-10-13)

### Export and copy

- **`tally export [scope] [ref]`:** `summary`, `tasks`, `task <id>`, `projects`, `project <name>`, `sessions` and `session <id>`, as CSV, JSON, Markdown or one line of text, to standard output, a file (`-o`) or the clipboard (`--copy`). `tally export --format csv` works as before.
- **Time sheets:** the tasks CSV adds `project`, `todo_id`, `prompts`, wall and active time as `h:mm` and as decimal hours, and the end `note`, after the columns it had before.
- **Dashboard:** an *Export* menu in the header (summary, all tasks, projects, sessions, the chosen project), on the task list, on a task, on projects and a project, and on sessions; downloads and copy to the clipboard. A task and a session have a **Copy** button that puts one line for a time tracker on the clipboard. `/api/export` serves the same files.

### Dashboard

- **Own addresses for projects:** `#projects/<name>` opens that project; selecting one updates the address.
- **Which project:** when one project is chosen (in the header or with *Sessions of this project*), Tasks, Todos and Sessions show a bar with its name, a link to its details and *All projects*; the sessions table drops the repeated project column.
- **Projects table:** the tracking column is left-aligned next to the project, the project column takes the free space, and the last column keeps clear of the scroll bar.
- **Narrow screens:** the task and project headers move their buttons under the title instead of squeezing it, and menus open on screen.

## 0.6.3 (2026-10-13)

Design pass over every view in light and dark at desktop, 760 px and 390 px width, on real data.

- **Header:** the theme is one icon button (monitor: follows the system, sun: light, moon: dark) and Refresh is an icon button that spins while it reloads. On phones the project filter and the buttons span the full width.
- **Tables on small screens:** Sessions and Projects hide secondary columns (models, share bars, span, calls, agent, last activity, tasks, todos) step by step, and long project names are shortened, so the session title, tokens and cost stay readable and nothing scrolls sideways. The tracking mode stays selectable on phones.
- **Sessions toolbar:** on phones the period and agent filters span the full width.

## 0.6.2 (2026-10-13)

- **Dashboard, session and task detail:** the cost tile includes Claude Code's own unlogged calls, like the sessions list (it showed $24.00 where the list showed $24.70), with the estimate named in the hint and the note below. A session without subagents says *main session only* instead of *0 agents*.
- **Replacing old dashboards:** finding a pre-0.6 dashboard by its process list now matches only a Python interpreter running the `tally` launcher with `dashboard`. Before, any process whose command line mentioned both words could be stopped, such as a shell running `tally dashboard` in a longer command.

## 0.6.1 (2026-10-13)

Reviewed against a real installation: no tasks yet, 750 sessions in 54 projects.

- **First run:** with no tasks, the Tasks tab explains what a task is, shows the start command for each agent and what the agents used in the last 30 days, with links to Sessions and Todos, instead of an empty list and a lone *Select a task*. An empty todo board explains how to add or import todos.
- **Projects:** every project with sessions in the last 30 days is listed, most used first, with sessions, tokens and cost from the sessions index whatever its tracking mode. Projects Tally has not set up yet are marked *new* and can be set up from the table. The detail shows the same 30-day numbers, a daily chart of all sessions, and what Tally keeps for good. Before, an untracked project showed zeros and unknown projects were missing.
- **Session titles:** a session without an AI title is named after its first prompt instead of its id (dashboard and `tally sessions`).
- **Money:** amounts from $1,000 are shown as `$9,052` (dashboard and CLI).
- **Layout:** the projects table scrolls with a sticky header, so the detail stays in view.

## 0.6.0 (2026-10-13)

### Dashboard

- **New look:** an app icon and the version in the header, cards with consistent spacing, one date format (`7 Oct, 07:30`, `Today`, `3 min ago`) whatever the browser's language, higher text contrast in the light theme, visible keyboard focus, and layouts that fit down to phone width.
- **Tasks:** filter by name and status; the detail can **rename** a task, **end** an active one and **delete** an ended one, with an inline confirmation. Long paths no longer crowd the header, and large workflow phases start collapsed.
- **Todos:** **edit** title, priority, due date, project and notes, **delete** a todo, and copy its `/tally:do` command. Overdue items stand out; dropped ones are struck through.
- **Projects:** selecting a project no longer changes the global filter; the 30-day chart has date ticks and follows the window width.
- **Sessions:** rows open with the keyboard too, and the detail can be closed.
- **Settings:** grouped, with readable names next to the keys.
- **Feedback:** saved changes and errors appear as notifications, and the header shows when the dashboard server is not reachable.
- **Updates:** `/api/health` reports version and process id; `/tally:dashboard` and `tally dashboard` replace a dashboard left running by an older version instead of reusing it.

### CLI

- **Banner:** a figlet wordmark with a gold-to-orange gradient, the tagline and the version, on `tally`, `tally --help`, `tally setup` and the installer. `tally setup` highlights the current value of each question.

## 0.5.0 (2026-10-13)

### Onboarding

- **`tally setup`:** a few questions with defaults: prompt storage (`log.prompts`), logging detail (`log.depth`), what new projects get, automatic tasks, todo reminders, and Claude Code's status line. The first `./install.sh` asks them once; `--yes` or no terminal keeps the defaults. Answers are saved only at the end (Ctrl-D quits without changes), and running it again offers the current values.
- **Status line:** added to `~/.claude/settings.json` only when Claude Code has none (with a `settings.json.bak-tally` backup). An existing status line is left alone, with instructions for ccstatusline or your own script. `tally uninstall` removes it again if it is unchanged.
- **New setting `projects.default_mode`** (`ask`, `track`, `tasks`): a new project gets this mode and a one-line notice instead of the question.
- **Welcome:** the first session of each newly installed agent shows a short introduction (0 tokens).

### Branding

- **One message shape everywhere:** `▍Tally · <mark> <what happened>`, details, then a `→` line with the next commands. Errors start with `▍Tally · ✗` and say what to do instead (`Task #12 not found → /tally:list shows your tasks`). The same in Claude Code, Codex, pi and the CLI.
- **CLI:** colours on a terminal (the logo's orange for the brand, red errors, dim hints; `NO_COLOR` turns them off), errors on standard error, and `tally` alone shows the tally-mark logo with the most used commands. `tally doctor` also shows the setup and the status line.
- **Dashboard:** the logo in the header, a favicon and the title *Tally*. Codex shows *Tally* while a hook runs.

### Fixes

- `/tally:project <name> <bad mode>` raised an error inside the hook instead of answering.
- `tally rename` of a task that does not exist failed with a traceback.

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
