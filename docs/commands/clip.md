# `usm clip`

Cross-platform clipboard from stdin, with OSC 52 fallback so it works
through SSH.

```bash
echo "hello" | usm clip
cat README.md | usm clip
usm clip paste                    # only locally; OSC 52 is write-only
ssh remote 'usm clip --osc52 < /var/log/app.log'   # lands in YOUR clipboard
```

## How it picks the backend

In order, the first available wins:

| OS / env | Tool |
| --- | --- |
| macOS | `pbcopy` |
| Windows | `clip` |
| Linux + Wayland | `wl-copy` |
| Linux + X11 | `xclip` then `xsel` |
| WSL | `clip.exe` |
| anywhere | **OSC 52** escape to `/dev/tty` (or stderr) |

Use `--osc52` to force the escape sequence — useful inside SSH when the
remote machine has no clipboard tool of its own. The OSC 52 sequence is
interpreted by your local terminal emulator (modern Alacritty, Kitty,
WezTerm, iTerm2, Windows Terminal, tmux ≥ 3.3 with `set -g set-clipboard on`).

`--trim`/`--no-trim` toggles whether a single trailing newline is stripped
(default: trimmed).

`usm clip paste` only works with a local backend; OSC 52 is one-way.

## Source

[`scripts/clip.py`](https://github.com/HSPK/usm/blob/main/scripts/clip.py)
