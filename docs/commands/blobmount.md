# `usm blobmount`

Mount an Azure Storage container as a local filesystem with
[`blobfuse2`](https://github.com/Azure/azure-storage-fuse), and keep the
credential alive for as long as the mount lives.

```bash
usm blobmount mount /mnt/data myaccount mycontainer
usm blobmount ls
usm blobmount status data
```

## Why it isn't a one-shot script any more

The previous shell version minted a 6-day SAS at mount time and stopped
there. When the token expired the mount stayed *up* — `mountpoint` was still
happy — but every read started failing, which looks like data loss until you
find the FUSE log. blobfuse2 reads its credential once, at mount, and has no
way to be handed a new one.

So `blobmount` now runs a small supervisor per mount. It watches two things:

- **the SAS clock** — below `--sas-min-remaining` (default 30 min) it mints a
  fresh token, rewrites the config and remounts, well before anything breaks;
- **the mount itself** — a cheap `listdir` every `--probe-interval`
  (default 60s) distinguishes *healthy* from *mounted but broken*, and
  repairs the latter.

## Health, honestly

`usm blobmount ls` and `check` report a real probe, not just "is something
mounted":

| State | Meaning |
| --- | --- |
| `ok` | Mounted and readable. |
| `denied` | Mounted, but I/O fails — almost always an expired credential. |
| `stale` | `ENOTCONN`: blobfuse2 died and left the kernel mount behind. |
| `unmounted` | The directory exists but nothing is mounted on it. |
| `missing` | The directory (or its parent) is gone. |

The probe is bounded by a timer, because a hung FUSE mount will block
`listdir` forever otherwise.

```bash
usm blobmount check          # probe every mount; exit 1 if any is unhealthy
usm blobmount check data     # just one
```

That makes it usable straight from a health check or a cron guard.

## Credentials

Seven sources share the same SAS/AAD layer as [`usm azsync`](azsync.md).
`fic` is a blobmount-only adapter over the standard Kubernetes Workload
Identity environment.

| `--auth` | Flag | Source |
| --- | --- | --- |
| `az` | `--sas-ttl-hours` | Mint a user-delegation SAS with the Azure CLI (default). |
| `aad` | — | No SAS: blobfuse2 uses your Azure CLI login. Nothing to rotate. |
| `fic` | — | No SAS: blobfuse2 uses an Azure Workload Identity projected token. |
| `inline` | — | A SAS you supply. Cannot be rotated. |
| `env` | `--sas-env NAME` | An environment variable. |
| `file` | `--sas-file PATH` | A file, re-read on every refresh. |
| `exec` | `--sas-command CMD` | Run a command, take stdout. |
| `http` | `--sas-url URL` | GET an endpoint (`--sas-header 'K: V'`). |

Expiry always comes from the token's own `se=` field, so a provider that
over-promises can't cause a mid-life failure. The token is cached `0600`,
the rendered blobfuse2 config is `0600`, and every log line, table and error
message is redacted (`sig=***`).

```bash
# Kubernetes pod mutated by Azure Workload Identity
usm blobmount mount /mnt/data acct bucket --auth fic --no-supervise

# Rotated by an external agent
usm blobmount mount /mnt/data acct bucket --sas-file /run/secrets/blob.sas

# Minted by your own service
usm blobmount mount /mnt/data acct bucket \
    --sas-url https://sas.internal/mint?container=bucket \
    --sas-header "Authorization: Bearer $TOKEN"
```

## Commands

```bash
usm blobmount mount <dir> <account> <container>   # or a container URL
usm blobmount mount <dir> https://acct.blob.core.windows.net/bucket
usm blobmount ls [--all]        # --all also lists unmanaged blobfuse2 mounts
usm blobmount status <id>       # detail: health, SAS clock, refresh counters
usm blobmount check [<id>]      # probe; non-zero exit when unhealthy
usm blobmount refresh <id>      # rotate the SAS and remount now
usm blobmount config <id>       # show the rendered config (redacted)
usm blobmount logs <id> [-f]
usm blobmount start|stop <id>   # the supervisor, not the mount
usm blobmount umount <id> [--lazy]
usm blobmount enable|disable <id>
usm blobmount rm <id> [--keep-mounted]
```

`--no-supervise` mounts once and exits, matching the old shell behaviour —
useful in a container where something else owns the lifecycle. You get a
reminder that a SAS will then never be refreshed. FIC and AAD credentials
self-refresh and do not need SAS rotation.

### FIC / Azure Workload Identity

`--auth fic` requires the standard webhook contract:

```text
AZURE_CLIENT_ID
AZURE_TENANT_ID
AZURE_FEDERATED_TOKEN_FILE
```

The token file must exist and be readable. blobmount renders:

```yaml
azstorage:
  mode: spn
  tenantid: <AZURE_TENANT_ID>
  clientid: <AZURE_CLIENT_ID>
  oauth-token-path: <AZURE_FEDERATED_TOKEN_FILE>
```

No SAS is generated, cached, logged, or written into the config. A Kubernetes
workload normally obtains these values by setting `serviceAccountName`, adding
pod label `azure.workload.identity/use: "true"`, annotating the ServiceAccount
with `azure.workload.identity/client-id`, and configuring the matching FIC on
the managed identity.

## Start at boot

```bash
usm blobmount enable data
loginctl enable-linger $USER    # Linux: survive logout
```

Writes a systemd user unit (`usm-blobmount-<id>.service`) or a launchd agent
running `usm blobmount up <id>` with `Restart=always`, so the mount comes back
after a reboot *and* keeps rotating its credential.

## Installing blobfuse2

```bash
usm blobmount install
```

Pulls the pinned upstream `.deb`, installs `fuse3`, and adds
`user_allow_other` to `/etc/fuse.conf` (needed for `--allow-other`, which is
on by default; use `--no-allow-other` to skip that requirement). Debian and
Ubuntu only — elsewhere install blobfuse2 yourself and point
`$USM_BLOBFUSE2_BIN` at it.

## Shared modules

The SAS lifecycle, blob URL handling, service units, locking and redaction
live in
[`scripts/usm_azure.py`](https://github.com/HSPK/usm/blob/main/scripts/usm_azure.py),
shared verbatim with [`usm azsync`](azsync.md). Scripts declare shared modules
in `_config.json`:

```json
"blobmount": {
  "path": "blobmount.py",
  "modules": ["usm_azure.py", "usm_fic.py"]
}
```

`usm_fic.py` owns only projected-token validation. Module bytes are folded
into the command hash, so cached installs update atomically.

## Flags worth knowing

| Flag | Default | Purpose |
| --- | --- | --- |
| `--auth` | `az` | Credential source. |
| `--sas-min-remaining` | `1800` | Rotate below this many seconds left. |
| `--refresh-interval` | `21600` | Upper bound between refreshes. |
| `--probe-interval` | `60` | How often to verify the mount reads. |
| `--read-only` | off | Mount the container read-only. |
| `--cache-dir` / `--cache-size-mb` | per-container | `file_cache` location and cap. |
| `--no-allow-other` | off | Skip `--allow-other` and the `/etc/fuse.conf` edit. |
| `--no-supervise` | off | Mount once; do not keep the SAS fresh. |

## Prerequisites

- `blobfuse2` — `usm blobmount install`, or `$USM_BLOBFUSE2_BIN`.
- `az` CLI logged in, for `--auth az` (default) and `--auth aad`.
- Azure Workload Identity webhook variables and projected token for
  `--auth fic`.
- A writable parent for the mount directory (it is created if missing).

## Companion commands

Use [`usm cp`](cp.md) to move data in and out — it detects paths under a
blobfuse2 mount and routes them through `azcopy`, far faster than going
through FUSE. [`usm azsync`](azsync.md) does the same continuously.

## Source

[`scripts/blobmount.py`](https://github.com/HSPK/usm/blob/main/scripts/blobmount.py)
plus the shared
[`scripts/usm_azure.py`](https://github.com/HSPK/usm/blob/main/scripts/usm_azure.py)
and [`scripts/usm_fic.py`](https://github.com/HSPK/usm/blob/main/scripts/usm_fic.py).

Test suite at
[`tests/test_blobmount.py`](https://github.com/HSPK/usm/blob/main/tests/test_blobmount.py)
(191 tests, 97% line coverage) and
[`tests/test_usm_azure.py`](https://github.com/HSPK/usm/blob/main/tests/test_usm_azure.py)
(148 tests, 100% coverage of the shared layer). blobfuse2 is replaced by a
scripted fake, so mounting, health probing, credential rotation, the live
supervisor process and every error path — failed installs, busy unmounts,
stale endpoints, expired credentials, corrupt state — run without FUSE or
Azure. The listing is asserted to fit terminals from 70 to 200 columns and to
never print a token.
