#!/usr/bin/env bash

set -Eeuo pipefail

readonly AML_RESOURCE_TYPE="Microsoft.MachineLearningServices/workspaces"
readonly -a WORKSPACE_ROLES=(
  "Reader"
  "AzureML Data Scientist"
  "Contributor"
)
readonly -a STORAGE_ROLES=(
  "Reader"
  "Storage Blob Data Reader"
  "Storage Blob Data Contributor"
  "Storage Table Data Contributor"
)

declare -a SUBSCRIPTION_ROWS=()
declare -a WORKSPACE_ROWS=()
declare -a STORAGE_ROWS=()
declare -a SELECTED_WORKSPACE_INDEXES=()
declare -a SELECTED_STORAGE_INDEXES=()
declare -a SELECTED_WORKSPACE_NAMES=()
declare -a SELECTED_WORKSPACE_GROUPS=()
declare -a SELECTED_WORKSPACE_IDS=()
declare -a STORAGE_NAMES=()
declare -a STORAGE_IDS=()
declare -a SELECTED_WORKSPACE_ROLES=("${WORKSPACE_ROLES[@]}")
declare -a SELECTED_STORAGE_ROLES=("${STORAGE_ROLES[@]}")
declare -a FAILURES=()
declare -A ROLE_IDS=()

ASSIGNMENT_SCOPE=both
STORAGE_SELECTION_MODE=
SELECTED_SUBSCRIPTION_NAME=
SELECTED_SUBSCRIPTION_ID=
SELECTED_TENANT_ID=
CREATED_COUNT=0
EXISTING_COUNT=0
FAILED_COUNT=0

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: usm azrole

Interactively select an enabled Azure subscription, Azure Machine Learning
workspaces, and a Microsoft Entra user, then assign:

  Workspace scope:
    Reader
    AzureML Data Scientist
    Contributor

  Selected storage-account scope:
    Reader
    Storage Blob Data Reader
    Storage Blob Data Contributor
    Storage Table Data Contributor

Workspace selections accept comma-separated numbers and ranges, for example:
  1-10
  1,2,3,5
  1-3,5,8-10

Storage accounts can be selected independently with the same syntax. Press
Enter at the storage-account prompt to use the default storage accounts
associated with the selected workspaces.

After subscription selection, assignment scope is selected:
  1. Workspaces and storage accounts
  2. Workspaces only
  3. Storage accounts only

The first prompt selects the subscription used by this script. Press Enter to
use the subscription currently marked as default by Azure CLI. The selected ID
is passed explicitly to later commands and does not change the global Azure CLI
default. Authentication comes from the existing Azure CLI login; this script
does not accept or store credential secrets.

Roles are selected separately for each enabled scope. Role selections accept
the same comma-separated numbers and ranges; press Enter to select all roles.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

# Load enabled subscriptions available through the current Azure CLI login.
load_subscriptions() {
  local subscription_tsv

  if ! subscription_tsv=$(
    az account list \
      --only-show-errors \
      --query 'sort_by([?state == `Enabled`], &name)[].[name, id, tenantId, isDefault]' \
      --output tsv
  ); then
    die "Azure CLI is not signed in. Run 'az login' first."
  fi

  [[ -n "$subscription_tsv" ]] ||
    die "No enabled Azure subscriptions are available. Run 'az login' to refresh the account list."

  mapfile -t SUBSCRIPTION_ROWS <<< "$subscription_tsv"
}

# Display subscriptions and mark the Azure CLI default used by an empty choice.
print_subscriptions() {
  local index name subscription_id tenant_id is_default current

  printf '\nAvailable Azure subscriptions:\n\n'
  printf '%-5s %-36s %-36s %-36s %-7s\n' \
    "No." "Subscription" "Subscription ID" "Tenant ID" "Default"
  printf '%-5s %-36s %-36s %-36s %-7s\n' \
    "----" "------------------------------------" \
    "------------------------------------" "------------------------------------" \
    "-------"

  for index in "${!SUBSCRIPTION_ROWS[@]}"; do
    IFS=$'\t' read -r name subscription_id tenant_id is_default \
      <<< "${SUBSCRIPTION_ROWS[$index]}"
    current=
    if [[ "${is_default,,}" == true ]]; then
      current=yes
    fi
    printf '%-5d %-36s %-36s %-36s %-7s\n' \
      "$((index + 1))" "$name" "$subscription_id" "$tenant_id" "$current"
  done
}

