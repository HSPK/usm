from __future__ import annotations

import io
import os
import threading
import time

import pytest
from click.testing import CliRunner

import ssh

_REAL_EFFECTIVE_CONFIG = ssh.effective_config


@pytest.fixture(autouse=True)
def _no_ssh_config(monkeypatch):
    """Keep unit tests off the real `ssh -G`, and out of ~/.ssh/config."""
    monkeypatch.setattr(ssh, "effective_config", lambda *a, **k: {})


# Argument splitting -------------------------------------------------------


def test_split_ssh_args_separates_options_destination_and_command():
    options, destination, command = ssh.split_ssh_args(
        ["-p", "2222", "-4", "user@host", "uptime", "-l"]
    )

    assert options == ["-p", "2222", "-4"]
    assert destination == "user@host"
    assert command == ["uptime", "-l"]


def test_split_ssh_args_understands_glued_and_clustered_flags():
    options, destination, command = ssh.split_ssh_args(["-tp2222", "-tt", "host"])

    assert options == ["-tp2222", "-tt"]
    assert destination == "host"
    assert command == []


def test_split_ssh_args_consumes_value_from_end_of_cluster():
    options, destination, _ = ssh.split_ssh_args(["-tp", "2222", "host"])

    assert options == ["-tp", "2222"]
    assert destination == "host"


def test_split_ssh_args_handles_option_terminator():
    options, destination, command = ssh.split_ssh_args(["-4", "--", "host", "--", "ls"])

    assert options == ["-4"]
    assert destination == "host"
    assert command == ["ls"]


def test_split_ssh_args_without_destination():
    assert ssh.split_ssh_args([]) == ([], None, [])


def test_has_flag_ignores_letters_inside_option_values():
    assert ssh.has_flag(["-oStrictHostKeyChecking=no"], "t") is False
    assert ssh.has_flag(["-o", "SetEnv=t"], "t") is False
    assert ssh.has_flag(["-tt"], "t") is True
    assert ssh.has_flag(["-4", "-L", "8080:localhost:80"], "L") is True


# Command construction -----------------------------------------------------


def test_build_plan_injects_keepalives_before_the_destination():
    plan = ssh.build_plan("/usr/bin/ssh", ["-p", "2222", "host"])

    assert plan.argv[:4] == ["/usr/bin/ssh", "-p", "2222", "-o"]
    assert plan.argv[-1] == "host"
    assert "ServerAliveInterval=15" in plan.argv
    assert plan.destination == "host"
    assert plan.one_shot is False


def test_build_plan_appends_after_user_options_so_theirs_win(monkeypatch):
    # Fallback guarantee for when `ssh -G` is unavailable: ssh honours the
    # first value it sees, so ours must come after whatever the user typed.
    plan = ssh.build_plan("ssh", ["-o", "ServerAliveInterval=90", "host"])

    assert plan.argv.index("ServerAliveInterval=90") < plan.argv.index(
        "ServerAliveInterval=15"
    )


def test_build_plan_skips_options_the_user_already_configured(monkeypatch):
    monkeypatch.setattr(
        ssh, "effective_config", lambda *a, **k: {"serveraliveinterval": "90"}
    )

    plan = ssh.build_plan("ssh", ["host"])

    assert "ServerAliveInterval=15" not in plan.argv
    assert "ConnectTimeout=10" in plan.argv  # untouched by the user, so ours


def test_build_plan_adds_exit_on_forward_failure_only_with_forwards():
    plain = ssh.build_plan("ssh", ["host"])
    forwarded = ssh.build_plan("ssh", ["-L", "8080:localhost:80", "host"])

    assert "ExitOnForwardFailure=yes" not in plain.argv
    assert "ExitOnForwardFailure=yes" in forwarded.argv


def test_build_plan_can_skip_keepalives():
    plan = ssh.build_plan("ssh", ["host"], keepalive=False)

    assert plan.argv == ["ssh", "host"]


