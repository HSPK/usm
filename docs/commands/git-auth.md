# git-auth

`usm git-auth` selects a Git identity and SSH key from the directory that
contains a repository. More-specific directory mappings take precedence over
their parents, so a broad personal workspace can contain a narrower company
workspace.

All managed profiles, imported keys, mappings, generated Git configuration,
and shell helpers live under `~/.config/usm/git/`. Enabling shell integration
adds only a marker-fenced `source` line to the selected shell profile.

## Quick start

```bash
usm git-auth add personal "Your Name" you@example.com
usm git-auth add work "Your Name" you@company.com ~/.ssh/id_ed25519_work

usm git-auth use personal ~/projects
usm git-auth use work ~/projects/company

usm git-auth enable
source ~/.zshrc

cd ~/projects/company/service
usm git-auth status
git config user.email       # you@company.com
```

Git's native `includeIf gitdir:` conditions do the directory matching. The
shell helper appends the generated file to Git's runtime configuration through
`GIT_CONFIG_COUNT`; it does not replace `~/.gitconfig` or
`~/.config/git/config`.

## Profiles

```bash
usm git-auth add <alias> <name> <email> [ssh-key]
usm git-auth add <alias> -i
usm git-auth show <alias>
usm git-auth list
usm git-auth rm <alias>
```

Aliases must start with an ASCII letter and may otherwise contain letters,
numbers, `.`, `_`, and `-`. `add -i` prompts for missing values. An imported
private key is validated, copied to
`profiles/<alias>/identity`, and set to mode `0600`; its passphrase is never
stored by usm. Validation uses `ssh-keygen`, not just the key file header. Key
imports, replacements, removals, and profile updates are staged in a temporary
directory and swapped into place only after every field and rendered Git config
has passed validation.

`rm` refuses to delete a profile that still has directory mappings. Use
`rm <alias> --force` to delete both the profile and its mappings.

Change or remove optional fields with `set` and `unset`:

```bash
usm git-auth set work --key name "New Name"
usm git-auth set work \
  --key email new@company.com \
  --key git.commit.gpgsign true
usm git-auth set work --key ssh.identities-only false
usm git-auth set work --key ssh.option.ProxyJump bastion.example.com

usm git-auth unset work --key git.commit.gpgsign
usm git-auth unset work --key ssh.identity
usm git-auth rename old-work work
```

Supported fields are:

- `name` and `email`;
- `ssh.identity` / `ssh.key` (imports a key);
- `ssh.identities-only`;
- `ssh.option.<OpenSSHOption>`;
- `git.<section>.<name>` for additional simple Git settings.

`user.name`, `user.email`, and `core.sshCommand` are generated from the
profile and cannot be set through `git.*`.

## Directory mappings

```bash
usm git-auth use <alias> [path]
usm git-auth reset [path]
usm git-auth mappings
usm git-auth mappings --alias work
usm git-auth resolve [path]
```

Paths must exist and are stored as both their displayed absolute path and
resolved real path. `use` replaces an existing mapping only when it already
has the same alias; use `--force` to change it.

`reset` removes only a mapping defined exactly on the target directory. It
does not silently remove a parent mapping. If a parent still applies, the
command reports the inherited profile.

A mapping applies to repositories inside the directory. It does not switch
identity between subdirectories of one Git repository: one repository has one
effective profile. `use` therefore rejects a path below an existing repository
root and prints the root that should be bound instead.

Tree mappings match normal `.git` directories and submodules below the tree,
but deliberately do not match `.git/worktrees/<name>`. This prevents a linked
worktree outside a company directory from inheriting the company identity just
because its administrative directory is stored in the main repository. Run
`use` explicitly at a linked worktree root when it needs a profile; git-auth
then stores an exact gitdir rule for that worktree.

`resolve` lists every matching parent-to-child rule, its scope, the selected
profile, and the repository/worktree paths. Use `--json` for machine-readable
diagnostics.

## Enable and disable

