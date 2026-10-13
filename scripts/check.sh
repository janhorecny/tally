#!/usr/bin/env bash
# Every check of this repository, run locally. GitHub Actions is off (macOS runners
# are billed at ten times the minutes); .githooks/pre-push runs the quick checks.
#
#   scripts/check.sh          tests (python3), lint, byte-compile, manifests, doc links   ~10 s
#   scripts/check.sh --all    also Python 3.8, 3.11 and 3.13 (uv) and Linux (Docker)     ~1-2 min
#
# A missing tool (uv, docker, claude) is skipped with a note, never a failure.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2
ALL=0
for arg in "$@"; do
  case "$arg" in
    --all) ALL=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)"; exit 2 ;;
  esac
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then OK=$'\033[32m✓\033[0m'; BAD=$'\033[31m✗\033[0m'; DIM=$'\033[2m'; END=$'\033[0m'
else OK="✓"; BAD="✗"; DIM=""; END=""; fi
failed=0

step() {   # step "label" command...
  local label="$1"; shift
  local t0 out rc
  t0=$(date +%s)
  out=$("$@" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    printf '  %s %s %s(%ss)%s\n' "$OK" "$label" "$DIM" "$(( $(date +%s) - t0 ))" "$END"
  else
    printf '  %s %s\n' "$BAD" "$label"
    printf '%s\n' "$out" | tail -n 40 | sed 's/^/      /'
    failed=1
  fi
}
skip() { printf '  - %s %s(skipped: %s)%s\n' "$1" "$DIM" "$2" "$END"; }

echo "Tally checks"
step "tests, $(python3 -c 'import sys; print("Python %d.%d" % sys.version_info[:2])')" \
  python3 -m unittest discover -s tests -q
if command -v uvx >/dev/null 2>&1; then
  step "lint (ruff: undefined names, unused imports, syntax)" \
    uvx ruff check --isolated --quiet --select F,E9 plugin scripts tests
else skip "lint" "uvx not found"; fi
step "byte-compile" python3 -m compileall -q plugin scripts
if command -v claude >/dev/null 2>&1; then
  step "plugin and marketplace manifests" sh -c 'claude plugin validate plugin && claude plugin validate .'
else skip "plugin manifests" "claude not found"; fi
step "links in README, CHANGELOG and docs" python3 scripts/check_links.py

if [ $ALL = 1 ]; then
  if command -v uv >/dev/null 2>&1; then
    for v in 3.8 3.11 3.13; do
      step "tests, Python $v (uv)" uv run --no-project --python "$v" python -m unittest discover -s tests -q
    done
  else skip "Python 3.8 / 3.11 / 3.13" "uv not found"; fi
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    for v in 3.8 3.13; do
      step "tests, Linux, Python $v (Docker)" docker run --rm -v "$REPO":/src:ro -w /src -e PYTHONDONTWRITEBYTECODE=1 \
        "python:$v-slim" python -m unittest discover -s tests -q
    done
  else skip "Linux" "Docker is not running"; fi
fi

if [ $failed = 0 ]; then echo "All checks passed."; else echo "Some checks failed."; fi
exit $failed
