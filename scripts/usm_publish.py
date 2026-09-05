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
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator

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


class PublishTransportError(PublishError):
    """A transport failure whose classification survives the coordinator."""

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


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
        if self.after_publish == "delete" and os.name != "posix":
            raise PublishError(
                "safe checkpoint deletion requires POSIX directory handles"
            )
        if self.verify not in ("azcopy", "size", "md5"):
            raise PublishError("publish verify must be azcopy, size, or md5")
        if self.order not in ("mtime", "natural"):
            raise PublishError("publish order must be mtime or natural")
        if self.conflict not in ("fail", "replace"):
            raise PublishError("publish conflict must be fail or replace")
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
            for value in (self.stable, self.min_age)
        ):
            raise PublishError(
                "publish durations must be finite and cannot be negative"
            )
        if type(self.keep_last) is not int or self.keep_last < 0:
            raise PublishError("keep-last must be an integer and cannot be negative")
        if (
            self.ready_marker in ("", ".", "..", ".azsync-manifest.json")
            or "/" in self.ready_marker
            or "\\" in self.ready_marker
        ):
            raise PublishError("ready marker must be one file name")
        _literal_path(self.ready_marker, "ready marker")
        if self.unit == "directory" and self.excludes:
            raise PublishError(
                "directory publish cannot exclude files inside a checkpoint; "
                "use --publish-unit file"
            )
        for raw in self.paths:
            path = _relative_path(raw, "publish path")
            if source is not None:
                _checked_path(source, source / path, missing_ok=True)

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
    ctime_ns: int = 0

    def matches(self, info: os.stat_result) -> bool:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) == (
            self.device,
            self.inode,
            self.mode,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        )


@dataclass(frozen=True)
class TreeSnapshot:
    """Filesystem metadata fingerprint and identity, not a content checksum."""

    relpath: str
    unit: str
    files: tuple[FileFact, ...]
    marker_mtime_ns: int | None
    fingerprint: str
    identity: str
    bytes: int
    mtime_ns: int
    nodes: tuple[FileFact, ...] = ()
    marker_identity: str = ""

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
    marker_attempted: bool = False
    quarantine_manifest: dict | None = None

    def __post_init__(self) -> None:
        if self.state not in PUBLISH_STATES:
            raise PublishError(f"unknown publish state: {self.state}")


@dataclass
class PublishLedger:
    transactions: dict[str, Transaction] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PublishLedger":
        try:
            if path.is_symlink():
                raise PublishError(f"publish ledger is a symlink: {path}")
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            raise PublishError(f"cannot load publish ledger {path}: {exc}") from exc
        try:
            if (
                not isinstance(raw, dict)
                or type(raw.get("version")) is not int
                or raw["version"] != 1
                or not isinstance(raw.get("transactions"), dict)
            ):
                raise ValueError("expected version 1 and a transactions object")
            txs: dict[str, Transaction] = {}
            for relpath, item in raw["transactions"].items():
                if not isinstance(item, dict):
                    raise ValueError("transaction must be an object")
                tx = Transaction(**item)
                _relative_path(relpath, "ledger path")
                if tx.path != relpath:
                    raise ValueError("transaction path does not match its key")
                _transaction_name(tx.transaction)
                if (
                    not all(
                        isinstance(value, str)
                        for value in (tx.identity, tx.fingerprint)
                    )
                    or not tx.fingerprint
                ):
                    raise ValueError("invalid snapshot identity")
                for value in (tx.observed_at, tx.updated_at, tx.published_at):
                    if value is not None and (
                        type(value) not in (int, float) or not math.isfinite(value)
                    ):
                        raise ValueError("invalid transaction timestamp")
                if (
                    type(tx.marker_attempted) is not bool
                    or tx.error is not None
                    and not isinstance(tx.error, str)
                    or tx.quarantined_path is not None
                    and not isinstance(tx.quarantined_path, str)
                    or tx.quarantine_manifest is not None
                    and not isinstance(tx.quarantine_manifest, dict)
                ):
                    raise ValueError("invalid transaction metadata")
                txs[relpath] = tx
        except (TypeError, ValueError, PublishError) as exc:
            raise PublishError(f"invalid publish ledger {path}: {exc}") from exc
        return cls(txs)

    def save(self, path: Path) -> None:
        payload = {
            "version": 1,
            "transactions": {
                name: asdict(tx) for name, tx in sorted(self.transactions.items())
            },
        }
        try:
            if path.is_symlink():
                raise PublishError(f"publish ledger is a symlink: {path}")
            atomic_write(path, json.dumps(payload, indent=2))
        except OSError as exc:
            raise PublishError(f"cannot save publish ledger {path}: {exc}") from exc

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
    seed = f"{snapshot.relpath}\0{snapshot.identity}\0{now}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _natural_key(value: str) -> tuple:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
    )


