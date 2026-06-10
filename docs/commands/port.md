# `usm port`

Show what's listening on which port; free a port by killing whoever owns it.

```bash
usm port             # every LISTEN socket
usm port 8080        # who's on 8080
usm port ls          # explicit form of the bare command
usm port kill 8080   # SIGTERM the holder, then SIGKILL after 3s if still up
usm port kill 8080 --force   # SIGKILL immediately
```

## Output

`psutil.net_connections()` for `LISTEN` sockets, joined with `psutil.Process`
for command line + user. Without root you may see `-` in the PID/USER/COMMAND
columns for sockets owned by other users — run with `sudo` for full visibility.

## Why

Two daily-life cases:

1. **"What's already on port 8080?"** Trying to start `usm tunnel local 8080:...`
   and the bind fails — `usm port 8080` answers immediately.
2. **"Just free that port."** `usm port kill 8080` for the impatient.

## Source

[`scripts/port.py`](https://github.com/HSPK/usm/blob/main/scripts/port.py)
