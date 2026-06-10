# `usm wait`

Block until one or more endpoints are reachable. AND semantics: all targets
must come up before the command exits 0.

```bash
usm wait db:5432
usm wait http://api.local/health redis:6379
usm wait https://example.com/ -t 60 -i 2
usm wait tcp://db:5432 tcp://cache:11211
```

## Target forms

| Form | Probe |
| --- | --- |
| `host:port` | TCP connect |
| `tcp://host:port` | TCP connect (same) |
| `http://...` / `https://...` | HTTP GET; any status < 500 counts as up (so 200, 401, 404 all "up") |

## Flags

| Flag | Default | Effect |
| --- | --- | --- |
| `-t`, `--timeout` | 60 | Total seconds before giving up. |
| `-i`, `--interval` | 1.0 | Seconds between retries per target. |
| `--connect-timeout` | 3.0 | Per-attempt connect timeout. |
| `-q`, `--quiet` | — | Suppress per-target success lines. |

Each target is probed in its own thread; total wall time ≈ slowest target,
not the sum.

## Composes well

```bash
usm wait db:5432 && usm tunnel local 5432:db:5432 user@bastion
usm wait https://api/ http://internal:9000/ridiculous-name/healthz
```

## Source

[`scripts/wait.py`](https://github.com/HSPK/usm/blob/main/scripts/wait.py)
