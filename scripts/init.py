#!/usr/bin/env python3
"""usm init — cross-platform dev-environment bootstrapper (config-driven).

A small declarative engine: a YAML config defines *groups* of *items* and, per
item, the command to run on each platform (macOS / Linux / Windows). ``init.py``
detects the platform, picks the matching command, skips anything already
installed, and runs the rest.

The default config is embedded below. Override or extend it by editing
``~/.config/usm/init.yaml`` (run ``usm init --export-config`` to get a starting
point) or by pointing ``--config`` at your own file.

Examples:
  usm init                     # install the default groups
  usm init cli lang            # only these groups
  usm init -i                  # interactively choose groups
  usm init --dry-run           # show what would run, install nothing
  usm init --list              # list groups and tools
  usm init --export-config     # write the default config to ~/.config/usm/init.yaml
"""

from __future__ import annotations

import abc
import enum
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

import click
import yaml

DEFAULT_USER_CONFIG = Path.home() / ".config" / "usm" / "init.yaml"
PLATFORM_KEYS = ("macos", "linux", "windows")

# Extra bin dirs that fresh installers drop tools into; prepended to child
# PATHs so later steps (e.g. uv tools) can see tools installed earlier.
_EXTRA_BIN_PATHS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".cargo" / "bin",
    Path.home() / ".fnm",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)


DEFAULT_CONFIG_YAML = """\
# usm init configuration. Edit freely; this file is merged over the built-in
# defaults. `default_groups` run when `usm init` is called with no arguments.
default_groups: [lang, cli, editor, profile, uv-tools, ai-tools, tmux]

groups:
  lang:
    description: Language and version managers
    items: [uv, fnm]
  cli:
    description: Modern command-line tools
    items: [gh, ripgrep, fd, bat, eza, fzf, zoxide, starship, btop]
  editor:
    description: Neovim and its config
    items: [neovim, nvim-config]
  profile:
    description: Shell alias block (delegates to `usm inject-alias`)
    items: [profile]
  uv-tools:
    description: uv-managed Python CLI tools
    items: [azure-cli, nvitop, amlt]
  ai-tools:
    description: AI coding assistants (npm-based)
    items: [copilot-cli, claude-code]
  tmux:
    description: tmux and its plugin manager
    platforms: [linux, macos]
    items: [tmux, tmux-config]
  linux-extras:
    description: Linux-only build deps, tailscale, dua-cli (not in defaults)
    platforms: [linux]
    items: [build-deps, tailscale, dua-cli]

items:
  uv:
    check: uv
    macos: brew install uv
    linux: curl -LsSf https://astral.sh/uv/install.sh | sh
    windows: winget install -e --id astral-sh.uv
  fnm:
    check: fnm
    macos: brew install fnm
    linux: curl -fsSL https://fnm.vercel.app/install | bash
    windows: winget install -e --id Schniz.fnm
  gh:
    check: gh
    macos: brew install gh
    linux: |
      sudo mkdir -p -m 755 /etc/apt/keyrings
      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
      sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
      sudo apt-get update && sudo apt-get install -y gh
    windows: winget install -e --id GitHub.cli
  ripgrep:
    check: rg
    macos: brew install ripgrep
    linux: sudo apt-get update && sudo apt-get install -y ripgrep
    windows: winget install -e --id BurntSushi.ripgrep.MSVC
  fd:
    check: fd
    macos: brew install fd
    linux: sudo apt-get update && sudo apt-get install -y fd-find
    windows: winget install -e --id sharkdp.fd
  bat:
    check: bat
    macos: brew install bat
    linux: sudo apt-get update && sudo apt-get install -y bat
    windows: winget install -e --id sharkdp.bat
  eza:
    check: eza
    macos: brew install eza
    linux: sudo apt-get update && sudo apt-get install -y eza
    windows: winget install -e --id eza-community.eza
  fzf:
    check: fzf
    macos: brew install fzf
    linux: sudo apt-get update && sudo apt-get install -y fzf
    windows: winget install -e --id junegunn.fzf
  zoxide:
    check: zoxide
    macos: brew install zoxide
    linux: curl -sSfL https://raw.githubusercontent.com/ajeetdsouza/zoxide/main/install.sh | sh
    windows: winget install -e --id ajeetdsouza.zoxide
  starship:
    check: starship
    macos: brew install starship
    linux: curl -sS https://starship.rs/install.sh | sh -s -- -y
    windows: winget install -e --id Starship.Starship
  btop:
    check: btop
    macos: brew install btop
    linux: sudo apt-get update && sudo apt-get install -y btop
    windows: winget install -e --id aristocratos.btop4win
  neovim:
    check: nvim
    macos: brew install neovim
    linux: sudo apt-get update && sudo apt-get install -y neovim
    windows: winget install -e --id Neovim.Neovim
  tmux:
    check: tmux
    macos: brew install tmux
    linux: sudo apt-get update && sudo apt-get install -y tmux
  azure-cli:
    check: az
    all: uv tool install --upgrade azure-cli
  nvitop:
    check: nvitop
    all: uv tool install --upgrade nvitop
  amlt:
    check: amlt
    all: uv tool install --upgrade amlt --index-url https://msrpypi.azurewebsites.net/stable/leloojoo
  copilot-cli:
    check: copilot
    all: npm install -g @github/copilot
  claude-code:
    check: claude
    all: npm install -g @anthropic-ai/claude-code
  build-deps:
    platforms: [linux]
    linux: |
      sudo apt-get update
      sudo apt-get install -y build-essential zlib1g-dev libffi-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses-dev tk-dev python3-dev ffmpeg cmake autossh
  tailscale:
    check: tailscale
    platforms: [linux]
    linux: curl -fsSL https://tailscale.com/install.sh | sh
  dua-cli:
    check: dua
    platforms: [linux]
    linux: curl -LSfs https://raw.githubusercontent.com/Byron/dua-cli/master/ci/install.sh | sh -s -- --git Byron/dua-cli --target x86_64-unknown-linux-musl --crate dua
  nvim-config:
    action: nvim-config
  profile:
    action: profile
  tmux-config:
    action: tmux-config
    platforms: [linux, macos]
"""


