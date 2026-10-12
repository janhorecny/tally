#!/usr/bin/env bash
# Remove the tally plugin from Claude Code.
#
#   ./uninstall.sh          keep your task history (~/.claude/tally)
#   ./uninstall.sh --purge  also delete the database
set -euo pipefail

MARKETPLACE="tally"
PLUGIN="tally@${MARKETPLACE}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

claude plugin uninstall "$PLUGIN" >/dev/null 2>&1 && echo "plugin removed" || echo "plugin was not installed"
claude plugin marketplace remove "$MARKETPLACE" >/dev/null 2>&1 && echo "marketplace removed" || true
if [ -L "$HOME/.local/bin/tally" ]; then
  rm "$HOME/.local/bin/tally" && echo "CLI symlink removed"
fi
pkill -f "tally dashboard" 2>/dev/null && echo "dashboard stopped" || true
if [ "$PURGE" = 1 ]; then
  rm -rf "${TALLY_HOME:-$HOME/.claude/tally}" && echo "data deleted"
else
  echo "task history kept in ${TALLY_HOME:-$HOME/.claude/tally} (use --purge to delete)"
fi
