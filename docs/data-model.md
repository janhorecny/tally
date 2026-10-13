# Data model

The database is a single SQLite file, `~/.claude/tally/tally.db`, in WAL mode. Any SQLite client can open it, read-only or read-write, while Claude Code is running. Times are Unix seconds (REAL). Token counts are integers.

```
projects ──< sessions (project)            todos ──< todos (parent_id: plan steps)
    │                                         │
tasks (project, todo_id) ──< task_sessions >── sessions
  │                                              │
  ├──< prompts (prompt_id)                       ├──< usage
  └──  task_snapshots                            ├──< tool_calls
                                                 ├──< agents
                                                 ├──< workflows
                                                 ├──< turns
                                                 └──  session_costs
files  (ingest offsets, last prompt id)   kv  (internal state)
```

Per session:

- `usage`: one row per API response, with `prompt_id`;
- `tool_calls`: only with `log.depth = calls`;
- `agents`: subagents, forks and workflow agents;
- `workflows`: workflow runs;
- `turns`: turn intervals, with `prompt_id`;
- `session_costs`: Claude Code's exit-time totals.

## Tables

### `tasks`

One row per task.

| Column | Meaning |
|---|---|
| `id` | Task number, the `#id` shown everywhere. |
| `name`, `initial_prompt` | The name comes from `/tally:start <name>`, or from the first prompt when no name is given. |
| `status` | `active` or `ended`. |
| `started_at`, `ended_at` | Task lifetime. Wall time is `ended_at - started_at`. |
| `cwd`, `origin_session` | Where the task was started. |
| `created_by` | `command` (`/tally:start`), `new` (`/tally:new`), `auto` (auto-start) or `cli`. |
| `end_note` | Optional note from `/tally:end <note>`. |

Two further columns on `tasks`:

- `project`: the git repository the task was started in.
- `todo_id`: the todo the task works on, if it was started with `/tally:take` or `/tally:do`.

### `todos`

| Column | Meaning |
|---|---|
| `project`, `title`, `notes` | |
| `status` | `todo`, `doing`, `blocked`, `done` or `dropped`. |
| `priority` | 0 none, 1 low, 2 medium, 3 high. |
| `due_at` | Unix seconds, at the end of the due day. |
| `parent_id`, `position` | Plan structure (plan → task → step), in plan order. |
| `rank` | Place on the dashboard's board, set by dragging a card (lowest first); empty until then. |
| `source`, `source_ref` | Where the todo came from: `user`, `claude`, `import` or `dashboard`. For plan imports, the file plus the item key, which makes re-imports idempotent. |
| `created_at`, `updated_at`, `done_at` | |

A todo's measured work is the sum over `tasks WHERE todo_id = id`.

### `projects`

| Column | Meaning |
|---|---|
| `name`, `path` | The git repository (the main one for worktrees) and its path. |
| `mode` | `ask` (not decided yet), `track` (every session counts), `tasks` or `off`. |
| `first_seen`, `decided_at` | |

`sessions.project` links each session to its project.

### `task_sessions`

Which sessions count towards a task, and when. A task can span several sessions: after `/clear`, after `/tally:resume`, or in parallel terminals. A session counts during `[attached_at, detached_at]`, clipped to the task lifetime.

### `usage`

One row per API response, deduplicated by `msg_key = message.id:requestId`. The last transcript line of a response wins, because earlier lines can carry partial output counts.

| Column | Meaning |
|---|---|
| `session_id`, `agent_id` | `agent_id` is `''` for the main session, otherwise the subagent or workflow agent ID. |
| `ts` | Timestamp of the response. |
| `model`, `speed` | `speed` is `fast` for fast mode. |
| `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h` | Token counts. Total = the sum of all five. |
| `web_search` | Count of server-side web search requests. |
| `prompt_id` | The user prompt that caused the call. It comes from the `promptId` on transcript user lines; a subagent inherits the prompt that spawned it. |

Rows exist only for transcript bytes read while a task was active (or for every session of a project in `track` mode). History before a task is never parsed here. Whole sessions are read into the separate sessions index (see below).

### `agents`

One row per subagent, fork or workflow agent, taken from its `agent-<id>.meta.json`:

- `agent_type`, `description`;
- `parent_agent_id` (set for nested forks);
- `workflow_run_id`, `workflow_phase`;
- `model`, `spawn_depth`, `is_fork`;
- `first_ts` and `last_ts`, the first and last transcript entry seen.

### `workflows`

Workflow runs: `run_id`, `name`, `summary`, `status`, `started_at`, `duration_ms`, and `reported_total_tokens`, which is Claude Code's own count.

### `turns`

Main-session turn intervals, from two sources. The report uses the union of these intervals as active time.

- `turn_duration` records in the transcript (interactive mode). The `uuid` is the transcript entry's.
- Hook timing from prompt submit to `Stop` (all modes). The `uuid` starts with `hook:`.

### `prompts`

Every prompt submitted while a task was active, with its `prompt_id`. This is the same id Claude Code writes into the transcript, which is what makes the per-prompt breakdown exact. The text follows `log.prompts`: `full`, `preview`, or `off` (the row is kept, the text is not).

### `tool_calls`

One row per `tool_use` block, stored when `log.depth = calls`: `id` (the tool_use id), `session_id`, `agent_id`, `prompt_id`, `ts`, `tool`, and `is_error`. `is_error` is set from the matching `tool_result`.

### `task_snapshots`

