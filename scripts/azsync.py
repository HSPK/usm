#!/usr/bin/env python3
"""Persistent local → Azure Blob sync. Think `rsync -avuP` that never stops.

Watches a local directory and pushes it to Blob Storage with `azcopy sync`,
batching changes so a busy tree doesn't launch a transfer per keystroke.

Examples
--------
  usm azsync add ./data https://acct.blob.core.windows.net/bucket/data
  usm azsync add ./out /mnt/blob/out --delete --sas-command 'mint-sas.sh'
  usm azsync ls
  usm azsync status data
  usm azsync sync data                 # transfer now, skip the debounce
  usm azsync enable data               # start at boot (systemd/launchd)
  usm azsync once ./one-off https://acct.blob.core.windows.net/bucket/x

Design
------
`azcopy sync` already does a full source↔destination comparison, so the
watcher is only ever a *trigger*, never the source of truth: missed events
just delay a transfer until the next heartbeat, they can't cause drift.
That makes the watcher free to degrade (poll instead of inotify) safely.

The layers below are one-directional and individually testable:

    CLI ──▶ Supervisor ──▶ TriggerPolicy  (pure, clock-injected)
                       ├─▶ Watcher       (protocol: inotify | polling)
                       ├─▶ SasProvider   (protocol: aad|az|inline|env|
                       │                   file|exec|http)
                       └─▶ AzcopyEngine  (argv → run → parse → classify)

None of the four know about each other; only the supervisor wires them.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable

import click
from rich.console import Console

from usm_azure import (
    AUTH_KINDS,
    SECTION,
    DEFAULT_SAS_MIN_REMAINING,
    DEFAULT_SAS_TTL_HOURS,
    LOCAL_BIN_DIR,
    USM_CACHE_DIR,
    ExcludeSpec,
    FileLock,
    SasError,
    SasManager,
    SasToken,
    resolve_blob_path,
    ServiceManager,
    atomic_write,
    compact_duration,
    build_provider,
    has_sas,
    human_bytes,
    human_duration,
    join_sas,
    kv_table,
    new_table,
    parse_blob_url,
    pid_alive,
    read_json,
    redact,
    shorten_path,
    short_blob_target,
    slugify,
    split_sas,
    usm_bin,
)

console = Console(stderr=True)

# Overridable so a spawned supervisor (a separate process) can be pointed at
# a scratch directory, and so users can relocate the state.
STATE_DIR = Path(os.environ.get("USM_AZSYNC_STATE_DIR") or (USM_CACHE_DIR / "azsync"))

SERVICE = ServiceManager("usm-azsync-", "com.github.hspk.usm.azsync.")
SUPERVISE_ENV = "USM_AZSYNC_SUPERVISE_ID"

# Defaults for the trigger policy. See decide() for what each one does.
DEFAULT_QUIET_PERIOD = 5.0
DEFAULT_BATCH_FILES = 200
DEFAULT_BATCH_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_DELAY = 300.0
DEFAULT_INTERVAL = 3600.0
DEFAULT_MIN_GAP = 30.0
DEFAULT_MIN_FILES = 1

DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_MAX_INDEX_FILES = 200_000

BACKOFF_BASE = 30.0
BACKOFF_MAX = 900.0
STARTUP_GRACE_SECS = 2.0

HISTORY_LIMIT = 200

WATCH_MODES = ("auto", "inotify", "poll")


# Data model ----------------------------------------------------------------

WATCH_MODES = ("auto", "inotify", "poll")


@dataclass
class SyncJob:
    """User intent. Written by the CLI, read by everything."""

    id: str
    source: str
    dest: str  # blob URL, never carries a SAS (see SasProvider)

    # credentials
    auth: str = "az"
    sas_spec: str | None = None  # env var / file / command / url, per auth kind
    sas_headers: list[str] = field(default_factory=list)
    sas_ttl_hours: int = DEFAULT_SAS_TTL_HOURS
    sas_min_remaining: float = DEFAULT_SAS_MIN_REMAINING

    # trigger policy
    quiet_period: float = DEFAULT_QUIET_PERIOD
    batch_files: int = DEFAULT_BATCH_FILES
    batch_bytes: int = DEFAULT_BATCH_BYTES
    max_delay: float = DEFAULT_MAX_DELAY
    interval: float = DEFAULT_INTERVAL
    min_gap: float = DEFAULT_MIN_GAP
    min_files: int = DEFAULT_MIN_FILES

    # watching
    watch_mode: str = "auto"
    poll_interval: float = DEFAULT_POLL_INTERVAL
    max_index_files: int = DEFAULT_MAX_INDEX_FILES

    # transfer
    excludes: list[str] = field(default_factory=list)
    default_excludes: bool = True
    delete_destination: bool = False
    compare_hash: bool = False
    cap_mbps: float | None = None
    block_size_mb: float | None = None
    put_md5: bool = False
    extra_args: list[str] = field(default_factory=list)

    def exclude_spec(self) -> ExcludeSpec:
        return ExcludeSpec.build(self.excludes, defaults=self.default_excludes)

    def source_path(self) -> Path:
        return Path(self.source)

    def account_container(self) -> tuple[str, str]:
        return parse_blob_url(self.dest)

    def route(self) -> str:
        return f"{self.source} → {redact(self.dest)}"


@dataclass
class RuntimeState:
    """Daemon-owned counters. Never written by the CLI (avoids lost updates)."""

    state: str = "stopped"  # stopped|idle|syncing|backoff|failed
    pid: int | None = None
    supervisor_pid: int | None = None
    started_at: float | None = None
    watch_backend: str | None = None
    degraded: bool = False

    pending_files: int = 0
    pending_bytes: int = 0
    pending_since: float | None = None

    last_sync_at: float | None = None
    last_sync_end: float | None = None
    last_duration: float | None = None
    last_reason: str | None = None
    last_result: str | None = None
    last_error: str | None = None
    last_job_id: str | None = None

    total_syncs: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    backoff_until: float | None = None

    sas_expires_at: float | None = None
    next_deadline: float | None = None


# Store ---------------------------------------------------------------------


def _def_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.json"


def _state_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.state.json"


def _sas_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.sas"


def job_provider(job: SyncJob):
    """Build this job's SAS provider from its stored auth description."""
    account = container = None
    if (job.auth or "az").lower() == "az":
        try:
            account, container = job.account_container()
        except ValueError as exc:
            raise SasError(str(exc)) from exc
    return build_provider(
        job.auth,
        spec=job.sas_spec,
        headers=job.sas_headers,
        url=job.dest,
        account=account,
        container=container,
        ttl_hours=job.sas_ttl_hours,
    )


def job_sas_manager(job: SyncJob) -> SasManager:
    return SasManager(
        job_provider(job),
        _sas_path(job.id),
        min_remaining=job.sas_min_remaining,
    )


def log_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.log"


def history_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.history.jsonl"


def trigger_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.trigger"


def lock_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.lock"


def azcopy_dir(job_id: str) -> Path:
    return STATE_DIR / job_id / "azcopy"