def test_build_plan_wraps_the_session_in_tmux():
    plan = ssh.build_plan("ssh", ["host"], mux="tmux", session="work")

    assert plan.command == ["tmux", "new-session", "-A", "-D", "-s", "work"]
    assert plan.argv[-len(plan.command) :] == plan.command
    assert "-t" in plan.argv
    assert plan.one_shot is False


def test_build_plan_does_not_duplicate_force_tty():
    plan = ssh.build_plan("ssh", ["-t", "host"], mux="tmux")

    assert plan.argv.count("-t") == 1


def test_build_plan_quotes_the_session_name():
    plan = ssh.build_plan("ssh", ["host"], mux="screen", session="my session")

    assert "'my session'" in plan.command


def test_build_plan_rejects_mux_with_remote_command():
    with pytest.raises(ssh.click.UsageError):
        ssh.build_plan("ssh", ["host", "uptime"], mux="tmux")


def test_build_plan_requires_a_destination():
    with pytest.raises(ssh.click.UsageError):
        ssh.build_plan("ssh", ["-4"])


def test_build_plan_marks_remote_commands_as_one_shot():
    assert ssh.build_plan("ssh", ["host", "uptime"]).one_shot is True


# Retry policy -------------------------------------------------------------


def _attempt(code: int, duration: float = 60.0, **kwargs) -> ssh.Attempt:
    return ssh.Attempt(code=code, duration=duration, **kwargs)


def test_clean_logout_never_reconnects():
    assert ssh.classify(_attempt(0), ssh.Policy()) is ssh.Verdict.DONE


def test_dropped_connection_reconnects():
    assert ssh.classify(_attempt(255), ssh.Policy()) is ssh.Verdict.RETRY


def test_remote_exit_status_is_passed_through():
    assert ssh.classify(_attempt(1), ssh.Policy()) is ssh.Verdict.DONE


def test_signalled_ssh_is_treated_as_user_intent():
    assert ssh.classify(_attempt(-2), ssh.Policy()) is ssh.Verdict.DONE


def test_tilde_dot_escape_does_not_drag_the_user_back_in():
    # `~.` tears the link down locally, so ssh exits 255 with no exit status
    # -- indistinguishable from a drop except for this message.
    attempt = _attempt(
        255, duration=1800.0, stderr_tail="\r\nConnection to box closed.\r\n"
    )

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.DONE


def test_remote_side_hangup_is_still_a_drop_worth_reconnecting():
    attempt = _attempt(
        255, duration=1800.0, stderr_tail="Connection to box closed by remote host.\r\n"
    )

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.RETRY


def test_auth_failure_is_fatal():
    attempt = _attempt(
        255, duration=1.0, stderr_tail="user@host: Permission denied (publickey)."
    )

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.FATAL


def test_host_key_change_is_fatal():
    attempt = _attempt(255, stderr_tail="REMOTE HOST IDENTIFICATION HAS CHANGED!")

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.FATAL


def test_transient_network_errors_still_reconnect():
    attempt = _attempt(255, duration=0.4, stderr_tail="ssh: connect to host: timed out")

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.RETRY


def test_dns_failure_is_transient_not_fatal():
    # Wifi and VPN drops take DNS with them; a real typo is caught by
    # the fail-fast budget instead.
    attempt = _attempt(
        255,
        duration=0.2,
        stderr_tail="ssh: Could not resolve hostname box: Temporary failure",
    )

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.RETRY


def test_stderr_classification_can_be_disabled():
    attempt = _attempt(255, stderr_tail="Permission denied (publickey).")

    verdict = ssh.classify(attempt, ssh.Policy(read_stderr=False))

    assert verdict is ssh.Verdict.RETRY


def test_one_shot_command_is_never_rerun_on_an_ambiguous_255():
    policy = ssh.Policy(one_shot=True)

    # Could be the command's own status; rerunning it may repeat side effects.
    assert ssh.classify(_attempt(255, duration=0.2), policy) is ssh.Verdict.DONE
    assert ssh.classify(_attempt(255, duration=30.0), policy) is ssh.Verdict.DONE


