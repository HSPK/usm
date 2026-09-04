#!/usr/bin/env python3
"""Gated directory publication for azsync.

This module knows nothing about Azure or azcopy.  It answers four questions:

* Which files/directories are checkpoint units?
* Is a unit complete and unchanged long enough to publish?
* Which recent units must remain local?
* What transaction state survived the last process crash?

The transport and remote verification stay in ``azsync.py``.  Keeping the
filesystem policy pure makes the destructive half testable without Azure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable

from usm_daemon import atomic_write

PUBLISH_STATES = (
    "discovered",
    "waiting_marker",
    "waiting_stable",
    "uploading_payload",
    "verifying_payload",
    "publishing_manifest",
    "publishing_marker",
    "published",
    "quarantined",
    "deleted",
    "conflict",
    "failed",
)


class PublishError(Exception):
    """A publish policy or checkpoint tree is unsafe."""


@dataclass(frozen=True)
class PublishPolicy:
    paths: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    unit: str = "directory"
    ready_marker: str = ".complete"
    stable: float = 120.0
    min_age: float = 0.0
    keep_last: int = 2
    order: str = "mtime"
    after_publish: str = "keep"
    verify: str = "size"
    conflict: str = "fail"

    @property
    def enabled(self) -> bool:
        return bool(self.paths or self.patterns)

    def validate(self, source: Path | None = None) -> None:
        if not self.enabled:
            return
        if self.unit not in ("file", "directory"):
            raise PublishError("publish unit must be file or directory")
        if self.after_publish not in ("keep", "delete"):
            raise PublishError("after-publish must be keep or delete")
        if self.verify not in ("azcopy", "size", "md5"):
            raise PublishError("publish verify must be azcopy, size, or md5")
        if self.order not in ("mtime", "natural"):
            raise PublishError("publish order must be mtime or natural")
        if self.conflict not in ("fail", "replace"):
            raise PublishError("publish conflict must be fail or replace")
        if self.stable < 0 or self.min_age < 0 or self.keep_last < 0:
            raise PublishError("publish durations and keep-last cannot be negative")
        if (
            not self.ready_marker
            or "/" in self.ready_marker
            or "\\" in self.ready_marker
        ):
            raise PublishError("ready marker must be one file name")
        if self.unit == "directory" and self.excludes:
            raise PublishError(
                "directory publish cannot exclude files inside a checkpoint; "
                "use --publish-unit file"
            )
        for raw in self.paths:
            path = Path(raw)
            if path.is_absolute() or ".." in path.parts or raw in ("", "."):
                raise PublishError(f"publish path must stay below the source: {raw!r}")
            if source is not None:
                resolved = (source / path).resolve()
                root = source.resolve()
                if root not in resolved.parents:
                    raise PublishError(f"publish path escapes the source: {raw!r}")

    def matches_name(self, name: str) -> bool:
        import fnmatch

        if not self.patterns:
            return True
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.patterns)

    def excluded(self, relpath: str) -> bool:
        import fnmatch

        return any(fnmatch.fnmatch(relpath, pattern) for pattern in self.excludes)


@dataclass(frozen=True)
class FileFact:
    relpath: str
    size: int
    mtime_ns: int
    inode: int
    device: int
    mode: int


@dataclass(frozen=True)
class TreeSnapshot:
    relpath: str
    unit: str
    files: tuple[FileFact, ...]
    marker_mtime_ns: int | None
    fingerprint: str
    identity: str
    bytes: int
    mtime_ns: int

    @property
    def file_count(self) -> int:
        return len(self.files)

    def manifest(self, transaction: str) -> dict:
        return {
            "version": 1,
            "transaction": transaction,
            "path": self.relpath,
            "unit": self.unit,
            "files": self.file_count,
            "bytes": self.bytes,
            "fingerprint": self.fingerprint,
            "entries": [
                {"path": fact.relpath, "size": fact.size, "mtime_ns": fact.mtime_ns}
                for fact in self.files
            ],
        }


@dataclass
class Candidate:
    snapshot: TreeSnapshot
    marker: Path | None
    ready: bool
    reason: str
    order_key: tuple
    keep_local: bool = False


@dataclass
class Transaction:
    path: str
    fingerprint: str
    identity: str = ""
    state: str = "discovered"
    observed_at: float = 0.0
    updated_at: float = 0.0
    published_at: float | None = None
    quarantined_path: str | None = None
    error: str | None = None
    transaction: str = ""

    def __post_init__(self) -> None:
        if self.state not in PUBLISH_STATES:
            raise PublishError(f"unknown publish state: {self.state}")


@dataclass
class PublishLedger:
    transactions: dict[str, Transaction] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PublishLedger":
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        txs: dict[str, Transaction] = {}
        for relpath, item in (raw.get("transactions") or {}).items():
            if not isinstance(item, dict):
                continue
            try:
                tx = Transaction(**item)
            except (TypeError, ValueError, PublishError):
                continue
            txs[str(relpath)] = tx
        return cls(txs)

    def save(self, path: Path) -> None:
        payload = {
            "version": 1,
            "transactions": {
                name: asdict(tx) for name, tx in sorted(self.transactions.items())
            },
        }
        atomic_write(path, json.dumps(payload, indent=2))

    def observe(self, snapshot: TreeSnapshot, now: float) -> Transaction:
        current = self.transactions.get(snapshot.relpath)
        if current is None or current.identity != snapshot.identity:
            current = Transaction(
                path=snapshot.relpath,
                fingerprint=snapshot.fingerprint,
                identity=snapshot.identity,
                state="discovered",
                observed_at=now,
                updated_at=now,
                transaction=transaction_id(snapshot, now),
            )
            self.transactions[snapshot.relpath] = current
        return current

    def transition(
        self,
        relpath: str,
        state: str,
        now: float,
        *,
        error: str | None = None,
        quarantined_path: str | None = None,
    ) -> Transaction:
        if state not in PUBLISH_STATES:
            raise PublishError(f"unknown publish state: {state}")
        tx = self.transactions[relpath]
        tx.state = state
        tx.updated_at = now
        tx.error = error
        if state == "published":
            tx.published_at = now
        if quarantined_path is not None:
            tx.quarantined_path = quarantined_path
        return tx

    def prune_missing(self, present: set[str]) -> None:
        for relpath in list(self.transactions):
            tx = self.transactions[relpath]
            if relpath not in present and tx.state in (
                "discovered",
                "waiting_marker",
                "waiting_stable",
                "failed",
                "conflict",
            ):
                del self.transactions[relpath]


def transaction_id(snapshot: TreeSnapshot, now: float) -> str:
    seed = f"{snapshot.relpath}\0{snapshot.fingerprint}\0{now}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _natural_key(value: str) -> tuple:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
    )


def marker_for(path: Path, unit: str, marker: str) -> Path:
    return path / marker if unit == "directory" else path.with_name(path.name + marker)


def snapshot_unit(
    source: Path, path: Path, policy: PublishPolicy
) -> tuple[TreeSnapshot, Path | None]:
    root = source.resolve()
    try:
        rel_unit = path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError) as exc:
        raise PublishError(f"checkpoint escapes the source: {path}") from exc
    if ";" in rel_unit:
        # azcopy uses semicolon as the --include-path separator and provides
        # no escaping syntax.  Refusing is safer than uploading two paths or
        # publishing a marker for payload that was never selected.
        raise PublishError(
            f"checkpoint path contains ';', which azcopy cannot select safely: "
            f"{rel_unit}"
        )

    marker_path = marker_for(path, policy.unit, policy.ready_marker)
    marker: Path | None = marker_path
    marker_mtime = None
    try:
        marker_stat = marker_path.lstat()
        if stat.S_ISREG(marker_stat.st_mode):
            marker_mtime = marker_stat.st_mtime_ns
        else:
            marker = None
    except OSError:
        marker = None

    files: list[FileFact] = []
    paths: Iterable[Path]
    if policy.unit == "file":
        paths = (path,)
    else:
        paths = _regular_files(path)
    for item in paths:
        if item == marker_path:
            continue
        try:
            info = item.lstat()
            resolved = item.resolve()
            rel = resolved.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise PublishError(f"checkpoint member escapes the source: {item}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"checkpoint contains a non-regular file: {item}")
        if policy.excluded(rel):
            continue
        files.append(
            FileFact(
                relpath=rel,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                inode=info.st_ino,
                device=info.st_dev,
                mode=stat.S_IMODE(info.st_mode),
            )
        )
    files.sort(key=lambda fact: fact.relpath)
    digest = hashlib.sha256()
    identity = hashlib.sha256()
    for fact in files:
        portable = f"{fact.relpath}\0{fact.size}\0{fact.mtime_ns}\0".encode()
        digest.update(portable)
        identity.update(portable)
        identity.update(f"{fact.inode}\0{fact.device}\0".encode())
    latest = max((fact.mtime_ns for fact in files), default=0)
    return (
        TreeSnapshot(
            relpath=rel_unit,
            unit=policy.unit,
            files=tuple(files),
            marker_mtime_ns=marker_mtime,
            fingerprint=digest.hexdigest(),
            identity=identity.hexdigest(),
            bytes=sum(fact.size for fact in files),
            mtime_ns=latest,
        ),
        marker,
    )


def _regular_files(root: Path) -> Iterable[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise PublishError(f"cannot scan checkpoint {current}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                else:
                    yield path
            except OSError as exc:
                raise PublishError(
                    f"cannot inspect checkpoint member {path}: {exc}"
                ) from exc


def discover(
    source: Path,
    policy: PublishPolicy,
    ledger: PublishLedger,
    now: float | None = None,
) -> list[Candidate]:
    now = time.time() if now is None else now
    policy.validate(source)
    roots = [source / path for path in policy.paths] if policy.paths else [source]
    found: list[Candidate] = []
    present: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for path in entries:
            if policy.unit == "directory" and not path.is_dir():
                continue
            if policy.unit == "file" and not path.is_file():
                continue
            if not policy.matches_name(path.name):
                continue
            snapshot, marker = snapshot_unit(source, path, policy)
            present.add(snapshot.relpath)
            tx = ledger.observe(snapshot, now)
            marker_ready = marker is not None
            marker_last = (
                marker_ready
                and snapshot.marker_mtime_ns is not None
                and snapshot.marker_mtime_ns >= snapshot.mtime_ns
            )
            age = now - snapshot.mtime_ns / 1_000_000_000 if snapshot.mtime_ns else 0
            observed = now - tx.observed_at
            if (
                tx.state in ("published", "deleted")
                and tx.fingerprint == snapshot.fingerprint
            ):
                ready, reason = False, "already published"
            elif tx.state == "quarantined":
                ready, reason = False, "awaiting quarantine cleanup"
            elif not marker_ready:
                ready, reason = False, "waiting for marker"
                tx.state = "waiting_marker"
            elif not marker_last:
                ready, reason = False, "checkpoint changed after marker"
                tx.state = "waiting_marker"
            elif age < policy.min_age:
                ready, reason = False, "younger than min-age"
                tx.state = "waiting_stable"
            elif observed < policy.stable or age < policy.stable:
                ready, reason = False, "waiting for stability"
                tx.state = "waiting_stable"
            elif not snapshot.files:
                ready, reason = False, "checkpoint has no payload"
                tx.state = "failed"
            else:
                ready, reason = True, "ready"
            key = (
                (snapshot.mtime_ns, snapshot.relpath)
                if policy.order == "mtime"
                else (_natural_key(path.name), snapshot.relpath)
            )
            found.append(Candidate(snapshot, marker, ready, reason, key))
    ledger.prune_missing(present)
    found.sort(key=lambda item: item.order_key)
    if policy.after_publish == "delete" and policy.keep_last:
        for item in found[-policy.keep_last :]:
            item.keep_local = True
    return found


def flush_candidates(
    source: Path,
    policy: PublishPolicy,
    ledger: PublishLedger,
    *,
    checkpoint: str | None = None,
    settle: float = 1.0,
    clock=time.time,
    sleep=time.sleep,
) -> list[Candidate]:
    """Explicit completion signal: two equal snapshots across a short settle.

    This only accelerates the stability window.  Marker ordering, min-age,
    selectors and keep-last remain exactly the same.
    """
    if settle < 0:
        raise PublishError("flush settle cannot be negative")
    wanted = None
    if checkpoint is not None:
        path = Path(checkpoint)
        if path.is_absolute() or ".." in path.parts or checkpoint in ("", "."):
            raise PublishError(
                f"flush checkpoint must stay below the source: {checkpoint!r}"
            )
        wanted = path.as_posix().strip("/")

    accelerated = replace(policy, stable=0)
    first = discover(source, accelerated, ledger, clock())
    if wanted is not None:
        first = [item for item in first if item.snapshot.relpath == wanted]
        if not first:
            raise PublishError(
                f"checkpoint is not selected by the publish policy: {wanted}"
            )
    first_by_path = {item.snapshot.relpath: item for item in first}
    sleep(settle)
    second = discover(source, accelerated, ledger, clock())
    if wanted is not None:
        second = [item for item in second if item.snapshot.relpath == wanted]

    out: list[Candidate] = []
    for item in second:
        previous = first_by_path.get(item.snapshot.relpath)
        if previous is None:
            item.ready = False
            item.reason = "appeared during flush settle"
        elif previous.snapshot.identity != item.snapshot.identity:
            item.ready = False
            item.reason = "changed during flush settle"
        elif previous.snapshot.marker_mtime_ns != item.snapshot.marker_mtime_ns:
            item.ready = False
            item.reason = "marker changed during flush settle"
        out.append(item)
    return out


def snapshot_unchanged(
    source: Path, snapshot: TreeSnapshot, policy: PublishPolicy
) -> bool:
    path = source / snapshot.relpath
    try:
        current, marker = snapshot_unit(source, path, policy)
    except PublishError:
        return False
    return (
        marker is not None
        and current.identity == snapshot.identity
        and current.marker_mtime_ns == snapshot.marker_mtime_ns
    )


def quarantine(
    source: Path,
    snapshot: TreeSnapshot,
    transaction: str,
    *,
    dirname: str = ".azsync-moved",
    ready_marker: str = ".complete",
) -> Path:
    path = source / snapshot.relpath
    root = source / dirname / transaction
    target = root / snapshot.relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.rename(target)
    except OSError as exc:
        raise PublishError(f"cannot quarantine {snapshot.relpath}: {exc}") from exc
    marker = marker_for(path, snapshot.unit, ready_marker)
    if snapshot.unit == "file" and marker.exists():
        marker_target = marker_for(target, snapshot.unit, ready_marker)
        marker_target.parent.mkdir(parents=True, exist_ok=True)
        marker.rename(marker_target)
    return target


def clean_quarantine(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise PublishError(f"cannot delete quarantine {path}: {exc}") from exc
