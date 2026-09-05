"""Shared hermetic azsync fixtures and azcopy fakes."""

from __future__ import annotations
import json
import time
from pathlib import Path
import pytest
import azsync
import usm_azure
from usm_azure import (
    SasManager,
)
from azsync import (
    OK,
    AzcopyEngine,
    Supervisor,
    SyncJob,
)


def make_checkpoint(
    job: SyncJob, name: str = "checkpoint-100", *, marker: bool = True
) -> Path:
    root = Path(job.source) / "checkpoints" / name
    root.mkdir(parents=True)
    (root / "model.bin").write_bytes(b"weights")
    (root / "state.json").write_text("{}")
    if marker:
        (root / ".complete").touch()
    return root


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point every on-disk path at a throwaway directory.

    The env var matters for the spawned-supervisor tests: that child is a
    separate process and can only be redirected through the environment.
    """
    root = tmp_path / "azsync"
    root.mkdir()
    monkeypatch.setattr(azsync, "STATE_DIR", root)
    monkeypatch.setenv("USM_AZSYNC_STATE_DIR", str(root))
    return root


def make_job(tmp_path, **overrides) -> SyncJob:
    source = overrides.pop("source", None) or tmp_path / "src"
    Path(source).mkdir(parents=True, exist_ok=True)
    job = SyncJob(
        id=overrides.pop("id", "job1"),
        source=str(source),
        dest=overrides.pop("dest", "https://acct.blob.core.windows.net/bucket/path"),
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def sas_for(expires_in: float, *, now: float | None = None) -> str:
    import datetime

    now = now if now is not None else time.time()
    stamp = datetime.datetime.fromtimestamp(
        now + expires_in, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"sv=2021-08-06&se={stamp}&sp=rwdl&sig=ABC123"


class _StubProvider(usm_azure.SasProvider):
    kind = "stub"

    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.calls = 0

    def fetch(self):
        self.calls += 1
        value = self.tokens[min(self.calls - 1, len(self.tokens) - 1)]
        if isinstance(value, Exception):
            raise value
        return value, None


def envelope(kind: str, content) -> str:
    if not isinstance(content, str):
        content = json.dumps(content)
    return json.dumps({"MessageType": kind, "MessageContent": content})


FAKE_AZCOPY = '''#!/usr/bin/env python3
"""Replay a scripted azcopy response. Driven by $FAKE_PLAN (a JSON file)."""
import json, os, sys, time

plan_path = os.environ["FAKE_PLAN"]
with open(plan_path) as fh:
    plan = json.load(fh)

calls_path = plan_path + ".calls"
calls = []
if os.path.exists(calls_path):
    with open(calls_path) as fh:
        calls = json.load(fh)
calls.append(sys.argv[1:])
with open(calls_path, "w") as fh:
    json.dump(calls, fh)

steps = plan["steps"]
step = steps[min(len(calls) - 1, len(steps) - 1)]
time.sleep(step.get("sleep", 0))
for line in step.get("lines", []):
    print(json.dumps(line), flush=True)
sys.exit(step.get("exit", 0))
'''


@pytest.fixture
def fake_azcopy(tmp_path, monkeypatch):
    """Install a scripted azcopy and return a helper to program it."""
    binary = tmp_path / "fake-azcopy"
    binary.write_text(FAKE_AZCOPY)
    binary.chmod(0o755)
    plan_path = tmp_path / "plan.json"
    monkeypatch.setenv("USM_AZCOPY_BIN", str(binary))
    monkeypatch.setenv("FAKE_PLAN", str(plan_path))

    class Fake:
        path = str(binary)

        def program(self, *steps):
            plan_path.write_text(json.dumps({"steps": list(steps)}))

        @property
        def calls(self):
            calls_file = Path(str(plan_path) + ".calls")
            if not calls_file.exists():
                return []
            return json.loads(calls_file.read_text())

    fake = Fake()
    fake.program({"lines": [], "exit": 0})
    return fake


def ok_step(completed=2, size=2048, job_id="job-1", **extra):
    summary = {
        "JobID": job_id,
        "TransfersCompleted": completed,
        "TransfersFailed": 0,
        "TransfersSkipped": 0,
        "TotalBytesTransferred": size,
        "JobStatus": "Completed",
    }
    summary.update(extra)
    return {
        "lines": [{"MessageType": "EndOfJob", "MessageContent": json.dumps(summary)}],
        "exit": 0,
    }


def fail_step(message, *, exit_code=1, job_id="job-1", **summary_extra):
    summary = {"JobID": job_id, "ErrorMsg": message, "JobStatus": "Failed"}
    summary.update(summary_extra)
    return {
        "lines": [{"MessageType": "EndOfJob", "MessageContent": json.dumps(summary)}],
        "exit": exit_code,
    }


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


def build_supervisor(tmp_path, fake_azcopy, state_dir, **job_kw):
    job = make_job(tmp_path, **job_kw)
    azsync.save_job(job)
    clock = _Clock()
    provider = _StubProvider([sas_for(7200, now=clock.now)])
    manager = SasManager(
        provider, tmp_path / "cache.sas", min_remaining=job.sas_min_remaining
    )
    supervisor = Supervisor(
        job,
        engine=AzcopyEngine(job, state_dir=tmp_path / "wd"),
        sas=manager,
        clock=clock,
        log=lambda _m: None,
    )
    return supervisor, clock, provider


@pytest.fixture
def runner():
    import click.testing

    return click.testing.CliRunner()


def invoke(runner, args, **kw):
    return runner.invoke(azsync.cli, args, **kw)


def wait_until(predicate, timeout=25.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class FakePublishEngine:
    def __init__(self, job, results=None, on_run=None, remote_marker=None):
        self.job = job
        self.results = list(results or [])
        self.on_run = on_run
        self.remote_marker = remote_marker
        self.calls = []

    def remote_exists(self, remote_relpath, sas):
        self.calls.append(["probe", remote_relpath, sas or ""])
        if isinstance(self.remote_marker, Exception):
            raise self.remote_marker
        return bool(self.remote_marker)

    def remove_remote(self, remote_relpath, sas):
        self.calls.append(["remove", remote_relpath, sas or ""])
        self.remote_marker = False
        return azsync.SyncResult(status=OK)

    def build_publish_argv(self, snapshot, sas, *, marker, dry_run=False):
        return ["payload", snapshot.relpath, marker, sas or "", str(dry_run)]

    def build_exact_copy_argv(self, source, remote_relpath, sas):
        return ["exact", str(source), remote_relpath, sas or ""]

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if self.on_run:
            self.on_run(argv, len(self.calls))
        if self.results:
            return self.results.pop(0)
        if argv[0] == "payload":
            root = Path(self.job.source) / argv[1]
            files = [
                path
                for path in root.rglob("*")
                if path.is_file() and path.name != self.job.ready_marker
            ]
            return azsync.SyncResult(
                status=OK,
                completed=len(files),
                bytes=sum(path.stat().st_size for path in files),
            )
        return azsync.SyncResult(status=OK, completed=1)


def publish_job(tmp_path, **overrides):
    defaults = {
        "publish_paths": ["checkpoints"],
        "publish_patterns": ["checkpoint-*"],
        "publish_unit": "directory",
        "ready_marker": ".complete",
        "publish_stable": 0,
        "publish_keep_last": 0,
    }
    defaults.update(overrides)
    return make_job(tmp_path, **defaults)


class FilesystemPublishEngine(FakePublishEngine):
    """An azcopy-shaped engine that really copies into a local remote tree."""

    def __init__(self, job, remote):
        super().__init__(job)
        self.remote = remote
        self.phases = []

    def remote_exists(self, remote_relpath, sas):
        self.phases.append("probe")
        return (self.remote / remote_relpath).exists()

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "payload":
            self.phases.append("payload")
            rel = argv[1]
            local = Path(self.job.source) / rel
            remote = self.remote / rel
            assert not (remote / self.job.ready_marker).exists()
            for path in local.rglob("*"):
                if path.is_file() and path.name != self.job.ready_marker:
                    target = remote / path.relative_to(local)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
            assert not (remote / self.job.ready_marker).exists()
            files = [p for p in remote.rglob("*") if p.is_file()]
            return azsync.SyncResult(
                status=OK,
                completed=len(files),
                bytes=sum(p.stat().st_size for p in files),
            )
        if argv[0] == "exact":
            source, rel = Path(argv[1]), argv[2]
            target = self.remote / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.name == ".azsync-manifest.json":
                self.phases.append("manifest")
                assert not target.with_name(self.job.ready_marker).exists()
            else:
                self.phases.append("marker")
                assert target.name == self.job.ready_marker
                assert target.with_name(".azsync-manifest.json").exists()
            target.write_bytes(source.read_bytes())
            return azsync.SyncResult(
                status=OK, completed=1, bytes=source.stat().st_size
            )
        raise AssertionError(f"unexpected command: {argv}")
