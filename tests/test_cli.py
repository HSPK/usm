"""Smoke tests for top-level CLI subcommands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner


def test_version_command() -> None:
    from usmo.cli import cli

    res = CliRunner().invoke(cli, ["version"])
    assert res.exit_code == 0
    assert "usm" in res.output


def test_clean_when_empty() -> None:
    from usmo.cli import cli

    res = CliRunner().invoke(cli, ["clean"])
    assert res.exit_code == 0
    assert "Nothing to clean" in res.output


def _build_local_index(scripts_dir: Path) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    f = scripts_dir / "demo.sh"
    f.write_text("#!/usr/bin/env bash\necho hello\n")
    import hashlib

    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    (scripts_dir / "_config.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "registry": {"name": "default"},
                "packages": {
                    "demo": {
                        "description": "demo",
                        "latest": "1.0.0",
                        "versions": {
                            "1.0.0": {
                                "type": "script",
                                "path": "demo.sh",
                                "sha256": sha,
                            }
                        },
                    }
                },
            }
        )
    )


def test_install_via_cli_debug_mode(tmp_path: Path, monkeypatch) -> None:
    from usmo.cli import cli

    _build_local_index(tmp_path / "scripts")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    res = runner.invoke(cli, ["--debug", "install", "demo"])
    assert res.exit_code == 0, res.output
    assert "Installed" in res.output

    res = runner.invoke(cli, ["installed"])
    assert "demo" in res.output

    # Legacy dispatch: `usm demo` should run the installed entry.
    res = runner.invoke(cli, ["demo"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(cli, ["uninstall", "demo"])
    assert res.exit_code == 0, res.output
    assert "Removed" in res.output


def test_publish_outputs_snippet(tmp_path: Path) -> None:
    from usmo.cli import cli

    f = tmp_path / "thing.sh"
    f.write_text("echo thing\n")
    res = CliRunner().invoke(
        cli,
        [
            "publish",
            str(f),
            "--name",
            "thing",
            "--version",
            "0.1.0",
            "--description",
            "test",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "schema_version" in res.output
    assert "thing" in res.output


def test_search_local_debug(tmp_path: Path, monkeypatch) -> None:
    from usmo.cli import cli

    _build_local_index(tmp_path / "scripts")
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["--debug", "search", "demo"])
    assert res.exit_code == 0, res.output
    assert "demo" in res.output
