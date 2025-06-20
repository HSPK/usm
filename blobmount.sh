#!/bin/bash

check_blobfuse2() {
  if ! command -v blobfuse2 &>/dev/null; then
    echo "blobfuse2 could not be found, installing..."
    install_blobfuse2
  fi
}

install_blobfuse2() {
  wget https://github.com/Azure/azure-storage-fuse/releases/download/blobfuse2-2.3.2/blobfuse2-2.3.2-Ubuntu-20.04.x86_64.deb
  sudo apt update
  sudo apt install fuse3 -y
  sudo dpkg -i ./blobfuse2-2.3.2-Ubuntu-20.04.x86_64.deb
  sudo sed -i '1i user_allow_other' /etc/fuse.conf
}

# mount.sh file content
mount() {
  if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <mount_dir> <account> <container>"
    exit 1
  fi
  local mount_dir="$1"
  local account="$2"
  local container="$3"
  local config_file="$HOME/.config/blobfuse/$account-$container.yaml"

  # expiry date set to 6 days from now
  expiry_date=$(date -d "+6 days" +%Y-%m-%dT%H:%M:%SZ)
  sas_token=$(az storage container generate-sas --account-name "$account" --name "$container" --permissions acdlrw --expiry $expiry_date --auth-mode login --as-user)

  mkdir -p "$(dirname "$config_file")"
  mkdir -p $HOME/blob_tmp/$account-$container

  echo """file_cache:
    path: $HOME/blob_tmp/$account-$container
logging:
  type: syslog
  level: log_debug
components:
  - libfuse
  - file_cache
  - attr_cache
  - azstorage
libfuse:
  attribute-expiration-sec: 120
  entry-expiration-sec: 120
  negative-entry-expiration-sec: 240

attr_cache:
  timeout-sec: 7200

azstorage:
    type: block
    account-name: $account
    endpoint: https://$account.blob.core.windows.net/
    container: $container
    mode: sas
    sas: $sas_token""" >"$config_file"

  # check the premissions of the mount directory
  if [ ! -w "$mount_dir" ]; then
    echo "Mount directory $mount_dir is not writable. Please check permissions."
    exit 1
  fi
  mkdir -p $mount_dir
  if ! mountpoint -q "$mount_dir"; then
    echo "Mounting $container from $account to $mount_dir"
    blobfuse2 mount "$mount_dir" --config-file "$config_file" --allow-other
  else
    echo "$mount_dir is already mounted, only updating the SAS token"
  fi
}

check_blobfuse2
mount "$@"
