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
| max delay | `--max-delay 300` | Something is *always* writing; cap the staleness. |
| heartbeat | `--interval 3600` | Reconcile even with zero detected changes. |
| manual | `usm azsync sync <id>` | Right now. |

And two suppressors:

| Suppressor | Default | Meaning |
| --- | --- | --- |
| rate limit | `--min-gap 30` | Never start within 30s of the last transfer finishing. |
| backoff | — | After a failure: 30s, 60s, 120s … capped at 15m, reset on success. |

Triggers are deliberately time-only. Watcher events are coalesced by relative
path, and bytes mean growth since the previous observation rather than the
file's full size. A 1 GiB `train.log` receiving 10,000 appends is still one
dirty file; it does not launch 10,000 syncs or repeatedly add 1 GiB to a
counter. While it stays active, `max-delay` controls how often azcopy catches
up. Once it stops, `quiet-period` performs the final reconcile.
`--batch-files`, `--batch-bytes` and `--min-files` are removed. Old saved values
are ignored; file counts and estimated growth are diagnostic only.

### Normal sync and publisher are separate jobs

An active log can change while AzCopy reads it. AzCopy reports that cycle as
`partial`: other files arrived, but the moving log did not. That is not a
reason to hold a stable checkpoint hostage.

An azsync job therefore has exactly one inferred mode:

```text
no publish selector                  → normal
--publish-path / --publish-pattern   → publisher
```

There is no `--mode` switch and no mixed job. Use two definitions:

```bash
# Logs, metrics and ordinary output.
usm azsync add ./output <url> \
  --name train-live \
  --exclude checkpoints/

# Checkpoints only; no ordinary azcopy sync is ever run.
usm azsync add ./output/checkpoints <url>/checkpoints \
  --name train-checkpoints \
  --publish-pattern 'checkpoint-*' \
  --ready-marker .complete \
  --after-publish delete
```

Each has its own process, history, failure counter and backoff. The log job can
remain partial forever without delaying, gating or even being observed by the
checkpoint publisher. It also cannot make the 454 GiB publisher wait behind a
log reconcile. Once the log stops changing, its next quiet-period sync
converges to `ok`.

### Training-complete signal

`sync` wakes the daemon immediately but does not weaken checkpoint safety.
For a trainer that has just written `.complete`, use `flush`:

```bash
touch output/checkpoints/checkpoint-1000/.complete
usm azsync flush train-checkpoints \
  --checkpoint checkpoint-1000 \
  --wait \
  --timeout 1800
```

The request is persisted before the daemon is signalled. On POSIX, `SIGUSR1`
provides the low-latency wakeup; the JSON event on disk carries the meaning,
so a coalesced signal, Windows, or a supervisor crash cannot lose it. Claimed
unfinished events return to the pending queue after a crash. A terminal result
already written is never replayed. Invalid requests receive an explicit error.

An explicit flush accelerates `--publish-stable`, but does not bypass the
gate. It takes a snapshot, waits `--settle` (1 second), and takes another:

```text
.complete exists and is newest
  → snapshot A
  → settle
  → snapshot B
  → A.identity == B.identity
  → publish payload / manifest / marker
```

`--publish-min-age`, selectors and remote verification still apply. A write,
inode replacement or marker change during the settle returns exit code 2 and
leaves the checkpoint local. `--publish-keep-last` does **not** delay remote
publication; it only exempts the newest checkpoints from local deletion after
they are published. `--wait` returns when the publisher job finishes.

| Exit | Meaning |
| ---: | --- |
| `0` | Publication completed |
| `2` | Selected checkpoint is not ready |
| `3` | Partial transfer |
| `4` | Authentication/network/publish failure |
| `5` | Timed out waiting; the durable request may still complete |
| `130` | Cancelled |

Transfers never overlap within a job. Watcher changes belong to the next
batch; explicit signals remain queued until the current operation finishes.
Direct and queued commands use the same result and exit-code rules.

```bash
# Chatty build output: tolerate 10 minutes of staleness
usm azsync add ./out https://acct.blob.core.windows.net/artifacts/out \
    --quiet-period 15 --max-delay 600

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
usm azsync sync data-bucket             # enqueue an immediate reconcile
usm azsync sync data-bucket --wait      # and wait for its result
usm azsync flush data-bucket --wait     # training done: short safe settle
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

## Gated checkpoint publication

A training checkpoint is not an ordinary directory. Uploading it while the
trainer is still writing produces a remote checkpoint that looks usable but
isn't; uploading `.complete` in the same azcopy job is not sufficient because
azcopy does not promise that the marker arrives last.

Gate checkpoint publication behind a local marker:

```bash
usm azsync add ./output/checkpoints \
  https://acct.blob.core.windows.net/runs/job-1/checkpoints \
  --publish-pattern 'checkpoint-*' \
  --publish-unit directory \
  --ready-marker .complete \
  --publish-stable 120 \
  --after-publish keep
