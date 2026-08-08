# `usm azsync`

Watch a local directory and keep it mirrored to Azure Blob Storage. Think
`rsync -avuP` that never exits: it batches changes, transfers them with
`azcopy sync`, and rotates the SAS token underneath you.

```bash
usm azsync add ./data https://acct.blob.core.windows.net/bucket/data
usm azsync ls
usm azsync status data-bucket
```

## Why not just loop `usm cp`

`usm cp` is a one-shot transfer. `azsync` adds the three things a long-lived
mirror needs:

- **Batching** — a build that rewrites 5 000 files must not launch 5 000
  transfers, and a single saved file must not wait an hour.
- **Credential rotation** — `azcopy` bakes the SAS into the job at start and
  *cannot* rotate it mid-flight, so something has to mint a fresh one between
  transfers and recover when one expires anyway.
- **Supervision** — a daemon, restart-on-failure, and boot integration.

## When it transfers

The trigger policy is the heart of the command. A transfer fires on the
**first** of these:

| Trigger | Default | Meaning |
| --- | --- | --- |
| quiet period | `--quiet-period 5` | Writes stopped for 5s — the normal path. |
| volume | `--batch-files 200` / `--batch-bytes 256MiB` | Enough changed; go now, don't wait for quiet. |
| max delay | `--max-delay 300` | Something is *always* writing; cap the staleness. |
| heartbeat | `--interval 3600` | Reconcile even with zero detected changes. |
| manual | `usm azsync sync <id>` | Right now. |

And two suppressors:

| Suppressor | Default | Meaning |
| --- | --- | --- |
| rate limit | `--min-gap 30` | Never start within 30s of the last transfer finishing. |
| backoff | — | After a failure: 30s, 60s, 120s … capped at 15m, reset on success. |

A transfer already running never queues a second one; changes that arrive
while it runs simply belong to the next batch. If a transfer fails, its batch
is merged back so nothing is silently dropped.

```bash
# Chatty build output: coalesce hard, tolerate 10 minutes of staleness
usm azsync add ./out https://acct.blob.core.windows.net/artifacts/out \
    --quiet-period 15 --batch-files 1000 --max-delay 600

# Precious small files: push almost immediately
usm azsync add ./notes https://acct.blob.core.windows.net/backup/notes \
    --quiet-period 2 --min-gap 5
```

## The watcher is only a trigger

`azcopy sync` compares the whole tree on every run, so a missed filesystem
event costs *latency*, never correctness — the next heartbeat repairs it.
That is what makes the fallbacks safe:

- **inotify / FSEvents** (via `watchdog`) is used when available.
- **Polling** takes over automatically on network mounts, blobfuse
  mountpoints, or when the inotify watch limit is hit (`--watch-mode poll`
  forces it, `--poll-interval` tunes it).
- On very large trees the poller drops its per-file index and falls back to an
  aggregate signature, marking itself *degraded* — which just forces the next
  reconcile.

Excludes are applied in the watcher **and** translated into azcopy's
`--exclude-path` / `--exclude-pattern` / `--exclude-regex` flags, so `.git`
churn can't wake the daemon for a transfer that would skip it anyway.
Defaults match `usm rsync` (`.git/`, `node_modules/`, `__pycache__/`,
`*.pyc`, …) plus scratch files (`*.tmp`, `*.part`, `*.swp`).

## Credentials

