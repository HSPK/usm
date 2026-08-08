"""Tests for :mod:`usmo.ui` — the design system every usm command shares.

Two things are being protected here. First the vocabulary: if ``ok()`` stops
printing ``✓``, or a secret stops being redacted, every command in the family
changes at once. Second the layout guarantees: a listing row is one line and
fits the terminal, a detail value wraps rather than being cut.
"""

from __future__ import annotations

import io

import pytest

from usmo import ui


def render(renderable, width: int = 80) -> str:
    from rich.console import Console

    console = Console(width=width, file=io.StringIO(), no_color=True)
    console.print(renderable)
    return console.file.getvalue()


@pytest.fixture
def captured(monkeypatch):
    """Capture whatever the shared consoles print, without colour."""
    from rich.console import Console

    buffer = io.StringIO()
    console = Console(width=100, file=buffer, no_color=True)
    monkeypatch.setattr(ui, "_console", console)
    monkeypatch.setattr(ui, "_err_console", console)
    return buffer


# --- Vocabulary ------------------------------------------------------------


class TestStatusVocabulary:
    """The glyphs are the family's signature; pin them."""

    def test_glyphs(self):
        assert (ui.OK, ui.FAIL, ui.WARN, ui.STEP) == ("✓", "✗", "!", "→")
        assert (ui.CACHED, ui.MISSING) == ("●", "○")
        assert ui.DOT == "·"

    def test_ok(self, captured):
        ui.ok("done")
        assert captured.getvalue().strip() == "✓ done"

    def test_fail(self, captured):
        ui.fail("boom")
        assert captured.getvalue().strip() == "✗ boom"

    def test_warn(self, captured):
        ui.warn("careful")
        assert captured.getvalue().strip() == "! careful"

    def test_step(self, captured):
        ui.step("working")
        assert captured.getvalue().strip() == "→ working"

    def test_info_is_unadorned(self, captured):
        ui.info("plain")
        assert captured.getvalue().strip() == "plain"

    def test_hint(self, captured):
        ui.hint("try this")
        assert captured.getvalue().strip() == "try this"

    def test_title_with_subtitle(self, captured):
        ui.title("azsync", subtitle="4 syncs")
        out = captured.getvalue().strip()
        assert out.startswith("azsync") and "4 syncs" in out

    def test_failures_and_warnings_go_to_stderr(self, monkeypatch):
        from rich.console import Console

        out, errout = io.StringIO(), io.StringIO()
        monkeypatch.setattr(ui, "_console", Console(file=out, no_color=True))
        monkeypatch.setattr(ui, "_err_console", Console(file=errout, no_color=True))
        ui.ok("good")
        ui.fail("bad")
        ui.warn("hmm")
        assert "good" in out.getvalue() and "bad" not in out.getvalue()
        assert "bad" in errout.getvalue() and "hmm" in errout.getvalue()

    def test_status_words_are_levelled(self):
        assert "green" in ui.status("ok", "watching")
        assert "yellow" in ui.status("warn", "backoff")
        assert "red" in ui.status("fail", "failed")
        assert "dim" in ui.status("anything-else", "-")

    def test_state_glyphs(self):
        assert ui.CACHED in ui.state(True) and "green" in ui.state(True)
        assert ui.MISSING in ui.state(False) and "dim" in ui.state(False)

    def test_state_accepts_custom_glyphs(self):
        assert "on" in ui.state(True, yes="on", no="off")

    def test_identifier_and_muted(self):
        assert ui.identifier("name") == "[bold cyan]name[/bold cyan]"
        assert ui.muted("ctx") == "[dim]ctx[/dim]"

    def test_joined_uses_the_family_separator(self):
        assert ui.joined("a", "b", "c") == "a · b · c"

    def test_joined_drops_empties(self):
        assert ui.joined("a", "", None or "", "b") == "a · b"

    def test_legend(self):
        assert ui.legend(("●", "cached"), ("○", "missing")) == "● cached  ○ missing"


