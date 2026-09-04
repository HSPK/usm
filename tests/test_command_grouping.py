"""Repository-wide command-help readability contract.

A flat list is fine through five visible commands.  Above that every command
family must use the shared GroupedGroup and explicitly place each visible
command in a semantic section.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from usm_cli import GROUP_THRESHOLD, GroupedGroup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "scripts" / "_config.json").read_text())["scripts"]
COMMAND_MODULES = tuple(
    sorted(
        Path(entry["path"]).stem
        for entry in CONFIG.values()
        if entry["path"].endswith(".py")
    )
)


def command_data(module_name):
    module = importlib.import_module(module_name)
    cli = getattr(module, "cli", None)
    if cli is None:
        return module, None, set(), [], set()
    if not isinstance(cli, click.Group):
        return module, cli, set(), [], set()
    ctx = click.Context(cli)
    all_commands = {name: cli.get_command(ctx, name) for name in cli.list_commands(ctx)}
    visible = {
        name
        for name, command in all_commands.items()
        if command is not None and not command.hidden
    }
    listed = [
        name for _title, names in getattr(cli, "command_sections", ()) for name in names
    ]
    return module, cli, visible, listed, set(all_commands)


class TestLargeFamiliesAreGrouped:
    @pytest.mark.parametrize("module_name", COMMAND_MODULES)
    def test_threshold_contract(self, module_name):
        _module, cli, visible, _listed, _all = command_data(module_name)
        if cli is None:
            return
        if len(visible) > GROUP_THRESHOLD:
            assert isinstance(cli, GroupedGroup), (
                f"{module_name} has {len(visible)} commands but flat help"
            )

    @pytest.mark.parametrize("module_name", COMMAND_MODULES)
    def test_every_visible_command_is_sectioned(self, module_name):
        _module, _cli, visible, listed, _all = command_data(module_name)
        if len(visible) > GROUP_THRESHOLD:
            assert visible <= set(listed), (
                f"{module_name} leaves these under Other: "
                f"{sorted(visible - set(listed))}"
            )

    @pytest.mark.parametrize("module_name", COMMAND_MODULES)
    def test_no_command_is_in_two_sections(self, module_name):
        _module, _cli, visible, listed, _all = command_data(module_name)
        if len(visible) > GROUP_THRESHOLD:
            duplicates = {name for name in listed if listed.count(name) > 1}
            assert not duplicates, f"{module_name}: duplicate sections {duplicates}"

    @pytest.mark.parametrize("module_name", COMMAND_MODULES)
    def test_sections_only_name_real_commands(self, module_name):
        _module, _cli, _visible, listed, all_names = command_data(module_name)
        assert set(listed) <= all_names, (
            f"{module_name}: stale section entries {sorted(set(listed) - all_names)}"
        )

    @pytest.mark.parametrize("module_name", COMMAND_MODULES)
    def test_large_help_has_labels_not_one_commands_bucket(self, module_name):
        _module, cli, visible, _listed, _all = command_data(module_name)
        if cli is None:
            return
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0, result.output
        if len(visible) > GROUP_THRESHOLD:
            assert "\nCommands:" not in result.output
            for title, names in cli.command_sections:
                if visible & set(names):
                    assert f"\n{title}:" in result.output


class TestAzsyncSections:
    def test_expected_workflows(self):
        module, _cli, _visible, _listed, _all = command_data("azsync")
        sections = dict(module.AZSYNC_SECTIONS)
        assert sections["Transfer"] == ("sync", "flush")
        assert sections["Lifecycle"] == ("start", "stop", "restart")
        assert sections["Boot"] == ("enable", "disable")

    def test_help_keeps_related_pairs_together(self):
        _module, cli, _visible, _listed, _all = command_data("azsync")
        out = CliRunner().invoke(cli, ["--help"]).output
        transfer = out[out.index("Transfer:") : out.index("Lifecycle:")]
        assert "sync" in transfer and "flush" in transfer

    @pytest.mark.parametrize("width", [40, 60, 80, 120, 200])
    def test_grouped_help_survives_terminal_width(self, width):
        _module, cli, _visible, _listed, _all = command_data("azsync")
        result = CliRunner(env={"COLUMNS": str(width)}).invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Transfer:" in result.output
