"""Tests for alias-shim install/uninstall and config-only update helpers."""

from __future__ import annotations

import json
import os

import pytest

from usmo import core


@pytest.fixture
def tmp_local_bin(tmp_path, monkeypatch):
    """Redirect ~/.local/bin to a temp dir."""
    bin_dir = tmp_path / "local" / "bin"
    monkeypatch.setattr(core.constants, "LOCAL_BIN_DIR", bin_dir)
    monkeypatch.setattr(core, "LOCAL_BIN_DIR", bin_dir)
    return bin_dir


class TestLocalBinInPath:
    def test_present(self, tmp_local_bin, monkeypatch):
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_local_bin), "/usr/bin"]))
        assert core.local_bin_in_path() is True

    def test_absent(self, tmp_local_bin, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        assert core.local_bin_in_path() is False

    def test_trailing_slash_normalised(self, tmp_local_bin, monkeypatch):
        monkeypatch.setenv("PATH", f"{tmp_local_bin}{os.sep}:/usr/bin")
        assert core.local_bin_in_path() is True


class TestAliasInstall:
    def test_install_creates_executable_shim(self, tmp_local_bin):
        path = core.install_alias("clash", "cx", usm_bin="/opt/usm")
        assert path == tmp_local_bin / ("cx.cmd" if os.name == "nt" else "cx")
        assert path.exists()
        body = path.read_text()
        assert core.ALIAS_SHIM_MARKER in body
        assert "clash" in body
        assert "/opt/usm" in body
        if os.name != "nt":
            assert os.access(path, os.X_OK)

    def test_status_absent_then_ours(self, tmp_local_bin):
        _path, status = core.alias_status("cx")
        assert status == "absent"
        core.install_alias("clash", "cx", usm_bin="/opt/usm")
        _path, status = core.alias_status("cx")
        assert status == "ours"

    def test_status_foreign(self, tmp_local_bin):
        tmp_local_bin.mkdir(parents=True)
        target = core.alias_path("busy")
        target.write_text("#!/bin/sh\necho hi\n")
        _path, status = core.alias_status("busy")
        assert status == "foreign"

    def test_reinstall_overwrites(self, tmp_local_bin):
        core.install_alias("clash", "cx", usm_bin="/opt/usm")
        core.install_alias("proxy", "cx", usm_bin="/opt/usm")
        assert "proxy" in core.alias_path("cx").read_text()


class TestAliasUninstall:
    def test_uninstall_ours(self, tmp_local_bin):
        core.install_alias("clash", "cx", usm_bin="/opt/usm")
        removed = core.uninstall_alias("cx")
        assert removed == core.alias_path("cx")
        assert not core.alias_path("cx").exists()

    def test_uninstall_absent_returns_none(self, tmp_local_bin):
        assert core.uninstall_alias("ghost") is None

    def test_uninstall_foreign_raises_and_keeps_file(self, tmp_local_bin):
        tmp_local_bin.mkdir(parents=True)
        target = core.alias_path("busy")
        target.write_text("#!/bin/sh\necho original\n")
        with pytest.raises(core.ForeignAlias):
            core.uninstall_alias("busy")
        assert target.exists()
        assert "original" in target.read_text()


class TestUpdateConfig:
    def test_update_config_downloads_only_manifest(self, tmp_cache, monkeypatch):
        calls = []

        def fake_download(filename, *, on_progress=core._null_hook):
            calls.append(filename)
            return tmp_cache / "scripts" / filename

        monkeypatch.setattr(core.catalog, "download_file", fake_download)
        core.update_config()
        assert calls == [core.CONFIG_FILENAME]


class TestCatalogDiff:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("sha256:abcdef1234567890", "abcdef1"),
            ("plainhash12345", "plainha"),
            (None, "-"),
            ("", "-"),
        ],
    )
    def test_short_hash(self, value, expected):
        assert core.short_hash(value) == expected

    def test_read_catalog_meta_missing(self, tmp_cache):
        assert core.read_catalog_meta() == {}
        assert core.has_cached_config() is False

    def test_update_config_reports_changes(self, tmp_cache, monkeypatch):
        core.CACHE_SCRIPT_DIR.mkdir(parents=True)
        cfg = core.CACHE_SCRIPT_DIR / core.CONFIG_FILENAME
        cfg.write_text(
            json.dumps(
                {
                    "scripts": {
                        "a": {
                            "path": "a.sh",
                            "version": "1.0.0",
                            "hash": "sha256:aaa1",
                        },
                        "b": {
                            "path": "b.sh",
                            "version": "1.0.0",
                            "hash": "sha256:bbb1",
                        },
                        "gone": {
                            "path": "g.sh",
                            "version": "1.0.0",
                            "hash": "sha256:g",
                        },
                    }
                }
            )
        )
        assert core.has_cached_config() is True
        new = {
            "scripts": {
                "a": {"path": "a.sh", "version": "1.0.0", "hash": "sha256:aaa1"},
                "b": {"path": "b.sh", "version": "1.1.0", "hash": "sha256:bbb2"},
                "c": {"path": "c.sh", "version": "1.0.0", "hash": "sha256:ccc1"},
            }
        }

        def fake_download(filename, *, on_progress=core._null_hook):
            cfg.write_text(json.dumps(new))
            return cfg

        monkeypatch.setattr(core.catalog, "download_file", fake_download)
        by = {c.name: c for c in core.update_config()}
        assert set(by) == {"b", "c", "gone"}
        assert by["b"].status == "changed"
        assert by["b"].old_version == "1.0.0" and by["b"].new_version == "1.1.0"
        assert by["c"].status == "added"
        assert by["gone"].status == "removed"
