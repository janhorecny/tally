# Command reference

These commands run inside an agent session. The same commands exist in every agent; only the way you type them differs:

| | Claude Code | Codex | pi |
|---|---|---|---|
| form | `/tally:<cmd> [args]` | `tally:<cmd> [args]`, typed as a prompt | `/tally <cmd> [args]`, or `tally:<cmd> [args]` as a prompt |
| example | `/tally:start Fix login` | `tally:start Fix login` | `/tally start Fix login` |

Tally answers every command locally, so **they cost no tokens**. The exceptions are [`new`](#tallynew) and [`do`](#tallydo), which hand work to the model. In Claude Code the answer appears under the line `UserPromptSubmit operation blocked by hook:`, which is expected.

For scripts and the terminal, see the [CLI reference](cli.md).

**Syntax:** `<arg>` is required, `[arg]` is optional, and `a|b` means one of them. A task or todo `id` can be written as `12` or `#12`.

## Contents

| tasks | |
|---|---|
| [`start [name]`](#tallystart) | Start a task in this session. |
| [`new <prompt>`](#tallynew) | Start a task and send the prompt to the model. |
| [`status [id]`](#tallystatus) | Live totals of the current task. |
| [`stats [id]`](#tallystats) | Detailed statistics: per prompt, averages, tools. |
| [`end [id] [note]`](#tallyend) | End a task and show its final report. |
| [`list [n\|all]`](#tallylist) | Recent tasks. |
| [`show [id]`](#tallyshow) | Full report of any task. |
| [`resume <id>`](#tallyresume) | Count a task in this session too; reopens an ended task. |
| [`rename [id] <name>`](#tallyrename) | Rename a task. |

| todos | |
|---|---|
| [`todo <text>`](#tallytodo) | Add a todo. |
| [`todos [project\|all\|done]`](#tallytodos) | List todos. |
| [`take <id>`](#tallytake) | Pick up a todo and start measuring it. |
| [`do <id>`](#tallydo) | Pick up a todo and let the model work on it. |
| [`done [id] [note]`](#tallydone) | Finish a todo and end its task. |
| [`mark <id> <status> [note]`](#tallymark) | Set a todo's status. |

| projects and sessions | |
|---|---|
| [`project [name] [track\|tasks\|off]`](#tallyproject) | A project's overall statistics, or set its tracking mode. |
| [`projects`](#tallyprojects) | All projects. |
| [`sessions [days\|all] [agent] [here]`](#tallysessions) | Every session of every agent. |

| other | |
|---|---|
| [`settings [key [value]]`](#tallysettings) | Show or change settings. |
| [`dashboard`](#tallydashboard) | Open the local dashboard. |
| [`help`](#tallyhelp) | Show the help text. |

---

## Tasks

### `/tally:start`

Start a task in this session.

```
/tally:start [name]
```

| argument | |
|---|---|
| `name` | Task name. Without it, your next prompt becomes the name. |

- From now on, the main conversation, every subagent and every workflow agent of this session count towards the task.
- Your next prompt is stored as the task's initial prompt.
- If this session already has an active task, that task is ended first and its final report is shown.
- The task follows `/clear` automatically. To continue it in another terminal, use [`resume`](#tallyresume).

```
/tally:start Fix login redirect loop
/tally:start
```

CLI: [`tally start`](cli.md#tally-start)

### `/tally:new`

Start a task and send the prompt to the model as its first instruction.

```
/tally:new <prompt>
```

| argument | |
|---|---|
| `prompt` | Required. Stored as the initial prompt; the task name is derived from it. |

- This command is not blocked: the prompt goes on to the model, so its turn costs tokens as usual.
- Like `start`, it ends this session's active task first.
- pi in print mode (`pi -p`): use `tally:new …` as the prompt, because `/tally new` cannot hand work to the model there.

```
/tally:new Find why the nightly import is slow and fix it
```

### `/tally:status`

Live totals of a task.

```
/tally:status [id]
```

| argument | |
|---|---|
| `id` | Task to show. Default: the active task of this session. |

- Shows the report: time, tokens, cost, models and the agent tree. See [Reading the report](usage.md#reading-the-report).
- With no active task in this session, it lists the active tasks of other sessions.

```
/tally:status
/tally:status 12
```

CLI: [`tally status`](cli.md#tally-status)

### `/tally:stats`

Detailed statistics of a task.

```
/tally:stats [id]
```

| argument | |
|---|---|
| `id` | Task to show. Default: the active task of this session, otherwise the latest task. |

Adds the following to the report:

- **Averages:** prompts, automatic turns, subagents, workflows, average and longest turn, cache hit rate, output tokens per active minute.
- **Per prompt:** tokens, output, cost, duration, API calls, subagents and tool calls of every prompt.
- **Tools:** calls and errors per tool, including MCP tools. Needs [`log.depth`](configuration.md) `calls`.

```
/tally:stats
/tally:stats 12
```

CLI: [`tally stats`](cli.md#tally-stats)

### `/tally:end`

End a task and show its final report.

```
/tally:end [id] [note]
```

| argument | |
|---|---|
| `id` | Task to end. Default: the active task of this session. A first word that is a number is taken as the id. |
| `note` | Free text stored with the task, for example a PR number. |

- The report is frozen when the task ends. Usage written later, for example by background agents that are still running, is not counted.
- Ending a task that has already ended shows its report again.

```
/tally:end
/tally:end done, PR #214
/tally:end 12 abandoned
```

CLI: [`tally end`](cli.md#tally-end)

### `/tally:list`

Recent tasks with their status, start, wall time, active time, tokens and cost.

```
/tally:list [n|all]
```

| argument | |
|---|---|
| `n` | Number of tasks. Default: 15. |
| `all` | Every task. |

```
/tally:list
/tally:list all
```

CLI: [`tally list`](cli.md#tally-list)

### `/tally:show`

Full report of any task.

```
/tally:show [id]
```

| argument | |
|---|---|
| `id` | Task to show. Default: the active task of this session, otherwise the latest task. |

```
/tally:show 12
```

CLI: [`tally show`](cli.md#tally-show)

### `/tally:resume`

Count a task in this session too.

```
/tally:resume <id>
```

| argument | |
|---|---|
| `id` | Required. The task to continue. |

- **Active task:** this session now counts towards it as well, for example from a second terminal or after a restart.
- **Ended task:** it is reopened. The time between ending and reopening is not counted.
- Wall time keeps running from the task's original start.
- If this session has a different active task, that one is ended first.

```
/tally:list
/tally:resume 12
```

CLI: [`tally resume`](cli.md#tally-resume)

### `/tally:rename`

Rename a task.

```
/tally:rename [id] <name>
```

| argument | |
|---|---|
| `id` | Task to rename. Default: the active task of this session. A first word that is a number is taken as the id. |
| `name` | Required. The new name. |

```
/tally:rename Fix OAuth redirect loop
/tally:rename 12 Billing migration, part 1
```

CLI: [`tally rename`](cli.md#tally-rename)

## Todos

### `/tally:todo`

Add a todo.

```
/tally:todo <text>
```

`text` is the title. These markers anywhere in it are recognised and removed from the title:

| marker | example | meaning |
|---|---|---|
| `@name` | `@website` | Project. Default: the git repository you are in. |
| `!`, `!!`, `!!!` | `!!` | Priority: low, medium or high. |
| `due:<when>` | see below | Due date, at the end of that day. |
| ` -- ` | `Fix it -- only on Safari` | Everything after it becomes the notes. |

`<when>` can be:

- `today`, `tomorrow`, or the Slovak `dnes`, `zajtra`;
- a weekday: `mon` … `sun`, or the Slovak `po` … `ne`;
- a relative time: `+3d`, `+2w`, `+4h`;
- a date: `2026-10-20`, `20.10.`, `20.10.2026`.

```
/tally:todo Fix login redirect loop !! due:fri -- Safari only
/tally:todo Write release notes @website due:zajtra
```

CLI: [`tally todo add`](cli.md#tally-todo-add)

### `/tally:todos`

List todos.

```
/tally:todos [project|all|done]
```

| argument | |
|---|---|
| (none) | Open todos of this project: `todo`, `doing` and `blocked`. |
| `project` | Open todos of another project (`website` or `@website`). |
| `all` | Open todos of every project. |
| `done` | Done and dropped todos of this project. |

Each todo shows its status, priority, due date and plan progress. [`tally todo show <id>`](cli.md#tally-todo-show) adds the tokens, cost and time measured on it.

```
/tally:todos
/tally:todos all
```

CLI: [`tally todo list`](cli.md#tally-todo-list)

### `/tally:take`

Pick up a todo and start measuring it. You write the prompt.

```
/tally:take <id>
```

| argument | |
|---|---|
| `id` | Required. The todo. |

- The todo's status becomes `doing`, and a task linked to the todo starts in this session.
- A `done` or `dropped` todo is reopened.
- If this session has another active task, it is ended first.
- Work measured over several takes adds up on the todo.

```
/tally:take 3
```

CLI: [`tally todo take`](cli.md#tally-todo-take)

### `/tally:do`

Pick up a todo and let the model start on it right away.

```
/tally:do <id>
```

| argument | |
|---|---|
| `id` | Required. The todo. |

- The same as [`take`](#tallytake), and the todo (title, notes, steps) is handed to the model as its instruction.
- This command is not blocked, so the model's turn costs tokens as usual.
- pi in print mode (`pi -p`): use `tally:do 3` as the prompt, because `/tally do` cannot hand work to the model there.

```
/tally:do 3
```

### `/tally:done`

Finish a todo and end its task.

```
/tally:done [id] [note]
```

| argument | |
|---|---|
| `id` | The todo. Default: the todo of this session's active task. |
| `note` | Appended to the todo's notes, and stored as the end note of its task. |

- Every active task of the todo is ended, and the final report is shown, with the total over all tasks that worked on the todo.
- When every step of a plan item is done, the item, and then the plan, are closed automatically.

```
/tally:done
/tally:done 3 shipped in 2.4.1
```

CLI: [`tally todo done`](cli.md#tally-todo-done)

### `/tally:mark`

Set a todo's status.

```
/tally:mark <id> <status> [note]
```

| argument | |
|---|---|
| `id` | Required. The todo. |
| `status` | Required. `todo`, `doing`, `blocked`, `done` or `dropped`. |
| `note` | Appended to the todo's notes. |

Only the status changes: no task is started or ended. To measure work, use [`take`](#tallytake); to finish it with its task, use [`done`](#tallydone).

```
/tally:mark 4 blocked waiting for the API key
/tally:mark 5 dropped
```

CLI: [`tally todo mark`](cli.md#tally-todo-mark)

## Projects and sessions

### `/tally:project`

A project's overall statistics, or set how it is tracked.

```
/tally:project [name] [track|tasks|off]
```

| argument | |
|---|---|
| `name` | Project. Default: the git repository you are in. |
| `track` | Count every session in the project, with or without a task. |
| `tasks` | Count only explicit tasks (`start`, `new`, `take`, `do`). |
| `off` | Count nothing here, and ask nothing. |

Without a mode it shows the statistics: all time, the last 7 and 30 days, a daily sparkline, usage by model, tasks and their share of the usage, and todos.

Tally asks for the mode the first time an agent opens in a new repository. See [Projects](usage.md#projects).

```
/tally:project
/tally:project track
/tally:project website off
```

CLI: [`tally project`](cli.md#tally-project)

### `/tally:projects`

All projects with their mode, tokens and cost in the last 30 days, all-time tokens, number of tasks and open todos.

```
/tally:projects
```

CLI: [`tally projects`](cli.md#tally-projects)

### `/tally:sessions`

Every session of every agent, with or without a task.

```
/tally:sessions [days|all] [claude|codex|pi] [here]
```

| argument | |
|---|---|
| `days` | Sessions active in the last N days: `30` or `30d`. Default: 7. |
| `all` | All history. |
| `claude`, `codex`, `pi` | Only this agent. |
| `here` | Only the project you are in. |

The arguments can be combined in any order. The list shows the 20 most recent sessions with agent, project, span, active time, prompts, tokens, cost, models and title, plus totals per agent. The first run over a long period reads every transcript and can take a few seconds; later runs read only new data. See [Sessions](usage.md#sessions).

```
/tally:sessions
/tally:sessions 30 codex
/tally:sessions all here
```

CLI: [`tally sessions`](cli.md#tally-sessions), and [`tally session <id>`](cli.md#tally-session) for one session's full report.

## Other

### `/tally:settings`

Show or change settings.

```
/tally:settings [key [value]]
```

| argument | |
|---|---|
| (none) | Every setting with its value and allowed values. |
| `key` | One setting. |
| `key value` | Change it. The value is validated. |

| key | values |
|---|---|
| `log.depth` | `summary`, `prompts` (default) or `calls` |
| `log.prompts` | `full` (default), `preview` or `off` |
| `todos.reminder` | `on` (default) or `off` |
| `todos.reminder_limit` | a number (default 5) |
| `auto_start` | `off` (default) or `session` |
| `dashboard_port` | a number (default 8765) |

What each key means: [Configuration](configuration.md).

```
/tally:settings
/tally:settings log.depth calls
/tally:settings todos.reminder off
```

CLI: [`tally settings`](cli.md#tally-settings)

### `/tally:dashboard`

Start the local dashboard if it is not running, and open it in the browser (http://127.0.0.1:8765 by default). See [Dashboard](usage.md#dashboard).

```
/tally:dashboard
```

CLI: [`tally dashboard`](cli.md#tally-dashboard)

### `/tally:help`

Show a short overview of the commands.

```
/tally:help
```
