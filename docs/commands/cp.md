# `usm cp`

Copy files across local paths and blobfuse2 mountpoints, delegating to
`azcopy` whenever Azure Storage is on either side. Much faster than
copying through the FUSE filesystem.

```bash
usm cp [--use-sas-token] <source>... <destination>
```

## How it picks the transport

For each source/destination, `usm cp` checks whether the path lives
under a known blobfuse2 mountpoint (it inspects the active
`blobfuse2` processes to figure that out). If at least one side is
"Azure", it generates the equivalent `azcopy` URL and shells out to
`azcopy copy`. Otherwise it falls back to a regular `cp -r`.

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
| `--use-sas-token` / `-s` | Generate a fresh SAS token via `az` and use it in the azcopy URL instead of the cached one. |
| `--dry-run` / `-d` | Print the azcopy/cp command without executing. |

## Prerequisites

- A blobfuse2 mount created via [`usm blobmount`](blobmount.md) (or set up
  yourself with the same config layout).
- `azcopy` on PATH.
- `az` CLI logged in.

## Source

[`scripts/cp.py`](https://github.com/HSPK/usm/blob/main/scripts/cp.py).
Python script; uses `psutil` + `pyyaml` to read the mount metadata.