# --- Redaction -------------------------------------------------------------


class TestRedaction:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("sig=abc", "sig=***"),
            ("?sv=1&sig=abc&sp=r", "?sv=1&sig=***&sp=r"),
            ("https://x/y?sig=abc", "https://x/y?sig=***"),
            ("password=hunter2", "password=***"),
            ("token=abc123", "token=***"),
            ("api_key=zzz", "api_key=***"),
            ("api-key=zzz", "api-key=***"),
            ("SECRET=zzz", "SECRET=***"),
        ],
    )
    def test_patterns(self, raw, expected):
        assert ui.redact(raw) == expected

    def test_leaves_ordinary_text_alone(self):
        assert ui.redact("account-name: acct") == "account-name: acct"
        assert ui.redact("version=1.2.3") == "version=1.2.3"

    def test_handles_none_and_non_strings(self):
        assert ui.redact(None) == ""
        assert ui.redact(42) == "42"

    def test_every_status_helper_redacts(self, captured):
        for fn in (ui.ok, ui.fail, ui.warn, ui.step, ui.info, ui.hint):
            fn("leaked sig=SECRET123")
        assert "SECRET123" not in captured.getvalue()
        assert captured.getvalue().count("sig=***") == 6

    def test_title_redacts(self, captured):
        ui.title("x", subtitle="sig=SECRET123")
        assert "SECRET123" not in captured.getvalue()

    def test_detail_values_are_redacted(self):
        out = render(ui.detail([("url", "https://x?sig=SECRET123")]))
        assert "SECRET123" not in out and "sig=***" in out


# --- Formatting ------------------------------------------------------------


class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "-"),
            (0, "0B"),
            (512, "512B"),
            (2048, "2.0KiB"),
            (5 * 1024**2, "5.0MiB"),
            (3 * 1024**3, "3.0GiB"),
            (2 * 1024**4, "2.0TiB"),
        ],
    )
    def test_human_bytes(self, value, expected):
        assert ui.human_bytes(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [(None, "-"), (5, "5s"), (65, "1m05s"), (3600, "1h00m"), (90000, "1d01h")],
    )
    def test_human_duration(self, value, expected):
        assert ui.human_duration(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "-"),
            (0, "0s"),
            (59, "59s"),
            (60, "1m"),
            (3599, "59m"),
            (3600, "1h"),
            (86400, "1d"),
            (-5, "0s"),
        ],
    )
    def test_compact_duration(self, value, expected):
        assert ui.compact_duration(value) == expected

    def test_compact_duration_fits_a_column(self):
        for secs in (0, 59, 3600, 86400, 86400 * 999):
            assert len(ui.compact_duration(secs)) <= 4

    def test_shorten_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ui.Path, "home", classmethod(lambda cls: tmp_path))
        assert ui.shorten_path(tmp_path) == "~"
        assert ui.shorten_path(tmp_path / "a" / "b") == "~/a/b"
        assert ui.shorten_path("/mnt/data") == "/mnt/data"

    def test_shorten_path_does_not_mangle_a_sibling(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ui.Path, "home", classmethod(lambda cls: tmp_path))
        sibling = str(tmp_path) + "-other/x"
        assert ui.shorten_path(sibling) == sibling

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://acct.blob.core.windows.net/c/a/b", "acct/c/a/b"),
            ("https://acct.blob.core.windows.net/c", "acct/c"),
            ("https://acct.blob.core.windows.net/", "acct"),
            ("https://acct.blob.core.windows.net/c?sv=1&sig=x", "acct/c"),
            ("not a url", "not a url"),
            ("", ""),
        ],
    )
    def test_short_blob_target(self, url, expected):
        assert ui.short_blob_target(url) == expected

    def test_elide_keeps_the_tail_by_default(self):
        assert ui.elide("abcdefghij", 5) == "…ghij"

    def test_elide_head_mode(self):
        assert ui.elide("abcdefghij", 5, keep="head") == "abcd…"

    def test_elide_never_exceeds_the_limit(self):
        for limit in range(1, 12):
            assert len(ui.elide("x" * 40, limit)) <= limit

    def test_elide_leaves_short_text(self):
        assert ui.elide("abc", 10) == "abc"
        assert ui.elide("abc", 0) == "abc"

    @pytest.mark.parametrize(
        "count,expected", [(0, "0 syncs"), (1, "1 sync"), (2, "2 syncs")]
    )
    def test_plural(self, count, expected):
        assert ui.plural(count, "sync") == expected

    def test_plural_irregular(self):
        assert ui.plural(2, "entry", "entries") == "2 entries"


