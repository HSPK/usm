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

### Scripts

Scripts are defined in [`scripts/_config.json`](scripts/_config.json) and
downloaded on first use. Add new commands by editing that file — no Python
changes required.

| Command | Description |
| --- | --- |
| `usm init` | Bootstrap a fresh Ubuntu machine with common packages, shell aliases, `uv` tools, tmux plugins, and Neovim config. |
| `usm blobmount <mount_dir> <account> <container>` | Install `blobfuse2` if needed, generate a SAS token from your Azure CLI login, and mount a blob container locally. |
| `usm cu122` | Install NVIDIA driver 535, CUDA 12.2, and Vulkan-related packages on Ubuntu. |
| `usm cp [--use-sas-token] <source>... <destination>` | Copy between local paths and blobfuse2 mountpoints, delegating to `azcopy` when Azure storage is involved. |
| `usm check_py` | Print the active Python and pip locations and versions. |
| `usm sysinfo` | Print system, GPU, CUDA, MPI, and distributed-ML environment summary. |
| `usm inject-alias [--shell bash\|zsh\|powershell] [--file PATH]` | Insert or update the managed `usm` alias block in your shell rc file. |
| `usm tunnel <local\|remote\|socks\|ls\|stop\|start\|restart\|rm\|enable\|disable\|show\|logs>` | Start and manage SSH tunnels with persistent state; `enable`/`disable` install/remove a systemd `--user` unit for boot-time autostart. |
| `usm gpu [free\|watch\|kill]` | GPU inventory, free-picker (`CUDA_VISIBLE_DEVICES=$(usm gpu free 2)`), live watch, kill CUDA processes. |
| `usm port [PORT\|ls\|kill PORT]` | Show what's listening on a port; free a port by killing the holder. |
| `usm notify [config\|test] [-- CMD ARGS]` | Wrap a command and ping ntfy.sh / Telegram / generic webhook when it exits. |
| `usm secret <set\|get\|ls\|rm\|export\|run>` | Encrypted local env store (Fernet). Inject secrets into shells or processes. |
| `usm rsync [OPTIONS] SRC... DST` | rsync wrapper with sensible defaults + auto-excludes (`.git/`, `__pycache__/`, `.venv/`, ...). |
| `usm clip [paste]` | Cross-platform clipboard from stdin; OSC52 fallback for SSH sessions. |
| `usm wait TARGET...` | Block until host:port / TCP / HTTP endpoints are reachable (AND semantics). |
| `usm bench [--quick\|--full]` | Quick machine benchmark — CPU / memory / disk / network / optional GPU. |
| `usm share PATH [--tunnel SSH_TARGET[:PORT]]` | Serve a file/dir over HTTP; optionally exposed to a remote via `ssh -R`. |

### Built-in helpers

| Command | Description |
| --- | --- |
| `usm list` | List all available commands and their cache status. |
| `usm update` | Re-download the config and all cached scripts. |
| `usm clean` | Remove the script cache directory (`~/.cache/usm/scripts`). |
| `usm version` | Show the installed `usm` version. |

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

Open an SSH tunnel (local forward) and inspect it:

```bash
usm tunnel local 8080:db.internal:5432 user@bastion   # localhost:8080 -> db.internal:5432
usm tunnel remote 9000:3000 user@server               # server:9000 -> localhost:3000
usm tunnel socks 1080 user@gateway                    # SOCKS5 on localhost:1080
usm tunnel ls
usm tunnel stop local-8080-bastion                    # keeps the definition
usm tunnel start local-8080-bastion                   # relaunch a stopped tunnel
usm tunnel rm local-8080-bastion                      # delete the definition
```

Autostart a tunnel at login/boot via a systemd `--user` unit (Linux only).
The unit also restarts the tunnel automatically on failure:

```bash
usm tunnel enable local-8080-bastion        # installs ~/.config/systemd/user/usm-tunnel-<id>.service
usm tunnel disable local-8080-bastion       # removes the unit (keeps the definition)
# To have user units start at boot before you log in (one-time per user):
sudo loginctl enable-linger "$USER"
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
