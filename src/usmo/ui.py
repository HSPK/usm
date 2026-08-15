"""The usm design system: one visual language for the whole command family.

Every ``usm`` command — the launcher itself and the scripts it runs — renders
through this module, so they look like one tool rather than twenty-seven.
Scripts get it by declaring ``usmo`` in their ``requirements``:

    from usmo.ui import detail, fail, ok, table

The vocabulary, in one place:

===========  ==========================================================
``✓``        something finished
``✗``        something failed
``!``        a warning worth reading, but not fatal
``→``        a step starting, or a transition (``a → b``)
``●`` ``○``  present / absent (cached, mounted, enabled…)
===========  ==========================================================

Colour carries the same meaning everywhere: **cyan** is an identifier you
could type back (a name, an id), **dim** is context you can skip, green /
yellow / red are the three status levels. Anything that could be a secret is
redacted on the way out.

Tables never wrap: a row is one line, and columns are dropped as the
terminal narrows rather than every column being squeezed into uselessness.
Detail views are the opposite — they wrap, because silently truncating a
path is worse than a second line.

``rich`` is imported on first use, not at import time, so a command that only
prints a line or two stays fast.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console
    from rich.table import Table

__all__ = [
    "CACHED",
    "MISSING",
    "OK",
    "FAIL",
    "WARN",
    "STEP",
    "Column",
    "compact_duration",
    "console",
    "detail",
    "elide",
    "err",
    "fail",
    "hint",
    "human_bytes",
    "human_duration",
    "identifier",
    "info",
    "muted",
    "ok",
    "print",
    "redact",
    "rule",
    "SECTION",
    "shorten_path",
    "short_blob_target",
    "state",
    "step",
    "table",
    "title",
    "warn",
]

# -- vocabulary -------------------------------------------------------------

OK = "✓"
FAIL = "✗"
WARN = "!"
STEP = "→"
CACHED = "●"
MISSING = "○"

#: The same two glyphs, named for the other thing they mark. A daemon that is
#: up and a script that is downloaded are the same "present/absent" idea, and
#: giving them one shape is the point of having a design system; the aliases
#: exist so call sites read as what they mean.
RUNNING = CACHED
STOPPED = MISSING

#: Separator for composing several facts onto one line: ``a · b · c``.
DOT = "·"

STYLE_OK = "green"
STYLE_FAIL = "red"
STYLE_WARN = "yellow"
STYLE_ID = "bold cyan"
STYLE_MUTED = "dim"


# -- console ----------------------------------------------------------------

_console: Any = None
_err_console: Any = None


def console() -> "Console":
    """The shared stdout console (rich is imported on first use)."""
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console()
    return _console


def err() -> "Console":
    """The shared stderr console, for anything that isn't the result."""
    global _err_console
    if _err_console is None:
        from rich.console import Console

        _err_console = Console(stderr=True)
    return _err_console


def width(default: int = 100) -> int:
    """Terminal width, honouring ``COLUMNS`` and falling back sanely."""
    try:
        return console().width or default
    except Exception:  # pragma: no cover - console always resolves
        import shutil

        return shutil.get_terminal_size((default, 24)).columns


def print(*args, **kwargs) -> None:  # noqa: A001 - deliberate shadowing
    """``console().print``, so callers need one import."""
    console().print(*args, **kwargs)


# -- redaction --------------------------------------------------------------

_SECRET_RE = re.compile(
    r"((?:sig|signature|password|passwd|secret|token|api[-_]?key)=)[^&\s\"';]+",
    re.I,
)


def redact(text: Any) -> str:
    """Blank out anything that looks like a credential.

    Applied by every helper here, so a token cannot reach the terminal (or a
    log) just because someone interpolated a URL into a message.
    """
    return _SECRET_RE.sub(r"\1***", "" if text is None else str(text))


# -- status lines -----------------------------------------------------------


def _emit(glyph: str, style: str, message: str, *, to_err: bool = False) -> None:
    target = err() if to_err else console()
    target.print(f"[{style}]{glyph}[/{style}] {redact(message)}")


def ok(message: str, **kw) -> None:
    """``✓`` — something finished."""
    _emit(OK, STYLE_OK, message, **kw)