# --------------------------------------------------------------------------- #
# Platform
# --------------------------------------------------------------------------- #
class Platform(abc.ABC):
    """A target OS: its config key and how to run a shell command on it."""

    key: str

    @abc.abstractmethod
    def shell_argv(self, command: str) -> list[str]: ...


class _PosixPlatform(Platform):
    def shell_argv(self, command: str) -> list[str]:
        return ["bash", "-c", command]


class MacOS(_PosixPlatform):
    key = "macos"


class Linux(_PosixPlatform):
    key = "linux"


class Windows(Platform):
    key = "windows"

    def shell_argv(self, command: str) -> list[str]:
        return ["powershell", "-NoProfile", "-Command", command]


def detect_platform() -> Platform:
    if sys.platform == "darwin":
        return MacOS()
    if sys.platform == "win32":
        return Windows()
    return Linux()


# --------------------------------------------------------------------------- #
# Executor (injected so the engine never touches subprocess directly)
# --------------------------------------------------------------------------- #
class Executor(abc.ABC):
    @abc.abstractmethod
    def which(self, name: str) -> bool: ...

    @abc.abstractmethod
    def run(self, argv: Sequence[str], *, command: str) -> int: ...


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    extra = [str(p) for p in _EXTRA_BIN_PATHS if p.is_dir()]
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


class RealExecutor(Executor):
    def which(self, name: str) -> bool:
        return shutil.which(name) is not None

    def run(self, argv: Sequence[str], *, command: str) -> int:
        click.echo(f"  $ {command}")
        return subprocess.run(list(argv), env=_child_env()).returncode


class DryRunExecutor(Executor):
    def which(self, name: str) -> bool:
        return shutil.which(name) is not None

    def run(self, argv: Sequence[str], *, command: str) -> int:
        click.echo(f"  [dry-run] {command}")
        return 0


# --------------------------------------------------------------------------- #
# Domain model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Item:
    name: str
    check: str | None = None
    action: str | None = None
    commands: Mapping[str, str] = field(default_factory=dict)
    platforms: tuple[str, ...] | None = None

    @property
    def is_action(self) -> bool:
        return self.action is not None

    def supports(self, platform_key: str) -> bool:
        return self.platforms is None or platform_key in self.platforms

    def command_for(self, platform_key: str) -> str | None:
        return self.commands.get(platform_key) or self.commands.get("all")


@dataclass(frozen=True)
class Group:
    name: str
    description: str = ""
    items: tuple[str, ...] = ()
    platforms: tuple[str, ...] | None = None

    def supports(self, platform_key: str) -> bool:
        return self.platforms is None or platform_key in self.platforms


