from __future__ import annotations

import shutil
import subprocess
import sys
from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner

import serve


@pytest.fixture
def ssh_run(monkeypatch):
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, "dir Linux x86_64\n", "")
    )
    monkeypatch.setattr(serve.subprocess, "run", run)
    return run


def test_probe_uses_noninteractive_command_without_configured_forwards(ssh_run):
    probe = serve.probe_remote("li2", "~", timeout=90)
    assert probe == serve.RemoteProbe("dir", "Linux", "x86_64")
    args, kwargs = ssh_run.call_args
    argv = args[0]
    assert argv[0] == "ssh"
    assert argv[-2] == "li2"
    assert "if [ -d ~ ]" in argv[-1]
    for option in (
        "-T",
        "RemoteCommand=none",
        "BatchMode=yes",
        "ClearAllForwardings=yes",
        "StrictHostKeyChecking=accept-new",
        "ConnectTimeout=90",
    ):
        assert option in argv
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 90


@pytest.mark.parametrize(
    "stderr, expected",
    [
        (None, None),
        ("   ", None),
        ("jump host stalled", "jump host stalled"),
        (b"jump host \xff stalled", "jump host \ufffd stalled"),
    ],
)
def test_timeout_keeps_stderr_and_gives_repro_command(ssh_run, stderr, expected):
    ssh_run.side_effect = subprocess.TimeoutExpired(
        ["ssh", "li2", "private-snippet"], 30, stderr=stderr
    )
    with pytest.raises(click.ClickException) as caught:
        serve.probe_remote("li2", "~")
    msg = str(caught.value)
    assert "path/platform probe timed out after 30s" in msg
    assert "ssh -vvv -T" in msg
    assert "echo usm-ssh-ok" in msg
    assert "--ssh-timeout" in msg
    assert "BatchMode" in msg
    assert "private-snippet" not in msg
    assert "uname" not in msg
    if expected:
        assert f"SSH stderr:\n{expected}" in msg
    else:
        assert "SSH stderr:" not in msg
    assert ssh_run.call_count == 1


def test_timeout_bounds_stderr(ssh_run):
    ssh_run.side_effect = subprocess.TimeoutExpired(
        ["ssh"], 30, stderr=b"x" * 5000 + b"last error"
    )
    with pytest.raises(click.ClickException) as caught:
        serve.probe_remote("li2", "~")
    stderr = str(caught.value).split("SSH stderr:\n", 1)[1]
    assert len(stderr) == 2000
    assert stderr.endswith("last error")


def test_timeout_captures_real_process_stderr_without_reading_terminal(monkeypatch):
    run = subprocess.run
    child = (
        "import sys, time; "
        "assert sys.stdin.read() == ''; "
        "print('waiting for jump host', file=sys.stderr, flush=True); "
        "time.sleep(60)"
    )

    def fake_ssh(argv, **kwargs):
        assert argv[0] == "ssh"
        return run([sys.executable, "-c", child], **kwargs)

    monkeypatch.setattr(serve.subprocess, "run", fake_ssh)
    with pytest.raises(click.ClickException, match="waiting for jump host"):
        serve.probe_remote("li2", "~", timeout=1)


def test_missing_ssh_has_actionable_stage(ssh_run):
    ssh_run.side_effect = FileNotFoundError("ssh executable not found")
    with pytest.raises(click.ClickException, match="path/platform probe failed"):
        serve.probe_remote("li2", "~")


@pytest.mark.parametrize(
    "code, stdout, stderr, error",
    [
        (255, "", "Permission denied (publickey)", "Permission denied"),
        (0, "missing\n", "", "path not found"),
        (0, "", "", "unexpected probe output"),
        (0, "Welcome!\n", "", "unexpected probe output"),
    ],
)
def test_probe_failures(ssh_run, code, stdout, stderr, error):
    ssh_run.return_value = subprocess.CompletedProcess([], code, stdout, stderr)
    with pytest.raises(click.ClickException, match=error):
        serve.probe_remote("li2", "~")


def test_probe_accepts_login_banner_before_result(ssh_run):
    ssh_run.return_value.stdout = "Welcome!\ndir Linux aarch64\n"
    assert serve.probe_remote("li2", "~").machine == "aarch64"


@pytest.mark.parametrize(
    "code, stdout, stderr, error",
    [
        (255, "", "Permission denied", "Permission denied"),
        (1, "", "", "exit 1"),
        (0, "", "", "returned no output"),
    ],
)
@pytest.mark.parametrize("upgrade", [False, True])
def test_lookup_failure_never_attempts_download(
    ssh_run, code, stdout, stderr, error, upgrade
):
    ssh_run.return_value = subprocess.CompletedProcess([], code, stdout, stderr)
    with pytest.raises(click.ClickException, match=error):
        serve.ensure_remote_miniserve(
            "li2", serve.RemoteProbe("dir", "Linux", "x86_64"), upgrade=upgrade
        )
    assert ssh_run.call_count == 1


@pytest.mark.parametrize(
    "output, expected",
    [
        ("managed\n", serve.REMOTE_MINISERVE),
        ("/opt/bin/miniserve\n", "/opt/bin/miniserve"),
    ],
)
def test_lookup_existing_binary(ssh_run, output, expected):
    ssh_run.return_value.stdout = output
    assert (
        serve.ensure_remote_miniserve(
            "li2", serve.RemoteProbe("dir", "Linux", "x86_64"), timeout=90
        )
        == expected
    )
    assert ssh_run.call_count == 1
    assert ssh_run.call_args.kwargs["timeout"] == 90


