#!/usr/bin/env bash
# Install tally into Claude Code as a local plugin.
#
#   ./install.sh            install (or update) for your user
#   ./install.sh --no-cli   skip the ~/.local/bin/tally symlink
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKETPLACE="tally"
PLUGIN="tally@${MARKETPLACE}"
WITH_CLI=1
for arg in "$@"; do
  case "$arg" in
    --no-cli) WITH_CLI=0 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required (macOS: xcode-select --install)"
python3 - <<'EOF' || die "python3 >= 3.8 with sqlite3 is required"
import sys, sqlite3
assert sys.version_info >= (3, 8)
EOF
command -v claude >/dev/null || die "the claude CLI (Claude Code) is required"

# Migrate an install made under the previous name (task-counter, plugin "task").
OLD_HOME="$HOME/.claude/task-counter"
if claude plugin list 2>/dev/null | grep -q "task@task-counter"; then
  say "migrating: removing old plugin task@task-counter"
  claude plugin uninstall "task@task-counter" >/dev/null 2>&1 || true
fi
if claude plugin marketplace list 2>/dev/null | grep -q "task-counter"; then
  claude plugin marketplace remove "task-counter" >/dev/null 2>&1 || true
fi
[ -L "$HOME/.local/bin/task-counter" ] && rm "$HOME/.local/bin/task-counter"
pkill -f "task-counter dashboard" 2>/dev/null || true
if [ -z "${TALLY_HOME:-}" ] && [ -d "$OLD_HOME" ] && [ ! -e "$HOME/.claude/tally" ]; then
  say "migrating: $OLD_HOME -> ~/.claude/tally"
  mv "$OLD_HOME" "$HOME/.claude/tally"
  for ext in "" "-wal" "-shm"; do
    [ -e "$HOME/.claude/tally/tasks.db$ext" ] && mv "$HOME/.claude/tally/tasks.db$ext" "$HOME/.claude/tally/tally.db$ext"
  done
fi

say "1/4 validating plugin"
claude plugin validate "$REPO" >/dev/null
claude plugin validate "$REPO/plugin" >/dev/null

say "2/4 registering local marketplace '$MARKETPLACE' -> $REPO"
if claude plugin marketplace list 2>/dev/null | grep -q "$MARKETPLACE"; then
  claude plugin marketplace update "$MARKETPLACE" >/dev/null
else
  claude plugin marketplace add "$REPO" >/dev/null
fi

say "3/4 installing plugin $PLUGIN (user scope)"
if claude plugin list 2>/dev/null | grep -q "$PLUGIN"; then
  claude plugin update "$PLUGIN" >/dev/null || true
else
  claude plugin install "$PLUGIN" --scope user >/dev/null
fi

if [ "$WITH_CLI" = 1 ]; then
  say "4/4 linking CLI ~/.local/bin/tally"
  mkdir -p "$HOME/.local/bin"
  chmod +x "$REPO/plugin/bin/tally"
  ln -sf "$REPO/plugin/bin/tally" "$HOME/.local/bin/tally"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "   note: add ~/.local/bin to PATH to use 'tally' directly" ;;
  esac
else
  say "4/4 skipping CLI symlink"
fi

python3 "$REPO/plugin/bin/tally" doctor | sed 's/^/   /'
cat <<'EOF'

Installed. Restart Claude Code (or run /reload-plugins in an open session), then:

  /tally:start Fix the login bug     start tracking (zero tokens)
  /tally:new <prompt>                start a task and send <prompt> to Claude
  /tally:status                      live tokens / cost / time incl. subagents & workflows
  /tally:end                         final report
  /tally:list   /tally:dashboard     history and the local web dashboard
EOF
