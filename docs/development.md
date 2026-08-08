# Development

## Local setup

```bash
git clone https://github.com/HSPK/usm.git
cd usm
uv sync                                 # installs runtime + dev deps
uv tool install --force .               # makes `usm` point at this checkout
```

After `uv tool install --force .`, your `usm` command runs your local
edits. Run the released version again with `uv tool install --force usmo`.

## Running tests

```bash
uv run pytest -q
```

1260+ tests covering `usmo.core`, `scripts/openai_proxy.py`,
`scripts/azsync.py`, `scripts/usm_azure.py` and `scripts/blobmount.py`.
Tests live
under `tests/` and are wired up so scripts in `scripts/` can be imported
directly (see `tests/conftest.py`).

## Linting & formatting

The project uses [Ruff](https://docs.astral.sh/ruff/):

```bash
uv run ruff format .
uv run ruff check --fix .
```

Both run automatically via `pre-commit`:

```bash
pre-commit install
pre-commit run --all-files
```

## Adding a new command

1. Drop a file into `scripts/` (`.sh` or `.py`).
2. Add an entry in
   [`scripts/_config.json`](https://github.com/HSPK/usm/blob/main/scripts/_config.json):
   ```json
   {
     "scripts": {
       "myscript": {
         "description": "What this does in one line.",
         "path": "myscript.py",
         "requirements": ["click>=8.2.1"]
       }
     }
   }
   ```
   Only `path` is required; `description` shows up in `usm list`;
   `requirements` are installed once into a persistent per-script venv.

   Add `usmo` to `requirements` to use the shared
   [design system](design-system.md) (`from usmo.ui import ok, table, ...`)
   so the command matches the rest of the family. In `--debug`, usm installs
   the checkout you're standing in *editably*, so UI changes are live.

   A Python script can share code with another by declaring `modules`:

   ```json
   "myscript": {
     "path": "myscript.py",
     "modules": ["usm_azure.py"],
     "requirements": ["click>=8.2.1"]
   }
   ```

   Listed modules are downloaded into the same cache directory, so a plain
   `import usm_azure` works. Their bytes are folded into the entry's hash, so
   editing a shared module bumps every script that declares it.
3. Commit. The pre-commit hook fills in `version` and `hash`.
4. Test it: `usm --debug myscript`.
5. Add a docs page under `docs/commands/myscript.md` and link it from
   `mkdocs.yml`'s nav.

## Editing an existing script

1. Edit the file under `scripts/`.
2. Try it locally with `usm --debug <name>`.
3. Commit. Pre-commit auto-bumps the patch version + records the new hash.
4. If you want a minor/major bump, run manually:
   ```bash
   uv run python dev/bump_version.py <name> --bump minor
   ```

## Building the docs locally

```bash
uv sync --group docs
uv run mkdocs serve              # live-reloading at http://127.0.0.1:8000
uv run mkdocs build --strict     # one-shot build, fails on broken links
```

## Release flow

Tag a commit on `main` with `vMAJOR.MINOR.PATCH`:

```bash
git tag -a v0.3.1 -m "v0.3.1"
git push origin v0.3.1
```

That triggers `.github/workflows/release.yml`, which:

1. Builds the sdist + wheel with `uv build` (version comes from the tag
   via `hatch-vcs`).
2. Uploads them as an artifact.
3. Creates a GitHub Release with auto-generated notes from the commit
   range, attaching the artifacts.
4. Publishes to PyPI via Trusted Publishing (no secrets needed; the
   association is set up under the `release` environment on the repo).

Pre-releases (tags containing `-`, e.g. `v0.4.0-rc1`) skip PyPI and
mark the GitHub Release as prerelease.

## Repo layout

```
.
├── src/usmo/                  # the installable package
│   ├── cli/                   # click + rich frontend (app, presenters, …)
│   ├── core/                  # pure SDK (catalog, environments, …)
│   └── _version.py            # written by hatch-vcs at build
├── scripts/                   # everything served via the CLI
│   ├── _config.json
│   ├── tunnel.py
│   ├── init.py
│   ├── ...
│   └── install.sh             # bootstrap script (not a usm subcommand)
├── tests/                     # pytest suite
├── dev/bump_version.py        # pre-commit hook & manual release helper
├── docs/                      # mkdocs site (this site!)
├── mkdocs.yml
├── .github/workflows/
│   ├── release.yml            # tag -> build + GitHub Release + PyPI
│   └── docs.yml               # main -> mkdocs build + GitHub Pages
└── pyproject.toml
```
