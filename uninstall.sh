#!/usr/bin/env bash
# Remove Tally from your coding agents.
#
#   ./uninstall.sh                     from every agent it is installed in; keeps your history
#   ./uninstall.sh --agents codex      only from the given agents (claude, codex, pi)
#   ./uninstall.sh --purge             also delete the database (~/.claude/tally)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PURGE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) ARGS+=("$arg") ;;
  esac
done
python3 "$REPO/plugin/bin/tally" uninstall ${ARGS[@]+"${ARGS[@]}"}
if [ ${#ARGS[@]} -eq 0 ]; then
  [ -L "$HOME/.local/bin/tally" ] && rm "$HOME/.local/bin/tally" && echo "✓ CLI symlink removed"
  pkill -f "tally dashboard" 2>/dev/null && echo "✓ dashboard stopped" || true
fi
if [ "$PURGE" = 1 ]; then
  rm -rf "${TALLY_HOME:-$HOME/.claude/tally}" && echo "✓ data deleted"
else
  echo "task history kept in ${TALLY_HOME:-$HOME/.claude/tally} (use --purge to delete)"
fi