def marker_for(path: Path, unit: str, marker: str) -> Path:
    if (
        marker in ("", ".", "..", ".azsync-manifest.json")
        or "/" in marker
        or "\\" in marker
    ):
        raise PublishError("ready marker must be one non-reserved file name")
    _literal_path(marker, "ready marker")
    return path / marker if unit == "directory" else path.with_name(path.name + marker)


def _literal_path(raw: str, label: str = "checkpoint source path") -> None:
    for character in ";*?[":
        if character in raw:
            raise PublishError(
                f"{label} contains {character!r}, which azcopy cannot select safely: {raw}"
            )


def _relative_path(raw: str, label: str) -> Path:
    if not isinstance(raw, str):
        raise PublishError(f"{label} must be a relative path")
    path = Path(raw)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path == Path(".")
        or "\0" in raw
        or path.parts[0] == ".azsync-moved"
    ):
        raise PublishError(f"{label} must stay below the source: {raw!r}")
    _literal_path(raw, label)
    return path


def _checked_path(source: Path, path: Path, *, missing_ok: bool = False) -> Path:
    """Check lexical containment without ever resolving a symlink."""
    root, target = source.absolute(), path.absolute()
    _literal_path(str(target))
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PublishError(f"checkpoint escapes the source: {path}") from exc
    if ".." in target.parts:
        raise PublishError(f"checkpoint escapes the source: {path}")
    for parent in (*reversed(target.parents), target):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if missing_ok and parent != root and root in parent.parents:
                break
            raise PublishError(f"checkpoint path is missing: {parent}") from None
        except OSError as exc:
            raise PublishError(
                f"cannot inspect checkpoint path {parent}: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise PublishError(f"checkpoint path is a non-regular symlink: {parent}")
        if parent != target and not stat.S_ISDIR(info.st_mode):
            raise PublishError(f"checkpoint parent is not a directory: {parent}")
    return target


def _fact(path: Path, root: Path) -> FileFact:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PublishError(f"cannot inspect checkpoint member {path}: {exc}") from exc
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise PublishError(f"checkpoint contains a non-regular file: {path}")
    return FileFact(
        path.relative_to(root).as_posix(),
        info.st_size,
        info.st_mtime_ns,
        info.st_ino,
        info.st_dev,
        info.st_mode,
        info.st_ctime_ns,
    )


def _facts(root: Path, base: Path) -> tuple[FileFact, ...]:
    paths = [root]
    facts = []
    while paths:
        path = paths.pop()
        fact = _fact(path, base)
        facts.append(fact)
        if stat.S_ISDIR(fact.mode):
            try:
                paths.extend(path.iterdir())
            except OSError as exc:
                raise PublishError(f"cannot scan checkpoint {path}: {exc}") from exc
    facts.sort(key=lambda item: item.relpath)
    for fact in facts:
        _checked_path(base, base / fact.relpath)
        if _fact(base / fact.relpath, base) != fact:
            raise PublishError("checkpoint changed while being scanned")
    return tuple(facts)


def snapshot_unit(
    source: Path, path: Path, policy: PublishPolicy
) -> tuple[TreeSnapshot, Path | None]:
    root = source.absolute()
    path = _checked_path(root, path)
    rel_unit = path.relative_to(root).as_posix()
    _relative_path(rel_unit, "checkpoint path")
    unit_fact = _fact(path, root)
    if (
        policy.unit == "directory"
        and not stat.S_ISDIR(unit_fact.mode)
        or policy.unit == "file"
        and not stat.S_ISREG(unit_fact.mode)
    ):
        raise PublishError(f"checkpoint has the wrong unit type: {path}")
    marker_path = marker_for(path, policy.unit, policy.ready_marker)
    marker: Path | None = None
    marker_fact = None
    try:
        marker_stat = marker_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PublishError(f"cannot inspect ready marker: {exc}") from exc
    else:
        if not stat.S_ISREG(marker_stat.st_mode):
            raise PublishError(f"ready marker is a non-regular file: {marker_path}")
        marker, marker_fact = marker_path, _fact(marker_path, root)

    nodes = _facts(path, root)
    if policy.unit == "file" and marker_fact is not None:
        nodes = tuple(sorted((*nodes, marker_fact), key=lambda fact: fact.relpath))
    files = [
        fact
        for fact in nodes
        if stat.S_ISREG(fact.mode)
        and fact != marker_fact
        and not policy.excluded(fact.relpath)
    ]
    if policy.unit == "directory" and any(
        fact.relpath == f"{rel_unit}/.azsync-manifest.json" for fact in files
    ):
        raise PublishError("checkpoint contains reserved .azsync-manifest.json")
    digest = hashlib.sha256()
    identity = hashlib.sha256(f"{rel_unit}\0{policy.unit}\0".encode())
    for fact in files:
        portable = f"{fact.relpath}\0{fact.size}\0{fact.mtime_ns}\0".encode()
        digest.update(portable)
    identity.update(
        json.dumps([asdict(fact) for fact in nodes], sort_keys=True).encode()
    )
    latest = max((fact.mtime_ns for fact in files), default=0)
    return (
        TreeSnapshot(
            relpath=rel_unit,
            unit=policy.unit,
            files=tuple(files),
            marker_mtime_ns=marker_fact.mtime_ns if marker_fact else None,
            fingerprint=digest.hexdigest(),
            identity=identity.hexdigest(),
            bytes=sum(fact.size for fact in files),
            mtime_ns=latest,
            nodes=nodes,
            marker_identity=repr(marker_fact),
        ),
        marker,
    )


def discover(
    source: Path,
    policy: PublishPolicy,
    ledger: PublishLedger,
    now: float | None = None,
) -> list[Candidate]:
    now = time.time() if now is None else now
    policy.validate(source)
    source = _checked_path(source, source)
    if not source.is_dir():
        raise PublishError("checkpoint source must be a directory")
    roots = [source / path for path in policy.paths] if policy.paths else [source]
    found: list[Candidate] = []
    present: set[str] = set()
    for root in roots:
        _checked_path(source, root, missing_ok=True)
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise PublishError(f"cannot scan publish path {root}: {exc}") from exc
        for path in entries:
            if path.name == ".azsync-moved":
                continue
            if not policy.matches_name(path.name):
                continue
            fact = _fact(path, source)
            if policy.unit == "directory" and not stat.S_ISDIR(fact.mode):
                continue
            if policy.unit == "file" and not stat.S_ISREG(fact.mode):
                continue
            if policy.unit == "file" and (
                path.name.endswith(policy.ready_marker)
                or path.name.endswith(".azsync-manifest.json")
                or policy.excluded(path.relative_to(source).as_posix())
            ):
                continue
            snapshot, marker = snapshot_unit(source, path, policy)
            if snapshot.relpath in present:
                continue
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
    if type(settle) not in (int, float) or not math.isfinite(settle) or settle < 0:
        raise PublishError("flush settle must be finite and cannot be negative")
    wanted = None
    if checkpoint is not None:
        path = _relative_path(checkpoint, "flush checkpoint")
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
    missing = first_by_path.keys() - {item.snapshot.relpath for item in second}
    if missing:
        raise PublishError(
            f"checkpoint disappeared during flush settle: {', '.join(sorted(missing))}"
        )

    out: list[Candidate] = []
    for item in second:
        previous = first_by_path.get(item.snapshot.relpath)
        if previous is None:
            item.ready = False
            item.reason = "appeared during flush settle"
        elif previous.snapshot.marker_identity != item.snapshot.marker_identity:
            item.ready = False
            item.reason = "marker changed during flush settle"
        elif previous.snapshot.identity != item.snapshot.identity:
            item.ready = False
            item.reason = "changed during flush settle"
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
    record: Callable[[dict], None] | None = None,
) -> Path:
    """Move, validate again, then persist deletion proof before any removal.

    A crash before ``record`` leaves an unproven quarantine, which is preserved
    for recovery rather than inferred to be safe from its directory name.
    """
    if dirname != ".azsync-moved":
        raise PublishError("quarantine must use the source-owned .azsync-moved")
    _relative_path(snapshot.relpath, "quarantine checkpoint")
    root = quarantine_root(source, transaction)
    source = source.absolute()
    path = source / snapshot.relpath
    target = root / snapshot.relpath
    policy = PublishPolicy(unit=snapshot.unit, ready_marker=ready_marker)
    if not snapshot_unchanged(source, snapshot, policy):
        raise PublishError("cannot quarantine: checkpoint changed before rename")
    hashes = {}
    if snapshot.unit == "file":
        # Renaming changes ctime, so metadata alone cannot detect a same-size,
        # restored-mtime write racing the rename of a file or its sidecar.
        hashes = {
            fact.relpath: _content_digest(source / fact.relpath)
            for fact in snapshot.nodes
        }
    try:
        with _directory_fd(source) as source_fd:
            source_stat = os.fstat(source_fd)
            with _child_directory(
                source_fd, (".azsync-moved",), create=True
            ) as parent_fd:
                os.mkdir(transaction, dir_fd=parent_fd)
                with _child_directory(parent_fd, (transaction,)) as root_fd:
                    parents = Path(snapshot.relpath).parent.parts
                    with (
                        _child_directory(source_fd, parents) as old_fd,
                        _child_directory(root_fd, parents, create=True) as new_fd,
                    ):
                        os.rename(
                            path.name, target.name, src_dir_fd=old_fd, dst_dir_fd=new_fd
                        )
                        if snapshot.unit == "file":
                            name = marker_for(path, snapshot.unit, ready_marker).name
                            os.rename(name, name, src_dir_fd=old_fd, dst_dir_fd=new_fd)
    except OSError as exc:
        raise PublishError(f"cannot quarantine {snapshot.relpath}: {exc}") from exc

    actual = {fact.relpath: fact for fact in _facts(root, root)}
    expected_names = {"."}
    for fact in snapshot.nodes:
        expected_names.add(fact.relpath)
        expected_names.update(
            parent.as_posix() for parent in Path(fact.relpath).parents
        )
        moved = actual.get(fact.relpath)
        if moved is not None and (
            fact.relpath == snapshot.relpath or snapshot.unit == "file"
        ):
            moved = replace(moved, ctime_ns=fact.ctime_ns)
        if moved != fact:
            raise PublishError(
                "checkpoint changed during quarantine; preserved for recovery"
            )
    if set(actual) != expected_names or any(
        _content_digest(root / relpath) != digest for relpath, digest in hashes.items()
    ):
        raise PublishError("quarantine content changed; preserved for recovery")
    manifest = {
        "version": 1,
        "transaction": transaction,
        "source": [source_stat.st_dev, source_stat.st_ino],
        "path": snapshot.relpath,
        "unit": snapshot.unit,
        "marker": ready_marker,
        "entries": [asdict(fact) for fact in actual.values()],
    }
    if record is not None:
        record(manifest)
    return target


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PublishError(f"quarantine contains a non-regular file: {path}")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PublishError(f"cannot validate quarantine file {path}: {exc}") from exc
    return digest.hexdigest()


