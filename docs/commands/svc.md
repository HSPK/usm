# `usm svc`

Run any command as a managed service — started, kept alive, logged, and
optionally brought up at boot.

```bash
usm svc add web -- python -m http.server 8000   # define and start
usm svc ls                                      # what's running
usm svc status web                              # one service in detail
usm svc logs web -f                             # follow its output
usm svc restart web
usm svc enable web                              # also start it at boot
usm svc rm web                                  # stop, disable, forget
```

## How it runs

Each service is supervised by a small loop that spawns the command, captures
stdout and stderr into one rotating log, and restarts it according to its
policy:

| `--restart` | Behaviour |
| --- | --- |
| `always` (default) | Restart whatever the exit code |
| `on-failure` | Restart only on a non-zero exit |
| `never` | Run once |

A process that dies immediately is backed off exponentially (up to 5
minutes) so a broken command cannot spin. One that ran for a while before
exiting comes straight back.

`usm svc enable` points systemd (or launchd) at that same supervisor, so a
service behaves identically whether you started it by hand or the machine
did at boot.

## Options worth knowing

```bash
usm svc add job --cwd /srv/app --env KEY=value -- ./run.sh
usm svc add job --restart on-failure --restart-sec 30 -- ./flaky.sh
usm svc add job --no-start -- ./run.sh    # define without starting
usm svc ls --json                          # scriptable
```

Environment values are redacted in `status` output, so a token in `--env`
does not end up on someone's screen.

## Why

Tunnels, proxies, syncs and mounts each grew their own copy of "keep this
alive and start it at boot". This is that machinery on its own, pointed at
whatever you want — and it is now the same code those commands use.

## Source

[`scripts/svc.py`](https://github.com/HSPK/usm/blob/main/scripts/svc.py)