def test_one_shot_command_retries_when_the_session_never_came_up():
    policy = ssh.Policy(one_shot=True)
    attempt = _attempt(
        255, duration=11.0, stderr_tail="ssh: connect to host x port 22: timed out"
    )

    assert ssh.classify(attempt, policy) is ssh.Verdict.RETRY


def test_one_shot_retries_can_be_forced():
    policy = ssh.Policy(one_shot=True, retry_command=True)

    assert ssh.classify(_attempt(255), policy) is ssh.Verdict.RETRY


def test_wake_up_always_reconnects_even_on_a_clean_exit_code():
    attempt = _attempt(0, duration=0.1, woke=True)

    assert ssh.classify(attempt, ssh.Policy()) is ssh.Verdict.RETRY


# Backoff ------------------------------------------------------------------


def _no_jitter(_low, _high):
    return 0.0


def test_backoff_grows_and_is_capped():
    delays = [
        ssh.backoff_delay(n, base=2.0, cap=10.0, rng=_no_jitter) for n in range(1, 8)
    ]

    assert delays[0] == 2.0
    assert delays == sorted(delays)
    assert max(delays) == 10.0


def test_backoff_jitter_stays_within_bounds():
    for _ in range(100):
        delay = ssh.backoff_delay(3, base=2.0, cap=60.0)
        assert 0.0 <= delay <= 60.0


def test_backoff_does_not_overflow_for_absurd_failure_counts():
    assert ssh.backoff_delay(100_000, base=2.0, cap=60.0, rng=_no_jitter) == 60.0


# "Did we actually connect?" -----------------------------------------------


def test_connect_timeout_is_not_mistaken_for_a_live_session():
    # An unreachable host burns the whole ConnectTimeout (10s by default),
    # which outlasts min_uptime. Going by duration alone would call that a
    # healthy session, reset the backoff and never let fail-fast fire.
    attempt = _attempt(
        255,
        duration=10.5,
        stderr_tail="ssh: connect to host box port 22: Connection timed out",
    )

    assert ssh.session_established(attempt, ssh.Policy()) is False


def test_a_long_session_counts_as_established():
    # A real mid-session drop always leaves a strerror() string behind; the
    # empty-stderr version of this test proves nothing.
    attempt = _attempt(
        255,
        duration=3600.0,
        stderr_tail="Read from remote host box: Connection reset by peer\r\n",
    )

    assert ssh.session_established(attempt, ssh.Policy()) is True


def test_mid_session_reset_is_not_confused_with_a_failed_dial():
    # "Connection reset by peer" is printed both while dialling and from
    # packet.c hours into a session. Trusting it blindly would make the
    # supervisor announce "never connected" after three 2-hour sessions.
    long_session = _attempt(
        255, duration=7200.0, stderr_tail="Read from remote host box: Connection reset"
    )
    failed_dial = _attempt(
        255,
        duration=0.3,
        stderr_tail="ssh: connect to host box port 22: Connection reset",
    )

    assert ssh.never_connected(long_session, ssh.Policy()) is False
    assert ssh.never_connected(failed_dial, ssh.Policy()) is True


def test_mid_session_reset_does_not_rerun_a_one_shot_command():
    policy = ssh.Policy(one_shot=True)
    attempt = _attempt(
        255, duration=7200.0, stderr_tail="Read from remote host box: Connection reset"
    )

    assert ssh.classify(attempt, policy) is ssh.Verdict.DONE


def test_a_session_cut_right_after_login_is_not_established():
    assert ssh.session_established(_attempt(255, duration=0.4), ssh.Policy()) is False


def test_a_resumed_host_always_counts_as_established():
    attempt = _attempt(0, duration=0.1, woke=True)

    assert ssh.session_established(attempt, ssh.Policy()) is True


def test_duration_is_the_only_signal_without_stderr_classification():
    attempt = _attempt(255, duration=10.5, stderr_tail="Connection timed out")

    assert ssh.session_established(attempt, ssh.Policy(read_stderr=False)) is True


