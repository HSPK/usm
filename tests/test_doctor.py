"""Tests for scripts/doctor.py.

Doctor is a registry of small, independent health probes.  These tests stub
all host and command inputs so a laptop, CI container, and GPU server see the
same machine.  Error paths get equal billing because this command is most
valuable when the real host is already unhealthy.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
DOCTOR_PATH = ROOT / "scripts" / "doctor.py"
spec = importlib.util.spec_from_file_location("scripts/doctor.py", DOCTOR_PATH)
assert spec and spec.loader
doctor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = doctor
spec.loader.exec_module(doctor)

Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free")
Mem = namedtuple("Mem", "total available used percent")
Swap = namedtuple("Swap", "total used free percent sin sout")
CTX = doctor.DoctorContext(False, 1.0)
GiB = 1024**3
MiB = 1024**2


@pytest.fixture(autouse=True)
def stable_ui(monkeypatch):
    """Reset lazy Rich consoles so COLUMNS changes are honoured per test."""
    monkeypatch.setattr(doctor.ui, "_console", None)
    monkeypatch.setattr(doctor.ui, "_err_console", None)


def invoke(args, **env):
    return CliRunner().invoke(doctor.cli, args, env={"COLUMNS": "80", **env})


def completed(argv=None, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv or [], code, out, err)


def disk_machine(
    monkeypatch, *, used=50, total=100, f_files=100, f_ffree=50, parts=None
):
    monkeypatch.setattr(
        doctor.psutil,
        "disk_partitions",
        lambda all=False: parts
        if parts is not None
        else [Part("/dev/sda1", "/", "ext4", "")],
    )
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda path: Usage(total, used, max(total - used, 0)),
    )
    monkeypatch.setattr(
        doctor.os,
        "statvfs",
        lambda path: SimpleNamespace(f_files=f_files, f_ffree=f_ffree),
    )


def memory_machine(
    monkeypatch,
    *,
    available=5 * GiB,
    total=10 * GiB,
    swap_total=2 * GiB,
    swap_used=0,
    dmesg=False,
    dmesg_out="",
):
    monkeypatch.setattr(
        doctor.psutil,
        "virtual_memory",
        lambda: Mem(
            total, available, total - available, 100 * (total - available) / total
        ),
    )
    swap_free = max(swap_total - swap_used, 0)
    swap_pct = 0 if swap_total == 0 else 100 * swap_used / swap_total
    monkeypatch.setattr(
        doctor.psutil,
        "swap_memory",
        lambda: Swap(swap_total, swap_used, swap_free, swap_pct, 0, 0),
    )
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/bin/dmesg" if dmesg else None
    )
    monkeypatch.setattr(doctor, "run", lambda *a, **k: completed(a, 0, dmesg_out, ""))


def timedate(
    monkeypatch,
    *,
    present=True,
    code=0,
    out="System clock synchronized: yes\nNTP service: active\n",
    err="",
):
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/bin/timedatectl" if present else None
    )
    monkeypatch.setattr(doctor, "run", lambda *a, **k: completed(a, code, out, err))


def resolv(monkeypatch, text=None, exc=None):
    def fake_read_text(self):
        if str(self) != "/etc/resolv.conf":
            raise OSError("unexpected path")
        if exc:
            raise exc
        return text or ""

    monkeypatch.setattr(doctor.Path, "read_text", fake_read_text)


def route(
    monkeypatch,
    *,
    iface="eth0",
    state="up",
    present=True,
    route_exc=None,
    state_exc=None,
):
    def fake_read_text(self):
        if str(self) == "/proc/net/route":
            if route_exc:
                raise route_exc
            return "Iface Destination Flags\n" + (
                f"{iface} 00000000 0003\n" if present else ""
            )
        if str(self) == f"/sys/class/net/{iface}/operstate":
            if state_exc:
                raise state_exc
            return state
        raise OSError(f"unexpected path {self}")

    monkeypatch.setattr(doctor.Path, "read_text", fake_read_text)


def gpu_machine(
    monkeypatch, *, present=True, code=0, gpu_out="0, A100, 550, 70, 0\n", proc_out=""
):
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/bin/nvidia-smi" if present else None
    )

    def fake_run(argv, timeout=1):
        if "--query-gpu" in argv[1]:
            return completed(argv, code, gpu_out, "nvidia error" if code else "")
        return completed(argv, 0, proc_out, "")

    monkeypatch.setattr(doctor, "run", fake_run)


def services_machine(monkeypatch, *, present=True, responses=None):
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/bin/systemctl" if present else None
    )
    calls = []
    responses = responses or [completed(out=""), completed(out="")]

    def fake_run(argv, timeout=1):
        calls.append(argv)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(doctor, "run", fake_run)
    return calls


class TestRegistryContract:
    """Generic rules that should hold for every current and future check."""

    def test_registry_ids_are_unique_and_ordered(self):
        ids = [check.id for check in doctor.CHECKS]
        assert ids == list(doctor.CHECK_BY_ID)
        assert len(ids) == len(set(ids))

    def test_every_check_has_declarative_metadata(self):
        for check in doctor.CHECKS:
            assert check.id
            assert check.title
            assert check.category
            assert callable(check.func)

    @pytest.mark.parametrize("check", doctor.CHECKS, ids=lambda c: c.id)
    def test_registered_check_returns_valid_result_when_stubbed(
        self, check, monkeypatch
    ):
        stub = doctor.Check(
            check.id,
            check.title,
            check.category,
            lambda ctx: doctor.result("ok", "stub"),
            check.slow,
            check.online_only,
        )
        res = doctor.run_one(stub, CTX)
        assert res.status in {"ok", "warn", "fail", "skip"}
        json.dumps(res.data)

    def test_ls_lists_every_registered_check(self):
        res = invoke(["ls"])
        assert res.exit_code == 0
        for check in doctor.CHECKS:
            assert check.id in res.output
            assert check.category in res.output
            assert check.title in res.output

    @pytest.mark.parametrize("width", [40, 60, 80, 120, 200])
    def test_ls_survives_narrow_and_wide_terminals(self, width):
        res = invoke(["ls"], COLUMNS=str(width))
        assert res.exit_code == 0
        assert "disk" in res.output


class TestSelectionAndOrchestration:
    """CLI selection is the control plane; mistakes here run the wrong probes."""

    def test_only_selects_one_check(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            doctor,
            "run_one",
            lambda check, ctx: calls.append(check.id) or doctor.result("ok", check.id),
        )
        assert invoke(["--only", "disk"]).exit_code == 0
        assert calls == ["disk"]

    def test_skip_removes_one_check(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            doctor,
            "run_one",
            lambda check, ctx: calls.append(check.id) or doctor.result("ok", check.id),
        )
        assert invoke(["--skip", "disk"]).exit_code == 0
        assert "disk" not in calls
        assert len(calls) == len(doctor.CHECKS) - 1

    def test_category_selects_that_group(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            doctor,
            "run_one",
            lambda check, ctx: calls.append(check.id) or doctor.result("ok", check.id),
        )
        assert invoke(["--category", "network"]).exit_code == 0
        assert calls == ["dns", "network"]

    def test_only_skip_and_category_combine(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            doctor,
            "run_one",
            lambda check, ctx: calls.append(check.id) or doctor.result("ok", check.id),
        )
        res = invoke(
            [
                "--only",
                "dns",
                "--only",
                "network",
                "--skip",
                "dns",
                "--category",
                "network",
            ]
        )
        assert res.exit_code == 0
        assert calls == ["network"]

    def test_only_and_skip_same_check_yields_empty_selection(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            doctor,
            "run_one",
            lambda check, ctx: calls.append(check.id) or doctor.result("ok", check.id),
        )
        res = invoke(["--only", "disk", "--skip", "disk"])
        assert res.exit_code == 0
        assert calls == []

    def test_unknown_id_is_clear_error(self):
        res = invoke(["--only", "bogus"])
        assert res.exit_code != 0
        assert "unknown check id: bogus" in res.output

    def test_unknown_skip_id_is_clear_error(self):
        res = invoke(["--skip", "bogus"])
        assert res.exit_code != 0
        assert "unknown check id: bogus" in res.output

    def test_unknown_category_is_clear_error(self):
        res = invoke(["--category", "bogus"])
        assert res.exit_code != 0
        assert "unknown category: bogus" in res.output

    def test_check_exception_is_contained_without_traceback(self):
        checks = [
            doctor.Check(
                "bad",
                "Bad",
                "x",
                lambda ctx: (_ for _ in ()).throw(ValueError("secret boom")),
            ),
            doctor.Check(
                "good", "Good", "x", lambda ctx: doctor.result("ok", "still ran")
            ),
        ]
        results = doctor.run_checks(checks, CTX)
        assert [r.status for _, r in results] == ["fail", "ok"]
        assert "Traceback" not in results[0][1].summary
        assert results[1][1].summary == "still ran"

    def test_timeout_is_contained_and_run_finishes_promptly(self):
        checks = [
            doctor.Check(
                "slow",
                "Slow",
                "x",
                lambda ctx: time.sleep(0.2) or doctor.result("ok", "late"),
            ),
            doctor.Check("fast", "Fast", "x", lambda ctx: doctor.result("ok", "fast")),
        ]
        start = time.monotonic()
        results = doctor.run_checks(checks, doctor.DoctorContext(False, 0.01))
        assert time.monotonic() - start < 0.15
        assert [r.status for _, r in results] == ["warn", "ok"]
        assert "timed out" in results[0][1].summary

    @pytest.mark.parametrize(
        "statuses,strict,code",
        [
            (["ok"], False, 0),
            (["fail"], False, 1),
            (["warn"], False, 0),
            (["warn"], True, 1),
            (["skip"], False, 0),
            ([], False, 0),
        ],
    )
    def test_exit_code_matrix(self, statuses, strict, code):
        checks = [
            doctor.Check(s, s, "x", lambda ctx: doctor.result("ok", ""))
            for s in statuses
        ]
        results = [
            (check, doctor.result(status, status))
            for check, status in zip(checks, statuses)
        ]
        assert doctor.exit_code(results, strict=strict) == code

    def test_deterministic_output_and_order(self, monkeypatch):
        checks = [
            doctor.Check("a", "A", "cat", lambda ctx: doctor.result("ok", "same")),
            doctor.Check("b", "B", "cat", lambda ctx: doctor.result("warn", "same")),
        ]
        monkeypatch.setattr(doctor, "select_checks", lambda *_: checks)
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda checks, ctx: [
                (check, doctor.result("ok", check.id)) for check in checks
            ],
        )
        first = invoke([]).output
        second = invoke([]).output
        assert first == second
        assert first.index("a") < first.index("b")


class TestOfflineGuarantee:
    """Default doctor must be safe on air-gapped or broken hosts."""

    def test_full_report_without_online_never_touches_network(self, monkeypatch):
        stubs = [
            doctor.Check(
                check.id,
                check.title,
                check.category,
                lambda ctx: doctor.result("ok", "stub"),
                check.slow,
                check.online_only,
            )
            for check in doctor.CHECKS
        ]
        monkeypatch.setattr(doctor, "select_checks", lambda *_: stubs)
        monkeypatch.setattr(
            doctor.socket,
            "getaddrinfo",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket used")),
        )
        monkeypatch.setattr(
            doctor.urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("http used")),
        )
        res = invoke([])
        assert res.exit_code == 0


class TestDiskCheck:
    """Disk thresholds are boundary-prone and inode exhaustion is silent."""

    @pytest.mark.parametrize(
        "used,status",
        [
            (50, "ok"),
            (84.9, "ok"),
            (85, "warn"),
            (94.9, "warn"),
            (95, "fail"),
            (96, "fail"),
        ],
    )
    def test_block_usage_boundaries(self, monkeypatch, used, status):
        disk_machine(monkeypatch, used=used, total=100)
        assert doctor.check_disk(CTX).status == status

    def test_inode_exhaustion_warns_with_bytes_free(self, monkeypatch):
        disk_machine(monkeypatch, used=10, total=100, f_files=100, f_ffree=15)
        res = doctor.check_disk(CTX)
        assert res.status == "warn"
        assert res.data["filesystems"][0]["inode_pct"] == 85

    def test_inode_exhaustion_fails_at_95_percent(self, monkeypatch):
        disk_machine(monkeypatch, used=10, total=100, f_files=100, f_ffree=5)
        assert doctor.check_disk(CTX).status == "fail"

    def test_statvfs_failure_warns_not_crashes(self, monkeypatch):
        disk_machine(monkeypatch)
        monkeypatch.setattr(
            doctor.os, "statvfs", lambda path: (_ for _ in ()).throw(OSError("stale"))
        )
        res = doctor.check_disk(CTX)
        assert res.status == "warn"
        assert "stale" in res.details[0]

    def test_zero_size_filesystem_does_not_divide_by_zero(self, monkeypatch):
        disk_machine(monkeypatch, used=0, total=0, f_files=0, f_ffree=0)
        res = doctor.check_disk(CTX)
        assert res.status == "ok"
        assert res.data["filesystems"][0]["used_pct"] == 0

    def test_pseudo_filesystems_are_ignored(self, monkeypatch):
        disk_machine(monkeypatch, parts=[Part("proc", "/proc", "proc", "")])
        assert doctor.check_disk(CTX).status == "skip"

    def test_partition_listing_failure_skips(self, monkeypatch):
        monkeypatch.setattr(
            doctor.psutil,
            "disk_partitions",
            lambda all=False: (_ for _ in ()).throw(RuntimeError("no perms")),
        )
        assert doctor.check_disk(CTX).status == "skip"


class TestMemoryCheck:
    """Memory must distinguish low RAM, swap pressure, and missing probes."""

    def test_healthy_memory_with_no_swap_is_ok(self, monkeypatch):
        memory_machine(monkeypatch, swap_total=0, swap_used=0)
        res = doctor.check_memory(CTX)
        assert res.status == "ok"
        assert res.data["swap_total"] == 0

    def test_low_available_warns(self, monkeypatch):
        memory_machine(monkeypatch, available=600 * MiB, total=10 * GiB)
        assert doctor.check_memory(CTX).status == "warn"

    def test_critically_low_available_fails(self, monkeypatch):
        memory_machine(monkeypatch, available=100 * MiB, total=10 * GiB)
        assert doctor.check_memory(CTX).status == "fail"

    def test_swap_thrashing_warns(self, monkeypatch):
        memory_machine(
            monkeypatch, available=5 * GiB, swap_total=2 * GiB, swap_used=int(1.8 * GiB)
        )
        assert doctor.check_memory(CTX).status == "warn"

    def test_recent_oom_lines_warn(self, monkeypatch):
        memory_machine(
            monkeypatch, dmesg=True, dmesg_out="ordinary\nKilled process 7\n"
        )
        res = doctor.check_memory(CTX)
        assert res.status == "warn"
        assert res.data["oom_events"] == ["Killed process 7"]

    def test_unreadable_dmesg_is_detail_not_failure(self, monkeypatch):
        memory_machine(monkeypatch, dmesg=True)
        monkeypatch.setattr(
            doctor, "run", lambda *a, **k: completed(a, 1, "", "denied")
        )
        res = doctor.check_memory(CTX)
        assert res.status == "ok"
        assert "dmesg unreadable" in res.details

    def test_psutil_failure_skips(self, monkeypatch):
        monkeypatch.setattr(
            doctor.psutil,
            "virtual_memory",
            lambda: (_ for _ in ()).throw(RuntimeError("psutil")),
        )
        assert doctor.check_memory(CTX).status == "skip"


class TestLoadCheck:
    """Load is interpreted relative to CPU count, including odd platforms."""

    @pytest.mark.parametrize(
        "load,cpus,status",
        [
            (3.9, 4, "ok"),
            (4, 4, "ok"),
            (6.1, 4, "warn"),
            (12.1, 4, "fail"),
            (2, None, "warn"),
        ],
    )
    def test_load_thresholds(self, monkeypatch, load, cpus, status):
        monkeypatch.setattr(doctor.os, "getloadavg", lambda: (load, load, load))
        monkeypatch.setattr(doctor.os, "cpu_count", lambda: cpus)
        assert doctor.check_load(CTX).status == status

    def test_platform_without_getloadavg_skips(self, monkeypatch):
        monkeypatch.delattr(doctor.os, "getloadavg", raising=False)
        assert doctor.check_load(CTX).status == "skip"


class TestUptimeCheck:
    """Reboot-required is a Debian marker; uptime itself is best-effort."""

    def test_reboot_marker_absent_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor.Path, "read_text", lambda self: "60 0")
        monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
        assert doctor.check_uptime(CTX).status == "ok"

    def test_reboot_marker_present_warns(self, monkeypatch):
        monkeypatch.setattr(doctor.Path, "read_text", lambda self: "60 0")
        monkeypatch.setattr(
            doctor.Path, "exists", lambda self: str(self) == "/var/run/reboot-required"
        )
        assert doctor.check_uptime(CTX).status == "warn"

    def test_unreadable_uptime_falls_back_to_boot_time(self, monkeypatch):
        monkeypatch.setattr(
            doctor.Path,
            "read_text",
            lambda self: (_ for _ in ()).throw(OSError("no proc")),
        )
        monkeypatch.setattr(doctor.psutil, "boot_time", lambda: time.time() - 120)
        monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
        res = doctor.check_uptime(CTX)
        assert res.status == "ok"
        assert res.data["uptime_seconds"] is not None

    def test_unreadable_everything_still_reports(self, monkeypatch):
        monkeypatch.setattr(
            doctor.Path,
            "read_text",
            lambda self: (_ for _ in ()).throw(OSError("no proc")),
        )
        monkeypatch.setattr(
            doctor.psutil,
            "boot_time",
            lambda: (_ for _ in ()).throw(RuntimeError("no boot")),
        )
        monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
        assert doctor.check_uptime(CTX).status == "ok"


class TestClockCheck:
    """Clock checks must be offline unless explicitly allowed."""

    def test_synced_clock_is_ok(self, monkeypatch):
        timedate(monkeypatch)
        assert doctor.check_clock(CTX).status == "ok"

    def test_not_synced_warns(self, monkeypatch):
        timedate(monkeypatch, out="System clock synchronized: no\n")
        assert doctor.check_clock(CTX).status == "warn"

    def test_timedatectl_absent_skips(self, monkeypatch):
        timedate(monkeypatch, present=False)
        assert doctor.check_clock(CTX).status == "skip"

    def test_timedatectl_nonzero_skips(self, monkeypatch):
        timedate(monkeypatch, code=1, err="no systemd")
        assert doctor.check_clock(CTX).status == "skip"

    def test_timedatectl_garbage_warns(self, monkeypatch):
        timedate(monkeypatch, out="not parseable at all\n")
        assert doctor.check_clock(CTX).status == "warn"

    def test_http_skew_not_checked_offline(self, monkeypatch):
        timedate(monkeypatch)
        monkeypatch.setattr(
            doctor,
            "_http_date_skew",
            lambda timeout: (_ for _ in ()).throw(AssertionError("online used")),
        )
        assert doctor.check_clock(CTX).status == "ok"

    def test_online_skew_warns(self, monkeypatch):
        timedate(monkeypatch)
        monkeypatch.setattr(doctor, "_http_date_skew", lambda timeout: (10.0, None))
        assert doctor.check_clock(doctor.DoctorContext(True, 1)).status == "warn"

    def test_online_skew_failure_warns(self, monkeypatch):
        timedate(monkeypatch)
        monkeypatch.setattr(
            doctor, "_http_date_skew", lambda timeout: (None, "bad date")
        )
        assert doctor.check_clock(doctor.DoctorContext(True, 1)).status == "warn"


class TestDnsCheck:
    """Offline DNS only parses configuration; resolution is online-only."""

    def test_normal_resolv_conf_parses(self, monkeypatch):
        resolv(monkeypatch, "# c\nnameserver 1.1.1.1\nnameserver 8.8.8.8\n")
        res = doctor.check_dns(CTX)
        assert res.status == "ok"
        assert res.data["resolvers"] == ["1.1.1.1", "8.8.8.8"]

    @pytest.mark.parametrize("text", ["", "# only comments\n"])
    def test_empty_or_comment_only_resolv_warns(self, monkeypatch, text):
        resolv(monkeypatch, text)
        assert doctor.check_dns(CTX).status == "warn"

    def test_missing_resolv_warns(self, monkeypatch):
        resolv(monkeypatch, exc=OSError("missing"))
        assert doctor.check_dns(CTX).status == "warn"

    def test_non_utf8_resolv_warns(self, monkeypatch):
        resolv(monkeypatch, exc=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))
        assert doctor.check_dns(CTX).status == "warn"

    def test_offline_does_not_resolve(self, monkeypatch):
        resolv(monkeypatch, "nameserver 1.1.1.1\n")
        monkeypatch.setattr(
            doctor.socket,
            "getaddrinfo",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")),
        )
        assert doctor.check_dns(CTX).status == "ok"

    def test_online_resolution_success(self, monkeypatch):
        resolv(monkeypatch, "nameserver 1.1.1.1\n")
        monkeypatch.setattr(
            doctor.socket,
            "getaddrinfo",
            lambda *a, **k: [(None, None, None, None, ("1.2.3.4", 443))],
        )
        res = doctor.check_dns(doctor.DoctorContext(True, 1))
        assert res.status == "ok"
        assert set(res.data["answers"]) == {"github.com", "pypi.org"}

    def test_online_resolution_failure_fails(self, monkeypatch):
        resolv(monkeypatch, "nameserver 1.1.1.1\n")
        monkeypatch.setattr(
            doctor.socket,
            "getaddrinfo",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no dns")),
        )
        assert doctor.check_dns(doctor.DoctorContext(True, 1)).status == "fail"


class TestNetworkCheck:
    """Default route and link state catch common disconnected remotes."""

    def test_default_route_and_up_interface_ok(self, monkeypatch):
        route(monkeypatch, state="up")
        assert doctor.check_network(CTX).status == "ok"

    def test_missing_default_route_fails(self, monkeypatch):
        route(monkeypatch, present=False)
        assert doctor.check_network(CTX).status == "fail"

    def test_down_interface_fails(self, monkeypatch):
        route(monkeypatch, state="down")
        assert doctor.check_network(CTX).status == "fail"

    def test_unreadable_route_table_fails(self, monkeypatch):
        route(monkeypatch, route_exc=OSError("no ip command/proc route"))
        assert doctor.check_network(CTX).status == "fail"

    def test_unreadable_interface_state_is_unknown_but_not_failure(self, monkeypatch):
        route(monkeypatch, state_exc=OSError("no sysfs"))
        assert doctor.check_network(CTX).status == "ok"


class TestGpuCheck:
    """nvidia-smi output is vendor text; malformed rows must not crash."""

    def test_no_nvidia_smi_skips(self, monkeypatch):
        gpu_machine(monkeypatch, present=False)
        assert doctor.check_gpu(CTX).status == "skip"

    def test_zero_gpus_skips(self, monkeypatch):
        gpu_machine(monkeypatch, gpu_out="")
        assert doctor.check_gpu(CTX).status == "skip"

    def test_healthy_gpu_ok(self, monkeypatch):
        gpu_machine(monkeypatch, proc_out="GPU-1, 123, python, 100\n")
        res = doctor.check_gpu(CTX)
        assert res.status == "ok"
        assert res.data["processes"][0]["pid"] == 123

    def test_high_temperature_warns(self, monkeypatch):
        gpu_machine(monkeypatch, gpu_out="0, A100, 550, 85, 0\n")
        assert doctor.check_gpu(CTX).status == "warn"

    def test_extreme_temperature_fails(self, monkeypatch):
        gpu_machine(monkeypatch, gpu_out="0, A100, 550, 90, 0\n")
        assert doctor.check_gpu(CTX).status == "fail"

    def test_uncorrectable_ecc_fails(self, monkeypatch):
        gpu_machine(monkeypatch, gpu_out="0, A100, 550, 70, 1\n")
        assert doctor.check_gpu(CTX).status == "fail"

    def test_nvidia_smi_nonzero_skips(self, monkeypatch):
        gpu_machine(monkeypatch, code=1, gpu_out="")
        assert doctor.check_gpu(CTX).status == "skip"

    def test_malformed_truncated_csv_skips(self, monkeypatch):
        gpu_machine(monkeypatch, gpu_out="0, only-name\n")
        assert doctor.check_gpu(CTX).status == "skip"

    def test_na_fields_do_not_crash(self, monkeypatch):
        gpu_machine(
            monkeypatch,
            gpu_out="0, A100, [N/A], [N/A], [N/A]\n",
            proc_out="GPU-1, [N/A], proc, [N/A]\n",
        )
        res = doctor.check_gpu(CTX)
        assert res.status == "ok"
        assert res.data["gpus"][0]["temperature_c"] == 0

    def test_process_query_failure_is_detail_only(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: "/bin/nvidia-smi")

        def fake_run(argv, timeout=1):
            if "--query-gpu" in argv[1]:
                return completed(argv, 0, "0, A100, 550, 70, 0\n", "")
            raise OSError("no process query")

        monkeypatch.setattr(doctor, "run", fake_run)
        res = doctor.check_gpu(CTX)
        assert res.status == "ok"
        assert "GPU process query unavailable" in res.details


class TestMountsCheck:
    """Mount probes are bounded because stale NFS/blobfuse can hang stat."""

    def test_healthy_mount_ok(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_mounts_from_mountinfo", lambda: [("/mnt/a", "nfs")]
        )
        monkeypatch.setattr(doctor, "_bounded_stat", lambda path, timeout: (True, None))
        assert doctor.check_mounts(CTX).status == "ok"

    def test_enotconn_mount_fails(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_mounts_from_mountinfo", lambda: [("/mnt/a", "nfs")]
        )
        monkeypatch.setattr(
            doctor,
            "_bounded_stat",
            lambda path, timeout: (False, OSError(errno.ENOTCONN, "stale")),
        )
        assert doctor.check_mounts(CTX).status == "fail"

    def test_eacces_mount_warns(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_mounts_from_mountinfo", lambda: [("/mnt/a", "nfs")]
        )
        monkeypatch.setattr(
            doctor,
            "_bounded_stat",
            lambda path, timeout: (False, OSError(errno.EACCES, "denied")),
        )
        assert doctor.check_mounts(CTX).status == "warn"

    def test_hanging_mount_warns_promptly(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_mounts_from_mountinfo", lambda: [("/mnt/a", "nfs")]
        )
        monkeypatch.setattr(
            doctor, "_bounded_stat", lambda path, timeout: (False, TimeoutError("slow"))
        )
        start = time.monotonic()
        res = doctor.check_mounts(doctor.DoctorContext(False, 0.01))
        assert time.monotonic() - start < 0.1
        assert res.status == "warn"

    def test_no_mounts_skips(self, monkeypatch):
        monkeypatch.setattr(doctor, "_mounts_from_mountinfo", lambda: [])
        assert doctor.check_mounts(CTX).status == "skip"

    def test_mountinfo_parser_ignores_pseudo_filesystems(self):
        text = "1 2 3:4 / /mnt/a rw - nfs server:/x rw\n1 2 3:4 / /run rw - tmpfs tmpfs rw\n"
        assert doctor._mounts_from_mountinfo(
            SimpleNamespace(read_text=lambda: text)
        ) == [("/mnt/a", "nfs")]

    def test_real_bounded_stat_timeout(self, monkeypatch):
        monkeypatch.setattr(doctor.os, "stat", lambda path: time.sleep(0.1))
        ok, exc = doctor._bounded_stat("/mnt/hang", 0.001)
        assert not ok and isinstance(exc, TimeoutError)


class TestServicesCheck:
    """systemctl failures should be reported without requiring systemd in tests."""

    def test_no_failed_units_ok(self, monkeypatch):
        services_machine(monkeypatch)
        assert doctor.check_services(CTX).status == "ok"

    def test_failed_user_unit_fails(self, monkeypatch):
        services_machine(
            monkeypatch,
            responses=[
                completed(out="bad.service loaded failed failed bad\n"),
                completed(out=""),
            ],
        )
        assert doctor.check_services(CTX).status == "fail"

    def test_failed_system_unit_fails(self, monkeypatch):
        services_machine(
            monkeypatch,
            responses=[
                completed(out=""),
                completed(out="sys.service loaded failed failed bad\n"),
            ],
        )
        assert doctor.check_services(CTX).status == "fail"

    def test_systemctl_absent_skips(self, monkeypatch):
        services_machine(monkeypatch, present=False)
        assert doctor.check_services(CTX).status == "skip"

    def test_systemctl_nonzero_without_output_skips(self, monkeypatch):
        services_machine(
            monkeypatch,
            responses=[
                completed(code=2, err="no systemd"),
                completed(code=2, err="no systemd"),
            ],
        )
        assert doctor.check_services(CTX).status == "skip"

    def test_unparseable_output_is_still_reported_as_failed(self, monkeypatch):
        services_machine(
            monkeypatch, responses=[completed(code=0, out="???\n"), completed(out="")]
        )
        res = doctor.check_services(CTX)
        assert res.status == "fail"
        assert res.data["user_failed"] == ["???"]

    def test_systemctl_exception_skips(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: "/bin/systemctl")
        monkeypatch.setattr(
            doctor, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("missing"))
        )
        assert doctor.check_services(CTX).status == "skip"


class TestUsmCheck:
    """usm cache health is about size, venv count, and catalog hash drift."""

    def test_missing_cache_or_catalog_skips(self, tmp_cache):
        assert doctor.check_usm(CTX).status == "skip"

    def test_clean_cache_ok(self, tmp_cache, monkeypatch):
        from usmo.core import catalog, constants
        from usmo.core.model import Script

        constants.CACHE_SCRIPT_DIR.mkdir(parents=True)
        constants.CACHE_ENV_DIR.mkdir(parents=True)
        (constants.CACHE_SCRIPT_DIR / "_config.json").write_text('{"scripts": {}}')
        (constants.CACHE_ENV_DIR / "doctor").mkdir()
        monkeypatch.setattr(
            catalog,
            "load_scripts",
            lambda: {"doctor": Script("doctor", "doctor.py", hash="sha256:x")},
        )
        monkeypatch.setattr(catalog, "script_files_match", lambda script: True)
        res = doctor.check_usm(CTX)
        assert res.status == "ok"
        assert res.data["venv_count"] == 1

    def test_hash_mismatch_fails(self, tmp_cache, monkeypatch):
        from usmo.core import catalog, constants
        from usmo.core.model import Script

        constants.CACHE_SCRIPT_DIR.mkdir(parents=True)
        (constants.CACHE_SCRIPT_DIR / "_config.json").write_text('{"scripts": {}}')
        monkeypatch.setattr(
            catalog,
            "load_scripts",
            lambda: {"doctor": Script("doctor", "doctor.py", hash="sha256:x")},
        )
        monkeypatch.setattr(catalog, "script_files_match", lambda script: False)
        assert doctor.check_usm(CTX).status == "fail"

    def test_catalog_cannot_be_read_warns(self, tmp_cache, monkeypatch):
        from usmo.core import catalog, constants

        constants.CACHE_SCRIPT_DIR.mkdir(parents=True)
        (constants.CACHE_SCRIPT_DIR / "_config.json").write_text('{"scripts": {}}')
        monkeypatch.setattr(
            catalog,
            "load_scripts",
            lambda: (_ for _ in ()).throw(RuntimeError("bad catalog")),
        )
        assert doctor.check_usm(CTX).status == "warn"

    def test_dir_size_ignores_unreadable_files(self):
        unreadable = SimpleNamespace(
            is_file=lambda: True,
            stat=lambda: (_ for _ in ()).throw(OSError("gone")),
        )
        fake_root = SimpleNamespace(rglob=lambda pattern: [unreadable])
        assert doctor._dir_size(fake_root) == 0


class TestPythonCheck:
    """Python environment hints must not depend on the invoking shell."""

    def test_local_bin_on_path_is_ok(self, monkeypatch):
        monkeypatch.setenv("PATH", str(Path.home() / ".local" / "bin"))
        monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
        assert doctor.check_python(CTX).status == "ok"

    def test_local_bin_missing_warns(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
        assert doctor.check_python(CTX).status == "warn"

    def test_externally_managed_marker_is_reported(self, monkeypatch):
        monkeypatch.setenv("PATH", str(Path.home() / ".local" / "bin"))
        monkeypatch.setattr(
            doctor.Path, "exists", lambda self: str(self).endswith("EXTERNALLY-MANAGED")
        )
        res = doctor.check_python(CTX)
        assert res.status == "ok"
        assert res.data["externally_managed"] is True

    def test_modern_version_and_executable_are_structured(self, monkeypatch):
        monkeypatch.setenv("PATH", str(Path.home() / ".local" / "bin"))
        monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
        res = doctor.check_python(CTX)
        assert res.data["version"]
        assert res.data["executable"]


class TestJsonAndOutput:
    """Machine and human outputs have different contracts and both matter."""

    def test_json_shape_and_types_for_all_statuses(self, monkeypatch):
        checks = [
            doctor.Check(
                s,
                s.title(),
                "cat",
                lambda ctx, s=s: doctor.result(
                    s, f"{s} summary", [f"{s} detail"], number=1
                ),
            )
            for s in ["ok", "warn", "fail", "skip"]
        ]
        monkeypatch.setattr(doctor, "select_checks", lambda *_: checks)
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda checks, ctx: [(check, check.func(ctx)) for check in checks],
        )
        res = invoke(["--json"])
        assert res.exit_code == 1
        payload = json.loads(res.output)
        assert list(payload) == ["checks"]
        assert [item["status"] for item in payload["checks"]] == [
            "ok",
            "warn",
            "fail",
            "skip",
        ]
        for item in payload["checks"]:
            assert isinstance(item["id"], str)
            assert isinstance(item["details"], list)
            assert isinstance(item["data"], dict)

    def test_default_hides_ok_details(self, monkeypatch):
        check = doctor.Check(
            "x", "X", "cat", lambda ctx: doctor.result("ok", "fine", ["hidden detail"])
        )
        monkeypatch.setattr(doctor, "select_checks", lambda *_: [check])
        monkeypatch.setattr(
            doctor, "run_checks", lambda checks, ctx: [(check, check.func(ctx))]
        )
        assert "hidden detail" not in invoke([]).output

    def test_verbose_shows_ok_details(self, monkeypatch):
        check = doctor.Check(
            "x", "X", "cat", lambda ctx: doctor.result("ok", "fine", ["shown detail"])
        )
        monkeypatch.setattr(doctor, "select_checks", lambda *_: [check])
        monkeypatch.setattr(
            doctor, "run_checks", lambda checks, ctx: [(check, check.func(ctx))]
        )
        assert "shown detail" in invoke(["-v"]).output

    def test_warn_and_fail_details_show_by_default(self, monkeypatch):
        checks = [
            doctor.Check(
                "w",
                "W",
                "cat",
                lambda ctx: doctor.result("warn", "warn", ["warn detail"]),
            ),
            doctor.Check(
                "f",
                "F",
                "cat",
                lambda ctx: doctor.result("fail", "fail", ["fail detail"]),
            ),
        ]
        monkeypatch.setattr(doctor, "select_checks", lambda *_: checks)
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda checks, ctx: [(check, check.func(ctx)) for check in checks],
        )
        out = invoke([]).output
        assert "warn detail" in out
        assert "fail detail" in out

    @pytest.mark.parametrize("width", [40, 60, 80, 120, 200])
    def test_report_survives_terminal_widths(self, monkeypatch, width):
        check = doctor.Check(
            "x",
            "Long Check",
            "cat",
            lambda ctx: doctor.result("ok", "fine", ["detail line"]),
        )
        monkeypatch.setattr(doctor, "select_checks", lambda *_: [check])
        monkeypatch.setattr(
            doctor, "run_checks", lambda checks, ctx: [(check, check.func(ctx))]
        )
        res = invoke(["-v"], COLUMNS=str(width))
        assert res.exit_code == 0
        assert "x" in res.output
