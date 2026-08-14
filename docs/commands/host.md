# `usm host`

An SSH inventory for people who work across many machines, plus the ability
to run one command on all of them.

```bash
usm host add gpu1 ubuntu@10.0.0.4 --identity ~/.ssh/lab --tag gpu
usm host ls
usm host connect gpu1
usm host exec --tag gpu -- nvidia-smi --query-gpu=name --format=csv
usm host check                      # who is reachable
usm host show gpu1
usm host rm gpu1
```

## It edits `~/.ssh/config`, carefully

Managed entries live inside a marker-fenced block, the same convention
`usm inject-alias` uses for shell rc files. Everything outside that block is
left byte-for-byte alone, re-running updates the block instead of
duplicating it, and writes are atomic with the file mode preserved. A
missing or duplicated end marker is refused rather than guessed at, leaving
the original untouched.

Because the entries are ordinary `ssh_config` stanzas, `ssh`, `scp`, `rsync`
and anything else that reads that file pick them up for free.

## Fan-out

`usm host exec` runs across hosts concurrently (`--parallel`, default 8) and
reports a per-host result table with exit codes and durations, followed by
the output. `--fail-fast` stops at the first failure; `--raw` prints full
output instead of truncating; `--json` makes it scriptable. The exit code
reflects whether every host succeeded.

## Source

[`scripts/host.py`](https://github.com/HSPK/usm/blob/main/scripts/host.py)
