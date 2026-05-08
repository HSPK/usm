"""Tests for the registry config loader and CLI registry subcommands."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner


def test_default_config_when_missing() -> None:
    from usmo import registry

    cfg = registry.load_config()
    assert cfg.default_registry == registry.DEFAULT_REGISTRY_ID
    assert any(r.url == registry.DEFAULT_REGISTRY_URL for r in cfg.registries)


def test_save_and_reload(tmp_path: Path) -> None:
    from usmo import registry

    cfg = registry.RegistryConfig(
        registries=[
            registry.Registry(id="primary", url="https://example.com/a/"),
            registry.Registry(id="mirror", url="https://example.com/b/"),
        ],
        default_registry="primary",
    )
    p = tmp_path / "config.toml"
    registry.save_config(cfg, p)

    loaded = registry.load_config(p)
    assert loaded.default_registry == "primary"
    assert [r.id for r in loaded.registries] == ["primary", "mirror"]


def test_iter_search_order_prefers_specified_registry() -> None:
    from usmo import registry

    cfg = registry.RegistryConfig(
        registries=[
            registry.Registry(id="a", url="https://x/a/"),
            registry.Registry(id="b", url="https://x/b/"),
        ],
        default_registry="a",
    )
    order = [r.id for r in cfg.iter_search_order("b")]
    assert order == ["b", "a"]


def test_invalid_default_registry_rejected(tmp_path: Path) -> None:
    from usmo import registry

    p = tmp_path / "config.toml"
    p.write_text(
        'default_registry = "missing"\n\n'
        '[[registries]]\nid = "real"\nurl = "https://x/"\n'
    )
    import pytest

    with pytest.raises(ValueError):
        registry.load_config(p)


def test_cli_registry_add_list_remove() -> None:
    from usmo.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["registry", "add", "extra", "https://example.org/r/"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(cli, ["registry", "list"])
    assert "extra" in res.output

    res = runner.invoke(cli, ["registry", "remove", "extra"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(cli, ["registry", "list"])
    assert "extra" not in res.output
