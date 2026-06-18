# `usm init`

Bootstrap a machine with modern dev tools — across **macOS, Linux, and
Windows**. It's a small, config-driven engine: a YAML file declares *groups*
of *items*, and each item's install command per platform. `init` detects the
OS, picks the matching command, skips anything already installed, and runs the
rest.

```bash
usm init                     # install the default groups
usm init cli lang            # only these groups
usm init -i                  # interactively choose groups
usm init --dry-run           # show the plan, install nothing
usm init --list              # list groups and tools
usm init --export-config     # write the default config to ~/.config/usm/init.yaml
```

By default (no flags) it installs everything in `default_groups` non-interactively.

## Default tools

| Group | Tools |
| --- | --- |
| `lang` | `uv`, `fnm` |
| `cli` | `gh`, `ripgrep`, `fd`, `bat`, `eza`, `fzf`, `zoxide`, `starship`, `btop` |
| `editor` | `neovim` + `~/.config/nvim/init.vim` |
| `profile` | shell alias block (delegates to [`inject-alias`](inject-alias.md)) |
| `uv-tools` | `azure-cli`, `nvitop`, `amlt` (via `uv tool install`) |
| `tmux` | `tmux` + Tmux Plugin Manager (POSIX only; skipped on Windows) |

`linux-extras` (build deps, Tailscale, `dua-cli`) is **Linux-only** and **not**
in the defaults — run it explicitly with `usm init linux-extras`.

## Per-platform install

Each tool maps to one command per OS, run directly (no package-manager
abstraction). macOS uses Homebrew, Linux uses apt + official installers,
Windows uses winget.

```yaml
items:
  ripgrep:
    check: rg                                   # `which rg` hit → skip
    macos:   brew install ripgrep
    linux:   sudo apt-get update && sudo apt-get install -y ripgrep
    windows: winget install -e --id BurntSushi.ripgrep.MSVC
```

- `check` is the binary used for the idempotency test; if it's on `PATH` the
  step is skipped.
- An `all:` key is used as a fallback for platforms without a specific command
  (e.g. the `uv tool install` recipes are identical everywhere).

## Configuration

The default config is embedded in the script, so `usm init` works with zero
setup. To customize, layer your own on top — later layers win:

1. embedded defaults
2. `~/.config/usm/init.yaml` (skip with `--no-user-config`)
3. `--config <path>`

Merging is deep, so you can override just one platform of one tool, add a new
tool, or change `default_groups` without restating everything.

```bash
usm init --export-config       # write the defaults somewhere editable
$EDITOR ~/.config/usm/init.yaml
usm init                       # now runs your version
```

## Flags

| Flag | Effect |
| --- | --- |
| `[GROUPS]...` | Groups to install (default: `default_groups`) |
| `-i, --interactive` | Confirm each group before installing |
| `-n, --dry-run` | Print the commands; install nothing |
| `--config PATH` | Merge an external YAML config over the defaults |
| `--no-user-config` | Ignore `~/.config/usm/init.yaml` |
| `-l, --list` | List groups and tools, then exit |
| `--export-config` | Write the default config to `~/.config/usm/init.yaml` |
| `--force` | Overwrite when exporting the config |

## Caveats

- **Linux** recipes assume Debian/Ubuntu (`apt`). On other distros, edit the
  commands in your config.
- **macOS** assumes [Homebrew](https://brew.sh) is installed.
- **Windows** assumes `winget` (Windows 10+). `tmux` and `tmux-config` are
  skipped.
- `uv-tools` need `uv` on `PATH`; the `lang` group installs it first, and child
  processes get `~/.local/bin`, `~/.cargo/bin`, and Homebrew bins prepended —
  but on Windows a freshly `winget`-installed tool may not be visible until a
  new shell.
- Re-running is safe: installed tools are skipped, and the alias block is
  managed in place.

## Source

[`scripts/init.py`](https://github.com/HSPK/usm/blob/main/scripts/init.py).
Edit it, send a PR if it's useful to you too.
