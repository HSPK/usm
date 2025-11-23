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

  cache_dir="$HOME/.cache/blobfuse2/$account-$container"
  echo """file_cache:
    path: ${cache_dir}
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
  if [ ! -w "$(dirname $mount_dir)" ]; then
    echo "$(dirname $mount_dir) is not writable. Please check permissions."
    exit 1
  fi
  mkdir -p $mount_dir
  if ! mountpoint -q "$mount_dir"; then
    echo "Mounting $container from $account to $mount_dir"
    # delete cache directory if it exists, prompt user for confirmation
    if [ -d "$cache_dir" ]; then
      echo "Cache directory $cache_dir already exists. Deleting it."
      read -p "Are you sure you want to delete it? (y/n): " confirm </dev/tty
      if [[ "$confirm" == "y" ]]; then
        rm -rf "$cache_dir"
      else
        exit 1
      fi
    else
      echo "Cache directory $cache_dir does not exist, creating it."
      mkdir -p "$cache_dir"
    fi
    AZCOPY_AUTO_LOGIN_TYPE=AZCLI blobfuse2 mount "$mount_dir" --config-file "$config_file" --allow-other
    # check if successful
    if [ $? -ne 0 ]; then
      echo "Mounting failed. Please check the configuration and try again."
      exit 1
    else
      echo "Mounted $container from $account to $mount_dir successfully."
    fi
  else
    echo "$mount_dir is already mounted, only updating the SAS token"
  fi
}

check_blobfuse2
mount "$@"