"""Tests for helpers and CLI wiring in scripts/cp.py."""

from __future__ import annotations

import io
import tarfile
import zipfile

import pytest
from click.testing import CliRunner

import cp


class TestBlobDetection:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://acct.blob.core.windows.net/c/x", True),
            ("http://acct.blob.core.windows.net/c/x", True),
            ("https://acct.dfs.core.windows.net/c/x", True),
            ("https://acct.blob.core.chinacloudapi.cn/c/x", True),
            ("https://example.com/c/x", False),
            ("/mnt/data/x", False),
            ("./local", False),
        ],
    )
    def test_is_https_blob(self, url, expected):
        assert cp._is_https_blob(url) is expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://a.blob.core.windows.net/c/x?sv=1&sig=abc", True),
            ("https://a.blob.core.windows.net/c/x?sig=abc", True),
            ("https://a.blob.core.windows.net/c/x?sv=1", False),
            ("https://a.blob.core.windows.net/c/x", False),
        ],
    )
    def test_has_sas(self, url, expected):
        assert cp._has_sas(url) is expected

    def test_parse_blob_url(self):
        acct, cont = cp._parse_blob_url(
            "https://myacct.blob.core.windows.net/mycontainer/path/to/file?sig=x"
        )
        assert acct == "myacct"
        assert cont == "mycontainer"

    def test_parse_blob_url_missing_container(self):
        with pytest.raises(cp.click.ClickException):
            cp._parse_blob_url("https://myacct.blob.core.windows.net/")


class TestAzcopyTarget:
    @pytest.mark.parametrize(
        "machine,expected",
        [
            ("x86_64", "amd64"),
            ("amd64", "amd64"),
            ("aarch64", "arm64"),
            ("arm64", "arm64"),
        ],
    )
    def test_normalize_arch(self, machine, expected):
        assert cp._normalize_arch(machine) == expected

    def test_download_url_resolves(self, monkeypatch):
        monkeypatch.setattr(cp.platform, "system", lambda: "Linux")
        monkeypatch.setattr(cp.platform, "machine", lambda: "x86_64")
        assert cp._azcopy_download_url() == "https://aka.ms/downloadazcopy-v10-linux"

    def test_download_url_unmapped(self, monkeypatch):
        monkeypatch.setattr(cp.platform, "system", lambda: "Plan9")
        monkeypatch.setattr(cp.platform, "machine", lambda: "sparc")
        with pytest.raises(cp.click.ClickException):
            cp._azcopy_download_url()

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("azcopy_linux_amd64_10.0.0/azcopy", True),
            ("azcopy_windows_amd64_10.0.0/azcopy.exe", True),
            ("azcopy_linux_amd64_10.0.0/", False),
            ("azcopy_linux_amd64_10.0.0/NOTICE.txt", False),
        ],
    )
    def test_is_azcopy_member(self, name, expected):
        assert cp._is_azcopy_member(name) is expected


class TestExtractBinary:
    def test_extract_from_targz(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            payload = b"#!/bin/sh\necho azcopy\n"
            info = tarfile.TarInfo("azcopy_linux_amd64_10.0.0/azcopy")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        data = cp._extract_azcopy_binary(buf.getvalue(), "x")
        assert data == b"#!/bin/sh\necho azcopy\n"

    def test_extract_from_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("azcopy_windows_amd64_10.0.0/azcopy.exe", b"MZbinary")
        data = cp._extract_azcopy_binary(buf.getvalue(), "x")
        assert data == b"MZbinary"

    def test_extract_unknown_format(self):
        with pytest.raises(cp.click.ClickException):
            cp._extract_azcopy_binary(b"not an archive", "x")


class TestCli:
    def _run(self, args):
        return CliRunner().invoke(cp.copy, args)

    def test_local_to_local_uses_cp(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cp, "check_blobfuse2_mountpoints", lambda: {})
        src = tmp_path / "a"
        src.mkdir()
        dst = tmp_path / "b"
        result = self._run(["--dry-run", str(src), str(dst)])
        assert result.exit_code == 0
        assert "Handing over to native cp" in result.output
        assert "cp -r" in result.output

    def test_https_dest_appends_sas(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cp, "check_blobfuse2_mountpoints", lambda: {})
        monkeypatch.setattr(
            cp, "generate_sas_token", lambda a, c, **k: "sv=x&sig=GENERATED"
        )
        src = tmp_path / "a"
        src.mkdir()
        url = "https://acct.blob.core.windows.net/cont/path"
        result = self._run(["--dry-run", str(src), url])
        assert result.exit_code == 0
        assert "azcopy" in result.output
        assert "sig=GENERATED" in result.output
        assert "--recursive" in result.output

    def test_https_source_with_existing_sas_kept(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cp, "check_blobfuse2_mountpoints", lambda: {})

        def _boom(*a, **k):
            raise AssertionError("generate_sas_token should not be called")

        monkeypatch.setattr(cp, "generate_sas_token", _boom)
        url = "https://acct.blob.core.windows.net/cont/file?sv=1&sig=PRESET"
        dst = tmp_path / "out"
        result = self._run(["--dry-run", url, str(dst)])
        assert result.exit_code == 0
        assert "sig=PRESET" in result.output

    def test_blobfuse_source_to_local(self, monkeypatch, tmp_path):
        mnt = tmp_path / "mnt"
        (mnt / "sub").mkdir(parents=True)
        f = mnt / "sub" / "file.txt"
        f.write_text("x")
        mountpoints = {
            str(mnt): {
                "url": "https://acct.blob.core.windows.net/cont/",
                "account_name": "acct",
                "container_name": "cont",
            }
        }
        monkeypatch.setattr(cp, "check_blobfuse2_mountpoints", lambda: mountpoints)
        monkeypatch.setattr(cp, "generate_sas_token", lambda a, c, **k: "sig=MOUNTSAS")
        dst = tmp_path / "out"
        result = self._run(["--dry-run", str(f), str(dst)])
        assert result.exit_code == 0
        assert (
            "https://acct.blob.core.windows.net/cont/sub/file.txt?sig=MOUNTSAS"
            in result.output
        )
        assert "--recursive" in result.output

    def test_use_az_skips_sas(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cp, "check_blobfuse2_mountpoints", lambda: {})

        def _boom(*a, **k):
            raise AssertionError("generate_sas_token must not run with --use-az")

        monkeypatch.setattr(cp, "generate_sas_token", _boom)
        src = tmp_path / "a"
        src.mkdir()
        url = "https://acct.blob.core.windows.net/cont/path"
        result = self._run(["--dry-run", "--use-az", str(src), url])
        assert result.exit_code == 0
        assert "sig=" not in result.output
        assert url in result.output

    def test_install_invokes_download(self, monkeypatch, tmp_path):
        target = tmp_path / "azcopy"

        def fake_download(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("#!/bin/sh\necho 'azcopy version 10.0.0-test'\n")
            dest.chmod(0o755)

        monkeypatch.setattr(cp, "_local_azcopy", lambda: target)
        monkeypatch.setattr(cp, "_azcopy_download_url", lambda: "http://fake")
        monkeypatch.setattr(cp, "_download_azcopy", fake_download)
        result = self._run(["--install"])
        assert result.exit_code == 0
        assert target.exists()
        assert "azcopy installed at" in result.output
        assert "10.0.0-test" in result.output
