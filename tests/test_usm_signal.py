"""Durable supervisor signal queue tests."""

from __future__ import annotations

import json
import threading
import time

import pytest

from usm_signal import SignalError, SignalEvent, SignalQueue, SignalResult


@pytest.fixture
def queue(tmp_path):
    return SignalQueue(tmp_path / "signals")


class TestEvents:
    def test_create(self):
        event = SignalEvent.create("flush", {"checkpoint": "x"})
        assert event.kind == "flush"
        assert event.payload["checkpoint"] == "x"
        assert len(event.id) > 20

    @pytest.mark.parametrize("kind", ["", "/", "../x", "a b", "💥"])
    def test_bad_kind(self, kind):
        with pytest.raises(SignalError):
            SignalEvent.create(kind)

    def test_ids_are_unique_and_sortable(self):
        events = [SignalEvent.create("sync") for _ in range(20)]
        assert len({event.id for event in events}) == 20
        assert [event.id for event in events] == sorted(event.id for event in events)

    def test_round_trip(self):
        event = SignalEvent.create("sync", {"x": 1})
        assert SignalEvent.from_dict(event.__dict__) == event

    @pytest.mark.parametrize(
        "raw",
        [None, [], {}, {"id": "x"}, {"id": "../x", "kind": "sync", "created_at": 1}],
    )
    def test_bad_shapes(self, raw):
        with pytest.raises(SignalError):
            SignalEvent.from_dict(raw)

    def test_result_round_trip(self):
        result = SignalResult("e", "ok", 1.0, {"x": 1})
        assert SignalResult.from_dict(result.__dict__) == result

    @pytest.mark.parametrize("raw", [None, [], {}, {"event_id": "x"}])
    def test_bad_result_shapes(self, raw):
        with pytest.raises(SignalError):
            SignalResult.from_dict(raw)


class TestQueue:
    def test_submit_persists_before_returning(self, queue):
        event = queue.submit("sync", {"why": "training done"})
        raw = json.loads(queue.event_path(event.id).read_text())
        assert raw["kind"] == "sync"
        assert raw["payload"]["why"] == "training done"

    def test_submit_prepares_directories(self, queue):
        queue.submit("sync")
        assert queue.pending.is_dir()
        assert queue.working.is_dir()
        assert queue.results.is_dir()

    def test_non_json_payload_is_refused(self, queue):
        with pytest.raises(SignalError, match="JSON"):
            queue.submit("sync", {"bad": object()})

    def test_claim_oldest(self, queue):
        first = queue.submit("sync")
        second = queue.submit("flush")
        assert queue.claim() == first
        assert queue.claim() == second
        assert queue.claim() is None

    def test_claim_moves_to_working(self, queue):
        event = queue.submit("sync")
        queue.claim()
        assert not queue.event_path(event.id).exists()
        assert queue.event_path(event.id, working=True).exists()

    def test_complete_writes_result_and_removes_working(self, queue):
        event = queue.submit("sync")
        queue.claim()
        result = queue.complete(event, "ok", {"files": 3})
        assert result.status == "ok"
        assert queue.read_result(event.id).detail["files"] == 3
        assert not queue.event_path(event.id, working=True).exists()

    def test_complete_result_must_be_json(self, queue):
        event = queue.submit("sync")
        queue.claim()
        with pytest.raises(SignalError, match="JSON"):
            queue.complete(event, "ok", {"bad": object()})
        assert queue.event_path(event.id, working=True).exists()

    def test_missing_result_is_none(self, queue):
        assert queue.read_result("none") is None

    def test_corrupt_event_is_reported_and_removed(self, queue):
        queue._prepare()
        (queue.pending / "bad.json").write_text("{")
        with pytest.raises(SignalError, match="queued"):
            queue.claim()
        assert not list(queue.working.iterdir())

    def test_corrupt_result_is_reported(self, queue):
        queue._prepare()
        (queue.results / "bad.json").write_text("{")
        with pytest.raises(SignalError, match="result"):
            queue.read_result("bad")

    def test_pending_count(self, queue):
        assert queue.pending_count() == 0
        queue.submit("sync")
        queue.submit("flush")
        assert queue.pending_count() == 2
        queue.claim()
        assert queue.pending_count() == 1

    def test_concurrent_submitters_do_not_overwrite(self, queue):
        ids = []
        lock = threading.Lock()

        def submit(index):
            event = queue.submit("sync", {"index": index})
            with lock:
                ids.append(event.id)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(ids)) == 50
        claimed = []
        while event := queue.claim():
            claimed.append(event)
        assert len(claimed) == 50
        assert {event.payload["index"] for event in claimed} == set(range(50))

    def test_two_claimers_get_each_event_once(self, queue):
        for i in range(30):
            queue.submit("sync", {"i": i})
        claimed = []
        lock = threading.Lock()

        def drain():
            while True:
                event = queue.claim()
                if event is None:
                    return
                with lock:
                    claimed.append(event.id)

        threads = [threading.Thread(target=drain) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(claimed) == len(set(claimed)) == 30


class TestCrashRecovery:
    def test_working_event_returns_to_pending(self, queue):
        event = queue.submit("flush")
        queue.claim()
        assert queue.recover() == 1
        assert queue.claim() == event

    def test_pending_copy_wins_over_duplicate_working(self, queue):
        event = queue.submit("flush")
        queue._prepare()
        queue.event_path(event.id, working=True).write_text(
            queue.event_path(event.id).read_text()
        )
        assert queue.recover() == 1
        assert queue.event_path(event.id).exists()
        assert not queue.event_path(event.id, working=True).exists()

    def test_recover_empty(self, queue):
        assert queue.recover() == 0


class TestWait:
    def test_returns_completed_result(self, queue):
        event = queue.submit("sync")
        queue.claim()
        queue.complete(event, "ok")
        assert queue.wait(event.id, 1).status == "ok"

    def test_waits_for_other_thread(self, queue):
        event = queue.submit("sync")

        def finish():
            time.sleep(0.05)
            queue.claim()
            queue.complete(event, "ok", {"done": True})

        thread = threading.Thread(target=finish)
        thread.start()
        result = queue.wait(event.id, 2, interval=0.01)
        thread.join()
        assert result.detail["done"] is True

    def test_timeout_is_none(self, queue):
        event = queue.submit("sync")
        assert queue.wait(event.id, 0.02, interval=0.005) is None

    def test_zero_timeout_checks_once(self, queue):
        event = queue.submit("sync")
        assert queue.wait(event.id, 0) is None


class TestPruning:
    def test_keeps_newest_results(self, queue):
        events = []
        for i in range(5):
            event = queue.submit("sync")
            queue.claim()
            queue.complete(event, "ok", {"i": i})
            events.append(event)
            time.sleep(0.002)
        assert queue.prune_results(2) == 3
        left = list(queue.results.glob("*.json"))
        assert len(left) == 2
        assert {path.stem for path in left} == {events[-1].id, events[-2].id}

    def test_keep_zero_removes_all(self, queue):
        event = queue.submit("sync")
        queue.claim()
        queue.complete(event, "ok")
        assert queue.prune_results(0) == 1
        assert not list(queue.results.iterdir())

    def test_prune_empty(self, queue):
        assert queue.prune_results() == 0
