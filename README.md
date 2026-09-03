# usm

[![Tests](https://github.com/HSPK/usm/actions/workflows/tests.yml/badge.svg)](https://github.com/HSPK/usm/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/usmo)](https://pypi.org/project/usmo/)
[![Downloads](https://img.shields.io/pypi/dm/usmo)](https://pypi.org/project/usmo/)
[![Python](https://img.shields.io/pypi/pyversions/usmo)](https://pypi.org/project/usmo/)
[![License](https://img.shields.io/pypi/l/usmo)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-hspk.github.io%2Fusm-0a7)](https://hspk.github.io/usm/)

**One CLI for the boxes you SSH into.**

34 tools for what you actually do on a remote machine — open a tunnel, find a
free GPU, mount a blob container, serve a directory, kill whatever is holding
port 8080 — behind a single command that installs in one line.

```bash
curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash
```

Then, on any machine:

```console
$ usm search proxy
2 match(es) for 'proxy'

     name           version   description
 ──────────────────────────────────────────────────────────────────────────
 ●   openai-proxy   v1.4.1    Local OpenAI + Anthropic compatible proxy fo…
 ○   proxy          v1.0.1    HTTP/SOCKS/Shadowsocks proxy server or simpl…

● cached  ○ not yet downloaded · 1/2 cached · usm <name> --help for details
```

## Why

Every one of these started as a script pasted between machines until it rotted.
`usm` is the version that stopped rotting: one install, one help system, and one
set of conventions everywhere you work.

- **Nothing to set up per machine.** Scripts download on first use and are
  cached under `~/.cache/usm/scripts`, pinned by SHA-256.
- **No dependency roulette.** Each script declares its own requirements and gets
  its own venv, so `usm gpu` never breaks because `usm openai-proxy` wanted a
  different httpx — and nothing lands in your system Python.
- **Long-running things stay running.** Tunnels, proxies, syncs, and mounts
  install as systemd/launchd units with `enable`, `status`, and `logs`.
- **Consistent by design.** Same verbs (`ls`, `status`, `logs`, `enable`), same
  output vocabulary, same `--json` where it matters. See the
  [design system](https://hspk.github.io/usm/design-system/).

The package installs as `usmo`; the command is `usm`.

## Commands

```bash
usm list                 # everything
usm list --cached        # just what's downloaded
usm search blob          # match names and descriptions
usm <command> --help     # help for one command
```

**Network & remote**

| Command | What it does |
| --- | --- |
| [`host`](https://hspk.github.io/usm/commands/host/) | SSH inventory in a fenced `~/.ssh/config` block, plus fan-out exec |
| [`ssh`](https://hspk.github.io/usm/commands/ssh/) | ssh with auto-reconnect and terminal repair |
| [`tunnel`](https://hspk.github.io/usm/commands/tunnel/) | SSH tunnels (local/remote/SOCKS) with state + autostart |
| [`proxy`](https://hspk.github.io/usm/commands/proxy/) | Turn a box into an HTTP/SOCKS/Shadowsocks proxy, or a Clash client |
| [`clash`](https://hspk.github.io/usm/commands/clash/) | ClashX-style manager for mihomo: subs, TUN, nodes, latency |
| [`net`](https://hspk.github.io/usm/commands/net/) | Inspect, diagnose, and monitor host networking |
| [`port`](https://hspk.github.io/usm/commands/port/) | Show what holds a port; free it |
| [`wait`](https://hspk.github.io/usm/commands/wait/) | Block until host:port / TCP / HTTP answers |
| [`session`](https://hspk.github.io/usm/commands/session/) | Inspect and manage logged-in user sessions |

**Files & storage**

| Command | What it does |
| --- | --- |
| [`share`](https://hspk.github.io/usm/commands/share/) | Serve a file/dir over HTTP, optionally tunneled out |
| [`serve`](https://hspk.github.io/usm/commands/serve/) | Full file server via miniserve (uploads, range, zip, auth) |
| [`rsync`](https://hspk.github.io/usm/commands/rsync/) | rsync with sensible defaults + auto-excludes |
| [`cp`](https://hspk.github.io/usm/commands/cp/) | Copy across local and blobfuse paths, delegating to azcopy |
| [`azrole`](https://hspk.github.io/usm/commands/azrole/) | Assign AML workspace and storage roles to an Entra user |
| [`azsync`](https://hspk.github.io/usm/commands/azsync/) | Watch a directory and keep it mirrored to Azure Blob |
| [`blobmount`](https://hspk.github.io/usm/commands/blobmount/) | Mount blob containers, keeping the SAS fresh |
| [`dl`](https://hspk.github.io/usm/commands/dl/) | Resumable, checksum-verified downloads with mirrors |
| [`space`](https://hspk.github.io/usm/commands/space/) | Find what's eating the disk and reclaim it safely |
| [`clip`](https://hspk.github.io/usm/commands/clip/) | Clipboard from stdin; OSC52 fallback over SSH |

**Machine & hardware**

| Command | What it does |
| --- | --- |
| [`doctor`](https://hspk.github.io/usm/commands/doctor/) | One-pass health check: disk, memory, GPU, mounts, services |
| [`init`](https://hspk.github.io/usm/commands/init/) | Bootstrap a machine with modern dev tools (cross-platform) |
| [`gpu`](https://hspk.github.io/usm/commands/gpu/) | GPU inventory, free-picker, watch, and kill |
| [`disk`](https://hspk.github.io/usm/commands/disk/) | Inspect, partition, format, and mount disks |
| [`bench`](https://hspk.github.io/usm/commands/bench/) | Benchmark CPU / memory / disk / network / GPU |
| [`cu122`](https://hspk.github.io/usm/commands/cu122/) | Install NVIDIA driver + CUDA 12.2 |
| [`sysinfo`](https://hspk.github.io/usm/commands/sysinfo/) | System / GPU / CUDA environment summary |
| [`check_py`](https://hspk.github.io/usm/commands/sysinfo/) | Show the active Python/pip versions |

**Dev workflow**

| Command | What it does |
| --- | --- |
| [`svc`](https://hspk.github.io/usm/commands/svc/) | Run any command as a supervised service, with boot integration |
| [`watch`](https://hspk.github.io/usm/commands/watch/) | Re-run a command whenever files change, debounced |
| [`secret`](https://hspk.github.io/usm/commands/secret/) | Encrypted local env store; inject secrets into processes |
| [`git-auth`](https://hspk.github.io/usm/commands/git-auth/) | Select Git identities and SSH keys by directory |
| [`notify`](https://hspk.github.io/usm/commands/notify/) | Ping ntfy.sh / Telegram / webhook when a command exits |
| [`openai-proxy`](https://hspk.github.io/usm/commands/openai-proxy/) | Local OpenAI + Anthropic compatible proxy for Microsoft TRAPI |
| [`inject-alias`](https://hspk.github.io/usm/commands/inject-alias/) | Manage a marker-fenced alias block in your shell rc |

Full reference: <https://hspk.github.io/usm/commands/>.

## Examples

```bash
usm init                                          # bootstrap a fresh machine
usm tunnel local 8080:db:5432 user@bastion        # forward a port, keep it up
usm port kill 8080                                # evict whatever squats there
usm blobmount mount /mnt/data acct container      # mount blob, refresh the SAS
usm notify -- python train.py                     # ping your phone when it exits
usm share ./build --tunnel user@bastion           # hand someone a file, from anywhere
usm doctor                                        # is this machine healthy?
usm svc add api -- ./serve.sh                     # keep it running, and at boot
usm watch src -- pytest -q                        # re-run tests on every save

CUDA_VISIBLE_DEVICES=$(usm gpu free 2) python train.py   # grab the 2 idlest GPUs
```

Install any script as a short command on your `PATH`:

```bash
usm install clash cx     # `cx ...` now runs `usm clash ...`
usm uninstall cx
```

## Updating

```bash
usm update               # refresh the catalog
usm update --all         # re-download every cached script
usm update tunnel        # refresh a single script
```

Scripts self-heal: if cached files stop matching the catalog's recorded hash,
`usm` refreshes the manifest before building the environment, so a stale
catalog can never pair old requirements with new code.

## How it works

The CLI (`src/usmo/cli/`) is a thin frontend over the `usmo.core` SDK; the
command manifest lives in [`scripts/_config.json`](scripts/_config.json).
Adding a command means adding a script and an entry — no Python changes.

- Shell scripts run under `bash`; Python scripts run under the current
  interpreter, or in a persistent per-script venv when they declare
  `requirements`.
- Scripts are fetched from this repository and cached, pinned by content hash.
- `--upgrade` forces a fresh download; `--debug` runs the local file in
  `scripts/` instead, so iterating feels like editing any other repo file.

See [Architecture](https://hspk.github.io/usm/architecture/) for the full
picture, and [Development](https://hspk.github.io/usm/development/) to
contribute a script.

## Notes

- Some scripts target Ubuntu; `init`, `clip`, and `tunnel` are cross-platform.
- `azrole`, `blobmount`, `azsync`, and `cp` expect Azure CLI / `azcopy` /
  `blobfuse2` as described in their command documentation.

## License

[MIT](LICENSE) © Hangxing Wei
