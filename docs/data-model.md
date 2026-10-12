# Data model

The database is a single SQLite file, `~/.claude/tally/tally.db`, in WAL mode. Any SQLite client can open it, read-only or read-write, while Claude Code is running. Times are Unix seconds (REAL). Token counts are integers.

```
tasks ──< task_sessions >── sessions
  │                             │
  ├──< prompts                  ├──< usage      (one row per API response; agent_id '' = main session)
  └──  task_snapshots           ├──< agents     (subagents, forks, workflow agents)
                                ├──< workflows  (workflow runs)
                                ├──< turns      (main-session turn intervals)
                                └──  session_costs (Claude Code's exit-time totals)
files  (ingest offsets)   kv  (internal state)
```

## Tables

### `tasks`

One row per task.

| column | |
|---|---|
| `id` | Task number, the `#id` shown everywhere. |
| `name`, `initial_prompt` | The name comes from `/tally:start <name>`, or from the first prompt when no name is given. |
| `status` | `active` or `ended`. |
| `started_at`, `ended_at` | Task lifetime. Wall time is `ended_at - started_at`. |
| `cwd`, `origin_session` | Where the task was started. |
| `created_by` | `command` (`/tally:start`), `new` (`/tally:new`), `auto` (auto-start) or `cli`. |
| `end_note` | Optional note from `/tally:end <note>`. |

### `task_sessions`

Which sessions count towards a task, and when. A task can span several sessions: after `/clear`, after `/tally:resume`, or in parallel terminals. A session counts during `[attached_at, detached_at]`, clipped to the task lifetime.

### `usage`

One row per API response, deduplicated by `msg_key = message.id:requestId`. The last transcript line of a response wins, because earlier lines can carry partial output counts.

| column | |
|---|---|
| `session_id`, `agent_id` | `agent_id` is `''` for the main session, otherwise the subagent or workflow agent ID. |
| `ts` | Timestamp of the response. |
| `model`, `speed` | `speed` is `fast` for fast mode. |
| `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h` | Token counts. Total = the sum of all five. |
| `web_search` | Count of server-side web search requests. |

Rows exist only for transcript bytes read while a task was active, or by `tally session`. History before a task is never parsed.

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

Every prompt submitted while a task was active.

### `task_snapshots`

The full report (`report_json`) and a small `summary_json` (task, time, totals, unlogged), both frozen when a task ends. Lists, the dashboard and the status line read only the summary. Snapshots survive Claude Code's transcript cleanup (`cleanupPeriodDays`). A snapshot is rebuilt only when one of its sessions gets a newer cost record (`session_costs.updated_at` later than `computed_at`).

### `session_costs`

The last `cost-state` record of each session, which Claude Code writes on exit (`cs_json`, with exact per-model tokens and `costUSD`). Alongside it: the session's logged tokens and per-call cost at that moment (`logged_json`), and when the record was stored (`updated_at`). Used for the unlogged-usage estimate.

### `files`, `kv`

Internal state: per-file ingest byte offsets, open turn markers, and the last hook heartbeat.

## Example queries

```sql
-- tasks per day with tokens and output
SELECT date(t.started_at, 'unixepoch', 'localtime') AS day, COUNT(DISTINCT t.id) AS tasks,
       SUM(u.input + u.output + u.cache_read + u.cache_write_5m + u.cache_write_1h) AS tokens,
       SUM(u.output) AS output
FROM tasks t
JOIN task_sessions s ON s.task_id = t.id
JOIN usage u ON u.session_id = s.session_id
 AND u.ts BETWEEN MAX(t.started_at, s.attached_at) AND MIN(COALESCE(t.ended_at, 1e12), COALESCE(s.detached_at, 1e12))
GROUP BY day ORDER BY day DESC;

-- most expensive agent types across all tasks (tokens)
SELECT COALESCE(a.agent_type, 'main') AS type, COUNT(DISTINCT u.agent_id) AS agents,
       SUM(u.input + u.output + u.cache_read + u.cache_write_5m + u.cache_write_1h) AS tokens
FROM usage u LEFT JOIN agents a ON a.session_id = u.session_id AND a.agent_id = u.agent_id
GROUP BY type ORDER BY tokens DESC;

-- final totals of ended tasks straight from the snapshots
SELECT t.id, t.name,
       json_extract(s.report_json, '$.totals.total') AS tokens,
       json_extract(s.report_json, '$.totals.cost')  AS cost_usd,
       json_extract(s.report_json, '$.time.active_s') AS active_s
FROM tasks t JOIN task_snapshots s ON s.task_id = t.id ORDER BY t.id DESC;
```

For spreadsheets, `tally export --format csv` is simpler. It gives one row per task with totals, times, the number of subagents and workflows, and the initial prompt.

## Schema migrations

`PRAGMA user_version` holds the schema version (currently 3). `db.connect()` migrates inside one `BEGIN IMMEDIATE` transaction, so two hooks starting at once cannot both migrate.

- **v1 → v3:** `task_sessions` changes from one row per (task, session) to one row per interval.
- **v2 → v3:** `summary_json` and `updated_at` are added.

Existing data is kept.
