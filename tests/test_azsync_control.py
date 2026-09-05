"""Behavioral azsync regression tests; see azsync_support for fixtures."""

from __future__ import annotations
import time
import pytest
import azsync
import usm_daemon
from usm_signal import SignalEvent, SignalResult
from usm_azure import (
    SasToken,
)
from azsync import (
    NETWORK,
    OK,
    PARTIAL,
    Supervisor,
)

from azsync_support import (
    fail_step,
    invoke,
    make_checkpoint,
    make_job,
    ok_step,
    publish_job,
    wait_until,
)


class TestSupervisorSignalQueue:
    def _supervisor(self, job, state_dir, publisher=None):
        queue = azsync.SignalQueue(state_dir / "signals")
        publisher = (
            publisher
            or type(
                "Publisher",
                (),
                {
                    "next_wake": None,
                    "run": lambda self, token, **kwargs: azsync.PublishRun(),
                    "ledger": type("Ledger", (), {"transactions": {}})(),
                },
            )()
        )
        return Supervisor(
            job,
            publisher=publisher,
            signals=queue,
            log=lambda _m: None,
        ), queue

    def test_sync_event_forces_next_tick(self, tmp_path, state_dir, monkeypatch):
        job = make_job(tmp_path)
        sup, queue = self._supervisor(job, state_dir)
        event = queue.submit("sync")
        calls = []
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda reason, signal=None: calls.append((reason, signal))
            or azsync.SyncResult(status=OK),
        )
        decision = sup.tick()
        assert decision.reason == "manual"
        assert calls[0][1].id == event.id
        assert queue.read_result(event.id).status == OK

    def test_flush_payload_reaches_publisher(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        seen = {}

        class Publisher:
            next_wake = None
            ledger = type("Ledger", (), {"transactions": {}})()

            def run(self, token, **kwargs):
                seen.update(kwargs)
                return azsync.PublishRun(published=1)

        sup, queue = self._supervisor(job, state_dir, Publisher())
        event = queue.submit(
            "flush",
            {"checkpoint": "checkpoints/checkpoint-100", "settle": 0.25},
        )
        # Avoid transport; this test is about event semantics.
        sup.sas = type(
            "Sas",
            (),
            {
                "enabled": False,
                "ensure": lambda self, now, **kw: SasToken("", None),
                "current": lambda self: None,
                "needed_lifetime": lambda self, duration: 1,
                "provider": type("P", (), {"refreshable": False})(),
            },
        )()
        sup.engine = type(
            "Engine",
            (),
            {
                "build_argv": lambda self, token: ["sync"],
                "run": lambda self, argv, **kw: azsync.SyncResult(status=OK),
            },
        )()
        sup.tick()
        assert seen["flush_checkpoint"] == "checkpoints/checkpoint-100"
        assert seen["flush_settle"] == 0.25
        assert queue.read_result(event.id).detail["publish"]["published"] == 1

    def test_unknown_event_is_completed_invalid(self, tmp_path, state_dir, monkeypatch):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        sup.state.last_sync_end = time.time()
        event = queue.submit("dance")
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda *a, **kw: pytest.fail("invalid event must not sync"),
        )
        sup.tick()
        assert queue.read_result(event.id).status == "invalid"

    def test_waiting_flush_result_is_distinct(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        sup, queue = self._supervisor(job, state_dir)
        event = queue.submit("flush")
        published = azsync.PublishRun(
            status="waiting",
            discovered=1,
            waiting=[{"path": "x", "reason": "waiting for marker"}],
        )
        sup._complete_signal(event, published)
        assert queue.read_result(event.id).status == "waiting"

    def test_partial_result_is_preserved(self, tmp_path, state_dir):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        sup.state.last_sync_end = time.time()
        event = queue.submit("sync")
        sup._complete_signal(event, azsync.SyncResult(status=PARTIAL))
        assert queue.read_result(event.id).status == PARTIAL

    def test_result_updates_runtime_state(self, tmp_path, state_dir):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        event = queue.submit("sync")
        sup._complete_signal(event, azsync.SyncResult(status=OK))
        assert sup.state.signal_last_kind == "sync"
        assert sup.state.signal_last_result == OK
        assert sup.state.signal_last_at is not None

    def test_corrupt_event_does_not_kill_supervisor(
        self, tmp_path, state_dir, monkeypatch
    ):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        sup.state.last_sync_end = time.time()
        queue._prepare()
        (queue.pending / "bad.json").write_text("{")
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda *a, **kw: pytest.fail("corrupt event must not sync"),
        )
        assert not sup.tick().should_sync

    def test_legacy_trigger_file_still_forces_sync(
        self, tmp_path, state_dir, monkeypatch
    ):
        job = make_job(tmp_path)
        sup, _queue = self._supervisor(job, state_dir)
        azsync.trigger_path(job.id).write_text("legacy")
        calls = []
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda reason, event=None: calls.append((reason, event))
            or azsync.SyncResult(status=OK),
        )
        sup.tick()
        assert calls == [("manual", None)]
        assert not azsync.trigger_path(job.id).exists()


