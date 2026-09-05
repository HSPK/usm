"""Checkpoint publication orchestration, independent of azcopy and azsync."""

from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Protocol

from usm_daemon import USM_CACHE_DIR
from usm_publish import (
    Candidate,
    PublishError,
    PublishLedger,
    PublishPolicy,
    PublishTransportError,
    Transaction,
    TreeSnapshot,
    clean_quarantine,
    discover,
    flush_candidates,
    quarantine,
    quarantine_root,
    snapshot_unchanged,
)


class PublishJob(Protocol):
    id: str

    def source_path(self) -> Path: ...

    def publish_policy(self) -> PublishPolicy: ...


class Token(Protocol):
    token: str | None


class TransferResult(Protocol):
    status: str
    completed: int
    failed: int
    skipped: int
    bytes: int
    error: str | None


class PublishEngine(Protocol):
    def remote_exists(self, rel: str, sas: str | None) -> bool: ...

    def run(self, argv: list[str]) -> TransferResult: ...

    def build_publish_argv(
        self,
        snapshot: TreeSnapshot,
        sas: str | None,
        *,
        marker: str,
        dry_run: bool = False,
    ) -> list[str]: ...

    def build_exact_copy_argv(
        self,
        path: Path,
        rel: str,
        sas: str | None,
    ) -> list[str]: ...

    def remove_remote(self, rel: str, sas: str | None) -> TransferResult: ...


@dataclass
class PublishRun:
    status: str = "ok"
    discovered: int = 0
    ready: int = 0
    published: int = 0
    deleted: int = 0
    retained: int = 0
    bytes: int = 0
    duration: float = 0.0
    error: str | None = None
    waiting: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def summary(self) -> str:
        counts = (
            f"{self.published} published, {self.deleted} deleted, "
            f"{self.retained} retained"
        )
        return counts + (f", {len(self.waiting)} waiting" if self.waiting else "")


class _Stopped(PublishError):
    def __init__(self, message: str, status: str, state: str = "failed") -> None:
        super().__init__(message)
        self.status, self.state = status, state


