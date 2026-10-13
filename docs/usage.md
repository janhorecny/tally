# Usage guide

This guide walks through Tally by scenario. Every command, with its arguments and examples, is in the [command reference](commands.md); the terminal commands are in the [CLI reference](cli.md).

Tally works in **Claude Code**, **Codex** and **pi**. The examples use the Claude Code form, `/tally:<cmd>`. In Codex, type `tally:<cmd>` as a prompt; in pi, use `/tally <cmd>`. See [Agents](#agents) for the differences.

Commands are answered locally and cost no tokens, except `new` and `do`, which hand work to the model.

## Scenarios

### A named task

```
/tally:start Migrate billing to Stripe v2
> Read src/billing and plan the migration …     ← stored as the initial prompt
… work, subagents, workflows …
/tally:status                                    ← live totals at any time
/tally:end done, PR #214
```

### A task that is just a prompt

```
/tally:new Find why the nightly import is slow and fix it
```

This starts task "Find why the nightly import is slow and fix it" and sends that prompt to Claude in the same step.

### Background agents and workflows

Everything the session spawns while the task is open is counted, including:

- background subagents;
- forks they spawn;
- every agent of a `Workflow` run.

In the report, workflow agents are grouped under their workflow and phase. Agents still running when you `/tally:end` are not counted after that moment. If you want to wait for them, check `/tally:status`, or end the task later.

### `/clear` in the middle of a task

Nothing to do. The task continues in the new session automatically.

### A second terminal, or after a restart

```
/tally:list                 ← find the task id, e.g. 12
/tally:resume 12            ← this session now counts towards #12 as well
```

`/tally:resume` also works on an ended task: it reopens it. The time between ending and reopening is not counted.

### Starting a new task while one is running

`/tally:start` and `/tally:new` end the session's current task first, and show its final report.

### Automatic tasks

```bash
tally config auto_start session
```

The first prompt of every new session then starts a task named after it. Use `/tally:end` or `/tally:start` as usual. To switch it off: `tally config auto_start off`.

## Todos

A quick todo list per project. Each todo can be picked up as a measured task, so you can see what it really cost.

```
/tally:todo Fix login redirect loop !! due:fri -- Safari only
/tally:todo Write release notes @website due:zajtra
/tally:todos                       ← open todos of this project
/tally:take 3                      ← status doing, Tally measures from now; you write the prompt
/tally:do 3                        ← the same, and Claude starts working on it immediately
/tally:done                        ← todo done, its task ends, report shown
```

### Quick syntax

`@project`, `!`/`!!`/`!!!` priority, `due:<when>` and ` -- notes` work in `/tally:todo`, the dashboard and `tally todo add`. Due dates accept English and Slovak day names, `+3d`, `20.10.` and more; see [`/tally:todo`](commands.md#tallytodo) for the full list.

### Statuses

`todo` → `doing` (picked up) → `done`. Two other statuses: `blocked` (waiting on something) and `dropped` (won't do).

- **Ending without done:** if you `/tally:end` a todo's task without `/tally:done`, the todo goes back to `todo`. It keeps the tokens and time measured so far, and the next `/tally:take` adds to them.
- **Steps:** a todo with steps (from a plan) is marked done automatically once all its steps are done.

### Reminders

When an agent starts in a project that has open todos, Tally shows them (0 tokens). Items being worked on come first, then overdue ones, then the rest by priority. The status line can show `📋 3 todos · 1 overdue` as well. Switch the reminder off with `/tally:settings todos.reminder off`.

### Plans: superpowers and others

```bash
tally todo import docs/plans/2026-10-12-export.md
```

- **What is imported:** `### Task N: …` headings become todos. `**Step N: …**` lines and `- [ ]` / `- [x]` checklists under them become their steps. Code blocks are ignored.
- **Re-importing is safe:** it adds new items and marks checked ones done, and never reopens anything.
- **Claude can do it itself:** the plugin ships a `tally:todos` skill. When a plan is written (for example by superpowers `writing-plans`), Claude imports it. While executing it (for example superpowers `executing-plans`), Claude marks each task `doing` and then `done`, so progress shows up live in `/tally:todos` and the dashboard.

## Projects

The first time an agent opens in a git repository Tally does not know yet, it asks (0 tokens):

```
▍Tally · 👋 First time in project website. Track it?
  /tally:project track   count every session here (overall project stats)
  /tally:project tasks   only tasks you start (/tally:start, /tally:take)
  /tally:project off     ignore this project, don't ask again
  → tally setup sets a default for new projects
```

To skip the question, set a default for new projects with `tally setup` or `/tally:settings projects.default_mode track` (or `tasks`). A new project then gets that mode, with a one-line notice instead of the question.

| mode | what is counted |
|---|---|
| `track` | Every session in the project, with or without a task. This gives the project's overall usage and the share that went through tasks. |
| `tasks` | Only explicit tasks (`/tally:start`, `/tally:new`, `/tally:take`). |
| `off` | Nothing, and no questions or reminders. |

`/tally:project` shows the project's overall statistics:

- all time, the last 7 days and the last 30 days;
- a daily sparkline;
- usage by model;
- tasks and how much of the usage they account for;
- todos.

`/tally:projects` lists all projects. The dashboard's *Projects* tab also shows the projects of the last 30 days that Tally has not set up yet, with how many sessions each has, so you can pick a mode for each. Until a project is set up, the status line shows a hint.

The mode also decides what the [sessions overview](#sessions) reads: the sessions of projects set to `track` or `tasks`. A project that is not set up is only counted (sessions, size of its transcripts), not read. Choosing a mode in the dashboard reads its sessions right away, with all the history the agents still keep (Claude Code deletes transcripts after 30 days unless `cleanupPeriodDays` says otherwise).

## Sessions

Tasks measure what you choose to measure. The sessions overview answers a different question: what did every session cost, with or without a task?

```
/tally:sessions                 # last 7 days, every agent
/tally:sessions 30 codex        # last 30 days, Codex only
/tally:sessions all here        # all history of this project
tally sessions --days 30        # the same in the terminal
tally session <id or id prefix> # the full report of one session
```

```
▍Tally · Sessions, last 7 days: 42 · 1.12B tokens · $512.40 · 318 prompts · active 21h 05m
  Claude Code 37 · 1.09B · $498.10   Codex 3 · 24.1M · $13.90   pi 2 · 310.2k · $0.40

  id       last activity    agent  project      span   active  prm   tokens      cost  models                   title / tasks
  3f9c2a1b 2026-10-12 14:47 claude web-app    44m 10s  31m 02s   12    4.21M     $3.12  opus-5-5, sonnet-5-5     #3 Fix login redirect loop
  9b81e0d4 2026-10-12 11:20 codex  api        1h 05m   22m 40s    6    8.40M     $4.75  gpt-6.1-sol, gpt-6-luna  add pagination to /orders
  c2d7f6a0 2026-10-11 17:02 pi     docs       12m 30s        -    3   180.4k     $0.21  zai/glm-5.2              write the API changelog
```

- **What is listed:** every tracked session with at least one API call, newest first. That covers Claude Code, Codex and pi, with or without a task, including sessions from before Tally was installed.
- **Which sessions are tracked** (setting `sessions.scope`):
  - `enabled` (default): sessions of projects set to `track` or `tasks`, sessions a task counted, and sessions added one by one in the dashboard. The others are not read; the list ends with how many there are (`+ 958 sessions in 55 projects not tracked`).
  - `all`: every session, except projects set to `off`.
- **Adding one session:** the dashboard's *Sessions* tab has a *Not tracked* view with an **Add** button on each session; a project's detail lists its sessions the same way. An added session is read at once and listed with a chip *added*; **Remove** in its detail takes it out again. Projects set to `off` are not offered.
- **Columns:**
  - **span:** first prompt to last API call.
  - **active:** time the agent was working on turns (Claude Code's turn records).
  - **prm:** prompts you typed. Automatic turns, such as a background agent reporting back, are not counted.
  - **cost:** includes Claude Code's own unlogged calls when its session cost record exists.
  - **title / tasks:** Claude Code's session title (the first prompt for Codex and pi) and the Tally tasks that counted the session.
- **How it is read:** tracked sessions are read from the agents' own transcripts into a separate index, `~/.claude/tally/sessions.db`. The task database is not touched. Other transcripts are only described: the session, its project and title, from the start and end of the file. Reading 1,100 sessions (4.1 GB) took 19 s the first time; after that only new bytes are read, typically in well under a second.
- **Dashboard:** the *Sessions* tab has the same list with period and agent filters (the project filter in the header applies too), and *Tracked* / *Not tracked*. Click a session for its full report: per-prompt breakdown, agents, models.
- **Privacy:** session titles follow `log.prompts`. With `off`, no titles or prompt texts are stored in the index.

## Detailed statistics

`/tally:stats` (or `tally stats [id]`) adds the following to the report:

- **Averages:** prompts, automatic turns, subagents, workflows; average and longest turn; cache hit rate; output tokens per active minute.
- **Per prompt:** tokens, output, cost, duration, API calls, subagents and tool calls for every prompt of the task.
  - **Exact attribution:** every API call carries the id of the user prompt that caused it, and subagents inherit the prompt that spawned them.
  - **Automatic turns:** turns Claude Code starts on its own, for example when a background agent finishes, are listed separately.
- **Tools:** calls and errors per tool, including MCP tools. This needs `log.depth calls`.

The same appears in the dashboard's task detail: summary tiles, a per-prompt table and a tools table.

## Settings

```
/tally:settings                         ← show everything
/tally:settings log.depth calls         ← logging depth: summary | prompts | calls
/tally:settings log.prompts preview     ← logging type: full | preview | off
/tally:settings todos.reminder off
```

The dashboard's *Settings* tab offers the same controls, and `tally setup` asks about the main ones in a terminal. See [configuration](configuration.md) for every key.

## Messages

Every answer from Tally has the same shape, in every agent and in the terminal:

```
▍Tally · ▶ Task #3 started: Fix login redirect loop      ← what happened
  Counting tokens and time for this session, …           ← details
  → /tally:status live totals · /tally:end finish        ← what you can do next
```

Errors start with `▍Tally · ✗`. In a terminal the CLI adds colours; set `NO_COLOR=1` to turn them off.

## Agents

### Claude Code

Everything described here. It is installed as the plugin `tally@tally`, with the `/tally:*` commands and the `tally:todos` skill.

### Codex

- **Commands:** type them as a prompt, for example `tally:start Fix login` or `tally:status`. The Codex TUI rejects unknown slash commands. A `UserPromptSubmit` hook answers the prompt and blocks it, so the model is never called.
- **One-time step:** Codex asks you to approve new hooks. Open Codex and confirm Tally's hooks in `/hooks`.
- **What is counted:** every thread of the session, including **subagents**, which are separate threads with their own rollout files. Usage comes from Codex's cumulative `token_count` records, as the difference to the previous total. Cached input is shown as cache reads.
- **Cost:** an estimate at OpenAI API list prices. With a ChatGPT login Codex is not billed per token.
- **Skill:** the `tally-todos` skill is installed in `~/.agents/skills`, so Codex can import plans and update todos itself.
- **Status line:** Codex's status line has fixed items and no custom segment. It already shows tokens and an estimated thread cost.

### pi

- **Commands:** `/tally <cmd> …`, one command with tab completion for its subcommands. `tally:<cmd>` typed as a prompt works too.
- **Display:**
  - reports appear in a widget above the editor;
  - short messages appear as notifications;
  - the footer shows the live task (`▶ #3 … · 4.2M tok · $1.20 · 12m · 📋 2`).
- **Cost:** the cost pi computes itself. It is $0 for subscription providers.
- **Subagents:** counted when the subagent tool reports its nested usage on the tool result, which is pi's convention and is done by `pi-subagents` and `@tintinweb/pi-subagents`. `pi-dynamic-workflows` runs its agents in memory and reports no usage, so it cannot be counted.
- **Handing work to the model:**
  - `/tally do 3` in the TUI hands the todo to the model.
  - In print mode (`pi -p`) the process ends before a queued run starts, so use `tally:do 3` as the prompt there.
- **Skill:** the `tally-todos` skill is installed in `~/.pi/agent/skills`.

## Reading the report

```
▍Tally · Task #3 · Fix login redirect loop   [■ ended]
  prompt   The login page loops back to /login after OAuth callback, find out why and fix it
  time     44m 10s wall · 31m 02s active (12 turns) · 2026-10-12 14:03 → 14:47
  tokens   4.21M total · in 12.3k · out 85.1k · cache write 410.2k · cache read 3.70M
  cost     ≈ $3.12 at list price · 171 API calls
  unlogged ≈ +120.4k tokens · +$0.084  Claude Code's own calls (…), estimated from the session cost record
  models   opus-5-5 3.90M $2.80 · sonnet-5-5 310.0k $0.32

  breakdown                                        tokens   output      cost      time  calls
  main session (own)                                3.10M    60.2k     $2.10   31m 02s     42
  ├─ general-purpose · Explore auth flow            410.0k     8.1k    $0.300    1m 10s      9
  │  └─ fork · Write regression test                 92.0k     2.0k    $0.071       48s      3
  └─ workflow · review-changes (25 agents)          700.0k    16.0k    $0.720    6m 30s    120
     ├─ phase · Review (20)                         560.0k    12.1k    $0.580    4m 10s     96
     └─ phase · Verify (5)                          140.0k     3.9k    $0.140    2m 20s     24
  TOTAL                                             4.21M    85.1k     $3.12   44m 10s    171

  by agent type
    general-purpose               1 agents    410.0k    $0.300
    main                          1 agents     3.10M     $2.10
    workflow-subagent            25 agents    700.0k    $0.720
```

### Header lines

- **time:** *wall* runs from start to end. *active* is the time Claude was working on turns, with overlaps counted once. *turns* is how many there were.
- **tokens:** *total* = input + output + cache write + cache read. *Cache read* is usually the largest part and also the cheapest. *Output* is the part you would usually compare between tasks.
- **cost:** an estimate at API list price, computed per call. Pro and Max subscriptions are not billed per token, but the number still compares tasks fairly.
- **unlogged:** Claude Code's own calls that are not in any transcript. This line appears only after the session has exited (see [how it works](how-it-works.md#claude-codes-own-calls-reconciliation)).

### Breakdown rows

- **main session (own):** only what the main conversation itself used.
- **Every other row:** that node *plus everything below it*. The top-level rows add up to TOTAL.
- **`time` column:** from the agent's first to its last activity inside the task.
- **Long lists:** shortened to the 8 largest children, followed by a `… N more` row. The dashboard and `tally show <id> --json` contain everything.

## Dashboard

`/tally:dashboard` or `tally dashboard --open` serves http://127.0.0.1:8765.

| tab | what you can do |
|---|---|
| *Tasks* | Before the first task: how to start one in each agent, and what your agents used in the last 30 days (or, before any project is tracked, how many sessions they ran). Then: filter the list by name or status. The detail shows summary tiles, token composition, the expandable agent tree, tables by model, agent type and workflow, and the per-prompt breakdown. **Rename** a task, **end** an active one, or **delete** an ended one; ending and deleting ask for confirmation first. |
| *Todos* | A board with Doing, Todo, Blocked and Done. Add todos with the quick syntax, change the status, tick plan steps, **edit** title, priority, due date, project and notes, or **delete** a todo. *Copy /tally:do* copies the command that hands a todo to your agent. |
| *Projects* | Every project with sessions in the last 30 days, in two groups. *Tracked*, most used first: sessions, tokens and cost from the [sessions index](#sessions). *Not tracked*: how many sessions each has, not read; choose `track` or `tasks` right in the table, or in the project's detail, and its sessions are read at once. The detail adds what Tally keeps for good, a daily chart, models and tasks; for a project that is not tracked, its sessions with an **Add** button each. |
| *Sessions* | The tracked sessions of every agent, and the ones not tracked with an **Add** button; see [Sessions](#sessions). |
| *Settings* | The same settings as `/tally:settings`, grouped and described. |

- **Live:** an active task refreshes every 5 seconds. The header shows when the data was last loaded, or that the dashboard server is not reachable.
- **Project filter:** the selector in the header limits Tasks, Todos and Sessions to one project.
- **Keyboard:** `R` reloads the data (as does the ↻ button), `/` jumps to the task filter, `Enter` opens the focused row.
- **Small screens:** tables keep the columns that identify a row and its cost; the rest is in the detail.
- **Themes:** the button with the monitor, sun or moon icon switches between following the system, light and dark.
- **Updates:** a dashboard left running by an older Tally version is replaced automatically by `/tally:dashboard` or `tally dashboard`.
- **Addresses:** every task, project and session has its own URL, for example `#tasks/12`, `#projects/web-app` and `#sessions/3f9c2a1b-…`, so you can bookmark or share them on your machine.
- **One project:** with a project chosen in the header (or after *Sessions of this project*), Tasks, Todos and Sessions show a bar with the project's name, a link to its details and *All projects*.

### Export and copy

| where | what |
|---|---|
| The ⤓ button in the header | The summary of the last 30 days (Markdown, CSV, JSON), all tasks as a time sheet, all projects, the sessions of the last 30 days, and the chosen project. |
| Tasks list | Every task (of the chosen project): time sheet CSV, Markdown table, JSON; or copy one line per task. |
| A task | **Copy**: one line for your time tracker. *Export*: the report as Markdown, a time-sheet row, JSON. |
| Projects | All projects as CSV, Markdown or JSON. |
| A project | Its report (Markdown), tokens per day (CSV), JSON, its tasks and its sessions. |
| Sessions | The sessions of the current filters. A session: **Copy** one line, or its report as Markdown or JSON. |

The terminal has the same exports: [`tally export`](cli.md#tally-export).

**Stopping it:** `/tally:dashboard stop`, `tally dashboard --stop`, or *Settings → Dashboard → Stop the dashboard*. Started by `/tally:dashboard`, it also stops by itself after two hours without an open page (`dashboard_idle_minutes`, `0` keeps it running), and `/tally:dashboard` starts it again in a moment.

## Status line

`tally statusline` reads Claude Code's status line JSON on stdin and prints one line:

```
▶ #3 Fix login redirect loop · 4.21M tok · $3.12 · 44m 10s · 📋 2
```

Without an active task it shows the project's open todos (`📋 3 todos · 1 overdue`), a hint for a project that is not set up yet, or nothing.

- **Set up for you:** `tally setup` (and the first install) offers to add it when Claude Code has no status line yet. Uninstalling removes it again.
- **With ccstatusline:** add a *Custom Command* widget with the command `tally statusline`.
- **As the only status line, by hand:** put this in `~/.claude/settings.json`:

```json
"statusLine": {"type": "command", "command": "tally statusline", "refreshInterval": 10}
```

`--width 40` sets the maximum length of the task name.

## Recipes

The CLI's commands and options are in the [CLI reference](cli.md).

```bash
# all tasks to a spreadsheet (a time sheet: h:mm and decimal hours per task)
tally export -o ~/Desktop/tally-tasks.csv

# one task into your time tracker
tally export task 12 -f text --copy

# a project report for a client or a PR
tally export project web-app -f md -o web-app.md

# what did a past session cost, including subagents? (no task needed)
tally sessions --days 7           # find it (first column: id prefix)
tally session 3f9c2a1b            # its full report

# the ten most expensive sessions of the last 30 days
tally sessions --json --limit 0 | python3 -c "import json,sys; r=json.load(sys.stdin)['sessions']; [print(s['session_id'][:8], s['agent'], round(s['totals']['cost'],2), s['title']) for s in sorted(r, key=lambda s: -s['totals']['cost'])[:10]]"

# the five most expensive tasks
tally list --all --json | python3 -c "import json,sys; r=json.load(sys.stdin); [print(t['id'], round(t['cost'],2), t['name']) for t in sorted(r, key=lambda t: -t['cost'])[:5]]"

# per-agent output tokens of task 12
tally show 12 --json | python3 -c "
import json,sys
def walk(n, d=0):
    print('  '*d + n['label'], n['rollup']['output'])
    [walk(c, d+1) for c in n['children']]
[walk(n) for n in json.load(sys.stdin)['tree']]"
```

For direct SQL access, see [data model](data-model.md).
