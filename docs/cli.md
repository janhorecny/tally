# CLI reference

`tally` is the command line interface to the same database the agents write to. `./install.sh` links it to `~/.local/bin/tally`; without that link, run `python3 plugin/bin/tally` from the repository.

```
tally                       the logo and the most used commands
tally <command> [options]
tally <command> -h          help for one command
tally --version
```

Inside an agent session, the [in-agent commands](commands.md) are usually quicker. The CLI is for the terminal, scripts and exports.

**Session ids:** commands that act on "this session" (`start`, `resume`, `todo take`, and the defaults of `end`, `status` and `stats`) take `--session <id>`. Inside Claude Code it defaults to `$CLAUDE_CODE_SESSION_ID`. Outside an agent, pass it explicitly.

**JSON:** every command that prints a report or a list has `--json`, for scripts.

**Output:** messages start with `▍Tally ·` and errors with `▍Tally · ✗`; errors go to standard error with exit code 1. On a terminal the output is coloured. It is plain when piped, and `NO_COLOR=1` turns colours off everywhere.

## Contents

| tasks | |
|---|---|
| [`start`](#tally-start) | Start a task in a session. |
| [`end`](#tally-end) | End a task and print its report. |
| [`status`](#tally-status) | Live report of a task. |
| [`stats`](#tally-stats) | Detailed statistics of a task. |
| [`show`](#tally-show) | Report of any task. |
| [`list`](#tally-list) | List tasks. |
| [`resume`](#tally-resume) | Continue a task in a session. |
| [`rename`](#tally-rename) | Rename a task. |
| [`delete`](#tally-delete) | Delete a task. |
| [`export`](#tally-export) | Export tasks, a task, projects, a project, sessions, a session or a summary. |

| todos | |
|---|---|
| [`todo add`](#tally-todo-add) | Add a todo. |
| [`todo list`](#tally-todo-list) | List todos. |
| [`todo next`](#tally-todo-next) | The next todo to work on. |
| [`todo show`](#tally-todo-show) | A todo with its steps and measured work. |
| [`todo take`](#tally-todo-take) | Pick up a todo in a session. |
| [`todo done`](#tally-todo-done) | Mark a todo done and end its task. |
| [`todo mark`](#tally-todo-mark) | Set a todo's status. |
| [`todo edit`](#tally-todo-edit) | Change a todo. |
| [`todo rm`](#tally-todo-rm) | Delete a todo. |
| [`todo import`](#tally-todo-import) | Import a markdown plan. |

| projects and sessions | |
|---|---|
| [`project`](#tally-project) | A project's overall statistics, or set its mode. |
| [`projects`](#tally-projects) | All projects. |
| [`sessions`](#tally-sessions) | Every session of every agent. |
| [`session`](#tally-session) | Full report of one session. |

| settings and setup | |
|---|---|
| [`setup`](#tally-setup) | The onboarding questions. |
| [`settings`](#tally-settings) | Show or change settings (validated). |
| [`config`](#tally-config) | Show or set raw configuration keys. |
| [`dashboard`](#tally-dashboard) | Local web dashboard. |
| [`statusline`](#tally-statusline) | Status line segment. |
| [`install`](#tally-install) | Install into agents. |
| [`uninstall`](#tally-uninstall) | Remove from agents. |
| [`update`](#tally-update) | Update to the latest version. |
| [`doctor`](#tally-doctor) | Check the installation. |

---

## Tasks

### `tally start`

Start a task in a session. In-agent: [`/tally:start`](commands.md#tallystart).

```
tally start [name] [--session ID]
```

| | |
|---|---|
| `name` | Task name. Without it, the session's next prompt becomes the name. |
| `--session ID` | The session to count. Default: `$CLAUDE_CODE_SESSION_ID`. |

If the session already has an active task, it is ended first.

```bash
tally start "Nightly docs refresh" --session 3f9c2a1b-…
```

### `tally end`

End a task and print its final report. In-agent: [`/tally:end`](commands.md#tallyend).

```
tally end [id] [--note TEXT] [--session ID]
```

| | |
|---|---|
| `id` | Task to end. Default: the active task of the session, otherwise the only active task. |
| `--note TEXT` | Note stored with the task. |
| `--session ID` | Session whose active task is meant. Default: `$CLAUDE_CODE_SESSION_ID`. |

With several active tasks and no id, it stops and lists them.

```bash
tally end 12 --note "PR #214"
```

### `tally status`

Live report of a task. In-agent: [`/tally:status`](commands.md#tallystatus).

```
tally status [id] [--session ID] [--brief] [--json]
```

| | |
|---|---|
| `id` | Task to show. Default: as for [`end`](#tally-end). |
| `--brief` | Only the header lines: time, tokens, cost, models. |
| `--json` | The full report as JSON. |

### `tally stats`

Detailed statistics of a task: per prompt, averages, tools. In-agent: [`/tally:stats`](commands.md#tallystats).

```
tally stats [id] [--session ID] [--json]
```

| | |
|---|---|
| `id` | Task to show. Default: the active task of the session, otherwise the only active task, otherwise the latest task. |
| `--json` | The keys `task`, `time`, `totals`, `stats` and `prompts`. |

### `tally show`

Report of any task. In-agent: [`/tally:show`](commands.md#tallyshow).

```
tally show <id> [--prompts] [--json]
```

| | |
|---|---|
| `id` | Required. The task. |
| `--prompts` | Also list the task's prompts. |
| `--json` | The full report as JSON, including the prompts. |

```bash
tally show 12 --json > task-12.json
```

### `tally list`

List tasks, newest first: status, start, wall time, active time, tokens, cost. Alias: `tally ls`. In-agent: [`/tally:list`](commands.md#tallylist).

```
tally list [-n N | --all] [--json]
```

| | |
|---|---|
| `-n`, `--limit N` | Number of tasks. Default: 20. |
| `--all` | Every task. |
| `--json` | One object per task with its totals. |

### `tally resume`

Continue a task in a session; an ended task is reopened. In-agent: [`/tally:resume`](commands.md#tallyresume).

```
tally resume <id> [--session ID]
```

### `tally rename`

Rename a task. In-agent: [`/tally:rename`](commands.md#tallyrename).

```
tally rename <id> <name>
```

### `tally delete`

Delete a task. The usage stays in the database with its sessions; only the task and its report are removed.

```
tally delete <id> [-y]
```

| | |
|---|---|
| `-y`, `--yes` | Do not ask for confirmation. |

### `tally export`

Export for spreadsheets, time trackers and documents. The dashboard's *Export* menus and *Copy* buttons produce exactly the same content.

```
tally export [scope] [ref] [-f FORMAT] [-o FILE | --copy] [-p PROJECT] [--days N | --all] [--agent AGENT]
```

| scope | what | formats (the first is the default) |
|---|---|---|
| `tasks` (default) | Every task, or the tasks of one project with `-p` | `csv`, `json`, `md`, `text` |
| `task <id>` | One task | `md`, `text`, `json`, `csv` |
| `projects` | Every project with its last 30 days | `csv`, `json`, `md` |
| `project <name>` | One project; `csv` is its tokens per day | `md`, `json`, `csv` |
| `sessions` | Sessions of a period (`--days`, `--all`, `--agent`, `-p`) | `csv`, `json`, `md` |
| `session <id>` | One session (an id prefix is enough) | `md`, `text`, `json` |
| `summary` | Everything at a glance: totals, agents, projects, models, tasks | `md`, `json`, `csv` |

| option | |
|---|---|
| `-f`, `--format` | `csv` for spreadsheets, `md` for documents and issues, `json` for scripts, `text` for time trackers. |
| `-o`, `--output FILE` | Write to a file. Default: standard output. |
| `--copy` | Copy to the clipboard (`pbcopy`, `wl-copy`, `xclip` or `xsel`). |
| `-p`, `--project` | Tasks or sessions of one project. |
| `--days N`, `--all` | Sessions and summary: the last N days (default 30) or all history. |

- **Time sheets:** the tasks CSV has one row per task with `started`, `ended`, `wall_hm` and `active_hm` (hours:minutes), `wall_hours` and `active_hours` (decimal), `project`, tokens, `cost_usd` and the end `note`. The columns from before 0.7 come first, in the same order.
- **Copy and paste:** `text` is one line per task or session, for example `Fix login redirect loop (#3) · web-app · 2026-10-12 14:03–14:47 · active 31m 02s · wall 44m 10s · 4.21M tokens · $3.12 · PR #214`.
- **Markdown:** tables that render on GitHub, in Notion and in most editors.

```bash
tally export -o ~/Desktop/tasks.csv                     # every task, one row each
tally export task 12 -f text --copy                     # paste it into your time tracker
tally export project web-app -f md -o web-app.md        # a report of one project
tally export tasks -p web-app -f csv                    # the time sheet of one project
tally export sessions --days 7 -f md                    # last week's sessions as a table
tally export summary --copy                             # everything, as Markdown, to the clipboard
```

## Todos

`tally todo` without a subcommand prints its help. The project defaults to the git repository of the current directory.

### `tally todo add`

Add a todo. In-agent: [`/tally:todo`](commands.md#tallytodo).

```
tally todo add <text> [-p PROJECT] [--priority 0-3] [--due WHEN] [--notes TEXT] [--parent ID] [--source SOURCE] [--json]
```

| | |
|---|---|
| `text` | Title. The [quick syntax](commands.md#tallytodo) (`@project`, `!!`, `due:fri`, ` -- notes`) is recognised. |
| `-p`, `--project` | Project. |
| `--priority` | `0` none, `1` low, `2` medium, `3` high. |
| `--due WHEN` | Due date, as in the quick syntax: `today`, `fri`, `+3d`, `2026-10-20`, `20.10.` |
| `--notes TEXT` | Notes. |
| `--parent ID` | Make it a step of another todo. |
| `--source` | Who created it: `user` (default), `claude` or `import`. |

`--priority`, `--due` and `--notes` win over the quick syntax; `@project` in the text wins over `-p`.

```bash
tally todo add "Fix login redirect loop !! due:fri -- Safari only"
tally todo add "Write release notes" -p website --due tomorrow
```

### `tally todo list`

List todos. Alias: `tally todo ls`. In-agent: [`/tally:todos`](commands.md#tallytodos).

```
tally todo list [-p PROJECT | --all] [--status STATUS] [--tree] [--json]
```

| | |
|---|---|
| `-p`, `--project` | Project. |
| `--all` | Every project. |
| `--status` | `open` (default: todo, doing, blocked), `done` (done and dropped), `all`, or one status. |
| `--tree` | Show plan steps under their todos. |

### `tally todo next`

The next todo to work on: one in progress first, then by priority and due date.

```
tally todo next [-p PROJECT | --all] [--json]
```

### `tally todo show`

A todo with its notes, steps, and the tokens, cost and time of every task that worked on it.

```
tally todo show <id> [--json]
```

### `tally todo take`

Pick up a todo: status `doing`, and a task linked to it starts in the session. In-agent: [`/tally:take`](commands.md#tallytake).

```
tally todo take <id> [--session ID]
```

### `tally todo done`

Mark a todo done and end its active tasks. In-agent: [`/tally:done`](commands.md#tallydone).

```
tally todo done <id> [--note TEXT]
```

### `tally todo mark`

Set a todo's status; no task is started or ended. In-agent: [`/tally:mark`](commands.md#tallymark).

```
tally todo mark <id> <todo|doing|blocked|done|dropped> [--note TEXT]
```

### `tally todo edit`

Change a todo.

```
tally todo edit <id> [--title TEXT] [--notes TEXT] [--project NAME] [--priority 0-3] [--due WHEN]
```

`--due ''` clears the due date.

### `tally todo rm`

Delete a todo and its steps. Tasks that worked on it are kept.

```
tally todo rm <id> [-y]
```

### `tally todo import`

Create or update todos from a markdown plan.

```
tally todo import <file> [-p PROJECT] [--source SOURCE] [--json]
```

- `### Task N: …` headings (and `## 1. …`, `Phase N`) become todos; `**Step N: …**` lines and `- [ ]` / `- [x]` checklists under them become their steps.
- Re-importing the same file is safe: new items are added, checked items are marked done, nothing is reopened.

```bash
tally todo import docs/plans/2026-10-12-export.md
```

## Projects and sessions

### `tally project`

A project's overall statistics, or set its tracking mode. In-agent: [`/tally:project`](commands.md#tallyproject).

```
tally project [name] [--set track|tasks|off] [--days N] [--series N] [--json]
```

| | |
|---|---|
| `name` | Project. Default: the git repository of the current directory. |
| `--set` | Set the mode instead of showing statistics. |
| `--days N` | Only the last N days. |
| `--series N` | Days in the daily series. Default: 14. |

### `tally projects`

All projects with their mode and totals. In-agent: [`/tally:projects`](commands.md#tallyprojects).

```
tally projects [--json]
```

### `tally sessions`

The tracked sessions of every agent, with or without a task (setting `sessions.scope`; the last line says how many are not tracked). In-agent: [`/tally:sessions`](commands.md#tallysessions).

```
tally sessions [--days N | --all] [--agent claude|codex|pi] [-p PROJECT] [--limit N] [--no-refresh] [--json]
```

| | |
|---|---|
| `--days N` | Sessions active in the last N days. Default: 30. |
| `--all` | All history. |
| `--agent` | Only this agent. |
| `-p`, `--project` | Only this project. |
| `--limit N` | Rows to show. Default: 40; `0` shows all. |
| `--no-refresh` | Show the index as it is, without reading new transcript data. |
| `--json` | Sessions plus totals per agent. |

Before listing, new transcript data of the period is read into the sessions index (`~/.claude/tally/sessions.db`). The first run over a long period can take a few seconds and shows progress; later runs read only what is new. See [Sessions](usage.md#sessions).

```bash
tally sessions --days 7
tally sessions --all --agent codex
tally sessions --json --limit 0 > sessions.json
```

### `tally session`

Full report of one session, with or without a task: agent tree, models, per-prompt breakdown. Works for Claude Code, Codex and pi.

```
tally session <id | id prefix | path/to/transcript.jsonl> [--json]
```

The id prefix is the first column of `tally sessions`.

```bash
tally session 3f9c2a1b
```

## Settings and setup

### `tally setup`

Ask the onboarding questions again. The installer asks them once, on the first install.

```
tally setup
```

```
▍Tally · Setup · Enter keeps the value in [brackets]
  Store prompt text?          full / preview / off       [full]
  Logging detail?             summary / prompts / calls  [prompts]
  New projects:               ask / track / tasks        [ask]
  Sessions overview reads:    enabled / all              [enabled]
  Start a task automatically with each session? y/N
  Show open todos at session start? Y/n
  Tell me when a new version is out? Y/n
  Add Tally to Claude Code's status line? Y/n
```

| question | setting |
|---|---|
| Store prompt text? | `log.prompts` |
| Logging detail? | `log.depth` |
| New projects | `projects.default_mode` |
| Sessions overview reads | `sessions.scope` |
| Start a task automatically … | `auto_start` (`session` or `off`) |
| Show open todos … | `todos.reminder` |
| Tell me when a new version is out? | `updates.check` |
| Add Tally to Claude Code's status line? | `statusLine` in `~/.claude/settings.json` |

- **Answers:** the value in brackets is the current one, so Enter keeps it. A unique prefix is enough (`pr` for `prompts`), and a wrong answer is asked again. Everything is saved at the end; Ctrl-D quits without saving.
- **Status line:** asked only when Claude Code is installed and has no status line yet. An existing status line is never changed; setup shows how to add Tally to it instead. Before writing, `settings.json` is backed up as `settings.json.bak-tally`. `tally uninstall` removes the status line again if it is still the one setup added.
- **Needs a terminal:** in scripts, use [`tally settings`](#tally-settings).

### `tally settings`

Show or change settings. Values are validated. In-agent: [`/tally:settings`](commands.md#tallysettings).

```
tally settings [key [value]]
```

The keys are listed in [Configuration](configuration.md).

```bash
tally settings
tally settings log.prompts preview
```

### `tally config`

Show or set raw keys of `~/.claude/tally/config.json`, including those without validation (`pricing`, `dashboard_host`). A value is parsed as JSON when possible.

```
tally config [key [value]]
```

```bash
tally config                                    # the whole file
tally config pricing '{"my-proxy-model": {"input": 1.0, "output": 4.0}}'
```

### `tally dashboard`

Local web dashboard. Alias: `tally serve`. In-agent: [`/tally:dashboard`](commands.md#tallydashboard).

```
tally dashboard [--open] [--background] [--stop] [--idle-minutes N] [--host HOST] [--port PORT]
```

| | |
|---|---|
| `--open` | Open the browser. |
| `--background` | Start detached (or reuse a running dashboard) and return. Default: run in the foreground until Ctrl+C. |
| `--stop` | Stop the running dashboard. |
| `--idle-minutes N` | Stop after N minutes without an open page. A dashboard started in the background (`--background`, `/tally:dashboard`) uses `dashboard_idle_minutes`, 120 by default; in the foreground it runs until you stop it. |
| `--host`, `--port` | Default: `dashboard_host` and `dashboard_port` from the configuration (127.0.0.1:8765). |

If a dashboard of the same version is already running, it is reused; one of an older version is stopped and replaced. An open page checks in every minute, so the idle stop only happens when nobody has it open. *Settings → Dashboard* in the page has a *Stop* button as well.

### `tally statusline`

One line for a status line. Reads Claude Code's status line JSON on standard input. See [Status line](usage.md#status-line).

```
tally statusline [--width N] [--session ID]
```

| | |
|---|---|
| `--width N` | Maximum length of the task name. Default: 28. |

It prints, depending on the state:

- an active task: `▶ #3 Fix login redirect loop · 4.21M tok · $3.12 · 44m 10s · 📋 2`;
- no task, open todos: `📋 3 todos · 1 overdue`;
- a project that is not set up yet: `▍Tally · new project: /tally:project track|tasks|off`;
- otherwise nothing.

### `tally install`

Install into agents. `install.sh` (and the one-line installer) runs this and also links the CLI to `~/.local/bin/tally`. The first install also asks the [setup](#tally-setup) questions (not with `--yes` or without a terminal), and each newly installed agent shows a one-time welcome at its next session start.

```
tally install [--agents claude,codex,pi | --all] [-y]
```

| | |
|---|---|
| `--agents LIST` | Comma-separated agents. |
| `--all` | Every supported agent. |
| `-y`, `--yes` | No questions: every detected agent. |

Without options it asks for each detected agent. `./install.sh --no-cli` skips the CLI link.

### `tally uninstall`

Remove from agents. History is kept.

```
tally uninstall [--agents LIST]
```

Without `--agents` it removes Tally from every agent it is installed in, and from Claude Code's status line if setup added it. `./uninstall.sh` also removes the CLI link and stops the dashboard; `./uninstall.sh --purge` also deletes the data directory.

### `tally update`

Update to the latest version.

```
tally update [--check]
```

| | |
|---|---|
| (none) | Pull the latest version into the git checkout Tally runs from (fast-forward only), install it into every agent Tally is in, restart a running dashboard, and list what changed since your version. A checkout that is already current is installed into the agents when Claude Code still runs an older copy. |
| `--check` | Only say whether a new version is out (asks GitHub now). |

A checkout with local changes is left alone. Installed from Claude Code's plugin manager, it runs `claude plugin marketplace update tally` and `claude plugin update tally@tally` instead. Afterwards run `/reload-plugins` in Claude Code, `/reload` in pi, and restart Codex.

Tally also checks on its own once a day (setting `updates.check`) and mentions a new version at session start, below CLI commands and in the dashboard.

### `tally doctor`

Check the installation: version, data directory, tasks, the last hook that fired, sessions per agent, the transcripts directory, the state per agent, whether setup has run, Claude Code's status line, where Tally is installed and whether an update is out, and logged errors. See [Troubleshooting](troubleshooting.md).

```
tally doctor
```

### Internal commands

`tally hook <event> [--agent claude|codex|pi]` and `tally slash <command> …` are run by the agent integrations. You do not need them.
