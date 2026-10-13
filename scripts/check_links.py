#!/usr/bin/env python3
"""Check the relative links and #anchors of the README, the changelog and docs/.

    python3 scripts/check_links.py

Anchors follow GitHub's rules: lower case, punctuation dropped, spaces as
hyphens, a numeric suffix for repeated headings. Exits 1 if a link is broken.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set:
    seen, out, in_code = {}, set(), False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            s = slug(m.group(1))
            n = seen.get(s, 0)
            seen[s] = n + 1
            out.add(s if n == 0 else "%s-%d" % (s, n))
    return out


def main() -> int:
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md"] + sorted((ROOT / "docs").glob("*.md"))
    cache = {f.resolve(): anchors(f) for f in files}
    bad = 0
    for f in files:
        text = re.sub(r"```.*?```", "", f.read_text(), flags=re.S)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, frag = target.partition("#")
            dest = (f.parent / path).resolve() if path else f.resolve()
            if not dest.exists():
                print("%s: missing file %s" % (f.relative_to(ROOT), target))
                bad += 1
            elif frag and dest.suffix == ".md" and frag not in cache.setdefault(dest, anchors(dest)):
                print("%s: missing anchor %s" % (f.relative_to(ROOT), target))
                bad += 1
    if bad:
        print("%d broken link%s" % (bad, "" if bad == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
