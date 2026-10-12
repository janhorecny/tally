"""SQLite storage. One file, WAL mode, safe for concurrent hook processes."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import paths

SCHEMA_VERSION = 2

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
    created_by      TEXT,                                -- command | new | auto | cli
    end_note        TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);

-- Sessions a task spans (a task can continue after /clear or in another session).
CREATE TABLE IF NOT EXISTS task_sessions (
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id   TEXT NOT NULL,
    attached_at  REAL NOT NULL,
    detached_at  REAL,
    reason       TEXT,
    PRIMARY KEY (task_id, session_id)
);
CREATE INDEX IF NOT EXISTS task_sessions_sid ON task_sessions(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    transcript_path  TEXT,
    cwd              TEXT,
    claude_pid       INTEGER,
    first_seen       REAL,
    last_seen        REAL
);

-- Incremental ingest bookkeeping, one row per transcript file.
CREATE TABLE IF NOT EXISTS files (
    path             TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    agent_id         TEXT NOT NULL DEFAULT '',
    workflow_run_id  TEXT,
    offset           INTEGER NOT NULL DEFAULT 0,
    meta_loaded      INTEGER NOT NULL DEFAULT 0
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
    web_search      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_session_ts ON usage(session_id, ts);

-- Claude Code writes a turn_duration record after every main-session turn.
CREATE TABLE IF NOT EXISTS turns (
    uuid         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    ts_end       REAL NOT NULL,
    duration_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_session_ts ON turns(session_id, ts_end);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    session_id  TEXT,
    ts          REAL NOT NULL,
    prompt      TEXT
);
CREATE INDEX IF NOT EXISTS prompts_task ON prompts(task_id);

-- Final report frozen when a task ends (survives transcript cleanup).
CREATE TABLE IF NOT EXISTS task_snapshots (
    task_id      INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    computed_at  REAL NOT NULL,
    report_json  TEXT NOT NULL
);

-- Claude Code's own per-session totals (cost-state record written at exit),
-- used to estimate usage that never reaches a transcript.
CREATE TABLE IF NOT EXISTS session_costs (
    session_id   TEXT PRIMARY KEY,
    scan_offset  INTEGER NOT NULL DEFAULT 0,
    cs_ts        REAL,
    cs_json      TEXT,
    logged_json  TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(path) if path else paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)


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
