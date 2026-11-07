"""Tests for the copy/verification/deletion boundary of Azure Blob moves."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import mv
from azsync_support import ok_step


SOURCE = "https://acct.blob.core.windows.net/data/source"
DESTINATION = "https://acct.blob.core.windows.net/data/destination"


def copy_output(**changes):
    summary = {
        "JobStatus": "Completed",
        "TransfersCompleted": 2,
        "TransfersFailed": 0,
        "TransfersSkipped": 0,
    }
    summary.update(changes)
    return json.dumps(
        {"MessageType": "EndOfJob", "MessageContent": json.dumps(summary)}
    )


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(mv.cp, "check_blobfuse2_mountpoints", lambda: {})
    monkeypatch.setattr(mv.cp, "_find_azcopy", lambda: None)
    install = Mock(return_value="/fake/azcopy")
    sas = Mock(return_value="sig=GENERATED")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=copy_output()),
            subprocess.CompletedProcess([], 0),
        ]
    )
    monkeypatch.setattr(mv.cp, "ensure_azcopy", install)
    monkeypatch.setattr(mv.cp, "generate_sas_token", sas)
    monkeypatch.setattr(mv.subprocess, "run", run)
    return install, sas, run


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_aborted_move_has_no_side_effects(backend, answer):
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input=answer)
    assert result.exit_code != 0
    assert "[y/N]" in result.output
    for action in backend:
        action.assert_not_called()


def test_confirmed_move_copies_to_exact_destination_then_deletes(backend):
    install, sas, run = backend
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == 0, result.output
    install.assert_called_once_with()
    assert sas.call_count == 2
    copy, delete = run.call_args_list
    assert copy.args[0][:4] == [
        "/fake/azcopy",
        "copy",
        SOURCE + "?sig=GENERATED",
        DESTINATION + "?sig=GENERATED",
    ]
    assert "--as-subdir=false" in copy.args[0]
    assert "--overwrite=false" in copy.args[0]
    assert "--recursive" in copy.args[0]
    assert "--output-type=json" in copy.args[0]
    assert delete.args[0] == [
        "/fake/azcopy",
        "remove",
        SOURCE + "?sig=GENERATED",
        "--recursive",
    ]
    assert "Move completed." in result.output
    assert "GENERATED" not in result.output


def test_explicit_overwrite(backend):
    result = CliRunner().invoke(
        mv.move, ["--overwrite", SOURCE, DESTINATION], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert "--overwrite=true" in backend[2].call_args_list[0].args[0]
    assert "WILL be overwritten" in result.output


def test_use_az_does_not_generate_sas_or_modify_parent_env(backend, monkeypatch):
    monkeypatch.delenv("AZCOPY_AUTO_LOGIN_TYPE", raising=False)
    result = CliRunner().invoke(mv.move, ["--use-az", SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == 0, result.output
    backend[1].assert_not_called()
    for call in backend[2].call_args_list:
        assert call.kwargs["env"]["AZCOPY_AUTO_LOGIN_TYPE"] == "AZCLI"
    assert "AZCOPY_AUTO_LOGIN_TYPE" not in mv.rm.os.environ


def test_existing_sas_urls_preserved_and_hidden(backend):
    source, destination = (
        SOURCE + "?sig=SOURCE_SECRET",
        DESTINATION + "?sig=DEST_SECRET",
    )
    result = CliRunner().invoke(mv.move, [source, destination], input="y\n")
    assert result.exit_code == 0, result.output
    backend[1].assert_not_called()
    assert backend[2].call_args_list[0].args[0][2:4] == [source, destination]
    assert "SECRET" not in result.output


def test_dry_run_never_installs_authenticates_or_executes(backend):
    result = CliRunner().invoke(mv.move, ["--dry-run", "--use-az", SOURCE, DESTINATION])
    assert result.exit_code == 0, result.output
    assert f"azcopy copy {SOURCE} {DESTINATION}" in result.output
    assert f"azcopy remove {SOURCE} --recursive" in result.output
    assert "Only after a verified" in result.output
    assert "AZCOPY_AUTO_LOGIN_TYPE=AZCLI" in result.output
    for action in backend:
        action.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"JobStatus": "CompletedWithErrors"},
        {"JobStatus": "Cancelled"},
        {"TransfersFailed": 1},
        {"TransfersSkipped": 1},
        {"TransfersCompleted": 0},
        {"TransfersCompleted": None},
        {"TransfersCompleted": True},
        {"TransfersFailed": -1},
        {"TransfersSkipped": "bad"},
        {"ErrorMsg": "failure"},
    ],
)
def test_incomplete_or_skipped_copy_never_deletes(backend, changes):
    backend[2].side_effect = [
        subprocess.CompletedProcess([], 0, copy_output(**changes))
    ]
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == 1
    assert "source retained" in result.output
    backend[2].assert_called_once()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "not json",
        "[]",
        '{"MessageType":"EndOfJob","MessageContent":"{broken"}',
        '{"MessageType":"EndOfJob","MessageContent":"[]"}',
        '{"MessageType":"Info","MessageContent":"started"}',
        '{"MessageType":"Error","MessageContent":"SECRET"}\n' + copy_output(),
    ],
)
def test_missing_or_malformed_summary_never_deletes(backend, output):
    backend[2].side_effect = [subprocess.CompletedProcess([], 0, output)]
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == 1
    assert "source retained" in result.output
    assert "SECRET" not in result.output
    backend[2].assert_called_once()


@pytest.mark.parametrize("code,expected", [(7, 7), (-9, 137)])
def test_failed_copy_never_deletes_and_propagates_exit(backend, code, expected):
    backend[2].side_effect = [subprocess.CompletedProcess([], code, copy_output())]
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == expected
    assert "azcopy copy failed" in result.output
    backend[2].assert_called_once()


def test_delete_failure_reports_incomplete_move(backend):
    backend[2].side_effect = [
        subprocess.CompletedProcess([], 0, copy_output()),
        subprocess.CompletedProcess([], 5),
    ]
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == 5
    assert "azcopy remove failed" in result.output
    assert "Move completed." not in result.output


@pytest.mark.parametrize(
    "source,destination",
    [
        (SOURCE, SOURCE),
        (SOURCE + "/", SOURCE),
        (SOURCE, SOURCE + "/child"),
        (SOURCE + "/child", SOURCE),
        (SOURCE, SOURCE.replace(".blob.core.", ".dfs.core.")),
        (SOURCE, SOURCE.replace("source", "%73ource") + "?sig=OTHER"),
        (SOURCE, SOURCE + "?sig=OTHER"),
        (SOURCE, SOURCE + "//child"),
        (SOURCE + "/*", DESTINATION),
        (SOURCE + "?snapshot=old", DESTINATION),
        (SOURCE, DESTINATION + "?versionid=old"),
        (SOURCE + "#fragment", DESTINATION),
        (SOURCE + "/../other", DESTINATION),
        ("https://acct.blob.core.windows.net/data/", DESTINATION),
        ("./local", DESTINATION),
        (SOURCE, "./local"),
    ],
)
def test_invalid_or_overlapping_paths_fail_before_confirmation(
    backend, source, destination
):
    result = CliRunner().invoke(mv.move, [source, destination], input="y\n")
    assert result.exit_code == 2
    assert "Move this file/directory" not in result.output
    for action in backend:
        action.assert_not_called()


def test_blobfuse_paths_resolve_and_encode(backend, monkeypatch, tmp_path):
    mount = tmp_path / "mount"
    monkeypatch.setattr(
        mv.cp,
        "check_blobfuse2_mountpoints",
        lambda: {str(mount): {"url": "https://acct.blob.core.windows.net/data/"}},
    )
    result = CliRunner().invoke(
        mv.move, [str(mount / "old #1"), str(mount / "new #1")], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert backend[2].call_args_list[0].args[0][2:4] == [
        "https://acct.blob.core.windows.net/data/old%20%231?sig=GENERATED",
        "https://acct.blob.core.windows.net/data/new%20%231?sig=GENERATED",
    ]


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, ["az"]), FileNotFoundError("az not found")],
)
def test_auth_failure_retains_source(backend, error):
    backend[1].side_effect = error
    result = CliRunner().invoke(mv.move, [SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == 1
    backend[0].assert_not_called()
    backend[2].assert_not_called()


@pytest.mark.parametrize("skipped", [0, 1])
def test_real_subprocess_moves_only_after_full_success(
    fake_azcopy, monkeypatch, skipped
):
    monkeypatch.setattr(mv.cp, "check_blobfuse2_mountpoints", lambda: {})
    fake_azcopy.program(ok_step(TransfersSkipped=skipped), {"exit": 0})
    result = CliRunner().invoke(mv.move, ["--use-az", SOURCE, DESTINATION], input="y\n")
    assert result.exit_code == (1 if skipped else 0), result.output
    assert [call[0] for call in fake_azcopy.calls] == (
        ["copy"] if skipped else ["copy", "remove"]
    )
