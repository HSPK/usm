#!/usr/bin/env python3
"""Pack and restore the sessions your coding agents keep on disk.

Copilot CLI, Codex and Claude Code each keep their history in a directory
under your home. Moving to another machine, rebuilding a box, or just
wanting last month's transcripts back means copying those directories --
which is where it gets interesting:

* **They are live SQLite.** Copilot and Codex keep session state in SQLite
  with write-ahead logging. Copying the ``.db`` while the agent is running
  gives you a torn snapshot, and copying the ``-wal`` alongside it is only
  right if nothing writes in between. These are snapshotted through
  SQLite's own backup API instead, which is consistent under a live writer.
* **They contain things tar cannot carry.** Codex leaves a unix socket in
  ``~/.codex/ipc``; sockets, fifos and devices are skipped rather than
  archived as something that will not restore.
* **They are mostly enormous JSONL.** A single session's ``events.jsonl``
  runs to hundreds of megabytes and compresses about 5x with gzip.

Examples:
  usm convo ls                        # what's on this machine
  usm convo pack                      # everything, to ./usm-convo-<host>-<date>.tar.gz
  usm convo pack --tool copilot -o /backup/copilot.tar.gz
  usm convo pack --compress xz        # ~8x instead of ~5x, much slower
  usm convo info archive.tar.gz       # what's inside, without unpacking
  usm convo restore archive.tar.gz --dry-run
  usm convo restore archive.tar.gz --merge
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
import shutil
import socket
import sqlite3
import stat
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import click
from usmo import ui

CONTEXT = {"help_option_names": ["-h", "--help"]}

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

#: Noise every agent leaves behind: rebuildable, machine-specific, or not a
#: file at all. Patterns match the path relative to the tool's root.
GENERIC_JUNK = (
    "*-wal",
    "*-shm",
    "*/node_modules/*",
    "node_modules/*",
    ".DS_Store",
    "*/.DS_Store",
    "tmp/*",
    ".tmp/*",
    "*/tmp/*",
    "ipc/*",
    "*.sock",
    "*/*.sock",
)

#: Logs are useful when debugging the agent and dead weight otherwise, so
#: they are separated from the junk above and kept behind --include-logs.
LOG_PATTERNS = (
    "logs/*",
    "*/logs/*",
    "*.log",
    "*/*.log",
)

#: Files whose names suggest credentials. They are still packed by default --
#: this is your own data going to your own machine -- but they are counted so
#: the summary can say so, and --exclude-secrets drops them.
SECRET_PATTERNS = (
    "*token*",
    "*credential*",
    "*.key",
    "*.pem",
    "auth.json",
    "*/auth.json",
    "*apikey*",
    "*api_key*",
)

COMPRESSORS = {
    "gz": ("w:gz", ".tar.gz"),
    "xz": ("w:xz", ".tar.xz"),
    "bz2": ("w:bz2", ".tar.bz2"),
    "none": ("w", ".tar"),
}


# ==========================================================================
# Tool registry
# ==========================================================================


@dataclass(frozen=True)
class Tool:
    """One agent's on-disk state.

    The whole root is packed rather than a hand-picked list of session
    subdirectories: these tools move fast, and a layout guess that goes
    stale silently drops someone's history.
    """

    name: str
    label: str
    relative_root: str
    junk: tuple[str, ...] = ()

    def root(self, home: Path | None = None) -> Path:
        return (home or Path.home()) / self.relative_root

    def exists(self, home: Path | None = None) -> bool:
        return self.root(home).is_dir()


TOOLS: tuple[Tool, ...] = (
    Tool("copilot", "GitHub Copilot CLI", ".copilot", ("restart/*", "servers/*")),
    Tool("codex", "Codex", ".codex", ()),
    Tool("claude", "Claude Code", ".claude", ("shell-snapshots/*", "statsig/*")),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def resolve_tools(names: Iterable[str]) -> list[Tool]:
    chosen = []
    for name in names:
        tool = TOOLS_BY_NAME.get(name.lower())
        if tool is None:
            known = ", ".join(sorted(TOOLS_BY_NAME))
            raise click.ClickException(f"Unknown tool {name!r}. Known: {known}.")
        if tool not in chosen:
            chosen.append(tool)
    return chosen


# ==========================================================================
# Selecting what to pack
# ==========================================================================


def matches_any(relpath: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(relpath, pattern) for pattern in patterns)


def is_secret(relpath: str) -> bool:
    return matches_any(relpath.lower(), SECRET_PATTERNS)


@dataclass
class Selection:
    """What a pack would contain, decided before anything is written."""

    files: list[tuple[Path, str]] = field(default_factory=list)
    total_bytes: int = 0
    skipped_special: int = 0
    skipped_unreadable: int = 0
    excluded: int = 0
    secrets: int = 0
    databases: list[Path] = field(default_factory=list)


def walk_tool(
    tool: Tool,
    *,
    home: Path | None = None,
    include_logs: bool = False,
    include_junk: bool = False,
    exclude_secrets: bool = False,
    extra_excludes: Iterable[str] = (),
) -> Selection:
    """Decide what to pack for one tool, skipping what cannot be restored."""
    selection = Selection()
    root = tool.root(home)
    if not root.is_dir():
        return selection

    excludes: list[str] = list(extra_excludes)
    if not include_junk:
        excludes += list(GENERIC_JUNK) + list(tool.junk)
    if not include_logs:
        excludes += list(LOG_PATTERNS)

    for path in sorted(_walk(root)):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - _walk stays under root
            continue
        try:
            info = path.lstat()
        except OSError:
            selection.skipped_unreadable += 1
            continue
        # A socket or fifo cannot be meaningfully restored, and tar either
        # refuses it or stores something that will not come back.
        if not stat.S_ISREG(info.st_mode):
            selection.skipped_special += 1
            continue
        if matches_any(rel, excludes):
            selection.excluded += 1
            continue
        secret = is_secret(rel)
        if secret and exclude_secrets:
            selection.excluded += 1
            continue
        if not os.access(path, os.R_OK):
            selection.skipped_unreadable += 1
            continue
        if secret:
            selection.secrets += 1
        if is_sqlite(path):
            selection.databases.append(path)
        selection.files.append((path, f"{tool.name}/{rel}"))
        selection.total_bytes += info.st_size
    return selection


def _walk(root: Path) -> Iterator[Path]:
    """Every entry under *root*, not following symlinked directories."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
            except OSError:
                continue
            yield path


