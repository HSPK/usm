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
- Python subcommands run with the package interpreter, which keeps `uv tool`
  installs isolated and reliable.
- Local `--debug` mode for iterating on scripts in this repository without
  downloading from GitHub.
- Simple release flow driven by Git tags.

## Installation

### Quick Install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash
```

The script will automatically install `uv` (via the official installer at
<https://astral.sh/uv>) if needed, then install `usmo` via `uv tool install`.
You may need to run `source ~/.bashrc` (or restart your shell) afterwards
for the `usm` command to become available.

### Manual Install

Install [uv](https://docs.astral.sh/uv/#installation) first, then install
`usmo` as a uv-managed tool:

```bash
uv tool install usmo
# upgrade later with:
uv tool install --upgrade usmo
```

PyPI package page: <https://pypi.org/project/usmo/>

Or install from a local checkout while developing:

```bash
uv sync
uv tool install --force .
```

After installation, the CLI is available as:

```bash
usm <command> [args...]
```

## Commands

`usm` bundles machine-setup scripts, networking/proxy tools, file servers, and a
few built-in helpers. Scripts live in [`scripts/_config.json`](scripts/_config.json)
and download on first use — add new ones by editing that file, no Python changes
required.

See everything (with one-line descriptions and cache status) from the CLI:

```bash
usm list                 # all commands
usm <command> --help     # help for one command
```

Full reference: <https://hspk.github.io/usm/commands/>.

### Aliases

Install any script as a short command on your `PATH`:

```bash
usm install clash cx     # `cx ...` now runs `usm clash ...`
usm uninstall cx
```

Shims are written to `~/.local/bin`; usm warns if that directory isn't on your
`PATH`.

### Updating

```bash
usm update               # refresh the catalog (_config.json) only
usm update --all         # re-download every cached script
usm update tunnel        # refresh a single script
```

## Examples

```bash
usm init                                        # bootstrap a machine
usm blobmount /mnt/data myaccount mycontainer   # mount an Azure blob container
usm cp /mnt/data/project ./backup               # azcopy-backed when Azure is involved
usm --upgrade check_py                          # force-refresh a script before running
usm --debug check_py                            # run from local ./scripts (no download)
```

See `usm <command> --help` or the [docs](https://hspk.github.io/usm/commands/) for
per-command usage.

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

When you push a tag like `v0.1.9`, GitHub Actions will:

1. Build the source distribution and wheel with `uv build` (version is derived
   from the git tag automatically via `hatch-vcs`).
2. Create or update a GitHub Release for that tag.
3. Upload the built artifacts to the release page.
4. Publish to PyPI via Trusted Publishing (no secrets needed).

Create a new release with:

```bash
git tag -a v0.1.9 -m "v0.1.9"
git push origin v0.1.9
```

## Notes

- Some scripts are tailored for Ubuntu-based environments.
- `blobmount` and `cp` expect Azure CLI / `azcopy` / `blobfuse2` style workflows.
- Cached scripts live in `~/.cache/usm/scripts`.
