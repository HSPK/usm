# `usm doctor`

One command that answers "is this machine healthy?" — the thing to run first
when a remote box is misbehaving.

```bash
usm doctor                 # the full report
usm doctor ls              # what it can check
usm doctor --only disk --only gpu
usm doctor --category hardware
usm doctor --json          # machine-readable
usm doctor --strict        # warnings are failures too
```

Exit code is non-zero when any check fails, so it drops straight into a
health probe or a CI step.

## What it checks

| Check | Looks for |
| --- | --- |
| `disk` | Filesystem usage and inode exhaustion |
| `memory` | Available memory, swap pressure, recent OOM kills |
| `load` | Load average against CPU count |
| `reboot` | A pending reboot |
| `clock` | NTP synchronisation |
| `dns` | Configured resolvers (and resolution, with `--online`) |
| `network` | Default route and interface state |
| `gpu` | Driver/CUDA versions, temperature, ECC errors |
| `mounts` | Stale or broken mounts (ENOTCONN / EACCES) |
| `services` | Failed systemd units |
| `usm` | Cache size, per-script venvs, catalog/file drift |
| `python` | Interpreter, externally-managed marker, PATH |

Anything a machine cannot answer is reported as *skipped*, not failed: no
GPU, no systemd, no `timedatectl` are all normal.

## Offline by default

No check touches the network unless you pass `--online`. That keeps the
report fast and safe to run on an isolated host; DNS and clock skew then
report configuration rather than live results.

Every check is bounded by `--timeout`, so a dead NFS mount or a wedged
`nvidia-smi` cannot hang the report.

## Source

[`scripts/doctor.py`](https://github.com/HSPK/usm/blob/main/scripts/doctor.py)