def _from_dict(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def save_job(job: SyncJob) -> None:
    atomic_write(_def_path(job.id), json.dumps(asdict(job), indent=2))


def load_job(job_id: str) -> SyncJob:
    raw = read_json(_def_path(job_id))
    if not isinstance(raw, dict):
        raise KeyError(job_id)
    return _from_dict(SyncJob, raw)


def list_jobs() -> list[SyncJob]:
    if not STATE_DIR.exists():
        return []
    out = []
    for path in sorted(STATE_DIR.glob("*.json")):
        if path.name.endswith(".state.json"):
            continue
        raw = read_json(path)
        if not isinstance(raw, dict):
            continue
        try:
            out.append(_from_dict(SyncJob, raw))
        except TypeError:
            continue
    return out


def save_state(job_id: str, state: RuntimeState) -> None:
    atomic_write(_state_path(job_id), json.dumps(asdict(state), indent=2))


def load_state(job_id: str) -> RuntimeState:
    raw = read_json(_state_path(job_id))
    if not isinstance(raw, dict):
        return RuntimeState()
    try:
        return _from_dict(RuntimeState, raw)
    except TypeError:
        return RuntimeState()


def delete_job(job_id: str) -> None:
    import shutil

    for path in (
        _def_path(job_id),
        _state_path(job_id),
        _sas_path(job_id),
        log_path(job_id),
        history_path(job_id),
        trigger_path(job_id),
        lock_path(job_id),
    ):
        path.unlink(missing_ok=True)
    shutil.rmtree(STATE_DIR / job_id, ignore_errors=True)


def append_history(job_id: str, record: dict) -> None:
    path = history_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    _trim_history(path)


def _trim_history(path: Path) -> None:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    if len(lines) > HISTORY_LIMIT * 2:
        atomic_write(path, "\n".join(lines[-HISTORY_LIMIT:]) + "\n")


def read_history(job_id: str, limit: int = 10) -> list[dict]:
    path = history_path(job_id)
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def make_job_id(source: Path, dest: str, custom: str | None = None) -> str:
    """Derive a stable, readable id; de-duplicate with a numeric suffix."""
    if custom:
        return slugify(custom) or "azsync"
    base = slugify(source.name) or "root"
    try:
        _, container = parse_blob_url(dest)
        if container and slugify(container) != base:
            base = f"{base}-{slugify(container)}"
    except ValueError:
        pass
    base = base or "azsync"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    used = {p.stem for p in STATE_DIR.glob("*.json")}
    if base not in used:
        return base
    for i in range(2, 1000):
        candidate = f"{base}-{i}"
        if candidate not in used:
            return candidate
    return f"{base}-{int(time.time())}"


# Change accounting ---------------------------------------------------------


@dataclass
class ChangeStat:
    """What changed since the last transfer started."""

    files: int = 0
    bytes: int = 0
    deletes: int = 0
    first_at: float | None = None
    last_at: float | None = None

    def record(self, now: float, *, size: int = 0, deleted: bool = False) -> None:
        self.files += 1
        self.bytes += max(0, size)
        if deleted:
            self.deletes += 1
        if self.first_at is None:
            self.first_at = now
        self.last_at = now

    def merge(self, other: "ChangeStat") -> None:
        if not other.files:
            return
        self.files += other.files
        self.bytes += other.bytes
        self.deletes += other.deletes
        firsts = [t for t in (self.first_at, other.first_at) if t is not None]
        lasts = [t for t in (self.last_at, other.last_at) if t is not None]
        self.first_at = min(firsts) if firsts else None
        self.last_at = max(lasts) if lasts else None

    def copy(self) -> "ChangeStat":
        return ChangeStat(
            self.files, self.bytes, self.deletes, self.first_at, self.last_at
        )

    def clear(self) -> None:
        self.files = self.bytes = self.deletes = 0
        self.first_at = self.last_at = None

    def __bool__(self) -> bool:
        return self.files > 0


class ChangeAccumulator:
    """Thread-safe ChangeStat: watcher threads write, supervisor drains."""

    def __init__(self) -> None:
        self._stat = ChangeStat()
        self._lock = threading.Lock()
        self._degraded = False
        self.updated = threading.Event()

    def record(self, now: float, *, size: int = 0, deleted: bool = False) -> None:
        with self._lock:
            self._stat.record(now, size=size, deleted=deleted)
        self.updated.set()

    def mark_degraded(self) -> None:
        """Watcher lost events; force a transfer since sync self-heals."""
        with self._lock:
            self._degraded = True
        self.updated.set()

    @property
    def degraded(self) -> bool:
        with self._lock:
            return self._degraded

    def snapshot(self) -> ChangeStat:
        with self._lock:
            return self._stat.copy()

    def take(self) -> ChangeStat:
        """Atomically remove and return the batch (called at job start)."""
        with self._lock:
            taken = self._stat.copy()
            self._stat.clear()
            self._degraded = False
            self.updated.clear()
            return taken

    def give_back(self, stat: ChangeStat) -> None:
        """Return a failed batch so its changes aren't silently dropped."""
        with self._lock:
            self._stat.merge(stat)
        if stat.files:
            self.updated.set()


# Trigger policy ------------------------------------------------------------

WAIT = "wait"
SYNC = "sync"


@dataclass(frozen=True)
class TriggerConfig:
    quiet_period: float = DEFAULT_QUIET_PERIOD
    batch_files: int = DEFAULT_BATCH_FILES
    batch_bytes: int = DEFAULT_BATCH_BYTES
    max_delay: float = DEFAULT_MAX_DELAY
    interval: float = DEFAULT_INTERVAL
    min_gap: float = DEFAULT_MIN_GAP
    min_files: int = DEFAULT_MIN_FILES

    @classmethod
    def from_job(cls, job: SyncJob) -> "TriggerConfig":
        return cls(
            quiet_period=job.quiet_period,
            batch_files=job.batch_files,
            batch_bytes=job.batch_bytes,
            max_delay=job.max_delay,
            interval=job.interval,
            min_gap=job.min_gap,
            min_files=job.min_files,
        )


@dataclass(frozen=True)
class PolicyInput:
    """Everything decide() is allowed to look at — no globals, no clock."""

    running: bool = False
    forced: bool = False
    degraded: bool = False
    last_end: float | None = None
    backoff_until: float | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""
    wake_at: float | None = None  # when the caller should re-evaluate

    @property
    def should_sync(self) -> bool:
        return self.action == SYNC


def decide(
    now: float, acc: ChangeStat, rt: PolicyInput, cfg: TriggerConfig
) -> Decision:
    """Decide whether to launch a transfer now, and when to look again.

    Ordering matters. Suppressors come first (a running job, backoff and the
    rate limit outrank every trigger), then the reasons to fire, cheapest and
    most urgent first. Every WAIT carries the deadline that could change the
    answer, so the caller sleeps exactly that long instead of polling.
    """
    if rt.running:
        return Decision(WAIT, "syncing")

    if rt.backoff_until and now < rt.backoff_until:
        return Decision(WAIT, "backoff", rt.backoff_until)

    # A manual trigger overrides the rate limit; that's what "now" means.
    if rt.forced:
        return Decision(SYNC, "manual")

    gap_ready = (rt.last_end + cfg.min_gap) if rt.last_end is not None else now
    heartbeat_at = (rt.last_end + cfg.interval) if rt.last_end is not None else now

    if now < gap_ready:
        # Rate limited, but note whether there is anything worth waking for.
        return Decision(WAIT, "min-gap", gap_ready)

    # The watcher admitted it lost events: resync, sync is a full compare.
    if rt.degraded:
        return Decision(SYNC, "degraded")

    if not acc.files:
        if now >= heartbeat_at:
            return Decision(SYNC, "heartbeat")
        return Decision(WAIT, "idle", heartbeat_at)

    if acc.files >= cfg.batch_files or (
        cfg.batch_bytes and acc.bytes >= cfg.batch_bytes
    ):
        return Decision(SYNC, "volume")

    max_delay_at = (acc.first_at or now) + cfg.max_delay
    if now >= max_delay_at:
        return Decision(SYNC, "max-delay")

    quiet_at = (acc.last_at or now) + cfg.quiet_period
    if now >= quiet_at and acc.files >= cfg.min_files:
        return Decision(SYNC, "quiet")

    # Not enough yet: wake at whichever deadline arrives first.
    candidates = [max_delay_at, heartbeat_at]
    if acc.files >= cfg.min_files:
        candidates.append(quiet_at)
    return Decision(WAIT, "debounce", min(candidates))


def backoff_delay(consecutive_failures: int) -> float:
    """Exponential backoff, capped. 1 failure → 30s, 2 → 60s, … ≤ 15m."""
    if consecutive_failures <= 0:
        return 0.0
    return min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (consecutive_failures - 1)))


# ==========================================================================
# azcopy engine
#
# Owns everything about the external binary: where it is, how the command is
# built, how its NDJSON output is read back, and what a failure means. The
# supervisor only sees SyncResult.
# ==========================================================================

AZCOPY_DOWNLOADS = {
    ("linux", "amd64"): "https://aka.ms/downloadazcopy-v10-linux",
    ("linux", "arm64"): "https://aka.ms/downloadazcopy-v10-linux-arm64",
    ("darwin", "amd64"): "https://aka.ms/downloadazcopy-v10-mac",
    ("darwin", "arm64"): "https://aka.ms/downloadazcopy-v10-mac-arm64",
    ("windows", "amd64"): "https://aka.ms/downloadazcopy-v10-windows",
    ("windows", "arm64"): "https://aka.ms/downloadazcopy-v10-windows-arm64",
}

