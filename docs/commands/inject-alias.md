# `usm inject-alias`

Insert or update a managed alias block in your shell rc file. Re-running
the command updates the block in place instead of duplicating it (the
trick is start/end markers).

```bash
usm inject-alias [--shell bash|zsh|powershell] [--file PATH]
```

## What gets added

A curated set of one-letter aliases the author uses everywhere — `ll`,
`gs`/`ga`/`gm`/`gb`/`gp` for git, `tn`/`ta`/`tm` for tmux, `..`/`...`,
`ca` for `conda activate`, etc. See
[`scripts/inject_alias.py`](https://github.com/HSPK/usm/blob/main/scripts/inject_alias.py)
for the exact list — the alias body is part of the script source.

Plus a few PATH exports (`~/.local/bin`, `~/.cargo/bin`) and
`AZCOPY_AUTO_LOGIN_TYPE=AZCLI`.

## Default target

- **Unix**: `~/.bashrc`
- **Windows**: PowerShell `$PROFILE`
- **Interactive TTY**: prompts you for `bash` / `zsh` / `powershell` if
  `--shell` wasn't given.

## How re-runs stay idempotent

The block is wrapped in two marker lines:

```text
## __USM_INIT_ALIAS_BEGIN__
... aliases here ...
## __USM_INIT_ALIAS_END__
```

`inject-alias` looks for those markers and replaces everything between
them, or appends a fresh block if it can't find them. Anything you write
outside the markers is left alone.

## Examples

```bash
# Default: bashrc on Unix
usm inject-alias

# Force zsh syntax (writes to ~/.zshrc)
usm inject-alias --shell zsh

# PowerShell on Windows
usm inject-alias --shell powershell

# Custom file (e.g. a test target or a non-standard location)
usm inject-alias --file ~/.config/myshell/aliases.sh

# Mix --shell with --file when the file extension doesn't tell the truth
usm inject-alias --shell powershell --file ~/Documents/PowerShell/Profile.ps1
```

## Removing

Delete everything between the two marker lines from your rc file. They're
designed to be greppable:

```bash
grep -n __USM_INIT_ALIAS_ ~/.bashrc
```

## Source

[`scripts/inject_alias.py`](https://github.com/HSPK/usm/blob/main/scripts/inject_alias.py).