# Terminal repair ----------------------------------------------------------


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b[?1000l",  # click tracking
        "\x1b[?1002l",  # button-event tracking
        "\x1b[?1003l",  # any-event tracking
        "\x1b[?1006l",  # SGR coordinates
        "\x1b[?2004l",  # bracketed paste
        "\x1b[?1049l",  # alternate screen
        "\x1b[?25h",  # cursor visible
    ],
)
def test_sanitize_sequence_resets_the_modes_that_cause_garbage(sequence):
    assert sequence in ssh.SANITIZE_SEQUENCE


def test_sanitize_sequence_never_clears_the_scrollback():
    assert "\x1bc" not in ssh.SANITIZE_SEQUENCE


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_terminal_guard_writes_the_reset_burst(monkeypatch):
    fake = _FakeTty()
    monkeypatch.setattr(ssh.sys, "stdout", fake)

    ssh.TerminalGuard(sanitize=True).restore()

    assert fake.getvalue() == ssh.SANITIZE_SEQUENCE


def test_terminal_guard_respects_no_sanitize(monkeypatch):
    fake = _FakeTty()
    monkeypatch.setattr(ssh.sys, "stdout", fake)

    ssh.TerminalGuard(sanitize=False).restore()

    assert fake.getvalue() == ""


def test_terminal_guard_skips_non_tty_streams(monkeypatch):
    plain = io.StringIO()
    monkeypatch.setattr(ssh.sys, "stdout", plain)
    monkeypatch.setattr(ssh.sys, "stderr", plain)

    ssh.TerminalGuard(sanitize=True).restore()

    assert plain.getvalue() == ""


# ssh binary resolution ----------------------------------------------------


def _make_executable(directory, name, body):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_resolve_ssh_skips_usm_alias_shims(tmp_path, monkeypatch):
    shim_dir = tmp_path / "local-bin"
    real_dir = tmp_path / "usr-bin"
    _make_executable(
        shim_dir,
        "ssh",
        f'#!/usr/bin/env bash\n# {ssh.ALIAS_SHIM_MARKER}: ssh\nexec usm ssh "$@"\n',
    )
    real = _make_executable(real_dir, "ssh", "#!/bin/sh\n")
    monkeypatch.setattr(ssh.os, "name", "posix")
    monkeypatch.setenv("PATH", f"{shim_dir}:{real_dir}")

    assert ssh.resolve_ssh() == str(real)


def test_resolve_ssh_errors_when_only_a_shim_exists(tmp_path, monkeypatch):
    shim_dir = tmp_path / "local-bin"
    _make_executable(
        shim_dir, "ssh", f"#!/usr/bin/env bash\n# {ssh.ALIAS_SHIM_MARKER}: ssh\n"
    )
    monkeypatch.setattr(ssh.os, "name", "posix")
    monkeypatch.setenv("PATH", str(shim_dir))

    with pytest.raises(ssh.click.ClickException):
        ssh.resolve_ssh()


# Misc helpers -------------------------------------------------------------


def test_exit_status_maps_signals_the_way_a_shell_does():
    assert ssh.exit_status(0) == 0
    assert ssh.exit_status(255) == 255
    assert ssh.exit_status(-2) == 130  # SIGINT
    assert ssh.exit_status(-15) == 143  # SIGTERM


def test_suspend_gap_is_the_time_the_monotonic_clock_missed():
    assert ssh.suspend_gap(mono_delta=1.0, wall_delta=1.0) == 0.0
    assert ssh.suspend_gap(mono_delta=1.0, wall_delta=301.0) == 300.0


def test_format_duration():
    assert ssh.format_duration(42) == "42s"
    assert ssh.format_duration(125) == "2m05s"
    assert ssh.format_duration(3725) == "1h02m"


class _FakeStderr:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def test_stderr_tail_forwards_everything_and_keeps_the_end(monkeypatch):
    fake = _FakeStderr()
    monkeypatch.setattr(ssh.sys, "stderr", fake)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"0123456789abcdef")
    os.close(write_fd)  # EOF, so the pump returns on its own
    tail = ssh.StderrTail(limit=8)

    tail.pump(open(read_fd, "rb"))

    assert fake.buffer.getvalue() == b"0123456789abcdef"
    assert tail.text() == "89abcdef"


