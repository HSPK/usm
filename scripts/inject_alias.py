#!/usr/bin/env python3

from __future__ import annotations

import platform
import sys
from pathlib import Path

import click

BEGIN_MARKER = "## __USM_INIT_ALIAS_BEGIN__"
END_MARKER = "## __USM_INIT_ALIAS_END__"
SUPPORTED_SHELLS = ("bash", "zsh", "powershell")
POSIX_ALIAS_BLOCK_BODY = """alias ll="ls -lh"
alias gs="git status"
alias ga="git add"
alias gm="git commit -m"
alias gb="git branch"
alias gp="git push && git push --tags"
alias gc="git checkout"
alias tn="tmux new -s"
alias p4="proxychains4"
alias ta="tmux attach -t"
alias tm="tmux -u"
alias ..="cd .."
alias ...="cd ../../"
alias ca="conda activate"
alias azl="az login"
alias gu="nvidia-smi"
alias v="nvim"
gmp () {
    git add .
    git commit -m "$1"
    git push
}
export PATH="$HOME/.local/bin:$PATH"
export PATH="$HOME/.cargo/bin:$PATH"
export AZCOPY_AUTO_LOGIN_TYPE=AZCLI"""
POWERSHELL_ALIAS_BLOCK_BODY = """function ll { Get-ChildItem @args }
function gs { git status @args }
function ga { git add @args }
function gm { git commit -m @args }
function gb { git branch @args }
function gp {
    git push @args
    if ($LASTEXITCODE -eq 0) {
        git push --tags
    }
}
function gc { git checkout @args }
function tn { tmux new -s @args }
function p4 { proxychains4 @args }
function ta { tmux attach -t @args }
function tm { tmux -u @args }
function .. { Set-Location .. }
function ... { Set-Location ../.. }
function ca { conda activate @args }
function azl { az login @args }
function gu { nvidia-smi @args }
function v { nvim @args }
function gmp {
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Message
    )

    git add .
    git commit -m $Message
    git push
}

$usmLocalBin = Join-Path $HOME ".local/bin"
if (-not (($env:Path -split ';') -contains $usmLocalBin)) {
    $env:Path = "$usmLocalBin;$env:Path"
}

$usmCargoBin = Join-Path $HOME ".cargo/bin"
if (-not (($env:Path -split ';') -contains $usmCargoBin)) {
    $env:Path = "$usmCargoBin;$env:Path"
}

$env:AZCOPY_AUTO_LOGIN_TYPE = "AZCLI"
"""


def current_system_name() -> str:
    return platform.system()


def default_shell_for_system(system_name: str) -> str:
    return "powershell" if system_name == "Windows" else "bash"


def prompt_choices_for_system(system_name: str) -> list[tuple[str, str, str]]:
    if system_name == "Windows":
        return [
            ("1", "powershell", "PowerShell profile"),
            ("2", "bash", "~/.bashrc"),
        ]

    return [
        ("1", "bash", "~/.bashrc"),
        ("2", "zsh", "~/.zshrc"),
    ]


