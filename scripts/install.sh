#!/usr/bin/env bash
# install.sh – one-liner installer for usm via pipx
# Usage: curl -fsSL https://raw.githubusercontent.com/HSPK/usm/main/scripts/install.sh | bash
set -euo pipefail

info()  { printf '\033[1;34m[info]\033[0m  %s\n' "$*"; }
warn()  { printf '\033[1;33m[warn]\033[0m  %s\n' "$*"; }
error() { printf '\033[1;31m[error]\033[0m %s\n' "$*"; }

# ---------- 1. Ensure Python 3 is available ----------
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Please install Python 3.10+ first."
    exit 1
fi

# ---------- 2. Ensure pipx is installed ----------
if ! command -v pipx &>/dev/null; then
    info "pipx not found – installing via pip …"
    python3 -m pip install --user pipx 2>/dev/null \
        || python3 -m pip install pipx 2>/dev/null \
        || { error "Failed to install pipx. Install it manually: https://pipx.pypa.io/stable/installation/"; exit 1; }

    # pipx ensurepath adds ~/.local/bin to PATH in shell rc files
    python3 -m pipx ensurepath 2>/dev/null || true

    # Make pipx available in the current session
    export PATH="$HOME/.local/bin:$PATH"

    if ! command -v pipx &>/dev/null; then
        warn "pipx was installed but is not on PATH yet."
        warn "Run:  source ~/.bashrc  (or restart your shell), then rerun this script."
        exit 1
    fi
fi

info "Using pipx at: $(command -v pipx)"

# ---------- 3. Install / upgrade usmo ----------
if pipx list 2>/dev/null | grep -q 'usmo'; then
    info "usmo is already installed – upgrading …"
    pipx upgrade usmo
else
    info "Installing usmo …"
    pipx install usmo
fi

# ---------- 4. Verify ----------
if command -v usm &>/dev/null; then
    info "✅ usm installed successfully!"
    usm --help
else
    warn "usm was installed but the command is not on PATH."
    warn "You may need to run:"
    warn "  source ~/.bashrc   # or source ~/.zshrc"
    warn "Then try:  usm --help"
fi