The full report (`report_json`) and a small `summary_json` (task, time, totals, unlogged), both frozen when a task ends. Lists, the dashboard and the status line read only the summary. Snapshots survive Claude Code's transcript cleanup (`cleanupPeriodDays`). A snapshot is rebuilt only when one of its sessions gets a newer cost record (`session_costs.updated_at` later than `computed_at`).

### `session_costs`

The last `cost-state` record of each session, which Claude Code writes on exit (`cs_json`, with exact per-model tokens and `costUSD`). Alongside it: the session's logged tokens and per-call cost at that moment (`logged_json`), and when the record was stored (`updated_at`). Used for the unlogged-usage estimate.

### `files`, `kv`

Internal state: per-file ingest byte offsets, open turn markers, and the last hook heartbeat.

## Example queries

```sql
-- tasks per day with tokens and output
SELECT date(t.started_at, 'unixepoch', 'localtime') AS day,
       COUNT(DISTINCT t.id) AS tasks,
       SUM(u.input + u.output + u.cache_read
           + u.cache_write_5m + u.cache_write_1h) AS tokens,
       SUM(u.output) AS output
FROM tasks t
JOIN task_sessions s ON s.task_id = t.id
JOIN usage u ON u.session_id = s.session_id
 AND u.ts BETWEEN MAX(t.started_at, s.attached_at)
              AND MIN(COALESCE(t.ended_at, 1e12), COALESCE(s.detached_at, 1e12))
GROUP BY day ORDER BY day DESC;

-- most expensive agent types across all tasks (tokens)
SELECT COALESCE(a.agent_type, 'main') AS type, COUNT(DISTINCT u.agent_id) AS agents,
       SUM(u.input + u.output + u.cache_read
           + u.cache_write_5m + u.cache_write_1h) AS tokens
FROM usage u
LEFT JOIN agents a ON a.session_id = u.session_id AND a.agent_id = u.agent_id
GROUP BY type ORDER BY tokens DESC;

-- open todos per project with the tokens spent on them so far
SELECT d.project, d.id, d.title, d.status, COUNT(t.id) AS tasks
FROM todos d LEFT JOIN tasks t ON t.todo_id = d.id
WHERE d.status IN ('todo', 'doing', 'blocked') AND d.parent_id IS NULL
GROUP BY d.id ORDER BY d.project, d.priority DESC;

-- final totals of ended tasks straight from the snapshots
SELECT t.id, t.name,
       json_extract(s.report_json, '$.totals.total') AS tokens,
       json_extract(s.report_json, '$.totals.cost')  AS cost_usd,
       json_extract(s.report_json, '$.time.active_s') AS active_s
FROM tasks t JOIN task_snapshots s ON s.task_id = t.id ORDER BY t.id DESC;
```

For spreadsheets, `tally export --format csv` is simpler. It gives one row per task with totals, times, the number of subagents and workflows, and the initial prompt.

## The sessions index (`sessions.db`)

`~/.claude/tally/sessions.db` has the same tables, filled from whole transcripts for the sessions overview, plus two more:

### `indexed`

One row per session of any agent: `session_id`, `agent`, `path`, `cwd`, `project`, `title`, the transcript's `size` and `mtime` when it was last read, `summary_json` (totals, models, prompts, automatic turns, wall and active time, unlogged estimate) and `indexed_at`. `prompt_offset` and `prompt_turn` track how far the main transcript was scanned for prompts. In this database `prompts.task_id` is NULL: the rows are the session's own prompts.

### `seen`

One row per transcript Tally has looked at, read or not: `path`, `agent`, `session_id`, `cwd`, `project`, `title`, `size` and `mtime`. Only the start and end of the file are read for it. The dashboard lists the sessions that are not tracked from here.

It is a cache. Delete it whenever you like; `tally sessions` rebuilds it. Which sessions are tracked is not in the cache: the project modes and `added_sessions` (sessions added one by one in the dashboard) are in `tally.db`.

```sql
-- sessions.db: cost per agent and day, last 14 days
SELECT agent,
       date(json_extract(summary_json, '$.last_ts'), 'unixepoch', 'localtime') AS day,
       COUNT(*) AS sessions,
       ROUND(SUM(json_extract(summary_json, '$.totals.cost')), 2) AS cost
FROM indexed WHERE summary_json IS NOT NULL
  AND json_extract(summary_json, '$.last_ts') > strftime('%s', 'now') - 14 * 86400
GROUP BY agent, day ORDER BY day DESC;
```

## Schema migrations

`PRAGMA user_version` holds the schema version (currently 8). `db.connect()` migrates inside one `BEGIN IMMEDIATE` transaction, so two hooks starting at once cannot both migrate.

- **v1 → v3:** `task_sessions` changes from one row per (task, session) to one row per interval.
- **v2 → v3:** `summary_json` and `updated_at` are added.
- **v3 → v4:** adds `todos`, `tool_calls`, `prompt_id` columns, and `tasks.project`/`todo_id`, with the project filled in from the cwd.
- **v4 → v5:** adds `projects` and `sessions.project`.
- **v5 → v6:** adds `sessions.agent`, `usage.cost` and `files.state` (Codex and pi).
- **v6 → v7:** adds `added_sessions`.
- **v7 → v8:** adds `todos.rank`, the order of the cards on the dashboard's board.

The migration creates tables first, then adds missing columns, then indexes. So any older version upgrades in one step.

Existing data is kept.