# ==========================================================================
# SQLite
# ==========================================================================


def is_sqlite(path: Path) -> bool:
    """Detect by magic rather than by suffix; these tools use both."""
    if path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def snapshot_sqlite(src: Path, dst: Path) -> bool:
    """Copy a possibly-live database consistently. False if it isn't one.

    The backup API takes a read lock per page and copes with a writer
    running throughout, which a plain file copy does not: with WAL enabled
    the committed state lives partly in the -wal file, so a naive copy can
    land mid-transaction.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = destination = None
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5.0)
        destination = sqlite3.connect(str(dst))
        source.backup(destination)
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        for handle in (destination, source):
            if handle is not None:
                try:
                    handle.close()
                except sqlite3.Error:  # pragma: no cover
                    pass


# ==========================================================================
# Packing
# ==========================================================================


def default_archive_name(compress: str) -> str:
    suffix = COMPRESSORS[compress][1]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"usm-convo-{socket.gethostname().split('.')[0]}-{stamp}{suffix}"


def build_manifest(selections: dict[str, Selection]) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": socket.gethostname(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        "platform": platform.platform(),
        "tools": {
            name: {
                "root": TOOLS_BY_NAME[name].relative_root,
                "label": TOOLS_BY_NAME[name].label,
                "files": len(selection.files),
                "bytes": selection.total_bytes,
                "databases": len(selection.databases),
                "secrets": selection.secrets,
            }
            for name, selection in selections.items()
        },
    }


def pack(
    selections: dict[str, Selection],
    destination: Path,
    *,
    compress: str = "gz",
    on_file=None,
) -> dict[str, Any]:
    """Write the archive. Returns the manifest that went into it."""
    mode, _ = COMPRESSORS[compress]
    manifest = build_manifest(selections)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so an interrupted pack never
    # leaves something that looks like a complete backup.
    tmp = destination.with_name(f".{destination.name}.partial")
    staging = Path(tempfile.mkdtemp(prefix="usm-convo-"))
    try:
        with tarfile.open(tmp, mode) as archive:
            payload = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(payload)
            info.mtime = int(time.time())
            archive.addfile(info, io_bytes(payload))

            for selection in selections.values():
                database_paths = set(selection.databases)
                for path, arcname in selection.files:
                    if on_file is not None:
                        on_file(arcname)
                    if path in database_paths:
                        snap = staging / arcname.replace("/", "_")
                        if snapshot_sqlite(path, snap):
                            archive.add(snap, arcname=arcname, recursive=False)
                            continue
                    try:
                        archive.add(path, arcname=arcname, recursive=False)
                    except (OSError, ValueError):
                        # Vanished or became unreadable mid-pack; the rest of
                        # the backup is still worth having.
                        continue
        tmp.replace(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if tmp.exists():
            tmp.unlink()
    return manifest


def io_bytes(payload: bytes):
    import io

    return io.BytesIO(payload)


# ==========================================================================
# Restoring
# ==========================================================================


class UnsafeMember(Exception):
    """An archive member that would write outside the destination."""


def safe_member_path(name: str, destination: Path) -> Path:
    """Where *name* may be written, or refuse it.

    An archive is untrusted input even when you made it: a member called
    ``../../.ssh/authorized_keys`` or ``/etc/passwd`` is the classic way to
    turn "restore my sessions" into remote code execution.
    """
    if not name or name in (".", "/"):
        raise UnsafeMember(f"empty member name: {name!r}")
    pure = Path(name)
    if pure.is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise UnsafeMember(f"absolute path in archive: {name}")
    if any(part == ".." for part in pure.parts):
        raise UnsafeMember(f"parent traversal in archive: {name}")
    target = (destination / pure).resolve()
    root = destination.resolve()
    if target != root and root not in target.parents:
        raise UnsafeMember(f"member escapes the destination: {name}")
    return target


def read_manifest(archive_path: Path) -> dict[str, Any]:
    try:
        with tarfile.open(archive_path) as archive:
            member = archive.extractfile(MANIFEST_NAME)
            if member is None:
                raise click.ClickException(f"{archive_path} has no {MANIFEST_NAME}.")
            return json.loads(member.read())
    except tarfile.TarError as exc:
        raise click.ClickException(f"cannot read {archive_path}: {exc}") from exc
    except KeyError as exc:
        raise click.ClickException(
            f"{archive_path} is not a usm convo archive (no {MANIFEST_NAME})."
        ) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"{archive_path} has a corrupt manifest.") from exc


@dataclass
class RestoreReport:
    written: int = 0
    skipped_existing: int = 0
    refused: list[str] = field(default_factory=list)
    tools: set[str] = field(default_factory=set)
    bytes: int = 0


def restore(
    archive_path: Path,
    *,
    home: Path | None = None,
    into: Path | None = None,
    only: Iterable[str] = (),
    merge: bool = False,
    dry_run: bool = False,
) -> RestoreReport:
    """Extract an archive back into place, refusing anything unsafe."""
    report = RestoreReport()
    wanted = {tool.name for tool in resolve_tools(only)} if only else None
    base_home = home or Path.home()

    with tarfile.open(archive_path) as archive:
        for member in archive:
            if member.name == MANIFEST_NAME:
                continue
            if not member.isfile():
                # Directories are implied by the files; links and devices are
                # the other half of the traversal problem.
                if not member.isdir():
                    report.refused.append(member.name)
                continue
            tool_name, _, rel = member.name.partition("/")
            if not rel:
                report.refused.append(member.name)
                continue
            if wanted is not None and tool_name not in wanted:
                continue
            tool = TOOLS_BY_NAME.get(tool_name)
            if tool is None:
                report.refused.append(member.name)
                continue

            destination = into / tool_name if into else tool.root(base_home)
            try:
                target = safe_member_path(rel, destination)
            except UnsafeMember:
                report.refused.append(member.name)
                continue

            report.tools.add(tool_name)
            if target.exists() and not merge:
                report.skipped_existing += 1
                continue
            report.written += 1
            report.bytes += member.size
            if dry_run:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover - isfile() already checked
                continue
            with open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(target, member.mode & 0o777 or 0o600)
    return report


# ==========================================================================
# Presentation
# ==========================================================================


def newest_mtime(root: Path) -> float | None:
    newest = None
    for path in _walk(root):
        try:
            mtime = path.lstat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def print_tools(home: Path | None = None) -> None:
    columns = [
        ui.Column("", justify="center", min_width=1),
        ui.Column("tool", style=ui.STYLE_ID, min_width=7),
        ui.Column("state", min_width=10, ratio=1),
        ui.Column("size", justify="right", min_width=6),
        ui.Column("files", justify="right", min_width=5, hide_below=64),
        ui.Column("last used", justify="right", min_width=9, hide_below=76),
    ]
    built = ui.table(*columns, title="Agent sessions")
    found = 0
    for tool in TOOLS:
        root = tool.root(home)
        present = root.is_dir()
        selection = walk_tool(tool, home=home) if present else Selection()
        if present:
            found += 1
        newest = newest_mtime(root) if present else None
        built.add_row(
            *ui.row_for(
                columns,
                {
                    "": ui.state(present),
                    "tool": tool.name,
                    "state": ui.shorten_path(root) if present else "not present",
                    "size": ui.human_bytes(selection.total_bytes) if present else "—",
                    "files": str(len(selection.files)) if present else "—",
                    "last used": ui.compact_duration(time.time() - newest) + " ago"
                    if newest
                    else "—",
                },
            )
        )
    ui.print(built)
    if not found:
        ui.hint("Nothing to pack: none of these agents has state in your home.")
    else:
        ui.hint(
            ui.joined(
                f"{found}/{len(TOOLS)} present",
                "logs and caches excluded by default",
                "usm convo pack to archive",
            )
        )


# ==========================================================================
# CLI
# ==========================================================================


@click.group(context_settings=CONTEXT, invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Pack and restore Copilot / Codex / Claude Code sessions."""
    if ctx.invoked_subcommand is None:
        print_tools()