def powershell_profile_path(home: Path) -> Path:
    candidates = [
        home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def target_path_for_shell(shell: str, home: Path) -> Path:
    if shell == "bash":
        return home / ".bashrc"
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "powershell":
        return powershell_profile_path(home)
    raise ValueError(f"Unsupported shell '{shell}'.")


def shell_label(shell: str, home: Path) -> str:
    target_path = target_path_for_shell(shell, home)
    if shell == "powershell":
        return f"PowerShell profile ({target_path})"
    return shell


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def prompt_for_shell(system_name: str) -> str:
    choices = prompt_choices_for_system(system_name)
    default_choice, default_shell, _ = choices[0]

    click.echo("Select the shell config file to update:")
    for choice_key, shell_name, label in choices:
        default_suffix = " [default]" if choice_key == default_choice else ""
        click.echo(f"  {choice_key}) {shell_name:<10} ({label}){default_suffix}")

    user_choice = click.prompt("Choice", default=default_choice, show_default=False)
    normalized_choice = str(user_choice).strip().lower()

    for choice_key, shell_name, _ in choices:
        if normalized_choice in {choice_key, shell_name}:
            return shell_name

    click.echo(f"Unrecognized choice, defaulting to {default_shell}.")
    return default_shell


def render_alias_block(shell: str) -> str:
    block_body = (
        POWERSHELL_ALIAS_BLOCK_BODY if shell == "powershell" else POSIX_ALIAS_BLOCK_BODY
    )
    return f"{BEGIN_MARKER}\n{block_body}\n{END_MARKER}\n"


def strip_existing_managed_block(content: str) -> tuple[str, bool]:
    lines = content.splitlines()
    kept_lines: list[str] = []
    inside_managed_block = False
    found_begin = False
    found_end = False

    for line in lines:
        if line == BEGIN_MARKER:
            if inside_managed_block:
                raise ValueError("Found a nested usm alias block marker.")
            inside_managed_block = True
            found_begin = True
            continue

        if line == END_MARKER:
            if not inside_managed_block:
                raise ValueError("Found an end marker without a matching begin marker.")
            inside_managed_block = False
            found_end = True
            continue

        if not inside_managed_block:
            kept_lines.append(line)

    if inside_managed_block or found_begin != found_end:
        raise ValueError(
            "Found an incomplete managed alias block. Please fix it manually first."
        )

    return "\n".join(kept_lines).strip("\n"), found_begin and found_end


def upsert_alias_block(target_path: Path, shell: str) -> str:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    original_content = (
        target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    )
    cleaned_content, replaced_existing = strip_existing_managed_block(original_content)
    alias_block = render_alias_block(shell)

    if cleaned_content:
        updated_content = f"{cleaned_content}\n\n{alias_block}"
    else:
        updated_content = alias_block

    target_path.write_text(updated_content, encoding="utf-8")
    return "updated" if replaced_existing else "inserted"


def resolve_target(
    shell: str | None,
    file: Path | None,
    system_name: str | None = None,
    interactive: bool | None = None,
) -> tuple[Path, str, str]:
    home = Path.home()
    resolved_system_name = system_name or current_system_name()
    is_interactive_session = is_interactive() if interactive is None else interactive
    resolved_shell = shell

    if resolved_shell is None:
        if (
            file is None
            and is_interactive_session
            and resolved_system_name in {"Linux", "Darwin", "Windows"}
        ):
            resolved_shell = prompt_for_shell(resolved_system_name)
        else:
            resolved_shell = default_shell_for_system(resolved_system_name)
            default_message = (
                f"No shell selected; using {resolved_shell} syntax for {file.expanduser()}."
                if file is not None
                else f"No shell selected; defaulting to {target_path_for_shell(resolved_shell, home)}."
            )
            click.echo(default_message)

    if file is not None:
        return file.expanduser(), resolved_shell, "custom file"

    return (
        target_path_for_shell(resolved_shell, home),
        resolved_shell,
        shell_label(resolved_shell, home),
    )


@click.command()
@click.option(
    "--shell",
    type=click.Choice(SUPPORTED_SHELLS, case_sensitive=False),
    help="Shell config to update.",
)
@click.option(
    "--file",
    type=click.Path(path_type=Path, dir_okay=False, writable=True, resolve_path=False),
    help="Write to an explicit file instead of a shell profile.",
)
def cli(shell: str | None, file: Path | None) -> None:
    """Insert or update the managed usm alias block in a shell rc file."""

    normalized_shell = shell.lower() if shell is not None else None

    try:
        target_path, resolved_shell, label = resolve_target(normalized_shell, file)
        action = upsert_alias_block(target_path, resolved_shell)
    except ValueError as error:
        raise click.ClickException(str(error)) from error

    click.echo(f"Managed usm aliases {action} in {target_path} ({label}).")
    if resolved_shell == "powershell":
        click.echo(f"Run: . {target_path}")
    else:
        click.echo(f"Run: source {target_path}")


if __name__ == "__main__":
    cli()
