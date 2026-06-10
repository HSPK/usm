# `usm secret`

Encrypted local env store. Stash API keys / tokens / connection strings
once, inject them into the shell or a child process when you need them.

```bash
usm secret set OPENAI_API_KEY=sk-...
usm secret set -g prod DB_URL=postgres://...
usm secret ls                              # keys only; values masked
usm secret ls --reveal                     # show values
usm secret get OPENAI_API_KEY              # print one value
usm secret rm HF_TOKEN
eval "$(usm secret export prod)"           # export into current shell
usm secret run prod -- python app.py       # spawn with secrets injected
```

## How it stores things

| Path | What |
| --- | --- |
| `~/.config/usm/secret.key` | Fernet key (auto-generated on first use, chmod 600) |
| `~/.config/usm/secrets.json.enc` | The encrypted store (chmod 600) |

Encryption: `cryptography.Fernet` (AES-128 CBC + HMAC-SHA256). The key is
**not** wrapped with a passphrase — security is "files on your laptop with
file permissions". For real-world threat models that need more than that,
use something heavier like `age` / `sops` / a cloud KMS.

**Back up the key file if you don't want to lose the secrets.** Lose the
key, the store is gone.

## Groups

Every secret lives in a group (default: `default`). Groups let you keep
"dev/staging/prod" — or "openai/anthropic" — separated. Pass `-g GROUP`
to `set` / `ls` / `rm`; `export` and `run` take the group as a positional
argument.

```bash
usm secret set -g aws AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
usm secret set -g openai OPENAI_API_KEY=...
usm secret run openai -- python ask.py
```

## `export` vs `run`

- **`export GROUP`** prints `export KEY='VAL'` lines to stdout. Use with
  `eval "$(usm secret export GROUP)"` to merge into your current shell —
  the secrets stay in this shell's env until you exit it.
- **`run GROUP -- cmd`** `execvp`s `cmd` with the secrets in its env. The
  parent shell never sees them. **Prefer this** when running a single
  command — it's strictly safer.

## Source

[`scripts/secret.py`](https://github.com/HSPK/usm/blob/main/scripts/secret.py)
