"""Tests for pure helpers and CLI wiring in scripts/disk.py."""

from __future__ import annotations

import pytest

import disk


def mkdev(**over) -> disk.Dev:
    """Build a Dev with sensible blank defaults; override per test."""
    base = dict(
        name="sdz",
        path="/dev/sdz",
        type="disk",
        size=1000,
        fstype=None,
        fssize=None,
        fsused=None,
        fsavail=None,
        fsusep=None,
        mountpoint=None,
        label=None,
        partlabel=None,
        uuid=None,
        model=None,
        vendor=None,
        serial=None,
        rota=None,
        tran=None,
        pttype=None,
        rm=False,
        ro=False,
        hotplug=False,
        pkname=None,
        children=[],
    )
    base.update(over)
    return disk.Dev(**base)


class TestFmtBytes:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (None, "0B"),
            (0, "0B"),
            (512, "512B"),
            (1024, "1.0KiB"),
            (1024 * 1024, "1.0MiB"),
            (1024**3, "1.0GiB"),
            (1024**4, "1.0TiB"),
        ],
    )
    def test_fmt_bytes(self, n, expected):
        assert disk.fmt_bytes(n) == expected


class TestInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, 0),
            (5, 5),
            ("42", 42),
            ("  7 ", 7),
            ("nope", 0),
            (True, 0),
        ],
    )
    def test_int(self, value, expected):
        assert disk._int(value) == expected


class TestToDev:
    def test_parses_nested_tree(self):
        raw = {
            "name": "sdb",
            "path": "/dev/sdb",
            "type": "disk",
            "size": 1000,
            "model": " Virtual Disk ",
            "vendor": "Msft    ",
            "children": [
                {
                    "name": "sdb1",
                    "path": "/dev/sdb1",
                    "type": "part",
                    "size": "900",
                    "fstype": "ext4",
                    "fssize": "800",
                    "fsused": "200",
                    "fsuse%": "25%",
                    "mountpoint": "/mnt",
                }
            ],
        }
        dev = disk._to_dev(raw)
        assert dev.name == "sdb" and dev.size == 1000
        assert dev.model == "Virtual Disk"  # stripped
        assert dev.vendor == "Msft"
        assert len(dev.children) == 1
        child = dev.children[0]
        assert child.type == "part" and child.size == 900
        assert child.fssize == 800 and child.fsused == 200
        assert child.mountpoint == "/mnt"

    def test_blank_strings_become_none(self):
        dev = disk._to_dev({"name": "x", "model": "   ", "fstype": None})
        assert dev.model is None and dev.fstype is None


class TestIter:
    def test_flattens_depth_first(self):
        tree = mkdev(
            name="sdb",
            children=[mkdev(name="sdb1", type="part"), mkdev(name="sdb2", type="part")],
        )
        names = [d.name for d in disk._iter([tree])]
        assert names == ["sdb", "sdb1", "sdb2"]


class TestPct:
    def test_from_percent_string(self):
        assert disk._pct(mkdev(fsusep="37%")) == 37

    def test_computed_from_sizes(self):
        assert disk._pct(mkdev(fssize=1000, fsused=250)) == 25

    def test_none_when_unknown(self):
        assert disk._pct(mkdev()) is None


class TestResolveFs:
    def test_aliases(self):
        assert disk.FS_ALIASES["ext"] == "ext4"
        assert disk.FS_ALIASES["fat"] == "vfat"
        assert disk.FS_ALIASES["msdos"] == "vfat"

    def test_unsupported_raises(self):
        with pytest.raises(disk.click.ClickException):
            disk._resolve_fs("zzz")

    def test_alias_resolves_when_available(self, monkeypatch):
        monkeypatch.setattr(disk, "_have", lambda _c: True)
        assert disk._resolve_fs("fat") == "vfat"
        assert disk._resolve_fs("EXT4") == "ext4"

    def test_missing_binary_raises(self, monkeypatch):
        monkeypatch.setattr(disk, "_have", lambda _c: False)
        with pytest.raises(disk.click.ClickException):
            disk._resolve_fs("ext4")


class TestMkfsArgv:
    def test_ext4_with_label(self):
        assert disk._mkfs_argv("ext4", "/dev/x", "data") == [
            "mkfs.ext4",
            "-F",
            "-L",
            "data",
            "/dev/x",
        ]

    def test_vfat_label_upper_truncated(self):
        argv = disk._mkfs_argv("vfat", "/dev/x", "longlabelname")
        assert argv == ["mkfs.vfat", "-n", "LONGLABELNA", "/dev/x"]

    def test_fat32_extra_flags(self):
        assert disk._mkfs_argv("fat32", "/dev/x", None) == [
            "mkfs.vfat",
            "-F",
            "32",
            "/dev/x",
        ]


class TestDefaultMountpoint:
    @pytest.mark.parametrize(
        "dev,expected",
        [
            (mkdev(name="sdb1"), "/mnt/sdb1"),
            (mkdev(name="sdb1", label="My Data!"), "/mnt/My_Data_"),
            (mkdev(name="sdb1", partlabel="pl"), "/mnt/pl"),
        ],
    )
    def test_default_mountpoint(self, dev, expected):
        assert disk._default_mountpoint(dev) == expected