# Failure classes, ordered by how the supervisor should react.
OK = "ok"
PARTIAL = "partial"
AUTH_EXPIRED = "auth_expired"
AUTH_INVALID = "auth_invalid"
NETWORK = "network"
FATAL = "fatal"
CANCELLED = "cancelled"

_AUTH_EXPIRED_MARKERS = (
    "signature not valid in the specified time frame",
    "authenticationfailed",
    "server failed to authenticate the request",
    "sas token has expired",
    "signature did not match",
)
_AUTH_INVALID_MARKERS = (
    "authorizationpermissionmismatch",
    "authorizationfailure",
    "this request is not authorized to perform this operation",
    "invalid credentials",
    "no such host",
)
_FATAL_MARKERS = (
    "containernotfound",
    "the specified container does not exist",
    "cannot start job due to error",
    "failed to traverse",
    "does not exist",
    "invalidargument",
)
_NETWORK_MARKERS = (
    "dial tcp",
    "connection reset",
    "connection refused",
    "i/o timeout",
    "timeout",
    "temporary failure",
    "eof",
    "serverbusy",
    "operationtimedout",
    "internalerror",
    "503",
    "500",
)


class AzcopyNotFound(Exception):
    pass


def _azcopy_filename() -> str:
    import platform

    return "azcopy.exe" if platform.system().lower() == "windows" else "azcopy"


def find_azcopy() -> str | None:
    """Locate azcopy without downloading. Shares `usm cp`'s managed location."""
    import shutil

    override = os.environ.get("USM_AZCOPY_BIN")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("azcopy")
    if found:
        return found
    local = LOCAL_BIN_DIR / _azcopy_filename()
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return None


def ensure_azcopy() -> str:
    path = find_azcopy()
    if path:
        return path
    raise AzcopyNotFound(
        "azcopy not found. Install it with `usm cp --install`, put it on PATH, "
        "or point $USM_AZCOPY_BIN at an existing binary."
    )


@dataclass
class SyncResult:
    """Outcome of one azcopy invocation."""

    status: str = OK
    exit_code: int = 0
    job_id: str | None = None
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    bytes: int = 0
    duration: float = 0.0
    error: str | None = None
    job_status: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def summary(self) -> str:
        if self.status == OK and not self.completed:
            return "up to date"
        bits = [f"{self.completed} transferred"]
        if self.bytes:
            bits.append(human_bytes(self.bytes))
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.failed:
            bits.append(f"{self.failed} failed")
        return ", ".join(bits)


def parse_azcopy_json(stream: Iterable[str]) -> tuple[dict, list[str]]:
    """Fold azcopy's NDJSON into ``(end_of_job_summary, error_lines)``.

    Each line is ``{"MessageType": ..., "MessageContent": ...}`` where the
    EndOfJob content is itself a JSON *string*. Non-JSON lines (azcopy still
    prints the odd banner) are kept as diagnostics rather than dropped.
    """
    summary: dict = {}
    errors: list[str] = []
    for raw in stream:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("{"):
            errors.append(line)
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            errors.append(line)
            continue
        kind = envelope.get("MessageType")
        content = envelope.get("MessageContent")
        if kind == "EndOfJob" and isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    summary = parsed
            except json.JSONDecodeError:
                errors.append(content)
        elif kind in ("Error", "Prompt") and isinstance(content, str) and content:
            errors.append(content)
    return summary, errors


def classify_failure(text: str, exit_code: int) -> str:
    """Map azcopy's error text onto a recovery strategy."""
    blob = (text or "").lower()
    for marker in _AUTH_EXPIRED_MARKERS:
        if marker in blob:
            return AUTH_EXPIRED
    for marker in _AUTH_INVALID_MARKERS:
        if marker in blob:
            return AUTH_INVALID
    for marker in _FATAL_MARKERS:
        if marker in blob:
            return FATAL
    for marker in _NETWORK_MARKERS:
        if marker in blob:
            return NETWORK
    # Unknown failures are treated as transient: retrying is cheap and sync
    # is idempotent, whereas giving up needs human attention.
    return NETWORK if exit_code else OK


def interpret_result(
    summary: dict, errors: list[str], exit_code: int, duration: float
) -> SyncResult:
    result = SyncResult(
        exit_code=exit_code,
        job_id=summary.get("JobID") or None,
        completed=int(summary.get("TransfersCompleted") or 0),
        failed=int(summary.get("TransfersFailed") or 0),
        skipped=int(summary.get("TransfersSkipped") or 0),
        bytes=int(summary.get("TotalBytesTransferred") or 0),
        job_status=summary.get("JobStatus") or None,
        duration=duration,
    )
    text = "\n".join([str(summary.get("ErrorMsg") or ""), *errors[-20:]]).strip()
    result.error = redact(text)[-2000:] or None

    if exit_code == 0 and not result.failed:
        result.status = OK
        return result
    if result.job_status and result.job_status.lower() == "cancelled":
        result.status = CANCELLED
        return result

    classified = classify_failure(text, exit_code)
    if classified == OK:
        # Exit code 0 but transfers failed: sync will retry them next round.
        result.status = PARTIAL if result.failed else OK
    elif classified == NETWORK and result.completed and result.failed:
        result.status = PARTIAL
    else:
        result.status = classified
    if result.status == OK and result.failed:
        result.status = PARTIAL
    return result


class AzcopyEngine:
    """Build, run and interpret `azcopy sync`."""

    def __init__(
        self,
        job: SyncJob,
        *,
        binary: str | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.job = job
        self._binary = binary
        self.work_dir = state_dir or azcopy_dir(job.id)

    @property
    def binary(self) -> str:
        if self._binary is None:
            self._binary = ensure_azcopy()
        return self._binary

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        log_dir = self.work_dir / "log"
        plan_dir = self.work_dir / "plan"
        log_dir.mkdir(parents=True, exist_ok=True)
        plan_dir.mkdir(parents=True, exist_ok=True)
        env["AZCOPY_LOG_LOCATION"] = str(log_dir)
        env["AZCOPY_JOB_PLAN_LOCATION"] = str(plan_dir)
        # Interactive prompts would hang a daemon forever.
        env.setdefault("AZCOPY_CONCURRENCY_VALUE", "AUTO")
        if self.job.auth == "aad":
            env.setdefault("AZCOPY_AUTO_LOGIN_TYPE", "AZCLI")
        return env

    def build_argv(self, sas: str | None, *, dry_run: bool = False) -> list[str]:
        job = self.job
        source = str(Path(job.source))
        dest = join_sas(split_sas(job.dest)[0], sas)
        argv = [
            self.binary,
            "sync",
            source,
            dest,
            "--recursive",
            f"--delete-destination={'true' if job.delete_destination else 'false'}",
            "--output-type=json",
            "--log-level=ERROR",
        ]
        if job.compare_hash:
            argv.append("--compare-hash=MD5")
            argv.append("--put-md5")
        elif job.put_md5:
            argv.append("--put-md5")
        argv += job.exclude_spec().to_azcopy_flags()
        if job.cap_mbps:
            argv += ["--cap-mbps", str(job.cap_mbps)]
        if job.block_size_mb:
            argv += ["--block-size-mb", str(job.block_size_mb)]
        if dry_run:
            argv.append("--dry-run")
        argv += list(job.extra_args)
        return argv

    def build_resume_argv(self, job_id: str, sas: str | None) -> list[str]:
        argv = [self.binary, "jobs", "resume", job_id, "--output-type=json"]
        if sas:
            argv += ["--destination-sas", sas]
        return argv

    # -- execution ---------------------------------------------------------

    def run(
        self,
        argv: list[str],
        *,
        on_start=None,
        log=None,
        timeout: float | None = None,
    ) -> SyncResult:
        started = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self.env(),
            )
        except FileNotFoundError as exc:
            return SyncResult(status=FATAL, exit_code=127, error=str(exc), duration=0.0)
        if on_start is not None:
            on_start(proc)

        # The deadline has to cover reading too: azcopy streams progress, so
        # waiting only on exit would let a stalled transfer hang forever.
        timed_out = threading.Event()
        watchdog = None
        if timeout:

            def expire():
                timed_out.set()
                try:
                    proc.kill()
                except OSError:  # pragma: no cover - already gone
                    pass

            watchdog = threading.Timer(timeout, expire)
            watchdog.daemon = True
            watchdog.start()

        lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                if log is not None and line.strip():
                    log(redact(line.rstrip()))
            proc.wait()
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if proc.stdout is not None:
                proc.stdout.close()

        if timed_out.is_set():
            return SyncResult(
                status=NETWORK,
                exit_code=-1,
                error=f"azcopy exceeded the {timeout:g}s timeout",
                duration=time.time() - started,
            )
        summary, errors = parse_azcopy_json(lines)
        return interpret_result(
            summary, errors, proc.returncode or 0, time.time() - started
        )