@cli.command("ls")
def cmd_ls():
    """Show which agents have state on this machine."""
    print_tools()


def _tool_option(fn):
    return click.option(
        "--tool",
        "tools",
        multiple=True,
        help="Limit to this agent (repeatable). Default: all present.",
    )(fn)


@cli.command("pack")
@click.argument("output", required=False, type=click.Path(path_type=Path))
@_tool_option
@click.option(
    "-o",
    "--output",
    "output_opt",
    type=click.Path(path_type=Path),
    help="Archive path.",
)
@click.option(
    "-c",
    "--compress",
    type=click.Choice(sorted(COMPRESSORS)),
    default="gz",
    show_default=True,
    help="gz is ~5x and instant; xz is ~8x and much slower.",
)
@click.option("--include-logs", is_flag=True, help="Also pack agent log files.")
@click.option("--include-junk", is_flag=True, help="Also pack caches and temp files.")
@click.option(
    "--exclude-secrets", is_flag=True, help="Drop files that look like credentials."
)
@click.option(
    "--exclude", "extra", multiple=True, help="Skip paths matching this glob."
)
@click.option("--dry-run", is_flag=True, help="Report what would be packed.")
def cmd_pack(
    output,
    tools,
    output_opt,
    compress,
    include_logs,
    include_junk,
    exclude_secrets,
    extra,
    dry_run,
):
    """Archive agent sessions into one compressed file."""
    chosen = resolve_tools(tools) if tools else [t for t in TOOLS if t.exists()]
    if not chosen:
        raise click.ClickException(
            "No agent state found. `usm convo ls` shows what was looked for."
        )

    selections: dict[str, Selection] = {}
    for tool in chosen:
        selections[tool.name] = walk_tool(
            tool,
            include_logs=include_logs,
            include_junk=include_junk,
            exclude_secrets=exclude_secrets,
            extra_excludes=extra,
        )

    total_files = sum(len(s.files) for s in selections.values())
    total_bytes = sum(s.total_bytes for s in selections.values())
    databases = sum(len(s.databases) for s in selections.values())
    secrets = sum(s.secrets for s in selections.values())
    special = sum(s.skipped_special for s in selections.values())
    unreadable = sum(s.skipped_unreadable for s in selections.values())

    if not total_files:
        raise click.ClickException("Nothing to pack after exclusions.")

    target = Path(output_opt or output or default_archive_name(compress))

    if dry_run:
        rows: list[Any] = [(ui.SECTION, "Would pack")]
        for name, selection in selections.items():
            rows.append(
                (
                    name,
                    ui.joined(
                        ui.plural(len(selection.files), "file"),
                        ui.human_bytes(selection.total_bytes),
                    ),
                )
            )
        rows += [
            (ui.SECTION, "Total"),
            ("files", str(total_files)),
            ("size", ui.human_bytes(total_bytes)),
            ("archive", ui.shorten_path(target)),
        ]
        ui.print_detail(rows)
        _report_skips(special, unreadable, secrets, databases)
        return

    ui.step(f"packing {ui.plural(total_files, 'file')} ({ui.human_bytes(total_bytes)})")
    started = time.time()
    with click.progressbar(length=total_files, label="  archiving") as bar:
        pack(selections, target, compress=compress, on_file=lambda _: bar.update(1))
    size = target.stat().st_size
    ratio = (total_bytes / size) if size else 0
    ui.ok(
        ui.joined(
            ui.shorten_path(target),
            ui.human_bytes(size),
            f"{ratio:.1f}x smaller" if ratio else "",
            ui.compact_duration(time.time() - started),
        )
    )
    _report_skips(special, unreadable, secrets, databases)


