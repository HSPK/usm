"""Tests for scripts/inject_alias.py — the managed alias block in a shell rc.

This script had no coverage at all until the managed-block refactor, despite
writing into ``~/.bashrc``, ``~/.zshrc`` and the PowerShell profile — files
where a bad write costs someone their shell. The block mechanics now live in
usm_blocks and are tested there; what is tested here is everything this
script decides on its own: which file, which shell, what the block says, and
what it does when the answer is unclear.
"""

from __future__ import annotations

import os
import stat

import pytest
from click.testing import CliRunner

import inject_alias
from inject_alias import (
    BEGIN_MARKER,
    END_MARKER,
    render_alias_block,
    render_alias_body,
    resolve_target,
    target_path_for_shell,
    upsert_alias_block,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(inject_alias.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


# -- what the block contains -----------------------------------------------


class TestRendering:
    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_posix_body_is_specialised_per_shell(self, shell):
        body = render_alias_body(shell)
        assert "__SHELL__" not in body, "the placeholder must be substituted"
        assert shell in body

    def test_powershell_gets_its_own_body(self):
        body = render_alias_body("powershell")
        assert "Get-ChildItem" in body
        assert "__SHELL__" not in body

    def test_the_block_is_fenced(self):
        block = render_alias_block("bash")
        assert block.startswith(BEGIN_MARKER + "\n")
        assert block.endswith(END_MARKER + "\n")

    def test_the_block_ends_with_exactly_one_newline(self):
        block = render_alias_block("bash")
        assert block.endswith("\n") and not block.endswith("\n\n")

    def test_bash_and_zsh_blocks_differ(self):
        assert render_alias_block("bash") != render_alias_block("zsh")


# -- choosing the file -----------------------------------------------------


class TestTargetSelection:
    @pytest.mark.parametrize("shell,name", [("bash", ".bashrc"), ("zsh", ".zshrc")])
    def test_posix_shells_map_to_their_rc(self, shell, name, tmp_path):
        assert target_path_for_shell(shell, tmp_path).name == name

    def test_powershell_maps_into_the_profile_directory(self, tmp_path):
        path = target_path_for_shell("powershell", tmp_path)
        assert path.name.endswith(".ps1")

    def test_an_explicit_file_wins(self, home, tmp_path):
        custom = tmp_path / "custom.rc"
        path, shell, label = resolve_target("bash", custom)
        assert path == custom and shell == "bash"

    def test_a_non_interactive_session_does_not_prompt(self, home):
        path, shell, _ = resolve_target(None, None, "Linux", interactive=False)
        assert shell in inject_alias.SUPPORTED_SHELLS
        assert path.parent == home

    def test_an_unknown_system_still_resolves(self, home):
        _, shell, _ = resolve_target(None, None, "Plan9", interactive=False)
        assert shell in inject_alias.SUPPORTED_SHELLS


# -- writing ---------------------------------------------------------------


class TestUpserting:
    def test_inserts_into_a_missing_file(self, tmp_path):
        rc = tmp_path / ".bashrc"
        assert upsert_alias_block(rc, "bash") == "inserted"
        assert BEGIN_MARKER in rc.read_text()

    def test_creates_missing_parents(self, tmp_path):
        rc = tmp_path / "deep" / "nested" / ".bashrc"
        upsert_alias_block(rc, "bash")
        assert rc.exists()

    def test_updates_an_existing_block(self, tmp_path):
        rc = tmp_path / ".bashrc"
        upsert_alias_block(rc, "bash")
        assert upsert_alias_block(rc, "zsh") == "updated"
        assert rc.read_text().count(BEGIN_MARKER) == 1

    def test_is_idempotent(self, tmp_path):
        rc = tmp_path / ".bashrc"
        upsert_alias_block(rc, "bash")
        first = rc.read_text()
        upsert_alias_block(rc, "bash")
        assert rc.read_text() == first

    def test_repeated_runs_never_stack_blocks(self, tmp_path):
        rc = tmp_path / ".bashrc"
        for _ in range(5):
            upsert_alias_block(rc, "bash")
        assert rc.read_text().count(BEGIN_MARKER) == 1
        assert rc.read_text().count(END_MARKER) == 1

    def test_hand_written_content_survives(self, tmp_path):
        rc = tmp_path / ".bashrc"
        original = "# mine\nexport EDITOR=vim\n"
        rc.write_text(original)
        upsert_alias_block(rc, "bash")
        assert rc.read_text().startswith(original)

    def test_content_after_the_block_survives_an_update(self, tmp_path):
        rc = tmp_path / ".bashrc"
        upsert_alias_block(rc, "bash")
        rc.write_text(rc.read_text() + "export LATER=1\n")
        upsert_alias_block(rc, "zsh")
        assert rc.read_text().endswith("export LATER=1\n")

    def test_a_file_without_a_trailing_newline(self, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_text("export A=1")
        upsert_alias_block(rc, "bash")
        assert rc.read_text().startswith("export A=1\n")

    def test_an_empty_file(self, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_text("")
        assert upsert_alias_block(rc, "bash") == "inserted"

    def test_an_existing_mode_is_preserved(self, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_text("x\n")
        os.chmod(rc, 0o600)
        upsert_alias_block(rc, "bash")
        assert stat.S_IMODE(rc.stat().st_mode) == 0o600

    def test_no_temp_files_are_left(self, tmp_path):
        rc = tmp_path / ".bashrc"
        upsert_alias_block(rc, "bash")
        assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []

    def test_a_symlinked_rc_edits_the_target(self, tmp_path):
        """Dotfile setups symlink the rc; editing the link would break them."""
        real = tmp_path / "dotfiles" / "bashrc"
        real.parent.mkdir()
        real.write_text("# real\n")
        link = tmp_path / ".bashrc"
        link.symlink_to(real)
        upsert_alias_block(link, "bash")
        assert link.is_symlink()
        assert BEGIN_MARKER in real.read_text()

    @pytest.mark.parametrize(
        "broken",
        [
            f"{BEGIN_MARKER}\nunterminated\n",
            f"{END_MARKER}\n",
            f"{BEGIN_MARKER}\na\n{BEGIN_MARKER}\nb\n{END_MARKER}\n",
        ],
        ids=["missing-end", "missing-begin", "duplicate-begin"],
    )
    def test_a_broken_block_is_refused_and_changes_nothing(self, tmp_path, broken):
        rc = tmp_path / ".bashrc"
        rc.write_text(broken)
        with pytest.raises(ValueError):
            upsert_alias_block(rc, "bash")
        assert rc.read_text() == broken

    def test_a_marker_quoted_inside_the_rc_is_not_treated_as_ours(self, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_text(f'echo "{BEGIN_MARKER}"\n')
        assert upsert_alias_block(rc, "bash") == "inserted"
        assert f'echo "{BEGIN_MARKER}"' in rc.read_text()

    def test_non_utf8_content_is_refused(self, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_bytes(b"\xff\xfe\n")
        with pytest.raises(ValueError):
            upsert_alias_block(rc, "bash")
        assert rc.read_bytes() == b"\xff\xfe\n"


# -- the command -----------------------------------------------------------


class TestCommandLine:
    def test_writes_to_the_shell_rc(self, home, runner):
        result = runner.invoke(inject_alias.cli, ["--shell", "bash"])
        assert result.exit_code == 0
        assert BEGIN_MARKER in (home / ".bashrc").read_text()

    def test_shell_choice_is_case_insensitive(self, home, runner):
        result = runner.invoke(inject_alias.cli, ["--shell", "BASH"])
        assert result.exit_code == 0
        assert (home / ".bashrc").exists()

    def test_an_explicit_file_is_honoured(self, home, runner, tmp_path):
        target = tmp_path / "custom.rc"
        result = runner.invoke(
            inject_alias.cli, ["--shell", "bash", "--file", str(target)]
        )
        assert result.exit_code == 0 and BEGIN_MARKER in target.read_text()

    def test_an_unsupported_shell_is_rejected(self, home, runner):
        result = runner.invoke(inject_alias.cli, ["--shell", "fish"])
        assert result.exit_code != 0

    def test_a_broken_block_exits_non_zero_without_a_traceback(self, home, runner):
        rc = home / ".bashrc"
        rc.write_text(f"{BEGIN_MARKER}\nunterminated\n")
        result = runner.invoke(inject_alias.cli, ["--shell", "bash"])
        assert result.exit_code != 0
        assert "Traceback" not in result.output

    def test_help_works(self, runner):
        result = runner.invoke(inject_alias.cli, ["--help"])
        assert result.exit_code == 0 and "Usage:" in result.output

    def test_running_twice_reports_updated(self, home, runner):
        runner.invoke(inject_alias.cli, ["--shell", "bash"])
        result = runner.invoke(inject_alias.cli, ["--shell", "bash"])
        assert result.exit_code == 0
        assert (home / ".bashrc").read_text().count(BEGIN_MARKER) == 1


class TestInteractiveSelection:
    """The prompt only appears when there is a human to answer it."""

    def test_windows_offers_powershell_first(self):
        choices = inject_alias.prompt_choices_for_system("Windows")
        assert choices[0][1] == "powershell"

    def test_posix_offers_bash_first(self):
        choices = inject_alias.prompt_choices_for_system("Linux")
        assert choices[0][1] == "bash"
        assert [c[1] for c in choices] == ["bash", "zsh"]

    @pytest.mark.parametrize(
        "answer,expected",
        [
            ("1", "bash"),
            ("2", "zsh"),
            ("bash", "bash"),
            ("zsh", "zsh"),
            ("ZSH", "zsh"),
            ("  2  ", "zsh"),
        ],
    )
    def test_a_recognised_answer_selects_that_shell(
        self, monkeypatch, answer, expected
    ):
        monkeypatch.setattr(inject_alias.click, "prompt", lambda *a, **kw: answer)
        assert inject_alias.prompt_for_shell("Linux") == expected

    def test_an_unrecognised_answer_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(inject_alias.click, "prompt", lambda *a, **kw: "banana")
        assert inject_alias.prompt_for_shell("Linux") == "bash"

    def test_an_empty_answer_takes_the_default(self, monkeypatch):
        monkeypatch.setattr(inject_alias.click, "prompt", lambda *a, **kw: "1")
        assert inject_alias.prompt_for_shell("Windows") == "powershell"

    def test_an_interactive_session_is_prompted(self, home, monkeypatch):
        asked = []
        monkeypatch.setattr(
            inject_alias,
            "prompt_for_shell",
            lambda system: asked.append(system) or "zsh",
        )
        _, shell, _ = resolve_target(None, None, "Linux", interactive=True)
        assert asked == ["Linux"] and shell == "zsh"

    def test_is_interactive_follows_both_streams(self, monkeypatch):
        class Stream:
            def __init__(self, tty):
                self._tty = tty

            def isatty(self):
                return self._tty

        monkeypatch.setattr(inject_alias.sys, "stdin", Stream(True))
        monkeypatch.setattr(inject_alias.sys, "stdout", Stream(True))
        assert inject_alias.is_interactive() is True
        monkeypatch.setattr(inject_alias.sys, "stdout", Stream(False))
        assert inject_alias.is_interactive() is False


class TestPowerShellProfile:
    def test_prefers_an_existing_profile(self, tmp_path):
        existing = (
            tmp_path
            / "Documents"
            / "WindowsPowerShell"
            / "Microsoft.PowerShell_profile.ps1"
        )
        existing.parent.mkdir(parents=True)
        existing.write_text("")
        assert inject_alias.powershell_profile_path(tmp_path) == existing

    def test_falls_back_to_the_modern_location(self, tmp_path):
        path = inject_alias.powershell_profile_path(tmp_path)
        assert "PowerShell" in str(path) and path.name.endswith(".ps1")

    def test_the_modern_location_wins_when_both_exist(self, tmp_path):
        modern = (
            tmp_path / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        )
        legacy = (
            tmp_path
            / "Documents"
            / "WindowsPowerShell"
            / "Microsoft.PowerShell_profile.ps1"
        )
        for path in (modern, legacy):
            path.parent.mkdir(parents=True)
            path.write_text("")
        assert inject_alias.powershell_profile_path(tmp_path) == modern

    def test_label_names_the_profile_path(self, tmp_path):
        label = inject_alias.shell_label("powershell", tmp_path)
        assert "PowerShell profile" in label

    def test_label_for_a_posix_shell_is_just_the_shell(self, tmp_path):
        assert inject_alias.shell_label("bash", tmp_path) == "bash"

    def test_an_unsupported_shell_is_a_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="Unsupported shell"):
            target_path_for_shell("fish", tmp_path)

    def test_windows_defaults_to_powershell(self):
        assert inject_alias.default_shell_for_system("Windows") == "powershell"

    def test_other_systems_default_to_bash(self):
        assert inject_alias.default_shell_for_system("Darwin") == "bash"

    def test_current_system_name_is_reported(self, monkeypatch):
        monkeypatch.setattr(inject_alias.platform, "system", lambda: "Haiku")
        assert inject_alias.current_system_name() == "Haiku"


class TestCommandOutput:
    def test_posix_tells_you_to_source_it(self, home, runner):
        result = runner.invoke(inject_alias.cli, ["--shell", "bash"])
        assert "source" in result.output

    def test_powershell_tells_you_to_dot_it(self, home, runner):
        result = runner.invoke(inject_alias.cli, ["--shell", "powershell"])
        assert result.exit_code == 0
        assert "Run: . " in result.output
