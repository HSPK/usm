#!/usr/bin/env python3
"""Cross-platform clipboard from stdin, with OSC52 fallback for SSH.

Examples:
  echo hi | usm clip
  cat file.txt | usm clip
  usm clip paste
  ssh remote 'usm clip --osc52 < file.log'   # lands in your local clipboard
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys

import click


def _local_copy(data: bytes) -> str | None:
    """Try native clipboard tools in order; return tool name on success."""
    candidates: list[tuple[str, list[str]]] = []
    if sys.platform == "darwin":
        candidates.append(("pbcopy", ["pbcopy"]))
    elif sys.platform == "win32":
        candidates.append(("clip", ["clip"]))
    else:
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(("wl-copy", ["wl-copy"]))
        if os.environ.get("DISPLAY"):
            candidates.append(("xclip", ["xclip", "-selection", "clipboard"]))
            candidates.append(("xsel", ["xsel", "--clipboard", "--input"]))
        if os.environ.get("WSL_DISTRO_NAME") or shutil.which("clip.exe"):
            candidates.append(("clip.exe", ["clip.exe"]))
    for name, argv in candidates:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(argv, input=data, check=True)
            return name
        except (subprocess.CalledProcessError, OSError):
            continue
    return None


def _local_paste() -> bytes | None:
    candidates: list[list[str]] = []
    if sys.platform == "darwin":
        candidates.append(["pbpaste"])
    elif sys.platform == "win32":
        candidates.append(["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"])
    else:
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(["wl-paste", "--no-newline"])
        if os.environ.get("DISPLAY"):
            candidates.append(["xclip", "-selection", "clipboard", "-o"])
            candidates.append(["xsel", "--clipboard", "--output"])
    for argv in candidates:
        if not shutil.which(argv[0]):
            continue
        try:
            return subprocess.check_output(argv)
        except (subprocess.CalledProcessError, OSError):
            continue
    return None


def _osc52(data: bytes) -> bool:
    """Emit an OSC 52 escape so the *terminal emulator* puts it in the local clipboard."""
    b64 = base64.b64encode(data).decode("ascii")
    seq = f"\033]52;c;{b64}\a"
    target = "/dev/tty"
    try:
        with open(target, "w") as tty:
            tty.write(seq)
            tty.flush()
        return True
    except OSError:
        try:
            sys.stderr.write(seq)
            sys.stderr.flush()
            return True
        except OSError:
            return False


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Copy stdin to the clipboard (auto-detects pbcopy / wl-copy / xclip / clip.exe / OSC52).",
)
@click.option("--osc52", is_flag=True, help="Force OSC52 escape (works through SSH).")
@click.option(
    "--trim/--no-trim",
    default=True,
    show_default=True,
    help="Strip a single trailing newline (use --no-trim to keep it).",
)
@click.pass_context
def cli(ctx, osc52, trim):
    if ctx.invoked_subcommand is not None:
        return
    data = sys.stdin.buffer.read()
    if trim and data.endswith(b"\n"):
        data = data[:-1]
    if osc52:
        ok = _osc52(data)
        if not ok:
            raise click.ClickException(
                "Failed to write OSC52 escape to /dev/tty or stderr."
            )
        click.echo(f"copied {len(data)} bytes via OSC52", err=True)
        return
    tool = _local_copy(data)
    if tool:
        click.echo(f"copied {len(data)} bytes via {tool}", err=True)
        return
    if _osc52(data):
        click.echo(
            f"copied {len(data)} bytes via OSC52 (no native tool found)", err=True
        )
        return
    raise click.ClickException(
        "No clipboard tool found and OSC52 unavailable. "
        "Install pbcopy/wl-copy/xclip/xsel, or run inside an OSC52-capable terminal."
    )


@cli.command(
    "paste",
    help="Print the clipboard contents to stdout (local only; OSC52 is write-only).",
)
def cmd_paste():
    data = _local_paste()
    if data is None:
        raise click.ClickException("No clipboard paste tool available.")
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    cli()