# --- Tables ----------------------------------------------------------------


class TestTables:
    def _wide(self):
        built = ui.table(
            ui.Column("ID", min_width=4),
            ui.Column("Path", min_width=8, ratio=1),
            ui.Column("State", min_width=5),
            terminal_width=120,
        )
        built.add_row("a" * 60, "b" * 120, "c" * 40)
        return built

    def test_rows_stay_on_one_line(self):
        body = [ln for ln in render(self._wide(), 60).splitlines() if ln.strip()]
        assert len(body) == 3  # header, rule, one row

    @pytest.mark.parametrize("width", [50, 70, 80, 120, 200])
    def test_never_exceeds_the_terminal(self, width):
        for line in render(self._wide(), width).splitlines():
            assert len(line) <= width

    def test_columns_are_dropped_below_their_threshold(self):
        columns = [
            ui.Column("Always"),
            ui.Column("Wide", hide_below=100),
        ]
        assert len(ui.table(*columns, terminal_width=120).columns) == 2
        assert len(ui.table(*columns, terminal_width=80).columns) == 1

    def test_visible_columns_matches_the_table(self):
        columns = [ui.Column("A"), ui.Column("B", hide_below=100)]
        assert [c.header for c in ui.visible_columns(columns, 80)] == ["A"]
        assert [c.header for c in ui.visible_columns(columns, 120)] == ["A", "B"]

    def test_row_for_drops_the_same_values(self):
        columns = [ui.Column("A"), ui.Column("B", hide_below=100)]
        values = {"A": "1", "B": "2"}
        assert ui.row_for(columns, values, 80) == ["1"]
        assert ui.row_for(columns, values, 120) == ["1", "2"]

    def test_row_for_tolerates_missing_values(self):
        assert ui.row_for([ui.Column("A")], {}, 120) == [""]

    def test_accepts_plain_headers_and_tuples(self):
        built = ui.table("A", ("B", {"justify": "right"}), terminal_width=100)
        assert built.columns[1].justify == "right"
        assert all(c.no_wrap for c in built.columns)

    def test_column_options_are_forwarded(self):
        built = ui.table(
            ui.Column("A", justify="center", style="bold", min_width=7),
            terminal_width=100,
        )
        column = built.columns[0]
        assert column.justify == "center" and column.style == "bold"
        assert column.min_width == 7 and column.no_wrap is True

    def test_a_wrapping_column_is_opt_in(self):
        built = ui.table(ui.Column("A", wrap=True), terminal_width=100)
        assert built.columns[0].no_wrap is False
        assert built.columns[0].overflow == "fold"

    def test_title_is_rendered(self):
        built = ui.table("A", title="Commands", terminal_width=100)
        assert "Commands" in render(built)

    def test_column_repr_is_useful(self):
        assert "hide_below" in repr(ui.Column("A", hide_below=90))


class TestDetail:
    def test_sections_become_blank_rows(self):
        out = render(ui.detail([("a", "1"), ui.SECTION, ("b", "2")]))
        lines = out.splitlines()
        assert any("a" in ln for ln in lines) and any("b" in ln for ln in lines)
        assert any(not ln.strip() for ln in lines)

    def test_values_wrap_instead_of_truncating(self):
        out = render(ui.detail([("key", "v" * 150)]), 60)
        assert "…" not in out, "a detail value must never be silently cut"
        assert out.count("v") == 150

    def test_handles_none(self):
        assert "key" in render(ui.detail([("key", None)]))

    def test_keys_are_not_wrapped(self):
        built = ui.detail([("a", "1")])
        assert built.columns[0].no_wrap is True


