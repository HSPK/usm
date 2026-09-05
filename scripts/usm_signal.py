#!/usr/bin/env python3
"""A durable filesystem command queue; signals only provide a wakeup."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NoReturn


class SignalError(Exception):
    """A queue operation failed or an event/result is malformed."""


def _validate_id(event_id: str) -> None:
    if not isinstance(event_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", event_id
    ):
        raise SignalError(f"invalid signal id: {event_id!r}")


def _validate_record(ident, label, timestamp, data) -> None:
    _validate_id(ident)
    if not isinstance(label, str) or not label:
        raise SignalError("invalid signal kind/status")
    try:
        finite = type(timestamp) in (int, float) and math.isfinite(timestamp)
    except OverflowError:
        finite = False
    if not finite:
        raise SignalError("signal timestamp must be a finite number")
    if not isinstance(data, dict):
        raise SignalError("signal payload/detail must be an object")


def _from_dict(cls, raw, fields):
    if not isinstance(raw, dict):
        raise SignalError("signal event/result must be an object")
    try:
        return cls(*(raw[key] for key in fields[:3]), raw.get(fields[3], {}))
    except KeyError as exc:
        raise SignalError(f"missing signal field: {exc}") from exc


@contextmanager
def _storage():
    try:
        yield
    except OSError as exc:
        raise SignalError(f"signal queue I/O failed: {exc}") from exc


@dataclass(frozen=True)
class SignalEvent:
    id: str
    kind: str
    created_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _validate_record(self.id, self.kind, self.created_at, self.payload)
        if not self.kind.replace("-", "").replace("_", "").isalnum():
            raise SignalError(f"invalid signal kind: {self.kind!r}")
        object.__setattr__(self, "payload", dict(self.payload))

    @classmethod
    def create(cls, kind: str, payload: dict[str, Any] | None = None) -> "SignalEvent":
        # Fixed-width time orders requests; randomness separates concurrent writers.
        ident = f"{time.time_ns():020d}-{uuid.uuid4().hex[:12]}"
        return cls(ident, kind, time.time(), {} if payload is None else payload)

    @classmethod
    def from_dict(cls, raw: dict) -> "SignalEvent":
        return _from_dict(cls, raw, ("id", "kind", "created_at", "payload"))


@dataclass(frozen=True)
class SignalResult:
    event_id: str
    status: str
    completed_at: float
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _validate_record(self.event_id, self.status, self.completed_at, self.detail)
        object.__setattr__(self, "detail", dict(self.detail))

    @classmethod
    def from_dict(cls, raw: dict) -> "SignalResult":
        return _from_dict(cls, raw, ("event_id", "status", "completed_at", "detail"))


class SignalQueue:
    """Atomic pending → working → immutable result lifecycle.

    Recovery requires stopped workers. A crash before result publication can
    replay work; a published terminal result is never replayed or overwritten.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending = root / "pending"
        self.working = root / "working"
        self.results = root / "results"
        self.quarantine = root / "quarantine"

    def _prepare(self) -> None:
        with _storage():
            for path in (self.pending, self.working, self.results, self.quarantine):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)

    def event_path(self, event_id: str, *, working: bool = False) -> Path:
        _validate_id(event_id)
        return (self.working if working else self.pending) / f"{event_id}.json"

    def result_path(self, event_id: str) -> Path:
        _validate_id(event_id)
        return self.results / f"{event_id}.json"

    @staticmethod
    def _paths(directory: Path):
        return sorted(path for path in directory.iterdir() if path.suffix == ".json")

    @staticmethod
    def _read(path: Path, cls):
        _validate_id(path.stem)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise SignalError(f"symlink queued signal/result: {path.name}")
        if not stat.S_ISREG(mode):
            raise SignalError(
                f"queued signal/result is not a regular file: {path.name}"
            )
        try:

            def opener(name, flags):
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                return os.open(name, flags)

            def number(value):
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("non-finite JSON number")
                return value

            with open(path, encoding="utf-8", opener=opener) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise SignalError("opened signal/result is not a regular file")
                record = cls.from_dict(
                    json.load(stream, parse_float=number, parse_constant=number)
                )
        except (ValueError, SignalError) as exc:
            raise SignalError(f"invalid queued signal/result: {path.name}") from exc
        ident = record.id if isinstance(record, SignalEvent) else record.event_id
        if ident != path.stem:
            raise SignalError(f"queued signal/result id mismatch: {path.name}")
        return record

    @staticmethod
    def _publish(path: Path, record) -> bool:
        try:
            encoded = json.dumps(asdict(record), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SignalError(
                "signal payload/detail must be JSON-serializable"
            ) from exc
        temporary = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".", dir=path.parent, delete=False
        )
        try:
            with temporary as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Exclusive publication makes the first terminal result permanent.
                os.link(temporary.name, path)
            except FileExistsError:
                created = False
            else:
                created = True
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return created
        finally:
            Path(temporary.name).unlink(missing_ok=True)

    def submit(self, kind: str, payload: dict[str, Any] | None = None) -> SignalEvent:
        event = SignalEvent.create(kind, payload)
        with _storage():
            self._prepare()
            if not self._publish(self.event_path(event.id), event):
                raise SignalError(f"signal id already exists: {event.id}")
        return event

    def _quarantine(self, path: Path) -> None:
        os.replace(path, self.quarantine / f"{path.name}.{uuid.uuid4().hex}")

    def _retire(self, path: Path, result: SignalResult) -> None:
        try:
            if result.status == "invalid":
                self._quarantine(path)
            else:
                path.unlink()
        except FileNotFoundError:
            if path.exists():
                raise

    def _reject(self, path: Path, error: SignalError) -> NoReturn:
        try:
            _validate_id(path.stem)
        except SignalError:
            # Unsafe filenames cannot identify a waiter, but remain evidence.
            pass
        else:
            if not self._publish(
                self.result_path(path.stem),
                SignalResult(path.stem, "invalid", time.time(), {"error": str(error)}),
            ):
                self.read_result(path.stem)
        self._quarantine(path)
        raise error

    def _unfinished(self, path: Path) -> SignalEvent | None:
        try:
            event = self._read(path, SignalEvent)
        except SignalError as exc:
            self._reject(path, exc)
        result = self.read_result(event.id)
        if result is None:
            return event
        self._retire(path, result)
        return None

    def claim(self) -> SignalEvent | None:
        """Claim the oldest request, skipping races and completed duplicates."""
        with _storage():
            self._prepare()
            for source in self._paths(self.pending):
                target = self.working / source.name
                try:
                    os.replace(source, target)
                except FileNotFoundError:
                    if source.exists():
                        raise
                    continue
                event = self._unfinished(target)
                if event is not None:
                    return event
        return None

    def complete(
        self,
        event: SignalEvent,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> SignalResult:
        path = self.result_path(event.id)
        with _storage():
            self._prepare()
            result = self.read_result(event.id)
            if result is None:
                result = SignalResult(
                    event.id, status, time.time(), {} if detail is None else detail
                )
                if not self._publish(path, result):
                    result = self.read_result(event.id)
                    if result is None:
                        raise SignalError(f"terminal result disappeared: {event.id}")
            self._retire(self.event_path(event.id, working=True), result)
            return result

    def read_result(self, event_id: str) -> SignalResult | None:
        path = self.result_path(event_id)
        with _storage():
            try:
                return self._read(path, SignalResult)
            except FileNotFoundError:
                return None

    def wait(
        self,
        event_id: str,
        timeout: float,
        *,
        interval: float = 0.1,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> SignalResult | None:
        _validate_id(event_id)
        deadline = clock() + max(0.0, timeout)
        while True:
            result = self.read_result(event_id)
            if result is not None:
                return result
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            sleep(min(interval, remaining))

    def recover(self) -> int:
        """Requeue unfinished claims after stopping all workers."""
        recovered = 0
        with _storage():
            self._prepare()
            for source in self._paths(self.working):
                event = self._unfinished(source)
                if event is None:
                    continue
                target = self.event_path(event.id)
                if target.exists():
                    if self._unfinished(target) != event:
                        self._reject(
                            source,
                            SignalError(f"conflicting queued signal: {event.id}"),
                        )
                    source.unlink()
                else:
                    os.replace(source, target)
                recovered += 1
        return recovered

    def pending_count(self) -> int:
        with _storage():
            try:
                return len(self._paths(self.pending))
            except FileNotFoundError:
                return 0

    def prune_results(self, keep: int = 100, *, min_age: float | None = None) -> int:
        """Retain results by default so slow waiters cannot lose their response.

        Opt-in pruning requires an age in seconds and no outstanding waiters.
        Pending/working results are always retained for crash recovery.
        """
        if min_age is None:
            return 0
        if not math.isfinite(min_age) or min_age <= 0:
            raise SignalError("result retention age must be positive and finite")
        removed = 0
        with _storage():
            self._prepare()
            paths = sorted(
                self._paths(self.results), key=lambda p: p.stat().st_mtime, reverse=True
            )
            cutoff = time.time() - min_age
            for path in paths[max(0, keep) :]:
                _validate_id(path.stem)
                if (
                    path.stat().st_mtime < cutoff
                    and not self.event_path(path.stem).exists()
                    and not self.event_path(path.stem, working=True).exists()
                ):
                    self._read(path, SignalResult)
                    path.unlink()
                    removed += 1
        return removed
