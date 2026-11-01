"""Terminal output for the CLI: restrained color, the Hollis mark, tables, JSON.

Color follows the conventions scripts expect: off when stdout is not a terminal, when NO_COLOR
is set (any value), with --no-color or TERM=dumb; FORCE_COLOR forces it on. Glyphs fall back to
ASCII when the output encoding is not UTF-8 or HOLLIS_ASCII is set.
"""

import json
import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO, cast

from pydantic import BaseModel

from hollis.platform.schemas import RunPhase

# Brand palette (brand/README.md): brass accent, warm ink; 256-color fallbacks for older terminals.
_TRUE = {"brass": "38;2;217;169;78", "red": "38;2;214;92;74", "green": "38;2;111;168;106"}
_256 = {"brass": "38;5;179", "red": "38;5;167", "green": "38;5;107"}

_PHASES: dict[RunPhase, tuple[str, str, str]] = {
    # phase: (unicode glyph, ascii glyph, color)
    RunPhase.COMPLETED: ("●", "+", "green"),
    RunPhase.RUNNING: ("◐", "~", "brass"),
    RunPhase.QUEUED: ("·", ".", "dim"),
    RunPhase.WAITING_APPROVAL: ("◆", "!", "brass"),
    RunPhase.FAILED: ("✕", "x", "red"),
    RunPhase.CANCELLED: ("○", "-", "dim"),
    RunPhase.INTERRUPTED: ("◌", "?", "brass"),
}


@dataclass
class Console:
    out: TextIO
    err: TextIO
    color: bool
    unicode: bool
    json: bool = False

    @classmethod
    def detect(cls, *, json_output: bool, no_color: bool) -> "Console":
        out, err = sys.stdout, sys.stderr
        forced = bool(os.environ.get("FORCE_COLOR"))
        color = forced or (
            not no_color
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and out.isatty()
        )
        encoding = (getattr(out, "encoding", None) or "").lower().replace("-", "")
        unicode = encoding.startswith("utf") and not os.environ.get("HOLLIS_ASCII")
        return cls(
            out=out, err=err, color=color and not json_output, unicode=unicode, json=json_output
        )

    # --- styling ------------------------------------------------------------------------------

    def style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        palette = _TRUE if os.environ.get("COLORTERM") in ("truecolor", "24bit") else _256
        codes = [
            {"bold": "1", "dim": "2", "italic": "3", "underline": "4"}.get(s) or palette.get(s, "")
            for s in styles
        ]
        return f"\x1b[{';'.join(c for c in codes if c)}m{text}\x1b[0m"

    def glyph(self, unicode: str, ascii: str) -> str:
        return unicode if self.unicode else ascii

    def phase(self, phase: RunPhase) -> str:
        uni, asc, color = _PHASES[phase]
        return self.style(f"{self.glyph(uni, asc)} {phase.value.replace('_', ' ')}", color)

    def mark(self) -> tuple[str, str]:
        """The Hollis mark in two terminal lines: a ring, then a path ending in a square."""
        if self.unicode:
            return self.style("○", "brass"), self.style("└─", "brass") + self.style("■", "brass")
        return self.style("o", "brass"), self.style("`-#", "brass")

    # --- output -------------------------------------------------------------------------------

    def print(self, text: str = "") -> None:
        print(text, file=self.out)

    def note(self, text: str) -> None:
        """Secondary information, on stderr so it never pollutes piped output."""
        print(self.style(text, "dim"), file=self.err)

    def warn(self, text: str) -> None:
        print(f"{self.style('warning', 'brass', 'bold')}: {text}", file=self.err)

    def error(
        self, code: str, detail: str, hint: str | None = None, status: int | None = None
    ) -> None:
        if self.json:
            body: dict[str, Any] = {"error": {"code": code, "detail": detail}}
            if status is not None:
                body["error"]["status"] = status
            print(json.dumps(body), file=self.err)
            return
        print(f"{self.style('error', 'red', 'bold')}: {detail}", file=self.err)
        if hint:
            print(self.style(f"  {hint}", "dim"), file=self.err)

    def data(self, value: Any) -> None:
        """Machine-readable output (with --json)."""
        print(json.dumps(jsonable(value), indent=2, ensure_ascii=False), file=self.out)

    def line(self, value: Any) -> None:
        """One JSON value per line (JSON Lines, for streams)."""
        print(json.dumps(jsonable(value), ensure_ascii=False), file=self.out, flush=True)

    def fields(self, rows: Iterable[tuple[str, Any]], *, indent: int = 0) -> None:
        items = [(k, v) for k, v in rows if v not in (None, "", ())]
        if not items:
            return
        width = max(len(k) for k, _ in items)
        pad = " " * indent
        for key, value in items:
            self.print(f"{pad}{self.style(key.ljust(width), 'dim')}  {value}")

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        flex: int | None = None,
        empty: str = "Nothing here yet.",
    ) -> None:
        """Columns without borders; the `flex` column is truncated to fit the terminal."""
        if not rows:
            self.print(self.style(empty, "dim"))
            return
        widths = [max(visible_len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
        if flex is not None:
            total = sum(widths) + 2 * (len(widths) - 1)
            available = shutil.get_terminal_size((120, 24)).columns
            if total > available:
                widths[flex] = max(12, widths[flex] - (total - available))
        self.print(
            "  ".join(
                self.style(h.upper().ljust(w), "dim") for h, w in zip(headers, widths, strict=True)
            ).rstrip()
        )
        for row in rows:
            cells = [fit(cell, width) for cell, width in zip(row, widths, strict=True)]
            self.print("  ".join(cells).rstrip())


def visible_len(text: str) -> int:
    """Length without ANSI escape sequences."""
    length, escape = 0, False
    for char in text:
        if char == "\x1b":
            escape = True
        elif escape:
            escape = char != "m"
        else:
            length += 1
    return length


def fit(text: str, width: int) -> str:
    length = visible_len(text)
    if length <= width:
        return text + " " * (width - length)
    if "\x1b" in text:  # styled cells are short labels; never cut inside an escape sequence
        return text
    return text[: max(width - 1, 0)] + "…"


def short_id(value: object) -> str:
    return str(value)[:8]


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    seconds = ((now or datetime.now(UTC)) - when).total_seconds()
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)}m ago"
    if seconds < 86_400:
        return f"{round(seconds / 3600)}h ago"
    if seconds < 7 * 86_400:
        return f"{round(seconds / 86_400)}d ago"
    return when.strftime("%b %d")


def duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest}s"


def tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        return [jsonable(v) for v in items]
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(k): jsonable(v) for k, v in mapping.items()}
    return value