# ==========================================================================
# Watchers
#
# Only ever a trigger: `azcopy sync` compares the whole tree anyway, so a
# watcher that misses events costs latency (until the heartbeat), never
# correctness. That licence is what lets the inotify backend fall back to
# polling, and lets either one declare itself degraded and move on.
# ==========================================================================


class Watcher:
    """Feed a ChangeAccumulator. Backends differ only in how they notice."""

    backend = "none"

    def __init__(self, root: Path, excludes: ExcludeSpec, acc: ChangeAccumulator):
        self.root = root
        self.excludes = excludes
        self.acc = acc

    def start(self) -> None:  # pragma: no cover - overridden
        pass

    def stop(self) -> None:  # pragma: no cover - overridden
        pass

    def _relpath(self, path: str) -> str | None:
        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            return None
        if rel.startswith(".."):
            return None
        return rel.replace(os.sep, "/")

    def _admit(self, path: str) -> str | None:
        """Return the relative path if it should count as a change."""
        rel = self._relpath(path)
        if rel is None or rel == ".":
            return None
        return None if self.excludes.matches(rel) else rel


class InotifyWatcher(Watcher):
    """watchdog-backed: inotify on Linux, FSEvents on macOS."""

    backend = "inotify"

    def __init__(self, root: Path, excludes: ExcludeSpec, acc: ChangeAccumulator):
        super().__init__(root, excludes, acc)
        self._observer = None

    @staticmethod
    def available() -> bool:
        try:
            import watchdog.observers  # noqa: F401
        except Exception:
            return False
        return True

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:
                if getattr(event, "is_directory", False):
                    return
                kind = event.event_type
                if kind not in ("created", "modified", "deleted", "moved", "closed"):
                    return
                # A move shows up as one event with both ends; count the
                # destination (that's the file azcopy will upload).
                path = getattr(event, "dest_path", None) or event.src_path
                if watcher._admit(path) is None:
                    return
                size = 0
                deleted = kind == "deleted"
                if not deleted:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                watcher.acc.record(time.time(), size=size, deleted=deleted)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.root), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except RuntimeError:  # pragma: no cover - observer already dead
            pass
        self._observer = None


class PollingWatcher(Watcher):
    """Stdlib fallback for network mounts, blobfuse, and inotify-less hosts.

    Keeps a ``{relpath: (mtime, size)}`` index and diffs it. Above
    ``max_index_files`` the index is dropped and the tree is reduced to an
    aggregate signature — still enough to *trigger* a sync, which is all a
    watcher owes us.
    """

    backend = "poll"

    def __init__(
        self,
        root: Path,
        excludes: ExcludeSpec,
        acc: ChangeAccumulator,
        *,
        interval: float = DEFAULT_POLL_INTERVAL,
        max_index_files: int = DEFAULT_MAX_INDEX_FILES,
    ) -> None:
        super().__init__(root, excludes, acc)
        self.interval = interval
        self.max_index_files = max_index_files
        self._index: dict[str, tuple[float, int]] | None = {}
        self._signature: tuple[int, int, float] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def scan(self) -> tuple[dict[str, tuple[float, int]], tuple[int, int, float]]:
        index: dict[str, tuple[float, int]] = {}
        count = 0
        total = 0
        newest = 0.0
        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                rel = self._relpath(entry.path)
                if rel is None or self.excludes.matches(rel):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                total += stat.st_size
                newest = max(newest, stat.st_mtime)
                if len(index) < self.max_index_files:
                    index[rel] = (stat.st_mtime, stat.st_size)
        return index, (count, total, newest)

    def poll_once(self) -> None:
        index, signature = self.scan()
        indexed = len(index) < self.max_index_files
        previous, prev_sig = self._index, self._signature
        self._index = index if indexed else None
        self._signature = signature

        if prev_sig is None:
            return  # first scan establishes the baseline
        if not indexed or previous is None:
            if signature != prev_sig:
                # No per-file detail available; report one aggregate change.
                delta = abs(signature[1] - prev_sig[1])
                self.acc.record(time.time(), size=delta)
                self.acc.mark_degraded()
            return

        now = time.time()
        changed = 0
        for rel, meta in index.items():
            old = previous.get(rel)
            if old is None or old != meta:
                self.acc.record(now, size=meta[1])
                changed += 1
        for rel in previous:
            if rel not in index:
                self.acc.record(now, deleted=True)
                changed += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - never kill the watcher
                self.acc.mark_degraded()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.poll_once()  # baseline, synchronously
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="azsync-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def build_watcher(job: SyncJob, acc: ChangeAccumulator, *, warn=None) -> Watcher:
    """Pick a backend, honouring --watch-mode and degrading when needed."""
    root = job.source_path()
    excludes = job.exclude_spec()
    mode = (job.watch_mode or "auto").lower()

    def polling() -> Watcher:
        return PollingWatcher(
            root,
            excludes,
            acc,
            interval=job.poll_interval,
            max_index_files=job.max_index_files,
        )

    if mode == "poll":
        return polling()
    if mode == "inotify":
        if not InotifyWatcher.available():
            raise click.ClickException(
                "--watch-mode inotify needs the 'watchdog' package."
            )
        return InotifyWatcher(root, excludes, acc)

    if InotifyWatcher.available():
        watcher = InotifyWatcher(root, excludes, acc)
        try:
            watcher.start()
            return _StartedWatcher(watcher)
        except Exception as exc:  # inotify limits, network fs, no permissions
            if warn is not None:
                warn(f"inotify unavailable ({exc}); falling back to polling.")
            try:
                watcher.stop()
            except Exception:
                pass
    elif warn is not None:
        warn("watchdog not installed; using the polling watcher.")
    return polling()


class _StartedWatcher(Watcher):
    """Wrap a watcher that `build_watcher` already had to start to test it."""

    def __init__(self, inner: Watcher) -> None:
        self.inner = inner
        self.backend = inner.backend
        self.root = inner.root
        self.excludes = inner.excludes
        self.acc = inner.acc

    def start(self) -> None:
        pass  # already running

    def stop(self) -> None:
        self.inner.stop()


# ==========================================================================
# Supervisor
#
# The only stateful, IO-orchestrating layer: it owns the loop, the process
# lifetime and the state file. Everything it decides comes from the pure
# policy; everything it transfers goes through the engine.
# ==========================================================================