class TestGuards:
    def test_protected_mounts_root(self):
        disk_dev = mkdev(
            name="sda",
            children=[mkdev(name="sda1", type="part", mountpoint="/")],
        )
        assert disk._protected_mounts(disk_dev) == ["/"]

    def test_protected_mounts_swap(self):
        assert disk._protected_mounts(mkdev(fstype="swap")) == ["[SWAP]"]

    def test_mounted_targets(self):
        d = mkdev(
            name="sdb",
            children=[mkdev(name="sdb1", type="part", mountpoint="/mnt")],
        )
        assert disk._mounted_targets(d) == [("sdb1", "/mnt")]

    def test_guard_refuses_system_disk(self):
        d = mkdev(children=[mkdev(name="p1", type="part", mountpoint="/boot")])
        with pytest.raises(disk.click.ClickException, match="system disk"):
            disk._guard_destructive(d, "format", force=True)

    def test_guard_refuses_mounted(self):
        d = mkdev(mountpoint="/data")
        with pytest.raises(disk.click.ClickException, match="in use"):
            disk._guard_destructive(d, "format", force=True)

    def test_guard_requires_force_for_existing_fs(self):
        d = mkdev(fstype="ext4")
        with pytest.raises(disk.click.ClickException, match="force"):
            disk._guard_destructive(d, "format", force=False)
        # With force it passes (returns None).
        assert disk._guard_destructive(d, "format", force=True) is None

    def test_guard_requires_force_for_existing_partitions(self):
        d = mkdev(children=[mkdev(name="p1", type="part")])
        with pytest.raises(disk.click.ClickException, match="force"):
            disk._guard_destructive(d, "partition", force=False)

    def test_guard_allows_blank_disk(self):
        assert disk._guard_destructive(mkdev(), "partition", force=False) is None


class TestMakeSinglePartition:
    def _capture(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(disk, "_have", lambda _c: False)
        monkeypatch.setattr(
            disk, "_must_run", lambda argv, **k: calls.append(argv) or ""
        )
        return calls

    def test_gpt_uses_partition_name(self, monkeypatch):
        calls = self._capture(monkeypatch)
        disk._make_single_partition(mkdev(path="/dev/sdz"), "gpt", "data")
        mkpart = next(c for c in calls if "mkpart" in c)
        assert mkpart[mkpart.index("mkpart") + 1] == "data"
        mklabel = next(c for c in calls if "mklabel" in c)
        assert mklabel[-1] == "gpt"

    def test_mbr_forces_primary(self, monkeypatch):
        calls = self._capture(monkeypatch)
        disk._make_single_partition(mkdev(path="/dev/sdz"), "mbr", "data")
        mkpart = next(c for c in calls if "mkpart" in c)
        assert mkpart[mkpart.index("mkpart") + 1] == "primary"
        mklabel = next(c for c in calls if "mklabel" in c)
        assert mklabel[-1] == "msdos"


class TestFstab:
    @pytest.fixture
    def fstab(self, tmp_path, monkeypatch):
        path = tmp_path / "fstab"
        path.write_text("# header\nUUID=keep\t/keep\text4\tdefaults\t0\t2\n")
        monkeypatch.setattr(disk, "FSTAB", str(path))
        return path

    def test_spec_prefers_uuid(self):
        assert disk._fstab_spec(mkdev(uuid="abc")) == "UUID=abc"
        assert disk._fstab_spec(mkdev(path="/dev/sdz")) == "/dev/sdz"

    def test_add_entry_by_uuid(self, fstab):
        dev = mkdev(path="/dev/sdz", uuid="new-uuid", fstype="ext4")
        disk._add_fstab(dev, "/data", None)
        text = fstab.read_text()
        assert "UUID=new-uuid\t/data\text4\tdefaults\t0\t2" in text
        assert "UUID=keep" in text  # untouched

    def test_add_dedups_same_mountpoint(self, fstab):
        dev = mkdev(uuid="u1", fstype="ext4")
        disk._add_fstab(dev, "/data", None)
        dev2 = mkdev(uuid="u2", fstype="xfs")
        disk._add_fstab(dev2, "/data", None)
        lines = [ln for ln in fstab.read_text().splitlines() if "/data" in ln]
        assert len(lines) == 1 and "u2" in lines[0]

    def test_remove_entry(self, fstab):
        disk._remove_fstab("UUID=keep", "/keep")
        assert "UUID=keep" not in fstab.read_text()


class TestCli:
    def test_commands_present(self):
        cmds = set(disk.cli.commands)
        assert {
            "ls",
            "info",
            "usage",
            "fstab",
            "partition",
            "format",
            "wipe",
            "mount",
            "unmount",
            "setup",
        } <= cmds

    def test_umount_alias_present(self):
        assert "umount" in disk.cli.commands

    def test_sections_reference_real_commands(self):
        cmds = set(disk.cli.commands)
        for _title, names in disk.COMMAND_SECTIONS:
            assert set(names) <= cmds
