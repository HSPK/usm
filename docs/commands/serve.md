# `usm serve`

Full-featured directory server — **uploads, drag-and-drop UI, range
requests, on-the-fly zip / tar.gz, QR codes, basic auth** — built on
[miniserve](https://github.com/svenstaro/miniserve) (a single Rust
binary). `usm` auto-installs the binary on first run, locally or on a
remote SSH host. No system packages, no sudo.

```bash
usm serve ./reports                       # serve the directory locally
usm serve ~/data -p 8000 --bind 0.0.0.0   # expose on the LAN
usm serve ~/data --no-upload              # read-only
usm serve ~/data -a alice:wonderland -q   # basic auth + terminal QR code

usm serve user@host:/srv/logs             # spawn miniserve on the remote
usm serve user@host:~/models -p 8080      # ssh -L brings it to localhost
usm serve ~/data --tunnel u@bastion       # push: serve locally, expose on bastion
```

## What it does

1. **Resolves a binary**: prefers `~/.cache/usm/bin/miniserve` (the
   usm-managed install), then `$PATH`, otherwise downloads the
   appropriate pinned release into `~/.cache/usm/bin/miniserve`
   (`chmod +x`). On the remote, same logic — miniserve lives under
   `~/.cache/usm/bin/miniserve` and is downloaded via `curl`, `wget`,
   or `python3` urllib (whichever the host has).
2. **Spawns miniserve**:
   - Local source — directly, on `--bind`:`--port`.
   - Remote source (`user@host:/path`) — over `ssh -L LPORT:127.0.0.1:RPORT`
     so the server stays on the remote's `127.0.0.1` and you get it on
     your laptop.
3. **Forwards Ctrl-C** to the whole tree (server + ssh) for clean
   shutdown.

## Defaults

| Option | Default | Why |
| --- | --- | --- |
| `--upload / --no-upload` | **upload** | Uploads (with mkdir + overwrite) are on; pass `--no-upload` for read-only. |
| `--delete` | off | Deletion is destructive — opt-in only. |
| `--hidden / --no-hidden` | **hidden** | Dotfiles are listed by default (it's almost always what you want when sharing your own files). |
| `--bind` | `127.0.0.1` | LAN exposure is explicit (`--bind 0.0.0.0`). |
| `--auth` | none | Set with `USER:PASS`; see miniserve docs for hashed form. |
| archives | on | Three download buttons (`.tar`, `.tar.gz`, `.zip`) at the top of every folder page; pass `--no-archive` to disable. |
| `--upgrade` (`-U`) | — | Force re-download of the miniserve binary on this run. |

## Local vs. remote — picking the right command

| | `usm share` | `usm serve` |
| --- | --- | --- |
| Engine | stdlib `http.server` | miniserve (Rust binary) |
| Extra install | none | ~2.3 MB binary, auto-downloaded |
| Range / resume | no | **yes** |
| Upload | no | **yes** (drag-drop UI) |
| Directory zip / tar.gz | no | **yes** |
| Browser UI | basic listing | dark-themed, sortable, searchable |
| Remote runtime | python3 / uv | self-contained binary |

Use **`share`** for the absolute minimal-dependency case (and when you
just want to push something to a teammate's box). Use **`serve`** for
serious browsing / large files / uploads / "give me the whole
folder".

## Security notes

- Default bind is `127.0.0.1`; you have to be explicit to expose on the
  LAN.
- Uploads default to **on** because that's the whole point of running
  this instead of `usm share`. If you're serving over a network you
  don't trust, add `--auth USER:PASS` (or pass `--no-upload`).
- Remote (`user@host:/path`) mode binds miniserve to the remote's
  `127.0.0.1` only; the ssh tunnel does the rest. No remote port is
  exposed publicly.

## Source

[`scripts/serve.py`](https://github.com/HSPK/usm/blob/main/scripts/serve.py)
