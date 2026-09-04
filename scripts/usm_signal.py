#!/usr/bin/env python3
"""A tiny durable command queue for long-running usm supervisors.

The filesystem is the source of truth; POSIX signals are only a low-latency
wakeup.  Writing the event before sending SIGUSR1 means a lost/coalesced
signal, a Windows host, or a supervisor crash cannot lose the request.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from usm_daemon import atomic_write


class SignalError(Exception):
    """A queued event or result is malformed."""


@dataclass(frozen=True)
class SignalEvent:
    id: str
    kind: str
    created_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, kind: str, payload: dict[str, Any] | None = None) -> "SignalEvent":
        if not kind or not kind.replace("-", "").replace("_", "").isalnum():
            raise SignalError(f"invalid signal kind: {kind!r}")
        now = time.time()
        # Fixed-width nanoseconds preserve submission order in directory
        # listings; randomness prevents collisions across concurrent writers.
        ident = f"{time.time_ns():020d}-{uuid.uuid4().hex[:12]}"
        return cls(ident, kind, now, dict(payload or {}))

    @classmethod
    def from_dict(cls, raw: dict) -> "SignalEvent":
        if not isinstance(raw, dict):
            raise SignalError("signal event must be an object")
        try:
            event = cls(
                id=str(raw["id"]),
                kind=str(raw["kind"]),
                created_at=float(raw["created_at"]),
                payload=dict(raw.get("payload") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SignalError("invalid signal event") from exc
        if not event.id or "/" in event.id or "\\" in event.id:
            raise SignalError("invalid signal id")
        if not event.kind:
            raise SignalError("invalid signal kind")
        return event


@dataclass(frozen=True)
class SignalResult:
    event_id: str
    status: str
    completed_at: float
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "SignalResult":
        if not isinstance(raw, dict):
            raise SignalError("signal result must be an object")
        try:
            return cls(
                event_id=str(raw["event_id"]),
                status=str(raw["status"]),
                completed_at=float(raw["completed_at"]),
                detail=dict(raw.get("detail") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SignalError("invalid signal result") from exc


class SignalQueue:
    """Atomic pending → working → result lifecycle."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending = root / "pending"
        self.working = root / "working"
        self.results = root / "results"

    def _prepare(self) -> None:
        for path in (self.pending, self.working, self.results):
            path.mkdir(parents=True, exist_ok=True)

    def event_path(self, event_id: str, *, working: bool = False) -> Path:
        return (self.working if working else self.pending) / f"{event_id}.json"

    def result_path(self, event_id: str) -> Path:
        return self.results / f"{event_id}.json"

    def submit(self, kind: str, payload: dict[str, Any] | None = None) -> SignalEvent:
        self._prepare()
        event = SignalEvent.create(kind, payload)
        try:
            encoded = json.dumps(asdict(event), indent=2)
        except (TypeError, ValueError) as exc:
            raise SignalError("signal payload must be JSON-serializable") from exc
        atomic_write(self.event_path(event.id), encoded)
        return event

    def _read_event(self, path: Path) -> SignalEvent:
        try:
            return SignalEvent.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, SignalError) as exc:
            raise SignalError(f"cannot read queued signal {path.name}") from exc

    def claim(self) -> SignalEvent | None:
        """Atomically claim the oldest pending event.

        Another process may win the rename; keep trying until the directory is
        exhausted rather than treating that race as an empty queue.
        """
        self._prepare()
        for source in sorted(self.pending.glob("*.json")):
            target = self.working / source.name
            try:
                os.replace(source, target)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SignalError(f"cannot claim signal {source.name}: {exc}") from exc
            try:
                return self._read_event(target)
            except SignalError:
                target.unlink(missing_ok=True)
                raise
        return None

    def complete(
        self,
        event: SignalEvent,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> SignalResult:
        self._prepare()
        result = SignalResult(event.id, status, time.time(), dict(detail or {}))
        try:
            encoded = json.dumps(asdict(result), indent=2)
        except (TypeError, ValueError) as exc:
            raise SignalError("signal result must be JSON-serializable") from exc
        atomic_write(self.result_path(event.id), encoded)
        self.event_path(event.id, working=True).unlink(missing_ok=True)
        return result

    def read_result(self, event_id: str) -> SignalResult | None:
        path = self.result_path(event_id)
        if not path.exists():
            return None
        try:
            return SignalResult.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, SignalError) as exc:
            raise SignalError(f"cannot read result for {event_id}") from exc

    def wait(
        self,
        event_id: str,
        timeout: float,
        *,
        interval: float = 0.1,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> SignalResult | None:
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
        """Return events claimed by a crashed worker to the pending queue."""
        self._prepare()
        recovered = 0
        for source in sorted(self.working.glob("*.json")):
            target = self.pending / source.name
            try:
                if target.exists():
                    # A duplicate pending copy already exists; keep the
                    # pending one and discard the stale working claim.
                    source.unlink()
                else:
                    os.replace(source, target)
                recovered += 1
            except FileNotFoundError:
                continue
        return recovered

    def pending_count(self) -> int:
        try:
            return sum(1 for _ in self.pending.glob("*.json"))
        except OSError:
            return 0

    def prune_results(self, keep: int = 100) -> int:
        self._prepare()
        paths = sorted(
            self.results.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in paths[max(0, keep) :]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed
