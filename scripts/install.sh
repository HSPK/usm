#!/usr/bin/env bash
# install.sh – one-liner installer for usm via uv tool
# Usage: curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash
set -euo pipefail

info()  { printf '\033[1;34m[info]\033[0m  %s\n' "$*"; }
warn()  { printf '\033[1;33m[warn]\033[0m  %s\n' "$*"; }
error() { printf '\033[1;31m[error]\033[0m %s\n' "$*"; }

# ---------- 1. Ensure uv is installed ----------
if ! command -v uv &>/dev/null; then
    info "uv not found – installing via the official installer …"
    if ! command -v curl &>/dev/null; then
        error "curl not found. Install curl or uv manually: https://docs.astral.sh/uv/#installation"
        exit 1
    fi
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # The uv installer drops the binary into ~/.local/bin (or ~/.cargo/bin).
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv &>/dev/null; then
        warn "uv was installed but is not on PATH yet."
        warn "Run:  source ~/.bashrc  (or restart your shell), then rerun this script."
        exit 1
    fi
fi

info "Using uv at: $(command -v uv)"

# ---------- 2. Install / upgrade usmo ----------
info "Installing/upgrading usmo …"
uv tool install --upgrade usmo

# ---------- 3. Ensure uv's tool bin dir is on PATH ----------
uv tool update-shell 2>/dev/null || true

# ---------- 4. Verify ----------
if command -v usm &>/dev/null; then
    info "✅ usm installed successfully!"
    usm --help
else
    warn "usm was installed but the command is not on PATH."
    warn "You may need to run:  source ~/.bashrc  (or restart your shell)"
    warn "Then try:  usm --help"
fi

