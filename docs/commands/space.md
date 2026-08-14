# `usm space`

Find out what is eating the disk, and get it back.

```bash
usm space                  # usage summary + what's reclaimable
usm space top              # biggest things under .
usm space top /var -n 20   # biggest things under /var
usm space caches           # known caches and their sizes
usm space reclaim --dry-run
usm space reclaim --only pip --only uv --yes
```

`usm disk` partitions and mounts; this one is purely about space.

## Caches it knows

pip, uv, npm, yarn, cargo, Go build cache, HuggingFace hub, torch hub,
conda/mamba packages, apt archives, journald, Docker, usm's own per-script
virtualenvs, and the trash. Anything absent is simply skipped, and anything
needing root is reported rather than attempted.

## Deleting is opt-in

`reclaim` shows what it would remove and how much that frees, then asks.
`--yes` skips the prompt; `--dry-run` never deletes.

It refuses to touch anything outside a known cache location — `/`, `$HOME`
itself, `/etc` and friends are rejected outright, and a cache directory that
resolves outside its expected root (via a symlink or a redirected env var)
is refused too. Permission errors and files that vanish mid-walk are
reported without aborting the rest of the run.

## Source

[`scripts/space.py`](https://github.com/HSPK/usm/blob/main/scripts/space.py)
