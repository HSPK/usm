#!/usr/bin/env python3
"""Shared click presentation for command families.

Five commands fit in one flat list.  Beyond that, alphabetical order hides
the workflow: `disable` lands beside `dry-run`, while its counterpart
`enable` is several rows away.  Large usm CLIs declare semantic sections and
this group renders them consistently.
"""

from __future__ import annotations

from collections.abc import Iterable

import click

GROUP_THRESHOLD = 5


class GroupedGroup(click.Group):
    """Flat help for small CLIs, labelled sections for large ones."""

    command_sections: tuple[tuple[str, tuple[str, ...]], ...] = ()
    group_threshold = GROUP_THRESHOLD
    other_title = "Other"

    def visible_commands(self, ctx: click.Context) -> list[tuple[str, click.Command]]:
        commands = []
        for name in sorted(self.list_commands(ctx)):
            command = self.get_command(ctx, name)
            if command is not None and not command.hidden:
                commands.append((name, command))
        return commands

    def section_membership(
        self, ctx: click.Context
    ) -> tuple[set[str], set[str], set[str]]:
        """Return (visible, explicitly listed, duplicate section members)."""
        visible = {name for name, _ in self.visible_commands(ctx)}
        listed: set[str] = set()
        duplicates: set[str] = set()
        for _title, names in self.command_sections:
            for name in names:
                if name in listed:
                    duplicates.add(name)
                listed.add(name)
        return visible, listed, duplicates

    def format_commands(self, ctx: click.Context, formatter) -> None:
        visible = self.visible_commands(ctx)
        if len(visible) <= self.group_threshold:
            super().format_commands(ctx, formatter)
            return

        rendered: set[str] = set()
        width = max(20, formatter.width - 8)
        for title, names in self.command_sections:
            rows = []
            for name in names:
                if name in rendered:
                    continue
                command = self.get_command(ctx, name)
                if command is None or command.hidden:
                    continue
                rendered.add(name)
                rows.append((name, command.get_short_help_str(width)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)

        extra = [(name, command) for name, command in visible if name not in rendered]
        if extra:
            with formatter.section(self.other_title):
                formatter.write_dl(
                    [
                        (name, command.get_short_help_str(width))
                        for name, command in extra
                    ]
                )


def grouped_class(
    sections: Iterable[tuple[str, Iterable[str]]],
    *,
    name: str = "CommandGroup",
) -> type[GroupedGroup]:
    """Create a declarative GroupedGroup subclass for one script."""
    normalized = tuple(
        (str(title), tuple(str(command) for command in commands))
        for title, commands in sections
    )
    return type(name, (GroupedGroup,), {"command_sections": normalized})
