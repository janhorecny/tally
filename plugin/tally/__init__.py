"""tally: per-task token and time tracking for Claude Code.

Everything is local: hooks feed a SQLite database under ~/.claude/tally,
token usage is read from Claude Code's own transcripts (main session, subagents
and workflow agents).
"""

__version__ = "0.11.1"
