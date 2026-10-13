#!/usr/bin/env bash
# Publish the version in plugin/tally/__init__.py: checks, push, tag, GitHub release.
#
#   scripts/release.sh            the version must be committed on main, with its CHANGELOG section on top
#   scripts/release.sh --dry-run  only the checks
#
# Installed copies learn about a new version from the latest GitHub release (this
# script creates it), once a day; `tally update` pulls main. Push only finished versions.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

V="$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+?)\"", open("plugin/tally/__init__.py").read()).group(1))')"
python3 - "$V" <<'EOF' || exit 1
import json, re, sys
v = sys.argv[1]
found = {
    "plugin/.claude-plugin/plugin.json": json.load(open("plugin/.claude-plugin/plugin.json"))["version"],
    ".claude-plugin/marketplace.json (metadata)": json.load(open(".claude-plugin/marketplace.json"))["metadata"]["version"],
    ".claude-plugin/marketplace.json (plugin)": json.load(open(".claude-plugin/marketplace.json"))["plugins"][0]["version"],
}
bad = {k: x for k, x in found.items() if x != v}
if bad:
    sys.exit("error: version %s in __init__.py, but %s" % (v, ", ".join("%s in %s" % (x, k) for k, x in bad.items())))
top = re.search(r"^## (\S+)", open("CHANGELOG.md").read(), re.M)
if not top or top.group(1) != v:
    sys.exit("error: CHANGELOG.md does not start with a section for %s" % v)
EOF
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "not on main"
[ -z "$(git status --porcelain)" ] || die "uncommitted changes"
git rev-parse -q --verify "refs/tags/v$V" >/dev/null && die "tag v$V exists already; bump the version first"
scripts/check.sh || die "checks failed"
echo "Tally $V: ready"
[ "$DRY" = 1 ] && exit 0

NOTES="$(awk -v v="$V" '$0 ~ "^## " v " " {on=1; next} /^## / {if (on) exit} on' CHANGELOG.md)"
git push origin main
git tag -a "v$V" -m "Tally $V"
git push origin "v$V"
if command -v gh >/dev/null; then
  printf '%s\n' "$NOTES" | gh release create "v$V" --title "Tally $V" --notes-file - --verify-tag
else
  echo "gh not found: create the GitHub release for v$V by hand"
fi
echo "✓ released $V"
