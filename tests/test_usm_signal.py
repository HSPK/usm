"""Durable supervisor signal queue tests."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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

    @pytest.mark.parametrize("kind", ["", "/", "../x", "a b", "💥", None, 1, []])
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

    def test_create_snapshots_the_payload_mapping(self):
        payload = {"x": 1}
        event = SignalEvent.create("sync", payload)
        payload["x"] = 2
        assert event.payload == {"x": 1}

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

    @pytest.mark.parametrize(
        "timestamp", [None, True, "1", [], float("nan"), float("inf"), -float("inf")]
    )
    @pytest.mark.parametrize("record_type", [SignalEvent, SignalResult])
    def test_timestamps_are_typed_and_finite(self, timestamp, record_type):
        with pytest.raises(SignalError, match="timestamp"):
            record_type("event", "ok", timestamp)

    @pytest.mark.parametrize("data", [None, False, [], [["key", "value"]], ""])
    @pytest.mark.parametrize(
        "record_type,fields",
        [
            (SignalEvent, ("id", "kind", "created_at", "payload")),
            (SignalResult, ("event_id", "status", "completed_at", "detail")),
        ],
    )
    def test_mapping_fields_are_not_coerced(self, record_type, fields, data):
        raw = dict(zip(fields, ("event", "ok", 1, data)))
        with pytest.raises(SignalError, match="object"):
            record_type.from_dict(raw)


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

    @pytest.mark.parametrize("payload", [False, [], [["key", "value"]]])
    def test_non_mapping_payload_is_refused(self, queue, payload):
        with pytest.raises(SignalError, match="object"):
            queue.submit("sync", payload)
        assert queue.pending_count() == 0

    @pytest.mark.parametrize("number", [float("nan"), float("inf")])
    def test_non_finite_json_is_refused(self, queue, number):
        with pytest.raises(SignalError, match="JSON"):
            queue.submit("sync", {"bad": number})
        assert queue.pending_count() == 0

    def test_id_collision_does_not_overwrite_existing_request(self, queue, monkeypatch):
        event = queue.submit("sync", {"original": True})
        monkeypatch.setattr(SignalEvent, "create", lambda *args: event)
        with pytest.raises(SignalError, match="already exists"):
            queue.submit("sync")
        assert queue.claim() == event

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_events_and_results_are_private(self, queue):
        event = queue.submit("sync")
        assert queue.event_path(event.id).stat().st_mode & 0o777 == 0o600
        queue.complete(queue.claim(), "ok")
        assert queue.result_path(event.id).stat().st_mode & 0o777 == 0o600
        assert not list(queue.pending.iterdir())
        assert list(queue.results.iterdir()) == [queue.result_path(event.id)]

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

    @pytest.mark.parametrize("detail", [False, [], [["key", "value"]]])
    def test_complete_result_must_be_mapping(self, queue, detail):
        event = queue.submit("sync")
        queue.claim()
        with pytest.raises(SignalError, match="object"):
            queue.complete(event, "ok", detail)
        assert queue.event_path(event.id, working=True).exists()
        assert queue.read_result(event.id) is None

    def test_first_terminal_result_is_immutable(self, queue):
        event = queue.submit("sync")
        queue.claim()
        first = queue.complete(event, "ok", {"original": True})
        assert queue.complete(event, "error", {"original": False}) == first
        assert queue.read_result(event.id) == first

    def test_concurrent_completions_return_the_same_terminal_result(self, queue):
        event = queue.submit("sync")
        queue.claim()
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(
                workers.map(
                    lambda index: queue.complete(event, "ok", {"winner": index}),
                    range(8),
                )
            )
        assert all(result == queue.read_result(event.id) for result in results)

    def test_missing_result_is_none(self, queue):
        assert queue.read_result("none") is None

    def test_corrupt_event_is_terminal_and_quarantined(self, queue):
        queue._prepare()
        (queue.pending / "bad.json").write_text("{")
        with pytest.raises(SignalError, match="queued"):
            queue.claim()
        assert not list(queue.working.iterdir())
        assert queue.wait("bad", 0).status == "invalid"
        assert [path.read_text() for path in queue.quarantine.iterdir()] == ["{"]

    def test_corrupt_result_is_reported(self, queue):
        queue._prepare()
        (queue.results / "bad.json").write_text("{")
        with pytest.raises(SignalError, match="result"):
            queue.read_result("bad")

    def test_result_must_match_lookup_id(self, queue):
        queue._prepare()
        queue.result_path("requested").write_text(
            json.dumps(SignalResult("different", "ok", 1).__dict__)
        )
        with pytest.raises(SignalError, match="mismatch"):
            queue.read_result("requested")

    @pytest.mark.parametrize(
        "changes",
        [
            {"event_id": "../escape"},
            {"status": False},
            {"completed_at": "1"},
            {"completed_at": float("inf")},
            {"detail": [["key", "value"]]},
            {"detail": {"bad": float("nan")}},
            {"detail": {"bad": float("inf")}},
        ],
    )
    def test_malformed_results_fail_closed(self, queue, changes):
        queue._prepare()
        content = json.dumps(SignalResult("event", "ok", 1).__dict__ | changes)
        queue.result_path("event").write_text(content)
        with pytest.raises(SignalError):
            queue.read_result("event")
        assert queue.result_path("event").read_text() == content

    @pytest.mark.parametrize(
        "event_id",
        ["../escape", ".", "..", "/absolute", "a/b", "a\\b", "a\x00b", "", None, 1],
    )
    @pytest.mark.parametrize(
        "operation",
        [
            lambda queue, ident: queue.event_path(ident),
            lambda queue, ident: queue.event_path(ident, working=True),
            lambda queue, ident: queue.result_path(ident),
            lambda queue, ident: queue.read_result(ident),
            lambda queue, ident: queue.wait(ident, 0),
            lambda queue, ident: queue.complete(SimpleNamespace(id=ident), "ok"),
        ],
        ids=["pending", "working", "result-path", "read", "wait", "complete"],
    )
    def test_all_path_apis_reject_unsafe_ids(self, queue, event_id, operation):
        with pytest.raises(SignalError, match="id"):
            operation(queue, event_id)
        assert not queue.root.exists()

    @pytest.mark.parametrize(
        "changes",
        [
            {"id": "different"},
            {"id": "../escape"},
            {"id": 1},
            {"payload": [["key", "value"]]},
            {"payload": []},
            {"payload": None},
            {"payload": {"bad": float("nan")}},
            {"payload": {"bad": float("inf")}},
            {"created_at": "1"},
            {"created_at": float("nan")},
        ],
    )
    @pytest.mark.parametrize("operation", ["claim", "recover"])
    def test_invalid_events_fail_closed(self, queue, changes, operation):
        event = queue.submit("sync")
        if operation == "recover":
            queue.claim()
        path = queue.event_path(event.id, working=operation == "recover")
        content = json.dumps(event.__dict__ | changes)
        path.write_text(content)
        with pytest.raises(SignalError):
            getattr(queue, operation)()
        assert queue.wait(event.id, 0).status == "invalid"
        assert not path.exists()
        assert [path.read_text() for path in queue.quarantine.iterdir()] == [content]
        assert queue.claim() is None

    @pytest.mark.parametrize("filename", ["bad.name.json", "..json", "bad\nname.json"])
    def test_unsafe_filenames_are_only_quarantined(self, queue, filename):
        queue._prepare()
        content = json.dumps(SignalEvent.create("sync").__dict__)
        (queue.pending / filename).write_text(content)
        with pytest.raises(SignalError, match="id"):
            queue.claim()
        assert not list(queue.results.iterdir())
        assert [path.read_text() for path in queue.quarantine.iterdir()] == [content]

    def test_symlink_event_is_not_followed(self, queue):
        queue._prepare()
        evidence = queue.root / "outside.json"
        evidence.write_text(json.dumps(SignalEvent("linked", "sync", 1).__dict__))
        queue.event_path("linked").symlink_to(evidence)
        with pytest.raises(SignalError, match="symlink"):
            queue.claim()
        assert queue.wait("linked", 0).status == "invalid"
        assert evidence.exists()
        assert any(path.is_symlink() for path in queue.quarantine.iterdir())

    @pytest.mark.skipif(os.name != "posix", reason="POSIX special files")
    @pytest.mark.parametrize("location", ["pending", "results"])
    @pytest.mark.parametrize("entry_type", ["fifo", "socket", "directory"])
    def test_non_regular_files_are_rejected_without_reading(
        self, queue, monkeypatch, location, entry_type
    ):
        queue._prepare()
        path = getattr(queue, location) / "special.json"
        if entry_type == "fifo":
            os.mkfifo(path)
        elif entry_type == "socket":
            monkeypatch.chdir(queue.root)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(f"{location}/special.json")
        else:
            path.mkdir()
        inode = path.lstat().st_ino
        original_open = os.open

        def nonblocking_open(name, flags, *args, **kwargs):
            if Path(name).name == path.name:
                assert flags & os.O_NONBLOCK
                assert flags & os.O_NOFOLLOW
            return original_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", nonblocking_open)
        with pytest.raises(SignalError, match="regular"):
            if location == "pending":
                queue.claim()
            else:
                queue.read_result("special")
        if location == "pending":
            assert queue.wait("special", 0).status == "invalid"
            evidence = list(queue.quarantine.iterdir())
            assert any(entry.lstat().st_ino == inode for entry in evidence)
        else:
            assert path.lstat().st_ino == inode

    @pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO and open flags")
    def test_fifo_swap_during_open_is_nonblocking_and_rejected(
        self, queue, monkeypatch
    ):
        event = queue.submit("sync")
        working = queue.event_path(event.id, working=True)
        original_open = os.open

        def swap_for_fifo(name, flags, *args, **kwargs):
            if Path(name) == working:
                assert flags & os.O_NONBLOCK
                assert flags & os.O_NOFOLLOW
                working.unlink()
                os.mkfifo(working)
            return original_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap_for_fifo)
        with pytest.raises(SignalError):
            queue.claim()
        assert queue.wait(event.id, 0).status == "invalid"
        assert any(
            stat.S_ISFIFO(path.lstat().st_mode) for path in queue.quarantine.iterdir()
        )

    def test_overflowed_json_payload_is_rejected(self, queue):
        event = queue.submit("sync")
        content = json.dumps(event.__dict__ | {"payload": {"bad": "placeholder"}})
        queue.event_path(event.id).write_text(content.replace('"placeholder"', "1e999"))
        with pytest.raises(SignalError):
            queue.claim()
        assert queue.wait(event.id, 0).status == "invalid"

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

    def test_simultaneous_writers_and_readers_preserve_every_request(self, queue):
        writers_done = threading.Event()

        def submit(group):
            writer = SignalQueue(queue.root)
            return [
                writer.submit("sync", {"index": index})
                for index in range(group * 20, (group + 1) * 20)
            ]

        def drain():
            reader = SignalQueue(queue.root)
            claimed = []
            while not writers_done.is_set() or reader.pending_count():
                event = reader.claim()
                if event is None:
                    time.sleep(0.001)
                    continue
                reader.complete(event, "ok", event.payload)
                claimed.append(event.id)
            return claimed

        with ThreadPoolExecutor(max_workers=8) as workers:
            readers = [workers.submit(drain) for _ in range(4)]
            writers = [workers.submit(submit, group) for group in range(4)]
            try:
                submitted = [event for writer in writers for event in writer.result(10)]
            finally:
                writers_done.set()
            claimed = [ident for reader in readers for ident in reader.result(10)]
        assert sorted(claimed) == sorted(event.id for event in submitted)
        assert all(
            queue.wait(event.id, 0).detail == event.payload for event in submitted
        )
        assert queue.claim() is None
        assert not list(queue.working.iterdir())


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

    def test_corrupt_pending_duplicate_is_terminal(self, queue):
        event = queue.submit("sync")
        queue.claim()
        queue.event_path(event.id).write_text("{")
        with pytest.raises(SignalError):
            queue.recover()
        assert queue.wait(event.id, 0).status == "invalid"
        queue.recover()
        assert queue.claim() is None
        assert "{" in [path.read_text() for path in queue.quarantine.iterdir()]

    def test_conflicting_copies_are_terminal_and_preserved(self, queue):
        event = queue.submit("sync", {"original": True})
        queue.claim()
        queue.event_path(event.id).write_text(
            json.dumps(event.__dict__ | {"payload": {"original": False}})
        )
        with pytest.raises(SignalError, match="conflicting"):
            queue.recover()
        assert queue.wait(event.id, 0).status == "invalid"
        assert queue.claim() is None
        assert {
            json.loads(path.read_text())["payload"]["original"]
            for path in queue.quarantine.iterdir()
        } == {True, False}

    def test_recover_empty(self, queue):
        assert queue.recover() == 0

    def test_crash_after_result_publication_does_not_replay(self, queue, monkeypatch):
        event = queue.submit("sync")
        queue.claim()
        working = queue.event_path(event.id, working=True)
        original = Path.unlink

        def fail_cleanup(path, *args, **kwargs):
            if path == working:
                raise OSError("simulated crash after result publication")
            return original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_cleanup)
            with pytest.raises(SignalError, match="simulated crash"):
                queue.complete(event, "ok")
        result = queue.read_result(event.id)
        assert working.exists()
        queue.recover()
        assert not working.exists()
        assert queue.claim() is None
        assert queue.wait(event.id, 0) == result

    def test_completed_pending_duplicate_is_not_claimed(self, queue):
        event = queue.submit("sync")
        result = queue.complete(event, "ok")
        assert queue.claim() is None
        assert queue.wait(event.id, 0) == result

    def test_repeated_completion_keeps_invalid_evidence(self, queue):
        event = queue.submit("sync")
        queue.claim()
        queue.event_path(event.id, working=True).write_text("{")
        queue.result_path(event.id).write_text(
            json.dumps(SignalResult(event.id, "invalid", 1).__dict__)
        )
        assert queue.complete(event, "ok").status == "invalid"
        assert [path.read_text() for path in queue.quarantine.iterdir()] == ["{"]

    def test_crash_before_quarantine_preserves_invalid_evidence(
        self, queue, monkeypatch
    ):
        queue._prepare()
        queue.event_path("broken").write_text("{")

        def fail_quarantine(path):
            raise OSError("simulated quarantine failure")

        with monkeypatch.context() as patch:
            patch.setattr(queue, "_quarantine", fail_quarantine)
            with pytest.raises(SignalError, match="quarantine failure"):
                queue.claim()
        assert queue.wait("broken", 0).status == "invalid"
        with pytest.raises(SignalError):
            queue.recover()
        assert [path.read_text() for path in queue.quarantine.iterdir()] == ["{"]
        assert queue.claim() is None


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
    @pytest.mark.parametrize("keep", [0, 2, 100])
    def test_pruning_is_opt_in_for_slow_waiters(self, queue, keep):
        events = []
        for i in range(5):
            event = queue.submit("sync")
            queue.claim()
            queue.complete(event, "ok", {"i": i})
            events.append(event)
        queue.prune_results(keep)
        assert all(queue.wait(event.id, 0).status == "ok" for event in events)

    def test_explicit_pruning_retains_recent_and_recovery_results(self, queue):
        old, recent, active = [queue.submit("sync") for _ in range(3)]
        queue.complete(queue.claim(), "ok")
        queue.complete(queue.claim(), "ok")
        queue.complete(active, "ok")
        ancient = time.time() - 2 * 86400
        for event in (old, active):
            os.utime(queue.result_path(event.id), (ancient, ancient))
        queue.prune_results(0, min_age=86400)
        assert queue.read_result(old.id) is None
        assert queue.wait(recent.id, 0).status == "ok"
        assert queue.wait(active.id, 0).status == "ok"

    def test_explicit_pruning_keeps_newest_old_results(self, queue):
        events = []
        for index in range(3):
            event = queue.submit("sync")
            queue.complete(queue.claim(), "ok")
            ancient = time.time() - (3 - index) * 86400
            os.utime(queue.result_path(event.id), (ancient, ancient))
            events.append(event)
        queue.prune_results(1, min_age=86400)
        assert queue.read_result(events[0].id) is None
        assert queue.read_result(events[1].id) is None
        assert queue.wait(events[2].id, 0).status == "ok"

    def test_prune_empty(self, queue):
        assert queue.prune_results() == 0


class TestStorageErrors:
    def test_prepare_errors_are_signal_errors(self, queue, monkeypatch):
        def fail(*args, **kwargs):
            raise PermissionError("cannot create directory")

        monkeypatch.setattr(Path, "mkdir", fail)
        with pytest.raises(SignalError, match="cannot create directory"):
            queue.submit("sync")

    def test_publication_errors_leave_no_temporary_event(self, queue, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("cannot publish")

        monkeypatch.setattr(os, "link", fail)
        with pytest.raises(SignalError, match="cannot publish"):
            queue.submit("sync")
        assert not list(queue.pending.iterdir())

    @pytest.mark.parametrize("operation", ["claim", "recover"])
    @pytest.mark.parametrize("error", [PermissionError, FileNotFoundError])
    def test_move_errors_preserve_events(self, queue, monkeypatch, operation, error):
        event = queue.submit("sync")
        if operation == "recover":
            queue.claim()

        def fail(*args, **kwargs):
            raise error("cannot move")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(SignalError, match="cannot move"):
            getattr(queue, operation)()
        assert queue.event_path(event.id, working=operation == "recover").exists()

    def test_recovery_does_not_hide_result_publication_errors(self, queue, monkeypatch):
        event = queue.submit("sync")
        queue.claim()
        queue.event_path(event.id, working=True).write_text("{")

        def fail(*args, **kwargs):
            raise FileNotFoundError("cannot publish invalid result")

        monkeypatch.setattr(os, "link", fail)
        with pytest.raises(SignalError, match="cannot publish invalid result"):
            queue.recover()
        assert queue.event_path(event.id, working=True).read_text() == "{"

    def test_event_io_errors_are_not_treated_as_corruption(self, queue, monkeypatch):
        event = queue.submit("sync")

        def fail(*args, **kwargs):
            raise PermissionError("cannot read")

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", fail)
            with pytest.raises(SignalError, match="cannot read"):
                queue.claim()
        assert queue.event_path(event.id, working=True).exists()
        assert queue.read_result(event.id) is None
        assert not list(queue.quarantine.iterdir())

    @pytest.mark.parametrize("operation", ["pending_count", "recover"])
    def test_listing_errors_are_not_silenced(self, queue, monkeypatch, operation):
        queue._prepare()

        def fail(*args, **kwargs):
            raise PermissionError("cannot list")

        monkeypatch.setattr(Path, "iterdir", fail)
        with pytest.raises(SignalError, match="cannot list"):
            getattr(queue, operation)()

    def test_result_io_errors_are_not_silenced(self, queue, monkeypatch):
        event = queue.submit("sync")
        queue.complete(queue.claim(), "ok")

        def fail(*args, **kwargs):
            raise PermissionError("cannot read result")

        monkeypatch.setattr(os, "open", fail)
        with pytest.raises(SignalError, match="cannot read result"):
            queue.read_result(event.id)

    def test_prune_errors_are_not_silenced(self, queue, monkeypatch):
        event = queue.submit("sync")
        queue.complete(queue.claim(), "ok")
        ancient = time.time() - 2 * 86400
        os.utime(queue.result_path(event.id), (ancient, ancient))

        def fail(*args, **kwargs):
            raise PermissionError("cannot remove result")

        monkeypatch.setattr(Path, "unlink", fail)
        with pytest.raises(SignalError, match="cannot remove result"):
            queue.prune_results(0, min_age=86400)