def fail(message: str, **kw) -> None:
    """``✗`` — something failed."""
    kw.setdefault("to_err", True)
    _emit(FAIL, STYLE_FAIL, message, **kw)


def warn(message: str, **kw) -> None:
    """``!`` — worth reading, but not fatal."""
    kw.setdefault("to_err", True)
    _emit(WARN, STYLE_WARN, message, **kw)


def step(message: str, **kw) -> None:
    """``→`` — a step starting."""
    _emit(STEP, STYLE_MUTED, message, **kw)


def info(message: str, **kw) -> None:
    """An unadorned line, still redacted."""
    target = err() if kw.get("to_err") else console()
    target.print(redact(message))


def hint(message: str, **kw) -> None:
    """Secondary guidance: what to run next, why something was skipped."""
    target = err() if kw.get("to_err") else console()
    target.print(f"[{STYLE_MUTED}]{redact(message)}[/{STYLE_MUTED}]")


def title(text: str, *, subtitle: str | None = None) -> None:
    """A section heading: the identifier, then optional context."""
    line = f"[{STYLE_ID}]{redact(text)}[/{STYLE_ID}]"
    if subtitle:
        line += f"  [{STYLE_MUTED}]{redact(subtitle)}[/{STYLE_MUTED}]"
    console().print(line)


def rule(text: str = "") -> None:
    console().rule(f"[{STYLE_MUTED}]{redact(text)}[/{STYLE_MUTED}]" if text else "")


# -- inline markup ----------------------------------------------------------


def identifier(text: Any) -> str:
    """Something the user could type back: a name, an id, a path."""
    return f"[{STYLE_ID}]{redact(text)}[/{STYLE_ID}]"


def muted(text: Any) -> str:
    return f"[{STYLE_MUTED}]{redact(text)}[/{STYLE_MUTED}]"


def state(present: bool, *, yes: str = CACHED, no: str = MISSING) -> str:
    """``●``/``○`` for a binary condition, coloured consistently."""
    return f"[{STYLE_OK}]{yes}[/{STYLE_OK}]" if present else f"[dim]{no}[/dim]"


def status(level: str, text: str) -> str:
    """Inline coloured status word: ``ok`` / ``warn`` / ``fail`` / ``muted``."""
    style = {
        "ok": STYLE_OK,
        "warn": STYLE_WARN,
        "fail": STYLE_FAIL,
        "muted": STYLE_MUTED,
    }.get(level, STYLE_MUTED)
    return f"[{style}]{redact(text)}[/{style}]"


def joined(*parts: str) -> str:
    """Compose facts onto one line with the family separator."""
    return f" {DOT} ".join(p for p in parts if p)


def status_text(flag: bool, yes: str = "running", no: str = "stopped") -> str:
    """A coloured word for a binary state, for detail views.

    Tables use the glyph (:func:`state`); a detail row reads better with the
    word, and both should agree on the colour.
    """
    return status("ok" if flag else "muted", yes if flag else no)


# -- formatting -------------------------------------------------------------


def human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


def human_duration(secs: float | None) -> str:
    """Precise, variable-width. For detail views."""
    if secs is None:
        return "-"
    secs = max(0.0, float(secs))
    if secs <= 0:
        return "0s"
    if secs < 0.1:
        # Anything under a second used to render as "0s", which is wrong for
        # the things that are actually fast: a settle window, a quick run.
        return f"{int(secs * 1000)}ms"
    if secs < 1:
        return f"{secs:.1f}s"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"


def compact_duration(secs: float | None) -> str:
    """One unit, at most four characters. For table columns.

    ``human_duration`` is precise but variable width, which is exactly what
    makes a column wrap to three lines.
    """
    if secs is None:
        return "-"
    secs = max(0.0, float(secs))
    if secs <= 0:
        return "0s"
    if secs < 0.1:
        # Anything under a second used to render as "0s", which is wrong for
        # the things that are actually fast: a settle window, a quick run.
        return f"{int(secs * 1000)}ms"
    if secs < 1:
        return f"{secs:.1f}s"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def shorten_path(path: Any) -> str:
    """Replace the home prefix with ``~``.

    Worth doing everywhere: a home like ``/home/first.last@example.com``
    otherwise eats half an 80-column table.
    """
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def short_blob_target(url: str) -> str:
    """``https://acct.blob.core.windows.net/c/a/b?sas`` → ``acct/c/a/b``.

    The scheme and endpoint are identical on every row, so showing them only
    pushes the part that differs off the edge.
    """
    import urllib.parse

    text = (url or "").split("?", 1)[0]
    parts = urllib.parse.urlsplit(text)
    if not parts.netloc:
        return text
    account = parts.netloc.split(":")[0].split(".")[0]
    path = parts.path.strip("/")
    return f"{account}/{path}" if path else account


