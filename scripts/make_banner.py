#!/usr/bin/env python3
"""Generate the README banner (light + dark SVG) into docs/assets/.

    python3 scripts/make_banner.py

Colours follow the dashboard palette (plugin/tally/dashboard.html).
"""

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"
W, H = 1280, 400

THEMES = {
    "light": {
        "bg": "#fcfcfb", "border": "#e1e0d9", "dots": "#d6d5ce",
        "card": "#ffffff", "card_border": "#e1e0d9", "card_head": "#f3f2ee",
        "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "accent": "#eb6834", "bar": "#2a78d6", "track": "#ebeae4",
        "chip": "#f3f2ee", "chip_border": "#e1e0d9",
        "dot1": "#e34948", "dot2": "#eda100", "dot3": "#1baf7a",
    },
    "dark": {
        "bg": "#0d0d0d", "border": "#2c2c2a", "dots": "#262624",
        "card": "#1a1a19", "card_border": "#2c2c2a", "card_head": "#222220",
        "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "accent": "#d95926", "bar": "#3987e5", "track": "#2c2c2a",
        "chip": "#1a1a19", "chip_border": "#2c2c2a",
        "dot1": "#e66767", "dot2": "#c98500", "dot3": "#199e70",
    },
}

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def logo(c, x, y, h=76, gap=18, sw=9):
    """Four tally strokes and the diagonal fifth."""
    parts = []
    for i in range(4):
        xi = x + i * gap
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="%d" stroke-linecap="round"/>'
                     % (xi, y, xi, y + h, c["ink"], sw))
    x1, y1, x2, y2 = x - 12, y + h - 10, x + 3 * gap + 12, y + 10
    # surface-coloured halo keeps the diagonal readable where it crosses the strokes
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="%d" stroke-linecap="round"/>'
                 % (x1, y1, x2, y2, c["bg"], sw + 6))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="%d" stroke-linecap="round"/>'
                 % (x1, y1, x2, y2, c["accent"], sw))
    return "\n  ".join(parts)


def chip(c, x, y, text, width):
    return ('<rect x="%d" y="%d" width="%d" height="34" rx="17" fill="%s" stroke="%s"/>'
            '<text x="%d" y="%d" font-family="%s" font-size="15" fill="%s">%s</text>'
            % (x, y, width, c["chip"], c["chip_border"], x + 16, y + 22, SANS, c["ink2"], esc(text)))


def terminal(c, x, y, w, h):
    rows = [  # label, share of task, value
        ("main session", 0.74, "3.10M"),
        ("├ explore auth", 0.10, "410k"),
        ("│ └ fork · tests", 0.022, "92k"),
        ("└ workflow ×25", 0.17, "700k"),
    ]
    out = [
        '<rect x="%d" y="%d" width="%d" height="%d" rx="14" fill="%s" stroke="%s"/>' % (x, y, w, h, c["card"], c["card_border"]),
        '<path d="M%d %d h%d a14 14 0 0 1 14 14 v22 h-%d v-22 a14 14 0 0 1 14 -14 z" fill="%s"/>'
        % (x + 14, y, w - 28, w, c["card_head"]),
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s"/>' % (x, y + 36, x + w, y + 36, c["card_border"]),
    ]
    for i, col in enumerate((c["dot1"], c["dot2"], c["dot3"])):
        out.append('<circle cx="%d" cy="%d" r="6" fill="%s"/>' % (x + 24 + i * 20, y + 18, col))
    out.append('<text x="%d" y="%d" font-family="%s" font-size="13" fill="%s" text-anchor="middle">claude</text>'
               % (x + w / 2, y + 23, MONO, c["muted"]))
    tx = x + 26
    ty = y + 74
    out.append('<text x="%d" y="%d" font-family="%s" font-size="15" xml:space="preserve"><tspan fill="%s">&gt; </tspan>'
               '<tspan fill="%s" font-weight="600">/tally:end</tspan><tspan fill="%s"> shipped</tspan></text>'
               % (tx, ty, MONO, c["muted"], c["accent"], c["ink2"]))
    out.append('<text x="%d" y="%d" font-family="%s" font-size="15" font-weight="700" fill="%s">'
               '■ Task #3 · Fix login redirect loop</text>' % (tx, ty + 34, MONO, c["ink"]))
    out.append('<text x="%d" y="%d" font-family="%s" font-size="14" fill="%s">'
               '4.21M tokens · $3.12 · 44m wall · 31m active</text>' % (tx, ty + 60, MONO, c["ink2"]))
    bar_x, bar_w = x + 200, w - 300
    ry = ty + 100
    for label, share, value in rows:
        out.append('<text x="%d" y="%d" font-family="%s" font-size="14" fill="%s">%s</text>'
                   % (tx, ry, MONO, c["ink2"], esc(label)))
        out.append('<rect x="%d" y="%d" width="%d" height="8" rx="4" fill="%s"/>' % (bar_x, ry - 9, bar_w, c["track"]))
        out.append('<rect x="%d" y="%d" width="%.1f" height="8" rx="4" fill="%s"/>'
                   % (bar_x, ry - 9, max(8, bar_w * share), c["bar"]))
        out.append('<text x="%d" y="%d" font-family="%s" font-size="14" fill="%s" text-anchor="end">%s</text>'
                   % (x + w - 26, ry, MONO, c["ink"], value))
        ry += 28
    return "\n  ".join(out)


