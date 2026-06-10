# `usm blobmount`

Mount an Azure Storage container as a local filesystem via
[`blobfuse2`](https://github.com/Azure/azure-storage-fuse).

```bash
usm blobmount <mount_dir> <account> <container>
```

## What happens

1. If `blobfuse2` isn't installed, it's pulled in via the Microsoft apt repo.
2. The current Azure CLI session (`az login`) is used to mint a short-lived
   SAS token for the container.
3. A `blobfuse2` mount is started in the background; the container appears
   as a normal directory at `<mount_dir>`.

## Example

```bash
az login
usm blobmount /mnt/data myaccount mycontainer
ls /mnt/data
```

## Prerequisites

- Azure CLI (`az`) and an active login with permission on the storage
  account
- A writable empty directory at `<mount_dir>` (it's created if missing)
- Ubuntu (the apt repo it pulls from is Ubuntu-only)

## Companion command

After mounting, use [`usm cp`](cp.md) to copy in/out of the mount; it
delegates to `azcopy` when one side is in the Azure namespace, which is
much faster than going through the FUSE filesystem.

## Source

[`scripts/blobmount.sh`](https://github.com/HSPK/usm/blob/main/scripts/blobmount.sh).
