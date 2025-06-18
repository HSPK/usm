install_nesseraries() {
    echo "Installing necessary packages..."
    sudo apt-get update
    sudo apt-get install build-essential zlib1g-dev libffi-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses-dev tk-dev python3-dev pipx -y
    sudo apt install autossh neovim zsh tmux -y
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

install_pyenv() {
    if ! command -v pyenv &>/dev/null; then
        echo "pyenv is not installed. Installing pyenv..."
        curl -L https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash
        echo 'export PATH="$HOME/.pyenv/bin:$PATH"' >>~/.bashrc
        echo 'eval "$(pyenv init -)"' >>~/.bashrc
        echo 'eval "$(pyenv virtualenv-init -)"' >>~/.bashrc
        export PATH="$HOME/.pyenv/bin:$PATH"
        pyenv install 3.10
        pyenv global 3.10
        pip install click PyYaml
    else
        echo "pyenv is already installed."
    fi
}
update_bashrc() {
    echo """alias ll='ls -lh'
alias gs='git status'
alias ga='git add'
alias gm='git commit -m'
alias gb='git branch'
alias gp='git push && git push --tags'
alias gc='git checkout'
alias tn='tmux new -s'
alias p4='proxychains4'
alias ta='tmux attach -t'
alias tm='tmux -u'
alias ..='cd ..'
alias ...='cd ../../'
alias ca='conda activate'
alias azl='az login --use-device-code'
alias gu='nvidia-smi'
alias v='nvim'
gmp () {
	git add .
	git commit -m $1
	git push
}
export PATH=/home/$(whoami)/.local/bin:$PATH
export AZCOPY_AUTO_LOGIN_TYPE=AZCLI
""" >>~/.bashrc
}

install_pipx() {
    if ! command -v pipx &>/dev/null; then
        echo "pipx is not installed. Installing pipx..."
        sudo apt install pipx -y
        pipx ensurepath
    else
        echo "pipx is already installed."
    fi
}

install_pipx_packages() {
    export PATH="$HOME/.local/bin:$PATH"
    local packages=(
        "uv"
        "azure-cli"
        "amlt --pip-args=' --index-url https://msrpypi.azurewebsites.net/stable/leloojoo'"
    )

    for package in "${packages[@]}"; do
        echo "Installing $package using pipx..."
        pipx install "$package"
    done
}

install() {
    install_nesseraries
    install_tailscale
    install_pyenv
    update_bashrc
    install_pipx
    install_pipx_packages

    echo "Installation complete. Please restart your terminal or run 'source ~/.bashrc' to apply changes."
}

install "$@"