def banner(theme: str) -> str:
    c = THEMES[theme]
    title = "Tally: per-task token and time tracking for Claude Code, Codex and pi"
    return """<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{title}">
  <title>{title}</title>
  <defs>
    <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">
      <circle cx="2" cy="2" r="1.6" fill="{dots}"/>
    </pattern>
    <radialGradient id="fade" cx="78%" cy="45%" r="70%">
      <stop offset="0" stop-color="#fff" stop-opacity="1"/>
      <stop offset="1" stop-color="#fff" stop-opacity="0"/>
    </radialGradient>
    <mask id="dotmask"><rect width="{W}" height="{H}" fill="url(#fade)"/></mask>
    <clipPath id="round"><rect width="{W}" height="{H}" rx="18"/></clipPath>
  </defs>
  <g clip-path="url(#round)">
  <rect width="{W}" height="{H}" fill="{bg}"/>
  <rect width="{W}" height="{H}" fill="url(#dots)" mask="url(#dotmask)"/>
  </g>
  <rect x="0.5" y="0.5" width="{W1}" height="{H1}" rx="18" fill="none" stroke="{border}"/>
  {logo}
  <text x="196" y="172" font-family="{SANS}" font-size="92" font-weight="800" letter-spacing="-2" fill="{ink}">Tally</text>
  <text x="74" y="230" font-family="{SANS}" font-size="27" fill="{ink2}">Per-task token &amp; time tracking</text>
  <text x="74" y="266" font-family="{SANS}" font-size="27" fill="{ink2}">for <tspan fill="{ink}" font-weight="600">Claude Code</tspan>, <tspan fill="{ink}" font-weight="600">Codex</tspan> and <tspan fill="{ink}" font-weight="600">pi</tspan></text>
  {chip1}
  {chip2}
  {chip3}
  {terminal}
</svg>
""".format(
        W=W, H=H, W1=W - 1, H1=H - 1, title=esc(title), SANS=SANS,
        logo=logo(c, 86, 96),
        chip1=chip(c, 74, 302, "zero-token commands", 186),
        chip2=chip(c, 270, 302, "subagents & workflows", 198),
        chip3=chip(c, 478, 302, "local SQLite", 124),
        terminal=terminal(c, 700, 58, 508, 284),
        **c,
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        path = OUT / ("banner-%s.svg" % theme)
        path.write_text(banner(theme))
        print("wrote", path.relative_to(OUT.parent.parent), "%d bytes" % path.stat().st_size)


if __name__ == "__main__":
    main()
