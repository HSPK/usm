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

- **Versioned packages**: every command in the registry has a SemVer
  version, sha256 integrity hash, and is installed into a per-version
  directory under `~/.cache/usm/packages/<name>/<version>/`.
- **Multiple registries**: configure additional GitHub-hosted (or any
  HTTP) registries via `usm registry add`; usm searches them in order.
- **Two package formats**: a single executable script, or a tar.gz
  archive containing an `usm.toml` manifest with an `entry =` field
  plus any number of supporting files.
- **Persistent install state**: `usm installed`, `usm upgrade`, and
  `usm uninstall` track exactly which version of each package is
  present, with the source registry recorded.
- **Backward-compatible dispatch**: `usm <name> [args...]` still
  works — if the package is not yet installed it is auto-installed at
  the latest version before being executed.
- **Local debug mode**: `usm --debug ...` reads the index and files
  from `./scripts/` instead of the network, for iterating on a registry
  repo.
- **Reproducible publishing**: `tools/build_index.py` regenerates
  `_config.json` (sha256 + size) from a `scripts/` directory plus a
  small `versions.toml`. `usm publish FILE --name --version` prints a
  ready-to-commit JSON snippet for any registry repo.

## Installation

### Quick Install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash
```

The script will automatically install `pipx` if needed, then install `usmo` via
`pipx`. You may need to run `source ~/.bashrc` (or restart your shell) afterwards
for the `usm` command to become available.

### Manual Install

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

### Scripts

Scripts are defined in [`scripts/_config.json`](scripts/_config.json) and
downloaded on first use. Add new commands by editing that file — no Python
changes required.

| Command | Description |
| --- | --- |
| `usm init` | Bootstrap a fresh Ubuntu machine with common packages, shell aliases, `pipx` tools, tmux plugins, and Neovim config. |
| `usm blobmount <mount_dir> <account> <container>` | Install `blobfuse2` if needed, generate a SAS token from your Azure CLI login, and mount a blob container locally. |
| `usm cu122` | Install NVIDIA driver 535, CUDA 12.2, and Vulkan-related packages on Ubuntu. |
| `usm cp [--use-sas-token] <source>... <destination>` | Copy between local paths and blobfuse2 mountpoints, delegating to `azcopy` when Azure storage is involved. |
| `usm check_py` | Print the active Python and pip locations and versions. |
| `usm sysinfo` | Print system, GPU, CUDA, MPI, and distributed-ML environment summary. |
| `usm inject-alias [--shell bash\|zsh\|powershell] [--file PATH]` | Insert or update the managed `usm` alias block in your shell rc file. Defaults to `~/.bashrc` on Unix-like systems and the PowerShell profile on Windows, with interactive shell selection when run in a TTY. You can combine `--shell` with `--file` to control the syntax written to a custom path. |

### Built-in helpers

| Command | Description |
| --- | --- |
| `usm list` | List packages from every configured registry, with install status. |
| `usm available <name>` | Show all available versions of a package. |
| `usm info <name>` | Show package metadata (description, registry, sha256, install dir). |
| `usm search <query>` | Search package names and descriptions across registries. |
| `usm installed` | Show locally installed packages and their versions. |
| `usm install <name>[==<version>]` | Install (or pin) a package; verifies sha256. |
| `usm uninstall <name>` | Remove a previously installed package. |
| `usm upgrade [<name>]` | Upgrade one package, or all installed packages, to latest. |
| `usm run <name> [args...]` | Explicitly run an installed package's entry script. |
| `usm registry list/add/remove/default` | Manage the registry list in `~/.config/usm/config.toml`. |
| `usm publish FILE --name --version` | Print a JSON snippet to add to a registry index. |
| `usm update` | Refresh every registry index and re-install installed packages. |
| `usm clean` | Remove `~/.cache/usm/` (indices and installed packages). |
| `usm version` | Show the installed `usm` version. |

For backward compatibility, `usm <package> [args...]` still dispatches
straight to the package — the package is installed on first use.

## The Package Registry

A registry is a directory (typically a folder in a GitHub repo, served
via `raw.githubusercontent.com`) containing:

- `_config.json` — the registry index, schema v2.
- One file per package version: a single shell/Python script, or a
  tar.gz archive containing an `usm.toml` manifest.

### Index schema (v2)

```jsonc
{
  "schema_version": 2,
  "registry": {"name": "default"},
  "packages": {
    "init": {
      "description": "Initialize a new machine setup.",
      "latest": "0.1.0",
      "versions": {
        "0.1.0": {
          "type": "script",          // or "archive"
          "path": "init.sh",         // file inside the registry directory
          "sha256": "<hex>",
          "size": 6240,
          "requires_python": null,
          "depends": []
        }
      }
    }
  }
}
```

For backward compatibility the CLI still understands the old v1 format
(`{"scripts": {"name": {"description": "...", "path": "..."}}}`) but
without sha256 verification.

### Archive packages

For multi-file packages, ship a tarball whose root contains:

```
<name>-<version>/
  usm.toml      # entry = "main.sh"
  main.sh
  ... any other files ...
```

`usm install` extracts safely (rejecting path-traversal entries) into
`~/.cache/usm/packages/<name>/<version>/`, marks the entry script
executable, and records the install in `~/.cache/usm/state.json`.

### Multiple registries

```
usm registry add company https://raw.githubusercontent.com/acme/usm-registry/main/
usm registry default company
usm registry list
```

`usm install foo` searches registries in declared order (with the
preferred registry first), and the first one containing `foo` wins.

### Publishing

1. Add or update your script under `scripts/` (or your registry repo).
2. Edit `scripts/versions.toml` to bump the version / description.
3. Run `python tools/build_index.py` — this regenerates
   `scripts/_config.json` with fresh sha256/size for every script.
4. Commit and push. Consumers will pick the new version up via
   `usm update` or on next install.

For one-off or external packages, `usm publish <file> --name N --version V`
prints a JSON snippet you can paste into the index of any registry.

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

The CLI is a `click` group; built-in subcommands take precedence and
any other token is treated as a package name.

- The configured registries (default: this repo's `scripts/` folder
  on `main`) are queried for `_config.json` (schema v2). Indices are
  cached at `~/.cache/usm/index/<registry-id>/`.
- `usm install` downloads the file referenced by the requested version,
  verifies its sha256, and (for archives) extracts it under
  `~/.cache/usm/packages/<name>/<version>/`. The install is recorded in
  `~/.cache/usm/state.json`.
- `usm <name> [args...]` looks up the installed entry script and runs
  it with `bash` (`*.sh`) or `sys.executable` (`*.py`). If the package
  is not yet installed, latest is auto-installed first.
- `--upgrade` forces a refetch of the index and a reinstall.
- `--debug` reads the index and files from `./scripts/` instead of
  the network — useful when iterating on a registry repo.
- Tar archives are extracted with `filter='data'` and a manual path
  traversal check, rejecting members that try to escape the
  destination directory.

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

Run the unit tests:

```bash
uv run pytest
```

Regenerate `scripts/_config.json` after editing any script or
`scripts/versions.toml`:

```bash
python tools/build_index.py
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
