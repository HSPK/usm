"""Tests for Azure Blob deletion and its confirmation boundary."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import rm


URL = "https://acct.blob.core.windows.net/cont/path"


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(rm.cp, "check_blobfuse2_mountpoints", lambda: {})
    monkeypatch.setattr(rm.cp, "_find_azcopy", lambda: None)
    install = Mock(return_value="/fake/azcopy")
    sas = Mock(return_value="sig=GENERATED")
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(rm.cp, "ensure_azcopy", install)
    monkeypatch.setattr(rm.cp, "generate_sas_token", sas)
    monkeypatch.setattr(rm.subprocess, "run", run)
    return install, sas, run


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_decline_or_eof_never_deletes(backend, answer):
    result = CliRunner().invoke(rm.remove, [URL], input=answer)
    assert result.exit_code != 0
    assert URL in result.output
    assert "[y/N]" in result.output
    assert "Aborted" in result.output
    for action in backend:
        action.assert_not_called()


def test_confirmed_deletion_generates_sas(backend):
    install, sas, run = backend
    result = CliRunner().invoke(rm.remove, [URL], input="y\n")
    assert result.exit_code == 0, result.output
    install.assert_called_once_with()
    sas.assert_called_once_with("acct", "cont")
    assert run.call_args.args[0] == [
        "/fake/azcopy",
        "remove",
        URL + "?sig=GENERATED",
        "--recursive",
    ]


def test_existing_sas_is_preserved_but_hidden_in_confirmation(backend):
    _, sas, run = backend
    url = URL + "?sv=1&sig=PRESET"
    result = CliRunner().invoke(rm.remove, [url], input="y\n")
    assert result.exit_code == 0, result.output
    sas.assert_not_called()
    assert run.call_args.args[0][2] == url
    assert "PRESET" not in result.output


def test_use_az_sets_child_environment_only(backend, monkeypatch):
    monkeypatch.delenv("AZCOPY_AUTO_LOGIN_TYPE", raising=False)
    _, sas, run = backend
    result = CliRunner().invoke(rm.remove, ["--use-az", URL], input="y\n")
    assert result.exit_code == 0, result.output
    sas.assert_not_called()
    assert run.call_args.args[0][2] == URL
    assert run.call_args.kwargs["env"]["AZCOPY_AUTO_LOGIN_TYPE"] == "AZCLI"
    assert "AZCOPY_AUTO_LOGIN_TYPE" not in rm.os.environ


def test_dry_run_has_no_external_side_effects(backend):
    result = CliRunner().invoke(rm.remove, ["--dry-run", URL])
    assert result.exit_code == 0, result.output
    assert f"azcopy remove {URL} --recursive" in result.output
    assert "Delete these targets?" not in result.output
    for action in backend:
        action.assert_not_called()


@pytest.mark.parametrize("relative", ["", "folder", "folder/file #1.txt"])
def test_blobfuse_path_resolution(backend, monkeypatch, tmp_path, relative):
    mount = tmp_path / "mount"
    monkeypatch.setattr(
        rm.cp,
        "check_blobfuse2_mountpoints",
        lambda: {str(mount): {"url": "https://acct.blob.core.windows.net/cont/"}},
    )
    result = CliRunner().invoke(rm.remove, [str(mount / relative)], input="y\n")
    assert result.exit_code == 0, result.output
    expected = "https://acct.blob.core.windows.net/cont/"
    expected += rm.urllib.parse.quote(relative, safe="/") + "?sig=GENERATED"
    assert backend[2].call_args.args[0][2] == expected


def test_nested_mount_uses_closest_match(backend, monkeypatch, tmp_path):
    mount = tmp_path / "mount"
    monkeypatch.setattr(
        rm.cp,
        "check_blobfuse2_mountpoints",
        lambda: {
            str(mount): {"url": "https://acct.blob.core.windows.net/outer/"},
            str(mount / "nested"): {"url": "https://acct.blob.core.windows.net/inner/"},
        },
    )
    result = CliRunner().invoke(rm.remove, ["-d", str(mount / "nested" / "file")])
    assert result.exit_code == 0, result.output
    assert "https://acct.blob.core.windows.net/inner/file" in result.output
    assert "/outer/" not in result.output


def test_all_targets_validated_before_any_deletion(backend, tmp_path):
    result = CliRunner().invoke(rm.remove, [URL, str(tmp_path / "local")], input="y\n")
    assert result.exit_code == 2
    assert "not an Azure Blob URL" in result.output
    for action in backend:
        action.assert_not_called()


def test_mount_prefix_sibling_is_not_a_blob_path(backend, monkeypatch, tmp_path):
    mount = tmp_path / "mount"
    monkeypatch.setattr(
        rm.cp,
        "check_blobfuse2_mountpoints",
        lambda: {str(mount): {"url": "https://acct.blob.core.windows.net/cont/"}},
    )
    result = CliRunner().invoke(rm.remove, ["-d", str(tmp_path / "mount-other")])
    assert result.exit_code == 2
    backend[2].assert_not_called()


def test_multiple_targets_share_one_confirmation(backend):
    result = CliRunner().invoke(rm.remove, [URL, URL + "/second"], input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Delete these targets?") == 1
    assert backend[2].call_count == 2


def test_azcopy_failure_propagates_and_stops(backend):
    backend[2].return_value = subprocess.CompletedProcess([], 7)
    result = CliRunner().invoke(
        rm.remove, [URL + "?sig=SECRET", URL + "/second?sig=SECRET"], input="y\n"
    )
    assert result.exit_code == 7
    assert "azcopy remove failed" in result.output
    assert "SECRET" not in result.output
    backend[2].assert_called_once()


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, ["az"]), FileNotFoundError("az not found")],
)
def test_authentication_errors_prevent_deletion(backend, error):
    backend[1].side_effect = error
    result = CliRunner().invoke(rm.remove, [URL], input="y\n")
    assert result.exit_code == 1
    assert "Error:" in result.output
    backend[0].assert_not_called()
    backend[2].assert_not_called()


@pytest.mark.parametrize(
    "args", [[], ["--unknown", URL], ["https://acct.blob.core.windows.net/"]]
)
def test_invalid_arguments_never_delete(backend, args):
    result = CliRunner().invoke(rm.remove, args, input="y\n")
    assert result.exit_code != 0
    for action in backend:
        action.assert_not_called()