def elide(text: Any, limit: int, *, keep: str = "tail") -> str:
    """Trim to *limit*, keeping whichever end carries the meaning.

    Paths and targets are distinguished by their tail, so that is the default.
    """
    text = str(text)
    if limit <= 0 or len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    if keep == "tail":
        return "…" + text[-(limit - 1) :]
    return text[: limit - 1] + "…"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


# -- tables -----------------------------------------------------------------


class Column:
    """One column, plus the terminal width below which it is dropped.

    Dropping a column beats shrinking every column to three characters; the
    detail view always has the full picture anyway.

    A plain class rather than a dataclass: this module is imported by every
    ``usm`` invocation and ``dataclasses`` costs ~9ms on its own.
    """

    __slots__ = (
        "header",
        "min_width",
        "max_width",
        "justify",
        "style",
        "ratio",
        "wrap",
        "hide_below",
    )

    def __init__(
        self,
        header: str,
        *,
        min_width: int | None = None,
        max_width: int | None = None,
        justify: str = "left",
        style: str | None = None,
        ratio: int | None = None,
        wrap: bool = False,
        hide_below: int = 0,
    ) -> None:
        self.header = header
        self.min_width = min_width
        self.max_width = max_width
        self.justify = justify
        self.style = style
        self.ratio = ratio
        self.wrap = wrap
        #: Hide this column when the terminal is narrower than this.
        self.hide_below = hide_below

    def visible(self, terminal_width: int) -> bool:
        return terminal_width >= self.hide_below

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Column({self.header!r}, hide_below={self.hide_below})"


#: A table wider than this is harder to scan than it is informative -- the
#: eye loses the row on the way back from the last column. Tables stop here
#: however wide the terminal happens to be.
MAX_TABLE_WIDTH = 96


def _fitted_table_class():
    """Build (once) a Table that stops at its content instead of the margin.

    Rich sizes a table one of two ways, and neither is what we want on its
    own. Left alone, a table is exactly as wide as its content but, when that
    content overflows, rich shrinks every column it is allowed to -- and ours
    are all ``no_wrap``, so it ellipsizes the short identifying columns just
    as readily as the long path. Set ``expand`` and ``ratio`` instead and the
    squeeze lands only on the flexible column, but the table now always fills
    the terminal, trailing a rule far past the last character.

    So: measure the content, then pin the width to it (or to the cap, if the
    content is bigger). Rich's expand machinery does the distribution, but
    against the content width rather than the terminal width.
    """
    from rich.table import Table

    class FittedTable(Table):
        #: Fit to content, rather than filling the terminal.
        fit: bool = True
        #: Stop growing here even if the content would go further.
        fit_cap: int | None = MAX_TABLE_WIDTH

        def __rich_console__(self, console, options):
            if self.width is not None or not self.fit:
                yield from super().__rich_console__(console, options)
                return
            from rich.measure import Measurement

            natural = Measurement.get(console, options, self).maximum
            if self.title:
                # Otherwise a table narrower than its own title wraps the
                # title one character per line.
                natural = max(natural, _plain_width(self.title))
            cap = min(self.fit_cap or options.max_width, options.max_width)
            self.width = max(1, min(natural, cap))
            try:
                yield from super().__rich_console__(console, options)
            finally:
                self.width = None

    return FittedTable


_fitted_table = None


def _plain_width(text) -> int:
    """Width of a title once rich markup is discounted."""
    from rich.text import Text

    return len(Text.from_markup(str(text)).plain)


