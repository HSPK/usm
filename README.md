# usm

[![Release](https://img.shields.io/github/v/release/HSPK/usm)](https://github.com/HSPK/usm/releases)
[![PyPI](https://img.shields.io/pypi/v/usmo)](https://pypi.org/project/usmo/)
[![Release workflow](https://github.com/HSPK/usm/actions/workflows/release.yml/badge.svg)](https://github.com/HSPK/usm/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

`usm` is a lightweight CLI for machine bootstrap tasks and day-to-day utility scripts.
It gives you one command for common setup jobs, Azure/blobfuse workflows, and a few
personal productivity helpers, while keeping the underlying scripts easy to iterate on.

The package installs as `usmo`, but the executable command is `usm`.

## Highlights

- One entrypoint for machine setup, storage helpers, and quick admin tasks.
- On-demand script download and caching under `~/.cache/usm/scripts`.
- Python subcommands run with the package interpreter, which keeps `pipx` installs
  isolated and reliable.
- Local `--debug` mode for iterating on scripts in this repository without
  downloading from GitHub.
- Simple release flow driven by Git tags.

## Installation

Install from PyPI with `pipx`:

```bash
pipx install usmo
```

PyPI package page: <https://pypi.org/project/usmo/>

Or install from a local checkout while developing:

```bash
uv sync
pipx install --force .
```

After installation, the CLI is available as:

```bash
usm <command> [args...]
```

## Commands

| Command | Description |
| --- | --- |
| `usm init` | Bootstrap a fresh Ubuntu machine with common packages, shell aliases, `pipx` tools, tmux plugins, and Neovim config. |
| `usm blobmount <mount_dir> <account> <container>` | Install `blobfuse2` if needed, generate a SAS token from your Azure CLI login, and mount a blob container locally. |
| `usm cu122` | Install NVIDIA driver 535, CUDA 12.2, and Vulkan-related packages on Ubuntu. |
| `usm cp [--use-sas-token] <source>... <destination>` | Copy between local paths and blobfuse2 mountpoints, delegating to `azcopy` when Azure storage is involved. |
| `usm check_py` | Print the active Python and pip locations and versions. |
| `usm inject-alias [--shell bash|zsh|powershell] [--file PATH]` | Insert or update the managed `usm` alias block in your shell rc file. Defaults to `~/.bashrc` on Unix-like systems and the PowerShell profile on Windows, with interactive shell selection when run in a TTY. You can combine `--shell` with `--file` to control the syntax written to a custom path. |

## Examples

Initialize a new machine:

```bash
usm init
```

Mount a blob container:

```bash
usm blobmount /mnt/data myaccount mycontainer
```

Copy from a blobfuse mount to a local directory:

```bash
usm cp /mnt/data/project ./project-backup
```

Refresh the cached script before running it:

```bash
usm --upgrade check_py
```

Run against the local `scripts/` directory instead of downloading from GitHub:

```bash
usm --debug check_py
```

Inject aliases into your zsh profile:

```bash
usm inject-alias --shell zsh
```

Inject aliases into your PowerShell profile:

```bash
usm inject-alias --shell powershell
```

Write the managed alias block into a specific file:

```bash
usm inject-alias --file ~/.config/usm/test-shell.rc
```

Write PowerShell-flavored aliases into a custom profile file:

```bash
usm inject-alias --shell powershell --file ~/Documents/PowerShell/Microsoft.PowerShell_profile.ps1
```

## How it Works

The CLI keeps a small manifest of commands in `src/usmo/cli.py`.

- Shell scripts are executed with `bash`.
- Python scripts are executed with the current interpreter via `sys.executable`.
- Remote scripts are downloaded from this repository and cached locally.
- `--upgrade` forces a fresh download of the selected script.
- `--debug` bypasses the cache and runs the local file under `scripts/`.
- Managed alias insertion uses start/end markers so rerunning the command updates the
  block instead of duplicating it.
- `inject-alias` is implemented with `click` and supports bash, zsh, and PowerShell
  profile targets.

## Development

Install the project locally:

```bash
uv sync
```

Build distributable artifacts:

```bash
uv build
```

Smoke-test the installed command:

```bash
uv run usm check_py
```

## Release Flow

This repository includes a GitHub Actions workflow at
`.github/workflows/release.yml`.

When you push a tag like `v0.1.4`, GitHub Actions will:

1. Build the source distribution and wheel with `uv build`.
2. Create or update a GitHub Release for that tag.
3. Upload the built artifacts to the release page.
4. Publish to PyPI if the repository secret `PYPI_API_TOKEN` is configured.

Create a new release with:

```bash
git tag -a v0.1.4 -m "v0.1.4"
git push origin v0.1.4
```

## Notes

- Some scripts are tailored for Ubuntu-based environments.
- `blobmount` and `cp` expect Azure CLI / `azcopy` / `blobfuse2` style workflows.
- Cached scripts live in `~/.cache/usm/scripts`.
