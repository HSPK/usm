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
    mkdir -p ~/.local/share/nvim/undo
    cat > ~/.config/nvim/init.vim <<'EOF'
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
set undodir=~/.local/share/nvim/undo//
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
tnoremap <Esc> <C-\><C-n>
EOF
    echo "Wrote ~/.config/nvim/init.vim"
}

usage() {
    cat <<'EOF'
Usage: usm init [SUBCOMMAND ...]

With no SUBCOMMAND, runs the full bootstrap pipeline.
Otherwise runs each named step in order.

Subcommands:
  all          Full bootstrap (essentials, nvim, profile, uv, tools, tmux)
  essentials   apt + snap packages + dua-cli
  nvim         Write ~/.config/nvim/init.vim
  profile      Insert/update the managed alias block in ~/.bashrc or ~/.zshrc
  uv           Install uv via the official installer
  tools        Install uv-managed tools (azure-cli, nvitop, amlt)
  tmux         Install tmux plugin manager and write ~/.tmux.conf
  tailscale    Install tailscale and bring up the node

  -p           Alias for 'profile' (back-compat)
  -h, --help   Show this help

Examples:
  usm init                 # full bootstrap
  usm init nvim            # only neovim config
  usm init nvim profile    # multiple steps in order
EOF
}

run_step() {
    case "$1" in
        all)
            install_nesseraries
            config_nvim
            update_profile
            install_uv
            install_uv_tools
            install_tmux_plugins
            echo "Installation complete. Please restart your terminal or run 'source ~/.bashrc' to apply changes."
            ;;
        essentials)   install_nesseraries ;;
        nvim)         config_nvim ;;
        profile|-p)   update_profile ;;
        uv)           install_uv ;;
        tools)        install_uv_tools ;;
        tmux)         install_tmux_plugins ;;
        tailscale)    install_tailscale ;;
        -h|--help)    usage ;;
        *)
            echo "Unknown subcommand: $1" >&2
            usage >&2
            return 2 ;;
    esac
}

main() {
    if [ $# -eq 0 ]; then
        run_step all
        return
    fi
    for step in "$@"; do
        run_step "$step" || return $?
    done
}

main "$@"
