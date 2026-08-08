# Design system

Every `usm` command — the launcher and the scripts it runs — renders through
`usmo.ui`, so twenty-seven separate programs read as one tool.

```python
from usmo.ui import Column, detail, ok, table
```

## The vocabulary

| | meaning |
| --- | --- |
| `✓` | something finished |
| `✗` | something failed |
| `!` | worth reading, but not fatal |
| `→` | a step starting, or a transition (`old → new`) |
| `●` `○` | present / absent — cached, mounted, enabled |
| `·` | separator between facts on one line |

Colour carries the same meaning everywhere:

| | used for |
| --- | --- |
| **bold cyan** | an identifier you could type back: a name, an id |
| dim | context you can skip: paths, counts, hints |
| green / yellow / red | the three status levels |

Nothing else is coloured. If a value isn't an identifier, a status or
context, it stays plain.

## Status lines

```python
ui.ok("mounted ~/data")             # ✓ mounted ~/data
ui.warn("SAS expires in 12m")       # ! SAS expires in 12m       (stderr)
ui.fail("mount failed")             # ✗ mount failed             (stderr)
ui.step("refreshing credential")    # → refreshing credential
ui.hint("usm azsync ls to see all") # dim, secondary guidance
ui.title("azsync", subtitle="4 syncs")
```

`fail` and `warn` go to stderr so a command's *result* stays pipeable.

Everything is redacted on the way out — `sig=`, `password=`, `token=`,
`api-key=` and friends become `***`. A credential cannot reach a terminal or
a log just because someone interpolated a URL into a message.

## Tables

A listing row is **one line**. Columns are dropped as the terminal narrows
rather than every column being squeezed into uselessness — the detail view
always has the full picture.

```python
built = ui.table(
    ui.Column("ID", min_width=6),
    ui.Column("Source", hide_below=110),      # first to go
    ui.Column("Destination", min_width=14, ratio=1),
    ui.Column("SAS", justify="right", hide_below=80),
    title="Syncs",
)
```

A table stops at its content, not at the right margin — a rule trailing off
across a wide window reads as a stray line, not as a table. It grows to at
most `ui.MAX_TABLE_WIDTH` (96) columns; past that the `ratio` column takes
the squeeze on its own, so identifiers and states stay readable while only
the long path or description ellipsizes. Pass `max_width=` for a different
cap, or `expand=True` for the rare table that really should fill the width.

`hide_below` is the terminal width under which a column disappears. Build
rows to match with `ui.row_for`, keyed by header:

```python
for job in jobs:
    built.add_row(*ui.row_for(columns, {
        "ID": job.id,
        "Source": ui.shorten_path(job.source),
        "Destination": ui.short_blob_target(job.dest),
        "SAS": ui.compact_duration(remaining),
    }))
```

## Detail views

The inverse: values **wrap** rather than truncate, because silently cutting a
path in half is worse than a second line. Group with `SECTION`.

```python
ui.print_detail([
    ("source", ui.shorten_path(job.source)),
    ("destination", ui.short_blob_target(job.dest)),
    ui.SECTION,
    ("status", ui.status("ok", "watching")),
    ("last sync", "ok, 2m ago"),
])
```

```text
source       ~/data
destination  acct/research/exports

status       watching
last sync    ok, 2m ago
```

## Formatting

Two duration formats, deliberately:

| | | for |
| --- | --- | --- |
| `human_duration(9000)` | `2h30m` | detail views, where width is free |
| `compact_duration(9000)` | `2h` | table columns — never more than 4 chars |

Using the precise one in a column is what makes a row wrap to three lines.

| helper | |
| --- | --- |
| `human_bytes` | `2.0KiB`, `5.0MiB` |
| `shorten_path` | `/home/first.last@example.com/data` → `~/data` |
| `short_blob_target` | `https://acct.blob.core.windows.net/c/a?sas` → `acct/c/a` |
| `elide(text, n)` | trims, keeping the tail (paths are known by their end) |
| `plural(2, "sync")` | `2 syncs` |
| `joined(a, b, c)` | `a · b · c` |

## Adopting it in a script

Declare `usmo` in the script's requirements:

```json
"myscript": {
  "path": "myscript.py",
  "requirements": ["click>=8.2.1", "usmo>=0.11.0"]
}
```

Then import it like any library. `usm --debug myscript` installs the usmo
checkout you're standing in *editably*, so changes to the design system take
effect immediately — no release, no venv rebuild.

## Cost

`usmo.ui` imports only the standard library; `rich` is pulled in the first
time something is actually rendered. A command that prints two status lines
never pays for the table engine. `Column` is a plain class rather than a
dataclass for the same reason — `dataclasses` alone costs ~9ms, and this
module is imported on every `usm` invocation.

## Source

[`src/usmo/ui.py`](https://github.com/HSPK/usm/blob/main/src/usmo/ui.py),
tested at 100% line coverage in
[`tests/test_ui.py`](https://github.com/HSPK/usm/blob/main/tests/test_ui.py) —
including that the glyphs don't drift, that secrets never render, that a row
fits terminals from 50 to 200 columns, and that importing the module doesn't
drag in rich.