@pytest.mark.skipif(not ssh._CAN_POLL_PIPES, reason="POSIX pipe polling only")
def test_stderr_pump_stops_even_while_a_writer_holds_the_pipe(monkeypatch):
    # A ProxyCommand can inherit ssh's stderr and outlive it; a pump blocked
    # in read() would leak a thread and an fd on every single reconnect.
    fake = _FakeStderr()
    monkeypatch.setattr(ssh.sys, "stderr", fake)
    read_fd, write_fd = os.pipe()
    stream = open(read_fd, "rb")
    tail = ssh.StderrTail()
    thread = threading.Thread(target=tail.pump, args=(stream,), daemon=True)
    thread.start()
    try:
        os.write(write_fd, b"still here")
        time.sleep(ssh.POLL_INTERVAL * 2)
        tail.stop()
        thread.join(timeout=5.0)

        assert not thread.is_alive()
        assert tail.text() == "still here"
    finally:
        os.close(write_fd)


@pytest.mark.skipif(ssh.termios is None, reason="POSIX termios only")
def test_sane_termios_undoes_raw_mode():
    import pty
    import termios
    import tty

    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        assert not termios.tcgetattr(slave)[3] & termios.ICANON

        ssh.sane_termios(slave)

        lflag = termios.tcgetattr(slave)[3]
        assert lflag & termios.ICANON  # line editing back
        assert lflag & termios.ECHO  # typing visible again
        assert lflag & termios.ISIG  # Ctrl-C works again
    finally:
        os.close(master)
        os.close(slave)


