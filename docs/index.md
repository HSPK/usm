# usm

[![Release](https://img.shields.io/github/v/release/HSPK/usm)](https://github.com/HSPK/usm/releases)
[![PyPI](https://img.shields.io/pypi/v/usmo)](https://pypi.org/project/usmo/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

> Bootstrap machines and run cached utility scripts from one CLI.

`usm` is a lightweight CLI that wraps machine-setup tasks, Azure/blobfuse
workflows, and a handful of day-to-day productivity helpers behind a single
entry point. Scripts are fetched on demand, cached locally, and pinned by
content hash — so a fresh machine can be productive in one command.

The package installs as `usmo`, but the executable is `usm`.

## Highlights

- **One entry point** for machine setup, storage helpers, and quick admin tasks.
- **On-demand script download** with per-script versioning and SHA-256 pinning;
  cached under `~/.cache/usm/scripts`.
- **uv-managed isolation** for Python scripts — every script declares its own
  requirements, installed once into a persistent per-script venv, never
  polluting your Python env.
- **`--debug` mode** runs scripts from a local checkout, so iterating on a
  script feels like editing any other repo file.
- **Auto-update probe** notifies you when a cached script has a newer version
  upstream; you stay in control of when to actually upgrade.

## Quick start

```bash
# 1. install
curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash

# 2. see what's available
usm list

# 3. run something
usm sysinfo
usm tunnel local 8080:db:5432 user@bastion
usm inject-alias --shell zsh
```

See [Installation](installation.md) for alternatives, or jump to the command
you want from the [Commands](commands/index.md) section.

## At a glance

| Command | What it does |
| --- | --- |
| [`usm tunnel`](commands/tunnel.md) | SSH tunnels (local / remote / SOCKS) with state + systemd autostart |
| [`usm proxy`](commands/proxy.md) | Turn a box into an HTTP/SOCKS (+Shadowsocks) proxy, or a Clash client routing rule-matched traffic through one (mihomo) |
| [`usm clash`](commands/clash.md) | ClashX-style CLI manager for mihomo: subscriptions, TUN, mode, node selection, system proxy, LAN, live logs, latency tests |
| [`usm gpu`](commands/gpu.md) | GPU inventory, free-picker, watch, kill (nvidia-smi wrapper) |
| [`usm port`](commands/port.md) | Show what's on a port; kill the holder |
| [`usm notify`](commands/notify.md) | Wrap a command and ping ntfy.sh / Telegram / webhook when it exits |
| [`usm secret`](commands/secret.md) | Encrypted local env store; inject into shells or processes |
| [`usm rsync`](commands/rsync.md) | rsync with sensible defaults + auto-excludes |
| [`usm clip`](commands/clip.md) | Cross-platform clipboard; OSC52 fallback for SSH |
| [`usm wait`](commands/wait.md) | wait-for-it (host:port, TCP, HTTP) |
| [`usm bench`](commands/bench.md) | Quick machine benchmark (CPU / mem / disk / network / GPU) |
| [`usm share`](commands/share.md) | Serve a file/dir over HTTP, optionally tunneled out |
| [`usm serve`](commands/serve.md) | Full-featured file server (uploads, range, zip, auth) via miniserve |
| [`usm init`](commands/init.md) | Bootstrap a fresh Ubuntu machine |
| [`usm blobmount`](commands/blobmount.md) | Mount an Azure blob container locally |
| [`usm cp`](commands/cp.md) | Copy across local + blobfuse mountpoints, delegating to azcopy |
| [`usm cu122`](commands/cu122.md) | Install NVIDIA driver 535 + CUDA 12.2 on Ubuntu |
| [`usm inject-alias`](commands/inject-alias.md) | Manage a marker-fenced alias block in your shell rc |
| [`usm openai-proxy`](commands/openai-proxy.md) | Run a local OpenAI-compatible proxy to Microsoft TRAPI |
| [`usm sysinfo` / `check_py`](commands/sysinfo.md) | Print system / Python info |
| [Built-in helpers](commands/builtin.md) | `list`, `update`, `clean`, `version` |

## How it works in 30 seconds

Scripts are declared in
[`scripts/_config.json`](https://github.com/HSPK/usm/blob/main/scripts/_config.json)
and downloaded from `raw.githubusercontent.com/HSPK/usm/main/scripts/`. The
CLI looks the command up, fetches + caches the file (if missing), then runs
it: shell scripts under `bash`, Python scripts under the current interpreter,
and Python scripts with declared `requirements` in a persistent per-script
venv (`~/.cache/usm/envs/<name>`) that is built once and reused offline
thereafter.

See [Architecture](architecture.md) for the full picture.
