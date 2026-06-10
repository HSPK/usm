# Built-in helpers

Commands implemented directly in `usmo.cli` — they don't pull anything
from `scripts/` and never spawn a subprocess (with one exception:
`update` re-downloads scripts).

## `usm list`

Print every command (scripts + built-ins) with cache status and any
declared `uv` requirements.

```bash
usm list
```

Sample output:

```text
Available commands:

Scripts:
  blobmount             Mount a blob storage as a filesystem.              not cached
  check_py              Check Python3 installation and version.            cached
  cp                    Copy files with blob storage support.              not cached  +uv(3 req)
  cu122                 Setup CUDA 12.2 environment.                       not cached
  init                  Initialize a new machine setup.                    cached
  inject-alias          Insert or update the managed usm alias block ...   cached      +uv(1 req)
  openai-proxy          Run a local OpenAI-compatible proxy that for...    not cached  +uv(5 req)
  sysinfo               Print system, GPU, CUDA, MPI, ...                  not cached
  tunnel                Manage SSH tunnels (local/remote/SOCKS) with ...   cached      +uv(2 req)

Built-in:
  list                  List all available commands.
  update                Re-download config and all cached scripts.
  clean                 Remove the script cache directory.
  version               Show usm version.
```

The `+uv(N req)` tag means the script declares `requirements` in
`_config.json` and will run under `uv run --with ...`.

## `usm update`

Re-download `_config.json` plus every script that's currently cached.
Scripts that have never been cached are *not* downloaded — the goal of
update is to keep what you use fresh, not to bulk-fetch everything.

```bash
usm update
```

Pass one or more names to refresh just those scripts (forced download
even if they were never cached):

```bash
usm update share
usm update share cp
```

Output:

```text
Downloading: _config.json
Downloading: init.sh
  ✓ init
  – blobmount (not cached, skipped)
  ...
Update complete.
```

## `usm clean`

Remove `~/.cache/usm/scripts/` (the script cache directory). Next run of
any script will redownload it.

```bash
usm clean
```

Does **not** touch:

- `~/.cache/usm/tunnels/` (state files / logs for `usm tunnel`)
- `~/.cache/usm/.last_check` (auto-update probe timestamp)
- `~/.config/systemd/user/usm-tunnel-*.service` (installed units)

If you really want a fresh slate: `rm -rf ~/.cache/usm`.

## `usm version`

Print the installed `usmo` version.

```bash
usm version
# -> usm version 0.3.0
```

Resolved from (in order):

1. The `__version__` baked into `src/usmo/_version.py` at build time
   (`hatch-vcs` writes the git tag here).
2. `importlib.metadata.version("usmo")` as a fallback.
3. `unknown (editable install without build)` if both fail (e.g. when
   running straight from a `git clone` without `uv sync`).

## Auto-update probe

Independently of the commands above, any `usm <something>` invocation
may briefly fetch the upstream `_config.json` to check whether any of
your cached scripts have a newer version. If yes, it prints a banner
and (in an interactive TTY) prompts you to run `usm update`.

Controlled by `USM_AUTO_CHECK_INTERVAL` (seconds). The default is
`86400` (24h). `USM_AUTO_CHECK_INTERVAL=0` disables it entirely.
