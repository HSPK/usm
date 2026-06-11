# Built-in helpers

Commands implemented directly in `usmo.cli` — they don't pull anything
from `scripts/` and never spawn a subprocess (exceptions: `update` and
`install` read/refresh the catalog).

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
  list                  List all commands.
  update                Refresh the catalog; --all or NAME pulls scripts.
  install               Install a script as an alias in ~/.local/bin.
  uninstall             Remove an installed alias.
  clean                 Remove the script cache.
  version               Show usm version.
```

The `+uv(N req)` tag means the script declares `requirements` in
`_config.json` and will run under `uv run --with ...`.

## `usm update`

`usm update` with no arguments refreshes **only** the catalog
(`_config.json`) — cheap, and enough to learn which scripts have new
versions. It does not touch cached script files.

```bash
usm update            # refresh _config.json only
```

Pull script files explicitly:

```bash
usm update --all      # re-download every currently-cached script
usm update share      # refresh one script (downloaded even if never cached)
usm update share cp   # ...or several
```

`--all` only refreshes scripts you've already used; it won't bulk-fetch
the entire catalog. Named scripts are always (re)downloaded.

```text
Downloading: _config.json
Downloading: share.py
  ✓ share
Update complete.
```

## `usm install`

Install a script as a short standalone command (a tiny shim in
`~/.local/bin` that execs `usm <script>`):

```bash
usm install clash cx     # `cx ...` now runs `usm clash ...`
cx status
```

- If the alias name already exists and **isn't** a usm shim, you're
  prompted before it's overwritten (never clobbered silently).
- If `~/.local/bin` isn't on your `PATH`, usm prints the line to add.

## `usm uninstall`

Remove an alias previously created by `usm install`:

```bash
usm uninstall cx
```

Files in `~/.local/bin` that usm didn't create are refused (it only
removes its own shims).

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
and (in an interactive TTY) prompts you to pull them (`usm update --all`).

Controlled by `USM_AUTO_CHECK_INTERVAL` (seconds). The default is
`86400` (24h). `USM_AUTO_CHECK_INTERVAL=0` disables it entirely.