class PublishCoordinator:
    """Publish payload, transfer summary, manifest, then the visibility marker."""

    def __init__(
        self,
        job: PublishJob,
        engine: PublishEngine,
        ledger_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
        stopped: Callable[[], bool] | None = None,
    ) -> None:
        self.job, self.engine = job, engine
        self.policy = job.publish_policy()
        state_dir = Path(
            os.environ.get("USM_AZSYNC_STATE_DIR") or USM_CACHE_DIR / "azsync"
        )
        self.ledger_path = ledger_path or state_dir / job.id / "publish-ledger.json"
        self.clock, self.log = clock, log or (lambda _message: None)
        self.stopped = stopped or (lambda: False)
        self.ledger = PublishLedger.load(self.ledger_path)
        self.next_wake: float | None = None

    def _save(self) -> None:
        self.ledger.save(self.ledger_path)

    def _check_stop(self) -> None:
        if self.stopped():
            raise _Stopped("checkpoint publication cancelled", "cancelled")

    def _transition(
        self,
        snapshot: TreeSnapshot,
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        self.ledger.transition(snapshot.relpath, state, self.clock(), error=error)
        self._save()

    def scan(self) -> list[Candidate]:
        self._check_stop()
        now = self.clock()
        candidates = discover(
            self.job.source_path(),
            self.policy,
            self.ledger,
            now,
        )
        deadlines = []
        for candidate in candidates:
            if candidate.reason not in (
                "waiting for stability",
                "younger than min-age",
            ):
                continue
            tx = self.ledger.transactions[candidate.snapshot.relpath]
            mtime = candidate.snapshot.mtime_ns / 1_000_000_000
            deadline = max(
                tx.observed_at + self.policy.stable,
                mtime + self.policy.stable,
                mtime + self.policy.min_age,
            )
            if deadline > now:
                deadlines.append(deadline)
        self.next_wake = min(deadlines) if deadlines else None
        self._save()
        return candidates

    @staticmethod
    def _check_transfer(
        result: TransferResult,
        message: str,
        *,
        completed: int | None = None,
    ) -> None:
        if (
            result.status != "ok"
            or result.failed
            or result.skipped
            or completed is not None
            and result.completed != completed
        ):
            raise _Stopped(
                result.error or message,
                result.status if result.status != "ok" else "partial",
            )

    def _check_unchanged(self, snapshot: TreeSnapshot, message: str) -> None:
        self._check_stop()
        unchanged = snapshot_unchanged(self.job.source_path(), snapshot, self.policy)
        self._check_stop()
        if not unchanged:
            raise _Stopped(message, "waiting")

    def _payload_verified(self, snapshot: TreeSnapshot, result: TransferResult) -> bool:
        # size/md5 use the transfer summary plus azcopy's check-length. md5 adds
        # put-md5 in the engine; this is not an independent remote hash check.
        return (
            result.status == "ok"
            and not result.failed
            and not result.skipped
            and (
                self.policy.verify == "azcopy"
                or result.completed == snapshot.file_count
                and result.bytes == snapshot.bytes
            )
        )

    def _run_exact(self, source: Path, rel: str, token: Token) -> TransferResult:
        self._check_stop()
        return self.engine.run(
            self.engine.build_exact_copy_argv(source, rel, token.token or None),
        )

    @contextmanager
    def _staged(
        self,
        tx: Transaction,
        name: str,
        write: Callable[[BinaryIO], object],
    ) -> Iterator[Path]:
        path = self.ledger_path.parent / f"{tx.transaction}.{name}"
        owned = False
        try:
            try:
                with path.open("xb") as stream:
                    owned = True
                    write(stream)
            except FileExistsError as exc:
                raise PublishError(
                    f"stale publication file needs recovery: {path}"
                ) from exc
            yield path
        finally:
            if owned:
                path.unlink()

    def _copy_marker(self, candidate: Candidate, output: BinaryIO) -> None:
        marker = candidate.marker
        if marker is None:
            raise _Stopped("ready marker disappeared before publication", "waiting")
        rel = (
            marker.absolute().relative_to(self.job.source_path().absolute()).as_posix()
        )
        expected = next(
            (fact for fact in candidate.snapshot.nodes if fact.relpath == rel), None
        )
        try:
            descriptor = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise _Stopped(f"ready marker cannot be staged: {exc}", "waiting") from exc
        with os.fdopen(descriptor, "rb") as source:
            if expected is None or not expected.matches(os.fstat(source.fileno())):
                raise _Stopped("ready marker changed before staging", "waiting")
            shutil.copyfileobj(source, output)
            if not expected.matches(os.fstat(source.fileno())):
                raise _Stopped("ready marker changed during staging", "waiting")

    def _delete(self, candidate: Candidate, result: PublishRun) -> None:
        self._check_stop()
        if self.policy.after_publish != "delete":
            return
        snapshot = candidate.snapshot
        if candidate.keep_local:
            result.retained += 1
            return
        tx = self.ledger.transactions[snapshot.relpath]
        self._check_unchanged(
            snapshot, "checkpoint changed after publication; kept locally"
        )
        root = quarantine_root(self.job.source_path(), tx.transaction)

        def record(manifest: dict) -> None:
            tx.quarantine_manifest = manifest
            self.ledger.transition(
                snapshot.relpath,
                "quarantined",
                self.clock(),
                quarantined_path=str(root),
            )
            self._save()

        self._check_stop()
        quarantine(
            self.job.source_path(),
            snapshot,
            tx.transaction,
            ready_marker=self.policy.ready_marker,
            record=record,
        )
        self._clean_transaction(tx)
        result.deleted += 1
        self.log(f"deleted published checkpoint {snapshot.relpath}")

    def _clean_transaction(self, tx: Transaction) -> None:
        root = quarantine_root(self.job.source_path(), tx.transaction)
        if (
            tx.quarantined_path != str(root)
            or tx.published_at is None
            or not tx.quarantine_manifest
            or tx.quarantine_manifest.get("path") != tx.path
        ):
            raise PublishError(
                "quarantine has no proven published owner; preserved for recovery"
            )
        self._check_stop()
        clean_quarantine(
            root,
            source=self.job.source_path(),
            transaction=tx.transaction,
            manifest=tx.quarantine_manifest,
        )
        self.ledger.transition(tx.path, "deleted", self.clock())
        self._save()

    def _recover(self, result: PublishRun) -> None:
        for tx in self.ledger.transactions.values():
            self._check_stop()
            root = quarantine_root(self.job.source_path(), tx.transaction)
            if tx.quarantined_path is not None and tx.quarantined_path != str(root):
                raise PublishError(
                    "quarantine path is not the exact source-owned transaction root"
                )
            if tx.state == "quarantined" or tx.quarantined_path or root.exists():
                if tx.state == "deleted" and not root.exists():
                    continue
                self._clean_transaction(tx)
                result.deleted += 1

    def publish_one(self, candidate: Candidate, token: Token) -> PublishRun:
        started = self.clock()
        snapshot = candidate.snapshot
        tx = self.ledger.transactions[snapshot.relpath]
        result = PublishRun(discovered=1, ready=int(candidate.ready))
        try:
            self._check_stop()
            if tx.state == "published":
                self._delete(candidate, result)
                return result
            if not candidate.ready:
                if candidate.reason != "already published":
                    result.status = "waiting"
                    result.waiting = [
                        {"path": snapshot.relpath, "reason": candidate.reason}
                    ]
                return result
            self._check_unchanged(snapshot, "checkpoint changed before publication")
            separator = "/" if snapshot.unit == "directory" else ""
            remote_marker = snapshot.relpath + separator + self.policy.ready_marker
            try:
                exists = self.engine.remote_exists(remote_marker, token.token or None)
            except InterruptedError as exc:
                raise _Stopped(str(exc), "cancelled") from exc
            except PublishTransportError as exc:
                raise _Stopped(str(exc), exc.status) from exc
            except (PublishError, OSError) as exc:
                raise _Stopped(str(exc), "network") from exc
            self._check_stop()
            if exists:
                if tx.marker_attempted or tx.state == "publishing_marker":
                    raise _Stopped(
                        "remote marker exists after an interrupted publication; "
                        "cannot prove an owned manifest, preserved for recovery",
                        "fatal",
                        "conflict",
                    )
                if self.policy.conflict != "replace":
                    raise _Stopped(
                        f"remote checkpoint already has {self.policy.ready_marker}; "
                        "use --publish-conflict replace to republish it",
                        "fatal",
                        "conflict",
                    )
                self._check_transfer(
                    self.engine.remove_remote(remote_marker, token.token or None),
                    "cannot remove old ready marker",
                )

            self.log(f"publish start {snapshot.relpath} ({snapshot.bytes} bytes)")
            self._check_stop()
            self._transition(snapshot, "uploading_payload")
            upload = self.engine.run(
                self.engine.build_publish_argv(
                    snapshot,
                    token.token or None,
                    marker=self.policy.ready_marker,
                ),
            )
            self._check_stop()
            if not self._payload_verified(snapshot, upload):
                raise _Stopped(
                    upload.error
                    or f"payload verification failed: {upload.completed}/"
                    f"{snapshot.file_count} files, {upload.bytes}/{snapshot.bytes} bytes, "
                    f"{upload.skipped} skipped",
                    upload.status if upload.status != "ok" else "partial",
                )
            self._transition(snapshot, "verifying_payload")
            self._check_unchanged(
                snapshot, "checkpoint changed while its payload was uploading"
            )
            self._check_stop()
            self._transition(snapshot, "publishing_manifest")
            data = json.dumps(snapshot.manifest(tx.transaction), indent=2).encode()
            with self._staged(
                tx, "manifest.json", lambda output: output.write(data)
            ) as manifest:
                self._check_transfer(
                    self._run_exact(
                        manifest,
                        snapshot.relpath + separator + ".azsync-manifest.json",
                        token,
                    ),
                    "manifest upload failed",
                    completed=1,
                )
            self._check_stop()
            self._check_unchanged(
                snapshot,
                "checkpoint changed before its marker was published",
            )
            with self._staged(
                tx,
                "marker",
                lambda output: self._copy_marker(candidate, output),
            ) as marker:
                self._check_unchanged(
                    snapshot, "checkpoint changed before its marker was published"
                )
                tx.marker_attempted = True
                self._transition(snapshot, "publishing_marker")
                self._check_transfer(
                    self._run_exact(marker, remote_marker, token),
                    "ready marker upload failed",
                    completed=1,
                )
                self._transition(snapshot, "published")
                result.published, result.bytes = 1, snapshot.bytes
            self.log(f"published {snapshot.relpath} (marker last)")
            self._check_unchanged(
                snapshot,
                "checkpoint changed during marker publication; kept locally",
            )
            self._delete(candidate, result)
        except (PublishError, OSError) as exc:
            result.status = exc.status if isinstance(exc, _Stopped) else "fatal"
            if result.published and result.status not in ("waiting", "cancelled"):
                result.status = "partial"
            result.error = str(exc)
            if result.status == "waiting":
                result.waiting = [{"path": snapshot.relpath, "reason": result.error}]
            # Keep durable quarantine proof/state even if cleanup failed.
            try:
                if result.status != "cancelled" and tx.state not in (
                    "quarantined",
                    "published",
                ):
                    state = exc.state if isinstance(exc, _Stopped) else "failed"
                    self._transition(snapshot, state, error=result.error)
                else:
                    tx.error = result.error
                    self._save()
            except (PublishError, OSError) as persistence:
                result.status = "partial" if result.published else "fatal"
                self._record_error(
                    result, f"cannot persist publication state: {persistence}"
                )
        finally:
            result.duration = max(0.0, self.clock() - started)
        return result

    @staticmethod
    def _record_error(result: PublishRun, error: str | None) -> None:
        if error and error != result.error:
            result.error = f"{result.error}; {error}" if result.error else error

    def run(
        self,
        token: Token,
        *,
        flush_checkpoint: str | None = None,
        flush_settle: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> PublishRun:
        started = self.clock()
        total = PublishRun()
        if not self.policy.enabled:
            return total
        try:
            self._check_stop()
            self._recover(total)
            if flush_settle is None:
                candidates = self.scan()
            else:
                self.next_wake = None

                def settle_wait(delay: float) -> None:
                    self._check_stop()
                    sleep(delay)
                    self._check_stop()

                candidates = flush_candidates(
                    self.job.source_path(),
                    self.policy,
                    self.ledger,
                    checkpoint=flush_checkpoint,
                    settle=flush_settle,
                    clock=self.clock,
                    sleep=settle_wait,
                )
                self._save()
            self._check_stop()
            total.discovered = len(candidates)
            total.ready = sum(item.ready for item in candidates)
            total.waiting = [
                {"path": item.snapshot.relpath, "reason": item.reason}
                for item in candidates
                if not item.ready and item.reason != "already published"
            ]
            for candidate in candidates:
                self._check_stop()
                tx = self.ledger.transactions[candidate.snapshot.relpath]
                if not candidate.ready and tx.state != "published":
                    continue
                one = self.publish_one(candidate, token)
                for attr in ("published", "deleted", "retained", "bytes"):
                    setattr(total, attr, getattr(total, attr) + getattr(one, attr))
                total.waiting.extend(one.waiting)
                self._record_error(total, one.error)
                if one.status == "cancelled":
                    total.status = one.status
                    break
                if one.status not in ("ok", "waiting"):
                    if total.status in ("ok", "waiting"):
                        total.status = one.status
                elif one.status == "waiting" and total.status == "ok":
                    total.status = one.status
            if total.status not in ("ok", "waiting", "cancelled") and (
                total.published or total.deleted or total.retained
            ):
                total.status = "partial"
            elif total.status in ("ok", "waiting") and total.waiting:
                total.status = "waiting"
        except (PublishError, OSError) as exc:
            total.status = (
                exc.status
                if isinstance(exc, _Stopped)
                else "partial"
                if total.published or total.deleted
                else "fatal"
            )
            self._record_error(total, str(exc))
        total.duration = max(0.0, self.clock() - started)
        return total
