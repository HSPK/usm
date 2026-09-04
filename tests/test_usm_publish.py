"""Tests for gated checkpoint publication.

The dangerous bugs are: publishing ``.complete`` before its payload, deleting
a checkpoint that changed during upload, treating a recreated inode as the
same file, and allowing a publish path to escape the source.  Those cases get
direct tests here; Azure transport is exercised separately in test_azsync.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from usm_publish import (
    PUBLISH_STATES,
    PublishError,
    PublishLedger,
    PublishPolicy,
    clean_quarantine,
    discover,
    flush_candidates,
    quarantine,
    snapshot_unchanged,
    snapshot_unit,
    transaction_id,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "output"
    (root / "checkpoints").mkdir(parents=True)
    return root


@pytest.fixture
def policy():
    return PublishPolicy(
        paths=("checkpoints",),
        patterns=("checkpoint-*",),
        unit="directory",
        ready_marker=".complete",
        stable=10,
        keep_last=0,
    )


def checkpoint(
    source: Path,
    name: str = "checkpoint-100",
    *,
    files: dict[str, str | bytes] | None = None,
    marker: bool = True,
    age: float = 60,
) -> Path:
    root = source / "checkpoints" / name
    root.mkdir(parents=True)
    for rel, content in (
        files or {"model.bin": b"weights", "state.json": "{}"}
    ).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    stamp = time.time() - age
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))
    if marker:
        done = root / ".complete"
        done.touch()
        os.utime(done, (stamp + 1, stamp + 1))
    return root


def ready_candidates(source, policy, ledger=None, *, now=None):
    ledger = ledger or PublishLedger()
    now = time.time() if now is None else now
    discover(source, policy, ledger, now - policy.stable - 1)
    return discover(source, policy, ledger, now), ledger


class TestPolicyValidation:
    def test_disabled_policy_is_valid(self, source):
        PublishPolicy().validate(source)

    @pytest.mark.parametrize("unit", ["file", "directory"])
    def test_units(self, source, unit):
        PublishPolicy(paths=("x",), unit=unit).validate(source)

    def test_unknown_unit(self, source):
        with pytest.raises(PublishError, match="unit"):
            PublishPolicy(paths=("x",), unit="blob").validate(source)

    @pytest.mark.parametrize("action", ["keep", "delete"])
    def test_actions(self, source, action):
        PublishPolicy(paths=("x",), after_publish=action).validate(source)

    def test_unknown_action(self, source):
        with pytest.raises(PublishError, match="after-publish"):
            PublishPolicy(paths=("x",), after_publish="burn").validate(source)

    @pytest.mark.parametrize("verify", ["azcopy", "size", "md5"])
    def test_verify_modes(self, source, verify):
        PublishPolicy(paths=("x",), verify=verify).validate(source)

    def test_unknown_verify(self, source):
        with pytest.raises(PublishError, match="verify"):
            PublishPolicy(paths=("x",), verify="hope").validate(source)

    @pytest.mark.parametrize("order", ["mtime", "natural"])
    def test_orders(self, source, order):
        PublishPolicy(paths=("x",), order=order).validate(source)

    def test_unknown_order(self, source):
        with pytest.raises(PublishError, match="order"):
            PublishPolicy(paths=("x",), order="random").validate(source)

    @pytest.mark.parametrize("field", ["stable", "min_age", "keep_last"])
    def test_negative_numbers(self, source, field):
        with pytest.raises(PublishError, match="cannot be negative"):
            PublishPolicy(paths=("x",), **{field: -1}).validate(source)

    @pytest.mark.parametrize("marker", ["", "a/b", r"a\b"])
    def test_bad_markers(self, source, marker):
        with pytest.raises(PublishError, match="marker"):
            PublishPolicy(paths=("x",), ready_marker=marker).validate(source)

    @pytest.mark.parametrize("path", ["", ".", "..", "../x", "/tmp/x", "x/../../y"])
    def test_escaping_or_root_paths(self, source, path):
        with pytest.raises(PublishError, match="publish path"):
            PublishPolicy(paths=(path,)).validate(source)

    def test_directory_excludes_are_refused(self, source):
        with pytest.raises(PublishError, match="file"):
            PublishPolicy(paths=("x",), unit="directory", excludes=("*.log",)).validate(
                source
            )

    def test_file_excludes_are_allowed(self, source):
        PublishPolicy(paths=("x",), unit="file", excludes=("*.log",)).validate(source)

    def test_patterns_are_or(self):
        policy = PublishPolicy(patterns=("*.ckpt", "*.pt"))
        assert policy.matches_name("a.ckpt")
        assert policy.matches_name("a.pt")
        assert not policy.matches_name("a.log")

    def test_no_pattern_means_every_name(self):
        assert PublishPolicy(paths=("x",)).matches_name("anything")

    def test_excludes(self):
        policy = PublishPolicy(paths=("x",), unit="file", excludes=("*.log",))
        assert policy.excluded("x/train.log")
        assert not policy.excluded("x/model.pt")


class TestSnapshot:
    def test_directory_snapshot_lists_payload_but_not_marker(self, source, policy):
        root = checkpoint(source)
        snap, marker = snapshot_unit(source, root, policy)
        assert marker == root / ".complete"
        assert {Path(f.relpath).name for f in snap.files} == {
            "model.bin",
            "state.json",
        }

    def test_counts_and_bytes(self, source, policy):
        root = checkpoint(source, files={"a": b"123", "b": b"45678"})
        snap, _ = snapshot_unit(source, root, policy)
        assert snap.file_count == 2 and snap.bytes == 8

    def test_is_deterministic(self, source, policy):
        root = checkpoint(source)
        one, _ = snapshot_unit(source, root, policy)
        two, _ = snapshot_unit(source, root, policy)
        assert one == two

    def test_content_metadata_changes_fingerprint(self, source, policy):
        root = checkpoint(source)
        one, _ = snapshot_unit(source, root, policy)
        (root / "model.bin").write_bytes(b"changed")
        two, _ = snapshot_unit(source, root, policy)
        assert one.fingerprint != two.fingerprint

    def test_inode_replacement_changes_identity(self, source, policy):
        root = checkpoint(source)
        path = root / "model.bin"
        one, _ = snapshot_unit(source, root, policy)
        old = path.stat()
        replacement = root / "replacement"
        replacement.write_bytes(b"weights")
        replacement.replace(path)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        two, _ = snapshot_unit(source, root, policy)
        assert one.fingerprint == two.fingerprint
        assert one.identity != two.identity

    def test_fingerprint_is_portable_across_inode_changes(self, source, policy):
        root = checkpoint(source)
        first, _ = snapshot_unit(source, root, policy)
        other = source.parent / "other"
        shutil = __import__("shutil")
        shutil.copytree(root, other, copy_function=shutil.copy2)
        second, _ = snapshot_unit(
            source.parent, other, replace(policy, paths=("other",))
        )
        assert first.fingerprint != "" and second.fingerprint != ""

    def test_nested_files(self, source, policy):
        root = checkpoint(source, files={"a/b/c/model.bin": b"x"})
        snap, _ = snapshot_unit(source, root, policy)
        assert snap.files[0].relpath.endswith("a/b/c/model.bin")

    def test_empty_checkpoint_has_zero_payload(self, source, policy):
        root = checkpoint(source, files={}, marker=True)
        # helper's default is used for empty dict; make a genuinely empty one.
        for path in root.iterdir():
            path.unlink()
        (root / ".complete").touch()
        snap, _ = snapshot_unit(source, root, policy)
        assert snap.file_count == 0

    def test_symlink_payload_is_refused(self, source, policy, tmp_path):
        root = checkpoint(source)
        (root / "link").symlink_to(tmp_path / "outside")
        with pytest.raises(PublishError, match="non-regular|escapes"):
            snapshot_unit(source, root, policy)

    def test_fifo_payload_is_refused(self, source, policy):
        root = checkpoint(source)
        try:
            os.mkfifo(root / "pipe")
        except (AttributeError, OSError):
            pytest.skip("no fifo support")
        with pytest.raises(PublishError, match="non-regular"):
            snapshot_unit(source, root, policy)

    def test_symlink_marker_is_not_ready(self, source, policy, tmp_path):
        root = checkpoint(source, marker=False)
        target = tmp_path / "done"
        target.touch()
        (root / ".complete").symlink_to(target)
        _, marker = snapshot_unit(source, root, policy)
        assert marker is None

    def test_semicolon_path_is_refused(self, source, policy):
        root = checkpoint(source, "checkpoint-1;checkpoint-2")
        with pytest.raises(PublishError, match="semicolon|';'"):
            snapshot_unit(source, root, policy)

    def test_file_unit_uses_sidecar_marker(self, source):
        path = source / "checkpoints" / "model.ckpt"
        path.write_bytes(b"x")
        sidecar = path.with_name("model.ckpt.complete")
        sidecar.touch()
        policy = PublishPolicy(
            paths=("checkpoints",),
            patterns=("*.ckpt",),
            unit="file",
            ready_marker=".complete",
            stable=0,
            keep_last=0,
        )
        snap, marker = snapshot_unit(source, path, policy)
        assert snap.files[0].relpath.endswith("model.ckpt")
        assert marker == sidecar

    def test_manifest_shape(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        manifest = snap.manifest("tx1")
        assert manifest["transaction"] == "tx1"
        assert manifest["fingerprint"] == snap.fingerprint
        assert manifest["files"] == snap.file_count
        assert all(
            set(row) == {"path", "size", "mtime_ns"} for row in manifest["entries"]
        )


class TestReadiness:
    def test_missing_marker_waits(self, source, policy):
        checkpoint(source, marker=False)
        items = discover(source, policy, PublishLedger(), time.time())
        assert not items[0].ready and "marker" in items[0].reason

    def test_marker_older_than_payload_waits(self, source, policy):
        root = checkpoint(source)
        now = time.time()
        os.utime(root / ".complete", (now - 100, now - 100))
        os.utime(root / "model.bin", (now - 10, now - 10))
        items = discover(source, policy, PublishLedger(), now)
        assert not items[0].ready and "after marker" in items[0].reason

    def test_first_observation_is_not_stable(self, source, policy):
        checkpoint(source)
        items = discover(source, policy, PublishLedger(), time.time())
        assert not items[0].ready and "stability" in items[0].reason

    def test_second_unchanged_observation_becomes_ready(self, source, policy):
        checkpoint(source)
        items, _ = ready_candidates(source, policy)
        assert items[0].ready

    def test_a_change_resets_the_stability_clock(self, source, policy):
        root = checkpoint(source)
        ledger = PublishLedger()
        now = time.time()
        discover(source, policy, ledger, now - 20)
        path = root / "model.bin"
        path.write_bytes(b"new")
        (root / ".complete").touch()
        items = discover(source, policy, ledger, now)
        assert not items[0].ready

    def test_min_age(self, source, policy):
        checkpoint(source, age=5)
        policy = replace(policy, stable=0, min_age=60)
        items = discover(source, policy, PublishLedger(), time.time())
        assert not items[0].ready and "min-age" in items[0].reason

    def test_empty_checkpoint_is_not_ready(self, source, policy):
        root = source / "checkpoints" / "checkpoint-empty"
        root.mkdir()
        (root / ".complete").touch()
        policy = replace(policy, stable=0)
        items = discover(source, policy, PublishLedger(), time.time())
        assert not items[0].ready and "no payload" in items[0].reason

    def test_wrong_name_is_not_discovered(self, source, policy):
        checkpoint(source, "final-model")
        assert discover(source, policy, PublishLedger(), time.time()) == []

    def test_file_in_directory_mode_is_not_discovered(self, source, policy):
        (source / "checkpoints" / "checkpoint-file").write_text("x")
        assert discover(source, policy, PublishLedger(), time.time()) == []

    def test_directory_in_file_mode_is_not_discovered(self, source, policy):
        checkpoint(source)
        policy = replace(policy, unit="file")
        assert discover(source, policy, PublishLedger(), time.time()) == []

    def test_missing_publish_path_is_empty(self, source, policy):
        policy = replace(policy, paths=("not-there",))
        assert discover(source, policy, PublishLedger(), time.time()) == []

    def test_already_published_is_not_ready_again(self, source, policy):
        checkpoint(source)
        items, ledger = ready_candidates(source, policy)
        ledger.transition(items[0].snapshot.relpath, "published", time.time())
        again = discover(source, policy, ledger, time.time() + 1)
        assert not again[0].ready and "already" in again[0].reason

    def test_changed_published_checkpoint_becomes_a_new_transaction(
        self, source, policy
    ):
        root = checkpoint(source)
        items, ledger = ready_candidates(source, policy)
        old = ledger.transactions[items[0].snapshot.relpath].transaction
        ledger.transition(items[0].snapshot.relpath, "published", time.time())
        (root / "model.bin").write_bytes(b"new version")
        (root / ".complete").touch()
        discover(source, policy, ledger, time.time() + 20)
        tx = ledger.transactions[items[0].snapshot.relpath]
        assert tx.transaction != old and tx.state != "published"


class TestExplicitFlush:
    def test_bypasses_long_stability_after_two_equal_snapshots(self, source, policy):
        checkpoint(source)
        policy = replace(policy, stable=3600)
        ledger = PublishLedger()
        items = flush_candidates(source, policy, ledger, settle=0, sleep=lambda _: None)
        assert items[0].ready

    def test_still_requires_marker(self, source, policy):
        checkpoint(source, marker=False)
        items = flush_candidates(
            source, policy, PublishLedger(), settle=0, sleep=lambda _: None
        )
        assert not items[0].ready and "marker" in items[0].reason

    def test_still_requires_marker_newer_than_payload(self, source, policy):
        root = checkpoint(source)
        now = time.time()
        os.utime(root / ".complete", (now - 100, now - 100))
        os.utime(root / "model.bin", (now, now))
        items = flush_candidates(
            source, policy, PublishLedger(), settle=0, sleep=lambda _: None
        )
        assert not items[0].ready and "after marker" in items[0].reason

    def test_still_honours_min_age(self, source, policy):
        checkpoint(source, age=1)
        policy = replace(policy, min_age=60)
        items = flush_candidates(
            source, policy, PublishLedger(), settle=0, sleep=lambda _: None
        )
        assert not items[0].ready and "min-age" in items[0].reason

    def test_keep_last_does_not_block_flush_publication(self, source, policy):
        checkpoint(source)
        policy = replace(policy, keep_last=1, after_publish="delete")
        items = flush_candidates(
            source, policy, PublishLedger(), settle=0, sleep=lambda _: None
        )
        assert items[0].ready
        assert items[0].keep_local

    def test_change_during_settle_is_not_ready(self, source, policy):
        root = checkpoint(source)

        def change(_seconds):
            (root / "model.bin").write_bytes(b"changed")
            (root / ".complete").touch()

        items = flush_candidates(
            source, policy, PublishLedger(), settle=1, sleep=change
        )
        assert not items[0].ready and "changed during" in items[0].reason

    def test_inode_replacement_during_settle_is_not_ready(self, source, policy):
        root = checkpoint(source)
        path = root / "model.bin"

        def replace_inode(_seconds):
            before = path.stat()
            replacement = root / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        items = flush_candidates(
            source, policy, PublishLedger(), settle=1, sleep=replace_inode
        )
        assert not items[0].ready and "changed during" in items[0].reason

    def test_marker_change_during_settle_is_not_ready(self, source, policy):
        root = checkpoint(source)

        def touch_marker(_seconds):
            time.sleep(0.002)
            (root / ".complete").touch()

        items = flush_candidates(
            source, policy, PublishLedger(), settle=1, sleep=touch_marker
        )
        assert not items[0].ready and "marker changed" in items[0].reason

    def test_checkpoint_appearing_during_settle_is_not_ready(self, source, policy):
        def create(_seconds):
            checkpoint(source)

        items = flush_candidates(
            source, policy, PublishLedger(), settle=1, sleep=create
        )
        assert not items[0].ready and "appeared during" in items[0].reason

    def test_exact_checkpoint_only(self, source, policy):
        checkpoint(source, "checkpoint-1")
        checkpoint(source, "checkpoint-2")
        items = flush_candidates(
            source,
            policy,
            PublishLedger(),
            checkpoint="checkpoints/checkpoint-2",
            settle=0,
            sleep=lambda _: None,
        )
        assert [item.snapshot.relpath for item in items] == ["checkpoints/checkpoint-2"]

    def test_exact_checkpoint_must_match_policy(self, source, policy):
        checkpoint(source, "checkpoint-1")
        with pytest.raises(PublishError, match="not selected"):
            flush_candidates(
                source,
                policy,
                PublishLedger(),
                checkpoint="checkpoints/nope",
                settle=0,
                sleep=lambda _: None,
            )

    @pytest.mark.parametrize("path", ["/tmp/x", "../x", ".", ""])
    def test_unsafe_exact_checkpoint(self, source, policy, path):
        with pytest.raises(PublishError, match="below the source"):
            flush_candidates(
                source,
                policy,
                PublishLedger(),
                checkpoint=path,
                settle=0,
                sleep=lambda _: None,
            )

    def test_negative_settle_is_refused(self, source, policy):
        with pytest.raises(PublishError, match="negative"):
            flush_candidates(
                source, policy, PublishLedger(), settle=-1, sleep=lambda _: None
            )


class TestKeepLast:
    def _many(self, source, names):
        now = time.time()
        for i, name in enumerate(names):
            root = checkpoint(source, name, age=100 + len(names) - i)
            os.utime(root / "model.bin", (now - 100 + i, now - 100 + i))
            os.utime(root / ".complete", (now - 90 + i, now - 90 + i))

    def test_latest_two_are_kept(self, source, policy):
        self._many(source, ["checkpoint-1", "checkpoint-2", "checkpoint-3"])
        policy = replace(policy, stable=0, keep_last=2, after_publish="delete")
        items = discover(source, policy, PublishLedger(), time.time())
        assert all(item.ready for item in items)
        assert [i.snapshot.relpath for i in items if i.keep_local] == [
            "checkpoints/checkpoint-2",
            "checkpoints/checkpoint-3",
        ]

    def test_zero_keeps_none(self, source, policy):
        self._many(source, ["checkpoint-1", "checkpoint-2"])
        policy = replace(policy, stable=0, keep_last=0)
        assert all(
            i.ready for i in discover(source, policy, PublishLedger(), time.time())
        )

    def test_keep_mode_never_marks_retention_exemptions(self, source, policy):
        self._many(source, ["checkpoint-1", "checkpoint-2"])
        policy = replace(policy, stable=0, keep_last=2, after_publish="keep")
        items = discover(source, policy, PublishLedger(), time.time())
        assert all(item.ready for item in items)
        assert not any(item.keep_local for item in items)

    def test_more_keep_than_candidates_keeps_everything(self, source, policy):
        self._many(source, ["checkpoint-1"])
        policy = replace(policy, stable=0, keep_last=5, after_publish="delete")
        item = discover(source, policy, PublishLedger(), time.time())[0]
        assert item.ready and item.keep_local

    def test_natural_order_understands_numbers(self, source, policy):
        self._many(source, ["checkpoint-2", "checkpoint-10", "checkpoint-100"])
        policy = replace(
            policy,
            stable=0,
            keep_last=1,
            order="natural",
            after_publish="delete",
        )
        items = discover(source, policy, PublishLedger(), time.time())
        kept = [i for i in items if i.keep_local]
        assert kept[0].snapshot.relpath.endswith("checkpoint-100")

    def test_natural_order_handles_mixed_names(self, source, policy):
        self._many(source, ["checkpoint-final", "checkpoint-2"])
        policy = replace(policy, stable=0, keep_last=1, order="natural")
        # Regression: tuples containing raw int/str values cannot be compared.
        assert len(discover(source, policy, PublishLedger(), time.time())) == 2


class TestLedger:
    def test_missing_file_is_empty(self, tmp_path):
        assert PublishLedger.load(tmp_path / "none").transactions == {}

    @pytest.mark.parametrize("body", ["not json", "[]", "null", '{"transactions": []}'])
    def test_corrupt_shapes_are_empty(self, tmp_path, body):
        path = tmp_path / "ledger.json"
        path.write_text(body)
        assert PublishLedger.load(path).transactions == {}

    def test_observe_creates_a_transaction(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        tx = ledger.observe(snap, 100)
        assert tx.path == snap.relpath and tx.observed_at == 100
        assert tx.state == "discovered"

    def test_same_identity_preserves_observed_at(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        first = ledger.observe(snap, 100)
        second = ledger.observe(snap, 200)
        assert first is second and second.observed_at == 100

    def test_new_identity_resets_observed_at(self, source, policy):
        root = checkpoint(source)
        one, _ = snapshot_unit(source, root, policy)
        ledger = PublishLedger()
        ledger.observe(one, 100)
        (root / "model.bin").write_bytes(b"new")
        (root / ".complete").touch()
        two, _ = snapshot_unit(source, root, policy)
        assert ledger.observe(two, 200).observed_at == 200

    @pytest.mark.parametrize("state", PUBLISH_STATES)
    def test_every_state_round_trips(self, source, policy, tmp_path, state):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        ledger.transition(snap.relpath, state, 200, error="x")
        path = tmp_path / "ledger.json"
        ledger.save(path)
        loaded = PublishLedger.load(path).transactions[snap.relpath]
        assert loaded.state == state and loaded.error == "x"

    def test_unknown_state_is_refused(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        with pytest.raises(PublishError, match="state"):
            ledger.transition(snap.relpath, "teleported", 200)

    def test_published_at_is_recorded(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        assert ledger.transition(snap.relpath, "published", 250).published_at == 250

    def test_quarantine_path_is_recorded(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        tx = ledger.transition(
            snap.relpath, "quarantined", 250, quarantined_path="/tmp/q"
        )
        assert tx.quarantined_path == "/tmp/q"

    def test_missing_waiting_transactions_are_pruned(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        ledger.prune_missing(set())
        assert ledger.transactions == {}

    @pytest.mark.parametrize("state", ["published", "deleted", "quarantined"])
    def test_completed_transactions_are_not_pruned(self, source, policy, state):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        ledger.transition(snap.relpath, state, 200)
        ledger.prune_missing(set())
        assert snap.relpath in ledger.transactions

    def test_transaction_id_is_deterministic_for_the_same_input(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        assert transaction_id(snap, 100) == transaction_id(snap, 100)

    def test_transaction_id_changes_with_time(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        assert transaction_id(snap, 100) != transaction_id(snap, 101)

    def test_save_is_atomic_json(self, source, policy, tmp_path):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        ledger = PublishLedger()
        ledger.observe(snap, 100)
        path = tmp_path / "ledger.json"
        ledger.save(path)
        assert json.loads(path.read_text())["version"] == 1
        assert not list(tmp_path.glob("*.tmp*"))


class TestUnchanged:
    def test_unchanged_tree(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        assert snapshot_unchanged(source, snap, policy)

    def test_changed_content(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        (root / "model.bin").write_bytes(b"new")
        assert not snapshot_unchanged(source, snap, policy)

    def test_deleted_file(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        (root / "model.bin").unlink()
        assert not snapshot_unchanged(source, snap, policy)

    def test_added_file(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        (root / "new.bin").write_bytes(b"x")
        assert not snapshot_unchanged(source, snap, policy)

    def test_replaced_inode_with_same_metadata(self, source, policy):
        root = checkpoint(source)
        path = root / "model.bin"
        snap, _ = snapshot_unit(source, root, policy)
        before = path.stat()
        replacement = root / "replacement"
        replacement.write_bytes(b"weights")
        replacement.replace(path)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert not snapshot_unchanged(source, snap, policy)

    def test_marker_removed(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        (root / ".complete").unlink()
        assert not snapshot_unchanged(source, snap, policy)

    def test_marker_touched(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        time.sleep(0.002)
        (root / ".complete").touch()
        assert not snapshot_unchanged(source, snap, policy)


class TestQuarantine:
    def test_directory_is_atomically_moved(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        target = quarantine(source, snap, "tx1")
        assert not root.exists() and target.is_dir()
        assert (target / ".complete").exists()

    def test_file_and_sidecar_are_moved(self, source):
        path = source / "checkpoints" / "model.ckpt"
        path.write_bytes(b"x")
        marker = path.with_name(path.name + ".done")
        marker.write_text("")
        policy = PublishPolicy(
            paths=("checkpoints",),
            patterns=("*.ckpt",),
            unit="file",
            ready_marker=".done",
            stable=0,
            keep_last=0,
        )
        snap, _ = snapshot_unit(source, path, policy)
        target = quarantine(source, snap, "tx1", ready_marker=".done")
        assert target.exists()
        assert target.with_name(target.name + ".done").exists()
        assert not path.exists() and not marker.exists()

    def test_cleanup_removes_a_directory(self, source, policy):
        snap, _ = snapshot_unit(source, checkpoint(source), policy)
        target = quarantine(source, snap, "tx1")
        clean_quarantine(target)
        assert not target.exists()

    def test_cleanup_removes_a_file(self, tmp_path):
        path = tmp_path / "one"
        path.write_text("x")
        clean_quarantine(path)
        assert not path.exists()

    def test_cleanup_missing_path_is_idempotent(self, tmp_path):
        clean_quarantine(tmp_path / "gone")

    def test_quarantine_refuses_missing_source(self, source, policy):
        root = checkpoint(source)
        snap, _ = snapshot_unit(source, root, policy)
        __import__("shutil").rmtree(root)
        with pytest.raises(PublishError, match="quarantine"):
            quarantine(source, snap, "tx1")
