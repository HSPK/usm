"""Commands that forward a raw command line must not register short options.

click's short-option parser walks a glued token character by character, so any
registered short flag gets matched *inside* pass-through arguments. With ``-h``
active as a help alias, all of these print help and exit 0 without running
anything:

    usm rsync -avh ./src host:~/dst
    usm ssh -L8080:localhost:80 myhost
    usm secret run prod curl -sh https://example.com

Wrapper-owned short options (rsync's documented ``-n``/``-i``/``-p``/``-e``)
are a deliberate trade-off; an accidental ``-h`` is not.
"""

from __future__ import annotations

import click
import cp
import git_auth
import notify
import rsync
import secret
import ssh

# Every script whose command tree forwards arguments to another program.
ROOTS = [
    ("cp", cp.copy),
    ("git-auth", git_auth.cli),
    ("notify", notify.cli),
    ("rsync", rsync.cli),
    ("secret", secret.cli),
    ("ssh", ssh.cli),
]


def _walk(command, inherited_help_names, path):
    """Yield ``(path, settings, effective_help_names)`` for the whole tree.

    ``help_option_names`` is inherited from the parent context when a command
    does not set it, so it has to be threaded down rather than read per-command.
    """
    settings = command.context_settings or {}
    help_names = settings.get("help_option_names", inherited_help_names)
    yield path, settings, help_names
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            yield from _walk(sub, help_names, f"{path} {name}")


def _passthrough_commands():
    for name, root in ROOTS:
        for path, settings, help_names in _walk(root, ["--help"], name):
            if settings.get("ignore_unknown_options"):
                yield path, help_names


def test_there_is_something_to_check():
    assert len(list(_passthrough_commands())) >= 6


def test_passthrough_commands_register_no_short_help_alias():
    offenders = {
        path: help_names
        for path, help_names in _passthrough_commands()
        if any(
            name.startswith("-") and not name.startswith("--") for name in help_names
        )
    }

    assert not offenders, (
        f"these forward raw arguments, so a short help alias will be matched "
        f"inside them: {offenders}"
    )
