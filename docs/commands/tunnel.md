# `usm tunnel`

A friendlier wrapper around `ssh -L / -R / -D` with persistent state, short
ids, and optional autostart via launchd on macOS or systemd on Linux.

## Why

The raw flags are fiddly:

- `-L` vs `-R` direction is hard to remember.
- The 4-part `bind:port:host:port` spec is verbose for the common case.
- Background management (`-f -N`) leaves you to track PIDs yourself.
- No story for "make this come back after a reboot".

`usm tunnel` keeps the simple cases short, hands the supervision problem
to the OS service manager, and makes inspection (`ls`, `show`, `logs`) trivial.

## Subcommands

| Command | What it does |
| --- | --- |
| `local SPEC SSH_TARGET` | Start an `ssh -L` tunnel (detached). |
| `remote SPEC SSH_TARGET` | Start an `ssh -R` tunnel (detached). |
| `socks SPEC SSH_TARGET` | Start an `ssh -D` SOCKS5 proxy. |
| `ls [--prune]` | List all tunnels with route / PID / uptime / status / boot flag. |
| `start <id>` | Relaunch a stopped tunnel (or service manager start if enabled). |
| `stop <id\|all>` | Stop a tunnel but **keep the definition** for later. |
| `restart <id>` | `stop` + `start` (or service manager restart if enabled). |
| `rm <id\|all>` | Delete the definition (also disables autostart if enabled). |
| `enable <id>` | Install a launchd/systemd user service and start it. |
| `disable <id>` | Remove the service (keeps the definition). |
| `show <id>` | Dump the JSON definition + resolved ssh argv. |
| `logs <id> [-n N]` | Print the tail of the per-tunnel log. |

## Spec shortcuts

You almost never have to type the full `bind:port:host:port` form.

### `local` / `remote`

| Shape | Meaning |
| --- | --- |
| `PORT` | Same port on both ends; target host = `localhost`. |
| `LPORT:RPORT` | Different ports; target host = `localhost`. |
| `LPORT:RHOST:RPORT` | Forward through to a third host (resolved by the SSH peer). |
| `BIND:LPORT:RHOST:RPORT` | Full form; pick a non-loopback bind. |

For `remote` the roles flip: the first port is the one opened on the SSH
server, the second is reached from your machine.

### `socks`

| Shape | Meaning |
| --- | --- |
| `PORT` | Listen on `127.0.0.1:PORT`. |
| `BIND:PORT` | Listen on `BIND:PORT`. |

## Common recipes

=== "Reach a private database"

    ```bash
    usm tunnel local 5432:db.internal:5432 user@bastion
    psql -h 127.0.0.1 -p 5432 ...
    ```

=== "Expose local dev to a public server"

    ```bash
    usm tunnel remote 9000:3000 user@server
    # Now server:9000 -> your-machine:3000
    ```

=== "Browse as if from the server"

    ```bash
    usm tunnel socks 1080 user@home-server
    curl --socks5-hostname 127.0.0.1:1080 https://internal-wiki/
    ```

=== "Through a jump host"

    ```bash
    usm tunnel local 5432:db:5432 user@final -J jump@gateway
    ```

## Identity, port, jump host, extra options

Every subcommand accepts the same set of plumbing flags:

| Flag | Maps to |
| --- | --- |
| `-i PATH`, `--identity PATH` | `ssh -i` |
| `-p N`, `--ssh-port N` | `ssh -p` |
| `-J HOST`, `--jumphost HOST` | `ssh -J` |
| `-o KEY=VALUE` (repeatable) | `ssh -o` |
| `--name NAME` | Pick a custom id instead of the next free integer. |

## Tunnel ids

`local` / `remote` / `socks` assign the next free non-negative integer as
the id (so `ls` shows `0`, `1`, `2`, …). Use that id everywhere:

```bash
usm tunnel ls
usm tunnel stop 1
usm tunnel logs 1 -n 100
usm tunnel rm all
```

