#!/usr/bin/env bash
# Remove the task-counter plugin from Claude Code.
#
#   ./uninstall.sh          keep your task history (~/.claude/task-counter)
#   ./uninstall.sh --purge  also delete the database
set -euo pipefail

MARKETPLACE="task-counter"
PLUGIN="task@${MARKETPLACE}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

claude plugin uninstall "$PLUGIN" >/dev/null 2>&1 && echo "plugin removed" || echo "plugin was not installed"
claude plugin marketplace remove "$MARKETPLACE" >/dev/null 2>&1 && echo "marketplace removed" || true
if [ -L "$HOME/.local/bin/task-counter" ]; then
  rm "$HOME/.local/bin/task-counter" && echo "CLI symlink removed"
fi
pkill -f "task-counter dashboard" 2>/dev/null && echo "dashboard stopped" || true
if [ "$PURGE" = 1 ]; then
  rm -rf "${TASK_COUNTER_HOME:-$HOME/.claude/task-counter}" && echo "data deleted"
else
  echo "task history kept in ${TASK_COUNTER_HOME:-$HOME/.claude/task-counter} (use --purge to delete)"
fi
