"""Tests for the config-driven bootstrapper in scripts/init.py."""

from __future__ import annotations

import textwrap

import pytest
from click.testing import CliRunner

import init


class RecordingExecutor(init.Executor):
    """Executor that records commands and reports tools as installed/missing."""

    def __init__(self, installed: set[str] | None = None) -> None:
        self.installed = installed or set()
        self.commands: list[str] = []

    def which(self, name: str) -> bool:
        return name in self.installed

    def run(self, argv, *, command: str) -> int:
        self.commands.append(command)
        return 0


def _context(platform, executor, *, dry_run=False, tmp_home=None):
    return init.RunContext(
        platform=platform,
        executor=executor,
        dry_run=dry_run,
        home=tmp_home or init.Path.home(),
    )


class TestPlatformDetection:
    @pytest.mark.parametrize(
        "sys_platform,expected_key",
        [("darwin", "macos"), ("win32", "windows"), ("linux", "linux")],
    )
    def test_detect_platform(self, monkeypatch, sys_platform, expected_key):
        monkeypatch.setattr(init.sys, "platform", sys_platform)
        assert init.detect_platform().key == expected_key

    def test_posix_shell_argv(self):
        assert init.Linux().shell_argv("echo hi") == ["bash", "-c", "echo hi"]

    def test_windows_shell_argv(self):
        argv = init.Windows().shell_argv("echo hi")
        assert argv[0] == "powershell" and argv[-1] == "echo hi"


