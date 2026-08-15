#!/usr/bin/env python3
"""Edit a fenced region inside a file somebody else also owns.

Four usm commands write into files the user hand-maintains -- shell rc files,
``~/.ssh/config``, ``~/.tmux.conf`` -- and each had grown its own copy of the
same idea with its own bugs. This is that idea once:

* Only the text between the markers is ours. Everything outside is preserved
  byte for byte, including the bytes we cannot even decode.
* Re-applying updates the block in place. It never appends a second one.
* A block that is not intact -- missing end marker, markers out of order,
  duplicated, nested -- is refused rather than guessed at, and the file is
  left exactly as it was. Guessing here corrupts a file people cannot afford
  to lose.
* Writes are atomic and preserve the file's mode, so an interrupted write
  cannot truncate a shell rc and a 0600 config never quietly becomes 0644.

Markers are matched as **whole lines**. Substring matching looks equivalent
until a marker string appears inside a heredoc, a comment or a quoted string,
at which point it silently eats the wrong region.

The module deliberately raises :class:`BlockError` rather than a click
exception: it is imported by scripts that want to phrase the failure their
own way, and by tests that should not need click to check text handling.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BlockError",
    "BlockSplit",
    "ManagedBlock",
]


class BlockError(Exception):
    """The file cannot be edited safely; the caller should explain why."""


@dataclass(frozen=True)
class BlockSplit:
    """A file cut into the part before our block, the block, and the rest."""

    before: str
    block: str
    after: str
    found: bool

    @property
    def without_block(self) -> str:
        return self.before + self.after


def _split_keepends(text: str) -> list[str]:
    """Lines with their terminators, so CRLF and a missing final newline survive."""
    return text.splitlines(keepends=True)


class ManagedBlock:
    """One fenced region, identified by its begin and end marker lines."""

    def __init__(self, begin: str, end: str, *, label: str | None = None) -> None:
        if not begin.strip() or not end.strip():
            raise ValueError("markers must be non-empty")
        if begin.strip() == end.strip():
            raise ValueError("begin and end markers must differ")
        if any(ch in begin + end for ch in "\r\n"):
            raise ValueError("markers must be single lines")
        self.begin = begin
        self.end = end
        #: Used in error messages, e.g. "usm host block in ~/.ssh/config".
        self.label = label or "managed block"

    # -- pure text ---------------------------------------------------------

    def _marker_lines(self, content: str) -> tuple[list[int], list[int]]:
        begins, ends = [], []
        for index, raw in enumerate(_split_keepends(content)):
            stripped = raw.rstrip("\r\n")
            if stripped == self.begin:
                begins.append(index)
            elif stripped == self.end:
                ends.append(index)
        return begins, ends

    def split(self, content: str) -> BlockSplit:
        """Cut *content* around our block, refusing anything malformed."""
        begins, ends = self._marker_lines(content)
        if not begins and not ends:
            return BlockSplit(content, "", "", False)
        if len(begins) > 1:
            raise BlockError(f"duplicate begin marker for the {self.label}")
        if len(ends) > 1:
            raise BlockError(f"duplicate end marker for the {self.label}")
        if not begins:
            raise BlockError(
                f"incomplete {self.label}: end marker without a begin marker"
            )
        if not ends:
            raise BlockError(
                f"incomplete {self.label}: begin marker without an end marker"
            )
        start, stop = begins[0], ends[0]
        if stop < start:
            raise BlockError(
                f"incomplete {self.label}: end marker precedes the begin marker"
            )

        lines = _split_keepends(content)
        return BlockSplit(
            before="".join(lines[:start]),
            block="".join(lines[start : stop + 1]),
            after="".join(lines[stop + 1 :]),
            found=True,
        )

    def contains(self, content: str) -> bool:
        return self.split(content).found

    def body(self, content: str) -> str | None:
        """The lines between the markers, or None when there is no block."""
        split = self.split(content)
        if not split.found:
            return None
        lines = _split_keepends(split.block)
        return "".join(lines[1:-1])

    def render(self, body: str) -> str:
        """Markers around *body*, always ending in exactly one newline."""
        middle = body
        if middle and not middle.endswith("\n"):
            middle += "\n"
        return f"{self.begin}\n{middle}{self.end}\n"

    def apply(self, content: str, body: str) -> str:
        """Return *content* with our block set to *body*, in place or appended."""
        split = self.split(content)
        rendered = self.render(body)
        if split.found:
            return split.before + rendered + split.after
        if not content:
            return rendered
        # Exactly one blank line between their content and ours, however
        # their file happened to end. Appending flush against a hand-written
        # config reads as if it were part of it.
        if content.endswith("\n\n"):
            separator = ""
        elif content.endswith("\n"):
            separator = "\n"
        else:
            separator = "\n\n"
        return content + separator + rendered

    def remove(self, content: str) -> tuple[str, bool]:
        """Return *content* without our block, and whether there was one."""
        split = self.split(content)
        if not split.found:
            return content, False
        before, after = split.before, split.after
        # Drop the blank line `apply` inserts, so apply-then-remove restores
        # the file rather than slowly growing blank lines -- but only where
        # that blank line is now redundant. A blank line the user wrote
        # between two of their own stanzas is theirs to keep.
        if before.endswith("\n\n") and (after == "" or after.startswith("\n")):
            before = before[:-1]
        return before + after, True

    # -- files -------------------------------------------------------------

    def read(self, path: Path) -> str:
        """The file's text, or "" when it does not exist."""
        if not path.exists():
            return ""
        try:
            return path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlockError(
                f"{path} is not valid UTF-8; fix it manually first"
            ) from exc
        except OSError as exc:
            raise BlockError(f"cannot read {path}: {exc}") from exc

    def _resolve_target(self, path: Path, symlinks: str) -> Path:
        if not path.is_symlink():
            return path
        if symlinks == "refuse":
            raise BlockError(f"refusing to edit {path} because it is a symlink")
        if symlinks == "follow":
            try:
                return path.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise BlockError(f"cannot resolve symlink {path}: {exc}") from exc
        raise ValueError(f"unknown symlink policy {symlinks!r}")

    def write(
        self,
        path: Path,
        content: str,
        *,
        mode: int | None = None,
        dir_mode: int | None = None,
        symlinks: str = "refuse",
        backup: bool = False,
    ) -> None:
        """Replace *path* with *content*, atomically and without widening it.

        *mode* caps the result: an existing file keeps its own permissions
        masked to it, so a 0600 ssh config stays 0600 and a 0644 rc file is
        not silently tightened or loosened.
        """
        target = self._resolve_target(path, symlinks)
        if target.exists():
            if not target.is_file():
                raise BlockError(f"refusing to edit {target}: not a regular file")
            if not os.access(target, os.W_OK):
                raise BlockError(f"refusing to edit read-only {target}")

        directory = target.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if dir_mode is not None:
                os.chmod(directory, dir_mode)
        except OSError as exc:
            raise BlockError(f"cannot prepare {directory}: {exc}") from exc

        if target.exists():
            existing = target.stat().st_mode & 0o777
            final_mode = (existing & mode) or mode if mode is not None else existing
        else:
            final_mode = mode if mode is not None else 0o644

        # A crash between write and rename leaves one of these behind; clear
        # them so a stale file cannot be mistaken for the real thing.
        for stale in directory.glob(f".{target.name}.usm.*.tmp"):
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - racing another process
                pass

        if backup and target.exists():
            try:
                shutil.copy2(target, target.with_name(target.name + ".usm.bak"))
            except OSError as exc:
                raise BlockError(f"cannot back up {target}: {exc}") from exc

        tmp = directory / f".{target.name}.usm.{os.getpid()}.tmp"
        try:
            tmp.write_text(content, encoding="utf-8")
            os.chmod(tmp, final_mode)
            os.replace(tmp, target)
        except OSError as exc:
            raise BlockError(f"cannot write {target}: {exc}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover
                    pass

    def update(
        self,
        path: Path,
        body: str,
        *,
        mode: int | None = None,
        dir_mode: int | None = None,
        symlinks: str = "refuse",
        backup: bool = False,
    ) -> bool:
        """Set our block in *path* to *body*. True when the file changed."""
        target = self._resolve_target(path, symlinks)
        original = self.read(target)
        updated = self.apply(original, body)
        if updated == original:
            return False
        self.write(
            path,
            updated,
            mode=mode,
            dir_mode=dir_mode,
            symlinks=symlinks,
            backup=backup,
        )
        return True

    def remove_from(
        self,
        path: Path,
        *,
        mode: int | None = None,
        symlinks: str = "refuse",
        backup: bool = False,
    ) -> bool:
        """Drop our block from *path*. True when there was one to drop."""
        target = self._resolve_target(path, symlinks)
        original = self.read(target)
        if not original:
            return False
        updated, existed = self.remove(original)
        if not existed or updated == original:
            return False
        self.write(path, updated, mode=mode, symlinks=symlinks, backup=backup)
        return True
