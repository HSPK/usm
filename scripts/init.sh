install_nesseraries() {
    echo "Installing necessary packages..."
    sudo apt-get update
    sudo apt-get install build-essential zlib1g-dev libffi-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses-dev tk-dev python3-dev ffmpeg cmake autossh neovim zsh tmux -y
    sudo snap install btop gh -y
    curl -LSfs https://raw.githubusercontent.com/Byron/dua-cli/master/ci/install.sh | \
        sh -s -- --git Byron/dua-cli --target x86_64-unknown-linux-musl --crate dua --tag v2.29.0
}

install_tailscale() {
    if ! command -v tailscale &>/dev/null; then
        echo "tailscale is not installed. Installing tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh
        sudo tailscale up --advertise-exit-node --accept-routes
    else
        echo "tailscale is already installed."
    fi
}

update_profile() {
    # Detect current shell and set profile file
    local profile_file
    if [ -n "$ZSH_VERSION" ]; then
        profile_file="$HOME/.zshrc"
    elif [ -n "$BASH_VERSION" ]; then
        profile_file="$HOME/.bashrc"
    else
        # Default to bashrc if shell cannot be detected
        profile_file="$HOME/.bashrc"
    fi

    echo "Updating profile file: $profile_file"

    # The new aliases content
    local new_content='## __USM_INIT_ALIAS_BEGIN__
alias ll="ls -lh"
alias gs="git status"
alias ga="git add"
alias gm="git commit -m"
alias gb="git branch"
alias gp="git push && git push --tags"
alias gc="git checkout"
alias tn="tmux new -s"
alias p4="proxychains4"
alias ta="tmux attach -t"
alias tm="tmux -u"
alias ..="cd .."
alias ...="cd ../../"
alias ca="conda activate"
alias azl="az login"
alias gu="nvidia-smi"
alias v="nvim"
gmp () {
	git add .
	git commit -m "$1"
	git push
}
export PATH=/home/$(whoami)/.local/bin:$PATH
export PATH=/home/$(whoami)/.cargo/bin:$PATH
export AZCOPY_AUTO_LOGIN_TYPE=AZCLI
## __USM_INIT_ALIAS_END__'

    # Check if the markers exist in the profile file
    if [ -f "$profile_file" ] && grep -q "__USM_INIT_ALIAS_BEGIN__" "$profile_file" && grep -q "__USM_INIT_ALIAS_END__" "$profile_file"; then
        echo "Found existing USM aliases, replacing..."
        # Create a temporary file
        local temp_file=$(mktemp)
        
        # Use sed to replace content between markers
        sed '/## __USM_INIT_ALIAS_BEGIN__/,/## __USM_INIT_ALIAS_END__/d' "$profile_file" > "$temp_file"
        
        # Append new content
        echo "" >> "$temp_file"
        echo "$new_content" >> "$temp_file"
        
        # Replace the original file
        mv "$temp_file" "$profile_file"
        echo "USM aliases updated in $profile_file"
    else
        echo "No existing USM aliases found, appending..."
        # Append to the end of the file
        echo "" >> "$profile_file"
        echo "$new_content" >> "$profile_file"
        echo "USM aliases added to $profile_file"
    fi
}

install_uv() {
    if ! command -v uv &>/dev/null; then
        echo "uv is not installed. Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        uv tool update-shell 2>/dev/null || true
    else
        echo "uv is already installed."
    fi
}

install_uv_tools() {
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    local packages=(
        "azure-cli"
        "nvitop"
    )

    for package in "${packages[@]}"; do
        echo "Installing $package using uv tool..."
        uv tool install --upgrade "$package"
    done

    uv tool install --upgrade amlt --index-url https://msrpypi.azurewebsites.net/stable/leloojoo
}

install_tmux_plugins() {
    if [ ! -d "$HOME/.tmux/plugins/tpm" ]; then
        echo "Installing Tmux Plugin Manager..."
        git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
        echo """# List of plugins
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-sensible'

# Other examples:
# set -g @plugin 'github_username/plugin_name'
# set -g @plugin 'github_username/plugin_name#branch'
# set -g @plugin 'git@github.com:user/plugin'
# set -g @plugin 'git@bitbucket.com:user/plugin'

# available plugins: battery, cpu-usage, git, gpu-usage, ram-usage, tmux-ram-usage, network, network-bandwidth, network-ping, ssh-session, attached-clients, network-vpn, weather, time, mpc, spotify-tui, playerctl, kubernetes-context, synchronize-panes
set -g @dracula-plugins \"git cpu-usage ram-usage network-bandwidth battery time\"
set -g @dracula-border-contrast true
set -g @dracula-show-timezone false
set -g @dracula-military-time true
set -g @plugin 'dracula/tmux'

setw -g mouse on
# Initialize TMUX plugin manager (keep this line at the very bottom of tmux.conf)
run '~/.tmux/plugins/tpm/tpm'
""" >>~/.tmux.conf
        tmux source ~/.tmux.conf
        echo "Tmux Plugin Manager installed. Press 'prefix + I' to install plugins."
    else
        echo "Tmux Plugin Manager is already installed."
    fi
}

config_nvim() {
    mkdir -p ~/.config/nvim
    cat > ~/.config/nvim/init.vim <<'EOF'
syntax on
set tabstop=4
set expandtab
set shiftwidth=4
set backspace=indent,eol,start
set autoindent
set showmatch
set cul
set number
set noswapfile
set hlsearch
set ignorecase
set incsearch
inoremap jk <ESC>
let mapleader="'"
set clipboard+=unnamedplus
EOF
}

install() {
    if [ "$1" == "-p" ]; then
        update_profile
        echo "Profile updated. Please restart your terminal or source your profile file to apply changes."
        return
    fi

    install_nesseraries
    config_nvim
    update_profile
    install_uv
    install_uv_tools
    install_tmux_plugins

    echo "Installation complete. Please restart your terminal or run 'source ~/.bashrc' to apply changes."
}

install "$@"