Use `--name foo` if you want a memorable id (e.g. `--name prod-db`).
String ids still work as command targets; only the *default* changed.

## Sensible ssh defaults

Every tunnel runs with:

```
-N -T
-o ExitOnForwardFailure=yes
-o ServerAliveInterval=30
-o ServerAliveCountMax=3
-o StrictHostKeyChecking=accept-new
```

That gives roughly 90-second dead-peer detection without you having to
remember any of it. `accept-new` means first connection to a fresh host
is allowed; subsequent key changes still fail loudly.

Standalone tunnels are supervised internally by default. If `ssh` exits after
startup because the network drops, the remote closes the session, or a keepalive
check fails, usm waits 5 seconds and starts the same tunnel again. Immediate
startup failures still fail loudly so bad ports, auth, or host keys do not loop
forever. The supervisor is detached from the terminal, so closing the terminal
after `usm tunnel local` / `remote` / `socks` does not stop the tunnel; use
`usm tunnel stop <id>` when you want to close it.

## State files

Each tunnel is a JSON file under `~/.cache/usm/tunnels/<id>.json`. `stop`
clears the PID and timestamp but **keeps the file**, so `start <id>` can
relaunch with the same parameters. `rm` deletes both the file and the log.

Logs live under `~/.cache/usm/tunnels/logs/<id>.log` — appended to on every
restart, so you can debug across reconnects.

`show <id>` dumps everything:

```bash
$ usm tunnel show 1
{
  "id": "1",
  "kind": "local",
  "bind_addr": "127.0.0.1",
  "listen_port": 8080,
  "ssh_target": "user@bastion",
  ...
  "alive": true,
  "enabled": false,
  "argv": ["ssh", "-N", "-T", ...]
}
```

## Autostart at login / boot

On macOS, `usm tunnel enable <id>` writes
`~/Library/LaunchAgents/com.github.hspk.usm.tunnel.<id>.plist` and loads it
with `launchctl bootstrap`. The LaunchAgent:

- runs `usm tunnel up <id>` in the foreground
- starts at login (`RunAtLoad=true`)
- restarts when `ssh` exits (`KeepAlive=true`, `ThrottleInterval=5`)
- writes stdout/stderr to the tunnel log path

On Linux, `usm tunnel enable <id>` writes
`~/.config/systemd/user/usm-tunnel-<id>.service`, runs `daemon-reload`,
then `systemctl --user enable --now`. The unit:

- has `Type=simple` with `ExecStart=usm tunnel up <id>`
- sets a PATH including `~/.local/bin`, `~/.cargo/bin`, `/usr/local/bin` so
  `uv` is discoverable inside the user-systemd environment
- restarts when `ssh` exits (`Restart=always`, `RestartSec=5`)
- starts after `network-online.target`

After enabling, `usm tunnel ls` reflects service-manager state — `stop`,
`start`, and `restart` route through launchd/systemd automatically.

!!! warning "Linger for actual boot-time start"
    By default, user units only start when you log in. To have them start
    at boot (before SSH login), enable linger once per user:

    ```bash
    sudo loginctl enable-linger "$USER"
    ```

    `usm tunnel enable` will print this hint when linger isn't on yet.

### Why not `autossh`?

Earlier versions had an `--autossh` flag. It was removed in
[v0.3.0](https://github.com/HSPK/usm/releases/tag/v0.3.0): usm now handles
supervision itself in standalone mode, and launchd/systemd handles it for
enabled tunnels. Plain `ssh` with the defaults above detects dead peers in time
for the supervisor to reconnect. If you still prefer `autossh`, you can wrap
manually:

```bash
autossh -M 0 -N -L 8080:db:5432 user@bastion
```

## Removing a tunnel cleanly

```bash
usm tunnel rm 1         # stops + disables autostart if needed + deletes state
usm tunnel rm all       # same for every defined tunnel
```

If you ever uninstall `usm`, run `rm all` first so launchd/systemd doesn't keep
trying to call a binary that no longer exists.
