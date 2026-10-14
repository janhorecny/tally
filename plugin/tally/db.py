"""SQLite storage. One file, WAL mode, safe for concurrent hook processes."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import paths

SCHEMA_VERSION = 9

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    initial_prompt  TEXT,
    status          TEXT NOT NULL DEFAULT 'active',      -- active | ended
    started_at      REAL NOT NULL,                       -- unix seconds
    ended_at        REAL,
    cwd             TEXT,
    origin_session  TEXT,
    created_by      TEXT,                                -- command | new | auto | cli | todo
    end_note        TEXT,
    project         TEXT,
    todo_id         INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);

-- Intervals during which a session counts towards a task. A task can span
-- several sessions (/clear, /tally:resume, parallel terminals) and a session
-- can be attached to the same task more than once (end + resume).
CREATE TABLE IF NOT EXISTS task_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id   TEXT NOT NULL,
    attached_at  REAL NOT NULL,
    detached_at  REAL,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS task_sessions_sid ON task_sessions(session_id);
CREATE INDEX IF NOT EXISTS task_sessions_task ON task_sessions(task_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    transcript_path  TEXT,
    cwd              TEXT,
    claude_pid       INTEGER,
    first_seen       REAL,
    last_seen        REAL,
    project          TEXT,
    agent            TEXT                                 -- claude | codex | pi
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project);

-- Incremental ingest bookkeeping, one row per transcript file.
CREATE TABLE IF NOT EXISTS files (
    path             TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    agent_id         TEXT NOT NULL DEFAULT '',
    workflow_run_id  TEXT,
    offset           INTEGER NOT NULL DEFAULT 0,
    meta_loaded      INTEGER NOT NULL DEFAULT 0,
    last_prompt_id   TEXT,                                -- prompt the next lines belong to
    state            TEXT,                                -- reader state carried between reads (JSON)
    tools_offset     INTEGER                              -- tool calls are recorded (or skipped) up to here
);

CREATE TABLE IF NOT EXISTS agents (
    session_id       TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    agent_type       TEXT,
    description      TEXT,
    parent_agent_id  TEXT,
    tool_use_id      TEXT,
    workflow_run_id  TEXT,
    workflow_phase   TEXT,
    model            TEXT,
    spawn_depth      INTEGER,
    is_fork          INTEGER,
    transcript_path  TEXT,
    first_ts         REAL,
    last_ts          REAL,
    PRIMARY KEY (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS workflows (
    session_id   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    name         TEXT,
    summary      TEXT,
    status       TEXT,
    started_at   REAL,
    duration_ms  INTEGER,
    reported_total_tokens INTEGER,
    PRIMARY KEY (session_id, run_id)
);

-- One row per API response (deduplicated by message id + request id).
CREATE TABLE IF NOT EXISTS usage (
    msg_key         TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',            -- '' = main session
    ts              REAL NOT NULL,
    model           TEXT,
    speed           TEXT,
    input           INTEGER NOT NULL DEFAULT 0,
    output          INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_write_5m  INTEGER NOT NULL DEFAULT 0,
    cache_write_1h  INTEGER NOT NULL DEFAULT 0,
    web_search      INTEGER NOT NULL DEFAULT 0,
    prompt_id       TEXT,                                -- user prompt that caused the call
    cost            REAL                                 -- cost reported by the agent itself (pi, codex)
);
CREATE INDEX IF NOT EXISTS usage_session_ts ON usage(session_id, ts);

-- Claude Code writes a turn_duration record after every main-session turn.
CREATE TABLE IF NOT EXISTS turns (
    uuid         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    ts_end       REAL NOT NULL,
    duration_ms  INTEGER NOT NULL,
    prompt_id    TEXT
);
CREATE INDEX IF NOT EXISTS turns_session_ts ON turns(session_id, ts_end);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    session_id  TEXT,
    ts          REAL NOT NULL,
    prompt      TEXT,
    prompt_id   TEXT
);
CREATE INDEX IF NOT EXISTS prompts_task ON prompts(task_id);

-- Todo list: per project, status, priority, due date. Plan imports create
-- parent/child items. Tasks link back via tasks.todo_id.
CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT,
    title       TEXT NOT NULL,
    notes       TEXT,
    status      TEXT NOT NULL DEFAULT 'todo',             -- todo | doing | blocked | done | dropped
    priority    INTEGER NOT NULL DEFAULT 0,               -- 0 none, 1 low, 2 medium, 3 high
    due_at      REAL,
    parent_id   INTEGER REFERENCES todos(id) ON DELETE CASCADE,
    position    INTEGER,
    rank        REAL,                                     -- order on the dashboard's board (drag and drop)
    source      TEXT,                                     -- user | claude | import | dashboard
    source_ref  TEXT,                                     -- plan file + item key (re-import)
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    done_at     REAL
);
CREATE INDEX IF NOT EXISTS todos_project_status ON todos(project, status);
CREATE INDEX IF NOT EXISTS todos_parent ON todos(parent_id);
CREATE UNIQUE INDEX IF NOT EXISTS todos_source_ref ON todos(source_ref) WHERE source_ref IS NOT NULL;

-- Tool calls (log depth "calls"): one row per tool_use block.
CREATE TABLE IF NOT EXISTS tool_calls (
    id          TEXT PRIMARY KEY,                         -- tool_use id
    session_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL DEFAULT '',
    prompt_id   TEXT,
    ts          REAL NOT NULL,
    tool        TEXT NOT NULL,
    is_error    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tool_calls_session_ts ON tool_calls(session_id, ts);

-- Final report frozen when a task ends (survives transcript cleanup).
CREATE TABLE IF NOT EXISTS task_snapshots (
    task_id      INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    computed_at  REAL NOT NULL,
    report_json  TEXT NOT NULL,
    summary_json TEXT                -- task/time/totals/unlogged only, for lists
);

-- Claude Code's own per-session totals (cost-state record written at exit),
-- used to estimate usage that never reaches a transcript.
CREATE TABLE IF NOT EXISTS session_costs (
    session_id   TEXT PRIMARY KEY,
    scan_offset  INTEGER NOT NULL DEFAULT 0,
    cs_ts        REAL,
    cs_json      TEXT,
    logged_json  TEXT,
    updated_at   REAL                -- when the current cost record was stored
);

-- Projects (git repositories) Tally has seen, and how to track them:
-- ask (not decided yet) | track (every session counts) | tasks (only tasks) | off
CREATE TABLE IF NOT EXISTS projects (
    name        TEXT PRIMARY KEY,
    path        TEXT,
    mode        TEXT NOT NULL DEFAULT 'ask',
    first_seen  REAL,
    decided_at  REAL
);

-- Sessions added to the sessions overview one by one, whatever their project's mode.
CREATE TABLE IF NOT EXISTS added_sessions (
    session_id  TEXT PRIMARY KEY,
    added_at    REAL
);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


# SCHEMA is split on ";" -- comments must not contain one.
assert not any(";" in line.split("--", 1)[1] for line in SCHEMA.splitlines() if "--" in line)


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(path) if path else paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    _wal(conn)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _wal(conn: sqlite3.Connection) -> None:
    """WAL mode. Switching a new database to it needs a lock SQLite does not wait
    for (busy_timeout does not apply), so two hooks opening a fresh database at
    once would fail: try again for a moment."""
    for attempt in range(100):
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            if attempt == 99:
                raise
            time.sleep(0.05)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


# The database heals itself at every version: the tables, columns and indexes
# SCHEMA declares are compared with the real ones on every connect, and
# whatever is missing is added. A database already at SCHEMA_VERSION can still
# lack something: hooks run from a working tree, and an unreleased build of the
# same version may have named a column differently. Nothing is ever dropped or
# renamed; unknown tables and columns are left alone.

_expected = None       # (tables {name: [(column, type, notnull, default)]}, index names), once per process


def _expected_schema():
    """SCHEMA as SQLite itself reads it: built once per process in memory."""
    global _expected
    if _expected is None:
        mem = sqlite3.connect(":memory:", isolation_level=None)
        try:
            mem.execute("BEGIN")
            for stmt in _statements():
                mem.execute(stmt)
            mem.execute("COMMIT")
            tables: dict = {}
            for t, col, ctype, notnull, dflt in mem.execute(
                    "SELECT m.name, p.name, p.type, p.\"notnull\", p.dflt_value FROM sqlite_master m "
                    "JOIN pragma_table_info(m.name) p WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%' "
                    "ORDER BY m.name, p.cid"):
                tables.setdefault(t, []).append((col, ctype, notnull, dflt))
            indexes = {r[0] for r in mem.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
        finally:
            mem.close()
        _expected = (tables, indexes)
    return _expected


def _statements() -> list:
    return [x.strip() for x in SCHEMA.split(";") if x.strip()]


def _schema_gaps(conn: sqlite3.Connection):
    """(missing tables, {table: [missing column specs]}, missing indexes); all empty = healthy.
    One query on the real database: cheap enough for every connect."""
    tables, indexes = _expected_schema()
    have_cols: dict = {}
    have_idx = set()
    for kind, name, col in conn.execute(
            "SELECT m.type, m.name, p.name FROM sqlite_master m LEFT JOIN pragma_table_info(m.name) p "
            "ON m.type = 'table' WHERE m.type IN ('table', 'index')"):
        if kind == "index":
            have_idx.add(name.lower())
        elif col is not None:
            have_cols.setdefault(name.lower(), set()).add(col.lower())
    missing_tables = [t for t in tables if t.lower() not in have_cols]
    missing_cols = {}
    for t, cols in tables.items():
        have = have_cols.get(t.lower())
        if have is not None:
            gap = [c for c in cols if c[0].lower() not in have]
            if gap:
                missing_cols[t] = gap
    missing_idx = [i for i in indexes if i.lower() not in have_idx]
    return missing_tables, missing_cols, missing_idx


def _heal(conn: sqlite3.Connection) -> None:
    """Add what SCHEMA declares and the database lacks (inside a write transaction)."""
    stmts = _statements()
    for stmt in stmts:                            # tables first (IF NOT EXISTS) ...
        if "CREATE TABLE" in stmt:
            conn.execute(stmt)
    for table, cols in _schema_gaps(conn)[1].items():   # ... then columns, with their declared type and default ...
        for col, ctype, notnull, dflt in cols:
            spec = ['"%s"' % col]
            if ctype:
                spec.append(ctype)
            if dflt is not None:
                spec.append("DEFAULT %s" % dflt)
                if notnull:
                    spec.append("NOT NULL")       # ALTER TABLE adds NOT NULL only with a default
            conn.execute('ALTER TABLE "%s" ADD COLUMN %s' % (table, " ".join(spec)))
    for stmt in stmts:                            # ... then indexes, which may use them
        if "CREATE TABLE" not in stmt:
            try:
                conn.execute(stmt)
            except sqlite3.IntegrityError:        # a unique index over rows that already repeat a value:
                conn.execute(stmt.replace("CREATE UNIQUE INDEX", "CREATE INDEX", 1))   # keep the lookups fast


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION and not any(_schema_gaps(conn)):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]   # another process may have won
        if version < SCHEMA_VERSION or any(_schema_gaps(conn)):
            if version and version < 3:
                _migrate_to_3(conn)
            _heal(conn)
            if version and version < 4:
                _fill_projects(conn)
            if version < SCHEMA_VERSION:                              # never lowered
                conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _fill_projects(conn: sqlite3.Connection) -> None:
    from . import projects
    for row in conn.execute("SELECT id, cwd FROM tasks WHERE project IS NULL AND cwd IS NOT NULL").fetchall():
        conn.execute("UPDATE tasks SET project = ? WHERE id = ?", (projects.detect(row[1]), row[0]))


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """v1/v2 -> v3: task_sessions gets one row per interval; new columns."""
    if "id" not in _columns(conn, "task_sessions"):
        conn.execute("ALTER TABLE task_sessions RENAME TO task_sessions_v2")
        conn.execute("DROP INDEX IF EXISTS task_sessions_sid")
        conn.execute(
            "CREATE TABLE task_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, session_id TEXT NOT NULL, "
            "attached_at REAL NOT NULL, detached_at REAL, reason TEXT)")
        conn.execute(
            "INSERT INTO task_sessions(task_id, session_id, attached_at, detached_at, reason) "
            "SELECT task_id, session_id, attached_at, detached_at, reason FROM task_sessions_v2 ORDER BY attached_at")
        conn.execute("DROP TABLE task_sessions_v2")



@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE so concurrent hooks serialize their writes."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def kv_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
