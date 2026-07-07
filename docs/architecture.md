# Architecture

The whole thing is a small Python package plus a JSON manifest. This page
walks through how the pieces fit.

## The two layers

```
┌──────────────────────────────────────────────────────────────┐
│  usmo.cli         click + rich; user-facing UI               │
│                                                              │
│  usmo.core        pure SDK; no click/rich; parses config,    │
│                   manages cache, builds argv, raises typed   │
│                   exceptions                                 │
└──────────────────────────────────────────────────────────────┘
```

`usmo.core` is the part that's actually tested. `usmo.cli` is the
"presentation layer" — it formats output, prompts the user, and
turns SDK exceptions into nice error messages.

Both layers are packages split by responsibility (single-responsibility
principle), so each file stays small and focused:

```
usmo/core/                       usmo/cli/
  constants.py  paths, hooks       output.py      console + progress hooks
  errors.py     typed exceptions   presenters.py  rich tables / help / diffs
  model.py      the Script type    commands.py    built-in handlers + registry
  catalog.py    fetch/cache/diff    runner.py      run a script, map errors
  environments.py  venvs + run      app.py         thin click entry + dispatch
  aliases.py    ~/.local/bin shims
  version.py    installed version
  manifest.py   hashing / bump
```

`usmo/core/__init__.py` re-exports the full public API, so callers (and
tests) keep importing straight from `usmo.core`. The `usm` entry point is
`usmo.cli:cli`, defined in `app.py`, which just parses arguments and routes
to a handler — all logic lives in the focused modules.

## The manifest

[`scripts/_config.json`](https://github.com/HSPK/usm/blob/main/scripts/_config.json)
is the source of truth for which commands exist:

```json
{
  "scripts": {
    "tunnel": {
      "description": "Manage SSH tunnels ...",
      "path": "tunnel.py",
      "requirements": ["click>=8.2.1", "rich>=13.0"],
      "version": "1.3.1",
      "hash": "sha256:0d6d7519bd9a..."
    }
  }
}
```

Per-entry fields:

| Field | Type | Purpose |
| --- | --- | --- |
| `path` | str | Filename under `scripts/` (and what's downloaded). |
| `description` | str | One-line description shown by `usm list`. |
| `requirements` | list[str] | Optional. Installed once into a persistent per-script venv. |
| `python` | str | Optional. Pins the Python version of that venv. |
| `version` | str | Per-script semver. Shown by `usm update`. |
| `hash` | str | `sha256:<hex>` of the file. Maintained by the pre-commit hook. |

## Lifecycle of one invocation

```
$ usm tunnel local 8080 user@host
     │
     ▼
1. usmo.cli loads ~/.cache/usm/scripts/_config.json
     - downloads it from raw.githubusercontent.com/HSPK/usm/main/scripts/
       if the cache is cold (or if --upgrade)
     - may probe for newer versions in the background
     │
     ▼
2. Looks up "tunnel" -> Script(...)
     - if --debug: uses ./scripts/tunnel.py from cwd
     - else: ensures ~/.cache/usm/scripts/tunnel.py exists
       (downloads on cache miss or --upgrade)
     │
     ▼
3. Ensures a Python interpreter via core.ensure_env():
     - .py, no requirements -> the usm interpreter (sys.executable)
     - .py + requirements   -> a persistent venv at
                                 ~/.cache/usm/envs/<name>, built once with
                                 `uv venv` + `uv pip install` and reused
                                 thereafter (rebuilt only when requirements
                                 change or on --upgrade)
     │
     ▼
4. Builds argv via Script.build_argv(path, args, python=<interp>):
     - shell .sh -> bash <path> <args>
     - .py       -> <interp> <path> <args>
     │
     ▼
5. subprocess.run(argv, check=True)
     │
     ▼
6. Exit code propagated as-is. ClickException only on:
     - MissingUv (declared reqs, no uv on PATH)
     - EnvBuildError (venv build failed, e.g. PyPI unreachable)
     - OSError from spawn itself
```

## Why persistent per-script virtualenvs

Each script with `requirements` gets its own venv under
`~/.cache/usm/envs/<name>`:

- The env is built **once** (`uv venv` + `uv pip install`). After that,
  every invocation execs the venv's Python directly — **no network, no
  dependency resolution**. This matters for tools like `clash`/`proxy`
  that you run *to get online*: re-resolving against PyPI on every call
  would fail behind a firewall (`tls handshake eof`).
- A marker file (`.usm-env.json`) records the exact `requirements` +
  `python`. The env is rebuilt only when that spec changes, or when you
  pass `--upgrade`/`-U`.
- Each script's dependency tree is **completely isolated** from your
  system Python, conda, `pyenv`, your project's `.venv`, and from other
  scripts. No `numpy 1.x` vs `numpy 2.x` headaches.
- If the one-time build fails (PyPI blocked), `usm` prints a mirror hint
  (`UV_DEFAULT_INDEX=...`). Once uv has cached the wheels, even the first
  build works offline.

`usm clean` removes both the script cache and all per-script venvs.

## Version + hash hygiene

A pre-commit hook (`dev/bump_version.py`) walks every entry, hashes the
referenced file, and bumps the patch version when drift is detected.
That keeps `_config.json` honest:

- Edit `scripts/tunnel.py`, commit. Hook bumps `tunnel` 1.3.0 → 1.3.1
  (or to whatever level you ask for with `--bump minor|major`).
- The new hash is recorded.
- Users running an older cached copy can pull it with `usm update`.

You can run it manually:

```bash
uv run python dev/bump_version.py            # auto-sync all
uv run python dev/bump_version.py --check    # verify only, exit 1 on drift
uv run python dev/bump_version.py tunnel --bump minor
```

## On-disk layout

```
~/.cache/usm/
├── scripts/
│   ├── _config.json         # last-fetched manifest
│   ├── tunnel.py            # cached script files
│   └── ...
├── envs/                    # persistent per-script virtualenvs
│   └── clash/              # one venv per script with requirements
└── tunnels/                 # state for `usm tunnel`
    ├── 0.json
    ├── 1.json
    └── logs/
        ├── 0.log
        └── 1.log

~/.config/systemd/user/
└── usm-tunnel-0.service     # installed by `usm tunnel enable`

~/Library/LaunchAgents/
└── com.github.hspk.usm.tunnel.0.plist  # macOS autostart equivalent
```

## Release flow

See [Development](development.md) for tagging and PyPI publishing.
