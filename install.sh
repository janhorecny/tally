#!/usr/bin/env bash
# Install Tally into your coding agents: Claude Code, Codex and/or pi.
#
#   ./install.sh                         ask which detected agents to install into
#   ./install.sh --agents claude,pi      choose explicitly (claude, codex, pi)
#   ./install.sh --all | --yes           every supported / every detected agent, no questions
#   ./install.sh --no-cli                skip the ~/.local/bin/tally symlink
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_CLI=1
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-cli) WITH_CLI=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) ARGS+=("$arg") ;;
  esac
done

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
command -v python3 >/dev/null || die "python3 is required (macOS: xcode-select --install)"
python3 -c 'import sys, sqlite3; assert sys.version_info >= (3, 8)' || die "python3 >= 3.8 with sqlite3 is required"

# data from an install under the previous name (task-counter)
OLD_HOME="$HOME/.claude/task-counter"
if [ -z "${TALLY_HOME:-}" ] && [ -d "$OLD_HOME" ] && [ ! -e "$HOME/.claude/tally" ]; then
  mv "$OLD_HOME" "$HOME/.claude/tally"
  for ext in "" "-wal" "-shm"; do
    [ -e "$HOME/.claude/tally/tasks.db$ext" ] && mv "$HOME/.claude/tally/tasks.db$ext" "$HOME/.claude/tally/tally.db$ext"
  done
  echo "migrated ~/.claude/task-counter -> ~/.claude/tally"
fi
[ -L "$HOME/.local/bin/task-counter" ] && rm "$HOME/.local/bin/task-counter"

chmod +x "$REPO/plugin/bin/tally"
python3 "$REPO/plugin/bin/tally" install ${ARGS[@]+"${ARGS[@]}"}

if [ "$WITH_CLI" = 1 ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$REPO/plugin/bin/tally" "$HOME/.local/bin/tally"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "✓ CLI          ~/.local/bin/tally" ;;
    *) echo "✓ CLI          ~/.local/bin/tally (add ~/.local/bin to PATH)" ;;
  esac
fi
echo
python3 "$REPO/plugin/bin/tally" doctor | sed 's/^/  /'
