# `usm disk`

Friendlier disk management: inspect block devices, then partition, format, and
mount them without memorising `lsblk` / `parted` / `mkfs` / `mount` flags.

```bash
usm disk                       # tree of disks + partitions (prettier lsblk)
usm disk info sdb              # everything about one disk/partition
usm disk usage                # mounted filesystems with usage bars
usm disk fstab                # parsed /etc/fstab

usm disk setup sdb            # raw disk -> GPT + ext4 + mounted at /mnt/sdb
usm disk partition sdb        # one whole-disk partition (GPT) and nothing else
usm disk format sdb1 -l data  # mkfs.ext4 with label 'data'
usm disk mount sdb1 /data --fstab   # mount now and persist in /etc/fstab
usm disk unmount /data --fstab      # umount and drop the fstab entry
usm disk wipe sdb --force     # erase all signatures
```

## Inspect

The bare command (and `ls`) prints a tree of every disk and partition — size,
type, filesystem, label, mountpoint, and usage % — built from `lsblk --json`.
Loop and CD-ROM devices are hidden unless you pass `-a/--all`.

`info <device>` shows a full report for one disk or partition (model, serial,
SSD/HDD, transport, partition-table type, UUID, usage). `usage` is a `df`-style
table of mounted block filesystems with usage bars, and `fstab` renders
`/etc/fstab` as a table. These four are **read-only** and need no privileges.

## Manage

`partition <disk>` wipes a disk and creates a single partition spanning the
whole thing (`--table gpt|mbr`, default GPT). `format <device>` runs the right
`mkfs` for `--fs` (default `ext4`; also `xfs`, `btrfs`, `vfat`, `fat32`, `ntfs`,
`exfat`, `ext2/3`) with an optional `--label`. `wipe <device>` clears all
filesystem/partition signatures with `wipefs`.

## Mount

`mount <device> [mountpoint]` mounts a formatted device, defaulting the
mountpoint to `/mnt/<label-or-name>` and creating it if needed; `--fstab`
persists the mount by UUID. `unmount <target>` (alias `umount`) accepts a
device **or** a mountpoint, supports `--lazy`/`--force`, and removes the
`/etc/fstab` entry with `--fstab`.

`setup <disk>` is the one-shot workflow: it partitions, formats, and mounts a
raw disk in a single command (`--fs`, `--mountpoint`, `--label`, `--fstab`) —
going from a blank disk to a ready, mounted filesystem.

## Safety

The destructive commands (`partition`, `format`, `wipe`, `setup`) need root and
prompt for confirmation (skip with `-y`). They **refuse** any device that backs
`/`, `/boot`, `/usr`, `/var`, or swap, and any device that is currently mounted
(unmount it first). A device that already holds partitions or a filesystem
needs `--force` to overwrite, so a fat-fingered device name can't silently wipe
a populated data disk.

## Source

[`scripts/disk.py`](https://github.com/HSPK/usm/blob/main/scripts/disk.py)