@dataclass(frozen=True)
class InitConfig:
    default_groups: tuple[str, ...]
    groups: Mapping[str, Group]
    items: Mapping[str, Item]

    @classmethod
    def from_raw(cls, raw: Mapping) -> InitConfig:
        items = {
            name: _parse_item(name, body)
            for name, body in (raw.get("items") or {}).items()
        }
        groups = {
            name: _parse_group(name, body)
            for name, body in (raw.get("groups") or {}).items()
        }
        for group in groups.values():
            missing = [i for i in group.items if i not in items]
            if missing:
                raise click.ClickException(
                    f"group '{group.name}' references undefined items: "
                    f"{', '.join(missing)}"
                )
        return cls(
            default_groups=tuple(raw.get("default_groups") or ()),
            groups=groups,
            items=items,
        )


def _normalize_command(value) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v).strip() for v in value)
    return str(value).strip()


def _parse_item(name: str, raw) -> Item:
    if not isinstance(raw, Mapping):
        raise click.ClickException(f"item '{name}' must be a mapping")
    platforms = tuple(raw["platforms"]) if raw.get("platforms") else None
    check = raw.get("check")
    if "action" in raw:
        return Item(
            name=name, check=check, action=str(raw["action"]), platforms=platforms
        )
    commands = {
        key: _normalize_command(raw[key])
        for key in (*PLATFORM_KEYS, "all")
        if raw.get(key) is not None
    }
    return Item(name=name, check=check, commands=commands, platforms=platforms)