`azcopy` fixes the credential when a job starts and cannot rotate it
mid-transfer. `azsync` therefore refreshes *between* transfers, and asks for
enough remaining lifetime to cover the next one (adaptively: three times the
last transfer's duration, floor `--sas-min-remaining`, default 30 min). If a
job dies mid-flight with an expired signature anyway, the token is re-minted
and the job **resumed** — completed blocks are not re-uploaded.

| `--auth` | Flag | Source |
| --- | --- | --- |
| `aad` | — | No SAS at all; azcopy uses your Entra login and refreshes its own token. **Prefer this.** |
| `az` | `--sas-ttl-hours` | Mint a user-delegation SAS with the Azure CLI (default). |
| `inline` | — | A `sig=` already in the URL. Cannot be rotated; you get a warning as it nears expiry. |
| `env` | `--sas-env NAME` | Read an environment variable. |
| `file` | `--sas-file PATH` | Re-read a file on every refresh — works with any external rotator. |
| `exec` | `--sas-command CMD` | Run a command, take stdout. |
| `http` | `--sas-url URL` | GET an endpoint (`--sas-header 'K: V'`, repeatable). |

The four external sources accept a bare token, a full URL, or JSON
(`{"sas": "...", "expires_at": "..."}`). Whatever they return, the expiry is
taken from the token's own `se=` field — a provider that lies about its
lifetime cannot cause a mid-job failure. Passing any `--sas-*` flag selects
the matching `--auth` for you.

```bash
# Rotated by an external agent writing to a file
usm azsync add ./data https://acct.blob.core.windows.net/bucket/data \
    --sas-file /run/secrets/blob.sas

# Minted on demand by your own service
usm azsync add ./data https://acct.blob.core.windows.net/bucket/data \
    --sas-url https://sas.internal/mint?container=bucket \
    --sas-header "Authorization: Bearer $TOKEN"
```

Tokens are cached `0600` and every log line, table and error is redacted
(`sig=***`). One caveat: `azcopy sync` has no way to take a SAS other than in
the URL, so it *is* visible in `ps` while a transfer runs — use `--auth aad`
where you can.

## Running it

```bash
usm azsync add ./data https://acct...   # define + start watching
usm azsync ls                           # status, pending changes, SAS clock
usm azsync status data-bucket           # detail + recent transfer history
usm azsync sync data-bucket             # transfer now
usm azsync dry-run data-bucket          # what would move, transfers nothing
usm azsync logs data-bucket -f          # follow (--azcopy for azcopy's log)
usm azsync stop|start|restart data-bucket
usm azsync enable data-bucket           # systemd user unit / launchd agent
usm azsync rm data-bucket
```

`usm azsync once ./dir <url>` runs a single sync without defining anything —
`usm cp` with excludes and the SAS machinery.

`enable` writes a systemd user unit (`usm-azsync-<id>.service`) or a launchd
agent running `usm azsync up <id>` with `Restart=always`. On Linux, run
`loginctl enable-linger $USER` if it should survive logout.

## Safety

`--delete` is opt-in and destructive: it removes blobs that no longer exist
locally, on every transfer. Without it nothing is ever deleted at the
destination.

## Limits

- A large file still being written can be uploaded truncated; the next
  transfer repairs it. The quiet period is the mitigation — `azcopy` has no
  "file must be stable for N seconds" filter.
- Comparison is by last-modified time. `--compare-hash` switches to MD5:
  exact, but it reads every file.
- `stop_reason`-style detail is limited by azcopy: it never reports *which*
  files it skipped, only counts.
- Local → blob only. Pulling the other way is a plain `usm cp`.

## Flags

`usm azsync add --help` for the full list. The ones worth knowing:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--auth` | `az` (or `inline`) | Credential source, see above. |
| `--quiet-period` | `5` | Debounce before transferring. |
| `--batch-files` / `--batch-bytes` | `200` / `256MiB` | Fire early on big batches. |
| `--max-delay` | `300` | Staleness cap under constant writes. |
| `--interval` | `3600` | Reconcile with no changes detected. |
| `--min-gap` | `30` | Minimum idle time between transfers. |
| `--watch-mode` | `auto` | `inotify` or `poll`. |
| `-e/--exclude` | — | Extra exclude pattern (repeatable). |
| `--delete` | off | Mirror mode; deletes remote extras. |
| `--compare-hash` | off | MD5 instead of last-modified time. |
| `--cap-mbps` | — | Throttle the transfer. |

## Prerequisites

- `azcopy` — install with `usm cp --install`, or set `$USM_AZCOPY_BIN`.
  Both commands share `~/.cache/usm/bin/azcopy`.
- `az` CLI logged in, for `--auth az` (the default) and `--auth aad`.

## Shared module

The SAS lifecycle, blob URL handling, service units, locking and redaction
live in
[`scripts/usm_azure.py`](https://github.com/HSPK/usm/blob/main/scripts/usm_azure.py),
shared verbatim with [`usm blobmount`](blobmount.md) — so a credential source
added for one command works in the other.

## Source

[`scripts/azsync.py`](https://github.com/HSPK/usm/blob/main/scripts/azsync.py)
plus the shared `usm_azure.py`. Layered so each piece is testable on its own:
a pure trigger policy, a watcher protocol with two backends, a SAS provider
protocol with seven, and an azcopy engine — wired together by a single
supervisor.

Test suite at
[`tests/test_azsync.py`](https://github.com/HSPK/usm/blob/main/tests/test_azsync.py)
(288 tests, 95% line coverage). The engine and daemon run end to end against a
fake `azcopy` injected via `$USM_AZCOPY_BIN`, covering success, partial
failure, credential expiry → resume, network backoff, corrupt state files and
watcher fallback — without touching Azure. The listing is asserted to fit
terminals from 70 to 200 columns and to never print a token.
