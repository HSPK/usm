# `usm cp`

Copy files across local paths, blobfuse2 mountpoints, and `https://` Azure
Blob URLs, delegating to `azcopy` whenever Azure Storage is on either side.
Much faster than copying through the FUSE filesystem.

```bash
usm cp [--use-az] <source>... <destination>
usm cp --install
```

## How it picks the transport

For each source/destination, `usm cp` checks whether the path is an Azure
blob — either an `https://<account>.blob.core.windows.net/...` URL or a path
under a known blobfuse2 mountpoint (it inspects the active `blobfuse2`
processes to figure that out). If at least one side is "Azure", it generates
the equivalent `azcopy` URL and shells out to `azcopy copy`. Otherwise it
falls back to a regular `cp -r`.

When a blob path is involved, `usm cp` makes sure `azcopy` is available,
auto-installing it on first use if it is not found on `PATH` or in the
usm-managed location (`~/.cache/usm/bin/azcopy`).

## `https://` blob URLs

An `https://` blob address is used as-is — the account/container are not
re-derived to build the URL. By default a fresh SAS token is generated via
`az` (parsing the account and container out of the URL only for that) and
appended when the URL has none. URLs that already carry a `sig=` SAS token are
left untouched. Pass `--use-az` to skip SAS generation entirely and let
`azcopy` authenticate via your Azure CLI login instead.

```bash
# Local → https blob (SAS generated on the fly)
usm cp ./build https://acct.blob.core.windows.net/release/2025-06/

# https blob → local (URL already has a SAS token; used verbatim)
usm cp "https://acct.blob.core.windows.net/data/raw?sv=...&sig=..." ./inbox
```

## Installing azcopy

```bash
# Download the latest azcopy for this OS/arch into ~/.cache/usm/bin
usm cp --install
```

Builds are mapped for Linux, macOS, and Windows (amd64/arm64). Set
`$USM_AZCOPY_BIN` to point at an existing binary to bypass the managed copy.

## Examples

```bash
# Local copy (just shells out to cp -r)
usm cp ./project ./project-backup

# Local → blob (uses azcopy)
usm cp ./build /mnt/data/release-2025-06/

# Blob → local
usm cp /mnt/data/raw ./inbox

# Blob → blob (across containers)
usm cp /mnt/data/foo /mnt/archive/foo
```

## Flags

| Flag | Effect |
| --- | --- |
| `--use-az` | Authenticate `azcopy` via Azure CLI login instead of generating SAS tokens. |
| `--dry-run` / `-d` | Print the azcopy/cp command without executing. |
| `--install` | Download and install the latest `azcopy` (linux/mac/windows), then exit. |

## Prerequisites

- A blobfuse2 mount created via [`usm blobmount`](blobmount.md) (or set up
  yourself with the same config layout), or an `https://` blob URL.
- `azcopy` — auto-installed on demand, or install it explicitly with
  `usm cp --install` (override with `$USM_AZCOPY_BIN`).
- `az` CLI logged in (used to generate SAS tokens by default, or as the
  azcopy credential when `--use-az` is set).

## Source

[`scripts/cp.py`](https://github.com/HSPK/usm/blob/main/scripts/cp.py).
Python script; uses `psutil` + `pyyaml` to read the mount metadata.
