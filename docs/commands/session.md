# `usm session`

Inspect and manage logged-in user sessions on this host.

```bash
usm session              # w-style dashboard of current logins
usm session ssh          # only SSH sessions
usm session mux          # tmux / screen sessions
usm session history kim  # recent logins for user 'kim' (last)
usm session history --failed   # failed attempts (lastb; needs root)
usm session me           # details about your own session
usm session watch        # live-updating dashboard

usm session kill pts/3   # end one session by TTY
usm session logout kim   # end all of a user's sessions
usm session msg all "rebooting in 5m"   # wall broadcast
usm session lock kim     # passwd -l (needs root); unlock with `unlock`
```

## Inspect

The bare command (and `ls`) prints one row per login: user, TTY, type
(`ssh` / `tmux` / `tty`), remote address, login time, idle, foreground PID,
and the active command. Data comes from utmp via `psutil.users()`, enriched
with the TTY's idle time and the most-recently-started process on that TTY.
Your own session is marked `➤`.

`ssh` filters to remote logins; `mux` lists `tmux ls` / `screen -ls`;
`history` shells out to `last` (or `lastb` with `--failed`).

## Manage

`kill <tty>` ends a single session — via `loginctl terminate-session` when
available, otherwise `SIGHUP` (then `SIGKILL`) to the processes on that TTY.
It refuses to kill your own session unless you pass `--force`. `logout <user>`
uses `loginctl terminate-user` (falling back to signals). `msg` uses
`wall`/`write`; `lock`/`unlock` wrap `passwd -l/-u`.

Acting on **other users** needs privileges — if a command reports permission
denied, rerun it under `sudo`. Destructive actions prompt for confirmation
(skip with `-y`).

## Source

[`scripts/session.py`](https://github.com/HSPK/usm/blob/main/scripts/session.py)