class TestConfigParsing:
    def test_default_config_loads(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        assert "lang" in cfg.default_groups
        assert "uv" in cfg.items and "fnm" in cfg.items

    def test_command_for_platform(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        rg = cfg.items["ripgrep"]
        assert rg.command_for("windows").startswith("winget")
        assert rg.command_for("macos") == "brew install ripgrep"

    def test_all_key_is_platform_fallback(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        az = cfg.items["azure-cli"]
        assert az.command_for("macos") == az.command_for("windows")

    def test_action_items_parsed(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        assert cfg.items["profile"].is_action
        assert cfg.items["profile"].action == "profile"

    def test_group_referencing_unknown_item_raises(self):
        raw = {"groups": {"g": {"items": ["ghost"]}}, "items": {}}
        with pytest.raises(init.click.ClickException):
            init.InitConfig.from_raw(raw)


class TestConfigMerge:
    def test_external_config_deep_merges(self, tmp_path):
        override = tmp_path / "ov.yaml"
        override.write_text(
            textwrap.dedent(
                """
                default_groups: [lang]
                items:
                  uv:
                    linux: echo CUSTOM
                """
            )
        )
        cfg = init.ConfigRepository().load(config_path=override, use_user_config=False)
        assert cfg.default_groups == ("lang",)
        assert cfg.items["uv"].command_for("linux") == "echo CUSTOM"
        # untouched platform keys survive the merge
        assert cfg.items["uv"].command_for("macos") == "brew install uv"

    def test_user_config_path_respected(self, tmp_path, monkeypatch):
        user_cfg = tmp_path / "init.yaml"
        user_cfg.write_text("default_groups: [cli]\n")
        repo = init.ConfigRepository(user_config_path=user_cfg)
        assert repo.load(use_user_config=True).default_groups == ("cli",)
        # ignored when disabled
        assert repo.load(use_user_config=False).default_groups != ("cli",)


class TestPlanner:
    def test_resolve_default_groups(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        planner = init.Planner(cfg)
        names = [g.name for g in planner.resolve_groups(None)]
        assert names == list(cfg.default_groups)

    def test_unknown_group_raises(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        with pytest.raises(init.click.ClickException):
            init.Planner(cfg).resolve_groups(["does-not-exist"])

    def test_items_dedup_and_platform_filter(self):
        cfg = init.ConfigRepository().load(use_user_config=False)
        planner = init.Planner(cfg)
        groups = planner.resolve_groups(["linux-extras"])
        on_windows = planner.items_for(groups, "windows")
        on_linux = planner.items_for(groups, "linux")
        assert on_windows == []  # linux-only items filtered out
        assert {i.name for i in on_linux} == {"build-deps", "tailscale", "dua-cli"}


class TestSteps:
    def test_command_step_skips_when_installed(self):
        item = init.Item(name="rg", check="rg", commands={"linux": "install rg"})
        executor = RecordingExecutor(installed={"rg"})
        result = init.CommandStep(item).execute(_context(init.Linux(), executor))
        assert result is init.StepResult.SKIPPED
        assert executor.commands == []

    def test_command_step_runs_when_missing(self):
        item = init.Item(name="rg", check="rg", commands={"linux": "install rg"})
        executor = RecordingExecutor(installed=set())
        result = init.CommandStep(item).execute(_context(init.Linux(), executor))
        assert result is init.StepResult.OK
        assert executor.commands == ["install rg"]

    def test_command_step_unsupported_without_recipe(self):
        item = init.Item(name="tmux", commands={"linux": "install tmux"})
        executor = RecordingExecutor()
        result = init.CommandStep(item).execute(_context(init.Windows(), executor))
        assert result is init.StepResult.UNSUPPORTED
        assert executor.commands == []

    def test_failed_command_reports_failure(self):
        class Failing(RecordingExecutor):
            def run(self, argv, *, command):
                self.commands.append(command)
                return 1

        item = init.Item(name="x", commands={"linux": "boom"})
        result = init.CommandStep(item).execute(_context(init.Linux(), Failing()))
        assert result is init.StepResult.FAILED


class TestActions:
    def test_nvim_config_writes_file(self, tmp_path):
        item = init.Item(name="nvim-config", action="nvim-config")
        ctx = _context(init.Linux(), RecordingExecutor(), tmp_home=tmp_path)
        result = init.ActionStep(item).execute(ctx)
        config = tmp_path / ".config" / "nvim" / "init.vim"
        assert result is init.StepResult.OK
        assert config.is_file()
        assert "set undodir=" in config.read_text()

    def test_nvim_config_dry_run_writes_nothing(self, tmp_path):
        item = init.Item(name="nvim-config", action="nvim-config")
        ctx = _context(
            init.Linux(), RecordingExecutor(), dry_run=True, tmp_home=tmp_path
        )
        init.ActionStep(item).execute(ctx)
        assert not (tmp_path / ".config" / "nvim" / "init.vim").exists()

    def test_profile_action_delegates_to_inject_alias(self, tmp_path):
        item = init.Item(name="profile", action="profile")
        executor = RecordingExecutor()
        init.ActionStep(item).execute(
            _context(init.Linux(), executor, tmp_home=tmp_path)
        )
        assert executor.commands == ["usm inject-alias"]


class TestCli:
    def test_list_exits_clean(self):
        result = CliRunner().invoke(init.cli, ["--list"])
        assert result.exit_code == 0
        assert "Groups" in result.output and "uv, fnm" in result.output

    def test_unknown_group_exits_nonzero(self):
        result = CliRunner().invoke(init.cli, ["bogus"])
        assert result.exit_code != 0
        assert "unknown group" in result.output

    def test_export_config_round_trips(self, tmp_path, monkeypatch):
        target = tmp_path / "init.yaml"
        monkeypatch.setattr(init, "DEFAULT_USER_CONFIG", target)
        result = CliRunner().invoke(init.cli, ["--export-config"])
        assert result.exit_code == 0 and target.is_file()
        # guard without --force
        guarded = CliRunner().invoke(init.cli, ["--export-config"])
        assert guarded.exit_code != 0
        # exported file is valid config
        cfg = init.ConfigRepository(user_config_path=target).load()
        assert "lang" in cfg.groups

    def test_dry_run_installs_nothing(self, monkeypatch):
        monkeypatch.setattr(init.sys, "platform", "linux")
        monkeypatch.setattr(init.DryRunExecutor, "which", lambda self, name: False)
        result = CliRunner().invoke(init.cli, ["--dry-run", "lang"])
        assert result.exit_code == 0
        assert "[dry-run]" in result.output
        assert "dry run" in result.output
