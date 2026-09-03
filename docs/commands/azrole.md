# `usm azrole`

Interactively assign Azure RBAC roles for Azure Machine Learning workspaces
and their storage accounts to a Microsoft Entra user.

```bash
az login
usm azrole
```

## Workflow

The command guides you through:

1. Selecting an enabled Azure subscription.
2. Choosing workspace, storage-account, or combined assignment scope.
3. Selecting one or more AML workspaces and storage accounts.
4. Selecting the roles to grant.
5. Resolving the target user from a full UPN or object ID.
6. Reviewing and confirming the assignment plan.

Press Enter at the subscription prompt to use the Azure CLI default. The
selected subscription is passed explicitly to every subsequent command, so
`azrole` does not change the global Azure CLI default.

## Roles

Workspace roles:

- `Reader`
- `AzureML Data Scientist`
- `Contributor`

Storage-account roles:

- `Reader`
- `Storage Blob Data Reader`
- `Storage Blob Data Contributor`
- `Storage Table Data Contributor`

Roles can be selected by number or range, such as `1,3-4`. Existing
assignments are detected and skipped.

## Authentication and permissions

`azrole` contains no credentials and does not accept passwords, client
secrets, certificates, SAS values, or access tokens. It uses the existing
Azure CLI login to call Azure Resource Manager and Microsoft Graph.

The signed-in identity must be able to read the selected resources and user,
and must have permission to create role assignments at the selected scopes,
for example through `Owner`, `User Access Administrator`, or
`Role Based Access Control Administrator`.

The only cloud mutation is `az role assignment create`, and it runs only after
the final confirmation prompt.

## Source

[`scripts/azrole.sh`](https://github.com/HSPK/usm/blob/main/scripts/azrole.sh).