def table(
    *columns: "Column | str | tuple[str, dict]",
    title: str | None = None,
    expand: bool = False,
    terminal_width: int | None = None,
    max_width: int | None = MAX_TABLE_WIDTH,
) -> "Table":
    """A table in the family style: thin rule, no wrapping, one line per row.

    Sizes to its content rather than to the terminal, so the rule stops where
    the data does. When the content does not fit, the squeeze lands on the
    flexible (``ratio``) column and the identifying ones stay legible.

    Accepts :class:`Column` objects (which may declare ``hide_below``), plain
    header strings, or ``(header, {rich kwargs})`` pairs.
    """
    global _fitted_table
    from rich import box

    if _fitted_table is None:
        _fitted_table = _fitted_table_class()

    term = terminal_width if terminal_width is not None else width()
    built = _fitted_table(
        title=title,
        title_justify="left",
        title_style="bold",
        box=box.SIMPLE_HEAD,
        header_style=STYLE_MUTED,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        expand=True,
    )
    built.fit = not expand
    built.fit_cap = max_width
    for spec in columns:
        if isinstance(spec, Column):
            if not spec.visible(term):
                continue
            built.add_column(
                spec.header,
                justify=spec.justify,
                style=spec.style,
                min_width=spec.min_width,
                max_width=spec.max_width,
                ratio=spec.ratio,
                no_wrap=not spec.wrap,
                overflow="fold" if spec.wrap else "ellipsis",
            )
            continue
        header, opts = spec if isinstance(spec, tuple) else (spec, {})
        opts = dict(opts)
        opts.setdefault("no_wrap", True)
        opts.setdefault("overflow", "ellipsis")
        built.add_column(header, **opts)
    return built


def visible_columns(
    columns: Sequence[Column], terminal_width: int | None = None
) -> list[Column]:
    """The columns :func:`table` would keep — use it to build matching rows."""
    term = terminal_width if terminal_width is not None else width()
    return [c for c in columns if c.visible(term)]


def row_for(
    columns: Sequence[Column],
    values: dict,
    terminal_width: int | None = None,
) -> list:
    """Pick the values for the columns that survived, keyed by header.

    A cell must be text. Passing a list or a dict is a programming error that
    rich reports as an unrenderable object with no idea which column it came
    from -- which is how `usm host ls` once crashed for every host that had a
    tag. Numbers are converted, containers are refused by name.
    """
    row = []
    for column in visible_columns(columns, terminal_width):
        value = values.get(column.header, "")
        if isinstance(value, (list, tuple, set, dict)):
            raise TypeError(
                f"column {column.header!r} got a {type(value).__name__}; "
                "a table cell must be text"
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        row.append(value)
    return row


# -- detail views -----------------------------------------------------------

#: Marker for a blank separator row in :func:`detail`.
SECTION = object()


def detail(rows: Iterable, *, key_style: str = STYLE_MUTED) -> "Table":
    """Key/value view for ``status``-style output.

    Values wrap rather than truncate: unlike a listing, a detail view has room,
    and cutting a path in half is worse than a second line.

    Insert :data:`SECTION` on its own to separate groups, or ``(SECTION,
    "Label")`` to give the group a heading -- a long status view is much
    easier to scan when its parts are named.
    """
    from rich.table import Table

    built = Table(box=None, show_header=False, pad_edge=False)
    built.add_column(style=key_style, no_wrap=True)
    built.add_column(overflow="fold")
    first = True
    for row in rows:
        if row is SECTION:
            built.add_row("", "")
            first = False
            continue
        key, value = row
        if key is SECTION:
            if not first:
                built.add_row("", "")
            built.add_row(f"[not dim bold]{redact(value)}[/not dim bold]", "")
            first = False
            continue
        built.add_row(str(key), "" if value is None else redact(value))
        first = False
    return built


def print_detail(rows: Iterable, **kwargs) -> None:
    console().print(detail(rows, **kwargs))


def print_table(built: "Table", *, footer: str | None = None) -> None:
    console().print(built)
    if footer:
        hint(footer)


def legend(*pairs: tuple[str, str]) -> str:
    """A footer explaining the glyphs used above it."""
    return "  ".join(f"{glyph} {meaning}" for glyph, meaning in pairs)
