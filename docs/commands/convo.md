# `usm convo`

Pack and restore the sessions your coding agents keep on disk — Copilot CLI,
Codex, and Claude Code — so a machine rebuild or a move to another box does
not cost you your history.

```bash
usm convo ls                        # what's on this machine
usm convo pack                      # everything → ./usm-convo-<host>-<date>.tar.gz
usm convo pack --tool copilot -o /backup/copilot.tar.gz
usm convo info archive.tar.gz       # what's inside, without unpacking
usm convo restore archive.tar.gz --dry-run
usm convo restore archive.tar.gz --merge
```

It is named `convo` rather than `session` because [`usm session`](session.md)
already means logged-in users.

## What it packs

| Agent | State |
| --- | --- |
| `copilot` | `~/.copilot` |
| `codex` | `~/.codex` |
| `claude` | `~/.claude` |

The whole state directory is packed rather than a hand-picked list of session
folders: these tools move quickly, and a layout guess that goes stale silently
drops history. Logs, caches, temp directories and `node_modules` are left out
by default (`--include-logs`, `--include-junk` to keep them).

## Three things that make this harder than `tar czf`

**The sessions are live SQLite.** Copilot and Codex store session state in
SQLite with write-ahead logging, so a plain copy of the `.db` can catch a
write half-done, and recent history may live in the `-wal` rather than the
database file. Each database is snapshotted through SQLite's own backup API,
which is consistent even while the agent is running, and the `-wal`/`-shm`
files are deliberately not archived — they are only meaningful beside the
exact database they came from.

**Not everything is a file.** Codex leaves a unix socket in `~/.codex/ipc`.
Sockets, fifos, devices and symlinks are skipped and counted rather than
archived as something that cannot come back.

**Restoring is untrusted input.** An archive member named
`../../.ssh/authorized_keys` would turn "restore my sessions" into something
worse. Members are refused unless they resolve inside the destination, and
link and device members are refused outright.

## Compression

Sessions are mostly JSONL and compress well. Measured on a 39 MiB
`events.jsonl`:

| `--compress` | Result | Speed |
| --- | --- | --- |
| `gz` (default) | 5.1x smaller | instant |
| `xz` | 8.7x smaller | ~30x slower |
| `bz2`, `none` | | |

## Restoring

`restore` asks before it writes, and keeps existing files unless you pass
`--merge`. `--dry-run` reports what would happen, `--into DIR` extracts
somewhere else entirely so you can look before committing, and `--tool` picks
one agent out of an archive containing several.

## Credentials

Files whose names look like credentials (`auth.json`, `*token*`, `*.key`,
`*.pem`) are packed by default — this is your own data going to your own
machine — but they are counted, and the summary says so. `--exclude-secrets`
leaves them out.

## Source

[`scripts/convo.py`](https://github.com/HSPK/usm/blob/main/scripts/convo.py)