class Supervisor:
    def __init__(
        self,
        job: SyncJob,
        *,
        engine: AzcopyEngine | None = None,
        sas: SasManager | None = None,
        watcher: Watcher | None = None,
        acc: ChangeAccumulator | None = None,
        clock=time.time,
        log=None,
    ) -> None:
        self.job = job
        self.cfg = TriggerConfig.from_job(job)
        self.acc = acc or ChangeAccumulator()
        self.engine = engine or AzcopyEngine(job)
        self.sas = sas if sas is not None else job_sas_manager(job)
        self.watcher = watcher
        self.clock = clock
        self.state = RuntimeState()
        self._log = log or self._default_log
        self._stop = threading.Event()
        self._forced = False
        self._child: subprocess.Popen | None = None
        self._running = False

    # -- plumbing ----------------------------------------------------------

    def _default_log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"{stamp} {redact(message)}", flush=True)

    def log(self, message: str) -> None:
        self._log(message)

    def _persist(self) -> None:
        pending = self.acc.snapshot()
        self.state.pending_files = pending.files
        self.state.pending_bytes = pending.bytes
        self.state.pending_since = pending.first_at
        token = self.sas.current() if self.sas.enabled else None
        self.state.sas_expires_at = token.expires_at if token else None
        save_state(self.job.id, self.state)

    def request_stop(self) -> None:
        self._stop.set()
        self.acc.updated.set()
        child = self._child
        if child is not None and child.poll() is None:
            try:
                # azcopy checkpoints its job plan on SIGTERM, so the next
                # start can resume instead of re-walking everything.
                child.terminate()
            except OSError:  # pragma: no cover
                pass

    def request_sync(self) -> None:
        self._forced = True
        self.acc.updated.set()

    def _consume_trigger_file(self) -> None:
        path = trigger_path(self.job.id)
        if path.exists():
            path.unlink(missing_ok=True)
            self._forced = True

    # -- one transfer ------------------------------------------------------

    def _needed_lifetime(self) -> float:
        return self.sas.needed_lifetime(self.state.last_duration)

    def run_sync(self, reason: str) -> SyncResult:
        """Run one transfer end to end, including SAS refresh and recovery."""
        now = self.clock()
        batch = self.acc.take()
        self._running = True
        self.state.state = "syncing"
        self.state.last_reason = reason
        self.state.last_sync_at = now
        self._persist()

        detail = (
            f"{batch.files} change(s), {human_bytes(batch.bytes)}"
            if batch.files
            else "no local changes"
        )
        self.log(f"sync start ({reason}; {detail})")

        try:
            token = self.sas.ensure(now, need=self._needed_lifetime())
        except SasError as exc:
            self.acc.give_back(batch)
            self._running = False
            return self._finish(
                SyncResult(status=AUTH_INVALID, error=str(exc)), batch, reason
            )

        if (
            self.sas.enabled
            and token.expires_at
            and not self.sas.provider.refreshable
            and token.remaining(now) is not None
            and token.remaining(now) < self.job.sas_min_remaining
        ):
            self.log(
                f"warning: inline SAS expires in "
                f"{human_duration(token.remaining(now))} and cannot be rotated."
            )

        result = self._execute(token)

        if result.status == AUTH_EXPIRED and self.sas.provider.refreshable:
            self.log("credential rejected; refreshing SAS and resuming")
            try:
                fresh = self.sas.ensure(
                    self.clock(), need=self._needed_lifetime(), force=True
                )
            except SasError as exc:
                result = SyncResult(status=AUTH_INVALID, error=str(exc))
            else:
                result = self._retry_after_refresh(fresh, result)

        self._running = False
        return self._finish(result, batch, reason)

    def _execute(self, token: SasToken) -> SyncResult:
        argv = self.engine.build_argv(token.token or None)
        return self.engine.run(argv, on_start=self._track_child, log=self._azcopy_line)

    def _retry_after_refresh(self, token: SasToken, previous: SyncResult) -> SyncResult:
        """Resume the interrupted job with a new SAS, or start a fresh one."""
        if previous.job_id:
            argv = self.engine.build_resume_argv(previous.job_id, token.token or None)
            result = self.engine.run(
                argv, on_start=self._track_child, log=self._azcopy_line
            )
            if result.status != FATAL:
                return result
            self.log("resume rejected the job id; running a full sync instead")
        return self._execute(token)

    def _track_child(self, proc: subprocess.Popen) -> None:
        self._child = proc

    def _azcopy_line(self, line: str) -> None:
        # azcopy's JSON stream is noisy; only surface what a human needs.
        if '"MessageType":"Error"' in line or '"JobStatus"' in line:
            self.log(f"azcopy: {line[:400]}")

    def _finish(self, result: SyncResult, batch: ChangeStat, reason: str) -> SyncResult:
        self._child = None
        now = self.clock()
        self.state.last_sync_end = now
        self.state.last_duration = result.duration or None
        self.state.last_result = result.status
        self.state.last_error = result.error
        self.state.last_job_id = result.job_id
        self.state.total_syncs += 1

        if result.status in (OK, PARTIAL):
            if result.status == PARTIAL:
                self.state.total_failures += 1
                self.state.consecutive_failures += 1
            else:
                self.state.consecutive_failures = 0
            self.state.backoff_until = None
            self.state.state = "idle"
            self.log(f"sync {result.status} ({result.summary()})")
        elif result.status == CANCELLED:
            self.acc.give_back(batch)
            self.state.state = "idle"
            self.log("sync cancelled")
        else:
            # Nothing reached the destination we can rely on: put the batch
            # back so its changes still count towards the next decision.
            self.acc.give_back(batch)
            self.state.total_failures += 1
            self.state.consecutive_failures += 1
            delay = backoff_delay(self.state.consecutive_failures)
            self.state.backoff_until = now + delay
            self.state.state = "failed" if result.status == FATAL else "backoff"
            self.log(
                f"sync failed ({result.status}): "
                f"{(result.error or 'no detail').splitlines()[-1][:300]}"
            )
            if result.status != FATAL:
                self.log(f"retrying in {human_duration(delay)}")

        append_history(
            self.job.id,
            {
                "at": now,
                "reason": reason,
                "status": result.status,
                "duration": round(result.duration, 3),
                "files": batch.files,
                "completed": result.completed,
                "failed": result.failed,
                "skipped": result.skipped,
                "bytes": result.bytes,
                "job_id": result.job_id,
                "error": (result.error or "")[:500] or None,
            },
        )
        self._persist()
        return result

    # -- the loop ----------------------------------------------------------

    def _policy_input(self) -> PolicyInput:
        return PolicyInput(
            running=self._running,
            forced=self._forced,
            degraded=self.acc.degraded,
            last_end=self.state.last_sync_end,
            backoff_until=self.state.backoff_until,
        )

    def tick(self) -> Decision:
        """Evaluate once. Split out from run() so tests can drive it."""
        self._consume_trigger_file()
        decision = decide(
            self.clock(), self.acc.snapshot(), self._policy_input(), self.cfg
        )
        self.state.next_deadline = decision.wake_at
        if decision.should_sync:
            self._forced = False
            self.run_sync(decision.reason)
        return decision

    def run(self) -> int:
        job = self.job
        source = job.source_path()
        if not source.is_dir():
            self.log(f"source directory does not exist: {source}")
            self.state.state = "failed"
            self.state.last_error = f"source directory missing: {source}"
            self._persist()
            return 2

        if self.watcher is None:
            self.watcher = build_watcher(job, self.acc, warn=self.log)
        self.state.watch_backend = self.watcher.backend
        self.state.state = "idle"
        self.state.started_at = self.clock()
        self.state.supervisor_pid = os.getpid()
        self.state.pid = os.getpid()
        self._persist()
        self.log(
            f"watching {source} → {redact(split_sas(job.dest)[0])} "
            f"[{self.watcher.backend}, auth={job.auth}]"
        )

        try:
            self.watcher.start()
        except Exception as exc:
            self.log(f"watcher failed to start ({exc}); polling instead")
            self.watcher = PollingWatcher(
                source,
                job.exclude_spec(),
                self.acc,
                interval=job.poll_interval,
                max_index_files=job.max_index_files,
            )
            self.state.watch_backend = self.watcher.backend
            self.watcher.start()

        # An initial reconcile makes `azsync add` do something visible and
        # picks up whatever changed while the daemon was down.
        self._forced = True
        try:
            while not self._stop.is_set():
                decision = self.tick()
                if self._stop.is_set():
                    break
                if decision.should_sync:
                    continue
                self._persist()
                timeout = self._sleep_for(decision)
                self.acc.updated.wait(timeout)
                self.acc.updated.clear()
        finally:
            self.watcher.stop()
            self.state.state = "stopped"
            self.state.pid = None
            self.state.supervisor_pid = None
            self.state.next_deadline = None
            self._persist()
            self.log("stopped")
        return 0

    def _sleep_for(self, decision: Decision) -> float:
        """How long to block. Bounded so the trigger file is noticed promptly."""
        if decision.wake_at is None:
            return 5.0
        return max(0.2, min(5.0, decision.wake_at - self.clock()))


# ==========================================================================
# Daemon lifecycle + boot integration
# ==========================================================================


