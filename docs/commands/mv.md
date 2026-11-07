# `usm mv`

Move an Azure Blob file or directory by copying it with AzCopy, verifying the
copy's completion summary, and only then deleting the source. Both arguments
must be Azure Blob / DFS URLs or paths under active blobfuse2 mounts. Ordinary
local paths are rejected.

```bash
usm mv /mnt/data/old-name /mnt/data/new-name
usm mv --use-az \
  https://acct.blob.core.windows.net/data/old-name \
  https://acct.blob.core.windows.net/archive/new-name
usm mv --dry-run /mnt/data/old-name /mnt/archive/new-name
```

`DESTINATION` is the final path, not a parent directory: moving `old-name`
to `new-name` copies directory contents directly into `new-name`, not into
`new-name/old-name`. Existing destination directories are merged; unrelated
destination files are left untouched.

The command displays the resolved URLs without SAS tokens and asks for
confirmation, defaulting to no. It refuses container-wide moves, wildcard
paths, snapshot/version URLs, and equal or overlapping source/destination
paths, including Blob and DFS URLs for the same location.

By default conflicting files are not overwritten. If AzCopy skips any
transfers, fails, reports no completed transfers, or produces an invalid or
incomplete summary, the source is retained and the command fails. Use
`--overwrite` to explicitly allow replacement of conflicting destination files.
Successfully copied destination files are not rolled back on failure.

This is **not an atomic rename**. Keep source and destination unchanged
throughout the move, including the deletion phase. If source deletion fails,
the destination copy remains and some source files may already be deleted.

| Flag | Effect |
| --- | --- |
| `--use-az` | Authenticate through Azure CLI instead of generating SAS tokens. |
| `--dry-run` / `-d` | Preview both commands without confirmation, execution, SAS generation, or installation. |
| `--overwrite` | Allow replacing conflicting destination files. |

Authentication and automatic AzCopy installation reuse [`usm cp`](cp.md)
and [`usm rm`](rm.md). SAS generation happens only after confirmation.
