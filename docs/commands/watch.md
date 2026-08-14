# `usm watch`

Re-run a command whenever files change.

```bash
usm watch -- pytest -q                  # watch . and re-run tests
usm watch src tests -- pytest -q        # watch only these paths
usm watch --ext py,toml -- ruff check   # only these file types
usm watch --clear --initial -- make     # clear the screen, run once now
usm watch --settle 2 -- ./deploy.sh     # wait for edits to stop first
```

Everything after `--` is the command, taken verbatim — its own flags are
never parsed by `usm watch`.

## Debouncing

A burst of writes is one run, not a hundred. Saving a file, a formatter
rewriting a tree, or `git checkout` swapping a branch all produce many
events; `usm watch` waits for `--settle` seconds of quiet before running,
and extends that window for as long as changes keep arriving.

Changes that happen *during* a run are remembered and collapse into a single
re-run once it finishes, so you never end up with a queue of stale runs.

## Backends

inotify (via `watchdog`) when it is available, falling back to polling for
network filesystems, blobfuse mounts, and hosts that have run out of inotify
watches. Force one with `--watch-mode inotify|poll`.

`.git`, `__pycache__`, `node_modules`, `.venv` and similar are ignored by
default; add your own with `-x/--exclude`, or start from nothing with
`--no-default-excludes`.

## Source

[`scripts/watch.py`](https://github.com/HSPK/usm/blob/main/scripts/watch.py)