```bash
usm git-auth enable                   # detect $SHELL
usm git-auth enable --shell zsh
usm git-auth enable --shell bash
usm git-auth enable --shell all
usm git-auth enable --shell zsh --file ~/.custom-zshrc

usm git-auth disable
usm git-auth disable --shell zsh
```

Shell mode supports bash and zsh, honors zsh's `$ZDOTDIR`, and uses
`.bash_profile` for a macOS bash login shell when appropriate. Re-running `enable` updates the existing
managed block rather than adding duplicates. `disable` removes only that block
and keeps all profiles and mappings. Restart the shell after disabling to
clear the already-exported runtime variables. If the shell profile is a
symbolic link, git-auth updates its target without replacing the link.

Programs launched outside an enabled shell, such as a desktop IDE, do not
inherit its environment. Install the generated file through Git's global
include mechanism when those programs also need git-auth:

```bash
usm git-auth enable --global
usm git-auth disable --global
```

This adds/removes one `include.path` value in the user's global Git config; the
profile data and keys still remain under `~/.config/usm/git/`.

The two modes have intentionally visible precedence semantics:

- shell mode injects command-scope config, so the selected profile overrides a
  repository's local identity;
- global mode behaves like normal global Git config, so `.git/config` may
  override it.

`status` reports the active mode and precedence. `doctor` warns when both modes
are installed because terminal and desktop Git may then resolve local overrides
differently.

## Clone and one-off execution

`git clone` may contact an SSH server before the destination `.git` directory
exists, so a directory condition cannot select the key yet. Use an explicit
profile for that operation:

```bash
usm git-auth clone work git@github.com:company/repository.git
usm git-auth exec work -- git ls-remote git@github.com:company/repository.git
```

`exec` works with any command and adds the selected profile only to that child
process.

## SSH key generation

```bash
usm git-auth keygen work
usm git-auth keygen work --type rsa
usm git-auth keygen work --protect
usm git-auth test work github.com
usm git-auth test work github.com --connect
```

The default is an unencrypted Ed25519 key. `--protect` lets `ssh-keygen` prompt
for a passphrase. Existing managed keys are preserved unless `--force` is
given. Even with `--force`, the old key is not removed until the replacement is
successfully generated and validated.

`test` validates the effective OpenSSH configuration and key fingerprint
without making a network connection. Add `--connect` to perform a real
BatchMode `ssh -T` authentication attempt.

## Inspection and repair

```bash
usm git-auth status [path]
usm git-auth status --json
usm git-auth show work --json
usm git-auth mappings --json
usm git-auth doctor
usm git-auth doctor --fix
usm git-auth sync
```

`status` reports the nearest mapping, selected profile, key fingerprint, and
two sets of Git values with their source files when the target is a repository:

- `actual` is queried without injecting git-auth and is what Git currently
  uses;
- `expected` explicitly loads the generated rules and shows what the selected
  profile should provide.

A warning is shown when they differ, including when integration was configured
but the current shell has not been reloaded.

`doctor` checks dangling/legacy mappings, mappings below a repository root,
missing or invalid keys, private/public key mismatches, keys outside the managed
profile, unsafe permissions, stale per-profile and top-level generated files,
shell marker contents, global include state, and whether Git can parse every
rendered profile. `--fix` repairs permissions, stale generated files, public
keys when possible, and recorded shell/global integration, then runs the audit
again. It does not delete mappings or private keys. `sync` only regenerates
derived Git and shell files after manual inspection or editing and refuses to
compile dangling mappings. When an unfixable error such as a missing profile or
private key exists, `doctor --fix` makes no changes and returns a non-zero exit
code after reporting the complete audit.

## On-disk layout

```text
~/.config/usm/git/
├── config.json
├── mappings.json
├── generated.gitconfig
├── profiles/
│   └── work/
│       ├── profile.json
│       ├── gitconfig
│       ├── identity
│       └── identity.pub
├── shell/
│   ├── git-auth.bash
│   └── git-auth.zsh
└── .lock
```

`profile.json` and `mappings.json` are the source of truth. The `.gitconfig`
and shell files are generated and should not be edited directly.