def _transaction_name(transaction: str) -> None:
    if not isinstance(transaction, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", transaction
    ):
        raise PublishError("unsafe quarantine transaction name")


@contextmanager
def _child_directory(
    descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
) -> Iterator[int]:
    current = os.dup(descriptor)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


@contextmanager
def _directory_fd(path: Path) -> Iterator[int]:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with _child_directory(descriptor, path.absolute().parts[1:]) as child:
            yield child
    finally:
        os.close(descriptor)


def quarantine_root(source: Path, transaction: str) -> Path:
    _transaction_name(transaction)
    source = _checked_path(source, source)
    root = source / ".azsync-moved" / transaction
    _checked_path(source, root, missing_ok=True)
    return root


def clean_quarantine(
    path: Path,
    *,
    source: Path | None = None,
    transaction: str | None = None,
    manifest: dict | None = None,
) -> None:
    """Remove only exactly proven entries in this source's transaction root."""
    if source is None or transaction is None or manifest is None:
        raise PublishError("quarantine cleanup requires an owned manifest")
    root = quarantine_root(source, transaction)
    if path.absolute() != root:
        raise PublishError(
            "quarantine path is not the exact source-owned transaction root"
        )
    source_info = source.stat()
    if (
        manifest.get("version") != 1
        or manifest.get("transaction") != transaction
        or manifest.get("source") != [source_info.st_dev, source_info.st_ino]
        or not isinstance(manifest.get("entries"), list)
    ):
        raise PublishError("quarantine has no valid owned manifest")
    if not root.exists():
        return
    actual = _facts(root, root)
    if [asdict(fact) for fact in actual] != manifest["entries"]:
        raise PublishError("quarantine changed; preserved for recovery")
    facts = {fact.relpath: fact for fact in actual}
    children: dict[Path, list[FileFact]] = {}
    for fact in actual:
        if fact.relpath != ".":
            children.setdefault(Path(fact.relpath).parent, []).append(fact)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def remove_directory(name: str, parent: int, fact: FileFact) -> None:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mode) != (
            fact.device,
            fact.inode,
            fact.mode,
        ):
            raise PublishError("quarantine directory changed during cleanup")
        os.rmdir(name, dir_fd=parent)

    def remove_entries(descriptor: int, prefix: Path) -> None:
        for fact in children.get(prefix, []):
            rel = Path(fact.relpath)
            info = os.stat(rel.name, dir_fd=descriptor, follow_symlinks=False)
            if not fact.matches(info):
                raise PublishError(
                    "quarantine changed during cleanup; preserved for recovery"
                )
            if stat.S_ISDIR(fact.mode):
                child = os.open(rel.name, directory_flags, dir_fd=descriptor)
                try:
                    if not fact.matches(os.fstat(child)):
                        raise PublishError(
                            "quarantine directory changed during cleanup"
                        )
                    remove_entries(child, rel)
                finally:
                    os.close(child)
                remove_directory(rel.name, descriptor, fact)
            else:
                os.unlink(rel.name, dir_fd=descriptor)

    try:
        with _directory_fd(source) as source_fd:
            info = os.fstat(source_fd)
            if [info.st_dev, info.st_ino] != manifest["source"]:
                raise PublishError("quarantine source changed during cleanup")
            parent_fd = os.open(".azsync-moved", directory_flags, dir_fd=source_fd)
            try:
                descriptor = os.open(transaction, directory_flags, dir_fd=parent_fd)
                try:
                    if not facts["."].matches(os.fstat(descriptor)):
                        raise PublishError("quarantine root changed during cleanup")
                    remove_entries(descriptor, Path("."))
                finally:
                    os.close(descriptor)
                remove_directory(transaction, parent_fd, facts["."])
            finally:
                os.close(parent_fd)
    except OSError as exc:
        raise PublishError(f"cannot delete quarantine {path}: {exc}") from exc
