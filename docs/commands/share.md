# `usm share`

Quick one-shot file/directory sharing over HTTP. Optionally piggybacks on
SSH reverse-tunneling so the URL is reachable from outside.

```bash
usm share ./build.tar.gz                       # localhost only
usm share ./reports --port 8000 --bind 0.0.0.0 # LAN
usm share file.zip --tunnel user@bastion       # also expose on bastion
usm share dir --tunnel user@host:9000          # pin the remote port
```

## What it does

1. Starts `http.server.SimpleHTTPRequestHandler` rooted at the file's parent
   directory (or the directory itself if you point at a dir). Random free
   port unless you pass `--port`.
2. Prints the local URL — and the SHA-256 of the file if it's < 50 MiB.
3. If `--tunnel <ssh-target>` is given, additionally spawns
   `ssh -R RPORT:localhost:LPORT <ssh-target>` so anyone with shell access
   on that host can `curl http://localhost:RPORT/...`. Pass `host:port` to
   pin the remote port, or just `host` to pick one at random.
4. Ctrl-C cleans up both the HTTP server and the tunnel.

## When to use it

- "Hey, can you grab this build artifact from my VM right now?"
- Sharing a log/report inside a small dev team that already has SSH access
  to a common bastion.

## When NOT to use it

- Public-internet file distribution. There's no auth, no rate limiting, no
  HTTPS termination. Use a real file host.
- Large files / long-lived shares. This is for the "RIGHT NOW" case; for
  anything persistent, push to blob storage and use a SAS URL.

## Remote bind warning

By default, `ssh -R` binds on the remote host's `localhost` only —
external machines can't reach it. To bind on `0.0.0.0` you'd need
`GatewayPorts=yes` (or `clientspecified`) in the bastion's `sshd_config`.
`usm share` doesn't try to be clever here — if you want public exposure,
configure the bastion's sshd explicitly. Most of the time, sshing **into**
the bastion and `curl localhost:RPORT/file` is exactly what you want.

## Source

[`scripts/share.py`](https://github.com/HSPK/usm/blob/main/scripts/share.py)
