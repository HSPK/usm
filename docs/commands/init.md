# `usm init`

Bootstrap a fresh Ubuntu machine with the packages, shell aliases,
`uv` tools, tmux plugins, and Neovim config the project author uses.

```bash
usm init
```

## What it touches

- System packages via `apt` (build tools, git, tmux, neovim, htop, …)
- A `~/.tmux.conf` + Tmux Plugin Manager
- A Neovim config (LazyVim-style) under `~/.config/nvim`
- A managed alias block in `~/.bashrc` (uses the same start/end markers as
  [`inject-alias`](inject-alias.md))
- `uv` and a handful of `uv tool install`s

## Caveats

- Intended for **Ubuntu**. Probably works on Debian; doesn't on RHEL or macOS.
- It assumes `sudo` works for the current user.
- Re-running is safe — the alias block is replaced in place, `apt` is
  idempotent, the tmux config is overwritten.

## When to use it

On a fresh VM, container, or new laptop, before you start anything else. It
exists so the author doesn't have to think about "what did I install last
time?".

## Source

[`scripts/init.sh`](https://github.com/HSPK/usm/blob/main/scripts/init.sh).
Edit it, send a PR if it's useful to you too.
