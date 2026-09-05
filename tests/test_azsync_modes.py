"""Single-mode supervision: public outcomes, retry deadlines, and cleanup."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import azsync as az
from azsync_support import _Clock, make_job, publish_job, ok_step
from usm_azure import SasError, SasToken


class FakeEngine:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def build_argv(self, _token):
        return ["sync"]

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        return next(self.outcomes)


class FakePublisher:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.next_wake = None
        self.ledger = SimpleNamespace(transactions={})

    def run(self, token, **kwargs):
        self.calls.append(kwargs)
        return next(self.outcomes)


def supervisor(tmp_path, mode, outcomes, ident="job"):
    job = (publish_job if mode == "publisher" else make_job)(tmp_path, id=ident)
    engine = FakeEngine(outcomes if mode == "normal" else [])
    publisher = FakePublisher(outcomes if mode == "publisher" else [])
    sas = SimpleNamespace(
        enabled=False,
        current=lambda: None,
        provider=SimpleNamespace(refreshable=False),
        needed_lifetime=lambda _: 1,
        ensure=lambda *a, **kw: SasToken("", None),
    )
    sup = az.Supervisor(
        job,
        engine=engine,
        publisher=publisher,
        sas=sas,
        clock=_Clock(),
        log=lambda _: None,
    )
    return sup, engine, publisher


def outcome(mode, **kwargs):
    return (az.PublishRun if mode == "publisher" else az.SyncResult)(**kwargs)


@pytest.mark.parametrize("mode", ["normal", "publisher"])
@pytest.mark.parametrize(
    "status", [az.OK, az.PARTIAL, az.NETWORK, az.AUTH_INVALID, az.FATAL, az.CANCELLED]
)
def test_job_invokes_one_mode_only(tmp_path, state_dir, mode, status):
    sup, engine, publisher = supervisor(tmp_path, mode, [outcome(mode, status=status)])
    result = sup.run_sync("manual")
    assert result.status == status
    assert len(engine.calls) == (mode == "normal")
    assert len(publisher.calls) == (mode == "publisher")
    state = az.load_state(sup.job.id)
    assert state.last_result == status and state.total_syncs == 1
    assert sup._running is False and sup._child is None
    record = az.read_history(sup.job.id)[-1]
    assert record["mode"] == mode
    assert record["result"]["status"] == status
    assert "retain" not in record


def test_many_normal_partial_runs_do_not_touch_checkpoint_job(tmp_path, state_dir):
    logs, log_engine, unused = supervisor(
        tmp_path,
        "normal",
        [az.SyncResult(status=az.PARTIAL, failed=1, completed=3) for _ in range(100)],
        "logs",
    )
    checkpoints, unused_engine, publisher = supervisor(
        tmp_path, "publisher", [az.PublishRun(published=1)], "checkpoints"
    )
    for _ in range(100):
        logs.run_sync("active log")
    checkpoints.run_sync("ready")
    assert len(log_engine.calls) == 100 and unused.calls == []
    assert unused_engine.calls == [] and len(publisher.calls) == 1
    assert logs.state.consecutive_failures == 0
    assert checkpoints.state.last_result == az.OK


def test_normal_excludes_checkpoint_namespace_explicitly(tmp_path):
    job = make_job(tmp_path, excludes=["checkpoints/"])
    argv = az.AzcopyEngine(job, binary="azcopy").build_argv(None)
    assert argv[1] == "sync"
    assert "checkpoints" in argv[argv.index("--exclude-path") + 1].split(";")
    assert not job.is_publisher


def test_publisher_transport_cannot_build_ordinary_sync(tmp_path):
    job = publish_job(tmp_path)
    with pytest.raises(az.PublishError, match="cannot run ordinary"):
        az.AzcopyEngine(job, binary="azcopy").build_argv(None)


def test_help_advertises_inferred_mode_and_time_only_parameters():
    text = CliRunner().invoke(az.cli, ["add", "--help"]).output
    for flag in (
        "--mode ",
        "--batch-files",
        "--batch-bytes",
        "--min-files",
        "split-publish",
    ):
        assert flag not in text
    for flag in (
        "--quiet-period",
        "--max-delay",
        "--interval",
        "--min-gap",
        "--publish-pattern",
    ):
        assert flag in text


@pytest.mark.parametrize("mode", ["normal", "publisher"])
def test_retry_survives_restart_and_resets_on_success(tmp_path, state_dir, mode):
    sup, _, _ = supervisor(
        tmp_path, mode, [outcome(mode, status=az.NETWORK, error="offline")]
    )
    sup.run_sync("first")
    restarted, engine, publisher = supervisor(tmp_path, mode, [outcome(mode)])
    assert restarted.state.backoff_until == restarted.clock() + 30
    assert not restarted.tick().should_sync
    restarted.clock.advance(30)
    assert restarted.tick().should_sync
    assert restarted.state.backoff_until is None
    assert restarted.state.consecutive_failures == 0
    assert len(engine.calls) + len(publisher.calls) == 1


@pytest.mark.parametrize("mode", ["normal", "publisher"])
def test_auth_failure_returns_dirty_batch_once(tmp_path, state_dir, mode):
    sup, engine, publisher = supervisor(tmp_path, mode, [])
    sup.acc.record(100, size=5)

    def denied(*a, **kw):
        raise SasError("token unavailable")

    sup.sas.ensure = denied
    result = sup.run_sync("auth")
    assert result.status == az.AUTH_INVALID
    assert sup.acc.snapshot().files == 1
    assert engine.calls == publisher.calls == []


@pytest.mark.parametrize("mode", ["normal", "publisher"])
def test_stop_prevents_new_work(tmp_path, state_dir, mode):
    sup, engine, publisher = supervisor(tmp_path, mode, [])
    sup.request_stop()
    assert sup.run_sync("stopping").status == az.CANCELLED
    assert engine.calls == publisher.calls == []


def test_status_has_one_runtime_not_two_lanes(tmp_path, state_dir):
    job = publish_job(tmp_path)
    az.save_job(job)
    state = az.RuntimeState(
        total_syncs=5,
        total_failures=1,
        last_result=az.NETWORK,
        last_error="publisher down",
        publish_last_path="checkpoints/checkpoint-10",
    )
    az.save_state(job.id, state)
    text = CliRunner().invoke(az.cli, ["status", job.id]).output
    assert "publisher down" in text and "checkpoints/checkpoint-10" in text
    assert "5 / 1" in text and "Retain lane" not in text
    assert az.load_state(job.id) == state
    raw = json.loads(az._state_path(job.id).read_text())
    assert "retain" not in raw and "publish" not in raw


@pytest.mark.parametrize("field", ["batch_files", "batch_bytes", "min_files"])
def test_removed_volume_settings_are_ignored_in_saved_definitions(state_dir, field):
    raw = {
        "id": "old",
        "source": "/tmp",
        "dest": "https://a.blob.core.windows.net/b",
        field: 1,
    }
    az._def_path("old").write_text(json.dumps(raw))
    job = az.load_job("old")
    assert not hasattr(job, field)
    assert not job.is_publisher


@pytest.mark.parametrize("mode", ["normal", "publisher"])
@pytest.mark.parametrize(
    "status,exit_code", [(az.OK, 0), (az.PARTIAL, 3), (az.NETWORK, 4), ("waiting", 2)]
)
def test_direct_and_queued_results_use_same_exit_code(
    tmp_path, state_dir, mode, status, exit_code
):
    sup, _, _ = supervisor(tmp_path, mode, [outcome(mode, status=status)])
    result = sup.run_sync("manual")
    event = sup.signals.submit("sync")
    sup.signals.claim()
    sup._complete_signal(event, result)
    saved = sup.signals.read_result(event.id)
    assert saved.status == status
    assert saved.detail["mode"] == mode
    assert (saved.detail["publish"] is None) == (mode == "normal")
    if exit_code:
        with pytest.raises(SystemExit) as raised:
            az._wait_for_signal(sup.job, event, 0)
        assert raised.value.code == exit_code
    else:
        az._wait_for_signal(sup.job, event, 0)


@pytest.mark.parametrize(
    "payload", [{"settle": -1}, {"settle": "x"}, {"checkpoint": "../outside"}]
)
def test_invalid_flush_gets_terminal_result_without_running(
    tmp_path, state_dir, payload
):
    sup, engine, publisher = supervisor(tmp_path, "publisher", [])
    sup.state.last_sync_end = sup.clock()
    event = sup.signals.submit("flush", payload)
    sup.tick()
    assert sup.signals.read_result(event.id).status == "invalid"
    assert engine.calls == publisher.calls == []


@pytest.mark.parametrize(
    "field", ["quiet_period", "max_delay", "interval", "min_gap", "poll_interval"]
)
@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_invalid_clock_settings_rejected(tmp_path, field, value):
    job = make_job(tmp_path, **{field: value})
    with pytest.raises(az.click.ClickException):
        az.validate_job(job)


def test_time_policy_handles_epoch_zero_and_expired_heartbeat():
    cfg = az.TriggerConfig(quiet_period=5, max_delay=300, interval=3600, min_gap=0)
    assert (
        az.decide(
            5,
            az.ChangeStat(files=1, first_at=0, last_at=0),
            az.PolicyInput(last_end=0),
            cfg,
        ).reason
        == "quiet"
    )
    assert (
        az.decide(
            300,
            az.ChangeStat(files=1, first_at=0, last_at=300),
            az.PolicyInput(last_end=0),
            cfg,
        ).reason
        == "max-delay"
    )
    assert (
        az.decide(
            4000,
            az.ChangeStat(files=1, first_at=3999, last_at=4000),
            az.PolicyInput(last_end=0),
            cfg,
        ).reason
        == "heartbeat"
    )


def test_huge_failure_streak_does_not_overflow():
    assert az.backoff_delay(100000) == az.BACKOFF_MAX


@pytest.mark.parametrize("status", ["Cancelled", "Failed", "InProgress"])
def test_unsuccessful_job_status_is_not_success_even_with_zero_exit(status):
    result = az.interpret_result({"JobStatus": status}, [], 0, 0)
    assert result.status != az.OK


@pytest.mark.parametrize(
    "counter", ["TransfersFailed", "TransfersCompleted", "TotalBytesTransferred"]
)
@pytest.mark.parametrize("value", [-1, "broken", []])
def test_malformed_transfer_counters_fail_closed(counter, value):
    result = az.interpret_result({counter: value}, [], 0, 0)
    assert result.status == az.FATAL


def test_publisher_engine_inherits_stop_and_child_tracking(
    tmp_path, state_dir, fake_azcopy
):
    job = publish_job(tmp_path, auth="aad")
    engine = az.AzcopyEngine(job, state_dir=tmp_path / "work")
    sup = az.Supervisor(job, engine=engine, log=lambda _: None)
    fake_azcopy.program(ok_step(completed=1, size=1))
    result = engine.run([engine.binary, "copy", "source", "destination"])
    assert result.status == az.OK
    assert sup._child is not None and sup._child.poll() is not None
    sup.request_stop()
    result = engine.run([engine.binary, "copy", "source", "destination"])
    assert result.status == az.CANCELLED
    assert len(fake_azcopy.calls) == 1


def test_oserror_is_persisted_and_restores_running_flag(tmp_path, state_dir):
    sup, engine, _ = supervisor(tmp_path, "normal", [])

    def denied(*args, **kwargs):
        raise PermissionError("azcopy permission denied")

    engine.run = denied
    result = sup.run_sync("attempt")
    assert result.status == az.FATAL
    assert sup._running is False
    assert az.load_state(sup.job.id).last_error == "azcopy permission denied"
