"""Tests for alias-shim install/uninstall and config-only update helpers."""

from __future__ import annotations

import os

import pytest

from usmo import core


@pytest.fixture
def tmp_local_bin(tmp_path, monkeypatch):
    """Redirect ~/.local/bin to a temp dir."""
    bin_dir = tmp_path / "local" / "bin"
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

        monkeypatch.setattr(core, "download_file", fake_download)
        core.update_config()
        assert calls == [core.CONFIG_FILENAME]
