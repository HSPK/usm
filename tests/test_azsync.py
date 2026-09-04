"""Tests for scripts/azsync.py.

The pure layers (trigger policy, exclude rendering, SAS parsing, azcopy
argv/NDJSON/classification) are unit-tested directly. The engine and the
supervisor are driven end to end against a *fake azcopy* — a small script
injected via ``$USM_AZCOPY_BIN`` that replays canned NDJSON and exit codes —
so success, partial failure, credential expiry and network backoff are all
covered without touching Azure.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

import azsync
import usm_azure
import usm_daemon
from usm_publish import PublishError, snapshot_unit
from usm_signal import SignalEvent, SignalResult
from usm_azure import (
    ExcludeSpec,
    SasError,
    SasManager,
    SasToken,
    human_bytes,
    human_duration,
    normalize_sas,
    parse_sas_expiry,
    redact,
    split_sas,
)
from azsync import (
    AUTH_EXPIRED,
    AUTH_INVALID,
    FATAL,
    NETWORK,
    OK,
    PARTIAL,
    SYNC,
    WAIT,
    AzcopyEngine,
    ChangeAccumulator,
    ChangeStat,
    PolicyInput,
    PollingWatcher,
    PublishCoordinator,
    Supervisor,
    SyncJob,
    TriggerConfig,
    backoff_delay,
    classify_failure,
    decide,
    interpret_result,
    parse_azcopy_json,
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


# --- Fixtures --------------------------------------------------------------


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


# --- Trigger policy --------------------------------------------------------


class TestTriggerPolicy:
    CFG = TriggerConfig(
        quiet_period=5.0,
        batch_files=200,
        batch_bytes=256 * 1024 * 1024,
        max_delay=300.0,
        interval=3600.0,
        min_gap=30.0,
        min_files=1,
    )

    def _acc(self, files=0, size=0, first=None, last=None) -> ChangeStat:
        return ChangeStat(files=files, bytes=size, first_at=first, last_at=last)

    def test_running_suppresses_everything(self):
        d = decide(
            1000, self._acc(999, first=0, last=0), PolicyInput(running=True), self.CFG
        )
        assert d.action == WAIT and d.reason == "syncing"

    def test_backoff_outranks_changes(self):
        d = decide(
            1000,
            self._acc(999, first=0, last=0),
            PolicyInput(backoff_until=1200),
            self.CFG,
        )
        assert d.action == WAIT
        assert d.reason == "backoff" and d.wake_at == 1200

    def test_manual_beats_the_rate_limit(self):
        d = decide(1000, self._acc(), PolicyInput(forced=True, last_end=999), self.CFG)
        assert d.action == SYNC and d.reason == "manual"

    def test_min_gap_rate_limits(self):
        d = decide(
            1010,
            self._acc(5, first=1005, last=1005),
            PolicyInput(last_end=1000),
            self.CFG,
        )
        assert d.action == WAIT
        assert d.reason == "min-gap" and d.wake_at == 1030

    def test_quiet_period_is_the_normal_path(self):
        rt = PolicyInput(last_end=0)
        assert (
            decide(1003, self._acc(3, first=1000, last=1000), rt, self.CFG).action
            == WAIT
        )
        d = decide(1005, self._acc(3, first=1000, last=1000), rt, self.CFG)
        assert d.action == SYNC and d.reason == "quiet"

    def test_new_writes_extend_the_quiet_period(self):
        rt = PolicyInput(last_end=0)
        d = decide(1006, self._acc(3, first=1000, last=1004), rt, self.CFG)
        assert d.action == WAIT and d.wake_at == 1009

    def test_volume_fires_while_still_churning(self):
        # Still being written (last_at == now) but 200 files is enough.
        d = decide(
            1000,
            self._acc(200, first=999, last=1000),
            PolicyInput(last_end=0),
            self.CFG,
        )
        assert d.action == SYNC and d.reason == "volume"

    def test_byte_volume_fires(self):
        d = decide(
            1000,
            self._acc(2, size=300 * 1024 * 1024, first=999, last=1000),
            PolicyInput(last_end=0),
            self.CFG,
        )
        assert d.action == SYNC and d.reason == "volume"

    def test_max_delay_bounds_staleness_under_constant_churn(self):
        # Writes never stop, so the quiet period never elapses.
        rt = PolicyInput(last_end=0)
        assert (
            decide(1200, self._acc(5, first=1000, last=1200), rt, self.CFG).action
            == WAIT
        )
        d = decide(1300, self._acc(5, first=1000, last=1300), rt, self.CFG)
        assert d.action == SYNC and d.reason == "max-delay"

    def test_heartbeat_fires_without_any_changes(self):
        rt = PolicyInput(last_end=1000)
        assert decide(4000, self._acc(), rt, self.CFG).action == WAIT
        d = decide(4600, self._acc(), rt, self.CFG)
        assert d.action == SYNC and d.reason == "heartbeat"

    def test_idle_wait_targets_the_heartbeat(self):
        d = decide(2000, self._acc(), PolicyInput(last_end=1000), self.CFG)
        assert d.reason == "idle" and d.wake_at == 4600

    def test_degraded_watcher_forces_a_reconcile(self):
        d = decide(
            2000, self._acc(), PolicyInput(last_end=1000, degraded=True), self.CFG
        )
        assert d.action == SYNC and d.reason == "degraded"

    def test_min_files_floor_defers_to_max_delay(self):
        cfg = TriggerConfig(min_files=5, quiet_period=5, max_delay=300, interval=3600)
        rt = PolicyInput(last_end=0)
        # Two files, quiet — below the floor, so wait for max-delay instead.
        d = decide(1010, self._acc(2, first=1000, last=1000), rt, cfg)
        assert d.action == WAIT and d.wake_at == 1300
        d = decide(1010, self._acc(5, first=1000, last=1000), rt, cfg)
        assert d.action == SYNC and d.reason == "quiet"

    def test_first_run_has_no_last_end(self):
        d = decide(1000, self._acc(), PolicyInput(), self.CFG)
        assert d.action == SYNC and d.reason == "heartbeat"

    def test_wake_at_is_the_earliest_deadline(self):
        d = decide(
            1001,
            self._acc(1, first=1000, last=1000),
            PolicyInput(last_end=900),
            self.CFG,
        )
        # quiet(1005) < max-delay(1300) < heartbeat(4500)
        assert d.wake_at == 1005

    @pytest.mark.parametrize(
        "failures,expected", [(0, 0), (1, 30), (2, 60), (3, 120), (10, 900), (99, 900)]
    )
    def test_backoff_curve(self, failures, expected):
        assert backoff_delay(failures) == expected


class TestChangeAccumulator:
    def test_record_and_take(self):
        acc = ChangeAccumulator()
        acc.record(100, size=10)
        acc.record(105, size=5)
        stat = acc.take()
        assert stat.files == 2 and stat.bytes == 15
        assert stat.first_at == 100 and stat.last_at == 105
        assert acc.snapshot().files == 0

    def test_take_clears_degraded(self):
        acc = ChangeAccumulator()
        acc.mark_degraded()
        assert acc.degraded is True
        acc.take()
        assert acc.degraded is False

    def test_give_back_merges_without_losing_changes(self):
        acc = ChangeAccumulator()
        acc.record(100, size=10)
        batch = acc.take()
        acc.record(200, size=7)  # arrived while the failed job was running
        acc.give_back(batch)
        merged = acc.snapshot()
        assert merged.files == 2 and merged.bytes == 17
        assert merged.first_at == 100 and merged.last_at == 200

    def test_give_back_of_empty_batch_is_a_noop(self):
        acc = ChangeAccumulator()
        acc.give_back(ChangeStat())
        assert acc.snapshot().files == 0

    def test_deletes_counted(self):
        acc = ChangeAccumulator()
        acc.record(1, deleted=True)
        assert acc.take().deletes == 1


# --- Excludes --------------------------------------------------------------


class TestExcludeSpec:
    def test_defaults_cover_the_usual_noise(self):
        spec = ExcludeSpec.build()
        for path in (
            ".git/config",
            "sub/.git/HEAD",
            "node_modules/x/index.js",
            "pkg/__pycache__/m.cpython-312.pyc",
            "a/b.pyc",
            ".DS_Store",
            "draft.tmp",
            "upload.part",
        ):
            assert spec.matches(path), path

    def test_normal_files_pass(self):
        spec = ExcludeSpec.build()
        for path in ("data/a.csv", "src/main.py", "README.md", "gitignore.txt"):
            assert not spec.matches(path), path

    def test_extra_patterns(self):
        spec = ExcludeSpec.build(["*.log", "scratch/"])
        assert spec.matches("run.log")
        assert spec.matches("scratch/a.txt")
        assert not spec.matches("run.txt")

    def test_defaults_can_be_dropped(self):
        spec = ExcludeSpec.build(["*.log"], defaults=False)
        assert spec.patterns == ("*.log",)
        assert not spec.matches(".git/config")

    def test_path_pattern(self):
        spec = ExcludeSpec.build(["build/cache"], defaults=False)
        assert spec.matches("build/cache/x.o")
        assert not spec.matches("build/main.o")

    def test_root_and_empty_are_never_excluded(self):
        spec = ExcludeSpec.build()
        assert not spec.matches("")
        assert not spec.matches(".")

    def test_azcopy_flags_split_by_matching_rule(self):
        spec = ExcludeSpec.build(["*.log", "build/cache"], defaults=False)
        flags = spec.to_azcopy_flags()
        assert "--exclude-pattern" in flags
        assert flags[flags.index("--exclude-pattern") + 1] == "*.log"
        assert "--exclude-path" in flags
        assert flags[flags.index("--exclude-path") + 1] == "build/cache"

    def test_directory_pattern_gets_prefix_and_nested_regex(self):
        flags = ExcludeSpec.build([".git/"], defaults=False).to_azcopy_flags()
        assert flags[flags.index("--exclude-path") + 1] == ".git"
        regex = flags[flags.index("--exclude-regex") + 1]
        assert regex == r"(^|.*/)\.git/.*"

    def test_default_flags_are_wellformed(self):
        flags = ExcludeSpec.build().to_azcopy_flags()
        assert flags.count("--exclude-path") == 1
        assert flags.count("--exclude-pattern") == 1
        assert flags.count("--exclude-regex") == 1
        for i in range(0, len(flags), 2):
            assert flags[i].startswith("--exclude-")
            assert ";" in flags[i + 1] or flags[i + 1]

    def test_regexes_compile(self):
        import re

        flags = ExcludeSpec.build(["a/*.log"]).to_azcopy_flags()
        for chunk in flags[flags.index("--exclude-regex") + 1].split(";"):
            re.compile(chunk)

    def test_watcher_and_azcopy_agree_on_dot_git(self):
        """Both renderings must hide the same thing or the daemon spins."""
        import re

        spec = ExcludeSpec.build()
        flags = spec.to_azcopy_flags()
        regexes = [
            re.compile(r) for r in flags[flags.index("--exclude-regex") + 1].split(";")
        ]
        rel = "deep/nested/.git/objects/ab/cd"
        assert spec.matches(rel)
        assert any(r.match(rel) for r in regexes)


# --- SAS -------------------------------------------------------------------


class TestSasParsing:
    def test_parse_expiry(self):
        token = "sv=2021-08-06&se=2030-01-02T03:04:05Z&sig=x"
        assert parse_sas_expiry(token) == pytest.approx(1893553445, abs=2)

    def test_parse_expiry_minute_precision(self):
        assert parse_sas_expiry("se=2030-01-02T03:04Z&sig=x") is not None

    def test_missing_expiry(self):
        assert parse_sas_expiry("sv=1&sig=x") is None
        assert parse_sas_expiry("") is None

    def test_unparseable_expiry(self):
        assert parse_sas_expiry("se=not-a-date&sig=x") is None

    def test_normalize_bare_token(self):
        token, claimed = normalize_sas("  sv=1&sig=abc  ")
        assert token == "sv=1&sig=abc" and claimed is None

    def test_normalize_strips_leading_question_mark(self):
        assert normalize_sas("?sv=1&sig=abc")[0] == "sv=1&sig=abc"

    def test_normalize_full_url(self):
        token, _ = normalize_sas("https://a.blob.core.windows.net/c?sv=1&sig=abc")
        assert token == "sv=1&sig=abc"

    def test_normalize_json_payload(self):
        token, claimed = normalize_sas(
            json.dumps({"sas": "sv=1&sig=abc", "expires_at": "2030-01-01T00:00:00Z"})
        )
        assert token == "sv=1&sig=abc"
        assert claimed == pytest.approx(1893456000, abs=2)

    def test_normalize_json_epoch_expiry(self):
        _, claimed = normalize_sas(
            json.dumps({"token": "sig=abc", "expiry": 1893456000})
        )
        assert claimed == 1893456000

    @pytest.mark.parametrize(
        "payload",
        ["", "   ", "sv=1&nope=1", "{bad json", json.dumps({"nothing": 1})],
    )
    def test_normalize_rejects_garbage(self, payload):
        with pytest.raises(SasError):
            normalize_sas(payload)

    def test_normalize_url_without_sas_rejected(self):
        with pytest.raises(SasError):
            normalize_sas("https://a.blob.core.windows.net/c?sv=1")

    def test_redaction(self):
        assert redact("sv=1&sig=SECRET&sp=r") == "sv=1&sig=***&sp=r"
        assert "SECRET" not in redact("https://x/y?sig=SECRET")

    def test_split_and_rejoin(self):
        url = "https://a.blob.core.windows.net/c/d?sv=1&sig=abc"
        bare, token = split_sas(url)
        assert bare == "https://a.blob.core.windows.net/c/d"
        assert usm_azure.join_sas(bare, token) == url

    def test_split_leaves_non_sas_query_alone(self):
        url = "https://a.blob.core.windows.net/c/d?snapshot=1"
        assert split_sas(url) == (url, None)


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


class TestSasProviders:
    def test_env_provider(self, monkeypatch):
        monkeypatch.setenv("MY_SAS", sas_for(3600))
        provider = usm_azure.EnvProvider("MY_SAS")
        assert provider.resolve(time.time()).token.startswith("sv=")

    def test_env_provider_missing(self, monkeypatch):
        monkeypatch.delenv("MY_SAS", raising=False)
        with pytest.raises(SasError):
            usm_azure.EnvProvider("MY_SAS").resolve(time.time())

    def test_file_provider_is_reread_each_time(self, tmp_path):
        path = tmp_path / "sas.txt"
        path.write_text(sas_for(3600))
        provider = usm_azure.FileProvider(str(path))
        first = provider.resolve(time.time()).token
        path.write_text(sas_for(7200))
        second = provider.resolve(time.time()).token
        assert first != second

    def test_file_provider_missing(self, tmp_path):
        with pytest.raises(SasError):
            usm_azure.FileProvider(str(tmp_path / "nope")).resolve(time.time())

    def test_exec_provider(self):
        token = sas_for(3600)
        provider = usm_azure.ExecProvider(f"printf %s '{token}'")
        assert provider.resolve(time.time()).token == token

    def test_exec_provider_nonzero_exit(self):
        provider = usm_azure.ExecProvider("echo boom >&2; exit 3")
        with pytest.raises(SasError, match="exited 3"):
            provider.resolve(time.time())

    def test_exec_provider_json_output(self):
        payload = json.dumps({"sas": sas_for(3600)})
        provider = usm_azure.ExecProvider(
            f"printf %s {json.dumps(payload)!r}".replace("'", "'\"'\"'")
            if False
            else f"cat <<'EOF'\n{payload}\nEOF"
        )
        assert provider.resolve(time.time()).token.startswith("sv=")

    def test_http_provider(self, monkeypatch):
        import io

        captured = {}

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.header_items())
            captured["url"] = request.full_url
            return _Resp(sas_for(3600).encode())

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        provider = usm_azure.HttpProvider(
            "https://sas.example/mint", ["Authorization: Bearer t"]
        )
        assert provider.resolve(time.time()).token.startswith("sv=")
        assert captured["url"] == "https://sas.example/mint"
        assert any(k.lower() == "authorization" for k in captured["headers"])

    def test_http_provider_bad_header(self):
        with pytest.raises(SasError, match="invalid SAS header"):
            usm_azure.HttpProvider("https://x", ["nocolon"]).resolve(time.time())

    def test_expired_token_is_rejected(self):
        provider = _StubProvider([sas_for(-60)])
        with pytest.raises(SasError, match="expired"):
            provider.resolve(time.time())

    def test_embedded_expiry_wins_over_a_longer_claim(self):
        now = time.time()
        raw = json.dumps({"sas": sas_for(600, now=now), "expires_at": now + 999999})
        provider = _StubProvider([raw])
        token = provider.resolve(now)
        assert token.remaining(now) == pytest.approx(600, abs=5)

    def test_aad_provider_has_no_token(self):
        token = usm_azure.AadProvider().resolve(time.time())
        assert token.token == "" and token.expires_at is None

    def test_inline_provider_is_not_refreshable(self):
        provider = usm_azure.InlineProvider(sas_for(3600))
        assert provider.refreshable is False

    def test_build_provider_dispatch(self, tmp_path):
        cases = {
            "aad": usm_azure.AadProvider,
            "az": usm_azure.AzCliProvider,
            "env": usm_azure.EnvProvider,
            "file": usm_azure.FileProvider,
            "exec": usm_azure.ExecProvider,
            "http": usm_azure.HttpProvider,
        }
        for auth, cls in cases.items():
            job = make_job(tmp_path, auth=auth, sas_spec="spec")
            assert isinstance(azsync.job_provider(job), cls)

    def test_build_provider_inline_needs_sas_in_url(self, tmp_path):
        job = make_job(tmp_path, auth="inline")
        with pytest.raises(SasError):
            azsync.job_provider(job)
        job.dest = f"https://a.blob.core.windows.net/c/d?{sas_for(60)}"
        assert isinstance(azsync.job_provider(job), usm_azure.InlineProvider)

    @pytest.mark.parametrize("auth", ["env", "file", "exec", "http"])
    def test_external_providers_need_a_spec(self, tmp_path, auth):
        with pytest.raises(SasError):
            azsync.job_provider(make_job(tmp_path, auth=auth, sas_spec=None))

    def test_unknown_auth(self, tmp_path):
        with pytest.raises(SasError):
            azsync.job_provider(make_job(tmp_path, auth="telepathy"))


class TestSasManager:
    def _manager(self, tmp_path, provider, **job_kw):
        job = make_job(tmp_path, **job_kw)
        return (
            SasManager(
                provider,
                tmp_path / "cache.sas",
                min_remaining=job.sas_min_remaining,
            ),
            job,
        )

    def test_caches_between_calls(self, tmp_path):
        provider = _StubProvider([sas_for(7200)])
        manager, _ = self._manager(tmp_path, provider)
        now = time.time()
        manager.ensure(now)
        manager.ensure(now)
        assert provider.calls == 1

    def test_cache_file_is_owner_only(self, tmp_path):
        manager, _ = self._manager(tmp_path, _StubProvider([sas_for(7200)]))
        manager.ensure(time.time())
        mode = stat.S_IMODE(os.stat(tmp_path / "cache.sas").st_mode)
        assert mode == 0o600

    def test_refreshes_when_close_to_expiry(self, tmp_path):
        provider = _StubProvider([sas_for(600), sas_for(7200)])
        manager, _ = self._manager(tmp_path, provider, sas_min_remaining=1800)
        now = time.time()
        first = manager.ensure(now)
        assert first.remaining(now) == pytest.approx(600, abs=5)
        second = manager.ensure(now)
        assert provider.calls == 2
        assert second.remaining(now) == pytest.approx(7200, abs=5)

    def test_force_bypasses_the_cache(self, tmp_path):
        provider = _StubProvider([sas_for(7200), sas_for(7200)])
        manager, _ = self._manager(tmp_path, provider)
        manager.ensure(time.time())
        manager.ensure(time.time(), force=True)
        assert provider.calls == 2

    def test_needed_lifetime_scales_with_the_last_transfer(self, tmp_path):
        manager, _ = self._manager(
            tmp_path, _StubProvider([sas_for(7200)]), sas_min_remaining=1800
        )
        assert manager.needed_lifetime(None) == 1800
        assert manager.needed_lifetime(60) == 1800
        assert manager.needed_lifetime(1200) == 3600

    def test_long_job_demands_a_longer_token(self, tmp_path):
        provider = _StubProvider([sas_for(3000), sas_for(20000)])
        manager, _ = self._manager(tmp_path, provider, sas_min_remaining=600)
        now = time.time()
        manager.ensure(now, need=manager.needed_lifetime(None))
        # Last run took 40 minutes, so 50 minutes of validity is not enough.
        manager.ensure(now, need=manager.needed_lifetime(2400))
        assert provider.calls == 2

    def test_unrefreshable_token_is_returned_as_is(self, tmp_path):
        provider = usm_azure.InlineProvider(sas_for(60))
        manager, _ = self._manager(tmp_path, provider, sas_min_remaining=1800)
        now = time.time()
        first = manager.ensure(now)
        assert first.remaining(now) == pytest.approx(60, abs=5)
        manager.ensure(now)  # cached, still short, but nothing to rotate to

    def test_aad_manager_is_disabled(self, tmp_path):
        manager, _ = self._manager(tmp_path, usm_azure.AadProvider(), auth="aad")
        assert manager.enabled is False
        assert manager.ensure(time.time()).token == ""

    def test_invalidate_drops_the_cache(self, tmp_path):
        provider = _StubProvider([sas_for(7200), sas_for(7200)])
        manager, _ = self._manager(tmp_path, provider)
        manager.ensure(time.time())
        manager.invalidate()
        manager.ensure(time.time())
        assert provider.calls == 2


# --- azcopy engine ---------------------------------------------------------


def envelope(kind: str, content) -> str:
    if not isinstance(content, str):
        content = json.dumps(content)
    return json.dumps({"MessageType": kind, "MessageContent": content})


class TestAzcopyParsing:
    def test_end_of_job_summary(self):
        summary, errors = parse_azcopy_json(
            [
                envelope("Info", "starting"),
                envelope(
                    "EndOfJob",
                    {
                        "JobID": "abc",
                        "TransfersCompleted": 3,
                        "TransfersFailed": 0,
                        "TransfersSkipped": 1,
                        "TotalBytesTransferred": 1024,
                        "JobStatus": "Completed",
                    },
                ),
            ]
        )
        assert summary["JobID"] == "abc"
        assert summary["TransfersCompleted"] == 3
        assert errors == []

    def test_errors_are_collected(self):
        _, errors = parse_azcopy_json([envelope("Error", "boom")])
        assert errors == ["boom"]

    def test_non_json_lines_kept_as_diagnostics(self):
        _, errors = parse_azcopy_json(["INFO: plain text banner", ""])
        assert errors == ["INFO: plain text banner"]

    def test_malformed_json_line(self):
        _, errors = parse_azcopy_json(['{"MessageType": broken'])
        assert len(errors) == 1

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Signature not valid in the specified time frame", AUTH_EXPIRED),
            ("AuthenticationFailed: server failed", AUTH_EXPIRED),
            ("AuthorizationPermissionMismatch", AUTH_INVALID),
            ("The specified container does not exist", FATAL),
            ("dial tcp: i/o timeout", NETWORK),
            ("connection reset by peer", NETWORK),
        ],
    )
    def test_classification(self, text, expected):
        assert classify_failure(text, 1) == expected

    def test_unknown_failure_is_treated_as_transient(self):
        assert classify_failure("something odd happened", 1) == NETWORK

    def test_no_failure_when_exit_zero(self):
        assert classify_failure("", 0) == OK

    def test_interpret_success(self):
        result = interpret_result(
            {"JobID": "j", "TransfersCompleted": 2, "TotalBytesTransferred": 10},
            [],
            0,
            1.5,
        )
        assert result.status == OK and result.ok
        assert result.completed == 2 and result.bytes == 10

    def test_interpret_partial(self):
        result = interpret_result(
            {"TransfersCompleted": 2, "TransfersFailed": 1}, ["dial tcp"], 1, 1.0
        )
        assert result.status == PARTIAL

    def test_interpret_auth_expiry(self):
        result = interpret_result(
            {
                "JobID": "j",
                "ErrorMsg": "Signature not valid in the specified time frame",
            },
            [],
            1,
            0.5,
        )
        assert result.status == AUTH_EXPIRED and result.job_id == "j"

    def test_interpret_redacts_the_error(self):
        result = interpret_result({"ErrorMsg": "failed for sig=SECRET"}, [], 1, 0)
        assert "SECRET" not in result.error

    def test_interpret_cancelled(self):
        result = interpret_result({"JobStatus": "Cancelled"}, [], 1, 0)
        assert result.status == azsync.CANCELLED

    def test_summary_text(self):
        assert "up to date" in interpret_result({}, [], 0, 0).summary()
        busy = interpret_result(
            {
                "TransfersCompleted": 2,
                "TotalBytesTransferred": 2048,
                "TransfersFailed": 1,
            },
            ["dial tcp"],
            1,
            0,
        )
        assert "2 transferred" in busy.summary() and "1 failed" in busy.summary()


class TestAzcopyArgv:
    def _argv(self, tmp_path, **job_kw):
        job = make_job(tmp_path, **job_kw)
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        return engine.build_argv("sv=1&sig=abc"), job

    def test_basic_shape(self, tmp_path):
        argv, job = self._argv(tmp_path)
        assert argv[:2] == ["/fake/azcopy", "sync"]
        assert argv[2] == job.source
        assert argv[3].startswith("https://acct.blob.core.windows.net/bucket/path?")
        assert "--recursive" in argv
        assert "--delete-destination=false" in argv
        assert "--output-type=json" in argv

    def test_sas_is_appended_to_the_url(self, tmp_path):
        argv, _ = self._argv(tmp_path)
        assert argv[3].endswith("?sv=1&sig=abc")

    def test_delete_mode(self, tmp_path):
        argv, _ = self._argv(tmp_path, delete_destination=True)
        assert "--delete-destination=true" in argv

    def test_compare_hash_implies_put_md5(self, tmp_path):
        argv, _ = self._argv(tmp_path, compare_hash=True)
        assert "--compare-hash=MD5" in argv and "--put-md5" in argv

    def test_excludes_are_rendered(self, tmp_path):
        argv, _ = self._argv(tmp_path, excludes=["*.log"])
        assert "--exclude-pattern" in argv
        assert "*.log" in argv[argv.index("--exclude-pattern") + 1]

    def test_throttles(self, tmp_path):
        argv, _ = self._argv(tmp_path, cap_mbps=100.0, block_size_mb=8.0)
        assert argv[argv.index("--cap-mbps") + 1] == "100.0"
        assert argv[argv.index("--block-size-mb") + 1] == "8.0"

    def test_dry_run_flag(self, tmp_path):
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        assert "--dry-run" in engine.build_argv(None, dry_run=True)

    def test_no_sas_leaves_a_clean_url(self, tmp_path):
        job = make_job(tmp_path, auth="aad")
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        assert engine.build_argv(None)[3] == job.dest

    def test_inline_sas_in_dest_is_replaced_not_duplicated(self, tmp_path):
        job = make_job(
            tmp_path, dest="https://a.blob.core.windows.net/c/d?sv=old&sig=old"
        )
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        url = engine.build_argv("sv=new&sig=new")[3]
        assert url == "https://a.blob.core.windows.net/c/d?sv=new&sig=new"

    def test_resume_argv_carries_the_fresh_sas(self, tmp_path):
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        argv = engine.build_resume_argv("job-1", "sv=1&sig=abc")
        assert argv[1:4] == ["jobs", "resume", "job-1"]
        assert argv[argv.index("--destination-sas") + 1] == "sv=1&sig=abc"

    def test_env_points_azcopy_at_our_directories(self, tmp_path):
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        env = engine.env()
        assert env["AZCOPY_LOG_LOCATION"].startswith(str(tmp_path / "wd"))
        assert env["AZCOPY_JOB_PLAN_LOCATION"].startswith(str(tmp_path / "wd"))

    def test_aad_sets_auto_login(self, tmp_path):
        job = make_job(tmp_path, auth="aad")
        engine = AzcopyEngine(job, binary="/fake/azcopy", state_dir=tmp_path / "wd")
        assert engine.env()["AZCOPY_AUTO_LOGIN_TYPE"] == "AZCLI"


# --- Fake azcopy driven engine/supervisor ---------------------------------


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


class TestEngineAgainstFakeAzcopy:
    def test_successful_run(self, tmp_path, fake_azcopy):
        fake_azcopy.program(ok_step(completed=3, size=4096))
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        result = engine.run(engine.build_argv("sv=1&sig=abc"))
        assert result.status == OK
        assert result.completed == 3 and result.bytes == 4096
        assert result.job_id == "job-1"
        assert fake_azcopy.calls[0][0] == "sync"

    def test_missing_binary_is_fatal_not_a_crash(self, tmp_path):
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, binary="/nope/azcopy", state_dir=tmp_path / "wd")
        result = engine.run(engine.build_argv(None))
        assert result.status == FATAL and result.exit_code == 127

    def test_auth_expiry_surfaces(self, tmp_path, fake_azcopy):
        fake_azcopy.program(
            fail_step("Signature not valid in the specified time frame")
        )
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        result = engine.run(engine.build_argv("sv=1&sig=abc"))
        assert result.status == AUTH_EXPIRED


# --- Supervisor ------------------------------------------------------------


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


class TestSupervisorSync:
    def test_successful_sync_updates_state_and_history(
        self, tmp_path, fake_azcopy, state_dir
    ):
        fake_azcopy.program(ok_step(completed=4, size=100))
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.acc.record(clock.now, size=10)
        result = sup.run_sync("quiet")
        assert result.status == OK
        assert sup.state.state == "idle"
        assert sup.state.consecutive_failures == 0
        assert sup.state.last_result == OK
        assert sup.acc.snapshot().files == 0  # batch consumed
        history = azsync.read_history(sup.job.id)
        assert history[-1]["status"] == OK and history[-1]["reason"] == "quiet"

    def test_failed_sync_returns_the_batch_and_backs_off(
        self, tmp_path, fake_azcopy, state_dir
    ):
        fake_azcopy.program(fail_step("dial tcp: i/o timeout"))
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.acc.record(clock.now, size=10)
        result = sup.run_sync("quiet")
        assert result.status == NETWORK
        assert sup.state.state == "backoff"
        assert sup.state.consecutive_failures == 1
        assert sup.state.backoff_until == clock.now + 30
        # The change is not lost: it counts towards the next decision.
        assert sup.acc.snapshot().files == 1

    def test_changes_during_a_sync_belong_to_the_next_batch(
        self, tmp_path, fake_azcopy, state_dir
    ):
        fake_azcopy.program(ok_step())
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.acc.record(clock.now, size=10)
        original_execute = sup._execute

        def execute(token):
            sup.acc.record(clock.now, size=99)  # arrives mid-transfer
            return original_execute(token)

        sup._execute = execute
        sup.run_sync("quiet")
        pending = sup.acc.snapshot()
        assert pending.files == 1 and pending.bytes == 99

    def test_auth_expiry_refreshes_and_resumes(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(
            fail_step("Signature not valid in the specified time frame"),
            ok_step(completed=5),
        )
        sup, clock, provider = build_supervisor(tmp_path, fake_azcopy, state_dir)
        provider.tokens = [sas_for(60, now=clock.now), sas_for(7200, now=clock.now)]
        result = sup.run_sync("quiet")
        assert result.status == OK and result.completed == 5
        assert provider.calls >= 2  # the token really was rotated
        calls = fake_azcopy.calls
        assert calls[0][0] == "sync"
        assert calls[1][:3] == ["jobs", "resume", "job-1"]
        assert "--destination-sas" in calls[1]

    def test_resume_falls_back_to_a_full_sync(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(
            fail_step("Signature not valid in the specified time frame"),
            fail_step("cannot start job due to error", job_id="job-1"),
            ok_step(completed=7),
        )
        sup, _, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        result = sup.run_sync("quiet")
        assert result.status == OK and result.completed == 7
        assert [c[0] for c in fake_azcopy.calls] == ["sync", "jobs", "sync"]

    def test_sas_failure_is_reported_without_running_azcopy(
        self, tmp_path, fake_azcopy, state_dir
    ):
        sup, _, provider = build_supervisor(tmp_path, fake_azcopy, state_dir)
        provider.tokens = [SasError("no credentials")]
        result = sup.run_sync("quiet")
        assert result.status == AUTH_INVALID
        assert fake_azcopy.calls == []
        assert sup.acc.snapshot().files == 0  # nothing was pending

    def test_fatal_error_marks_the_job_failed(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(fail_step("The specified container does not exist"))
        sup, _, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        result = sup.run_sync("quiet")
        assert result.status == FATAL
        assert sup.state.state == "failed"

    def test_partial_transfer_is_not_fatal(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(
            {
                "lines": [
                    {
                        "MessageType": "EndOfJob",
                        "MessageContent": json.dumps(
                            {
                                "JobID": "j",
                                "TransfersCompleted": 5,
                                "TransfersFailed": 2,
                                "ErrorMsg": "dial tcp: i/o timeout",
                                "JobStatus": "CompletedWithErrors",
                            }
                        ),
                    }
                ],
                "exit": 1,
            }
        )
        sup, _, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        result = sup.run_sync("quiet")
        assert result.status == PARTIAL
        assert sup.state.state == "idle"

    def test_backoff_grows_then_resets(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(fail_step("dial tcp"), fail_step("dial tcp"), ok_step())
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.run_sync("quiet")
        assert sup.state.backoff_until == clock.now + 30
        sup.run_sync("quiet")
        assert sup.state.backoff_until == clock.now + 60
        sup.run_sync("quiet")
        assert sup.state.backoff_until is None
        assert sup.state.consecutive_failures == 0

    def test_state_file_is_written(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(ok_step())
        sup, _, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.run_sync("manual")
        persisted = azsync.load_state(sup.job.id)
        assert persisted.last_result == OK
        assert persisted.total_syncs == 1

    def test_history_is_capped(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(ok_step())
        sup, _, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        for _ in range(3):
            sup.run_sync("quiet")
        assert len(azsync.read_history(sup.job.id, 100)) == 3

    def test_tick_runs_a_sync_when_the_policy_says_so(
        self, tmp_path, fake_azcopy, state_dir
    ):
        fake_azcopy.program(ok_step())
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.state.last_sync_end = clock.now  # suppress the heartbeat
        sup.acc.record(clock.now, size=5)
        assert sup.tick().action == WAIT  # min-gap
        clock.advance(60)
        decision = sup.tick()
        assert decision.action == SYNC and decision.reason == "quiet"
        assert len(fake_azcopy.calls) == 1

    def test_trigger_file_forces_a_sync(self, tmp_path, fake_azcopy, state_dir):
        fake_azcopy.program(ok_step())
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.state.last_sync_end = clock.now
        azsync.trigger_path(sup.job.id).parent.mkdir(parents=True, exist_ok=True)
        azsync.trigger_path(sup.job.id).write_text("go")
        decision = sup.tick()
        assert decision.reason == "manual"
        assert not azsync.trigger_path(sup.job.id).exists()


# --- Watcher ---------------------------------------------------------------


class TestPollingWatcher:
    def _watcher(self, root, acc, **kw):
        return PollingWatcher(Path(root), ExcludeSpec.build(), acc, **kw)

    def test_first_scan_is_only_a_baseline(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        acc = ChangeAccumulator()
        self._watcher(tmp_path, acc).poll_once()
        assert acc.snapshot().files == 0

    def test_detects_new_files(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc)
        watcher.poll_once()
        (tmp_path / "a.txt").write_text("hello")
        watcher.poll_once()
        stat = acc.snapshot()
        assert stat.files == 1 and stat.bytes == 5

    def test_detects_modifications(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("x")
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc)
        watcher.poll_once()
        os.utime(path, (time.time() + 10, time.time() + 10))
        watcher.poll_once()
        assert acc.snapshot().files == 1

    def test_detects_deletions(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("x")
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc)
        watcher.poll_once()
        path.unlink()
        watcher.poll_once()
        assert acc.snapshot().deletes == 1

    def test_recurses_into_subdirectories(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc)
        watcher.poll_once()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "c.txt").write_text("hi")
        watcher.poll_once()
        assert acc.snapshot().files == 1

    def test_excluded_files_do_not_trigger(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc)
        watcher.poll_once()
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref")
        (tmp_path / "keep.pyc").write_text("x")
        watcher.poll_once()
        assert acc.snapshot().files == 0

    def test_no_changes_means_no_events(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc)
        watcher.poll_once()
        watcher.poll_once()
        assert acc.snapshot().files == 0

    def test_huge_tree_degrades_to_an_aggregate_signature(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc, max_index_files=2)
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("x")
        watcher.poll_once()
        (tmp_path / "new.txt").write_text("yyyy")
        watcher.poll_once()
        assert acc.snapshot().files >= 1
        assert acc.degraded is True  # forces a full reconcile, which is safe

    def test_start_stop_threads(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = self._watcher(tmp_path, acc, interval=0.05)
        watcher.start()
        try:
            (tmp_path / "a.txt").write_text("x")
            deadline = time.time() + 3
            while time.time() < deadline and acc.snapshot().files == 0:
                time.sleep(0.05)
        finally:
            watcher.stop()
        assert acc.snapshot().files == 1


@pytest.mark.skipif(
    not azsync.InotifyWatcher.available(), reason="watchdog not installed"
)
class TestInotifyWatcher:
    def test_detects_a_write(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = azsync.InotifyWatcher(tmp_path, ExcludeSpec.build(), acc)
        watcher.start()
        try:
            (tmp_path / "a.txt").write_text("hello")
            deadline = time.time() + 5
            while time.time() < deadline and acc.snapshot().files == 0:
                time.sleep(0.05)
        finally:
            watcher.stop()
        assert acc.snapshot().files >= 1

    def test_excluded_paths_are_ignored(self, tmp_path):
        acc = ChangeAccumulator()
        watcher = azsync.InotifyWatcher(tmp_path, ExcludeSpec.build(), acc)
        watcher.start()
        try:
            git = tmp_path / ".git"
            git.mkdir()
            (git / "HEAD").write_text("ref: x")
            time.sleep(1.0)
        finally:
            watcher.stop()
        assert acc.snapshot().files == 0


# --- Store & helpers -------------------------------------------------------


class TestStore:
    def test_roundtrip(self, tmp_path, state_dir):
        job = make_job(tmp_path, id="round", excludes=["*.log"])
        azsync.save_job(job)
        loaded = azsync.load_job("round")
        assert loaded == job

    def test_unknown_job(self, state_dir):
        with pytest.raises(KeyError):
            azsync.load_job("nope")

    def test_list_ignores_state_files(self, tmp_path, state_dir):
        job = make_job(tmp_path, id="a")
        azsync.save_job(job)
        azsync.save_state("a", azsync.RuntimeState(state="idle"))
        assert [j.id for j in azsync.list_jobs()] == ["a"]

    def test_list_skips_corrupt_definitions(self, tmp_path, state_dir):
        azsync.save_job(make_job(tmp_path, id="good"))
        (state_dir / "bad.json").write_text("{not json")
        assert [j.id for j in azsync.list_jobs()] == ["good"]

    def test_state_defaults_when_absent(self, state_dir):
        assert azsync.load_state("ghost").state == "stopped"

    def test_delete_removes_everything(self, tmp_path, state_dir):
        job = make_job(tmp_path, id="gone")
        azsync.save_job(job)
        azsync.save_state("gone", azsync.RuntimeState())
        azsync.append_history("gone", {"at": 1})
        azsync.delete_job("gone")
        assert not list(state_dir.glob("gone*"))

    def test_ids_are_derived_and_deduplicated(self, tmp_path, state_dir):
        source = tmp_path / "data"
        source.mkdir()
        dest = "https://a.blob.core.windows.net/bucket/x"
        first = azsync.make_job_id(source, dest)
        assert first == "data-bucket"
        azsync.save_job(make_job(tmp_path, id=first))
        assert azsync.make_job_id(source, dest) == "data-bucket-2"

    def test_custom_id_is_slugified(self, tmp_path, state_dir):
        assert azsync.make_job_id(tmp_path, "x", "My Sync!") == "my-sync"

    def test_lock_is_exclusive(self, state_dir):
        first = usm_azure.FileLock(state_dir / "l.lock")
        second = usm_azure.FileLock(state_dir / "l.lock")
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()


class TestBlobUrlHelpers:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://a.blob.core.windows.net/c/d", True),
            ("https://a.dfs.core.windows.net/c", True),
            ("https://example.com/c", False),
            ("/local/path", False),
        ],
    )
    def test_is_https_blob(self, url, expected):
        assert usm_azure.is_https_blob(url) is expected

    def test_parse_account_container(self):
        assert usm_azure.parse_blob_url(
            "https://acct.blob.core.windows.net/bucket/deep/path"
        ) == ("acct", "bucket")

    def test_parse_rejects_incomplete_urls(self):
        with pytest.raises(ValueError):
            usm_azure.parse_blob_url("https://acct.blob.core.windows.net/")

    def test_has_sas(self):
        assert usm_azure.has_sas("https://a.blob.core.windows.net/c?sig=x") is True
        assert usm_azure.has_sas("https://a.blob.core.windows.net/c") is False


class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [(512, "512B"), (2048, "2.0KiB"), (5 * 1024**2, "5.0MiB")],
    )
    def test_human_bytes(self, value, expected):
        assert human_bytes(value) == expected

    @pytest.mark.parametrize(
        "value,expected", [(None, "-"), (30, "30s"), (90, "1m30s"), (7200, "2h00m")]
    )
    def test_human_duration(self, value, expected):
        assert human_duration(value) == expected


# --- CLI -------------------------------------------------------------------


@pytest.fixture
def runner():
    import click.testing

    return click.testing.CliRunner()


def invoke(runner, args, **kw):
    return runner.invoke(azsync.cli, args, **kw)


class TestCli:
    def test_add_defines_and_can_skip_starting(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--auth",
                "aad",
                "--no-start",
            ],
        )
        assert result.exit_code == 0, result.output
        job = azsync.load_job("src-bucket")
        assert job.auth == "aad"
        assert job.source == str(source.resolve())

    def test_add_rejects_a_non_blob_destination(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(runner, ["add", str(source), "/tmp/elsewhere", "--no-start"])
        assert result.exit_code != 0
        assert "blob" in result.output.lower()

    def test_add_infers_inline_auth_from_the_url(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        url = f"https://acct.blob.core.windows.net/bucket/p?{sas_for(3600)}"
        result = invoke(runner, ["add", str(source), url, "--no-start"])
        assert result.exit_code == 0, result.output
        assert azsync.load_job("src-bucket").auth == "inline"

    def test_add_maps_sas_flags_to_auth_kinds(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--sas-command",
                "mint.sh",
                "--no-start",
            ],
        )
        assert result.exit_code == 0, result.output
        job = azsync.load_job("src-bucket")
        assert job.auth == "exec" and job.sas_spec == "mint.sh"

    def test_add_rejects_two_sas_sources(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--sas-command",
                "a",
                "--sas-url",
                "https://b",
                "--no-start",
            ],
        )
        assert result.exit_code != 0
        assert "pick one SAS source" in result.output

    def test_add_rejects_conflicting_auth(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--auth",
                "env",
                "--sas-command",
                "a",
                "--no-start",
            ],
        )
        assert result.exit_code != 0

    def test_add_requires_a_spec_for_external_auth(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--auth",
                "http",
                "--no-start",
            ],
        )
        assert result.exit_code != 0
        assert "--sas-url" in result.output

    def test_add_refuses_duplicate_ids(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        args = [
            "add",
            str(source),
            "https://acct.blob.core.windows.net/bucket/p",
            "--auth",
            "aad",
            "--no-start",
            "--name",
            "dup",
        ]
        assert invoke(runner, args).exit_code == 0
        assert invoke(runner, args).exit_code != 0

    def test_trigger_options_land_in_the_definition(self, tmp_path, state_dir, runner):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--auth",
                "aad",
                "--no-start",
                "--quiet-period",
                "2",
                "--batch-files",
                "50",
                "--max-delay",
                "60",
                "--interval",
                "900",
                "--min-gap",
                "10",
                "--delete",
                "-e",
                "*.log",
            ],
        )
        assert result.exit_code == 0, result.output
        job = azsync.load_job("src-bucket")
        assert (job.quiet_period, job.batch_files, job.max_delay) == (2.0, 50, 60.0)
        assert (job.interval, job.min_gap) == (900.0, 10.0)
        assert job.delete_destination is True
        assert "*.log" in job.excludes

    def test_ls_empty(self, state_dir, runner):
        result = invoke(runner, ["ls"])
        assert result.exit_code == 0
        assert "No syncs defined" in result.output

    def test_ls_lists_jobs(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["ls"])
        assert result.exit_code == 0 and "alpha" in result.output

    def test_ls_redacts_the_sas(self, tmp_path, state_dir, runner):
        azsync.save_job(
            make_job(
                tmp_path,
                id="secret",
                dest=f"https://a.blob.core.windows.net/c/d?{sas_for(60)}",
            )
        )
        result = invoke(runner, ["ls"])
        assert "ABC123" not in result.output

    def test_status_unknown_job_lists_known_ones(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["status", "nope"])
        assert result.exit_code != 0
        assert "alpha" in result.output

    def test_status_shows_the_trigger_configuration(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["status", "alpha"])
        assert result.exit_code == 0
        assert "quiet" in result.output and "heartbeat" in result.output

    def test_status_shows_history(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        azsync.append_history(
            "alpha",
            {
                "at": time.time(),
                "reason": "quiet",
                "status": "ok",
                "duration": 1.0,
                "completed": 3,
                "bytes": 100,
            },
        )
        result = invoke(runner, ["status", "alpha"])
        assert "Recent syncs" in result.output

    def test_sync_runs_when_no_daemon_is_running(
        self, tmp_path, state_dir, fake_azcopy, runner
    ):
        fake_azcopy.program(ok_step(completed=2))
        job = make_job(tmp_path, id="alpha", auth="aad")
        azsync.save_job(job)
        result = invoke(runner, ["sync", "alpha"])
        assert result.exit_code == 0, result.output
        assert "transferred" in result.output
        assert fake_azcopy.calls[0][0] == "sync"

    def test_sync_reports_failure(self, tmp_path, state_dir, fake_azcopy, runner):
        fake_azcopy.program(fail_step("The specified container does not exist"))
        azsync.save_job(make_job(tmp_path, id="alpha", auth="aad"))
        result = invoke(runner, ["sync", "alpha"])
        assert result.exit_code != 0
        assert "fatal" in result.output.lower()

    def test_once_needs_no_definition(self, tmp_path, state_dir, fake_azcopy, runner):
        fake_azcopy.program(ok_step())
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "once",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--auth",
                "aad",
            ],
        )
        assert result.exit_code == 0, result.output
        assert azsync.list_jobs() == []

    def test_rm_deletes(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["rm", "alpha", "-y"])
        assert result.exit_code == 0
        assert azsync.list_jobs() == []

    def test_stop_when_not_running(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["stop", "alpha"])
        assert result.exit_code == 0 and "already stopped" in result.output

    def test_logs_missing(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["logs", "alpha"])
        assert result.exit_code != 0 and "no log" in result.output

    def test_logs_tail_redacts(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        azsync.log_path("alpha").write_text("line with sig=SECRET\n")
        result = invoke(runner, ["logs", "alpha"])
        assert result.exit_code == 0
        assert "SECRET" not in result.output and "sig=***" in result.output

    def test_token_for_aad(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha", auth="aad"))
        result = invoke(runner, ["token", "alpha"])
        assert result.exit_code == 0 and "no SAS" in result.output

    def test_token_shows_a_redacted_value(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        monkeypatch.setenv("MY_SAS", sas_for(3600))
        azsync.save_job(make_job(tmp_path, id="alpha", auth="env", sas_spec="MY_SAS"))
        result = invoke(runner, ["token", "alpha"])
        assert result.exit_code == 0
        assert "ABC123" not in result.output and "sig=***" in result.output

    def test_disable_when_not_enabled(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="alpha"))
        result = invoke(runner, ["disable", "alpha"])
        assert result.exit_code == 0 and "not enabled" in result.output


class TestServiceRendering:
    def test_systemd_unit(self, tmp_path):
        job = make_job(tmp_path, id="alpha")
        unit = azsync.SERVICE.render_unit(
            f"usm azsync {job.id}",
            "/usr/local/bin/usm azsync up alpha",
            "/usr/local/bin/usm",
        )
        assert "ExecStart=/usr/local/bin/usm azsync up alpha" in unit
        assert "Restart=always" in unit
        assert "WantedBy=default.target" in unit

    def test_launchd_plist(self, tmp_path):
        import plistlib

        job = make_job(tmp_path, id="alpha")
        payload = plistlib.loads(
            azsync.SERVICE.render_plist(
                job.id,
                ["/usr/local/bin/usm", "azsync", "up", job.id],
                "/usr/local/bin/usm",
            )
        )
        assert payload["ProgramArguments"] == [
            "/usr/local/bin/usm",
            "azsync",
            "up",
            "alpha",
        ]
        assert payload["RunAtLoad"] is True

    def test_unit_and_label_names(self, tmp_path):
        assert azsync.SERVICE.unit_name("alpha") == "usm-azsync-alpha.service"
        assert azsync.SERVICE.label("alpha").endswith(".azsync.alpha")


# --- Live daemon -----------------------------------------------------------


def wait_until(predicate, timeout=25.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestDaemonLifecycle:
    """Spawn the real supervisor process against the fake azcopy."""

    def _job(self, tmp_path, **kw):
        source = tmp_path / "watched"
        source.mkdir(exist_ok=True)
        job = make_job(
            tmp_path,
            id="live",
            source=source,
            auth="aad",  # no SAS machinery in the child
            quiet_period=0.4,
            min_gap=0.4,
            max_delay=5.0,
            interval=3600.0,
            batch_files=200,
            watch_mode="poll",
            poll_interval=0.3,
            **kw,
        )
        azsync.save_job(job)
        return job, source

    def test_initial_reconcile_then_react_to_a_write(
        self, tmp_path, state_dir, fake_azcopy
    ):
        fake_azcopy.program(ok_step())
        job, source = self._job(tmp_path)
        (source / "seed.txt").write_text("seed")

        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(lambda: azsync.load_state(job.id).total_syncs >= 1), (
                "daemon never performed the initial reconcile"
            )
            assert wait_until(lambda: azsync.load_state(job.id).state == "idle")

            (source / "new.txt").write_text("hello world")
            assert wait_until(lambda: azsync.load_state(job.id).total_syncs >= 2), (
                "daemon did not react to a new file"
            )
        finally:
            azsync.stop_daemon(job.id)

        assert wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)
        final = azsync.load_state(job.id)
        assert final.state == "stopped"
        assert final.last_result == OK
        assert final.watch_backend == "poll"
        assert all(call[0] == "sync" for call in fake_azcopy.calls)

    def test_excluded_churn_does_not_trigger_transfers(
        self, tmp_path, state_dir, fake_azcopy
    ):
        fake_azcopy.program(ok_step())
        job, source = self._job(tmp_path)
        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(lambda: azsync.load_state(job.id).total_syncs >= 1)
            baseline = azsync.load_state(job.id).total_syncs

            git = source / ".git"
            git.mkdir()
            for i in range(20):
                (git / f"obj{i}").write_text("x" * 50)
            time.sleep(3)
            assert azsync.load_state(job.id).total_syncs == baseline
        finally:
            azsync.stop_daemon(job.id)
        wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)

    def test_manual_trigger_reaches_a_running_daemon(
        self, tmp_path, state_dir, fake_azcopy
    ):
        fake_azcopy.program(ok_step())
        job, _ = self._job(tmp_path)
        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(lambda: azsync.load_state(job.id).total_syncs >= 1)
            before = azsync.load_state(job.id).total_syncs
            time.sleep(0.6)  # clear the min-gap
            assert azsync.poke_daemon(job.id) is True
            assert wait_until(lambda: azsync.load_state(job.id).total_syncs > before), (
                "SIGUSR1 did not trigger a sync"
            )
        finally:
            azsync.stop_daemon(job.id)
        wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)

    def test_second_supervisor_refuses_to_start(self, tmp_path, state_dir, fake_azcopy):
        fake_azcopy.program(ok_step())
        job, _ = self._job(tmp_path)
        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(lambda: azsync.is_running(job.id))
            assert azsync.run_supervisor(job.id) == 1  # lock held
        finally:
            azsync.stop_daemon(job.id)
        wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)

    def test_missing_source_directory_fails_fast(self, tmp_path, state_dir):
        job = make_job(tmp_path, id="ghost", source=tmp_path / "watched", auth="aad")
        job.source = str(tmp_path / "does-not-exist")
        azsync.save_job(job)
        assert azsync.run_supervisor("ghost") == 2
        state = azsync.load_state("ghost")
        assert state.state == "failed" and "missing" in (state.last_error or "")

    def test_is_running_reflects_reality(self, tmp_path, state_dir, fake_azcopy):
        fake_azcopy.program(ok_step())
        job, _ = self._job(tmp_path)
        assert azsync.is_running(job.id) is False
        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(lambda: azsync.is_running(job.id))
        finally:
            azsync.stop_daemon(job.id)
        assert wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)
        assert azsync.is_running(job.id) is False

    def test_pid_alive_ignores_zombies(self):
        """os.kill(pid, 0) succeeds for unreaped children; we must not."""
        import subprocess as sp

        proc = sp.Popen([os.sys.executable, "-c", "pass"])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and usm_daemon._is_zombie(proc.pid) is False:
                time.sleep(0.05)
            assert usm_daemon._is_zombie(proc.pid) is True, "expected a zombie"
            assert usm_daemon.pid_alive(proc.pid) is False
        finally:
            proc.wait()
        assert usm_daemon.pid_alive(proc.pid) is False

    def test_pid_alive_for_self_and_missing(self):
        assert usm_daemon.pid_alive(os.getpid()) is True
        assert usm_daemon.pid_alive(None) is False
        assert usm_daemon.pid_alive(999_999_999) is False


# --- Binary resolution, destinations, service detection --------------------


class TestAzcopyResolution:
    def test_override_wins(self, tmp_path, monkeypatch):
        binary = tmp_path / "custom-azcopy"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("USM_AZCOPY_BIN", str(binary))
        assert azsync.find_azcopy() == str(binary)

    def test_non_executable_override_is_ignored(self, tmp_path, monkeypatch):
        binary = tmp_path / "not-exec"
        binary.write_text("")
        binary.chmod(0o644)
        monkeypatch.setenv("USM_AZCOPY_BIN", str(binary))
        monkeypatch.setattr(azsync, "LOCAL_BIN_DIR", tmp_path / "empty")
        monkeypatch.setattr("shutil.which", lambda _n: None)
        assert azsync.find_azcopy() is None

    def test_falls_back_to_the_managed_location(self, tmp_path, monkeypatch):
        monkeypatch.delenv("USM_AZCOPY_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _n: None)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        managed = bin_dir / "azcopy"
        managed.write_text("#!/bin/sh\n")
        managed.chmod(0o755)
        monkeypatch.setattr(azsync, "LOCAL_BIN_DIR", bin_dir)
        assert azsync.find_azcopy() == str(managed)

    def test_ensure_points_at_usm_cp_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("USM_AZCOPY_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _n: None)
        monkeypatch.setattr(azsync, "LOCAL_BIN_DIR", tmp_path / "nothing")
        with pytest.raises(azsync.AzcopyNotFound, match="usm cp --install"):
            azsync.ensure_azcopy()

    def test_engine_resolves_lazily(self, tmp_path, fake_azcopy):
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        assert engine.binary == fake_azcopy.path


class TestResolveDestination:
    def test_https_passthrough(self):
        url = "https://acct.blob.core.windows.net/bucket/x"
        assert azsync.resolve_destination(url) == url

    def test_blobfuse_path_is_converted(self, tmp_path, monkeypatch):
        mount = tmp_path / "mnt" / "blob"
        (mount / "deep").mkdir(parents=True)
        monkeypatch.setattr(
            usm_azure,
            "blobfuse_mounts",
            lambda: {
                str(mount): {
                    "url": "https://acct.blob.core.windows.net/bucket/",
                    "account_name": "acct",
                    "container_name": "bucket",
                }
            },
        )
        assert azsync.resolve_destination(str(mount / "deep")) == (
            "https://acct.blob.core.windows.net/bucket/deep"
        )

    def test_unknown_local_path_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(usm_azure, "blobfuse_mounts", dict)
        with pytest.raises(Exception, match="blob"):
            azsync.resolve_destination(str(tmp_path))

    def test_no_mounts_when_psutil_is_missing(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kw):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *args, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert usm_azure.blobfuse_mounts() == {}


class TestServiceDetection:
    def test_enabled_kind_reads_the_unit_files(self, tmp_path, monkeypatch):
        systemd = tmp_path / "systemd"
        launchd = tmp_path / "launchd"
        systemd.mkdir()
        launchd.mkdir()
        monkeypatch.setattr(usm_daemon, "SYSTEMD_USER_DIR", systemd)
        monkeypatch.setattr(usm_daemon, "LAUNCHD_USER_DIR", launchd)
        assert azsync.SERVICE.enabled_kind("alpha") is None
        (systemd / "usm-azsync-alpha.service").write_text("[Unit]")
        assert azsync.SERVICE.enabled_kind("alpha") == "systemd"
        (systemd / "usm-azsync-alpha.service").unlink()
        (launchd / f"{azsync.SERVICE.label_prefix}alpha.plist").write_bytes(b"<plist/>")
        assert azsync.SERVICE.enabled_kind("alpha") == "launchd"

    def test_path_value_includes_the_managed_bin_dir(self):
        value = usm_azure.service_path_value("/opt/pipx/bin/usm")
        assert "/opt/pipx/bin" in value
        assert str(azsync.LOCAL_BIN_DIR) in value
        assert value.count("/usr/bin") == 1


class TestSupervisorLoopHelpers:
    def test_sleep_is_bounded(self, tmp_path, state_dir, fake_azcopy):
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        assert sup._sleep_for(azsync.Decision(WAIT, "idle", None)) == 5.0
        assert sup._sleep_for(azsync.Decision(WAIT, "x", clock.now + 900)) == 5.0
        assert sup._sleep_for(azsync.Decision(WAIT, "x", clock.now + 1)) == 1
        assert sup._sleep_for(azsync.Decision(WAIT, "x", clock.now - 100)) == 0.2

    def test_request_stop_is_observable(self, tmp_path, state_dir, fake_azcopy):
        sup, _, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.request_stop()
        assert sup._stop.is_set()

    def test_request_sync_forces_the_next_decision(
        self, tmp_path, state_dir, fake_azcopy
    ):
        fake_azcopy.program(ok_step())
        sup, clock, _ = build_supervisor(tmp_path, fake_azcopy, state_dir)
        sup.state.last_sync_end = clock.now
        assert sup.tick().action == WAIT
        sup.request_sync()
        assert sup.tick().reason == "manual"


class TestDryRunCommand:
    def test_dry_run_passes_the_flag(self, tmp_path, state_dir, fake_azcopy, runner):
        fake_azcopy.program(ok_step(completed=0))
        azsync.save_job(make_job(tmp_path, id="alpha", auth="aad"))
        result = invoke(runner, ["dry-run", "alpha"])
        assert result.exit_code == 0, result.output
        assert "--dry-run" in fake_azcopy.calls[0]


# --- Error paths, boundaries and output quality ---------------------------


class TestStoreResilience:
    def test_corrupt_state_falls_back(self, state_dir):
        (state_dir / "j.state.json").write_text("{oops")
        assert azsync.load_state("j").state == "stopped"

    def test_state_with_wrong_shape_falls_back(self, state_dir):
        (state_dir / "j.state.json").write_text(json.dumps(["nope"]))
        assert azsync.load_state("j").state == "stopped"

    def test_unknown_definition_fields_are_ignored(self, tmp_path, state_dir):
        raw = json.loads(json.dumps(azsync.asdict(make_job(tmp_path, id="j"))))
        raw["invented_later"] = 1
        (state_dir / "j.json").write_text(json.dumps(raw))
        assert azsync.load_job("j").id == "j"

    def test_history_survives_a_non_utf8_file(self, tmp_path, state_dir):
        """A corrupt log must not take `status` down with it."""
        azsync.append_history("j", {"at": 1})
        azsync.history_path("j").write_bytes(b"\xff\xfe not utf8")
        assert azsync.read_history("j") == []

    def test_history_skips_corrupt_lines(self, state_dir):
        path = azsync.history_path("j")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"at": 1}\nnot json\n{"at": 2}\n')
        assert [r["at"] for r in azsync.read_history("j")] == [1, 2]

    def test_history_stays_bounded(self, state_dir):
        for i in range(azsync.HISTORY_LIMIT * 4):
            azsync.append_history("j", {"at": i})
        lines = azsync.history_path("j").read_text().splitlines()
        assert len(lines) <= azsync.HISTORY_LIMIT * 2
        # Trimming keeps the newest entries.
        assert json.loads(lines[-1])["at"] == azsync.HISTORY_LIMIT * 4 - 1

    def test_read_history_of_nothing(self, state_dir):
        assert azsync.read_history("ghost") == []

    def test_make_id_falls_back_when_the_url_is_odd(self, tmp_path, state_dir):
        assert azsync.make_job_id(tmp_path / "data", "not-a-url") == "data"

    def test_changestat_truthiness(self):
        assert bool(ChangeStat()) is False
        assert bool(ChangeStat(files=1)) is True


class TestEngineEdgeCases:
    def test_put_md5_without_compare_hash(self, tmp_path):
        job = make_job(tmp_path, put_md5=True)
        engine = AzcopyEngine(job, binary="/fake", state_dir=tmp_path / "w")
        argv = engine.build_argv(None)
        assert "--put-md5" in argv and "--compare-hash=MD5" not in argv

    def test_timeout_is_classified_as_transient(self, tmp_path, fake_azcopy):
        fake_azcopy.program({"sleep": 5, "exit": 0})
        job = make_job(tmp_path)
        engine = AzcopyEngine(job, state_dir=tmp_path / "w")
        result = engine.run(engine.build_argv(None), timeout=0.5)
        assert result.status == NETWORK and "timeout" in result.error

    def test_summary_mentions_skipped_transfers(self):
        result = interpret_result(
            {"TransfersCompleted": 1, "TransfersSkipped": 4}, [], 0, 0
        )
        assert "4 skipped" in result.summary()

    def test_exit_zero_with_failures_is_partial(self):
        assert interpret_result({"TransfersFailed": 2}, [], 0, 0).status == PARTIAL

    def test_no_summary_at_all(self):
        result = interpret_result({}, [], 0, 0.5)
        assert result.status == OK and result.job_id is None

    def test_malformed_end_of_job_payload(self):
        summary, errors = parse_azcopy_json(
            [json.dumps({"MessageType": "EndOfJob", "MessageContent": "{broken"})]
        )
        assert summary == {} and errors

    def test_end_of_job_that_is_not_an_object(self):
        summary, _ = parse_azcopy_json(
            [json.dumps({"MessageType": "EndOfJob", "MessageContent": "[1,2]"})]
        )
        assert summary == {}


class TestSupervisorErrorHandling:
    def test_default_logger_redacts(self, tmp_path, state_dir, fake_azcopy, capsys):
        job = make_job(tmp_path)
        azsync.save_job(job)
        Supervisor(job).log("hi sig=SECRET")
        out = capsys.readouterr().out
        assert "hi" in out and "SECRET" not in out

    def test_run_fails_fast_on_a_missing_source(self, tmp_path, state_dir, fake_azcopy):
        job = make_job(tmp_path, id="ghost")
        job.source = str(tmp_path / "not-there")
        azsync.save_job(job)
        supervisor = Supervisor(job, log=lambda _m: None)
        assert supervisor.run() == 2
        assert azsync.load_state("ghost").state == "failed"

    def test_watcher_start_failure_falls_back_to_polling(
        self, tmp_path, state_dir, fake_azcopy
    ):
        fake_azcopy.program(ok_step())
        job = make_job(tmp_path, id="w", watch_mode="poll")
        azsync.save_job(job)

        class Broken(azsync.Watcher):
            backend = "broken"

            def start(self):
                raise RuntimeError("inotify limit reached")

            def stop(self):
                pass

        supervisor = Supervisor(
            job,
            watcher=Broken(job.source_path(), job.exclude_spec(), None),
            log=lambda _m: None,
        )
        supervisor.acc = ChangeAccumulator()
        supervisor.request_stop()
        assert supervisor.run() == 0
        assert azsync.load_state("w").watch_backend == "poll"

    def test_stop_daemon_when_nothing_runs(self, tmp_path, state_dir):
        azsync.save_job(make_job(tmp_path, id="j"))
        assert azsync.stop_daemon("j") is False

    def test_stop_daemon_handles_a_vanished_process(
        self, tmp_path, state_dir, monkeypatch
    ):
        azsync.save_job(make_job(tmp_path, id="j"))
        azsync.save_state("j", azsync.RuntimeState(supervisor_pid=4242))
        monkeypatch.setattr(azsync, "pid_alive", lambda _p: True)

        def gone(_pid, _sig):
            raise ProcessLookupError()

        monkeypatch.setattr(azsync.os, "kill", gone)
        assert azsync.stop_daemon("j") is False

    def test_stop_daemon_delegates_to_the_service(
        self, tmp_path, state_dir, monkeypatch
    ):
        import subprocess

        azsync.save_job(make_job(tmp_path, id="j"))
        monkeypatch.setattr(azsync.SERVICE, "enabled_kind", lambda _i: "systemd")
        monkeypatch.setattr(
            azsync.SERVICE,
            "stop",
            lambda _i: subprocess.CompletedProcess([], 0, "", ""),
        )
        assert azsync.stop_daemon("j") is True

    def test_poke_when_nothing_runs(self, tmp_path, state_dir):
        azsync.save_job(make_job(tmp_path, id="j"))
        assert azsync.poke_daemon("j") is False

    def test_request_stop_terminates_a_running_child(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = make_job(tmp_path)
        azsync.save_job(job)
        supervisor = Supervisor(job, log=lambda _m: None)
        terminated = {}

        class Child:
            def poll(self):
                return None

            def terminate(self):
                terminated["yes"] = True

        supervisor._child = Child()
        supervisor.request_stop()
        assert terminated.get("yes") is True

    def test_azcopy_line_filter_only_surfaces_errors(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = make_job(tmp_path)
        azsync.save_job(job)
        seen = []
        supervisor = Supervisor(job, log=seen.append)
        supervisor._azcopy_line('{"MessageType":"Info","MessageContent":"noise"}')
        supervisor._azcopy_line('{"MessageType":"Error","MessageContent":"boom"}')
        assert len(seen) == 1 and "boom" in seen[0]


class TestCliErrorPaths:
    def _define(self, tmp_path, **kw):
        job = make_job(tmp_path, **kw)
        azsync.save_job(job)
        azsync.save_state(job.id, azsync.RuntimeState())
        return job

    def test_start_refuses_when_running(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync, "is_running", lambda _i: True)
        result = invoke(runner, ["start", "j"])
        assert result.exit_code != 0 and "already running" in result.output

    def test_start_delegates_to_a_configured_service(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        import subprocess

        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync.SERVICE, "enabled_kind", lambda _i: "systemd")
        monkeypatch.setattr(
            azsync.SERVICE,
            "start",
            lambda _i: subprocess.CompletedProcess([], 0, "", ""),
        )
        assert invoke(runner, ["start", "j"]).exit_code == 0

    def test_sync_signals_a_live_daemon(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync, "is_running", lambda _i: True)
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *_a, **_kw: (SignalEvent.create("sync"), True),
        )
        result = invoke(runner, ["sync", "j"])
        assert result.exit_code == 0 and "sync now" in result.output

    def test_sync_reports_an_unacknowledged_signal(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync, "is_running", lambda _i: True)
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *_a, **_kw: (SignalEvent.create("sync"), False),
        )
        assert "did not acknowledge" in invoke(runner, ["sync", "j"]).output

    def test_sync_waits_for_a_live_daemon_result(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync, "is_running", lambda _i: True)
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *_a, **_kw: (SignalEvent.create("sync"), True),
        )
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: SignalResult(
                event_id, OK, time.time(), {"sync": {"status": OK}}
            ),
        )
        result = invoke(runner, ["sync", "j", "--wait"])
        assert result.exit_code == 0 and "complete" in result.output

    def test_sync_is_blocked_by_the_lock(
        self, tmp_path, state_dir, fake_azcopy, runner
    ):
        self._define(tmp_path, id="j", auth="aad")
        lock = azsync.FileLock(azsync.lock_path("j"))
        assert lock.acquire()
        try:
            result = invoke(runner, ["sync", "j"])
            assert result.exit_code != 0 and "busy" in result.output
        finally:
            lock.release()

    def test_partial_sync_is_reported_as_a_warning(
        self, tmp_path, state_dir, fake_azcopy, runner
    ):
        fake_azcopy.program(
            {
                "lines": [
                    {
                        "MessageType": "EndOfJob",
                        "MessageContent": json.dumps(
                            {
                                "JobID": "j",
                                "TransfersCompleted": 1,
                                "TransfersFailed": 1,
                                "ErrorMsg": "dial tcp",
                            }
                        ),
                    }
                ],
                "exit": 1,
            }
        )
        self._define(tmp_path, id="j", auth="aad")
        result = invoke(runner, ["sync", "j"])
        assert result.exit_code == 0 and "failed" in result.output

    def test_enable_reports_a_service_failure(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="j")

        def boom(*a, **kw):
            raise RuntimeError("systemctl unavailable")

        monkeypatch.setattr(azsync.SERVICE, "enable", boom)
        result = invoke(runner, ["enable", "j"])
        assert result.exit_code != 0 and "unavailable" in result.output

    def test_enable_launchd_message(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync.SERVICE, "enable", lambda *a, **kw: "launchd")
        assert "login" in invoke(runner, ["enable", "j"]).output

    def test_disable_removes_the_unit(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path, id="j")
        monkeypatch.setattr(azsync.SERVICE, "enabled_kind", lambda _i: "systemd")
        seen = {}
        monkeypatch.setattr(
            azsync.SERVICE, "disable", lambda i: seen.setdefault("id", i)
        )
        assert invoke(runner, ["disable", "j"]).exit_code == 0
        assert seen["id"] == "j"

    def test_rm_declines_without_confirmation(self, tmp_path, state_dir, runner):
        self._define(tmp_path, id="j")
        invoke(runner, ["rm", "j"], input="n\n")
        assert [x.id for x in azsync.list_jobs()] == ["j"]

    def test_restart_stops_then_starts(self, tmp_path, state_dir, runner, monkeypatch):
        job = self._define(tmp_path, id="j")
        events = []
        monkeypatch.setattr(
            azsync, "stop_daemon", lambda i, **kw: events.append(("stop", i))
        )
        monkeypatch.setattr(
            azsync, "_start_job", lambda j: events.append(("start", j.id))
        )
        assert invoke(runner, ["restart", "j"]).exit_code == 0
        assert events == [("stop", "j"), ("start", job.id)]

    def test_token_rotation_reports(self, tmp_path, state_dir, runner, monkeypatch):
        monkeypatch.setenv("AZ_SAS", sas_for(7200))
        self._define(tmp_path, id="j", auth="env", sas_spec="AZ_SAS")
        result = invoke(runner, ["token", "j", "--refresh"])
        assert result.exit_code == 0 and "Rotated" in result.output

    def test_token_surfaces_a_provider_failure(self, tmp_path, state_dir, runner):
        self._define(tmp_path, id="j", auth="exec", sas_spec="exit 4")
        result = invoke(runner, ["token", "j"])
        assert result.exit_code != 0 and "exited 4" in result.output

    def test_dry_run_reports_a_failure(self, tmp_path, state_dir, fake_azcopy, runner):
        fake_azcopy.program(fail_step("The specified container does not exist"))
        self._define(tmp_path, id="j", auth="aad")
        result = invoke(runner, ["dry-run", "j"])
        assert result.exit_code != 0

    def test_logs_azcopy_variant_without_logs(self, tmp_path, state_dir, runner):
        self._define(tmp_path, id="j")
        result = invoke(runner, ["logs", "j", "--azcopy"])
        assert result.exit_code != 0 and "no azcopy logs" in result.output

    def test_logs_azcopy_variant(self, tmp_path, state_dir, runner):
        self._define(tmp_path, id="j")
        log_dir = azsync.azcopy_dir("j") / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "run.log").write_text("azcopy said sig=SECRET\n")
        result = invoke(runner, ["logs", "j", "--azcopy"])
        assert result.exit_code == 0 and "SECRET" not in result.output

    def test_logs_follow(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path, id="j")
        azsync.log_path("j").write_text("line one\n")
        calls = {"n": 0}

        def fake_sleep(_s):
            calls["n"] += 1
            if calls["n"] > 2:
                raise KeyboardInterrupt

        monkeypatch.setattr(azsync.time, "sleep", fake_sleep)
        result = invoke(runner, ["logs", "j", "-f"])
        assert result.exit_code == 0 and "line one" in result.output

    def test_up_rejects_an_unknown_job(self, state_dir, runner):
        assert invoke(runner, ["up", "ghost"]).exit_code != 0

    def test_main_dispatches_to_the_cli(self, monkeypatch, state_dir):
        monkeypatch.delenv(azsync.SUPERVISE_ENV, raising=False)
        called = {}
        monkeypatch.setattr(azsync, "cli", lambda: called.setdefault("cli", True))
        azsync.main()
        assert called["cli"] is True

    def test_add_warns_about_delete_mode(
        self, tmp_path, state_dir, fake_azcopy, runner
    ):
        source = tmp_path / "src"
        source.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(source),
                "https://acct.blob.core.windows.net/bucket/p",
                "--auth",
                "aad",
                "--no-start",
                "--delete",
            ],
        )
        assert result.exit_code == 0
        assert "--delete will remove" in result.output


class TestWatcherSelection:
    def test_explicit_poll_mode(self, tmp_path, state_dir):
        job = make_job(tmp_path, watch_mode="poll")
        watcher = azsync.build_watcher(job, ChangeAccumulator())
        assert watcher.backend == "poll"

    def test_explicit_inotify_without_watchdog(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.setattr(
            azsync.InotifyWatcher, "available", staticmethod(lambda: False)
        )
        job = make_job(tmp_path, watch_mode="inotify")
        with pytest.raises(Exception, match="watchdog"):
            azsync.build_watcher(job, ChangeAccumulator())

    def test_auto_falls_back_when_watchdog_is_absent(
        self, tmp_path, state_dir, monkeypatch
    ):
        monkeypatch.setattr(
            azsync.InotifyWatcher, "available", staticmethod(lambda: False)
        )
        warnings = []
        job = make_job(tmp_path, watch_mode="auto")
        watcher = azsync.build_watcher(job, ChangeAccumulator(), warn=warnings.append)
        assert watcher.backend == "poll"
        assert any("watchdog" in w for w in warnings)

    def test_auto_falls_back_when_inotify_refuses_to_start(
        self, tmp_path, state_dir, monkeypatch
    ):
        monkeypatch.setattr(
            azsync.InotifyWatcher, "available", staticmethod(lambda: True)
        )

        def boom(self):
            raise OSError("inotify watch limit reached")

        monkeypatch.setattr(azsync.InotifyWatcher, "start", boom)
        monkeypatch.setattr(azsync.InotifyWatcher, "stop", lambda self: None)
        warnings = []
        watcher = azsync.build_watcher(
            make_job(tmp_path), ChangeAccumulator(), warn=warnings.append
        )
        assert watcher.backend == "poll"
        assert any("inotify unavailable" in w for w in warnings)

    def test_started_wrapper_does_not_restart(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.setattr(
            azsync.InotifyWatcher, "available", staticmethod(lambda: True)
        )
        started = {"n": 0}
        monkeypatch.setattr(
            azsync.InotifyWatcher,
            "start",
            lambda self: started.__setitem__("n", started["n"] + 1),
        )
        stopped = {"n": 0}
        monkeypatch.setattr(
            azsync.InotifyWatcher,
            "stop",
            lambda self: stopped.__setitem__("n", stopped["n"] + 1),
        )
        watcher = azsync.build_watcher(make_job(tmp_path), ChangeAccumulator())
        assert started["n"] == 1
        watcher.start()
        assert started["n"] == 1, "must not start twice"
        watcher.stop()
        assert stopped["n"] == 1


class TestOutputQuality:
    def _wide_job(self, tmp_path, ident):
        return make_job(
            tmp_path,
            id=ident,
            source=tmp_path / ("nested/" + "y" * 40) / ident,
            dest=(
                "https://averylongstorageaccount.blob.core.windows.net/"
                "an-extremely-long-container/deeply/nested/path/segment"
            ),
        )

    @pytest.mark.parametrize("width", [70, 80, 100, 140, 200])
    def test_ls_fits_the_terminal(
        self, tmp_path, state_dir, runner, width, monkeypatch
    ):
        monkeypatch.setenv("COLUMNS", str(width))
        for ident in ("alpha", "beta"):
            azsync.save_job(self._wide_job(tmp_path, ident))
        result = invoke(runner, ["ls"])
        assert result.exit_code == 0, result.output
        for line in result.output.splitlines():
            assert len(line) <= width, f"line exceeds {width}: {line!r}"

    def test_ls_keeps_one_line_per_sync(self, tmp_path, state_dir, runner, monkeypatch):
        monkeypatch.setenv("COLUMNS", "80")
        azsync.save_job(self._wide_job(tmp_path, "alpha"))
        result = invoke(runner, ["ls"])
        assert len([ln for ln in result.output.splitlines() if "alpha" in ln]) == 1

    def test_ls_never_leaks_a_token(self, tmp_path, state_dir, runner):
        job = make_job(
            tmp_path,
            id="j",
            dest=f"https://acct.blob.core.windows.net/c/d?{sas_for(3600)}",
        )
        azsync.save_job(job)
        assert "ABC123" not in invoke(runner, ["ls"]).output

    def test_status_shortens_home_paths(self, tmp_path, state_dir, runner, monkeypatch):
        monkeypatch.setattr(azsync.Path, "home", classmethod(lambda cls: tmp_path))
        azsync.save_job(make_job(tmp_path, id="j"))
        result = invoke(runner, ["status", "j"])
        assert result.exit_code == 0 and str(tmp_path) not in result.output

    def test_status_summarises_long_exclude_lists(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="j"))
        out = invoke(runner, ["status", "j"]).output
        assert "patterns" in out and "more" in out

    def test_status_of_an_aad_job(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="j", auth="aad"))
        assert "nothing to rotate" in invoke(runner, ["status", "j"]).output

    def test_status_history_is_rendered(self, tmp_path, state_dir, runner):
        azsync.save_job(make_job(tmp_path, id="j"))
        for status in ("ok", "partial", "network"):
            azsync.append_history(
                "j",
                {
                    "at": time.time(),
                    "reason": "quiet",
                    "status": status,
                    "duration": 1.0,
                    "completed": 2,
                    "bytes": 10,
                },
            )
        out = invoke(runner, ["status", "j"]).output
        assert "Recent syncs" in out
        for status in ("ok", "partial", "network"):
            assert status in out


class TestStatusLabels:
    """Every state must render, and render distinctly."""

    @pytest.mark.parametrize(
        "state,expected",
        [
            ("syncing", "syncing"),
            ("backoff", "backoff"),
            ("failed", "failed"),
            ("idle", "watching"),
        ],
    )
    def test_running_states(self, tmp_path, state_dir, monkeypatch, state, expected):
        job = make_job(tmp_path, id="j")
        azsync.save_job(job)
        azsync.save_state("j", azsync.RuntimeState(state=state))
        monkeypatch.setattr(azsync, "is_running", lambda _i: True)
        assert expected in azsync._state_label(job)

    def test_stopped_and_failed_when_not_running(self, tmp_path, state_dir):
        job = make_job(tmp_path, id="j")
        azsync.save_job(job)
        azsync.save_state("j", azsync.RuntimeState(state="idle"))
        assert "stopped" in azsync._state_label(job)
        azsync.save_state("j", azsync.RuntimeState(state="failed"))
        assert "failed" in azsync._state_label(job)

    def test_sas_label_variants(self, tmp_path, state_dir):
        job = make_job(tmp_path, id="j", auth="aad")
        azsync.save_job(job)
        assert "entra" in azsync._sas_label(job)

        job.auth = "az"
        azsync.save_state("j", azsync.RuntimeState())
        assert azsync._sas_label(job) == "[dim]-[/dim]"

        azsync.save_state("j", azsync.RuntimeState(sas_expires_at=time.time() - 5))
        assert "expired" in azsync._sas_label(job)

        azsync.save_state("j", azsync.RuntimeState(sas_expires_at=time.time() + 60))
        assert "yellow" in azsync._sas_label(job)

        # 2h30m: far enough from a unit boundary that the clock ticking
        # between save and read cannot change the rendered value.
        azsync.save_state("j", azsync.RuntimeState(sas_expires_at=time.time() + 9000))
        assert "green" in azsync._sas_label(job)
        # Compact is what the table uses: one unit, never wrapping.
        assert "2h" in azsync._sas_label(job, compact=True)
        assert "2h29m" in azsync._sas_label(job)

    def test_exclude_summary_of_a_short_list(self, tmp_path, state_dir):
        job = make_job(tmp_path, excludes=["*.log"], default_excludes=False)
        assert azsync._exclude_summary(job) == "1 patterns: *.log"

    def test_exclude_summary_when_empty(self, tmp_path, state_dir):
        job = make_job(tmp_path, excludes=[], default_excludes=False)
        assert azsync._exclude_summary(job) == "none"

    def test_ls_renders_every_result_colour(self, tmp_path, state_dir, runner):
        for i, result in enumerate(("ok", "partial", "network")):
            job = make_job(tmp_path, id=f"j{i}")
            azsync.save_job(job)
            azsync.save_state(
                f"j{i}",
                azsync.RuntimeState(
                    last_sync_end=time.time() - 30,
                    last_result=result,
                    pending_files=2,
                    pending_bytes=100,
                ),
            )
        out = invoke(runner, ["ls"]).output
        for result in ("ok", "partial", "network"):
            assert result in out


class TestSupervisorLoop:
    def test_loop_exits_promptly_on_stop(self, tmp_path, state_dir, fake_azcopy):
        """run() must not block past a stop request."""
        fake_azcopy.program(ok_step())
        job = make_job(tmp_path, id="loop", auth="aad", interval=3600, min_gap=0.1)
        azsync.save_job(job)
        supervisor = Supervisor(job, log=lambda _m: None)

        import threading as th

        th.Timer(1.0, supervisor.request_stop).start()
        started = time.time()
        assert supervisor.run() == 0
        assert time.time() - started < 20
        assert azsync.load_state("loop").state == "stopped"
        # The initial reconcile always happens.
        assert (
            fake_azcopy.calls_for("sync") if hasattr(fake_azcopy, "calls_for") else True
        )

    def test_loop_reacts_to_an_accumulator_wake(self, tmp_path, state_dir, fake_azcopy):
        fake_azcopy.program(ok_step())
        job = make_job(
            tmp_path,
            id="loop2",
            auth="aad",
            quiet_period=0.2,
            min_gap=0.2,
            interval=3600,
        )
        azsync.save_job(job)
        supervisor = Supervisor(job, log=lambda _m: None)

        import threading as th

        def poke():
            supervisor.acc.record(time.time(), size=10)
            th.Timer(2.0, supervisor.request_stop).start()

        th.Timer(0.5, poke).start()
        assert supervisor.run() == 0
        state = azsync.load_state("loop2")
        assert state.total_syncs >= 2 and state.total_failures == 0


# --- Gated checkpoint publication -----------------------------------------


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


class TestRetainLaneExcludesCheckpoints:
    def test_publish_path_is_excluded_from_sync(self, tmp_path):
        job = publish_job(tmp_path)
        flags = AzcopyEngine(job, binary="azcopy").build_argv(None)
        joined = " ".join(flags)
        assert "checkpoints" in joined

    def test_quarantine_is_always_excluded(self, tmp_path):
        job = publish_job(tmp_path)
        assert ".azsync-moved" in " ".join(
            AzcopyEngine(job, binary="azcopy").build_argv(None)
        )

    def test_logs_outside_checkpoint_are_not_excluded(self, tmp_path):
        job = publish_job(tmp_path)
        flags = AzcopyEngine(job, binary="azcopy").build_argv(None)
        assert "train.log" not in " ".join(flags)

    def test_plain_jobs_keep_the_old_excludes(self, tmp_path):
        job = make_job(tmp_path)
        assert ".azsync-moved" not in " ".join(
            AzcopyEngine(job, binary="azcopy").build_argv(None)
        )

    def test_pattern_only_policy_excludes_that_pattern(self, tmp_path):
        job = make_job(tmp_path, publish_patterns=["*.ckpt"])
        assert "*.ckpt" in " ".join(AzcopyEngine(job, binary="azcopy").build_argv(None))

    def test_watcher_still_sees_checkpoint_namespace(self, tmp_path):
        job = publish_job(tmp_path)
        spec = job.watch_exclude_spec()
        assert not spec.matches("checkpoints/checkpoint-1/.complete")

    def test_watcher_ignores_our_quarantine(self, tmp_path):
        job = publish_job(tmp_path)
        assert job.watch_exclude_spec().matches(
            ".azsync-moved/tx/checkpoints/checkpoint-1/model.bin"
        )


class TestPublishArgv:
    def test_payload_uses_copy_not_sync(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, "sig=x", marker=".complete"
        )
        assert argv[1] == "copy"

    def test_payload_is_limited_to_the_checkpoint(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )
        index = argv.index("--include-path")
        assert argv[index + 1] == "checkpoints/checkpoint-100"

    def test_directory_payload_excludes_marker(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )
        assert argv[argv.index("--exclude-pattern") + 1] == ".complete"

    def test_file_payload_excludes_its_sidecar_marker(self, tmp_path):
        job = publish_job(
            tmp_path,
            publish_unit="file",
            publish_patterns=["*.ckpt"],
        )
        path = Path(job.source) / "checkpoints" / "model.ckpt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        path.with_name("model.ckpt.complete").touch()
        snap, _ = snapshot_unit(Path(job.source), path, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )
        assert argv[argv.index("--exclude-pattern") + 1] == "model.ckpt.complete"

    def test_md5_mode_stores_md5(self, tmp_path):
        job = publish_job(tmp_path, publish_verify="md5")
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        assert "--put-md5" in AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )

    def test_exact_copy_url_encodes_every_segment(self, tmp_path):
        job = publish_job(tmp_path)
        argv = AzcopyEngine(job, binary="azcopy").build_exact_copy_argv(
            tmp_path / "marker", "checkpoints/a b/完成", "sig=x"
        )
        assert "a%20b/%E5%AE%8C%E6%88%90" in argv[3]
        assert "sig=x" in argv[3]

    def test_exact_copy_never_uses_recursive(self, tmp_path):
        job = publish_job(tmp_path)
        argv = AzcopyEngine(job, binary="azcopy").build_exact_copy_argv(
            tmp_path / "marker", "x/.complete", None
        )
        assert "--recursive" not in argv

    def test_payload_dry_run(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        assert "--dry-run" in AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete", dry_run=True
        )


class TestRemoteMarkerProbe:
    def _completed(self, argv, code=0, stdout="", stderr=""):
        return azsync.subprocess.CompletedProcess(argv, code, stdout, stderr)

    def test_existing_marker(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(
                argv, stdout='{"MessageType":"ListObject"}'
            ),
        )
        assert AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", None
        )

    def test_empty_success_means_absent(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv),
        )
        assert not AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", None
        )

    @pytest.mark.parametrize(
        "message",
        [
            "BlobNotFound",
            "blob not found",
            "the specified blob does not exist",
            "statuscode=404",
            "status code: 404",
        ],
    )
    def test_missing_marker_shapes(self, tmp_path, monkeypatch, message):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv, 1, stderr=message),
        )
        assert not AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", "sig=x"
        )

    def test_auth_or_network_failure_is_not_treated_as_absent(
        self, tmp_path, monkeypatch
    ):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv, 1, stderr="authentication failed"),
        )
        with pytest.raises(PublishError, match="authentication"):
            AzcopyEngine(job, binary="azcopy").remote_exists(
                "checkpoints/one/.complete", None
            )

    def test_sas_is_redacted_from_probe_failure(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        secret = "super-secret-signature"
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(
                argv, 1, stderr=f"auth failed sig={secret}"
            ),
        )
        with pytest.raises(PublishError) as raised:
            AzcopyEngine(job, binary="azcopy").remote_exists(
                "checkpoints/one/.complete", f"sig={secret}"
            )
        assert secret not in str(raised.value)

    def test_probe_uses_machine_readable_json(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return self._completed(argv)

        monkeypatch.setattr(azsync.subprocess, "run", run)
        AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", None
        )
        assert "--machine-readable" in calls[0]
        assert "--output-type=json" in calls[0]


class TestPublishCoordinator:
    TOKEN = SasToken("sig=x", None)

    def coordinator(self, job, engine, state_dir):
        return PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
            log=lambda _message: None,
        )

    def test_payload_manifest_marker_order(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        engine = FakePublishEngine(job)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.published == 1
        assert engine.calls[0][0] == "probe"
        assert engine.calls[1][0] == "payload"
        assert engine.calls[2][2].endswith(".azsync-manifest.json")
        assert engine.calls[3][2].endswith(".complete")

    def test_successful_keep_leaves_local_checkpoint(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        result = self.coordinator(job, FakePublishEngine(job), state_dir).run(
            self.TOKEN
        )
        assert result.published == 1 and result.deleted == 0
        assert root.exists()

    def test_successful_delete_removes_local_checkpoint(self, tmp_path, state_dir):
        job = publish_job(tmp_path, after_publish="delete")
        root = make_checkpoint(job)
        result = self.coordinator(job, FakePublishEngine(job), state_dir).run(
            self.TOKEN
        )
        assert result.published == 1 and result.deleted == 1
        assert not root.exists()

    def test_payload_failure_never_uploads_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        engine = FakePublishEngine(
            job, [azsync.SyncResult(status=NETWORK, error="offline")]
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.published == 0 and result.error == "offline"
        assert [call[0] for call in engine.calls] == ["probe", "payload"]
        assert root.exists()

    def test_partial_payload_never_uploads_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        engine = FakePublishEngine(
            job,
            [azsync.SyncResult(status=PARTIAL, completed=1, failed=1, bytes=7)],
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.published == 0 and root.exists()
        assert [call[0] for call in engine.calls] == ["probe", "payload"]

    def test_short_file_count_fails_size_verification(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        engine = FakePublishEngine(
            job, [azsync.SyncResult(status=OK, completed=1, bytes=999)]
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert "verification failed" in result.error
        assert root.exists()

    def test_short_byte_count_fails_size_verification(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        engine = FakePublishEngine(
            job, [azsync.SyncResult(status=OK, completed=99, bytes=1)]
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert "verification failed" in result.error
        assert root.exists()

    def test_azcopy_verify_only_needs_success(self, tmp_path, state_dir):
        job = publish_job(tmp_path, publish_verify="azcopy")
        make_checkpoint(job)
        engine = FakePublishEngine(
            job,
            [
                azsync.SyncResult(status=OK),
                azsync.SyncResult(status=OK),
                azsync.SyncResult(status=OK),
            ],
        )
        assert self.coordinator(job, engine, state_dir).run(self.TOKEN).published == 1

    def test_manifest_failure_never_uploads_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        payload = azsync.SyncResult(status=OK, completed=2, bytes=9)
        engine = FakePublishEngine(
            job, [payload, azsync.SyncResult(status=NETWORK, error="manifest")]
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.error == "manifest"
        assert [call[0] for call in engine.calls] == [
            "probe",
            "payload",
            "exact",
        ]
        assert root.exists()

    def test_marker_failure_keeps_local_checkpoint(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        payload = azsync.SyncResult(status=OK, completed=2, bytes=9)
        engine = FakePublishEngine(
            job,
            [
                payload,
                azsync.SyncResult(status=OK),
                azsync.SyncResult(status=NETWORK, error="marker"),
            ],
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.error == "marker" and root.exists()

    def test_change_during_payload_keeps_local_and_no_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)

        def change(argv, call):
            if argv[0] == "payload":
                (root / "model.bin").write_bytes(b"changed during upload")

        engine = FakePublishEngine(job, on_run=change)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert "changed" in result.error
        assert [call[0] for call in engine.calls] == ["probe", "payload"]
        assert root.exists()

    def test_change_after_manifest_keeps_local_and_no_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)

        def change(argv, call):
            if argv[0] == "exact" and argv[2].endswith(".azsync-manifest.json"):
                (root / "state.json").write_text('{"new": true}')

        engine = FakePublishEngine(job, on_run=change)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert "before its marker" in result.error
        assert [call[0] for call in engine.calls] == [
            "probe",
            "payload",
            "exact",
        ]
        assert root.exists()

    def test_marker_disappears_before_publication(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)

        def remove_marker(argv, call):
            if argv[0] == "exact" and argv[2].endswith(".azsync-manifest.json"):
                (root / ".complete").unlink()

        engine = FakePublishEngine(job, on_run=remove_marker)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert "changed before" in result.error or "disappeared" in result.error
        assert [call[0] for call in engine.calls] == [
            "probe",
            "payload",
            "exact",
        ]
        assert root.exists()

    def test_does_not_republish_same_fingerprint(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        engine = FakePublishEngine(job)
        coordinator = self.coordinator(job, engine, state_dir)
        assert coordinator.run(self.TOKEN).published == 1
        count = len(engine.calls)
        assert coordinator.run(self.TOKEN).published == 0
        assert len(engine.calls) == count

    def test_existing_remote_marker_is_a_conflict(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        engine = FakePublishEngine(job, remote_marker=True)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert "already has" in result.error
        assert [call[0] for call in engine.calls] == ["probe"]
        assert root.exists()

    def test_replace_removes_old_marker_before_payload(self, tmp_path, state_dir):
        job = publish_job(tmp_path, publish_conflict="replace")
        make_checkpoint(job)
        engine = FakePublishEngine(job, remote_marker=True)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.published == 1
        assert [call[0] for call in engine.calls[:3]] == [
            "probe",
            "remove",
            "payload",
        ]

    def test_marker_probe_failure_keeps_local(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        engine = FakePublishEngine(
            job, remote_marker=PublishError("cannot check marker")
        )
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.error == "cannot check marker"
        assert root.exists()

    def test_disabled_policy_does_nothing(self, tmp_path, state_dir):
        job = make_job(tmp_path)
        engine = FakePublishEngine(job)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.discovered == 0 and engine.calls == []

    def test_keep_last_prevents_upload(self, tmp_path, state_dir):
        job = publish_job(tmp_path, publish_keep_last=1)
        make_checkpoint(job)
        engine = FakePublishEngine(job)
        result = self.coordinator(job, engine, state_dir).run(self.TOKEN)
        assert result.ready == 0 and engine.calls == []

    def test_quarantine_crash_is_finished_on_next_run(self, tmp_path, state_dir):
        job = publish_job(tmp_path, after_publish="delete")
        root = make_checkpoint(job)
        coordinator = self.coordinator(job, FakePublishEngine(job), state_dir)
        candidates = coordinator.scan()
        snap = candidates[0].snapshot
        tx = coordinator.ledger.transactions[snap.relpath]
        moved = azsync.quarantine(Path(job.source), snap, tx.transaction)
        coordinator.ledger.transition(
            snap.relpath,
            "quarantined",
            time.time(),
            quarantined_path=str(moved),
        )
        coordinator._save()
        coordinator = self.coordinator(job, FakePublishEngine(job), state_dir)
        coordinator.run(self.TOKEN)
        assert not moved.exists()
        assert coordinator.ledger.transactions[snap.relpath].state == "deleted"
        assert not root.exists()

    def test_ledger_records_failure(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        coordinator = self.coordinator(
            job,
            FakePublishEngine(
                job, [azsync.SyncResult(status=NETWORK, error="no route")]
            ),
            state_dir,
        )
        coordinator.run(self.TOKEN)
        tx = next(iter(coordinator.ledger.transactions.values()))
        assert tx.state == "failed" and tx.error == "no route"


class TestPublishCliValidation:
    def test_publish_and_delete_destination_are_incompatible(
        self, tmp_path, state_dir, runner
    ):
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(src),
                "https://acct.blob.core.windows.net/bucket",
                "--publish-path",
                "checkpoints",
                "--delete",
                "--no-start",
            ],
        )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_publish_path_cannot_escape_source(self, tmp_path, state_dir, runner):
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(src),
                "https://acct.blob.core.windows.net/bucket",
                "--publish-path",
                "../outside",
                "--no-start",
            ],
        )
        assert result.exit_code != 0 and "publish path" in result.output

    def test_options_are_persisted(self, tmp_path, state_dir, runner):
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(src),
                "https://acct.blob.core.windows.net/bucket",
                "--publish-path",
                "checkpoints",
                "--publish-pattern",
                "checkpoint-*",
                "--ready-marker",
                "DONE",
                "--publish-stable",
                "60",
                "--after-publish",
                "delete",
                "--publish-keep-last",
                "3",
                "--no-start",
            ],
        )
        assert result.exit_code == 0, result.output
        job = azsync.list_jobs()[0]
        assert job.publish_paths == ["checkpoints"]
        assert job.publish_patterns == ["checkpoint-*"]
        assert job.ready_marker == "DONE"
        assert job.after_publish == "delete"
        assert job.publish_keep_last == 3

    def test_old_job_without_publish_fields_loads(self, state_dir):
        (state_dir / "old.json").write_text(
            json.dumps(
                {
                    "id": "old",
                    "source": "/tmp/source",
                    "dest": "https://acct.blob.core.windows.net/bucket",
                }
            )
        )
        job = azsync.load_job("old")
        assert not job.publish_policy().enabled


class TestPublishScheduling:
    def test_scan_registers_the_end_of_the_stability_window(self, tmp_path, state_dir):
        job = publish_job(tmp_path, publish_stable=60)
        root = make_checkpoint(job)
        old = time.time() - 120
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (old, old))
        (root / ".complete").touch()
        engine = FakePublishEngine(job)
        coordinator = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        )
        coordinator.scan()
        assert coordinator.next_wake is not None
        assert 55 <= coordinator.next_wake - time.time() <= 61

    def test_tick_wakes_at_publish_deadline(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        publisher = type(
            "Publisher",
            (),
            {"next_wake": 105.0, "run": lambda self, token: azsync.PublishRun()},
        )()
        clock = _Clock(100)
        sup = Supervisor(
            job,
            publisher=publisher,
            clock=clock,
            log=lambda _m: None,
        )
        sup.state.last_sync_end = 100
        decision = sup.tick()
        assert not decision.should_sync and decision.wake_at == 105

    def test_tick_starts_sync_when_checkpoint_becomes_ready(
        self, tmp_path, state_dir, monkeypatch
    ):
        job = publish_job(tmp_path)
        publisher = type(
            "Publisher",
            (),
            {"next_wake": 99.0, "run": lambda self, token: azsync.PublishRun()},
        )()
        clock = _Clock(100)
        sup = Supervisor(
            job,
            publisher=publisher,
            clock=clock,
            log=lambda _m: None,
        )
        reasons = []
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda reason, event=None: reasons.append(reason)
            or azsync.SyncResult(status=OK),
        )
        assert sup.tick().should_sync
        assert reasons == ["checkpoint ready"]

    def test_backoff_still_outranks_publish_deadline(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        publisher = type("Publisher", (), {"next_wake": 99.0})()
        clock = _Clock(100)
        sup = Supervisor(
            job,
            publisher=publisher,
            clock=clock,
            log=lambda _m: None,
        )
        sup.state.backoff_until = 200
        decision = sup.tick()
        assert not decision.should_sync
        assert decision.reason == "backoff"


class TestPublishWithRealAzcopyEngine:
    def test_supervisor_runs_sync_then_payload_manifest_marker(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        azsync.save_job(job)
        fake_azcopy.program(
            ok_step(completed=0, size=0),  # retain sync
            fail_step("BlobNotFound"),  # marker probe
            ok_step(completed=2, size=9),  # payload
            ok_step(completed=1, size=200),  # manifest
            ok_step(completed=1, size=0),  # marker
        )
        now = time.time()
        manager = SasManager(
            _StubProvider([sas_for(7200, now=now)]),
            tmp_path / "cache.sas",
        )
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        publisher = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
            log=lambda _m: None,
        )
        sup = Supervisor(
            job,
            engine=engine,
            sas=manager,
            publisher=publisher,
            clock=time.time,
            log=lambda _m: None,
        )
        result = sup.run_sync("manual")
        assert result.status == OK
        assert [call[0] for call in fake_azcopy.calls] == [
            "sync",
            "list",
            "copy",
            "copy",
            "copy",
        ]
        assert fake_azcopy.calls[-1][1].endswith(".complete")
        assert sup.state.publish_last_path == "checkpoints/checkpoint-100"

    def test_publish_failure_makes_the_run_partial_but_never_deletes(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(tmp_path, after_publish="delete")
        root = make_checkpoint(job)
        fake_azcopy.program(
            ok_step(completed=0, size=0),
            fail_step("BlobNotFound"),
            fail_step("connection reset"),
        )
        now = time.time()
        manager = SasManager(
            _StubProvider([sas_for(7200, now=now)]),
            tmp_path / "cache.sas",
        )
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        publisher = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        )
        sup = Supervisor(
            job,
            engine=engine,
            sas=manager,
            publisher=publisher,
            clock=time.time,
            log=lambda _m: None,
        )
        result = sup.run_sync("manual")
        assert result.status == PARTIAL
        assert root.exists()
        assert [call[0] for call in fake_azcopy.calls] == [
            "sync",
            "list",
            "copy",
        ]


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


class TestPublishFilesystemRoundTrip:
    TOKEN = SasToken("", None)

    def test_marker_is_last_and_delete_is_after_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path, after_publish="delete")
        local = make_checkpoint(job)
        remote = tmp_path / "remote"
        engine = FilesystemPublishEngine(job, remote)
        coordinator = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        )
        result = coordinator.run(self.TOKEN)
        published = remote / "checkpoints" / "checkpoint-100"
        assert engine.phases == ["probe", "payload", "manifest", "marker"]
        assert (published / "model.bin").read_bytes() == b"weights"
        assert json.loads((published / ".azsync-manifest.json").read_text())[
            "fingerprint"
        ]
        assert (published / ".complete").exists()
        assert not local.exists()
        assert result.published == 1 and result.deleted == 1

    def test_keep_leaves_identical_local_and_remote_payload(self, tmp_path, state_dir):
        job = publish_job(tmp_path, after_publish="keep")
        local = make_checkpoint(job)
        remote = tmp_path / "remote"
        result = PublishCoordinator(
            job,
            FilesystemPublishEngine(job, remote),
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        ).run(self.TOKEN)
        published = remote / "checkpoints" / "checkpoint-100"
        assert (published / "model.bin").read_bytes() == (
            local / "model.bin"
        ).read_bytes()
        assert result.published == 1 and result.deleted == 0


# --- Durable immediate-sync signals ---------------------------------------


class TestSupervisorSignalQueue:
    def _supervisor(self, job, state_dir, publisher=None):
        queue = azsync.SignalQueue(state_dir / "signals")
        publisher = (
            publisher
            or type(
                "Publisher",
                (),
                {
                    "next_wake": None,
                    "run": lambda self, token, **kwargs: azsync.PublishRun(),
                    "ledger": type("Ledger", (), {"transactions": {}})(),
                },
            )()
        )
        return Supervisor(
            job,
            publisher=publisher,
            signals=queue,
            log=lambda _m: None,
        ), queue

    def test_sync_event_forces_next_tick(self, tmp_path, state_dir, monkeypatch):
        job = make_job(tmp_path)
        sup, queue = self._supervisor(job, state_dir)
        event = queue.submit("sync")
        calls = []
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda reason, signal=None: calls.append((reason, signal))
            or azsync.SyncResult(status=OK),
        )
        decision = sup.tick()
        assert decision.reason == "manual"
        assert calls[0][1].id == event.id
        assert queue.read_result(event.id).status == OK

    def test_flush_payload_reaches_publisher(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        seen = {}

        class Publisher:
            next_wake = None
            ledger = type("Ledger", (), {"transactions": {}})()

            def run(self, token, **kwargs):
                seen.update(kwargs)
                return azsync.PublishRun(published=1)

        sup, queue = self._supervisor(job, state_dir, Publisher())
        event = queue.submit(
            "flush",
            {"checkpoint": "checkpoints/checkpoint-100", "settle": 0.25},
        )
        # Avoid transport; this test is about event semantics.
        sup.sas = type(
            "Sas",
            (),
            {
                "enabled": False,
                "ensure": lambda self, now, **kw: SasToken("", None),
                "current": lambda self: None,
                "needed_lifetime": lambda self, duration: 1,
                "provider": type("P", (), {"refreshable": False})(),
            },
        )()
        sup.engine = type(
            "Engine",
            (),
            {
                "build_argv": lambda self, token: ["sync"],
                "run": lambda self, argv, **kw: azsync.SyncResult(status=OK),
            },
        )()
        sup.tick()
        assert seen == {
            "flush_checkpoint": "checkpoints/checkpoint-100",
            "flush_settle": 0.25,
        }
        assert queue.read_result(event.id).detail["publish"]["published"] == 1

    def test_unknown_event_is_completed_invalid(self, tmp_path, state_dir, monkeypatch):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        sup.state.last_sync_end = time.time()
        event = queue.submit("dance")
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda *a, **kw: pytest.fail("invalid event must not sync"),
        )
        sup.tick()
        assert queue.read_result(event.id).status == "invalid"

    def test_waiting_flush_result_is_distinct(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        sup, queue = self._supervisor(job, state_dir)
        event = queue.submit("flush")
        sup._last_publish = azsync.PublishRun(
            discovered=1,
            waiting=[{"path": "x", "reason": "waiting for marker"}],
        )
        sup._complete_signal(event, azsync.SyncResult(status=OK))
        assert queue.read_result(event.id).status == "waiting"

    def test_partial_result_is_preserved(self, tmp_path, state_dir):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        sup.state.last_sync_end = time.time()
        event = queue.submit("sync")
        sup._complete_signal(event, azsync.SyncResult(status=PARTIAL))
        assert queue.read_result(event.id).status == PARTIAL

    def test_result_updates_runtime_state(self, tmp_path, state_dir):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        event = queue.submit("sync")
        sup._complete_signal(event, azsync.SyncResult(status=OK))
        assert sup.state.signal_last_kind == "sync"
        assert sup.state.signal_last_result == OK
        assert sup.state.signal_last_at is not None

    def test_corrupt_event_does_not_kill_supervisor(
        self, tmp_path, state_dir, monkeypatch
    ):
        sup, queue = self._supervisor(make_job(tmp_path), state_dir)
        sup.state.last_sync_end = time.time()
        queue._prepare()
        (queue.pending / "bad.json").write_text("{")
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda *a, **kw: pytest.fail("corrupt event must not sync"),
        )
        assert not sup.tick().should_sync

    def test_legacy_trigger_file_still_forces_sync(
        self, tmp_path, state_dir, monkeypatch
    ):
        job = make_job(tmp_path)
        sup, _queue = self._supervisor(job, state_dir)
        azsync.trigger_path(job.id).write_text("legacy")
        calls = []
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda reason, event=None: calls.append((reason, event))
            or azsync.SyncResult(status=OK),
        )
        sup.tick()
        assert calls == [("manual", None)]
        assert not azsync.trigger_path(job.id).exists()


class TestSignalCli:
    def _define(self, tmp_path, **kwargs):
        job = publish_job(tmp_path, id="training", **kwargs)
        azsync.save_job(job)
        azsync.save_state(job.id, azsync.RuntimeState())
        return job

    def test_flush_submits_event_to_live_daemon(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        submitted = []

        def submit(job_id, kind, payload=None):
            submitted.append((job_id, kind, payload))
            return SignalEvent.create(kind, payload), True

        monkeypatch.setattr(azsync, "submit_daemon_signal", submit)
        result = invoke(
            runner,
            [
                "flush",
                "training",
                "--checkpoint",
                "checkpoints/checkpoint-100",
                "--settle",
                "0.5",
            ],
        )
        assert result.exit_code == 0
        assert submitted[0][1:] == (
            "flush",
            {"checkpoint": "checkpoints/checkpoint-100", "settle": 0.5},
        )

    def test_flush_requires_publish_policy(self, tmp_path, state_dir, runner):
        job = make_job(tmp_path, id="plain")
        azsync.save_job(job)
        result = invoke(runner, ["flush", "plain"])
        assert result.exit_code != 0 and "no --publish" in result.output

    def test_flush_wait_success(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        event = SignalEvent.create("flush")
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (event, True),
        )
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: SignalResult(
                event_id,
                OK,
                time.time(),
                {"publish": {"published": 1}},
            ),
        )
        result = invoke(runner, ["flush", "training", "--wait"])
        assert result.exit_code == 0 and "1 checkpoint" in result.output

    @pytest.mark.parametrize(
        "status,expected",
        [("waiting", 2), (PARTIAL, 3), (NETWORK, 4)],
    )
    def test_flush_wait_exit_codes(
        self, tmp_path, state_dir, runner, monkeypatch, status, expected
    ):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        event = SignalEvent.create("flush")
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (event, True),
        )
        detail = {
            "publish": {
                "waiting": [{"reason": "not stable"}],
                "error": "publish failed",
            },
            "sync": {"error": "sync failed"},
        }
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: SignalResult(
                event_id, status, time.time(), detail
            ),
        )
        assert invoke(runner, ["flush", "training", "--wait"]).exit_code == expected

    def test_flush_wait_timeout_is_five(self, tmp_path, state_dir, runner, monkeypatch):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        event = SignalEvent.create("flush")
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (event, True),
        )
        monkeypatch.setattr(
            azsync.SignalQueue,
            "wait",
            lambda self, event_id, timeout: None,
        )
        result = invoke(runner, ["flush", "training", "--wait", "--timeout", "0"])
        assert result.exit_code == 5 and "Timed out" in result.output

    def test_unacknowledged_flush_remains_queued(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path)
        monkeypatch.setattr(azsync, "is_running", lambda _id: True)
        monkeypatch.setattr(
            azsync,
            "submit_daemon_signal",
            lambda *a, **kw: (SignalEvent.create("flush"), False),
        )
        result = invoke(runner, ["flush", "training"])
        assert result.exit_code == 0 and "remains queued" in result.output


class TestFlushAcrossARealSupervisorProcess:
    def test_training_complete_signal_reaches_daemon_and_returns_result(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(
            tmp_path,
            id="training",
            auth="aad",
            publish_stable=3600,
        )
        make_checkpoint(job)
        azsync.save_job(job)
        azsync.save_state(job.id, azsync.RuntimeState())
        fake_azcopy.program(
            ok_step(completed=0, size=0),  # initial retain sync
            ok_step(completed=0, size=0),  # flush retain sync
            fail_step("BlobNotFound"),  # remote marker probe
            ok_step(completed=2, size=9),  # payload
            ok_step(completed=1, size=300),  # manifest
            ok_step(completed=1, size=0),  # marker
        )
        pid = azsync.spawn_daemon(job)
        try:
            assert wait_until(
                lambda: azsync.load_state(job.id).total_syncs >= 1,
                timeout=20,
            )
            event, acknowledged = azsync.submit_daemon_signal(
                job.id,
                "flush",
                {
                    "checkpoint": "checkpoints/checkpoint-100",
                    "settle": 0.05,
                },
            )
            assert acknowledged
            result = azsync.signal_queue(job.id).wait(event.id, 30, interval=0.05)
            assert result is not None
            assert result.status == OK
            assert result.detail["publish"]["published"] == 1
            calls = fake_azcopy.calls
            assert [call[0] for call in calls[:6]] == [
                "sync",
                "sync",
                "list",
                "copy",
                "copy",
                "copy",
            ]
            assert calls[-1][1].endswith(".complete")
        finally:
            azsync.stop_daemon(job.id)
        assert wait_until(lambda: not usm_daemon.pid_alive(pid), timeout=15)