@pytest.mark.parametrize("timeout, install_timeout", [(30, 180), (240, 240)])
def test_installation_keeps_download_budget(ssh_run, timeout, install_timeout):
    ssh_run.side_effect = [
        subprocess.CompletedProcess([], 0, "missing\n", ""),
        subprocess.CompletedProcess([], 0, "ok\n", ""),
    ]
    assert (
        serve.ensure_remote_miniserve(
            "li2", serve.RemoteProbe("dir", "Linux", "x86_64"), timeout=timeout
        )
        == serve.REMOTE_MINISERVE
    )
    assert [call.kwargs["timeout"] for call in ssh_run.call_args_list] == [
        timeout,
        install_timeout,
    ]


@pytest.mark.parametrize("operation", ["miniserve lookup", "miniserve installation"])
def test_setup_timeouts_report_the_correct_stage(ssh_run, operation):
    expired = subprocess.TimeoutExpired(["ssh"], 180, stderr=b"connection stalled")
    ssh_run.side_effect = (
        [expired]
        if operation == "miniserve lookup"
        else [subprocess.CompletedProcess([], 0, "missing\n", ""), expired]
    )
    with pytest.raises(click.ClickException, match=f"{operation} timed out"):
        serve.ensure_remote_miniserve(
            "li2", serve.RemoteProbe("dir", "Linux", "x86_64"), timeout=240
        )


def test_forward_runs_own_command_and_preserves_requested_tunnel(monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(serve, "_spawn", spawn)
    proc, port = serve.open_forward_serve(
        "li2", "~", serve.REMOTE_MINISERVE, 8080, "127.0.0.1", serve.MiniserveOpts()
    )
    argv = spawn.call_args.args[0]
    assert proc is spawn.return_value
    assert "RemoteCommand=none" in argv
    assert "-T" in argv
    assert "-L" in argv
    assert f"8080:127.0.0.1:{port}" in argv
    assert "ClearAllForwardings=yes" not in argv
    assert argv[-2] == "li2"
    assert argv[-1].startswith("exec ~/.cache/usm/bin/miniserve ")


@pytest.mark.parametrize(
    "timeout_args, expected", [([], 30), (["--ssh-timeout", "90"], 90)]
)
def test_cli_threads_timeout_through_both_setup_steps(
    monkeypatch, timeout_args, expected
):
    probe = Mock(return_value=serve.RemoteProbe("dir", "Linux", "x86_64"))
    install = Mock(return_value=serve.REMOTE_MINISERVE)
    forward = Mock(return_value=(Mock(), 45678))
    monkeypatch.setattr(serve, "probe_remote", probe)
    monkeypatch.setattr(serve, "ensure_remote_miniserve", install)
    monkeypatch.setattr(serve, "open_forward_serve", forward)
    monkeypatch.setattr(serve, "resolve_port", lambda _: 8080)
    monkeypatch.setattr(serve, "_wait_for_or_die", Mock())
    monkeypatch.setattr(serve, "run_until_done", Mock())
    result = CliRunner().invoke(serve.cli, ["li2:~", *timeout_args])
    assert result.exit_code == 0, result.output
    probe.assert_called_once_with("li2", "~", timeout=expected)
    install.assert_called_once_with(
        "li2", probe.return_value, upgrade=False, timeout=expected
    )
    forward.assert_called_once()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1.5"])
def test_cli_rejects_invalid_timeout(value):
    result = CliRunner().invoke(serve.cli, ["li2:~", "--ssh-timeout", value])
    assert result.exit_code == 2
    assert "Invalid value for '--ssh-timeout'" in result.output


def test_cli_probe_failure_does_not_install_or_start_server(monkeypatch, ssh_run):
    ssh_run.side_effect = subprocess.TimeoutExpired(
        ["ssh"], 30, stderr=b"proxy stalled"
    )
    install = Mock()
    forward = Mock()
    monkeypatch.setattr(serve, "ensure_remote_miniserve", install)
    monkeypatch.setattr(serve, "open_forward_serve", forward)
    monkeypatch.setattr(serve, "resolve_port", lambda _: 8080)
    result = CliRunner().invoke(serve.cli, ["li2:~"])
    assert result.exit_code == 1
    assert "path/platform probe timed out" in result.output
    assert "proxy stalled" in result.output
    install.assert_not_called()
    forward.assert_not_called()


def test_local_source_unchanged(tmp_path):
    source = serve.make_source(str(tmp_path), None, False, ssh_timeout=90)
    assert isinstance(source, serve.LocalServe)
    assert source.path == tmp_path


def test_ssh_effective_config_ignores_interactive_and_forwarding_defaults(tmp_path):
    ssh = shutil.which("ssh")
    if ssh is None:
        pytest.skip("OpenSSH is not installed")
    config = tmp_path / "config"
    config.write_text(
        "Host test-alias\n"
        "  HostName 192.0.2.1\n"
        "  User test-user\n"
        "  Port 2222\n"
        "  IdentityFile /fake/key\n"
        "  ProxyJump jump-alias\n"
        "  RequestTTY force\n"
        "  RemoteCommand sleep 600\n"
        "  LocalForward 12345 localhost:23456\n"
        "  RemoteForward 12346 localhost:23457\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [ssh, "-G", "-F", str(config), *serve._SSH_QUICK, "test-alias", "echo ok"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    config_values = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert config_values["hostname"] == "192.0.2.1"
    assert config_values["user"] == "test-user"
    assert config_values["port"] == "2222"
    assert config_values["identityfile"] == "/fake/key"
    assert config_values["proxyjump"] == "jump-alias"
    assert config_values["requesttty"] == "false"
    assert config_values.get("remotecommand", "none") == "none"
    assert "localforward" not in config_values
    assert "remoteforward" not in config_values