def _report_skips(special: int, unreadable: int, secrets: int, databases: int) -> None:
    if databases:
        ui.hint(f"{ui.plural(databases, 'database')} snapshotted consistently")
    notes = []
    if special:
        notes.append(f"{special} socket/fifo skipped")
    if unreadable:
        notes.append(f"{unreadable} unreadable skipped")
    if notes:
        ui.hint(ui.joined(*notes))
    if secrets:
        ui.warn(
            f"{ui.plural(secrets, 'file')} may hold credentials "
            "(--exclude-secrets to leave them out)"
        )


@cli.command("info")
@click.argument("archive", type=click.Path(exists=True, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def cmd_info(archive, as_json):
    """Show what an archive contains, without unpacking it."""
    manifest = read_manifest(archive)
    if as_json:
        click.echo(json.dumps(manifest, indent=2))
        return
    rows: list[Any] = [
        (ui.SECTION, "Archive"),
        ("file", ui.shorten_path(archive)),
        ("size", ui.human_bytes(archive.stat().st_size)),
        ("created", manifest.get("created_at", "—")),
        ("host", manifest.get("host", "—")),
        ("user", manifest.get("user", "—")),
    ]
    tools = manifest.get("tools") or {}
    if tools:
        rows.append((ui.SECTION, "Contents"))
        for name, entry in sorted(tools.items()):
            rows.append(
                (
                    name,
                    ui.joined(
                        ui.plural(entry.get("files", 0), "file"),
                        ui.human_bytes(entry.get("bytes", 0)),
                        f"{entry['databases']} db" if entry.get("databases") else "",
                    ),
                )
            )
    ui.print_detail(rows)


@cli.command("restore")
@click.argument("archive", type=click.Path(exists=True, path_type=Path))
@_tool_option
@click.option(
    "--into",
    type=click.Path(file_okay=False, path_type=Path),
    help="Extract here instead of into your home.",
)
@click.option("--merge", is_flag=True, help="Overwrite files that already exist.")
@click.option("--dry-run", is_flag=True, help="Report what would be written.")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def cmd_restore(archive, tools, into, merge, dry_run, yes):
    """Put an archive's sessions back."""
    manifest = read_manifest(archive)
    targets = ", ".join(sorted((manifest.get("tools") or {}) or {"?": None}))
    if not dry_run and not yes:
        where = ui.shorten_path(into) if into else "your home directory"
        mode = "overwriting existing files" if merge else "keeping existing files"
        click.confirm(
            f"Restore {targets} into {where}, {mode}?", abort=True, default=False
        )

    report = restore(archive, into=into, only=tools, merge=merge, dry_run=dry_run)

    verb = "would restore" if dry_run else "restored"
    ui.ok(
        ui.joined(
            f"{verb} {ui.plural(report.written, 'file')}",
            ui.human_bytes(report.bytes),
            ", ".join(sorted(report.tools)) or "nothing",
        )
    )
    if report.skipped_existing:
        ui.hint(
            f"{report.skipped_existing} already present (--merge to overwrite them)"
        )
    if report.refused:
        ui.warn(
            f"{ui.plural(len(report.refused), 'entry')} refused as unsafe: "
            + ", ".join(report.refused[:3])
            + ("…" if len(report.refused) > 3 else "")
        )


if __name__ == "__main__":
    cli()