# --- Import cost -----------------------------------------------------------


class TestImportCost:
    """Every usm invocation imports this module; keep it cheap."""

    def _modules_after(self, code: str) -> set[str]:
        import json
        import subprocess
        import sys

        probe = (
            f"import sys\n{code}\nimport json; print(json.dumps(sorted(sys.modules)))"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        return set(json.loads(out.stdout))

    def test_rich_is_not_imported_until_something_prints(self):
        loaded = self._modules_after("from usmo import ui")
        assert "rich" not in loaded

    def test_dataclasses_is_not_imported(self):
        """Column is a plain class precisely to avoid this ~9ms import."""
        loaded = self._modules_after("from usmo import ui; ui.Column('x')")
        assert "dataclasses" not in loaded

    def test_the_sdk_is_not_dragged_in(self):
        loaded = self._modules_after("from usmo import ui")
        for module in ("usmo.core.catalog", "usmo.core.aliases", "requests"):
            assert module not in loaded

    def test_rendering_still_works_after_the_lazy_import(self):
        self._modules_after(
            "from usmo import ui\n"
            "t = ui.table('A', terminal_width=80)\n"
            "t.add_row('x')\n"
            "assert 'rich' in sys.modules\n"
        )


# --- The scripts share the same system -------------------------------------


class TestSharedWithScripts:
    def test_usm_azure_reexports_the_design_system(self):
        import usm_azure

        assert usm_azure.redact is ui.redact
        assert usm_azure.human_bytes is ui.human_bytes
        assert usm_azure.compact_duration is ui.compact_duration
        assert usm_azure.shorten_path is ui.shorten_path
        assert usm_azure.SECTION is ui.SECTION
        assert usm_azure.new_table is ui.table
        assert usm_azure.kv_table is ui.detail

    def test_the_cli_uses_the_same_glyphs(self):
        from usmo.cli import presenters

        source = open(presenters.__file__).read()
        assert "ui.state(" in source, "listings should use the shared glyph helper"
        assert "ui.table(" in source, "listings should use the shared table"


class TestConsoleHelpers:
    def test_consoles_are_created_once(self, monkeypatch):
        monkeypatch.setattr(ui, "_console", None)
        monkeypatch.setattr(ui, "_err_console", None)
        assert ui.console() is ui.console()
        assert ui.err() is ui.err()
        assert ui.err() is not ui.console()

    def test_err_console_targets_stderr(self, monkeypatch):
        monkeypatch.setattr(ui, "_err_console", None)
        assert ui.err().stderr is True

    def test_width_follows_the_terminal(self, monkeypatch):
        monkeypatch.setattr(ui, "_console", None)
        monkeypatch.setenv("COLUMNS", "137")
        assert ui.width() == 137

    def test_print_goes_through_the_shared_console(self, captured):
        ui.print("hello")
        assert "hello" in captured.getvalue()

    def test_rule(self, captured):
        ui.rule("section")
        assert "section" in captured.getvalue()
        ui.rule()

    def test_print_detail(self, captured):
        ui.print_detail([("key", "value")])
        out = captured.getvalue()
        assert "key" in out and "value" in out

    def test_print_table_with_footer(self, captured):
        built = ui.table("A", terminal_width=80)
        built.add_row("x")
        ui.print_table(built, footer="a note")
        out = captured.getvalue()
        assert "x" in out and "a note" in out

    def test_print_table_without_footer(self, captured):
        built = ui.table("A", terminal_width=80)
        built.add_row("x")
        ui.print_table(built)
        assert "a note" not in captured.getvalue()

    def test_public_surface_is_declared(self):
        for name in ui.__all__:
            assert hasattr(ui, name), f"__all__ lists a missing name: {name}"
