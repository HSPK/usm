"""Tests for shared command-family help grouping."""

from __future__ import annotations

import click
from click.testing import CliRunner

from usm_cli import GROUP_THRESHOLD, GroupedGroup, grouped_class


def command(name):
    def decorator(group):
        @group.command(name, short_help=f"Help for {name}.")
        def child():
            pass

        return group

    return decorator


def make_group(count, sections=()):
    cls = grouped_class(sections, name="TestGroup")

    @click.group(cls=cls)
    def cli():
        pass

    for index in range(count):

        @cli.command(f"cmd-{index}", short_help=f"Command {index}.")
        def child():
            pass

    return cli


class TestThreshold:
    def test_threshold_is_five(self):
        assert GROUP_THRESHOLD == 5

    def test_five_commands_use_standard_commands_section(self):
        result = CliRunner().invoke(make_group(5), ["--help"])
        assert result.exit_code == 0
        assert "Commands:" in result.output
        assert "Other:" not in result.output

    def test_six_commands_use_sections(self):
        cli = make_group(6, [("Manage", ("cmd-0", "cmd-1"))])
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Manage:" in result.output
        assert "Other:" in result.output
        assert "Commands:" not in result.output

    def test_custom_threshold(self):
        class TinyGroup(GroupedGroup):
            group_threshold = 1
            command_sections = (("Run", ("a", "b")),)

        @click.group(cls=TinyGroup)
        def cli():
            pass

        for name in ("a", "b"):
            cli.add_command(click.Command(name, callback=lambda: None))
        assert "Run:" in CliRunner().invoke(cli, ["--help"]).output


class TestSections:
    def test_order_follows_section_declaration(self):
        cli = make_group(
            6,
            [
                ("Last alphabetically", ("cmd-5",)),
                ("First alphabetically", ("cmd-0",)),
            ],
        )
        out = CliRunner().invoke(cli, ["--help"]).output
        assert out.index("Last alphabetically:") < out.index("First alphabetically:")

    def test_order_inside_section_follows_declaration(self):
        cli = make_group(6, [("Run", ("cmd-4", "cmd-1", "cmd-3"))])
        out = CliRunner().invoke(cli, ["--help"]).output
        section = out[out.index("Run:") : out.index("Other:")]
        assert section.index("cmd-4") < section.index("cmd-1") < section.index("cmd-3")

    def test_unlisted_commands_go_to_other(self):
        cli = make_group(6, [("Run", ("cmd-0",))])
        out = CliRunner().invoke(cli, ["--help"]).output
        other = out[out.index("Other:") :]
        for index in range(1, 6):
            assert f"cmd-{index}" in other

    def test_unknown_section_members_are_ignored(self):
        cli = make_group(6, [("Run", ("missing", "cmd-0"))])
        out = CliRunner().invoke(cli, ["--help"]).output
        assert "cmd-0" in out
        assert "missing" not in out

    def test_duplicate_member_is_rendered_once(self):
        cli = make_group(6, [("One", ("cmd-0",)), ("Two", ("cmd-0", "cmd-1"))])
        out = CliRunner().invoke(cli, ["--help"]).output
        assert out.count("cmd-0") == 1

    def test_empty_sections_are_not_rendered(self):
        cli = make_group(6, [("Empty", ("missing",)), ("Run", ("cmd-0",))])
        assert "Empty:" not in CliRunner().invoke(cli, ["--help"]).output

    def test_hidden_commands_are_not_rendered(self):
        class HelpGroup(GroupedGroup):
            group_threshold = 0
            command_sections = (("Run", ("shown", "secret")),)

        @click.group(cls=HelpGroup)
        def cli():
            pass

        cli.add_command(click.Command("shown", callback=lambda: None))
        cli.add_command(click.Command("secret", callback=lambda: None, hidden=True))
        out = CliRunner().invoke(cli, ["--help"]).output
        assert "shown" in out and "secret" not in out

    def test_short_help_is_rendered(self):
        cli = make_group(6, [("Run", ("cmd-0",))])
        assert "Command 0." in CliRunner().invoke(cli, ["--help"]).output


class TestIntrospection:
    def test_visible_commands_excludes_hidden(self):
        cli = make_group(6)
        cli.add_command(click.Command("hidden", callback=lambda: None, hidden=True))
        ctx = click.Context(cli)
        assert "hidden" not in {name for name, _ in cli.visible_commands(ctx)}

    def test_membership_reports_unlisted(self):
        cli = make_group(6, [("Run", ("cmd-0", "cmd-1"))])
        visible, listed, duplicates = cli.section_membership(click.Context(cli))
        assert "cmd-5" in visible - listed
        assert not duplicates

    def test_membership_reports_duplicates(self):
        cli = make_group(6, [("A", ("cmd-0",)), ("B", ("cmd-0",))])
        _, _, duplicates = cli.section_membership(click.Context(cli))
        assert duplicates == {"cmd-0"}


class TestFactory:
    def test_factory_normalizes_lists_to_tuples(self):
        cls = grouped_class([("Run", ["a", "b"])])
        assert cls.command_sections == (("Run", ("a", "b")),)

    def test_factory_uses_requested_name(self):
        assert grouped_class([], name="MyGroup").__name__ == "MyGroup"

    def test_each_factory_call_is_independent(self):
        one = grouped_class([("A", ("a",))])
        two = grouped_class([("B", ("b",))])
        assert one.command_sections != two.command_sections
