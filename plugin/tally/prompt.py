"""Choices in the terminal: arrow keys, Enter, Esc (stdlib only).

pick() shows a question with its options on one line; ← → (or ↑ ↓, Tab)
move, Enter confirms, the first letters or the number of an option jump to
it, Esc or Ctrl-D cancels (Cancelled), Ctrl-C raises KeyboardInterrupt.
Without a terminal the callers fall back to typed answers.
"""

import os
import re
import select
import shutil
import sys
from typing import List, Optional

_ACCENT, _DIM, _BOLD, _RESET = "\033[1;38;5;208m", "\033[2m", "\033[1m", "\033[0m"
_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
KEYS = {"[C": "right", "[D": "left", "[A": "up", "[B": "down", "OC": "right", "OD": "left", "OA": "up",
        "OB": "down", "[Z": "left"}                      # [Z: Shift-Tab


class Cancelled(Exception):
    """Esc or Ctrl-D."""


def interactive() -> bool:
    """Arrow-key choices are possible: a terminal in and out, and termios."""
    try:
        import termios  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    except ImportError:
        return False


def width() -> int:
    return shutil.get_terminal_size((100, 24)).columns or 100       # 0: a terminal that reports no size


def visible(text: str) -> int:
    return len(_ANSI.sub("", text))


class Raw:
    """cbreak mode (keys one by one, no echo), cursor hidden; restored on exit."""

    def __enter__(self):
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        return False

    def key(self) -> str:
        """right, left, up, down, enter, esc, eof, backspace, or the character typed."""
        ch = os.read(self.fd, 1)
        if ch == b"\x1b":
            if not select.select([self.fd], [], [], 0.05)[0]:
                return "esc"
            seq = os.read(self.fd, 2)
            while select.select([self.fd], [], [], 0)[0] and len(seq) < 8 and not seq[-1:].isalpha() \
                    and seq[-1:] != b"~":
                seq += os.read(self.fd, 1)
            return KEYS.get(seq.decode("ascii", "ignore"), "")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\t":
            return "right"
        if ch == b"\x03":
            raise KeyboardInterrupt
        if ch == b"\x04":
            return "eof"
        if ch in (b"\x7f", b"\x08"):
            return "backspace"
        if ch and ch[0] >= 0xC0:                              # the rest of a UTF-8 character
            while select.select([self.fd], [], [], 0)[0] and len(ch) < 4:
                ch += os.read(self.fd, 1)
        return ch.decode("utf-8", "ignore")


def _line(label: str, options: List[str], index: int, color: bool, label_width: int, hint: bool) -> str:
    def c(code, text):
        return code + text + _RESET if color else text
    parts = [c(_ACCENT, "● " + o) if i == index else c(_DIM, "○ " + o) for i, o in enumerate(options)]
    line = "  %-*s %s" % (label_width, label, "   ".join(parts))
    if hint:
        line += c(_DIM, "   ← →")
    if visible(line) >= width():                          # too narrow for all options: the chosen one only
        line = "  %-*s %s %s" % (label_width, label, c(_ACCENT, "‹ %s ›" % options[index]),
                                 c(_DIM, "%d/%d" % (index + 1, len(options))))
    return line


def jump(options: List[str], typed: str) -> Optional[int]:
    """The option the typed letters (or number) point at."""
    if typed.isdigit() and 1 <= int(typed) <= len(options):
        return int(typed) - 1
    hits = [i for i, o in enumerate(options) if o.lower().startswith(typed.lower())]
    return hits[0] if hits else None


def _explain(text: str, color: bool) -> List[str]:
    import textwrap
    lines = textwrap.wrap(text, max(30, min(width(), 100) - 6), initial_indent="    ", subsequent_indent="    ")
    return [_DIM + x + _RESET if color else x for x in lines]


