# Commands

Every subcommand of `usm` falls into one of two buckets:

- **Scripts** — declared in
  [`scripts/_config.json`](https://github.com/HSPK/usm/blob/main/scripts/_config.json),
  downloaded on first use, cached under `~/.cache/usm/scripts/`. Add new
  commands by editing that JSON file; no Python changes needed.
- **Built-ins** — implemented directly in `usmo.cli`. They don't touch the
  network (except `update`).

## Scripts

| Command | Page |
| --- | --- |
| `tunnel` | [SSH tunnel manager](tunnel.md) |
| `init` | [Machine bootstrap](init.md) |
| `blobmount` | [Mount Azure blob](blobmount.md) |
| `cp` | [Copy with Azure support](cp.md) |
| `cu122` | [CUDA 12.2 install](cu122.md) |
| `inject-alias` | [Shell alias injection](inject-alias.md) |
| `openai-proxy` | [OpenAI → TRAPI proxy](openai-proxy.md) |
| `sysinfo` / `check_py` | [System / Python info](sysinfo.md) |

## Built-in helpers

See [Built-in helpers](builtin.md): `list`, `update`, `clean`, `version`.

## Global flags

These work for any subcommand:

| Flag | Purpose |
| --- | --- |
| `-U`, `--upgrade` | Force re-download of the script before running it. |
| `--debug` | Bypass the cache; run from `./scripts/<name>` in the current dir (for iterating on scripts inside a `git clone`). |
| `-h`, `--help` | Print usage for that command (works on the top-level CLI too). |

## Exit codes

`usm` propagates the script's exit code unchanged. If `ssh` exits 255 or your
script errors with `click.UsageError`, that's what your shell sees. The CLI
wrapper itself only emits its own non-zero exit on `MissingUv` (the script
needs `uv` but it's not installed) and on `OSError` from the spawn itself.

## Environment variables

| Variable | Effect |
| --- | --- |
| `USM_AUTO_CHECK_INTERVAL` | Seconds between auto-update probes. `0` disables. Default: `86400` (24h). |
