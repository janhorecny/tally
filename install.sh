#!/usr/bin/env bash
# Install Tally into your coding agents: Claude Code, Codex and/or pi.
#
#   curl -fsSL https://raw.githubusercontent.com/janhorecny/tally/main/install.sh | bash
#   ./install.sh                         from a clone: ask which detected agents to install into
#   ./install.sh --agents claude,pi      choose explicitly (claude, codex, pi)
#   ./install.sh --all | --yes           every supported / every detected agent, no questions
#   ./install.sh --no-cli                skip the ~/.local/bin/tally symlink
#
# Piped from curl, it clones Tally to ~/.local/share/tally (TALLY_DIR) and runs from
# there; options go after `bash -s --`. Run it again, or `tally update`, to update.
set -euo pipefail

REPO_URL="${TALLY_REPO:-https://github.com/janhorecny/tally.git}"
DEST="${TALLY_DIR:-$HOME/.local/share/tally}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# not run from a clone (curl | bash): fetch Tally first, then run its own copy of this script
SELF="${BASH_SOURCE[0]:-}"
if [ -z "$SELF" ] || [ ! -f "$(dirname "$SELF")/plugin/bin/tally" ]; then
  command -v git >/dev/null || die "git is required (macOS: xcode-select --install)"
  if [ -d "$DEST/.git" ]; then
    git -C "$DEST" pull --ff-only --quiet || die "could not update $DEST (local changes?)"
    echo "✓ Updated      $DEST"
  elif [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
    die "$DEST exists and is not a Tally clone; move it away or set TALLY_DIR"
  else
    mkdir -p "$(dirname "$DEST")"
    git clone --quiet --depth 1 "$REPO_URL" "$DEST" || die "could not clone $REPO_URL"
    echo "✓ Downloaded   $DEST"
  fi
  # questions need the terminal: stdin is this script when piped
  if [ -t 1 ] && { : </dev/tty; } 2>/dev/null; then
    exec bash "$DEST/install.sh" "$@" </dev/tty
  fi
  exec bash "$DEST/install.sh" "$@"
fi

REPO="$(cd "$(dirname "$SELF")" && pwd)"
WITH_CLI=1
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-cli) WITH_CLI=0 ;;
    -h|--help) sed -n '2,11p' "$REPO/install.sh"; exit 0 ;;
    *) ARGS+=("$arg") ;;
  esac
done

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
    *)
      case "${SHELL:-}" in */zsh) RC="~/.zshrc" ;; */bash) RC="~/.bashrc" ;; *) RC="your shell's profile" ;; esac
      echo "✓ CLI          ~/.local/bin/tally, which is not on your PATH yet. Add it:"
      echo "               echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $RC   (then open a new terminal)"
      ;;
  esac
fi
echo
python3 "$REPO/plugin/bin/tally" doctor | sed 's/^/  /'
echo
echo "  Update any time: tally update · remove: $REPO/uninstall.sh"