def pick(label: str, options: List[str], current: Optional[str] = None, color: bool = False,
         label_width: int = 27, explain: str = "") -> str:
    """Let the user choose one of options on one line; returns it. `explain`
    is shown under the question while it is open. The chosen value stays on
    screen after Enter, the explanation goes."""
    index = options.index(current) if current in options else 0
    typed = ""
    out = sys.stdout
    below = _explain(explain, color) if explain else []
    with Raw() as raw:
        while True:
            # the question, its explanation below, then back up to the question
            out.write("\r\033[J" + _line(label, options, index, color, label_width, True))
            if below:
                out.write("\n" + "\n".join(below) + "\033[%dA\r" % len(below))
            out.flush()
            k = raw.key()
            if k in ("right", "down"):
                index, typed = (index + 1) % len(options), ""
            elif k in ("left", "up"):
                index, typed = (index - 1) % len(options), ""
            elif k == "enter":
                break
            elif k in ("esc", "eof"):
                out.write("\r\033[J")
                raise Cancelled()
            elif k == "backspace":
                typed = ""
            elif k and k.isprintable() and len(k) == 1:
                hit = jump(options, typed + k)
                if hit is None:
                    typed, hit = k, jump(options, k)
                else:
                    typed += k
                if hit is not None:
                    index = hit
    value = options[index]
    out.write("\r\033[J  %-*s %s\n" % (label_width, label, (_ACCENT + value + _RESET) if color else value))
    out.flush()
    return value


def yes_no(label: str, current: bool, color: bool = False, label_width: int = 27, explain: str = "") -> bool:
    return pick(label, ["yes", "no"], "yes" if current else "no", color, label_width, explain) == "yes"


# ------------------------------------------------------------------ tally settings in a terminal

def settings_editor(color: bool = False) -> List[tuple]:
    """Every setting on its own line: ↑ ↓ choose one, ← → change its value
    (saved at once), digits type a number, q or Esc quits. Returns the
    (key, value) pairs that changed."""
    import textwrap
    from . import fmt, paths, settings

    keys = list(settings.SCHEMA)
    row, number, changed, note = 0, None, [], ""

    def c(code, text):
        return code + text + _RESET if color else text

    def value_of(k):
        return settings.show(settings.get(k, paths.load_config()))

    def draw():
        w = width()
        lines = [fmt.paint(fmt.head("Settings")) if color else fmt.head("Settings"),
                 c(_DIM, "  ↑ ↓ choose · ← → change, saved at once · type a number · q quits"), ""]
        group = None
        for i, k in enumerate(keys):
            g, label = settings.LABELS[k]
            if g != group:
                lines.append("  " + c(_BOLD, g))
                group = g
            here = i == row
            mark = c(_ACCENT, "›") if here else " "
            opts = settings.choices(k)
            cur = value_of(k)
            if opts:
                vals = "  ".join(c(_ACCENT, "● " + o) if o == cur else c(_DIM, "○ " + o) for o in opts)
            elif here and number is not None:
                vals = c(_ACCENT, number + "▏")
            else:
                vals = c(_ACCENT if here else "", cur)
            line = "  %s %-27s %s" % (mark, label, vals)
            if here and note:
                line += "   " + c(_DIM, note)
            lines.append(line if visible(line) < w else _ANSI.sub("", line)[: w - 1])
        k = keys[row]
        lines.append("")
        lines += [c(_DIM, x) for x in textwrap.wrap("%s · %s" % (k, settings.SCHEMA[k][1]), max(30, w - 4),
                                                       initial_indent="  ", subsequent_indent="  ")]
        return lines

    out = sys.stdout
    shown = 0
    with Raw() as raw:
        while True:
            lines = draw()
            if shown:
                out.write("\033[%dF\033[J" % shown)
            out.write("\n".join(lines) + "\n")
            out.flush()
            shown = len(lines)
            k = raw.key()
            key = keys[row]
            opts = settings.choices(key)
            if number is not None:                       # typing a number
                if k == "enter":
                    try:
                        v = settings.set_value(key, number or "0")
                        changed.append((key, v))
                        note = "✓ saved"
                    except ValueError as e:
                        note = str(e)
                    number = None
                elif k == "esc":
                    number, note = None, ""
                elif k == "backspace":
                    number = number[:-1]
                elif k.isdigit():
                    number += k
                continue
            note = ""
            if k in ("q", "esc", "eof", "enter"):
                break
            if k == "down":
                row = (row + 1) % len(keys)
            elif k == "up":
                row = (row - 1) % len(keys)
            elif k in ("right", "left") and opts:
                i = opts.index(value_of(key)) if value_of(key) in opts else 0
                try:
                    v = settings.set_value(key, opts[(i + (1 if k == "right" else -1)) % len(opts)])
                    changed.append((key, v))
                    note = "✓ saved"
                except ValueError as e:
                    note = str(e)
            elif k.isdigit() and not opts:
                number = k
        out.write("\033[%dF\033[J" % shown)
        out.flush()
    last = {}
    for key, v in changed:
        last[key] = v
    return list(last.items())
