# Installation

## Quick install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash
```

The installer will:

1. Install [`uv`](https://docs.astral.sh/uv/) (Astral's Python installer) if it
   isn't on your PATH.
2. Run `uv tool install usmo` so `usm` lands in `~/.local/bin/`.

If `~/.local/bin/` isn't on your PATH yet, either restart your shell or
`source ~/.bashrc` (the installer prints the right hint).

## Manual install

Install [uv](https://docs.astral.sh/uv/#installation) first, then:

```bash
uv tool install usmo

# upgrade later
uv tool install --upgrade usmo
```

PyPI page: <https://pypi.org/project/usmo/>

## From a local checkout

For iterating on the CLI itself (not just the scripts):

```bash
git clone https://github.com/HSPK/usm.git
cd usm
uv sync
uv tool install --force .
```

After this, `usm` runs your local edits. To go back to the released version:
`uv tool install --force usmo`.

## Verifying the install

```bash
usm version
usm list
```

`usm list` shows every command and which ones are already cached on disk.
Nothing is downloaded until you actually use it.

## Where things live

| Path | Purpose |
| --- | --- |
| `~/.local/bin/usm` | The CLI entry point (from `uv tool`). |
| `~/.cache/usm/scripts/` | Cached script files + the upstream `_config.json`. |
| `~/.cache/usm/.last_check` | Timestamp for the auto-update probe. |
| `~/.cache/usm/tunnels/` | State files for `usm tunnel` (per-tunnel JSON + logs). |
| `~/.config/systemd/user/usm-tunnel-*.service` | Units installed by `usm tunnel enable`. |

To remove everything: `usm clean` (just the script cache), or
`rm -rf ~/.cache/usm` (everything including tunnel state) and
`uv tool uninstall usmo`.

## Updating cached scripts

Each script carries its own version. `usm` probes the upstream manifest at
most once every 24h (controlled by `USM_AUTO_CHECK_INTERVAL` in seconds, `0`
disables) and prints a banner when something is newer. To actually upgrade:

```bash
usm update           # re-fetch the manifest + every cached script
usm -U <command>     # force a fresh download for one specific command
```

## Required external tools

`usm` itself only needs Python ≥ 3.10 and `uv`. Individual commands have
their own runtime requirements; see each command's page for details. The
most common are:

- `ssh` (for [`tunnel`](commands/tunnel.md))
- `bash` (every shell script runs under bash)
- `azcopy`, `blobfuse2`, `az` CLI (for [`blobmount`](commands/blobmount.md) /
  [`cp`](commands/cp.md))
- `systemctl --user` (only for `usm tunnel enable`)
