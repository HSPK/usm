# Built-in helpers

Commands implemented directly in `usmo.cli` — they don't pull anything
from `scripts/` and never spawn a subprocess (exceptions: `update` and
`install` read/refresh the catalog).

Every built-in supports `-h` / `--help`:

```bash
usm list --help
usm update -h
```

## `usm` (no arguments)

A short landing page — what usm is and where to go next. It deliberately
does **not** list the catalog: with 25+ commands that was a wall of text,
and it meant every bare `usm` hit the cache (or the network) before it
could print anything.

```text
usm 0.10.0
Run cached utility scripts from one CLI.

  usm list             See every available command
  usm search <query>   Find a command by name or description
  usm <name> --help    Help for one command
  usm update           Refresh the catalog
```

It touches nothing on disk, so it renders instantly even on a cold machine
with no network.

## `usm list`

List every available command.

```bash
usm list                 # everything, plus the built-ins
usm list azure           # only commands matching "azure"
usm list --cached        # only what's already downloaded
usm list --missing       # only what isn't
usm list --names         # bare names, one per line (for scripts/completion)
```

```text
Commands

     name         version   description
 ────────────────────────────────────────────────────────────────────────
 ●   azsync       v1.0.2    Watch a directory and keep it synced to Azure Blob.
 ○   bench        v1.0.3    Benchmark CPU / memory / disk / network / GPU.
 ●   blobmount    v1.0.2    Mount Azure Blob containers, refreshing the SAS.

● cached  ○ not yet downloaded  · 2/3 cached · usm <name> --help for details
```

The leading glyph is the cache state — `●` downloaded, `○` not yet. A
command that hasn't been downloaded works exactly the same; it is fetched
on first use.

`PATTERN` matches against both the name and the description, so
`usm list azure` finds `blobmount` even though "azure" isn't in its name.

## `usm search`

The same matching as `usm list PATTERN`, but phrased as a lookup: it
reports the number of hits, highlights what matched, and exits non-zero
when nothing does (so it composes in scripts).

```bash
usm search blob
usm search blob --names | xargs -n1 usm update
```

```text
2 match(es) for 'blob'

     name         version   description
 ────────────────────────────────────────────────────────────────────────
 ●   blobmount    v1.0.2    Mount Azure **Blob** containers, refreshing the SAS.
 ○   cp           v1.0.3    Copy files, with Azure **blob** support.
```

A near miss suggests a correction rather than dumping the catalog:

```text
$ usm search blomount
No command matches blomount.
Did you mean: blobmount?
```

The same suggestion appears when you mistype a command name outright
(`usm blomount`).

## `usm update`

`usm update` with no arguments refreshes **only** the catalog
(`_config.json`) — cheap, and enough to learn which scripts have new
versions. It does not touch cached script files. It prints a table of
what changed since your last refresh (version and short hash):

```bash
usm update            # refresh _config.json only
```

```text
Catalog changes (2)
  script   version         hash
  bench    1.0.3 → 1.0.4   ddf8e82 → 1a2b3c4
  clash    1.0.7 → 1.1.0   fe51647 → 9988776
Run usm update --all to pull the new scripts.
```

(When nothing changed it prints `Catalog is up to date.`; on a cold cache,
`Fetched catalog (N scripts).`)

Pull script files explicitly:

```bash
usm update --all      # re-download every currently-cached script
usm update share      # refresh one script (downloaded even if never cached)
usm update share cp   # ...or several
```

`--all` only refreshes scripts you've already used; it won't bulk-fetch
the entire catalog. Named scripts are always (re)downloaded and shown with
their resulting version and short hash.

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

Remove `~/.cache/usm/scripts/` (cached script files) and
`~/.cache/usm/envs/` (per-script virtualenvs). The next run of any script
redownloads it and rebuilds its env on demand.

```bash
usm clean
```

Does **not** touch:

- `~/.cache/usm/tunnels/` (state files / logs for `usm tunnel`)
- `~/Library/LaunchAgents/com.github.hspk.usm.tunnel.*.plist` (macOS autostart)
- `~/.config/systemd/user/usm-tunnel-*.service` (Linux autostart)

If you really want a fresh slate: `rm -rf ~/.cache/usm`.

## `usm version`

Print the installed `usmo` version.

```bash
usm version
usm -V          # same thing
# -> usm version 0.10.0
```

Resolved from (in order):

1. The `__version__` baked into `src/usmo/_version.py` at build time
   (`hatch-vcs` writes the git tag here).
2. `importlib.metadata.version("usmo")` as a fallback.
3. `unknown (editable install without build)` if both fail (e.g. when
   running straight from a `git clone` without `uv sync`).
