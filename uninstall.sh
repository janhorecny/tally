#!/usr/bin/env bash
# Remove Tally from your coding agents.
#
#   ./uninstall.sh                     from every agent it is installed in; keeps your history
#   ./uninstall.sh --agents codex      only from the given agents (claude, codex, pi)
#   ./uninstall.sh --purge             also delete the database (~/.claude/tally)
#
# A copy installed with the one-line installer (~/.local/share/tally) is deleted too.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGED="${TALLY_DIR:-$HOME/.local/share/tally}"
PURGE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) ARGS+=("$arg") ;;
  esac
done
python3 "$REPO/plugin/bin/tally" uninstall ${ARGS[@]+"${ARGS[@]}"}
if [ ${#ARGS[@]} -eq 0 ]; then
  [ -L "$HOME/.local/bin/tally" ] && rm "$HOME/.local/bin/tally" && echo "✓ CLI symlink removed"
  python3 "$REPO/plugin/bin/tally" dashboard --stop 2>/dev/null | grep -q stopped && echo "✓ dashboard stopped" || true
fi
if [ "$PURGE" = 1 ]; then
  rm -rf "${TALLY_HOME:-$HOME/.claude/tally}" && echo "✓ data deleted"
else
  echo "task history kept in ${TALLY_HOME:-$HOME/.claude/tally} (use --purge to delete)"
fi
if [ ${#ARGS[@]} -eq 0 ] && [ -d "$MANAGED/.git" ] && [ "$(cd "$MANAGED" && pwd)" = "$REPO" ]; then
  cd "$HOME" && rm -rf "$MANAGED" && echo "✓ removed      $MANAGED"
fi
