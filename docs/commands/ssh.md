# `usm ssh`

A drop-in `ssh` front-end that reconnects on its own and — just as
importantly — puts your terminal back together afterwards.

```bash
usm ssh user@host                          # reconnect forever, repair the tty
usm ssh --retries 5 --retry-delay 3 host
usm ssh --tmux gpu-node                    # reattach a persistent remote tmux
usm ssh -p 2222 -L 8080:localhost:80 host  # unknown flags go straight to ssh
usm ssh host -- nvidia-smi                 # one-shot remote command
usm ssh --print-cmd --tmux host            # show the resolved ssh command
usm ssh --fix-terminal                     # un-wedge a tty some other ssh broke
```

## The two problems it solves

**Dropped links.** Flaky wifi, a laptop suspend, or a VPN flap kills `ssh`
outright. `usm ssh` supervises the child process and reconnects with
exponential backoff, giving up only when retrying cannot help.

**Mouse garbage after a drop.** A remote `tmux`/`vim`/`htop` switches mouse
reporting, the alternate screen, and bracketed paste on *your* terminal. When
`ssh` dies hard it never gets to switch them back off, so every click starts
printing things like `[<35;80;20M`. After every attempt `usm ssh` restores the
saved `termios` state and emits the matching reset sequences. It never sends a
hard reset, so your scrollback survives.

Terminal already wedged by some other tool? `usm ssh --fix-terminal` applies a
`stty sane`-style repair to the controlling terminal (line editing, echo and
signals back on) plus the escape burst, and exits.

!!! note "Getting out"

    `~.` (OpenSSH's disconnect escape) is recognised and exits for good rather
    than reconnecting. `Ctrl-C` does not reach the wrapper while a session is
    up — `ssh` holds the terminal in raw mode — but it does during the wait
    between reconnects. OpenSSH's `~^Z` suspend escape is not supported.

## Suspend-aware reconnects

`time.monotonic()` freezes while the host sleeps; the boot/wall clock does not.
A gap between the two means you just woke up — and the TCP session did not. So
instead of waiting out the keepalive timeout with a frozen screen, `usm ssh`
drops the stale connection and reconnects immediately.

```bash
usm ssh --sleep-threshold 60 host     # only gaps over a minute count
usm ssh --no-sleep-detect host        # opt out entirely
```

## When it retries — and when it refuses to

| Situation | Behaviour |
| --- | --- |
| Clean logout (`exit 0`) | Stop. You are never fought over leaving. |
| Remote command's own non-zero status | Stop, status passed through. |
| `Ctrl-C` at a password prompt | Stop, exits `130`. |
| `ssh` exits 255 after a real session | Reconnect. |
| Resumed from suspend | Reconnect immediately. |
| Refused / timed out / DNS failure | Reconnect — these all come back when the network does. |
| Auth failure, host-key mismatch, bad option | Stop — retrying cannot help. |
| Never connected once, after `--fail-fast` attempts | Stop — most likely a typo or a wrong port. |

The fatal cases are recognised by reading `ssh`'s stderr (which is still
forwarded verbatim). Password and passphrase prompts go to `/dev/tty`, so
interactive logins are unaffected. Use `--no-classify` to switch it off.

Note the asymmetry in that last row. `--fail-fast` only applies while the
host has **never** answered during this invocation — that is the case where
retrying is probably pointless. As soon as one session has worked, `usm ssh`
assumes you want a resilient link and keeps reconnecting (with backoff, up to
`--retries`) for as long as the host stays away, so a reboot or a long tunnel
outage is ridden out rather than abandoned.

Whether a session counts as "connected" is decided by `ssh`'s stderr first and
elapsed time (`--min-uptime`) second — an unreachable host burns the whole
`ConnectTimeout` before failing, so duration alone would be misleading.

Exit code 255 is ambiguous — it can be `ssh` failing *or* a remote command's
own status. So when you run a one-shot command (`usm ssh host -- deploy.sh`),
it is reconnected only if `ssh` demonstrably never got the session up; it is
never blindly rerun, because it may already have had side effects. Pass
`--retry-command` if reruns are safe.

Tuning knobs: `--retries N` (`-1` unlimited, `0` disables reconnecting),
`--retry-delay`, `--max-delay`, `--min-uptime`, `--fail-fast`.

## Surviving the reconnect

Reconnecting hands you a *fresh* shell — whatever was running is gone. For work
that must outlive the link, put it in a remote multiplexer:

```bash
usm ssh --tmux host                   # tmux new-session -A -D -s usm
usm ssh --tmux --session build host   # name the session
usm ssh --screen host                 # GNU screen instead
```

Each reconnect reattaches the same session and evicts the stale client the drop
left behind. This is off by default so `usm ssh` stays a plain `ssh`.

## Injected ssh options

Unless you pass `--no-keepalive`, these are added so a half-dead link surfaces
in ~45s instead of hanging:

```
-o ServerAliveInterval=15 -o ServerAliveCountMax=3
-o TCPKeepAlive=yes -o ConnectTimeout=10
-o ExitOnForwardFailure=yes      # only when -L/-R/-D is present
```

They are only added when you have not set them yourself. `usm ssh` asks `ssh`
what it would actually use (`ssh -G <destination>`, which evaluates `Host` and
`Match` blocks without connecting) and leaves any keyword you configured alone
— so a `ConnectTimeout 60` for a slow bastion in `~/.ssh/config` survives.
Command-line `-o` wins too, since the injected copies are appended after your
arguments and `ssh` keeps the first value it sees.

Nothing security-related (host-key policy, ciphers, auth) is ever overridden.

## Argument handling

```
usm ssh [wrapper options] [ssh options] destination [command ...]
```

Wrapper options are long-form only (`--retries`, `--tmux`, …) and must come
**before** the ssh arguments; everything else is forwarded to `ssh` untouched,
including short flags like `-p`, `-i`, `-L`, `-J` and `-v`, in both spaced
(`-p 2222`) and glued (`-p2222`) form. The wrapper deliberately registers no
short options of its own — not even `-h` — so nothing can be mistaken for one
inside an ssh argument. Injected `-o` options are placed before the
destination so `ssh` still reads them as options.

`--print-cmd` shows the fully resolved command without running it.

If you alias this to plain `ssh` (`usm install ssh ssh`), the wrapper detects
the usm shim on `PATH` and skips it when locating the real `ssh` binary, so
there is no recursion.

## Source

[`scripts/ssh.py`](https://github.com/HSPK/usm/blob/main/scripts/ssh.py)
