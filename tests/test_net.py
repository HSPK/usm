"""Tests for pure helpers and CLI wiring in scripts/net.py."""

from __future__ import annotations

import pytest

import net


class TestFmtBytes:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (0, "0.0B"),
            (512, "512.0B"),
            (1024, "1.0KiB"),
            (1024 * 1024, "1.0MiB"),
            (1024**3, "1.0GiB"),
        ],
    )
    def test_fmt_bytes(self, n, expected):
        assert net.fmt_bytes(n) == expected

    def test_fmt_rate_suffix(self):
        assert net.fmt_rate(2048).endswith("/s")
        assert net.fmt_rate(2048) == "2.0KiB/s"


class TestInterfaces:
    def test_returns_ifaces(self):
        ifaces = net.interfaces()
        assert isinstance(ifaces, list) and ifaces
        for i in ifaces:
            assert isinstance(i.name, str)
            assert isinstance(i.up, bool)
            assert isinstance(i.ipv4, list)

    def test_loopback_present(self):
        # Loopback exists on every Linux host / CI runner.
        assert any(i.name == "lo" for i in net.interfaces())


class TestDnsServers:
    def test_returns_unique_list(self):
        servers = net.dns_servers()
        assert isinstance(servers, list)
        assert len(servers) == len(set(servers))  # deduped


class TestCli:
    def test_commands_present(self):
        cmds = set(net.cli.commands)
        assert {
            "ls",
            "addr",
            "routes",
            "dns",
            "neigh",
            "conns",
            "fw",
            "ping",
            "trace",
            "lookup",
            "mtu",
            "pubip",
            "speed",
        } <= cmds

    def test_sections_reference_real_commands(self):
        cmds = set(net.cli.commands)
        for _title, names in net.COMMAND_SECTIONS:
            assert set(names) <= cmds