def test_suspend_watch_fires_once_the_gap_exceeds_the_threshold(monkeypatch):
    clock = {"mono": 0.0, "wall": 0.0}
    sampled = threading.Event()

    def wall_clock():
        sampled.set()  # the watcher has taken its baseline
        return clock["wall"]

    monkeypatch.setattr(ssh.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(ssh, "suspend_clock", wall_clock)
    seen = []

    with ssh.SuspendWatch(seen.append, threshold=20.0, interval=0.01):
        assert sampled.wait(5.0), "watcher never sampled the clock"
        clock["mono"] += 1.0
        clock["wall"] += 121.0
        for _ in range(500):
            if seen:
                break
            time.sleep(0.01)

    assert seen and seen[0] == pytest.approx(120.0)


# CLI wiring ---------------------------------------------------------------


def _resolved(monkeypatch, *args):
    """Run the click command with --print-cmd and return the argv it built."""
    monkeypatch.setattr(ssh, "resolve_ssh", lambda explicit=None: "/usr/bin/ssh")
    monkeypatch.setattr(ssh, "effective_config", lambda *a, **k: {})
    result = CliRunner().invoke(ssh.cli, ["--print-cmd", *args])
    assert result.exit_code == 0, result.output
    return result.output.strip()


@pytest.mark.parametrize(
    "argument",
    [
        "-L8080:localhost:80",  # every one of these contains an "h"...
        "-i/home/me/id_ed25519",
        "-oStrictHostKeyChecking=no",
        "-oBatchMode=yes",
        "-F/etc/ssh/ssh_config",
        "-cchacha20-poly1305@openssh.com",
    ],
)
def test_glued_ssh_options_are_not_eaten_by_the_wrapper(monkeypatch, argument):
    # Registering any short option (such as "-h" for help) makes click's
    # short-option parser match it *inside* these tokens, print the wrapper's
    # help and exit 0 without ever running ssh.
    resolved = _resolved(monkeypatch, argument, "myhost")

    assert argument in resolved
    assert resolved.endswith("myhost")


def test_wrapper_options_are_still_parsed_before_ssh_arguments(monkeypatch):
    resolved = _resolved(monkeypatch, "--tmux", "--session", "work", "-p2222", "myhost")

    assert "-p2222" in resolved
    assert resolved.endswith("myhost tmux new-session -A -D -s work")


def test_fix_terminal_needs_no_destination(monkeypatch):
    monkeypatch.setattr(ssh.TerminalGuard, "reset", lambda self: None)

    result = CliRunner().invoke(ssh.cli, ["--fix-terminal"])

    assert result.exit_code == 0


def test_missing_destination_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(ssh, "resolve_ssh", lambda explicit=None: "/usr/bin/ssh")

    result = CliRunner().invoke(ssh.cli, ["--print-cmd", "-4"])

    assert result.exit_code == 2
    assert "No ssh destination" in result.output


# ~/.ssh/config interaction ------------------------------------------------


def test_keepalives_are_skipped_when_the_user_configured_them(monkeypatch):
    monkeypatch.setattr(ssh, "resolve_ssh", lambda explicit=None: "ssh")
    monkeypatch.setattr(
        ssh,
        "effective_config",
        lambda *a, **k: {"connecttimeout": "60", "serveraliveinterval": "0"},
    )
    result = CliRunner().invoke(ssh.cli, ["--print-cmd", "slow-bastion"])

    assert "ConnectTimeout=10" not in result.output  # user's 60 survives
    assert "ServerAliveInterval=15" in result.output  # still unset, so ours


def test_effective_config_parses_ssh_dash_g(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "user me\nconnecttimeout none\nserveraliveinterval 30\n"

    monkeypatch.setattr(ssh.subprocess, "run", lambda *a, **k: _Proc())

    config = _REAL_EFFECTIVE_CONFIG("ssh", [], "host")

    assert config["serveraliveinterval"] == "30"
    assert config["connecttimeout"] == "none"


def test_effective_config_is_empty_when_ssh_cannot_answer(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("no ssh")

    monkeypatch.setattr(ssh.subprocess, "run", boom)

    assert _REAL_EFFECTIVE_CONFIG("ssh", [], "host") == {}


# Supervisor loop ----------------------------------------------------------


def _supervisor(monkeypatch, attempts, **policy_kwargs):
    """Drive the real retry loop over a canned list of attempt outcomes."""
    plan = ssh.Plan(argv=["ssh", "box"], destination="box", command=[], mux=None)
    policy = ssh.Policy(retry_delay=0.0, max_delay=0.0, **policy_kwargs)
    supervisor = ssh.Supervisor(
        plan, policy, ssh.TerminalGuard(sanitize=False), quiet=True
    )
    pending = list(attempts)
    spawned = []

    def fake_spawn():
        attempt = pending.pop(0) if pending else pending_last[0]
        spawned.append(attempt)
        return attempt

    pending_last = [attempts[-1]]
    monkeypatch.setattr(supervisor, "_spawn", fake_spawn)
    monkeypatch.setattr(supervisor, "_sleep", lambda _delay: False)
    return supervisor, spawned


def test_loop_gives_up_when_the_host_was_never_reachable(monkeypatch):
    miss = _attempt(255, duration=0.2, stderr_tail="ssh: connect to host box port 22")
    supervisor, spawned = _supervisor(monkeypatch, [miss], fail_fast=3)

    assert supervisor.run() == 255
    assert len(spawned) == 3


def test_loop_keeps_retrying_a_host_that_answered_once(monkeypatch):
    live = _attempt(255, duration=600.0, stderr_tail="client_loop: Broken pipe")
    gone = _attempt(255, duration=0.2, stderr_tail="ssh: connect to host box port 22")
    supervisor, spawned = _supervisor(monkeypatch, [live, gone], fail_fast=3, retries=8)

    supervisor.run()

    # fail_fast=3 would have stopped at 3 without the "ever connected" rule.
    assert len(spawned) == 9


def test_loop_honours_the_retry_budget(monkeypatch):
    drop = _attempt(255, duration=600.0, stderr_tail="client_loop: Broken pipe")
    supervisor, spawned = _supervisor(monkeypatch, [drop], retries=2)

    assert supervisor.run() == 255
    assert len(spawned) == 3  # the first attempt plus two retries


def test_loop_stops_immediately_on_a_clean_logout(monkeypatch):
    supervisor, spawned = _supervisor(monkeypatch, [_attempt(0, duration=5.0)])

    assert supervisor.run() == 0
    assert len(spawned) == 1
