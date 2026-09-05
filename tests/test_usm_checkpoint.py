"""Publication barriers, deletion ownership, and restart behavior without Azure."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from usm_checkpoint import PublishCoordinator, PublishRun
from usm_publish import (
    PublishError,
    PublishLedger,
    PublishPolicy,
    clean_quarantine,
    discover,
    flush_candidates,
    quarantine,
    snapshot_unit,
)


@dataclass
class Transfer:
    status: str = "ok"
    completed: int = 1
    failed: int = 0
    skipped: int = 0
    bytes: int = 7
    error: str | None = None


class Engine:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.on_run = lambda phase: None
        self.remote = False

    def remote_exists(self, rel, sas):
        self.calls.append(("probe", rel))
        return self.remote

    def remove_remote(self, rel, sas):
        self.calls.append(("remove", rel))
        self.remote = False
        return Transfer()

    def build_publish_argv(self, snapshot, sas, *, marker, dry_run=False):
        return ["payload", snapshot.relpath]

    def build_exact_copy_argv(self, path, rel, sas):
        return ["manifest" if rel.endswith(".azsync-manifest.json") else "marker", rel]

    def run(self, argv):
        self.calls.append(tuple(argv))
        phase = argv[0]
        self.on_run(phase)
        result = self.results.get(phase, Transfer())
        if phase == "marker" and result.status == "ok":
            self.remote = True
        return result


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "source"
    (source / "checkpoints").mkdir(parents=True)
    policy = PublishPolicy(
        paths=("checkpoints",),
        patterns=("checkpoint-*",),
        stable=0,
        keep_last=0,
    )
    job = SimpleNamespace(
        id="test",
        source_path=lambda: source,
        publish_policy=lambda: policy,
    )
    engine = Engine()

    def coordinator(*, stop=None, **overrides):
        nonlocal policy
        policy = replace(policy, **overrides)
        return PublishCoordinator(
            job,
            engine,
            ledger_path=tmp_path / "state" / "ledger.json",
            stopped=stop,
        )

    def create(name="checkpoint-1"):
        path = source / "checkpoints" / name
        if policy.unit == "directory":
            path.mkdir()
            payload, marker = path / "weights", path / policy.ready_marker
        else:
            payload, marker = path, path.with_name(path.name + policy.ready_marker)
        payload.write_bytes(b"weights")
        marker.touch()
        stamp = time.time() - 60
        os.utime(payload, (stamp, stamp))
        os.utime(marker, (stamp + 1, stamp + 1))
        return path, payload, marker

    return SimpleNamespace(
        source=source,
        engine=engine,
        coordinator=coordinator,
        create=create,
        token=SimpleNamespace(token="sig=x"),
    )


def test_publish_order_and_restart_skip(setup):
    coordinator = setup.coordinator()
    path, _, _ = setup.create()
    result = coordinator.run(setup.token)
    assert result.status == "ok" and result.published == 1 and path.exists()
    assert [call[0] for call in setup.engine.calls] == [
        "probe",
        "payload",
        "manifest",
        "marker",
    ]
    assert not list(coordinator.ledger_path.parent.glob("*.manifest.json"))
    restarted = setup.coordinator()
    assert restarted.run(setup.token).published == 0
    assert len(setup.engine.calls) == 4 and restarted.next_wake is None


def test_publish_run_success_and_summary():
    result = PublishRun(published=2, deleted=1, retained=1)
    assert result.ok and result.summary() == "2 published, 1 deleted, 1 retained"
    result.status = "waiting"
    result.waiting = [{"path": "checkpoint", "reason": "waiting for marker"}]
    assert not result.ok and result.summary().endswith(", 1 waiting")


@pytest.mark.parametrize("flush", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_blocked_candidates_report_waiting_even_after_some_publish(setup, flush, mixed):
    coordinator = setup.coordinator()
    _, _, marker = setup.create()
    marker.unlink()
    if mixed:
        setup.create("checkpoint-2")
    result = coordinator.run(
        setup.token,
        flush_settle=0 if flush else None,
        sleep=lambda _: None,
    )
    assert result.status == "waiting" and not result.ok and result.error is None
    assert result.published == int(mixed)
    assert result.waiting == [
        {
            "path": "checkpoints/checkpoint-1",
            "reason": "waiting for marker",
        }
    ]


def test_already_published_candidates_are_not_blocked(setup):
    coordinator = setup.coordinator()
    setup.create()
    assert coordinator.run(setup.token).ok
    result = coordinator.run(setup.token, flush_settle=0, sleep=lambda _: None)
    assert result.ok and not result.waiting and result.published == 0


@pytest.mark.parametrize("phase", ["before", "probe", "payload", "manifest", "marker"])
def test_stop_callback_cancels_between_phases_without_deleting(setup, phase):
    stopped = phase == "before"
    coordinator = setup.coordinator(
        stop=lambda: stopped,
        after_publish="delete",
        conflict="replace",
    )
    path, _, _ = setup.create()
    probe = setup.engine.remote_exists

    def request_stop(current):
        nonlocal stopped
        if current == phase:
            stopped = True

    def remote_exists(rel, sas):
        result = probe(rel, sas)
        request_stop("probe")
        return result

    setup.engine.remote_exists = remote_exists
    setup.engine.remote = phase == "probe"
    setup.engine.on_run = request_stop
    result = coordinator.run(setup.token)
    assert result.status == "cancelled" and not result.ok and result.deleted == 0
    assert path.exists() and result.published == int(phase == "marker")
    if phase == "before":
        assert setup.engine.calls == []
    else:
        assert setup.engine.calls[-1][0] == phase
        tx = next(iter(coordinator.ledger.transactions.values()))
        assert tx.state != "failed"
        assert not list(coordinator.ledger_path.parent.glob("*.manifest.json"))
        if phase == "marker":
            assert tx.state == "published" and tx.marker_attempted


@pytest.mark.parametrize("phase", ["payload", "manifest", "marker"])
def test_engine_cancellation_stops_remaining_candidates(setup, phase):
    coordinator = setup.coordinator(after_publish="delete")
    first, _, _ = setup.create()
    second, _, _ = setup.create("checkpoint-2")
    setup.engine.results[phase] = Transfer(status="cancelled", error="stopped")
    result = coordinator.run(setup.token)
    assert result.status == "cancelled" and result.error == "stopped"
    assert first.exists() and second.exists()
    assert setup.engine.calls[-1][0] == phase
    assert sum(call[0] == "probe" for call in setup.engine.calls) == 1


def test_stop_between_candidates_preserves_completed_counts(setup):
    stopped = False
    coordinator = setup.coordinator(
        stop=lambda: stopped,
        after_publish="delete",
        order="natural",
    )
    first, _, _ = setup.create()
    second, _, _ = setup.create("checkpoint-2")

    def log(message):
        nonlocal stopped
        if message.startswith("deleted published"):
            stopped = True

    coordinator.log = log
    result = coordinator.run(setup.token)
    assert result.status == "cancelled" and result.published == result.deleted == 1
    assert not first.exists() and second.exists()
    assert sum(call[0] == "probe" for call in setup.engine.calls) == 1


def test_stop_during_flush_settle_prevents_publication(setup):
    stopped = False
    coordinator = setup.coordinator(stop=lambda: stopped)
    path, _, _ = setup.create()

    def settle(_):
        nonlocal stopped
        stopped = True
        path.rename(path.with_name("unselected"))

    result = coordinator.run(setup.token, flush_settle=0, sleep=settle)
    assert result.status == "cancelled" and setup.engine.calls == []


@pytest.mark.parametrize("phase", ["payload", "manifest", "marker"])
def test_transfer_failure_never_deletes_and_stops_later_phases(setup, phase):
    coordinator = setup.coordinator(after_publish="delete")
    path, _, _ = setup.create()
    setup.engine.results[phase] = Transfer(status="network", error="offline")
    result = coordinator.run(setup.token)
    assert result.error == "offline" and result.published == result.deleted == 0
    assert path.exists() and setup.engine.calls[-1][0] == phase


@pytest.mark.parametrize(
    "verify,transfer,success",
    [
        ("azcopy", Transfer(completed=0, bytes=0), True),
        ("size", Transfer(completed=0), False),
        ("size", Transfer(completed=2), False),
        ("size", Transfer(bytes=6), False),
        ("size", Transfer(bytes=8), False),
        ("size", Transfer(skipped=1), False),
        ("md5", Transfer(), True),
        ("azcopy", Transfer(failed=1), False),
        ("azcopy", Transfer(skipped=1), False),
    ],
)
def test_transfer_summary_contract(setup, verify, transfer, success):
    coordinator = setup.coordinator(verify=verify)
    setup.create()
    setup.engine.results["payload"] = transfer
    result = coordinator.run(setup.token)
    assert bool(result.published) is success


@pytest.mark.parametrize("phase", ["manifest", "marker"])
@pytest.mark.parametrize(
    "transfer",
    [
        Transfer(completed=0),
        Transfer(completed=2),
        Transfer(skipped=1),
        Transfer(failed=1),
    ],
)
def test_invalid_visibility_summary_never_authorizes_deletion(setup, phase, transfer):
    coordinator = setup.coordinator(after_publish="delete")
    path, _, _ = setup.create()
    setup.engine.results[phase] = transfer
    result = coordinator.run(setup.token)
    assert result.status == "partial" and result.published == result.deleted == 0
    assert path.exists() and setup.engine.calls[-1][0] == phase


@pytest.mark.parametrize("action", ["keep", "delete"])
def test_marker_is_staged_and_source_rechecked_after_transfer(setup, action):
    coordinator = setup.coordinator(after_publish=action)
    path, _, marker = setup.create()
    marker.write_bytes(b"ready-v1")
    staged = []
    build = setup.engine.build_exact_copy_argv

    def build_exact(source, rel, sas):
        if rel.endswith("/.complete"):
            assert source != marker
            staged.append(source)
        return build(source, rel, sas)

    def mutate(phase):
        if phase == "marker":
            before = marker.stat()
            marker.write_bytes(b"ready-v2")
            os.utime(marker, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert staged[0].read_bytes() == b"ready-v1"

    setup.engine.build_exact_copy_argv = build_exact
    setup.engine.on_run = mutate
    result = coordinator.run(setup.token)
    assert result.status == "waiting" and result.published == 1 and result.deleted == 0
    assert path.exists() and "marker publication" in result.error
    assert staged and not staged[0].exists()


@pytest.mark.parametrize("last_status", ["network", "cancelled"])
def test_later_candidate_does_not_overwrite_previous_error(setup, last_status):
    coordinator = setup.coordinator(order="natural")
    setup.create()
    setup.create("checkpoint-2")
    failures = iter(
        [
            Transfer(status="network", error="first failure"),
            Transfer(status=last_status, error="second failure"),
        ]
    )

    def fail(phase):
        if phase == "payload":
            setup.engine.results[phase] = next(failures)

    setup.engine.on_run = fail
    result = coordinator.run(setup.token)
    assert result.status == last_status
    assert result.error == "first failure; second failure"


@pytest.mark.parametrize("phase", ["payload", "manifest", "marker"])
def test_upload_mutation_preserves_local_checkpoint(setup, phase):
    coordinator = setup.coordinator(after_publish="delete")
    path, payload, _ = setup.create()

    def mutate(current):
        if current == phase:
            payload.write_bytes(b"changed")

    setup.engine.on_run = mutate
    result = coordinator.run(setup.token)
    assert result.error and "changed" in result.error
    assert path.exists() and not result.deleted
    assert result.published == int(phase == "marker")


def test_retained_checkpoint_ages_out_without_reupload(setup):
    coordinator = setup.coordinator(
        after_publish="delete", keep_last=1, order="natural"
    )
    first, _, _ = setup.create()
    assert coordinator.run(setup.token).retained == 1
    setup.engine.remote = False
    second, _, _ = setup.create("checkpoint-2")
    result = coordinator.run(setup.token)
    assert result.published == result.deleted == result.retained == 1
    assert not first.exists() and second.exists()
    assert [call[1] for call in setup.engine.calls if call[0] == "payload"] == [
        "checkpoints/checkpoint-1",
        "checkpoints/checkpoint-2",
    ]
    assert coordinator.next_wake is None


def test_file_unit_custom_marker_and_manifest_are_siblings(setup):
    coordinator = setup.coordinator(
        unit="file", ready_marker=".done", after_publish="delete"
    )
    path, _, marker = setup.create()
    result = coordinator.run(setup.token)
    assert result.published == result.deleted == 1
    assert not path.exists() and not marker.exists()
    assert (
        "manifest",
        "checkpoints/checkpoint-1.azsync-manifest.json",
    ) in setup.engine.calls
    assert not list((setup.source / ".azsync-moved").iterdir())


@pytest.mark.parametrize("conflict", ["fail", "replace"])
def test_interrupted_marker_attempt_never_automatically_overwrites(setup, conflict):
    coordinator = setup.coordinator(conflict=conflict)
    path, _, _ = setup.create()
    candidate = coordinator.scan()[0]
    tx = coordinator.ledger.transactions[candidate.snapshot.relpath]
    tx.marker_attempted = True
    coordinator._transition(candidate.snapshot, "publishing_marker")
    setup.engine.remote = True
    result = setup.coordinator().run(setup.token)
    assert result.status == "fatal" and "owned manifest" in result.error
    assert setup.engine.calls == [("probe", "checkpoints/checkpoint-1/.complete")]
    assert path.exists()


def test_explicit_replace_of_preexisting_checkpoint_is_allowed(setup):
    coordinator = setup.coordinator(conflict="replace")
    setup.create()
    setup.engine.remote = True
    assert coordinator.run(setup.token).published == 1
    assert [call[0] for call in setup.engine.calls[:3]] == [
        "probe",
        "remove",
        "payload",
    ]


@pytest.mark.parametrize("mutation", ["payload", "marker", "empty-directory"])
def test_identity_resets_stability_even_with_restored_mtime(setup, mutation):
    coordinator = setup.coordinator(stable=10)
    path, payload, marker = setup.create()
    first = discover(setup.source, coordinator.policy, coordinator.ledger, 100)[0]
    target = marker if mutation == "marker" else payload
    before = target.stat()
    if mutation == "empty-directory":
        (path / "empty").mkdir()
    else:
        target.write_bytes(b"weights" if mutation == "payload" else b"")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = discover(setup.source, coordinator.policy, coordinator.ledger, 120)[0]
    assert first.snapshot.identity != second.snapshot.identity
    assert coordinator.ledger.transactions[second.snapshot.relpath].observed_at == 120
    assert not second.ready


@pytest.mark.parametrize("unit", ["directory", "file"])
def test_rename_racing_same_size_mutation_preserves_quarantine(
    setup, monkeypatch, unit
):
    coordinator = setup.coordinator(unit=unit, after_publish="delete")
    path, payload, _ = setup.create()
    original = os.rename

    def raced_rename(src, dst, **kwargs):
        if src == path.name:
            before = payload.stat()
            payload.write_bytes(b"changed")
            os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", raced_rename)
    result = coordinator.run(setup.token)
    assert result.published == 1 and result.deleted == 0 and result.error
    tx = next(iter(coordinator.ledger.transactions.values()))
    moved = setup.source / ".azsync-moved" / tx.transaction / tx.path
    assert (
        moved / "weights" if unit == "directory" else moved
    ).read_bytes() == b"changed"
    restarted = setup.coordinator().run(setup.token)
    assert restarted.status == "fatal" and moved.exists()


def quarantined(setup):
    coordinator = setup.coordinator(after_publish="delete")
    setup.create()
    candidate = coordinator.scan()[0]
    snapshot = candidate.snapshot
    tx = coordinator.ledger.transactions[snapshot.relpath]
    coordinator._transition(snapshot, "published")
    root = setup.source / ".azsync-moved" / tx.transaction

    def record(manifest):
        tx.quarantine_manifest = manifest
        coordinator.ledger.transition(
            tx.path,
            "quarantined",
            time.time(),
            quarantined_path=str(root),
        )
        coordinator._save()

    target = quarantine(setup.source, snapshot, tx.transaction, record=record)
    return coordinator, tx, root, target


def test_proven_quarantine_recovers_and_is_idempotent(setup):
    _, tx, root, _ = quarantined(setup)
    restarted = setup.coordinator()
    assert restarted.run(setup.token).deleted == 1
    assert not root.exists()
    assert restarted.ledger.transactions[tx.path].state == "deleted"
    assert restarted.run(setup.token).deleted == 0
    assert setup.engine.calls == []


def test_quarantine_collision_preserves_unknown_content_and_source(setup):
    coordinator = setup.coordinator(after_publish="delete")
    path, payload, _ = setup.create()
    candidate = coordinator.scan()[0]
    tx = coordinator.ledger.transactions[candidate.snapshot.relpath]
    root = setup.source / ".azsync-moved" / tx.transaction
    root.mkdir(parents=True)
    unknown = root / "unknown"
    unknown.write_bytes(b"preserve me")
    result = coordinator.publish_one(candidate, setup.token)
    assert result.published == 1 and result.deleted == 0 and result.error
    assert path.exists() and payload.read_bytes() == b"weights"
    assert unknown.read_bytes() == b"preserve me"
    assert setup.coordinator().run(setup.token).status == "fatal"
    assert unknown.exists()


def test_failed_quarantine_proof_save_never_deletes(setup, monkeypatch):
    coordinator = setup.coordinator(after_publish="delete")
    setup.create()
    save = coordinator._save

    def fail_save():
        if any(
            tx.state == "quarantined" for tx in coordinator.ledger.transactions.values()
        ):
            raise PublishError("proof save failed")
        save()

    monkeypatch.setattr(coordinator, "_save", fail_save)
    result = coordinator.run(setup.token)
    tx = next(iter(coordinator.ledger.transactions.values()))
    target = setup.source / ".azsync-moved" / tx.transaction / tx.path
    assert result.status == "partial" and result.published == 1 and result.deleted == 0
    assert "proof save failed" in result.error and (target / "weights").exists()
    assert setup.coordinator().run(setup.token).status == "fatal"
    assert (target / "weights").read_bytes() == b"weights"


def test_cleanup_does_not_remove_a_replaced_directory(setup, monkeypatch):
    _, _, root, target = quarantined(setup)
    unlink = os.unlink

    def replace_directory(name, **kwargs):
        unlink(name, **kwargs)
        if name == "weights":
            target.rename(target.with_name("original-moved"))
            target.mkdir()

    monkeypatch.setattr(os, "unlink", replace_directory)
    result = setup.coordinator().run(setup.token)
    assert result.status == "fatal" and "directory changed" in result.error
    assert root.exists() and target.is_dir()


def test_stop_before_recovery_preserves_proven_quarantine(setup):
    _, _, root, target = quarantined(setup)
    result = setup.coordinator(stop=lambda: True).run(setup.token)
    assert result.status == "cancelled" and root.exists() and target.exists()
    assert not result.deleted and not setup.engine.calls


def test_stop_after_rename_preserves_proof_for_next_run(setup):
    coordinator = setup.coordinator(
        after_publish="delete",
        stop=lambda: any(
            tx.state == "quarantined" for tx in coordinator.ledger.transactions.values()
        ),
    )
    path, _, _ = setup.create()
    result = coordinator.run(setup.token)
    assert (
        result.status == "cancelled" and result.published == 1 and result.deleted == 0
    )
    tx = next(iter(coordinator.ledger.transactions.values()))
    assert tx.state == "quarantined" and tx.quarantine_manifest
    assert not path.exists()
    assert (setup.source / ".azsync-moved" / tx.transaction).exists()
    recovered = setup.coordinator().run(setup.token)
    assert recovered.ok and recovered.deleted == 1 and not recovered.published


@pytest.mark.parametrize("mutation", ["changed", "unknown", "symlink"])
def test_changed_or_unknown_quarantine_is_never_deleted(setup, mutation):
    _, _, root, target = quarantined(setup)
    if mutation == "changed":
        (target / "weights").write_bytes(b"changed")
    elif mutation == "unknown":
        (root / "unknown").write_bytes(b"recover me")
    else:
        (root / "link").symlink_to(setup.source / "checkpoints")
    result = setup.coordinator().run(setup.token)
    assert result.status == "fatal" and result.error and target.exists()
    assert (target / "weights").exists()


@pytest.mark.parametrize(
    "destination", ["source", "outside", "other-transaction", "unit"]
)
def test_cleanup_rejects_nonexact_quarantine_paths(setup, destination):
    _, tx, root, target = quarantined(setup)
    bad = {
        "source": setup.source,
        "outside": setup.source.parent,
        "other-transaction": root.with_name("other"),
        "unit": target,
    }[destination]
    with pytest.raises(PublishError, match="exact source-owned"):
        clean_quarantine(
            bad,
            source=setup.source,
            transaction=tx.transaction,
            manifest=tx.quarantine_manifest,
        )
    assert target.exists() and setup.source.exists()


@pytest.mark.parametrize(
    "location", ["source", "selector", "unit", "marker", "quarantine"]
)
def test_symlink_boundaries_fail_closed(setup, location):
    coordinator = setup.coordinator(after_publish="delete")
    path, _, marker = setup.create()
    if location == "quarantine":
        (setup.source / ".azsync-moved").symlink_to(setup.source.parent)
    else:
        selected = {
            "source": setup.source,
            "selector": setup.source / "checkpoints",
            "unit": path,
            "marker": marker,
        }[location]
        moved = selected.with_name(selected.name + "-original")
        selected.rename(moved)
        selected.symlink_to(moved)
    result = coordinator.run(setup.token)
    assert result.status in ("fatal", "partial") and result.error
    assert not result.deleted


def test_source_root_cannot_be_a_checkpoint(setup):
    coordinator = setup.coordinator()
    with pytest.raises(PublishError, match="below the source"):
        snapshot_unit(setup.source, setup.source, coordinator.policy)


@pytest.mark.parametrize("exact", [False, True])
def test_flush_disappearance_is_failure_not_success(setup, exact):
    coordinator = setup.coordinator()
    path, _, _ = setup.create()

    def disappear(_):
        path.rename(path.with_name("unselected"))

    result = coordinator.run(
        setup.token,
        flush_checkpoint="checkpoints/checkpoint-1" if exact else None,
        flush_settle=0,
        sleep=disappear,
    )
    assert result.status == "fatal" and "disappeared" in result.error
    assert setup.engine.calls == []


def test_future_deadline_only_and_no_published_spin(setup):
    coordinator = setup.coordinator(stable=10)
    setup.create()
    now = time.time()
    coordinator.clock = lambda: now
    coordinator.scan()
    assert coordinator.next_wake == now + 10
    coordinator.clock = lambda: now + 10
    assert coordinator.run(setup.token).published == 1
    coordinator.scan()
    assert coordinator.next_wake is None


@pytest.mark.parametrize(
    "change",
    [
        {"path": "../outside"},
        {"observed_at": "yesterday"},
        {"state": "unknown"},
        {"transaction": "../outside"},
        {"quarantined_path": 42},
        {"marker_attempted": "yes"},
    ],
)
def test_malformed_transaction_never_silently_resets(setup, change):
    coordinator = setup.coordinator()
    setup.create()
    coordinator.scan()
    raw = json.loads(coordinator.ledger_path.read_text())
    next(iter(raw["transactions"].values())).update(change)
    coordinator.ledger_path.write_text(json.dumps(raw))
    with pytest.raises(PublishError, match="ledger"):
        PublishLedger.load(coordinator.ledger_path)


def test_flush_marker_replacement_with_same_mtime_is_not_stable(setup):
    coordinator = setup.coordinator()
    _, _, marker = setup.create()

    def replace_marker(_):
        before = marker.stat()
        replacement = marker.with_name("replacement")
        replacement.write_bytes(b"")
        replacement.replace(marker)
        os.utime(marker, ns=(before.st_atime_ns, before.st_mtime_ns))

    candidates = flush_candidates(
        setup.source,
        coordinator.policy,
        coordinator.ledger,
        settle=0,
        sleep=replace_marker,
    )
    assert not candidates[0].ready and "marker changed" in candidates[0].reason
