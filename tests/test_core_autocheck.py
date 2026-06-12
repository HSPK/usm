"""Tests for the auto-check pipeline in usmo.core."""

from __future__ import annotations

import json
import os
import time

import pytest

from usmo import core


class TestAutoCheckInterval:
    @pytest.mark.parametrize(
        "env_val,expected",
        [
            (None, core.DEFAULT_AUTO_CHECK_INTERVAL),
            ("", core.DEFAULT_AUTO_CHECK_INTERVAL),
            ("0", 0),
            ("3600", 3600),
            ("garbage", core.DEFAULT_AUTO_CHECK_INTERVAL),
            ("-100", 0),
        ],
    )
    def test_interval(self, env_val, expected, monkeypatch):
        if env_val is None:
            monkeypatch.delenv(core.AUTO_CHECK_ENV, raising=False)
        else:
            monkeypatch.setenv(core.AUTO_CHECK_ENV, env_val)
        assert core.auto_check_interval() == expected


class TestShouldAutoCheck:
    def test_no_last_check_returns_true(self, tmp_cache, monkeypatch):
        monkeypatch.delenv(core.AUTO_CHECK_ENV, raising=False)
        assert core.should_auto_check() is True

    def test_fresh_returns_false(self, tmp_cache, monkeypatch):
        monkeypatch.delenv(core.AUTO_CHECK_ENV, raising=False)
        core.mark_checked()
        assert core.should_auto_check() is False

    def test_stale_returns_true(self, tmp_cache, monkeypatch):
        monkeypatch.delenv(core.AUTO_CHECK_ENV, raising=False)
        core.mark_checked()
        old = time.time() - core.DEFAULT_AUTO_CHECK_INTERVAL - 100
        os.utime(core.LAST_CHECK_FILE, (old, old))
        assert core.should_auto_check() is True

    def test_interval_zero_disables(self, tmp_cache, monkeypatch):
        monkeypatch.setenv(core.AUTO_CHECK_ENV, "0")
        assert core.should_auto_check() is False


class TestMarkChecked:
    def test_creates_file(self, tmp_cache):
        assert not core.LAST_CHECK_FILE.exists()
        core.mark_checked()
        assert core.LAST_CHECK_FILE.exists()

    def test_updates_mtime(self, tmp_cache):
        core.mark_checked()
        first = core.LAST_CHECK_FILE.stat().st_mtime
        time.sleep(0.05)
        core.mark_checked()
        assert core.LAST_CHECK_FILE.stat().st_mtime > first


def _seed_cache_versions(versions: dict[str, str]) -> None:
    core.CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "scripts": {n: {"path": f"{n}.sh", "version": v} for n, v in versions.items()}
    }
    (core.CACHE_SCRIPT_DIR / core.CONFIG_FILENAME).write_text(json.dumps(data))


class TestCheckForUpdate:
    def test_cold_cache_returns_none(self, tmp_cache, monkeypatch):
        called: list[int] = []
        monkeypatch.setattr(
            core.updates,
            "fetch_remote_script_versions",
            lambda *a, **k: called.append(1),
        )
        assert core.check_for_update(force=True) is None
        assert not called  # never even reached the network

    def test_marks_checked_on_cold_cache(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(
            core.updates, "fetch_remote_script_versions", lambda *a, **k: None
        )
        assert not core.LAST_CHECK_FILE.exists()
        core.check_for_update(force=True)
        assert core.LAST_CHECK_FILE.exists()

    def test_matching_versions_returns_empty_list(self, tmp_cache, monkeypatch):
        _seed_cache_versions({"foo": "1.0.0", "bar": "2.0.0"})
        monkeypatch.setattr(
            core.updates,
            "fetch_remote_script_versions",
            lambda *a, **k: {"foo": "1.0.0", "bar": "2.0.0"},
        )
        assert core.check_for_update(force=True) == []

    def test_per_script_diff_added_removed_bumped(self, tmp_cache, monkeypatch):
        _seed_cache_versions({"foo": "1.0.0", "bar": "2.0.0", "baz": "1.0.0"})
        monkeypatch.setattr(
            core.updates,
            "fetch_remote_script_versions",
            lambda *a, **k: {"foo": "1.1.0", "baz": "1.0.0", "qux": "1.0.0"},
        )
        diffs = core.check_for_update(force=True)
        by_name = {d.name: (d.local_version, d.remote_version) for d in diffs}
        assert by_name == {
            "foo": ("1.0.0", "1.1.0"),  # bumped
            "bar": ("2.0.0", None),  # removed remotely
            "qux": (None, "1.0.0"),  # new remotely
        }

    def test_network_failure_returns_none(self, tmp_cache, monkeypatch):
        _seed_cache_versions({"foo": "1.0.0"})
        monkeypatch.setattr(
            core.updates, "fetch_remote_script_versions", lambda *a, **k: None
        )
        assert core.check_for_update(force=True) is None

    def test_marks_checked_on_network_failure(self, tmp_cache, monkeypatch):
        _seed_cache_versions({"foo": "1.0.0"})
        monkeypatch.setattr(
            core.updates, "fetch_remote_script_versions", lambda *a, **k: None
        )
        core.check_for_update(force=True)
        assert core.LAST_CHECK_FILE.exists()

    def test_throttle_skips_when_not_due(self, tmp_cache, monkeypatch):
        monkeypatch.delenv(core.AUTO_CHECK_ENV, raising=False)
        _seed_cache_versions({"foo": "1.0.0"})
        core.mark_checked()  # fresh → next probe must be skipped
        called: list[int] = []
        monkeypatch.setattr(
            core.updates,
            "fetch_remote_script_versions",
            lambda *a, **k: called.append(1),
        )
        assert core.check_for_update() is None
        assert not called
