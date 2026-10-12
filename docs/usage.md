# Usage guide

## Commands

| command | what it does |
|---|---|
| `/tally:start [name]` | Start a task in this session. If you leave out the name, your next prompt becomes the name. The next prompt is stored as the initial prompt either way. |
| `/tally:new <prompt>` | Start a task and send `<prompt>` to Claude as its first instruction. |
| `/tally:status [id]` | Live totals for the current task, or for task `#id`. |
| `/tally:end [id] [note]` | End the task and print the final report. A first argument that is a number is taken as the task ID; the rest is the note. |
| `/tally:list [n\|all]` | Recent tasks (15 by default). |
| `/tally:show [id]` | Full report of any task (default: the current one, otherwise the latest). |
| `/tally:resume <id>` | Count task `#id` in this session too. Also reopens an ended task. |
| `/tally:rename [id] <name>` | Rename the current task or task `#id`. |
| `/tally:dashboard` | Start the local dashboard if needed, and open it in the browser. |
| `/tally:help` | Show the help text. |

Except for `/tally:new`, these commands are answered by the hook, so **they never reach the model and cost no tokens**. Claude Code shows the answer under the line `UserPromptSubmit operation blocked by hook:`. That line is expected.

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

## Reading the report

```
Task #3 · Fix login redirect loop   [■ ended]
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

`/tally:dashboard` or `tally dashboard --open` serves http://127.0.0.1:8765. It shows:

- the task list (live, refreshed every 5 s while a task is active);
- summary tiles;
- token composition;
- the expandable breakdown tree with share bars;
- tables by model, by agent type and by workflow;
- the prompts of the task.

Hover the bars for exact numbers. Light and dark themes follow the system, or the selector in the header.

The dashboard keeps running in the background after you close the page. To stop it: `pkill -f "tally dashboard"`.

## Status line

`tally statusline` reads Claude Code's status line JSON on stdin. While a task is running it prints one line; otherwise it prints nothing:

```
▶ #3 Fix login redirect loop · 4.21M tok · $3.12 · 44m 10s
```

- **With ccstatusline:** add a *Custom Command* widget with the command `tally statusline`.
- **As the only status line:** put this in `~/.claude/settings.json`:

```json
"statusLine": {"type": "command", "command": "tally statusline", "refreshInterval": 10}
```

Options: `--width 40` sets the maximum length of the task name.

## CLI

```bash
tally list [-n 50 | --all] [--json]
tally status [id] [--json] [--brief]     # default: the active task of $CLAUDE_CODE_SESSION_ID or the only active one
tally show <id> [--json] [--prompts]
tally end [id] [--note "…"]
tally start <name> --session <id>        # outside Claude Code you must name the session
tally resume <id> --session <id>
tally rename <id> <name>
tally delete <id> [-y]
tally export --format csv|json [-o file]
tally session <session-id | path/to/transcript.jsonl> [--json]
tally dashboard [--open] [--background] [--port 8766]
tally config [key [value]]
tally statusline
tally doctor
```

### Recipes

```bash
# all tasks to a spreadsheet
tally export --format csv -o ~/Desktop/claude-tasks.csv

# what did a past session cost, including subagents? (no task needed)
tally session 282bb40a-4829-48fe-b490-d8f1883b960f

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