```

This definition is a publisher because it has a publish selector. It never
runs ordinary `azcopy sync`; create a separate normal definition for logs and
exclude the checkpoint directory there. A checkpoint is eligible only when:

1. `.complete` exists and is newer than every payload file;
2. its file list, sizes, mtimes and inode identities remain unchanged for
   `--publish-stable`;
3. it is older than `--publish-min-age`.

Publication is ordered:

```text
payload (excluding .complete)
  → verify completed file count + transferred bytes
  → .azsync-manifest.json
  → .complete, as a separate final upload
```

Remote consumers should only use checkpoints with `.complete`. An existing
remote marker is a conflict by default; `--publish-conflict replace`
explicitly removes it before republishing, so an old marker never advertises
new payload as complete. Each upload addresses the exact checkpoint path,
not an `--include-path` prefix that could also match `checkpoint-1000-old`.

`--publish-verify size` requires exact AzCopy transfer counts/bytes with
length checking enabled. `md5` additionally sets `--put-md5`; it does **not**
download the remote payload or independently recompute its hash.
Checkpoint fingerprints describe metadata, not a cryptographic content hash.
The trainer must finish and close all writes before creating the marker and
must treat that checkpoint version as immutable.

To offload old checkpoints after publishing:

```bash
usm azsync add ./output/checkpoints <url>/checkpoints \
  --publish-pattern 'checkpoint-*' \
  --publish-unit directory \
  --ready-marker .complete \
  --publish-keep-last 2 \
  --after-publish delete
```

Deletion is still not direct. The unchanged local unit is atomically renamed
under `.azsync-moved/<transaction>/`, the transaction is persisted, then the
quarantine is revalidated and removed. Recovery only deletes quarantine with
matching persisted ownership and snapshot proof. Unknown, modified or corrupt
state fails closed and requires inspection; it is not silently discarded.
Local deletion requires POSIX directory handles. File-unit offload also hashes
the payload before and after rename to detect same-size writes racing that
rename; this costs two local reads. Directory-unit offload uses its recorded
tree identities. Neither is an independent remote content-hash verification.

`--publish-*` and `--delete` are incompatible: after offload removes the
source, destination mirroring would remove the archived blob on the next
sync.

`--publish-keep-last 2` means every ready checkpoint is published immediately,
but the newest two remain on local disk for fast training resume. With
checkpoints 100/200/300/400/500, all five exist remotely after publication;
100/200/300 are eligible for local cleanup and 400/500 are retained.
When a retained, already-published checkpoint later ages out of this window,
it can be cleaned locally without uploading it again.

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
| `--max-delay` | `300` | Staleness cap under constant writes. |
| `--interval` | `3600` | Reconcile with no changes detected. |
| `--min-gap` | `30` | Minimum idle time between transfers. |
| `--watch-mode` | `auto` | `inotify` or `poll`. |
| `-e/--exclude` | — | Extra exclude pattern (repeatable). |
| `--delete` | off | Mirror mode; deletes remote extras. |
| `--compare-hash` | off | MD5 instead of last-modified time. |
| `--cap-mbps` | — | Throttle the transfer. |
| `--publish-path` / `--publish-pattern` | — | Select gated checkpoint units. |
| `--publish-unit` | `directory` | Publish/delete one directory or one file. |
| `--ready-marker` | `.complete` | Completion gate, always uploaded last. |
| `--publish-stable` | `120` | Required unchanged period in seconds. |
| `--publish-keep-last` | `2` | Keep the newest checkpoints local. |
| `--after-publish` | `keep` | Keep or delete the local checkpoint. |
| `--publish-verify` | `size` | `azcopy`, `size`, or `md5`. |

## Prerequisites

- `azcopy` — install with `usm cp --install`, or set `$USM_AZCOPY_BIN`.
  Both commands share `~/.cache/usm/bin/azcopy`.
- `az` CLI logged in, for `--auth az` (the default) and `--auth aad`.

## Structure

- `azsync.py`: CLI, one job runtime, time policy, AzCopy transport.
- `usm_publish.py`: checkpoint selection, snapshots, ledger and safe quarantine.
- `usm_checkpoint.py`: payload/manifest/marker sequence and local retention.
- `usm_signal.py`: persistent requests, immutable terminal results.

The SAS lifecycle, blob URL handling, service units, locking and redaction
live in
[`scripts/usm_azure.py`](https://github.com/HSPK/usm/blob/main/scripts/usm_azure.py),
shared verbatim with [`usm blobmount`](blobmount.md) — so a credential source
added for one command works in the other.

## Source

[`scripts/azsync.py`](https://github.com/HSPK/usm/blob/main/scripts/azsync.py)
uses a single runtime/result path for the selected mode. No dual-lane state or
composite result remains.

Tests are split into transport/normal-sync, publication, control signals and
mode/scheduler contracts (`tests/test_azsync*.py`). Shared fixtures live in
`tests/azsync_support.py`. Pure checkpoint and queue invariants have their own
suites. Fake AzCopy subprocesses and temporary files exercise failure, restart
and publication ordering without touching production storage.