# Select and verify the subscription without changing Azure CLI's global default.
select_subscription() {
  local index name subscription_id tenant_id is_default
  local selection normalized selected_index selected_tsv
  local default_index=-1

  load_subscriptions
  print_subscriptions

  for index in "${!SUBSCRIPTION_ROWS[@]}"; do
    IFS=$'\t' read -r name subscription_id tenant_id is_default \
      <<< "${SUBSCRIPTION_ROWS[$index]}"
    if [[ "${is_default,,}" == true ]]; then
      default_index=$index
      break
    fi
  done

  while true; do
    if ((default_index >= 0)); then
      if ! read -r -p \
        "Select subscription [$((default_index + 1))]: " \
        selection; then
        die "No subscription selection was provided."
      fi
    elif ! read -r -p "Select subscription: " selection; then
      die "No subscription selection was provided."
    fi

    normalized=${selection//[[:space:]]/}
    if [[ -z "$normalized" ]]; then
      if ((default_index < 0)); then
        printf 'Select a subscription number.\n' >&2
        continue
      fi
      selected_index=$default_index
      break
    fi

    if [[ "$normalized" =~ ^[0-9]+$ ]] &&
      ((10#$normalized >= 1 && 10#$normalized <= ${#SUBSCRIPTION_ROWS[@]})); then
      selected_index=$((10#$normalized - 1))
      break
    fi

    printf 'Invalid selection. Enter a number from 1 to %d.\n' \
      "${#SUBSCRIPTION_ROWS[@]}" >&2
  done

  IFS=$'\t' read -r name subscription_id tenant_id is_default \
    <<< "${SUBSCRIPTION_ROWS[$selected_index]}"

  if ! selected_tsv=$(
    az account show \
      --only-show-errors \
      --subscription "$subscription_id" \
      --query '[name, id, tenantId]' \
      --output tsv
  ); then
    die "Unable to access the selected Azure subscription '$name' ($subscription_id)."
  fi

  IFS=$'\t' read -r \
    SELECTED_SUBSCRIPTION_NAME \
    SELECTED_SUBSCRIPTION_ID \
    SELECTED_TENANT_ID <<< "$selected_tsv"

  [[ -n "$SELECTED_SUBSCRIPTION_ID" ]] ||
    die "Azure CLI returned an empty ID for the selected subscription."
  [[ "${SELECTED_SUBSCRIPTION_ID,,}" == "${subscription_id,,}" ]] ||
    die "Azure CLI returned a different subscription than the one selected."

  printf '\nSelected subscription: %s (%s)\n' \
    "$SELECTED_SUBSCRIPTION_NAME" "$SELECTED_SUBSCRIPTION_ID"
  printf 'Tenant: %s\n' "$SELECTED_TENANT_ID"
}

uses_workspace_scope() {
  [[ "$ASSIGNMENT_SCOPE" == both || "$ASSIGNMENT_SCOPE" == workspace ]]
}

uses_storage_scope() {
  [[ "$ASSIGNMENT_SCOPE" == both || "$ASSIGNMENT_SCOPE" == storage ]]
}

prompt_for_assignment_scope() {
  local selection

  printf '\nAssignment scope:\n'
  printf '  1. Workspaces and storage accounts\n'
  printf '  2. Workspaces only\n'
  printf '  3. Storage accounts only\n'

  while true; do
    if ! read -r -p $'Select assignment scope [1]: ' selection; then
      die "No assignment scope was provided."
    fi

    case "${selection//[[:space:]]/}" in
      "" | 1)
        ASSIGNMENT_SCOPE=both
        return
        ;;
      2)
        ASSIGNMENT_SCOPE=workspace
        return
        ;;
      3)
        ASSIGNMENT_SCOPE=storage
        return
        ;;
      *)
        printf 'Invalid selection. Enter 1, 2, or 3.\n' >&2
        ;;
    esac
  done
}

parse_selection() {
  local raw_selection=$1
  local max_index=$2
  local output_name=$3
  local normalized part start end index
  local -a parts=()
  local -A seen=()
  local -n output_indexes=$output_name

  output_indexes=()
  normalized="${raw_selection//[[:space:]]/}"

  if [[ -z "$normalized" ||
        "$normalized" == ,* ||
        "$normalized" == *, ||
        "$normalized" == *,,* ]]; then
    printf 'Invalid selection. Use a value such as 1-3,5,8.\n' >&2
    return 1
  fi

  IFS=',' read -r -a parts <<< "$normalized"
  for part in "${parts[@]}"; do
    if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      start=$((10#${BASH_REMATCH[1]}))
      end=$((10#${BASH_REMATCH[2]}))
      if ((start < 1 || end > max_index || start > end)); then
        printf 'Invalid range "%s"; valid indexes are 1-%d.\n' \
          "$part" "$max_index" >&2
        return 1
      fi
    elif [[ "$part" =~ ^[0-9]+$ ]]; then
      start=$((10#$part))
      end=$start
      if ((start < 1 || start > max_index)); then
        printf 'Invalid index "%s"; valid indexes are 1-%d.\n' \
          "$part" "$max_index" >&2
        return 1
      fi
    else
      printf 'Invalid item "%s". Use numbers or ascending ranges.\n' \
        "$part" >&2
      return 1
    fi

    for ((index = start; index <= end; index++)); do
      if [[ -z "${seen[$index]+x}" ]]; then
        output_indexes+=("$((index - 1))")
        seen[$index]=1
      fi
    done
  done
}

prompt_for_role_group() {
  local label=$1
  local options_name=$2
  local selected_name=$3
  local selection normalized index
  local -a selected_indexes=()
  local -n role_options=$options_name
  local -n selected_roles=$selected_name

  printf '\nAvailable %s roles:\n\n' "$label"
  for index in "${!role_options[@]}"; do
    printf '  %d. %s\n' "$((index + 1))" "${role_options[$index]}"
  done

  while true; do
    if ! read -r -p \
      "Select $label roles (for example 1,3-4) [all]: " \
      selection; then
      die "No $label role selection was provided."
    fi

    normalized=${selection//[[:space:]]/}
    if [[ -z "$normalized" || "${normalized,,}" == all ]]; then
      selected_roles=("${role_options[@]}")
      return
    fi

    if parse_selection "$selection" "${#role_options[@]}" selected_indexes; then
      selected_roles=()
      for index in "${selected_indexes[@]}"; do
        selected_roles+=("${role_options[$index]}")
      done
      return
    fi
  done
}

prompt_for_roles() {
  if uses_workspace_scope; then
    prompt_for_role_group \
      "workspace" \
      WORKSPACE_ROLES \
      SELECTED_WORKSPACE_ROLES
  fi

  if uses_storage_scope; then
    prompt_for_role_group \
      "storage-account" \
      STORAGE_ROLES \
      SELECTED_STORAGE_ROLES
  fi
}

load_workspaces() {
  local workspace_tsv

  if ! workspace_tsv=$(
    az resource list \
      --only-show-errors \
      --subscription "$SELECTED_SUBSCRIPTION_ID" \
      --resource-type "$AML_RESOURCE_TYPE" \
      --query 'sort_by(@, &name)[].[name, resourceGroup, location, id]' \
      --output tsv
  ); then
    die "Unable to list Azure Machine Learning workspaces."
  fi

  [[ -n "$workspace_tsv" ]] ||
    die "No Azure Machine Learning workspaces found in the current subscription."

  mapfile -t WORKSPACE_ROWS <<< "$workspace_tsv"
}

load_storage_accounts() {
  local storage_tsv

  if ! storage_tsv=$(
    az resource list \
      --only-show-errors \
      --subscription "$SELECTED_SUBSCRIPTION_ID" \
      --resource-type "Microsoft.Storage/storageAccounts" \
      --query 'sort_by(@, &name)[].[name, resourceGroup, location, id]' \
      --output tsv
  ); then
    die "Unable to list storage accounts."
  fi

  [[ -n "$storage_tsv" ]] ||
    die "No storage accounts found in the current subscription."

  mapfile -t STORAGE_ROWS <<< "$storage_tsv"
}

load_resource_catalog() {
  local current=0
  local total=0

  if uses_workspace_scope; then
    total=$((total + 1))
  fi
  if uses_storage_scope; then
    total=$((total + 1))
  fi

  printf '\nLoading Azure resources:\n'
  if uses_workspace_scope; then
    current=$((current + 1))
    printf '  [%d/%d] AML workspaces ...\n' "$current" "$total"
    load_workspaces
    printf '        Found %d workspace(s).\n' "${#WORKSPACE_ROWS[@]}"
  fi

  if uses_storage_scope; then
    current=$((current + 1))
    printf '  [%d/%d] Storage accounts ...\n' "$current" "$total"
    load_storage_accounts
    printf '        Found %d storage account(s).\n' "${#STORAGE_ROWS[@]}"
  fi
}

print_workspaces() {
  local index name resource_group location workspace_id

  printf '\nAvailable Azure Machine Learning workspaces:\n\n'
  printf '%-5s %-32s %-28s %-16s\n' "No." "Workspace" "Resource group" "Location"
  printf '%-5s %-32s %-28s %-16s\n' "----" "--------------------------------" \
    "----------------------------" "----------------"

  for index in "${!WORKSPACE_ROWS[@]}"; do
    IFS=$'\t' read -r name resource_group location workspace_id \
      <<< "${WORKSPACE_ROWS[$index]}"
    printf '%-5d %-32s %-28s %-16s\n' \
      "$((index + 1))" "$name" "$resource_group" "$location"
  done
}

prompt_for_workspaces() {
  local selection

  while true; do
    if ! read -r -p $'\nSelect workspaces (for example 1-3,5): ' selection; then
      die "No workspace selection was provided."
    fi
    if parse_selection \
      "$selection" \
      "${#WORKSPACE_ROWS[@]}" \
      SELECTED_WORKSPACE_INDEXES; then
      break
    fi
  done
}

select_workspaces() {
  local selected_index name resource_group location workspace_id

  for selected_index in "${SELECTED_WORKSPACE_INDEXES[@]}"; do
    IFS=$'\t' read -r name resource_group location workspace_id \
      <<< "${WORKSPACE_ROWS[$selected_index]}"

    SELECTED_WORKSPACE_NAMES+=("$name")
    SELECTED_WORKSPACE_GROUPS+=("$resource_group")
    SELECTED_WORKSPACE_IDS+=("$workspace_id")
  done
}

print_storage_accounts() {
  local index name resource_group location storage_id

  printf '\nAvailable storage accounts:\n\n'
  printf '%-5s %-32s %-28s %-16s\n' \
    "No." "Storage account" "Resource group" "Location"
  printf '%-5s %-32s %-28s %-16s\n' \
    "----" "--------------------------------" \
    "----------------------------" "----------------"

  for index in "${!STORAGE_ROWS[@]}"; do
    IFS=$'\t' read -r name resource_group location storage_id \
      <<< "${STORAGE_ROWS[$index]}"
    printf '%-5d %-32s %-28s %-16s\n' \
      "$((index + 1))" "$name" "$resource_group" "$location"
  done
}

prompt_for_storage_accounts() {
  local selection prompt

  if uses_workspace_scope; then
    printf '\nPress Enter to use the default storage account associated with each selected workspace.\n'
    prompt='Select storage accounts independently (for example 1-3,5) [workspace defaults]: '
  else
    prompt='Select storage accounts (for example 1-3,5): '
  fi

  while true; do
    if ! read -r -p "$prompt" selection; then
      die "No storage-account selection was provided."
    fi

    if [[ -z "${selection//[[:space:]]/}" ]]; then
      if uses_workspace_scope; then
        STORAGE_SELECTION_MODE=workspace-defaults
        SELECTED_STORAGE_INDEXES=()
        return
      fi

      printf 'Select at least one storage account.\n' >&2
      continue
    fi

    if parse_selection \
      "$selection" \
      "${#STORAGE_ROWS[@]}" \
      SELECTED_STORAGE_INDEXES; then
      STORAGE_SELECTION_MODE=manual
      return
    fi
  done
}

select_manual_storage_accounts() {
  local selected_index name resource_group location storage_id storage_key
  local -A seen_storage=()

  for selected_index in "${SELECTED_STORAGE_INDEXES[@]}"; do
    IFS=$'\t' read -r name resource_group location storage_id \
      <<< "${STORAGE_ROWS[$selected_index]}"
    storage_key=${storage_id,,}

    if [[ -z "${seen_storage[$storage_key]+x}" ]]; then
      STORAGE_NAMES+=("$name")
      STORAGE_IDS+=("$storage_id")
      seen_storage[$storage_key]=1
    fi
  done
}

resolve_workspace_default_storage_accounts() {
  local index name workspace_id storage_id storage_name storage_key
  local total=${#SELECTED_WORKSPACE_IDS[@]}
  local -A seen_storage=()

  printf '\nLoading workspace default storage accounts:\n'
  for index in "${!SELECTED_WORKSPACE_IDS[@]}"; do
    name=${SELECTED_WORKSPACE_NAMES[$index]}
    workspace_id=${SELECTED_WORKSPACE_IDS[$index]}
    printf '  [%d/%d] %s ... ' "$((index + 1))" "$total" "$name"

    if ! storage_id=$(
      az resource show \
        --only-show-errors \
        --subscription "$SELECTED_SUBSCRIPTION_ID" \
        --ids "$workspace_id" \
        --query properties.storageAccount \
        --output tsv
    ); then
      die "Unable to read the default storage account for workspace '$name'."
    fi

    [[ -n "$storage_id" && "$storage_id" != "null" ]] ||
      die "Workspace '$name' does not expose an associated default storage account."
    storage_key=${storage_id,,}
    [[ "$storage_key" == */providers/microsoft.storage/storageaccounts/* ]] ||
      die "Workspace '$name' returned an invalid storage account resource ID."

    storage_name=${storage_id##*/}
    printf '%s\n' "$storage_name"

    if [[ -z "${seen_storage[$storage_key]+x}" ]]; then
      STORAGE_NAMES+=("$storage_name")
      STORAGE_IDS+=("$storage_id")
      seen_storage[$storage_key]=1
    fi
  done
}

select_storage_accounts() {
  case "$STORAGE_SELECTION_MODE" in
    manual)
      select_manual_storage_accounts
      ;;
    workspace-defaults)
      resolve_workspace_default_storage_accounts
      ;;
    *)
      die "Unknown storage-account selection mode."
      ;;
  esac
}

resolve_user() {
  local target_account user_tsv

  if ! read -r -p $'\nTarget user (full UPN or object ID): ' target_account; then
    die "No target account was provided."
  fi
  [[ -n "${target_account//[[:space:]]/}" ]] ||
    die "Target account cannot be empty."

  if ! user_tsv=$(
    az ad user show \
      --only-show-errors \
      --subscription "$SELECTED_SUBSCRIPTION_ID" \
      --id "$target_account" \
      --query '[id, userPrincipalName, displayName]' \
      --output tsv
  ); then
    die "Unable to resolve '$target_account' as a Microsoft Entra user. Use a full UPN or user object ID."
  fi

  IFS=$'\t' read -r TARGET_USER_ID TARGET_USER_UPN TARGET_USER_DISPLAY_NAME \
    <<< "$user_tsv"
  [[ -n "$TARGET_USER_ID" ]] ||
    die "Microsoft Entra returned an empty object ID for '$target_account'."
}

load_role_ids() {
  local role role_id index=0
  local -a candidate_roles=()
  local -a roles=()
  local -A seen_roles=()

  if uses_workspace_scope; then
    candidate_roles+=("${SELECTED_WORKSPACE_ROLES[@]}")
  fi
  if uses_storage_scope; then
    candidate_roles+=("${SELECTED_STORAGE_ROLES[@]}")
  fi

  for role in "${candidate_roles[@]}"; do
    if [[ -z "${seen_roles[$role]+x}" ]]; then
      roles+=("$role")
      seen_roles["$role"]=1
    fi
  done

  local total=${#roles[@]}

  printf '\nLoading Azure role definitions:\n'
  for role in "${roles[@]}"; do
    index=$((index + 1))
    printf '  [%d/%d] %s ... ' "$index" "$total" "$role"

    if ! role_id=$(
      az role definition list \
        --only-show-errors \
        --subscription "$SELECTED_SUBSCRIPTION_ID" \
        --name "$role" \
        --query "[?roleType == 'BuiltInRole'] | [0].name" \
        --output tsv
    ); then
      die "Unable to look up Azure role '$role'."
    fi
    [[ -n "$role_id" && "$role_id" != "null" ]] ||
      die "Azure built-in role not found: $role"
    ROLE_IDS["$role"]=$role_id
    printf 'loaded\n'
  done
}

print_plan() {
  local index role

  printf '\nAssignment plan\n'
  printf '  Subscription: %s (%s)\n' \
    "$SELECTED_SUBSCRIPTION_NAME" "$SELECTED_SUBSCRIPTION_ID"
  printf '  Tenant: %s\n' "$SELECTED_TENANT_ID"
  printf '  User: %s <%s>\n' "$TARGET_USER_DISPLAY_NAME" "$TARGET_USER_UPN"
  printf '  Object ID: %s\n' "$TARGET_USER_ID"

  if uses_workspace_scope; then
    printf '\n  Workspaces:\n'
    for index in "${!SELECTED_WORKSPACE_NAMES[@]}"; do
      printf '    - %s (resource group: %s)\n' \
        "${SELECTED_WORKSPACE_NAMES[$index]}" \
        "${SELECTED_WORKSPACE_GROUPS[$index]}"
    done

    printf '\n  Roles at every workspace scope:\n'
    for role in "${SELECTED_WORKSPACE_ROLES[@]}"; do
      printf '    - %s\n' "$role"
    done
  fi

  if uses_storage_scope; then
    printf '\n  Storage accounts:\n'
    for index in "${!STORAGE_NAMES[@]}"; do
      printf '    - %s\n' "${STORAGE_NAMES[$index]}"
    done

    printf '\n  Roles at every selected storage-account scope:\n'
    for role in "${SELECTED_STORAGE_ROLES[@]}"; do
      printf '    - %s\n' "$role"
    done

    printf '\n  Note: duplicate storage accounts are assigned only once.\n'
  fi
  printf '  Only the roles listed above will be assigned.\n'
}

confirm_plan() {
  local confirmation

  if ! read -r -p $'\nCreate these role assignments? [y/N]: ' confirmation; then
    die "Confirmation was not provided."
  fi

  case "${confirmation,,}" in
    y | yes) ;;
    *)
      printf 'No changes were made.\n'
      exit 0
      ;;
  esac
}

assignment_count() {
  local role_id=$1
  local scope=$2

  az role assignment list \
    --only-show-errors \
    --subscription "$SELECTED_SUBSCRIPTION_ID" \
    --assignee-object-id "$TARGET_USER_ID" \
    --role "$role_id" \
    --scope "$scope" \
    --query 'length(@)' \
    --output tsv
}

record_failure() {
  local role=$1
  local scope_label=$2

  FAILED_COUNT=$((FAILED_COUNT + 1))
  FAILURES+=("$role at $scope_label")
}

grant_role() {
  local role=$1
  local scope=$2
  local scope_label=$3
  local progress=$4
  local total=$5
  local role_id existing_count create_error

  role_id=${ROLE_IDS[$role]}
  printf '  [%d/%d] %-31s -> %s ... ' \
    "$progress" "$total" "$role" "$scope_label"

  if ! existing_count=$(assignment_count "$role_id" "$scope"); then
    printf 'FAILED (could not check existing assignments)\n'
    record_failure "$role" "$scope_label"
    return
  fi

  if [[ "$existing_count" =~ ^[0-9]+$ ]] && ((existing_count > 0)); then
    printf 'already exists\n'
    EXISTING_COUNT=$((EXISTING_COUNT + 1))
    return
  fi

  if create_error=$(
    az role assignment create \
      --only-show-errors \
      --subscription "$SELECTED_SUBSCRIPTION_ID" \
      --assignee-object-id "$TARGET_USER_ID" \
      --assignee-principal-type User \
      --role "$role_id" \
      --scope "$scope" \
      --output none 2>&1
  ); then
    printf 'created\n'
    CREATED_COUNT=$((CREATED_COUNT + 1))
    return
  fi

  # A failed response can be ambiguous, so verify before reporting failure.
  if existing_count=$(assignment_count "$role_id" "$scope") &&
    [[ "$existing_count" =~ ^[0-9]+$ ]] &&
    ((existing_count > 0)); then
    printf 'exists after an ambiguous response\n'
    EXISTING_COUNT=$((EXISTING_COUNT + 1))
    return
  fi

  printf 'FAILED\n'
  if [[ -n "$create_error" ]]; then
    printf '%s\n' "$create_error" | sed 's/^/    /' >&2
  fi
  record_failure "$role" "$scope_label"
}

apply_assignments() {
  local index role
  local progress=0
  local total=$((
    ${#SELECTED_WORKSPACE_IDS[@]} * ${#SELECTED_WORKSPACE_ROLES[@]} +
      ${#STORAGE_IDS[@]} * ${#SELECTED_STORAGE_ROLES[@]}
  ))

  printf '\nAuthorization progress: 0/%d\n' "$total"
  if uses_workspace_scope; then
    printf 'Creating workspace role assignments:\n'
    for index in "${!SELECTED_WORKSPACE_IDS[@]}"; do
      for role in "${SELECTED_WORKSPACE_ROLES[@]}"; do
        progress=$((progress + 1))
        grant_role \
          "$role" \
          "${SELECTED_WORKSPACE_IDS[$index]}" \
          "workspace/${SELECTED_WORKSPACE_NAMES[$index]}" \
          "$progress" \
          "$total"
      done
    done
  fi

  if uses_storage_scope; then
    if uses_workspace_scope; then
      printf '\n'
    fi
    printf 'Creating storage role assignments:\n'
    for index in "${!STORAGE_IDS[@]}"; do
      for role in "${SELECTED_STORAGE_ROLES[@]}"; do
        progress=$((progress + 1))
        grant_role \
          "$role" \
          "${STORAGE_IDS[$index]}" \
          "storage/${STORAGE_NAMES[$index]}" \
          "$progress" \
          "$total"
      done
    done
  fi
}

print_summary() {
  printf '\nSummary: %d created, %d already present, %d failed.\n' \
    "$CREATED_COUNT" "$EXISTING_COUNT" "$FAILED_COUNT"

  if ((FAILED_COUNT > 0)); then
    printf 'Failed assignments:\n' >&2
    printf '  - %s\n' "${FAILURES[@]}" >&2
    return 1
  fi
}

main() {
  if (($# > 0)); then
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        die "Unknown argument: $1"
        ;;
    esac
  fi

  require_command az

  select_subscription
  prompt_for_assignment_scope
  load_resource_catalog

  if uses_workspace_scope; then
    print_workspaces
    prompt_for_workspaces
    select_workspaces
  fi

  if uses_storage_scope; then
    print_storage_accounts
    prompt_for_storage_accounts
    select_storage_accounts
  fi

  prompt_for_roles
  resolve_user
  print_plan
  confirm_plan
  load_role_ids
  apply_assignments
  print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