class TestSignalCli:
    def _define(self, tmp_path, **kwargs):
        job = publish_job(tmp_path, id="training", **kwargs)
        azsync.save_job(job)
        azsync.save_state(job.id, azsync.RuntimeState())
        return job

    def test_flush_submits_event_to_live_daemon(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        submitted = []

        def submit(job_id, kind, payload=None):
            submitted.append((job_id, kind, payload))
            return SignalEvent.create(kind, payload), True

        monkeypatch.setattr(azsync, "submit_daemon_signal", submit)
        result = invoke(
            runner,
            [
                "flush",
                "training",
                "--checkpoint",
                "checkpoints/checkpoint-100",
                "--settle",
                "0.5",
            ],
        )
        assert result.exit_code == 0
        assert submitted[0][1:] == (
            "flush",
            {"checkpoint": "checkpoints/checkpoint-100", "settle": 0.5},
        )

    def test_flush_requires_publish_policy(self, tmp_path, state_dir, runner):
        job = make_job(tmp_path, id="plain")
        azsync.save_job(job)
        result = invoke(runner, ["flush", "plain"])
        assert result.exit_code != 0 and "no --publish" in result.output

    def test_flush_wait_success(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        event = SignalEvent.create("flush")
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (event, True),
        )
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: SignalResult(
                event_id,
                OK,
                time.time(),
                {"publish": {"published": 1, "retained": 1, "deleted": 0}},
            ),
        )
        result = invoke(runner, ["flush", "training", "--wait"])
        assert result.exit_code == 0 and "1 checkpoint" in result.output
        assert "1 retained locally" in result.output

    @pytest.mark.parametrize(
        "status,expected",
        [("waiting", 2), (PARTIAL, 3), (NETWORK, 4)],
    )
    def test_flush_wait_exit_codes(
        self, tmp_path, state_dir, runner, monkeypatch, status, expected
    ):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        event = SignalEvent.create("flush")
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (event, True),
        )
        detail = {
            "publish": {
                "waiting": [{"reason": "not stable"}],
                "error": "publish failed",
            },
            "sync": {"error": "sync failed"},
        }
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: SignalResult(
                event_id, status, time.time(), detail
            ),
        )
        assert invoke(runner, ["flush", "training", "--wait"]).exit_code == expected

    def test_flush_wait_timeout_is_five(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        event = SignalEvent.create("flush")
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (event, True),
        )
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: None,
        )
        result = invoke(runner, ["flush", "training", "--wait", "--timeout", "0"])
        assert result.exit_code == 5 and "Timed out" in result.output

    def test_unacknowledged_flush_remains_queued(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (SignalEvent.create("flush"), False),
        )
        result = invoke(runner, ["flush", "training"])
        assert result.exit_code == 0 and "remains queued" in result.output


class TestFlushAcrossARealSupervisorProcess:
    def test_training_complete_signal_reaches_daemon_and_returns_result(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(
            tmp_path,
            id="training",
            auth="aad",
            publish_stable=3600,
        )
        make_checkpoint(job)
        azsync.save_job(job)
        azsync.save_state(job.id, azsync.RuntimeState())
        fake_azcopy.program(
            fail_step("BlobNotFound"),  # remote marker probe
            ok_step(completed=2, size=9),  # payload
            ok_step(completed=1, size=300),  # manifest
            ok_step(completed=1, size=0),  # marker
        )
        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(
                lambda: azsync.load_state(job.id).total_syncs >= 1,
                timeout=20,
            )
            event, acknowledged = azsync.submit_daemon_signal(
                job.id,
                "flush",
                {
                    "checkpoint": "checkpoints/checkpoint-100",
                    "settle": 0.05,
                },
            )
            assert acknowledged
            result = azsync.signal_queue(job.id).wait(event.id, 30, interval=0.05)
            assert result is not None
            assert result.status == OK
            assert result.detail["publish"]["published"] == 1
            calls = fake_azcopy.calls
            assert [call[0] for call in calls[:4]] == [
                "list",
                "copy",
                "copy",
                "copy",
            ]
            assert calls[-1][2].split("?", 1)[0].endswith("/.complete")
        finally:
            azsync.stop_daemon(job.id)
        assert wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)