def spawn_daemon(job: SyncJob) -> int:
    """Start `usm azsync up <id>` detached, logging to the job's log file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_path(job.id), "ab", buffering=0)
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, SUPERVISE_ENV: job.id},
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - windows
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    argv = [sys.executable, str(Path(__file__).resolve()), "up", job.id]
    proc = subprocess.Popen(argv, **kwargs)
    return proc.pid


def is_running(job_id: str) -> bool:
    if SERVICE.enabled_kind(job_id):
        return SERVICE.is_active(job_id)
    state = load_state(job_id)
    return pid_alive(state.supervisor_pid)


def stop_daemon(job_id: str, *, timeout: float = 20.0) -> bool:
    """Ask the supervisor to finish the current transfer and exit."""
    import signal

    if SERVICE.enabled_kind(job_id):
        proc = SERVICE.stop(job_id)
        return proc is not None and proc.returncode == 0

    state = load_state(job_id)
    pid = state.supervisor_pid
    if not pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:  # pragma: no cover - only when azcopy ignores SIGTERM
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass
    return True


def poke_daemon(job_id: str) -> bool:
    """Ask a running supervisor to sync now (signal + trigger file)."""
    import signal

    trigger_path(job_id).parent.mkdir(parents=True, exist_ok=True)
    trigger_path(job_id).write_text(str(time.time()))
    state = load_state(job_id)
    pid = state.supervisor_pid
    if pid_alive(pid) and hasattr(signal, "SIGUSR1"):
        try:
            os.kill(pid, signal.SIGUSR1)
            return True
        except OSError:
            return False
    return pid_alive(pid)


def run_supervisor(job_id: str) -> int:
    """Foreground entry point (`usm azsync up <id>`, systemd ExecStart)."""
    import signal

    job = load_job(job_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path(job_id))
    if not lock.acquire():
        console.print(f"[red]✗[/red] {job_id} is already running.")
        return 1

    supervisor = Supervisor(job)
    if os.name == "posix":
        signal.signal(signal.SIGTERM, lambda *_: supervisor.request_stop())
        signal.signal(signal.SIGINT, lambda *_: supervisor.request_stop())
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, lambda *_: supervisor.request_sync())
    try:
        return supervisor.run()
    finally:
        lock.release()


# ==========================================================================
# CLI
# ==========================================================================


def resolve_destination(dest: str) -> str:
    """Accept an https blob URL or a path inside a blobfuse2 mount."""
    url = resolve_blob_path(dest)
    if url is None:
        raise click.ClickException(
            f"destination {dest!r} is neither an https blob URL nor inside a "
            "blobfuse2 mount. Pass "
            "https://<account>.blob.core.windows.net/<container>/<path>."
        )
    return url


def _require_job(job_id: str) -> SyncJob:
    try:
        return load_job(job_id)
    except KeyError:
        known = ", ".join(j.id for j in list_jobs()) or "none"
        raise click.ClickException(
            f"unknown sync {job_id!r}. Defined: {known}"
        ) from None


def _state_label(job: SyncJob) -> str:
    state = load_state(job.id)
    if not is_running(job.id):
        if state.state == "failed":
            return "[red]failed[/red]"
        return "[dim]stopped[/dim]"
    return {
        "syncing": "[cyan]syncing[/cyan]",
        "backoff": "[yellow]backoff[/yellow]",
        "failed": "[red]failed[/red]",
    }.get(state.state, "[green]watching[/green]")


def _sas_label(job: SyncJob, *, compact: bool = False) -> str:
    """Time left on the credential, coloured by urgency."""
    if job.auth == "aad":
        return "[dim]entra[/dim]"
    state = load_state(job.id)
    if not state.sas_expires_at:
        return "[dim]-[/dim]"
    remaining = state.sas_expires_at - time.time()
    if remaining <= 0:
        return "[red]expired[/red]"
    colour = "yellow" if remaining < job.sas_min_remaining else "green"
    render = compact_duration if compact else human_duration
    return f"[{colour}]{render(remaining)}[/{colour}]"


def _sync_options(fn):
    for decorator in reversed(
        [
            click.option("--name", help="Explicit id instead of a derived slug."),
            click.option(
                "--auth",
                type=click.Choice(AUTH_KINDS),
                default=None,
                help="Credential source. 'aad' uses your Entra login (no SAS to "
                "rotate); 'az' mints a user-delegation SAS; the rest read one "
                "from an external provider.",
            ),
            click.option("--sas-env", help="[--auth env] environment variable name."),
            click.option(
                "--sas-file",
                type=click.Path(),
                help="[--auth file] file re-read on every refresh.",
            ),
            click.option("--sas-command", help="[--auth exec] command printing a SAS."),
            click.option("--sas-url", help="[--auth http] endpoint returning a SAS."),
            click.option(
                "--sas-header",
                multiple=True,
                help="[--auth http] extra request header 'Key: Value' (repeatable).",
            ),
            click.option(
                "--sas-ttl-hours",
                type=int,
                default=DEFAULT_SAS_TTL_HOURS,
                show_default=True,
                help="[--auth az] lifetime of each minted SAS.",
            ),
            click.option(
                "--sas-min-remaining",
                type=float,
                default=DEFAULT_SAS_MIN_REMAINING,
                show_default=True,
                help="Refresh before a transfer if less than this many seconds left.",
            ),
            click.option(
                "--quiet-period",
                type=float,
                default=DEFAULT_QUIET_PERIOD,
                show_default=True,
                help="Wait for writes to settle this long before transferring.",
            ),
            click.option(
                "--batch-files",
                type=int,
                default=DEFAULT_BATCH_FILES,
                show_default=True,
                help="Transfer immediately once this many files changed.",
            ),
            click.option(
                "--batch-bytes",
                type=int,
                default=DEFAULT_BATCH_BYTES,
                show_default=True,
                help="Transfer immediately once this many bytes changed.",
            ),
            click.option(
                "--max-delay",
                type=float,
                default=DEFAULT_MAX_DELAY,
                show_default=True,
                help="Upper bound on staleness while the tree keeps changing.",
            ),
            click.option(
                "--interval",
                type=float,
                default=DEFAULT_INTERVAL,
                show_default=True,
                help="Reconcile this often even with no detected changes.",
            ),
            click.option(
                "--min-gap",
                type=float,
                default=DEFAULT_MIN_GAP,
                show_default=True,
                help="Minimum idle time between two transfers.",
            ),
            click.option(
                "--min-files",
                type=int,
                default=DEFAULT_MIN_FILES,
                show_default=True,
                help="Don't transfer for fewer changes than this (until --max-delay).",
            ),
            click.option(
                "--watch-mode",
                type=click.Choice(WATCH_MODES),
                default="auto",
                show_default=True,
                help="'auto' prefers inotify and falls back to polling.",
            ),
            click.option(
                "--poll-interval",
                type=float,
                default=DEFAULT_POLL_INTERVAL,
                show_default=True,
                help="Polling watcher scan interval.",
            ),
            click.option(
                "-e",
                "--exclude",
                "excludes",
                multiple=True,
                help="Extra exclude pattern (repeatable).",
            ),
            click.option(
                "--no-default-excludes",
                is_flag=True,
                help="Drop the built-in .git/node_modules/... exclude list.",
            ),
            click.option(
                "--delete",
                is_flag=True,
                help="Mirror mode: delete blobs missing from the source.",
            ),
            click.option(
                "--compare-hash",
                is_flag=True,
                help="Compare MD5 instead of last-modified time (slower, exact).",
            ),
            click.option("--cap-mbps", type=float, help="Throttle the transfer rate."),
            click.option("--block-size-mb", type=float, help="azcopy block size."),
        ]
    ):
        fn = decorator(fn)
    return fn


def _job_from_options(source: Path, dest: str, opts: dict) -> SyncJob:
    auth = opts.get("auth")
    spec = None
    provided = [
        ("env", opts.get("sas_env")),
        ("file", opts.get("sas_file")),
        ("exec", opts.get("sas_command")),
        ("http", opts.get("sas_url")),
    ]
    given = [(kind, value) for kind, value in provided if value]
    if len(given) > 1:
        raise click.ClickException(
            "pick one SAS source: "
            + ", ".join(f"--sas-{k if k != 'exec' else 'command'}" for k, _ in given)
        )
    if given:
        inferred, spec = given[0]
        if auth and auth != inferred:
            raise click.ClickException(
                f"--auth {auth} conflicts with the --sas-* flag you passed."
            )
        auth = inferred
    if auth is None:
        # Honour a SAS already pasted into the URL, else mint one with az.
        auth = "inline" if has_sas(dest) else "az"
    if auth in ("env", "file", "exec", "http") and not spec:
        flag = {"env": "--sas-env", "file": "--sas-file", "exec": "--sas-command"}.get(
            auth, "--sas-url"
        )
        raise click.ClickException(f"--auth {auth} needs {flag}.")

    return SyncJob(
        id="",
        source=str(source),
        dest=dest if auth == "inline" else split_sas(dest)[0],
        auth=auth,
        sas_spec=spec,
        sas_headers=list(opts.get("sas_header") or []),
        sas_ttl_hours=opts["sas_ttl_hours"],
        sas_min_remaining=opts["sas_min_remaining"],
        quiet_period=opts["quiet_period"],
        batch_files=opts["batch_files"],
        batch_bytes=opts["batch_bytes"],
        max_delay=opts["max_delay"],
        interval=opts["interval"],
        min_gap=opts["min_gap"],
        min_files=opts["min_files"],
        watch_mode=opts["watch_mode"],
        poll_interval=opts["poll_interval"],
        excludes=list(opts.get("excludes") or []),
        default_excludes=not opts["no_default_excludes"],
        delete_destination=opts["delete"],
        compare_hash=opts["compare_hash"],
        cap_mbps=opts.get("cap_mbps"),
        block_size_mb=opts.get("block_size_mb"),
    )


@click.group(
    help="Persistent local → Azure Blob sync (a watching, batching azcopy).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def cli() -> None:
    pass


@cli.command("add", short_help="Watch a directory and keep it synced.")
@click.argument("source", type=click.Path(exists=True, file_okay=False))
@click.argument("dest")
@click.option("--no-start", is_flag=True, help="Define it but don't start the daemon.")
@_sync_options
def cmd_add(source, dest, no_start, **opts):
    src = Path(source).resolve()
    resolved = resolve_destination(dest)
    job = _job_from_options(src, resolved, opts)
    job.id = make_job_id(src, resolved, opts.get("name"))
    if _def_path(job.id).exists():
        raise click.ClickException(f"{job.id} already exists; pick --name.")

    # Fail fast on a bad credential setup rather than in the daemon's log.
    try:
        job_provider(job)
    except SasError as exc:
        raise click.ClickException(str(exc)) from exc
    if job.delete_destination:
        console.print(
            "[yellow]![/yellow] --delete will remove blobs that are missing "
            "locally, on every sync."
        )
    save_job(job)
    save_state(job.id, RuntimeState())
    console.print(f"[green]✓[/green] {job.id}: {job.route()}")
    if no_start:
        console.print(f"[dim]Start it with: usm azsync start {job.id}[/dim]")
        return
    _start_job(job)


def _start_job(job: SyncJob) -> None:
    if is_running(job.id):
        raise click.ClickException(f"{job.id} is already running.")
    if SERVICE.enabled_kind(job.id):
        proc = SERVICE.start(job.id)
        if proc is not None and proc.returncode != 0:
            raise click.ClickException(proc.stderr.strip() or "service start failed")
        console.print(f"[green]✓[/green] Started {job.id} (service).")
        return
    pid = spawn_daemon(job)
    time.sleep(STARTUP_GRACE_SECS)
    state = load_state(job.id)
    if not pid_alive(state.supervisor_pid or pid):
        tail = _tail(log_path(job.id), 12)
        raise click.ClickException(
            f"{job.id} exited during startup.\n" + "\n".join(tail)
        )
    console.print(
        f"[green]✓[/green] Watching {job.source} "
        f"[dim](pid {state.supervisor_pid or pid}, {state.watch_backend or '?'})[/dim]"
    )


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


@cli.command("ls", short_help="List syncs.")
def cmd_ls():
    jobs = list_jobs()
    if not jobs:
        console.print("[dim]No syncs defined. Add one with `usm azsync add`.[/dim]")
        return

    width = console.width or 100
    # Drop columns as the terminal narrows rather than shrinking every one of
    # them into uselessness. `status` always shows the full picture.
    show_source = width >= 110
    show_boot = width >= 90

    columns = [("ID", {"min_width": 6})]
    if show_source:
        columns.append(("Source", {"min_width": 10, "max_width": 26}))
    columns += [
        # The only column allowed to take whatever is left over.
        ("Destination", {"min_width": 14, "ratio": 1}),
        ("Status", {"min_width": 8}),
        ("Pending", {"justify": "right", "min_width": 10}),
        ("Last sync", {"min_width": 9}),
        ("SAS", {"justify": "right", "min_width": 4}),
    ]
    if show_boot:
        columns.append(("Boot", {"justify": "center", "min_width": 4}))
    table = new_table(*columns, expand=True)

    now = time.time()
    for job in jobs:
        state = load_state(job.id)
        pending = (
            f"{state.pending_files} / {human_bytes(state.pending_bytes)}"
            if state.pending_files
            else "[dim]-[/dim]"
        )
        if state.last_sync_end:
            result = state.last_result or "?"
            colour = {"ok": "green", "partial": "yellow"}.get(result, "red")
            last = (
                f"[{colour}]{result}[/{colour}] "
                f"[dim]{compact_duration(now - state.last_sync_end)}[/dim]"
            )
        else:
            last = "[dim]never[/dim]"
        row = [job.id]
        if show_source:
            row.append(shorten_path(job.source))
        row += [
            short_blob_target(job.dest),
            _state_label(job),
            pending,
            last,
            _sas_label(job, compact=True),
        ]
        if show_boot:
            row.append(
                "[cyan]✓[/cyan]" if SERVICE.enabled_kind(job.id) else "[dim]-[/dim]"
            )
        table.add_row(*row)
    console.print(table)


def _trigger_summary(job: SyncJob) -> str:
    return (
        f"quiet {compact_duration(job.quiet_period)} · "
        f"batch {job.batch_files} files / {human_bytes(job.batch_bytes)} · "
        f"max-delay {compact_duration(job.max_delay)} · "
        f"heartbeat {compact_duration(job.interval)} · "
        f"min-gap {compact_duration(job.min_gap)}"
    )


def _exclude_summary(job: SyncJob) -> str:
    patterns = job.exclude_spec().patterns
    if not patterns:
        return "none"
    shown = ", ".join(patterns[:6])
    rest = len(patterns) - 6
    return f"{len(patterns)} patterns: {shown}" + (
        f", +{rest} more" if rest > 0 else ""
    )


@cli.command("status", short_help="Show one sync in detail.")
@click.argument("job_id")
@click.option("-n", "--history", type=int, default=5, show_default=True)
def cmd_status(job_id, history):
    job = _require_job(job_id)
    state = load_state(job.id)
    now = time.time()

    last_sync = "never"
    if state.last_sync_end:
        last_sync = f"{state.last_result or '-'}, {human_duration(now - state.last_sync_end)} ago"
        if state.last_duration:
            last_sync += f" (took {human_duration(state.last_duration)})"

    rows = [
        ("source", shorten_path(job.source)),
        ("destination", short_blob_target(job.dest)),
        SECTION,
        ("status", _state_label(job)),
        ("watcher", state.watch_backend or f"{job.watch_mode} (not started)"),
        (
            "uptime",
            human_duration(now - state.started_at) if state.started_at else "-",
        ),
        (
            "pending",
            f"{state.pending_files} files / {human_bytes(state.pending_bytes)}"
            if state.pending_files
            else "nothing queued",
        ),
        ("last sync", last_sync),
        ("syncs / failures", f"{state.total_syncs} / {state.total_failures}"),
        (
            "next wake",
            f"in {human_duration(state.next_deadline - now)}"
            if state.next_deadline and state.next_deadline > now
            else "-",
        ),
        SECTION,
        (
            "credential",
            job.auth
            + (f" ({job.sas_spec})" if job.sas_spec else "")
            + (
                f" · expires in {_sas_label(job)}"
                if job.auth != "aad"
                else " · nothing to rotate"
            ),
        ),
        ("triggers", _trigger_summary(job)),
        ("excludes", _exclude_summary(job)),
        ("delete destination", "yes" if job.delete_destination else "no"),
        ("boot", SERVICE.enabled_kind(job.id) or "-"),
    ]
    console.print(kv_table(rows))

    if state.last_error:
        console.print(f"\n[red]last error[/red]  {redact(state.last_error)[:600]}")

    records = read_history(job.id, history)
    if records:
        console.print("\n[bold]Recent syncs[/bold]")
        hist = new_table(
            "when",
            "reason",
            "result",
            ("took", {"justify": "right"}),
            ("files", {"justify": "right"}),
            ("bytes", {"justify": "right"}),
        )
        for record in reversed(records):
            status = record.get("status", "-")
            colour = {"ok": "green", "partial": "yellow"}.get(status, "red")
            hist.add_row(
                f"{compact_duration(now - record.get('at', now))} ago",
                record.get("reason", "-"),
                f"[{colour}]{status}[/{colour}]",
                human_duration(record.get("duration")),
                str(record.get("completed", 0)),
                human_bytes(record.get("bytes", 0)),
            )
        console.print(hist)


@cli.command("sync", short_help="Transfer now, skipping the debounce.")
@click.argument("job_id")
@click.option("--wait", is_flag=True, help="Run in the foreground and wait for it.")
def cmd_sync(job_id, wait):
    job = _require_job(job_id)
    if is_running(job.id) and not wait:
        if poke_daemon(job.id):
            console.print(f"[green]✓[/green] Asked {job.id} to sync now.")
        else:
            console.print(f"[yellow]![/yellow] {job.id} did not acknowledge.")
        return
    if is_running(job.id):
        raise click.ClickException(
            f"{job.id} is running; use `usm azsync sync {job.id}` without --wait."
        )
    _run_once(job, reason="manual")


def _run_once(job: SyncJob, *, reason: str) -> None:
    lock = FileLock(lock_path(job.id))
    if not lock.acquire():
        raise click.ClickException(f"{job.id} is busy (another sync holds the lock).")
    try:
        supervisor = Supervisor(job, log=lambda m: console.print(f"[dim]{m}[/dim]"))
        result = supervisor.run_sync(reason)
    finally:
        lock.release()
    if result.ok:
        console.print(f"[green]✓[/green] {result.summary()}")
        return
    if result.status == PARTIAL:
        console.print(f"[yellow]![/yellow] {result.summary()}")
        return
    raise click.ClickException(
        f"sync {result.status}: {redact(result.error or 'no detail')[:500]}"
    )


@cli.command("once", short_help="One-off sync without defining anything.")
@click.argument("source", type=click.Path(exists=True, file_okay=False))
@click.argument("dest")
@_sync_options
def cmd_once(source, dest, **opts):
    src = Path(source).resolve()
    job = _job_from_options(src, resolve_destination(dest), opts)
    job.id = "once-" + slugify(src.name or "root")
    _run_once(job, reason="once")


@cli.command("start", short_help="Start a stopped sync.")
@click.argument("job_id")
def cmd_start(job_id):
    _start_job(_require_job(job_id))


@cli.command("stop", short_help="Stop a sync (keeps the definition).")
@click.argument("job_id")
def cmd_stop(job_id):
    targets = list_jobs() if job_id == "all" else [_require_job(job_id)]
    for job in targets:
        stopped = stop_daemon(job.id)
        console.print(
            f"[green]✓[/green] {job.id}: {'stopped' if stopped else 'already stopped'}"
        )


@cli.command("restart", short_help="Restart a sync.")
@click.argument("job_id")
def cmd_restart(job_id):
    job = _require_job(job_id)
    stop_daemon(job.id)
    _start_job(job)


@cli.command("rm", short_help="Delete a sync definition.")
@click.argument("job_id")
@click.option("-y", "--yes", is_flag=True, help="Don't ask.")
def cmd_rm(job_id, yes):
    targets = list_jobs() if job_id == "all" else [_require_job(job_id)]
    for job in targets:
        if not yes and not click.confirm(f"Delete {job.id} ({job.source})?"):
            continue
        if SERVICE.enabled_kind(job.id):
            _disable(job)
        stop_daemon(job.id)
        delete_job(job.id)
        console.print(f"[green]✓[/green] Removed {job.id}.")


@cli.command("enable", short_help="Start this sync at boot.")
@click.argument("job_id")
def cmd_enable(job_id):
    job = _require_job(job_id)
    binary = usm_bin()
    try:
        kind = SERVICE.enable(
            job.id,
            [binary, "azsync", "up", job.id],
            description=f"usm azsync {job.id}: {job.source} -> blob",
            binary=binary,
            log_path=log_path(job.id),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if kind == "systemd":
        console.print(
            f"[green]✓[/green] {job.id} starts at boot (systemd user unit).\n"
            "[dim]Tip: `loginctl enable-linger $USER` keeps it running when "
            "you log out.[/dim]"
        )
    else:
        console.print(f"[green]✓[/green] {job.id} starts at login (launchd agent).")


def _disable(job: SyncJob) -> None:
    SERVICE.disable(job.id)


@cli.command("disable", short_help="Don't start this sync at boot.")
@click.argument("job_id")
def cmd_disable(job_id):
    job = _require_job(job_id)
    if not SERVICE.enabled_kind(job.id):
        console.print(f"[dim]{job.id} was not enabled.[/dim]")
        return
    _disable(job)
    console.print(f"[green]✓[/green] {job.id} no longer starts at boot.")


@cli.command("logs", short_help="Tail the sync log.")
@click.argument("job_id")
@click.option("-n", "--lines", type=int, default=40, show_default=True)
@click.option("-f", "--follow", is_flag=True)
@click.option("--azcopy", is_flag=True, help="Show azcopy's own log instead.")
def cmd_logs(job_id, lines, follow, azcopy):
    job = _require_job(job_id)
    if azcopy:
        log_dir = azcopy_dir(job.id) / "log"
        candidates = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise click.ClickException(f"no azcopy logs yet under {log_dir}")
        path = candidates[-1]
    else:
        path = log_path(job.id)
    if not path.exists():
        raise click.ClickException(f"no log at {path}")
    for line in _tail(path, lines):
        click.echo(redact(line))
    if not follow:
        return
    with open(path, errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                line = fh.readline()
                if line:
                    click.echo(redact(line.rstrip()))
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:  # pragma: no cover
            pass


@cli.command("token", short_help="Inspect or rotate the SAS.")
@click.argument("job_id")
@click.option("--refresh", is_flag=True, help="Force a new token now.")
def cmd_token(job_id, refresh):
    job = _require_job(job_id)
    try:
        manager = job_sas_manager(job)
    except SasError as exc:
        raise click.ClickException(str(exc)) from exc
    if not manager.enabled:
        console.print("[dim]auth=aad: azcopy uses your Entra login, no SAS.[/dim]")
        return
    try:
        token = manager.ensure(time.time(), force=refresh)
    except SasError as exc:
        raise click.ClickException(str(exc)) from exc
    remaining = token.remaining(time.time())
    console.print(f"source:    {token.source}")
    console.print(f"token:     {token.redacted()[:120]}")
    console.print(
        f"expires:   {human_duration(remaining)}" if remaining else "expires:   unknown"
    )
    if refresh:
        console.print("[green]✓[/green] Rotated.")


@cli.command("dry-run", short_help="Show what a sync would transfer.")
@click.argument("job_id")
def cmd_dry_run(job_id):
    job = _require_job(job_id)
    engine = AzcopyEngine(job)
    manager = job_sas_manager(job)
    token = manager.ensure(time.time())
    argv = engine.build_argv(token.token or None, dry_run=True)
    console.print(f"[dim]{redact(shlex.join(argv))}[/dim]")
    result = engine.run(argv)
    if result.ok:
        console.print(f"[green]✓[/green] {result.summary()}")
    else:
        raise click.ClickException(redact(result.error or result.status))


@cli.command("up", short_help="Run the supervisor in the foreground.")
@click.argument("job_id")
def cmd_up(job_id):
    _require_job(job_id)
    raise SystemExit(run_supervisor(job_id))


def main() -> None:
    # systemd/launchd and the detached spawn both re-enter through `up`.
    supervise_id = os.environ.get(SUPERVISE_ENV)
    if supervise_id and len(sys.argv) == 1:  # pragma: no cover - spawn path
        raise SystemExit(run_supervisor(supervise_id))
    cli()


if __name__ == "__main__":
    main()
