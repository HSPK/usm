"""Tests for pure helpers and CLI wiring in scripts/session.py."""

from __future__ import annotations

import time

import pytest

import session


class TestFmtDur:
    @pytest.mark.parametrize(
        "secs,expected",
        [
            (None, "?"),
            (-5, "0s"),
            (0, "0s"),
            (45, "45s"),
            (90, "1m"),
            (3600, "1h00m"),
            (3660, "1h01m"),
            (90000, "1d01h"),
        ],
    )
    def test_fmt_dur(self, secs, expected):
        assert session.fmt_dur(secs) == expected


class TestFmtLogin:
    def test_empty(self):
        assert session.fmt_login(0) == "-"

    def test_today_is_hh_mm(self):
        out = session.fmt_login(time.time())
        assert len(out) == 5 and out[2] == ":"

    def test_past_has_month(self):
        past = time.mktime((2020, 3, 14, 9, 5, 0, 0, 0, -1))
        out = session.fmt_login(past)
        assert "Mar" in out and "09:05" in out


class TestClassify:
    @pytest.mark.parametrize(
        "host,kind,remote",
        [
            ("", "tty", ""),
            (":0", "tty", ""),
            ("tmux(2099372).%5", "tmux", ""),
            ("screen", "tmux", ""),
            ("100.105.81.34", "ssh", "100.105.81.34"),
            ("client.example.com", "ssh", "client.example.com"),
        ],
    )
    def test_classify(self, host, kind, remote):
        assert session._classify(host) == (kind, remote)


class TestNormTty:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("3", "pts/3"),
            ("pts/3", "pts/3"),
            ("/dev/pts/3", "pts/3"),
            ("tty1", "tty1"),
            ("/dev/tty1", "tty1"),
        ],
    )
    def test_norm_tty(self, value, expected):
        assert session._norm_tty(value) == expected


class TestCli:
    def test_commands_present(self):
        cmds = set(session.cli.commands)
        assert {
            "ls",
            "ssh",
            "mux",
            "history",
            "me",
            "kill",
            "logout",
            "msg",
            "lock",
            "unlock",
            "watch",
        } <= cmds

    def test_sections_reference_real_commands(self):
        cmds = set(session.cli.commands)
        for _title, names in session.COMMAND_SECTIONS:
            assert set(names) <= cmds
