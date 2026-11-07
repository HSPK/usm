# `usm rm`

Delete Azure Blob files or directories using AzCopy, without deleting through
the FUSE filesystem. Accepts Azure Blob / DFS URLs and paths under active
blobfuse2 mounts, using the same mount detection, SAS authentication, and
AzCopy installation helpers as [`usm cp`](cp.md). Ordinary local paths are
rejected.

```bash
usm rm /mnt/data/old-file.txt
usm rm /mnt/data/old-directory
usm rm --use-az https://acct.blob.core.windows.net/data/old-directory
usm rm --dry-run /mnt/data/old-directory
```

All targets are resolved before any deletion. The command displays the
resolved Azure URLs (without SAS tokens) and asks `Delete these targets? [y/N]`.
Only an explicit yes proceeds; no, an empty answer, or end-of-input aborts.
Directories are deleted recursively, including all their contents. Multiple
targets share one confirmation.

By default, SAS tokens are generated via the logged-in `az` CLI after
confirmation; existing SAS URLs are preserved. `--use-az` lets AzCopy
authenticate through Azure CLI instead. AzCopy is installed automatically
when needed, or explicitly with `usm cp --install`.

| Flag | Effect |
| --- | --- |
| `--use-az` | Use Azure CLI authentication instead of generating SAS tokens. |
| `--dry-run` / `-d` | Print commands without confirmation, deletion, SAS generation, or AzCopy installation. |

Dry-run commands do not include freshly generated SAS tokens. AzCopy failures
stop subsequent deletions and return its exit code; previously completed
deletions are not rolled back.