def _parse_group(name: str, raw) -> Group:
    if not isinstance(raw, Mapping):
        raise click.ClickException(f"group '{name}' must be a mapping")
    platforms = tuple(raw["platforms"]) if raw.get("platforms") else None
    return Group(
        name=name,
        description=str(raw.get("description", "")),
        items=tuple(raw.get("items") or ()),
        platforms=platforms,
    )


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #
def _deep_merge(base: Mapping, override: Mapping) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise click.ClickException(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise click.ClickException(f"{path} must contain a YAML mapping")
    return dict(data)


class ConfigRepository:
    """Loads the embedded default config and merges optional overrides over it."""

    def __init__(
        self,
        default_yaml: str = DEFAULT_CONFIG_YAML,
        user_config_path: Path | None = None,
    ) -> None:
        self._default_yaml = default_yaml
        self._user_config_path = user_config_path or DEFAULT_USER_CONFIG

    @property
    def user_config_path(self) -> Path:
        return self._user_config_path

    def default_yaml_text(self) -> str:
        return self._default_yaml

    def load(
        self, *, config_path: Path | None = None, use_user_config: bool = True
    ) -> InitConfig:
        raw = yaml.safe_load(self._default_yaml) or {}
        if use_user_config and self._user_config_path.is_file():
            raw = _deep_merge(raw, _read_yaml(self._user_config_path))
        if config_path is not None:
            raw = _deep_merge(raw, _read_yaml(config_path))
        return InitConfig.from_raw(raw)


# --------------------------------------------------------------------------- #
# Run context + steps
# --------------------------------------------------------------------------- #
class StepResult(enum.Enum):
    OK = "ok"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass
class RunContext:
    platform: Platform
    executor: Executor
    dry_run: bool
    home: Path

    def which(self, name: str) -> bool:
        return self.executor.which(name)

    def run_shell(self, command: str) -> int:
        return self.executor.run(self.platform.shell_argv(command), command=command)


class Step(abc.ABC):
    def __init__(self, item: Item) -> None:
        self.item = item

    @property
    def name(self) -> str:
        return self.item.name

    @abc.abstractmethod
    def execute(self, ctx: RunContext) -> StepResult: ...


class CommandStep(Step):
    def execute(self, ctx: RunContext) -> StepResult:
        if not self.item.supports(ctx.platform.key):
            return StepResult.UNSUPPORTED
        command = self.item.command_for(ctx.platform.key)
        if command is None:
            return StepResult.UNSUPPORTED
        if self.item.check and ctx.which(self.item.check):
            return StepResult.SKIPPED
        code = ctx.run_shell(command)
        return StepResult.OK if code == 0 else StepResult.FAILED


class ActionStep(Step):
    def execute(self, ctx: RunContext) -> StepResult:
        if not self.item.supports(ctx.platform.key):
            return StepResult.UNSUPPORTED
        handler = ACTION_REGISTRY.get(self.item.action or "")
        if handler is None:
            click.echo(f"  unknown action: {self.item.action}", err=True)
            return StepResult.FAILED
        return handler(ctx)


class StepFactory:
    def create(self, item: Item) -> Step:
        return ActionStep(item) if item.is_action else CommandStep(item)


# --------------------------------------------------------------------------- #
# Actions (special, non-command steps)
# --------------------------------------------------------------------------- #
ActionHandler = Callable[[RunContext], StepResult]
ACTION_REGISTRY: dict[str, ActionHandler] = {}


def action(name: str) -> Callable[[ActionHandler], ActionHandler]:
    def register(fn: ActionHandler) -> ActionHandler:
        ACTION_REGISTRY[name] = fn
        return fn

    return register


_NVIM_INIT_VIM = """\
" --- Visual ---
syntax on
set number
set relativenumber
set cursorline
set termguicolors
set showmatch
set scrolloff=5
set sidescrolloff=8
set signcolumn=yes
set list listchars=tab:»·,trail:·,nbsp:␣

" --- Indentation ---
set tabstop=4
set shiftwidth=4
set expandtab
set autoindent
set smartindent
set backspace=indent,eol,start

" --- Search ---
set hlsearch
set incsearch
set ignorecase
set smartcase
set inccommand=split

" --- Editing / Buffers / Windows ---
set hidden
set confirm
set noswapfile
set undofile
set undodir=__UNDODIR__
set splitbelow
set splitright
set mouse=a
set timeoutlen=500
set clipboard+=unnamedplus
filetype plugin indent on

" --- Keymaps ---
let mapleader="'"
inoremap jk <ESC>
nnoremap <leader>w :w<CR>
nnoremap <leader>q :q<CR>
nnoremap <leader>h :nohlsearch<CR>
nnoremap <C-h> <C-w>h
nnoremap <C-j> <C-w>j
nnoremap <C-k> <C-w>k
nnoremap <C-l> <C-w>l
tnoremap <Esc> <C-\\><C-n>
"""


_TMUX_BEGIN_MARKER = "# __USM_TMUX_BEGIN__"
_TMUX_END_MARKER = "# __USM_TMUX_END__"

_TMUX_CONF = r"""# List of plugins
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-sensible'

set -g @dracula-plugins "git cpu-usage ram-usage network-bandwidth battery time"
set -g @dracula-border-contrast true
set -g @dracula-show-timezone false
set -g @dracula-military-time true
set -g @plugin 'dracula/tmux'

# --- Terminal, colour and clipboard ---
set -g default-terminal "tmux-256color"
set -g set-clipboard on
set -g focus-events on
set -sg escape-time 10
%if #{>=:#{version},3.2}
set -as terminal-features ",xterm-256color:RGB:clipboard"
%endif
%if #{>=:#{version},3.3}
set -g allow-passthrough on
%endif

# --- Comfort ---
set -g mouse on
set -g history-limit 50000
set -g base-index 1
setw -g pane-base-index 1
set -g renumber-windows on
setw -g mode-keys vi
setw -g aggressive-resize on
set -g display-time 2000
set -g status-interval 5
set -g set-titles on
set -g set-titles-string "#S · #W"

# --- Keys ---
bind R source-file ~/.tmux.conf \; display-message "tmux.conf reloaded"
bind | split-window -h -c "#{pane_current_path}"
bind - split-window -v -c "#{pane_current_path}"
bind c new-window -c "#{pane_current_path}"

# Initialize TMUX plugin manager (keep this line at the very bottom of tmux.conf)
run '~/.tmux/plugins/tpm/tpm'
"""

# Block appended by older usm versions (before the managed markers existed);
# removed on upgrade so the config is not duplicated.
_TMUX_LEGACY_CONF = """\
# List of plugins
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-sensible'

set -g @dracula-plugins "git cpu-usage ram-usage network-bandwidth battery time"
set -g @dracula-border-contrast true
set -g @dracula-show-timezone false
set -g @dracula-military-time true
set -g @plugin 'dracula/tmux'

setw -g mouse on
# Initialize TMUX plugin manager (keep this line at the very bottom of tmux.conf)
run '~/.tmux/plugins/tpm/tpm'
"""


def _strip_managed_block(content: str, begin: str, end: str) -> tuple[str, bool]:
    """Remove a ``begin``/``end`` delimited block, returning (rest, was_found)."""
    kept: list[str] = []
    inside = False
    found = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == begin:
            if inside:
                raise ValueError(f"nested {begin} marker; fix the file manually")
            inside = True
            found = True
            continue
        if stripped == end:
            if not inside:
                raise ValueError(f"{end} without a matching {begin}; fix it manually")
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        raise ValueError(f"unterminated {begin} block; fix the file manually")
    return "\n".join(kept).strip("\n"), found


def _upsert_managed_block(path: Path, body: str, begin: str, end: str) -> str:
    """Insert/refresh a managed block in ``path``; returns what happened."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    cleaned, replaced = _strip_managed_block(original, begin, end)
    block = f"{begin}\n{body.strip()}\n{end}\n"
    updated = f"{cleaned}\n\n{block}" if cleaned else block
    if updated == original:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return "updated" if replaced else "inserted"


def _nvim_paths(ctx: RunContext) -> tuple[Path, Path]:
    if ctx.platform.key == "windows":
        config = ctx.home / "AppData" / "Local" / "nvim" / "init.vim"
        undo = ctx.home / "AppData" / "Local" / "nvim-data" / "undo"
    else:
        config = ctx.home / ".config" / "nvim" / "init.vim"
        undo = ctx.home / ".local" / "share" / "nvim" / "undo"
    return config, undo


@action("nvim-config")
def _action_nvim_config(ctx: RunContext) -> StepResult:
    config_path, undo_dir = _nvim_paths(ctx)
    content = _NVIM_INIT_VIM.replace("__UNDODIR__", undo_dir.as_posix())
    if ctx.dry_run:
        click.echo(f"  [dry-run] write {config_path}")
        return StepResult.OK
    undo_dir.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    click.echo(f"  wrote {config_path}")
    return StepResult.OK


def _upsert_tmux_conf(conf_path: Path) -> str:
    """Sync the managed tmux block, dropping blocks appended by old versions."""
    original = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    without_legacy = original.replace(_TMUX_LEGACY_CONF, "")
    if without_legacy != original:
        conf_path.write_text(without_legacy.strip("\n"), encoding="utf-8")
    return _upsert_managed_block(
        conf_path, _TMUX_CONF, _TMUX_BEGIN_MARKER, _TMUX_END_MARKER
    )


@action("tmux-config")
def _action_tmux_config(ctx: RunContext) -> StepResult:
    tpm_dir = ctx.home / ".tmux" / "plugins" / "tpm"
    conf_path = ctx.home / ".tmux.conf"
    needs_tpm = not tpm_dir.is_dir()
    if ctx.dry_run:
        if needs_tpm:
            click.echo(f"  [dry-run] clone tpm into {tpm_dir}")
        click.echo(f"  [dry-run] sync managed tmux block in {conf_path}")
        return StepResult.OK
    if needs_tpm:
        code = ctx.run_shell(
            f"git clone https://github.com/tmux-plugins/tpm '{tpm_dir}'"
        )
        if code != 0:
            return StepResult.FAILED
    try:
        outcome = _upsert_tmux_conf(conf_path)
    except ValueError as error:
        click.echo(f"  {conf_path}: {error}", err=True)
        return StepResult.FAILED
    if outcome == "unchanged":
        return StepResult.OK if needs_tpm else StepResult.SKIPPED
    click.echo(
        f"  {outcome} managed tmux block in {conf_path} "
        "(press prefix + I in tmux to install plugins)"
    )
    return StepResult.OK


@action("profile")
def _action_profile(ctx: RunContext) -> StepResult:
    code = ctx.run_shell("usm inject-alias")
    return StepResult.OK if code == 0 else StepResult.FAILED


# --------------------------------------------------------------------------- #
# Planning + selection + orchestration
# --------------------------------------------------------------------------- #
class Planner:
    def __init__(self, config: InitConfig) -> None:
        self._config = config

    def resolve_groups(self, names: Sequence[str] | None) -> list[Group]:
        selected = list(names) if names else list(self._config.default_groups)
        groups: list[Group] = []
        for name in selected:
            group = self._config.groups.get(name)
            if group is None:
                available = ", ".join(sorted(self._config.groups))
                raise click.ClickException(
                    f"unknown group '{name}'. available: {available}"
                )
            groups.append(group)
        return groups

    def items_for(self, groups: Sequence[Group], platform_key: str) -> list[Item]:
        seen: set[str] = set()
        items: list[Item] = []
        for group in groups:
            for item_name in group.items:
                if item_name in seen:
                    continue
                seen.add(item_name)
                item = self._config.items[item_name]
                if item.supports(platform_key):
                    items.append(item)
        return items


class InteractiveSelector:
    def filter(self, groups: Sequence[Group]) -> list[Group]:
        kept: list[Group] = []
        for group in groups:
            label = group.description or group.name
            if click.confirm(f"Install '{group.name}' — {label}?", default=True):
                kept.append(group)
        return kept


_SYMBOL = {
    StepResult.OK: "✓",
    StepResult.SKIPPED: "•",
    StepResult.UNSUPPORTED: "·",
    StepResult.FAILED: "✗",
}


class Engine:
    def __init__(
        self,
        config: InitConfig,
        factory: StepFactory,
        context: RunContext,
        selector: InteractiveSelector | None = None,
    ) -> None:
        self._planner = Planner(config)
        self._factory = factory
        self._ctx = context
        self._selector = selector

    def run(self, group_names: Sequence[str] | None, *, interactive: bool) -> None:
        groups = self._planner.resolve_groups(group_names)
        groups = [g for g in groups if g.supports(self._ctx.platform.key)]
        if interactive and self._selector is not None:
            groups = self._selector.filter(groups)
        items = self._planner.items_for(groups, self._ctx.platform.key)
        if not items:
            click.echo("Nothing to do for this platform.")
            return

        failures: list[str] = []
        for item in items:
            step = self._factory.create(item)
            result = step.execute(self._ctx)
            self._report(step.name, result)
            if result is StepResult.FAILED:
                failures.append(step.name)

        click.echo(
            f"\nDone: {len(items)} step(s), {len(failures)} failed"
            f"{' (dry run)' if self._ctx.dry_run else ''}."
        )
        if failures:
            raise click.ClickException(
                f"{len(failures)} step(s) failed: {', '.join(failures)}"
            )

    def _report(self, name: str, result: StepResult) -> None:
        messages = {
            StepResult.OK: "would install" if self._ctx.dry_run else "installed",
            StepResult.SKIPPED: "already present",
            StepResult.UNSUPPORTED: f"no recipe for {self._ctx.platform.key}",
            StepResult.FAILED: "failed",
        }
        click.echo(f"{_SYMBOL[result]} {name}: {messages[result]}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_list(config: InitConfig) -> None:
    defaults = set(config.default_groups)
    click.echo("Groups (★ = run by default):")
    for name, group in config.groups.items():
        mark = "★" if name in defaults else " "
        platforms = f" [{', '.join(group.platforms)}]" if group.platforms else ""
        click.echo(f"  {mark} {name}{platforms} — {group.description}")
        click.echo(f"      {', '.join(group.items)}")


def _export_config(repo: ConfigRepository, *, force: bool) -> None:
    path = repo.user_config_path
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists; pass --force to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(repo.default_yaml_text(), encoding="utf-8")
    click.echo(f"Wrote default config to {path}")


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Bootstrap a machine with modern dev tools, driven by a YAML config.",
)
@click.argument("groups", nargs=-1)
@click.option(
    "-i", "--interactive", is_flag=True, help="Choose which groups to install."
)
@click.option("-n", "--dry-run", is_flag=True, help="Print the plan; install nothing.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="External YAML config merged over the built-in defaults.",
)
@click.option(
    "--no-user-config",
    is_flag=True,
    help=f"Ignore {DEFAULT_USER_CONFIG} even if it exists.",
)
@click.option(
    "-l", "--list", "list_only", is_flag=True, help="List groups and tools, then exit."
)
@click.option(
    "--export-config",
    is_flag=True,
    help=f"Write the default config to {DEFAULT_USER_CONFIG}, then exit.",
)
@click.option("--force", is_flag=True, help="Overwrite when exporting the config.")
def cli(
    groups: tuple[str, ...],
    interactive: bool,
    dry_run: bool,
    config_path: Path | None,
    no_user_config: bool,
    list_only: bool,
    export_config: bool,
    force: bool,
) -> None:
    repo = ConfigRepository()
    if export_config:
        _export_config(repo, force=force)
        return
    config = repo.load(config_path=config_path, use_user_config=not no_user_config)
    if list_only:
        _print_list(config)
        return
    context = RunContext(
        platform=detect_platform(),
        executor=DryRunExecutor() if dry_run else RealExecutor(),
        dry_run=dry_run,
        home=Path.home(),
    )
    engine = Engine(config, StepFactory(), context, InteractiveSelector())
    engine.run(list(groups) or None, interactive=interactive)


def main() -> None:
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.Abort:
        sys.exit(130)


if __name__ == "__main__":
    main()
