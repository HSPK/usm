# `usm rsync`

`rsync` wrapper with sensible defaults + an auto-exclude list. Pass through
to real `rsync` for anything else.

```bash
usm rsync ./project user@host:~/
usm rsync user@host:~/data ./data/
usm rsync -n ./src user@host:~/dev          # dry run
usm rsync --delete ./build user@host:/srv/app/   # mirror mode
usm rsync -i ~/.ssh/id_other -p 2222 ./x user@host:~/y
usm rsync --print-cmd ./scripts user@host:~/    # show resolved command, exit
```

## Defaults applied

```
rsync -avh --human-readable --info=progress2 --partial --partial-dir=.rsync-tmp
      --exclude .git/ --exclude .venv/ --exclude venv/ --exclude node_modules/
      --exclude __pycache__/ --exclude *.pyc --exclude .DS_Store
      --exclude .mypy_cache/ --exclude .pytest_cache/ --exclude .ruff_cache/
```

That gives you resumable transfers + colored progress + the noise files
most projects don't want to copy.

`--no-default-excludes` to skip them, `-e/--exclude PATTERN` (repeatable)
to add more, `--delete` for mirror mode, `-n/--dry-run` for a preview.

`-i KEY` / `-p PORT` set up `ssh` flags via rsync's `-e`. Anything more
exotic — pass extra raw flags after `--`:

```bash
usm rsync --print-cmd -- -P -z --bwlimit=1M ./src user@host:~/
```

## Source

[`scripts/rsync.py`](https://github.com/HSPK/usm/blob/main/scripts/rsync.py)
