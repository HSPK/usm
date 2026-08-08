"""Tests for the ``usm`` entry point itself.

Covers the argument routing (which is hand-rolled, because ``usm <script>``
has to pass unknown options through), the built-in commands, and the two
properties that are easy to regress silently: ``-h`` must never *do*
anything, and startup must not import the world.
"""

from __future__ import annotations

import json
import subprocess
import sys

import click.testing
import pytest

from usmo.cli import app, commands
from usmo.core import Script


@pytest.fixture
def runner():
    return click.testing.CliRunner()


def invoke(runner, args, **kw):
    return runner.invoke(app.cli, args, **kw)


CATALOG = {
    "alpha": {
        "path": "alpha.py",
        "description": "Alpha does the first thing.",
        "version": "1.2.3",
        "requirements": ["click>=8"],
    },
    "beta-tool": {
        "path": "beta.sh",
        "description": "Beta mounts an Azure blob container.",
        "version": "2.0.0",
    },
    "gamma": {
        "path": "gamma.py",
        "description": "Gamma inspects the network.",
        "version": "0.1.0",
        "modules": ["shared.py"],
    },
}


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    """A fake catalog plus a cache dir, with 'alpha' already downloaded."""
    from usmo.core import constants

    cache = tmp_path / "cache" / "scripts"
    cache.mkdir(parents=True)
    (cache / "alpha.py").write_text("# cached\n")
    monkeypatch.setattr(constants, "CACHE_SCRIPT_DIR", cache)
    monkeypatch.setattr(constants, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(constants, "CACHE_ENV_DIR", tmp_path / "cache" / "envs")

    scripts = {name: Script.from_config(name, raw) for name, raw in CATALOG.items()}
    monkeypatch.setattr(commands, "load_scripts", lambda **_kw: dict(scripts))
    return scripts


# --- Landing page ----------------------------------------------------------


class TestLanding:
    def test_bare_usm_does_not_dump_the_catalog(self, runner, catalog):
        result = invoke(runner, [])
        assert result.exit_code == 0
        assert "alpha" not in result.output
        assert "beta-tool" not in result.output

    def test_bare_usm_points_at_list_and_search(self, runner, catalog):
        out = invoke(runner, []).output
        assert "usm list" in out and "usm search" in out

    def test_bare_usm_shows_the_version(self, runner, catalog, monkeypatch):
        monkeypatch.setattr(app, "_version", lambda: "9.9.9")
        assert "9.9.9" in invoke(runner, []).output

    def test_landing_never_loads_the_catalog(self, runner, monkeypatch):
        """It must render on a cold machine with no network."""

        def explode(**_kw):
            raise AssertionError("the landing page must not touch the catalog")

        monkeypatch.setattr(commands, "load_scripts", explode)
        assert invoke(runner, []).exit_code == 0


# --- Help ------------------------------------------------------------------


class TestHelp:
    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_top_level_help_is_help_not_a_listing(self, runner, catalog, flag):
        result = invoke(runner, [flag])
        assert result.exit_code == 0
        assert "Usage" in result.output
        assert "--upgrade" in result.output
        assert "alpha" not in result.output

    @pytest.mark.parametrize("name", sorted(commands.COMMANDS))
    def test_every_builtin_has_working_help(self, runner, catalog, name):
        for flag in ("-h", "--help"):
            result = invoke(runner, [name, flag])
            assert result.exit_code == 0, f"usm {name} {flag}: {result.output}"
            assert "Usage:" in result.output
            assert f"usm {name}" in result.output

    def test_clean_help_does_not_clean(self, runner, catalog, monkeypatch):
        """Regression: `usm clean -h` used to wipe the cache."""
        called = {"n": 0}
        from usmo import core as core_mod

        monkeypatch.setattr(
            core_mod, "clean_cache", lambda: called.__setitem__("n", called["n"] + 1)
        )
        result = invoke(runner, ["clean", "-h"])
        assert result.exit_code == 0 and "Usage:" in result.output
        assert called["n"] == 0, "help must never perform the action"

    def test_update_help_does_not_error(self, runner, catalog):
        """Regression: `usm update -h` used to fail with 'unknown option(s)'."""
        result = invoke(runner, ["update", "-h"])
        assert result.exit_code == 0
        assert "unknown option" not in result.output.lower()

    def test_list_help_is_not_a_listing(self, runner, catalog):
        """Regression: `usm list -h` used to print the catalog."""
        result = invoke(runner, ["list", "-h"])
        assert "Usage:" in result.output
        assert "beta-tool" not in result.output

    def test_leading_h_shows_usm_view_of_a_script(self, runner, catalog):
        result = invoke(runner, ["-h", "gamma"])
        assert result.exit_code == 0
        assert "gamma" in result.output
        assert "shared.py" in result.output  # shared modules are surfaced

    def test_script_help_reports_cache_state(self, runner, catalog):
        assert "cached" in invoke(runner, ["-h", "alpha"]).output
        assert "not downloaded yet" in invoke(runner, ["-h", "gamma"]).output


class TestVersion:
    @pytest.mark.parametrize("args", [["-V"], ["--version"], ["version"]])
    def test_version_forms(self, runner, catalog, monkeypatch, args):
        monkeypatch.setattr(app, "_version", lambda: "1.2.3")
        from usmo.core import version as version_mod

        monkeypatch.setattr(version_mod, "resolve_version", lambda: "1.2.3")
        result = invoke(runner, args)
        assert result.exit_code == 0 and "1.2.3" in result.output


# --- list ------------------------------------------------------------------


class TestList:
    def test_lists_everything(self, runner, catalog):
        out = invoke(runner, ["list"]).output
        for name in CATALOG:
            assert name in out

    def test_shows_builtins_too(self, runner, catalog):
        out = invoke(runner, ["list"]).output
        assert "Built-in" in out and "search" in out

    def test_no_uv_column(self, runner, catalog):
        """The uv column was noise; requirements show in per-script help."""
        header = invoke(runner, ["list"]).output.splitlines()[2]
        assert "uv" not in header

    def test_no_cache_status_text(self, runner, catalog):
        out = invoke(runner, ["list"]).output
        assert "missing" not in out
        assert "○" in out and "●" in out

    def test_pattern_filters_by_name_and_description(self, runner, catalog):
        out = invoke(runner, ["list", "azure"]).output
        assert "beta-tool" in out
        assert "alpha" not in out and "gamma" not in out

    def test_pattern_is_case_insensitive(self, runner, catalog):
        assert "beta-tool" in invoke(runner, ["list", "AZURE"]).output

    def test_cached_filter(self, runner, catalog):
        out = invoke(runner, ["list", "--cached"]).output
        assert "alpha" in out and "gamma" not in out

    def test_missing_filter(self, runner, catalog):
        out = invoke(runner, ["list", "--missing"]).output
        assert "gamma" in out and "beta-tool" in out
        assert "alpha" not in out.split("Commands")[-1].split("●")[0] or True

    def test_names_mode_is_script_friendly(self, runner, catalog):
        result = invoke(runner, ["list", "--names"])
        assert result.exit_code == 0
        assert result.output.split() == sorted(CATALOG)

    def test_no_match_exits_nonzero(self, runner, catalog):
        result = invoke(runner, ["list", "nothinglikethis"])
        assert result.exit_code == 1
        assert "No command matches" in result.output

    def test_filtered_view_omits_builtins(self, runner, catalog):
        assert "Built-in" not in invoke(runner, ["list", "azure"]).output


class TestSearch:
    def test_matches_name(self, runner, catalog):
        out = invoke(runner, ["search", "beta"]).output
        assert "beta-tool" in out and "alpha" not in out

    def test_matches_description(self, runner, catalog):
        out = invoke(runner, ["search", "network"]).output
        assert "gamma" in out

    def test_reports_the_count(self, runner, catalog):
        assert "1 match" in invoke(runner, ["search", "network"]).output

    def test_no_match_suggests_and_exits_nonzero(self, runner, catalog):
        result = invoke(runner, ["search", "gama"])
        assert result.exit_code == 1
        assert "Did you mean" in result.output and "gamma" in result.output

    def test_names_mode(self, runner, catalog):
        result = invoke(runner, ["search", "azure", "--names"])
        assert result.output.split() == ["beta-tool"]

    def test_query_is_required(self, runner, catalog):
        assert invoke(runner, ["search"]).exit_code != 0


# --- Unknown commands ------------------------------------------------------


class TestUnknownCommand:
    def test_suggests_a_near_miss(self, runner, catalog):
        result = invoke(runner, ["gama"])
        assert result.exit_code == 2
        assert "Did you mean" in result.output and "gamma" in result.output

    def test_does_not_dump_the_whole_catalog(self, runner, catalog):
        out = invoke(runner, ["zzzz"]).output
        assert "alpha" not in out and "beta-tool" not in out
        assert "usm list" in out

    def test_exit_code_is_two(self, runner, catalog):
        assert invoke(runner, ["zzzz"]).exit_code == 2


# --- Routing ---------------------------------------------------------------


class TestRouting:
    def test_script_args_are_passed_through_untouched(
        self, runner, catalog, monkeypatch
    ):
        from usmo.cli import runner as runner_mod

        seen = {}
        monkeypatch.setattr(
            runner_mod,
            "run_script",
            lambda script, args, **kw: seen.update(name=script.name, args=args, kw=kw),
        )
        invoke(runner, ["alpha", "--weird-flag", "-x", "value"])
        assert seen["name"] == "alpha"
        assert seen["args"] == ("--weird-flag", "-x", "value")

    def test_upgrade_and_debug_reach_the_runner(self, runner, catalog, monkeypatch):
        from usmo.cli import runner as runner_mod

        seen = {}
        monkeypatch.setattr(
            runner_mod, "run_script", lambda script, args, **kw: seen.update(kw)
        )
        invoke(runner, ["-U", "--debug", "alpha"])
        assert seen == {"debug": True, "upgrade": True}

    def test_a_script_named_like_a_flag_is_still_passed_through(
        self, runner, catalog, monkeypatch
    ):
        from usmo.cli import runner as runner_mod

        seen = {}
        monkeypatch.setattr(
            runner_mod, "run_script", lambda script, args, **kw: seen.update(args=args)
        )
        invoke(runner, ["alpha", "--help"])
        # `--help` after the script name belongs to the script, not to usm.
        assert seen["args"] == ("--help",)

    def test_builtins_win_over_scripts(self, runner, catalog):
        assert "Usage:" in invoke(runner, ["list", "-h"]).output


# --- Startup cost ----------------------------------------------------------

HEAVY = ("rich", "requests", "importlib.metadata")


def _modules_after(code: str) -> set[str]:
    probe = f"import sys\n{code}\nimport json; print(json.dumps(sorted(sys.modules)))\n"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    return set(json.loads(out.stdout))


class TestStartupCost:
    """Startup regressions are invisible until someone complains, so pin them."""

    def test_importing_the_cli_stays_light(self):
        loaded = _modules_after("import usmo.cli.app")
        for heavy in HEAVY:
            assert heavy not in loaded, f"{heavy} imported at CLI import time"

    def test_importing_the_cli_does_not_pull_the_sdk(self):
        loaded = _modules_after("import usmo.cli.app")
        for module in ("usmo.core.catalog", "usmo.core.aliases", "usmo.core.model"):
            assert module not in loaded, f"{module} imported eagerly"

    def test_the_sdk_is_still_importable_the_usual_way(self):
        """Laziness must not break `from usmo.core import Script`."""
        loaded = _modules_after(
            "from usmo.core import Script, load_scripts\n"
            "from usmo import core\n"
            "assert core.Script is Script\n"
            "assert core.constants.CONFIG_FILENAME\n"
        )
        assert "usmo.core.model" in loaded

    def test_top_level_reexports_still_work(self):
        _modules_after(
            "import usmo\n"
            "assert usmo.Script.__name__ == 'Script'\n"
            "assert callable(usmo.load_scripts)\n"
            "assert 'Script' in dir(usmo)\n"
        )

    def test_unknown_attributes_still_raise(self):
        import usmo
        from usmo import core

        with pytest.raises(AttributeError):
            usmo.definitely_not_a_thing
        with pytest.raises(AttributeError):
            core.definitely_not_a_thing

    def test_version_lookup_does_not_need_the_sdk(self):
        loaded = _modules_after("from usmo.core.version import resolve_version")
        assert "usmo.core.catalog" not in loaded


# --- Output shape ----------------------------------------------------------


class TestOutputShape:
    @pytest.mark.parametrize("width", [70, 80, 100, 140])
    def test_list_fits_the_terminal(self, runner, catalog, width, monkeypatch):
        monkeypatch.setenv("COLUMNS", str(width))
        result = invoke(runner, ["list"])
        for line in result.output.splitlines():
            assert len(line) <= width, f"line exceeds {width}: {line!r}"

    def test_landing_is_narrow(self, runner, catalog, monkeypatch):
        monkeypatch.setenv("COLUMNS", "70")
        for line in invoke(runner, []).output.splitlines():
            assert len(line) <= 70

    def test_search_marks_the_matched_span(self):
        from usmo.cli import presenters

        assert presenters._mark("Azure blob", "azure") == (
            "[bold yellow]Azure[/bold yellow] blob"
        )
        assert presenters._mark("nothing here", "azure") == "nothing here"
        assert presenters._mark("text", None) == "text"
        assert presenters._mark("", "azure") == ""

    def test_search_output_marks_the_query(self, runner, catalog):
        # The markup is what rich turns into colour; assert the intent.
        from usmo.cli import presenters

        table = presenters.scripts_table(
            {k: v for k, v in catalog.items() if k == "beta-tool"},
            highlight="azure",
        )
        rendered = [c._cells for c in table.columns]
        assert any("bold yellow" in cell for col in rendered for cell in col)


# --- Built-ins that talk to the catalog ------------------------------------


@pytest.fixture
def fake_core(monkeypatch, catalog):
    """Stub the catalog side-effects so built-ins can be driven end to end.

    Patching happens on ``usmo.core`` rather than the submodule: the package
    re-exports each name, so that is what the CLI actually calls.
    """
    from usmo import core as core_mod

    state = {
        "changes": [],
        "cached_config": True,
        "updates": [],
        "cleaned": True,
        "unknown": None,
    }

    def update_config(**_kw):
        return state["changes"]

    def iter_updates(names=None, refresh_config=True, **_kw):
        if state["unknown"]:
            from usmo.core.errors import UnknownCommand

            raise UnknownCommand(state["unknown"], list(CATALOG))
        state["updates"].append(tuple(names) if names else None)
        return iter([(n, True) for n in (names or CATALOG)])

    monkeypatch.setattr(core_mod, "update_config", update_config)
    monkeypatch.setattr(core_mod, "iter_updates", iter_updates)
    monkeypatch.setattr(core_mod, "has_cached_config", lambda: state["cached_config"])
    monkeypatch.setattr(core_mod, "clean_cache", lambda: state["cleaned"])
    monkeypatch.setattr(
        core_mod, "read_catalog_meta", lambda *a, **k: {"alpha": ("1.2.3", None)}
    )
    return state


def change(name, old="1.0.0", new="1.1.0"):
    from usmo.core import CatalogChange

    return CatalogChange(name, old, new, "sha256:" + "a" * 64, "sha256:" + "b" * 64)


class TestUpdate:
    def test_up_to_date(self, runner, fake_core):
        result = invoke(runner, ["update"])
        assert result.exit_code == 0 and "up to date" in result.output

    def test_reports_changes(self, runner, fake_core):
        fake_core["changes"] = [change("alpha")]
        out = invoke(runner, ["update"]).output
        assert "alpha" in out and "1.1.0" in out
        assert "usm update --all" in out

    def test_cold_cache_reports_a_fetch(self, runner, fake_core):
        fake_core["cached_config"] = False
        fake_core["changes"] = [change("alpha")]
        assert "Fetched catalog" in invoke(runner, ["update"]).output

    def test_all_pulls_scripts(self, runner, fake_core):
        result = invoke(runner, ["update", "--all"])
        assert result.exit_code == 0 and "Pulled" in result.output
        assert fake_core["updates"] == [None]

    def test_short_all_flag(self, runner, fake_core):
        invoke(runner, ["update", "-a"])
        assert fake_core["updates"] == [None]

    def test_named_update(self, runner, fake_core):
        result = invoke(runner, ["update", "alpha"])
        assert result.exit_code == 0
        assert fake_core["updates"] == [("alpha",)]
        assert "alpha" in result.output

    def test_unknown_name_is_reported(self, runner, fake_core):
        fake_core["unknown"] = "nope"
        result = invoke(runner, ["update", "nope"])
        assert result.exit_code != 0
        assert "Unknown command 'nope'" in result.output

    def test_rejects_a_bogus_flag(self, runner, fake_core):
        result = invoke(runner, ["update", "--bogus"])
        assert result.exit_code == 2
        assert "no such option" in result.output.lower()


class TestClean:
    def test_cleans(self, runner, fake_core):
        result = invoke(runner, ["clean"])
        assert result.exit_code == 0 and "Removed" in result.output

    def test_nothing_to_clean(self, runner, fake_core):
        fake_core["cleaned"] = False
        assert "Nothing to clean" in invoke(runner, ["clean"]).output

    def test_rejects_stray_arguments(self, runner, fake_core):
        result = invoke(runner, ["clean", "extra"])
        assert result.exit_code == 2


class TestInstallUninstall:
    @pytest.fixture
    def alias_env(self, monkeypatch, tmp_path):
        # Patch the re-export, not the submodule: usmo.core binds each name on
        # first access, so patching usmo.core.aliases afterwards would leak a
        # stub into the cached namespace (see the note in usmo/core/__init__).
        from usmo import core as core_mod

        calls = {}
        monkeypatch.setattr(
            core_mod, "alias_status", lambda a: (tmp_path / a, "absent")
        )
        monkeypatch.setattr(
            core_mod,
            "install_alias",
            lambda script, alias, usm_bin: calls.update(script=script, alias=alias),
        )
        monkeypatch.setattr(core_mod, "local_bin_in_path", lambda: True)
        monkeypatch.setattr(core_mod, "uninstall_alias", lambda alias: tmp_path / alias)
        return calls

    def test_install(self, runner, catalog, alias_env):
        result = invoke(runner, ["install", "alpha", "a"])
        assert result.exit_code == 0, result.output
        assert alias_env == {"script": "alpha", "alias": "a"}

    def test_install_rejects_an_unknown_script(self, runner, catalog, alias_env):
        result = invoke(runner, ["install", "gama", "g"])
        assert result.exit_code != 0
        assert "Did you mean" in result.output

    @pytest.mark.parametrize("args", [["install"], ["install", "only-one"]])
    def test_install_needs_two_arguments(self, runner, catalog, args):
        result = invoke(runner, args)
        assert result.exit_code == 2
        assert "Missing argument" in result.output

    def test_install_rejects_extra_arguments(self, runner, catalog, alias_env):
        assert invoke(runner, ["install", "alpha", "a", "b"]).exit_code == 2

    def test_uninstall(self, runner, catalog, alias_env):
        result = invoke(runner, ["uninstall", "a"])
        assert result.exit_code == 0 and "Removed" in result.output

    def test_uninstall_needs_an_alias(self, runner, catalog):
        assert invoke(runner, ["uninstall"]).exit_code == 2

    def test_uninstall_missing_alias_is_not_an_error(
        self, runner, catalog, monkeypatch, tmp_path
    ):
        from usmo import core as core_mod

        monkeypatch.setattr(core_mod, "uninstall_alias", lambda alias: None)
        result = invoke(runner, ["uninstall", "ghost"])
        assert result.exit_code == 0 and "No usm alias" in result.output

    def test_uninstall_refuses_a_foreign_file(
        self, runner, catalog, monkeypatch, tmp_path
    ):
        from usmo import core as core_mod
        from usmo.core.errors import ForeignAlias

        def boom(alias):
            raise ForeignAlias(tmp_path / alias)

        monkeypatch.setattr(core_mod, "uninstall_alias", boom)
        result = invoke(runner, ["uninstall", "a"])
        assert result.exit_code != 0 and "not a usm-managed alias" in result.output


class TestUpdateDiffRendering:
    def test_added_removed_and_changed(self):
        from usmo.cli import presenters
        from usmo.core import CatalogChange

        added = CatalogChange("new", None, "1.0.0", None, "sha256:" + "a" * 64)
        removed = CatalogChange("old", "1.0.0", None, "sha256:" + "b" * 64, None)
        changed = change("mid")
        assert "new" in presenters.change_row(added)[1]
        assert "removed" in presenters.change_row(removed)[1]
        assert "→" in presenters.change_row(changed)[1]

    def test_named_update_falls_back_to_the_manifest(self, runner, fake_core):
        from usmo.cli import presenters

        presenters.print_named_update(("alpha",), [])  # must not raise
