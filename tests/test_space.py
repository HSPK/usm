"""Tests for scripts/space.py.

The space tool is allowed to delete files, so these tests bias toward safety
properties rather than line coverage: every reclaim path is redirected into a
throwaway tree, every external tool is faked, and an autouse deletion guard
fails the test if a regression attempts to unlink anything outside pytest's
sandbox.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import click.testing
import pytest

import space


# --- Fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def hermetic_world(tmp_path, monkeypatch):
    """No test may use the real home, real external tools, or real apt cache."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(space.shutil, "which", lambda _name: None)
    monkeypatch.setenv("COLUMNS", "80")

    real_exists = Path.exists
    real_is_symlink = Path.is_symlink

    def exists(path: Path) -> bool:
        if str(path).startswith("/var/cache/apt"):
            return False
        return real_exists(path)

    def is_symlink(path: Path) -> bool:
        if str(path).startswith("/var/cache/apt"):
            return False
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    return home


@pytest.fixture(autouse=True)
def deletion_guard(tmp_path, monkeypatch):
    """Catch the class of bug that matters most: deleting outside tmp_path."""
    allowed = tmp_path.resolve()
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir

    def inside(path: Path) -> bool:
        try:
            return path.resolve(strict=False).is_relative_to(allowed)
        except OSError:
            return False

    def guarded_unlink(path: Path, *args, **kwargs):
        if not inside(path):
            pytest.fail(f"attempted to unlink outside test sandbox: {path}")
        return real_unlink(path, *args, **kwargs)

    def guarded_rmdir(path: Path, *args, **kwargs):
        if not inside(path):
            pytest.fail(f"attempted to rmdir outside test sandbox: {path}")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    monkeypatch.setattr(Path, "rmdir", guarded_rmdir)


@pytest.fixture
def runner():
    return click.testing.CliRunner()


@pytest.fixture
def fake_home(hermetic_world):
    return hermetic_world


def invoke(runner, args, **kwargs):
    return runner.invoke(space.cli, args, catch_exceptions=False, **kwargs)


def write(path: Path, size: int = 4, byte: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(byte * size)
    return path


def one_cache(name: str, path: Path, *, requires_sudo: bool = False) -> space.Cache:
    return space.Cache(
        name,
        f"{name} cache",
        lambda: space._paths_size(space._existing([path])),
        space.remove_paths(lambda: [path]),
        lambda: [path],
        requires_sudo=requires_sudo,
    )


def patch_registry(monkeypatch, caches: list[space.Cache]) -> None:
    monkeypatch.setattr(space, "cache_registry", lambda pycache_root=None: caches)


def expected_size(root: Path) -> int:
    seen: set[tuple[int, int]] = set()

    def walk(path: Path) -> int:
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return 0
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            return 0
        seen.add(key)
        total = stat.st_size
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.iterdir(), key=lambda item: str(item)):
                total += walk(child)
        return total

    return walk(root)


# --- Deletion safety -------------------------------------------------------


class TestReclaimRefusesDangerousTargets:
    """Known-cache deletion must never turn into arbitrary path deletion."""

    @pytest.mark.parametrize(
        "target",
        [
            Path("/"),
            Path("/etc"),
            Path("/usr"),
            Path("/var"),
            Path("relative"),
            Path(""),
        ],
    )
    def test_safe_delete_rejects_system_home_relative_and_empty_paths(
        self, target, fake_home
    ):
        assert not space._safe_to_delete(target, [target])

    def test_safe_delete_rejects_home_itself(self, fake_home):
        assert not space._safe_to_delete(fake_home, [fake_home])

    def test_reclaim_refuses_injected_root_cache(self, runner, tmp_path, monkeypatch):
        cache = one_cache("bad", Path("/"))
        patch_registry(monkeypatch, [cache])

        result = invoke(runner, ["reclaim", "--only", "bad", "--yes"])

        assert result.exit_code == 0
        assert "refused dangerous path" in result.output
        assert "freed 0B" in result.output

    def test_reclaim_refuses_injected_etc_usr_var_and_relative_caches(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        Path("relative").mkdir()
        relative = space.Cache(
            "rel",
            "relative",
            lambda: space.SizeResult(1),
            space.remove_paths(lambda: [Path("relative")]),
            lambda: [Path("relative")],
        )
        caches = [
            one_cache("etc", Path("/etc")),
            one_cache("usr", Path("/usr")),
            one_cache("var", Path("/var")),
            relative,
        ]
        patch_registry(monkeypatch, caches)

        result = invoke(runner, ["reclaim", "--yes"])

        assert result.exit_code == 0
        assert result.output.count("refused dangerous path") == 4
        assert "freed 0B" in result.output

    def test_hf_home_set_to_root_is_refused(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", "/")
        root_marker = write(tmp_path / "decoy" / "hub" / "model", 8)
        cache = one_cache("huggingface", space._hf_hub_path())
        patch_registry(monkeypatch, [cache])

        result = invoke(runner, ["reclaim", "--only", "huggingface", "--yes"])

        assert result.exit_code == 0
        assert "refused dangerous path" in result.output
        assert root_marker.exists()

    def test_path_with_parent_reference_is_refused(self, runner, tmp_path, monkeypatch):
        decoy = write(tmp_path / "decoy" / "file", 8)
        (tmp_path / "safe").mkdir()
        bad = tmp_path / "safe" / ".." / "decoy"
        patch_registry(monkeypatch, [one_cache("dotdot", bad)])

        result = invoke(runner, ["reclaim", "--only", "dotdot", "--yes"])

        assert "refused dangerous path" in result.output
        assert decoy.exists()

    def test_symlinked_cache_dir_is_refused_and_decoys_survive(
        self, runner, fake_home, tmp_path, monkeypatch
    ):
        decoy = write(tmp_path / "decoy" / "important", 20)
        link = fake_home / ".cache" / "pip"
        link.parent.mkdir(parents=True)
        link.symlink_to(decoy.parent, target_is_directory=True)
        patch_registry(monkeypatch, [one_cache("pip", link)])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert "refused dangerous path" in result.output
        assert link.is_symlink()
        assert decoy.exists()

    def test_unknown_only_is_an_error_and_deletes_nothing(
        self, runner, fake_home, monkeypatch
    ):
        pip_file = write(fake_home / ".cache" / "pip" / "wheel", 10)
        patch_registry(monkeypatch, [one_cache("pip", pip_file.parent)])

        result = invoke(runner, ["reclaim", "--only", "bogus", "--yes"])

        assert result.exit_code != 0
        assert "unknown cache" in result.output
        assert pip_file.exists()


class TestReclaimConfirmationAndSelection:
    """User intent must be explicit and scoped before anything is removed."""

    def test_declined_confirmation_deletes_nothing(
        self, runner, fake_home, monkeypatch
    ):
        wheel = write(fake_home / ".cache" / "pip" / "wheel", 10)
        patch_registry(monkeypatch, [one_cache("pip", wheel.parent)])

        result = invoke(runner, ["reclaim", "--only", "pip"], input="n\n")

        assert result.exit_code == 0
        assert "aborted" in result.output
        assert wheel.exists()

    def test_accepted_confirmation_deletes_selected_cache(
        self, runner, fake_home, monkeypatch
    ):
        wheel = write(fake_home / ".cache" / "pip" / "wheel", 10)
        patch_registry(monkeypatch, [one_cache("pip", wheel.parent)])

        result = invoke(runner, ["reclaim", "--only", "pip"], input="y\n")

        assert result.exit_code == 0
        assert not wheel.exists()
        assert "freed" in result.output

    def test_dry_run_computes_size_but_deletes_nothing(
        self, runner, fake_home, monkeypatch
    ):
        wheel = write(fake_home / ".cache" / "pip" / "wheel", 1536)
        patch_registry(monkeypatch, [one_cache("pip", wheel.parent)])

        result = invoke(runner, ["reclaim", "--only", "pip", "--dry-run"])

        assert result.exit_code == 0
        assert "dry-run" in result.output
        assert "KiB" in result.output
        assert wheel.exists()

    def test_only_deletes_only_the_named_cache(self, runner, fake_home, monkeypatch):
        pip_file = write(fake_home / ".cache" / "pip" / "wheel", 10)
        uv_file = write(fake_home / ".cache" / "uv" / "pkg", 10)
        patch_registry(
            monkeypatch,
            [one_cache("pip", pip_file.parent), one_cache("uv", uv_file.parent)],
        )

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert not pip_file.exists()
        assert uv_file.exists()
        assert "uv" not in result.output

    def test_missing_cache_is_skipped_silently(self, runner, fake_home, monkeypatch):
        patch_registry(monkeypatch, [one_cache("pip", fake_home / ".cache" / "pip")])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert "Nothing reclaimable selected" in result.output
        assert "Traceback" not in result.output

    def test_requires_sudo_is_reported_not_attempted(
        self, runner, fake_home, monkeypatch
    ):
        apt = fake_home / "apt" / "archives"
        pkg = write(apt / "pkg.deb", 10)
        patch_registry(monkeypatch, [one_cache("apt", apt, requires_sudo=True)])
        monkeypatch.setattr(space.os, "geteuid", lambda: 1000)

        result = invoke(runner, ["reclaim", "--only", "apt", "--yes"])

        assert result.exit_code == 0
        assert "requires sudo" in result.output
        assert pkg.exists()

    def test_pycache_is_never_selected_unless_explicitly_requested(
        self, runner, tmp_path
    ):
        root = tmp_path / "project"
        pyc = write(root / "pkg" / "__pycache__" / "m.cpython.pyc", 10)

        assert "pycache" not in invoke(runner, ["caches"]).output
        result = invoke(
            runner,
            ["reclaim", "--pycache-root", str(root), "--only", "pycache", "--yes"],
        )

        assert result.exit_code == 0
        assert not pyc.exists()


class TestReclaimToleratesFilesystemRaces:
    """Cache cleanup should be best-effort: one bad entry cannot stop the rest."""

    def test_file_vanishes_during_delete_does_not_abort(
        self, runner, fake_home, monkeypatch
    ):
        cache = fake_home / ".cache" / "pip"
        doomed = write(cache / "gone", 10)
        survivor = write(fake_home / ".cache" / "uv" / "keep", 10)
        real_unlink = Path.unlink

        def vanishing_unlink(path: Path, *args, **kwargs):
            if path == doomed:
                real_unlink(path, *args, **kwargs)
                raise FileNotFoundError(path)
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", vanishing_unlink)
        patch_registry(
            monkeypatch, [one_cache("pip", cache), one_cache("uv", survivor.parent)]
        )

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert not doomed.exists()
        assert survivor.exists()

    def test_permission_denied_subdirectory_is_reported_and_remaining_cache_removed(
        self, runner, fake_home, monkeypatch
    ):
        cache = fake_home / ".cache" / "pip"
        blocked = cache / "blocked"
        blocked_file = write(blocked / "file", 10)
        ok_file = write(cache / "ok", 10)
        real_iterdir = Path.iterdir

        def denied_iterdir(path: Path):
            if path == blocked:
                raise PermissionError("denied")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", denied_iterdir)
        patch_registry(monkeypatch, [one_cache("pip", cache)])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert "denied" in result.output
        assert not ok_file.exists()
        assert blocked_file.exists()

    def test_broken_symlink_cache_root_is_reported_not_followed(
        self, runner, fake_home, monkeypatch
    ):
        broken = fake_home / ".cache" / "pip"
        broken.parent.mkdir(parents=True)
        broken.symlink_to(fake_home / "missing")
        patch_registry(monkeypatch, [one_cache("pip", broken)])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert "refused dangerous path" in result.output
        assert broken.is_symlink()

    def test_dangling_mountpoint_rmdir_error_is_reported_and_siblings_continue(
        self, runner, fake_home, monkeypatch
    ):
        cache = fake_home / ".cache" / "pip"
        busy = cache / "busy"
        write(busy / "file", 10)
        other = write(fake_home / ".cache" / "uv" / "pkg", 10)
        real_rmdir = Path.rmdir

        def busy_rmdir(path: Path, *args, **kwargs):
            if path == busy:
                raise OSError("Device busy")
            return real_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "rmdir", busy_rmdir)
        patch_registry(
            monkeypatch, [one_cache("pip", cache), one_cache("uv", other.parent)]
        )

        result = invoke(runner, ["reclaim", "--yes"])

        assert result.exit_code == 0
        assert "Device busy" in result.output
        assert not other.exists()

    def test_read_only_file_is_tolerated(self, runner, fake_home, monkeypatch):
        cache = fake_home / ".cache" / "pip"
        readonly = write(cache / "readonly", 10)
        readonly.chmod(0o444)
        patch_registry(monkeypatch, [one_cache("pip", cache)])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert not readonly.exists()

    def test_very_deep_tree_is_removed_without_recursing_outside_cache(
        self, runner, fake_home, monkeypatch
    ):
        leaf = fake_home / ".cache" / "pip"
        for idx in range(40):
            leaf = leaf / f"d{idx}"
        payload = write(leaf / "payload", 1)
        patch_registry(monkeypatch, [one_cache("pip", fake_home / ".cache" / "pip")])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert not payload.exists()

    def test_cache_path_that_is_file_is_removed(self, runner, fake_home, monkeypatch):
        cache_file = write(fake_home / ".cache" / "pip", 10)
        patch_registry(monkeypatch, [one_cache("pip", cache_file)])

        result = invoke(runner, ["reclaim", "--only", "pip", "--yes"])

        assert result.exit_code == 0
        assert not cache_file.exists()

    def test_partial_failure_exit_code_zero_and_summary_uses_actual_freed_bytes(
        self, runner, fake_home, monkeypatch
    ):
        pip_cache = fake_home / ".cache" / "pip"
        bad = write(pip_cache / "bad", 10)
        good = write(fake_home / ".cache" / "uv" / "good", 2048)
        real_unlink = Path.unlink

        def blocked_unlink(path: Path, *args, **kwargs):
            if path == bad:
                raise PermissionError("blocked")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", blocked_unlink)
        patch_registry(
            monkeypatch, [one_cache("pip", pip_cache), one_cache("uv", good.parent)]
        )

        result = invoke(runner, ["reclaim", "--yes"])

        assert result.exit_code == 0
        assert "blocked" in result.output
        assert "freed" in result.output
        assert not good.exists()
        assert bad.exists()


# --- Sizing ----------------------------------------------------------------


class TestSizeComputationProtectsAgainstDoubleCounting:
    """The summary and reclaim plan are only as trustworthy as the size walk."""

    def test_known_tree_matches_exact_lstat_sum(self, tmp_path):
        root = tmp_path / "tree"
        write(root / "a.bin", 10)
        write(root / "nested" / "b.bin", 20)
        (root / "empty").mkdir(parents=True)

        assert space.path_size(root).bytes == expected_size(root)

    def test_nested_directories_and_empty_directories_count_once(self, tmp_path):
        root = tmp_path / "tree"
        (root / "a" / "b" / "empty").mkdir(parents=True)
        write(root / "a" / "payload", 5)

        assert space.path_size(root).bytes == expected_size(root)

    def test_single_large_file_reports_its_size(self, tmp_path):
        large = write(tmp_path / "large.bin", 1024 * 1024)

        assert space.path_size(large).bytes == 1024 * 1024

    def test_hard_links_are_not_double_counted(self, tmp_path):
        root = tmp_path / "tree"
        original = write(root / "original", 123)
        os.link(original, root / "hardlink")

        assert space.path_size(root).bytes == expected_size(root)
        assert space.path_size(root).bytes < expected_size(root) + 123

    def test_symlink_loop_terminates_without_following(self, tmp_path):
        root = tmp_path / "tree"
        root.mkdir()
        (root / "loop").symlink_to(root, target_is_directory=True)

        assert space.path_size(root).bytes == expected_size(root)

    def test_unreadable_subdir_is_reported(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        blocked = root / "blocked"
        blocked.mkdir(parents=True)
        real_scandir = os.scandir

        def fake_scandir(path):
            if Path(path) == blocked:
                raise PermissionError("denied")
            return real_scandir(path)

        monkeypatch.setattr(space.os, "scandir", fake_scandir)

        sized = space.path_size(root)

        assert "denied" in "\n".join(sized.errors)

    def test_file_removed_between_scan_and_stat_is_ignored(self, tmp_path, monkeypatch):
        root = tmp_path / "tree"
        doomed = write(root / "gone", 10)
        real_scandir = os.scandir

        class Vanish:
            path = str(doomed)

            def stat(self, *, follow_symlinks=False):
                doomed.unlink(missing_ok=True)
                raise FileNotFoundError(doomed)

        def fake_scandir(path):
            ctx = real_scandir(path)

            class Wrapped:
                def __enter__(self):
                    ctx.__enter__()
                    return [Vanish()]

                def __exit__(self, *exc):
                    return ctx.__exit__(*exc)

            return Wrapped()

        monkeypatch.setattr(space.os, "scandir", fake_scandir)

        assert space.path_size(root).errors == ()

    def test_sparse_file_reports_logical_size(self, tmp_path):
        sparse = tmp_path / "sparse"
        sparse.parent.mkdir(parents=True, exist_ok=True)
        with sparse.open("wb") as fh:
            fh.seek(1024 * 1024 - 1)
            fh.write(b"x")

        assert space.path_size(sparse).bytes == 1024 * 1024

    def test_many_entries_do_not_crash_or_skip(self, tmp_path):
        root = tmp_path / "many"
        for idx in range(100):
            write(root / f"f{idx:03d}", 1)

        assert space.path_size(root).bytes == expected_size(root)

    def test_missing_path_sizes_to_zero(self, tmp_path):
        assert space.path_size(tmp_path / "missing").bytes == 0


# --- Top command -----------------------------------------------------------


class TestTopReportsLargestEntriesPredictably:
    """`space top` is the diagnostic users run before deleting anything."""

    def test_ordering_is_size_descending(self, runner, tmp_path):
        write(tmp_path / "small", 1)
        write(tmp_path / "large", 100)

        result = invoke(runner, ["top", str(tmp_path), "-n", "5", "--depth", "1"])

        assert result.output.index("large") < result.output.index("small")

    def test_n_limits_rows(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")
        root = tmp_path / "r"
        for idx in range(5):
            write(root / f"f{idx}", idx + 1)
        monkeypatch.chdir(tmp_path)

        result = invoke(runner, ["top", "r", "-n", "2", "--depth", "1"])

        assert "f4" in result.output
        assert "f3" in result.output
        assert "f2" not in result.output

    def test_depth_is_respected(self, runner, tmp_path):
        write(tmp_path / "a" / "b" / "deep", 100)

        shallow = invoke(runner, ["top", str(tmp_path), "--depth", "1"]).output
        deep = invoke(runner, ["top", str(tmp_path), "--depth", "3"]).output

        assert "deep" not in shallow
        assert "deep" in deep

    def test_missing_path_is_a_clear_error(self, runner, tmp_path):
        result = invoke(runner, ["top", str(tmp_path / "missing")])

        assert result.exit_code != 0
        assert "path does not exist" in result.output

    def test_file_path_is_reported(self, runner, tmp_path):
        file_path = write(tmp_path / "one.bin", 10)

        result = invoke(runner, ["top", str(file_path)])

        assert result.exit_code == 0
        assert "one.bin" in result.output

    def test_empty_directory_has_sensible_message(self, runner, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()

        result = invoke(runner, ["top", str(empty)])

        assert result.exit_code == 0
        assert "No entries found" in result.output

    def test_unreadable_directory_does_not_crash(self, runner, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        real_iterdir = Path.iterdir

        def denied(path: Path):
            if path == root:
                raise PermissionError("denied")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", denied)

        result = invoke(runner, ["top", str(root)])

        assert result.exit_code == 0
        assert "No entries found" in result.output

    def test_ties_are_broken_by_path_name(self, runner, tmp_path):
        write(tmp_path / "b", 10)
        write(tmp_path / "a", 10)

        result = invoke(runner, ["top", str(tmp_path), "--depth", "1"])

        assert result.output.index("a") < result.output.index("b")


# --- Cache registry --------------------------------------------------------


PATH_CACHE_NAMES = [
    "pip",
    "uv",
    "npm",
    "yarn",
    "cargo",
    "huggingface",
    "torch",
    "conda",
    "usm-envs",
    "trash",
]


class TestCacheRegistryDetectsOnlyKnownLocations:
    """Every declarative cache entry should behave the same as new ones are added."""

    def cache_by_name(self, name: str) -> space.Cache:
        caches = {cache.name: cache for cache in space.cache_registry()}
        return caches[name]

    @pytest.mark.parametrize("name", PATH_CACHE_NAMES)
    def test_registered_path_cache_absent_is_skipped(
        self, name, fake_home, monkeypatch
    ):
        if name == "conda":
            monkeypatch.setenv("CONDA_PKGS_DIRS", str(fake_home / "conda-pkgs"))
        cache = self.cache_by_name(name)

        rows = space._cache_rows([cache])

        assert rows == []

    @pytest.mark.parametrize("name", PATH_CACHE_NAMES)
    def test_registered_path_cache_present_empty_is_reported(
        self, name, fake_home, monkeypatch
    ):
        if name == "conda":
            monkeypatch.setenv("CONDA_PKGS_DIRS", str(fake_home / "conda-pkgs"))
        cache = self.cache_by_name(name)
        first = cache.paths()[0]
        first.mkdir(parents=True, exist_ok=True)

        rows = space._cache_rows([cache])

        assert rows[0]["name"] == name
        assert rows[0]["paths"] == [str(first)]

    @pytest.mark.parametrize("name", PATH_CACHE_NAMES)
    def test_registered_path_cache_present_with_content_has_size(
        self, name, fake_home, monkeypatch
    ):
        if name == "conda":
            monkeypatch.setenv("CONDA_PKGS_DIRS", str(fake_home / "conda-pkgs"))
        cache = self.cache_by_name(name)
        first = cache.paths()[0]
        write(first / "payload", 33)

        rows = space._cache_rows([cache])

        assert rows[0]["name"] == name
        assert rows[0]["bytes"] >= 33

    def test_hf_home_env_is_respected(self, tmp_path, monkeypatch):
        hf = tmp_path / "hf"
        monkeypatch.setenv("HF_HOME", str(hf))

        assert space._hf_hub_path() == hf / "hub"

    def test_hf_home_unset_uses_default_home_cache(self, fake_home, monkeypatch):
        monkeypatch.delenv("HF_HOME", raising=False)

        assert space._hf_hub_path() == fake_home / ".cache" / "huggingface" / "hub"

    def test_hf_home_nonexistent_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path / "missing"))

        rows = space._cache_rows([self.cache_by_name("huggingface")])

        assert rows == []

    def test_go_cache_env_from_go_tool_is_respected(self, tmp_path, monkeypatch):
        go_cache = tmp_path / "go-cache"
        write(go_cache / "obj", 11)
        monkeypatch.setattr(
            space.shutil, "which", lambda name: "/bin/go" if name == "go" else None
        )
        monkeypatch.setattr(
            space,
            "_run",
            lambda argv: SimpleNamespace(returncode=0, stdout=str(go_cache), stderr=""),
        )

        rows = {row["name"]: row for row in space._cache_rows(space.cache_registry())}

        assert rows["go"]["bytes"] >= 11

    def test_go_missing_is_skipped(self, monkeypatch):
        monkeypatch.setattr(space.shutil, "which", lambda _name: None)

        assert "go" not in {cache.name for cache in space.cache_registry()}

    def test_go_tool_nonzero_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            space.shutil, "which", lambda name: "/bin/go" if name == "go" else None
        )
        monkeypatch.setattr(
            space,
            "_run",
            lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="bad"),
        )

        assert "go" not in {cache.name for cache in space.cache_registry()}

    @pytest.mark.parametrize("tool", ["docker", "journalctl", "go", "conda"])
    def test_external_tool_absent_never_tracebacks(self, tool, monkeypatch):
        monkeypatch.setattr(space.shutil, "which", lambda name: None)

        rows = space._cache_rows(space.cache_registry())

        assert all(row["name"] != tool for row in rows)

    @pytest.mark.parametrize("tool", ["docker", "journalctl"])
    def test_external_tool_nonzero_is_reported(self, tool, monkeypatch):
        monkeypatch.setattr(space.shutil, "which", lambda name: f"/bin/{name}")
        monkeypatch.setattr(
            space,
            "_run",
            lambda argv: SimpleNamespace(returncode=2, stdout="", stderr="failed"),
        )

        rows = {row["name"]: row for row in space._cache_rows(space.cache_registry())}

        key = "journald" if tool == "journalctl" else tool
        assert rows[key]["errors"] == ["failed"]

    @pytest.mark.parametrize("tool", ["docker", "journalctl"])
    def test_external_tool_garbage_output_sizes_to_zero(self, tool, monkeypatch):
        monkeypatch.setattr(space.shutil, "which", lambda name: f"/bin/{name}")
        monkeypatch.setattr(
            space,
            "_run",
            lambda argv: SimpleNamespace(returncode=0, stdout="not a size", stderr=""),
        )

        rows = {row["name"]: row for row in space._cache_rows(space.cache_registry())}

        key = "journald" if tool == "journalctl" else tool
        assert rows[key]["bytes"] == 0

    @pytest.mark.parametrize("tool", ["docker", "journalctl", "go"])
    def test_external_tool_timeout_is_graceful(self, tool, monkeypatch):
        monkeypatch.setattr(space.shutil, "which", lambda name: f"/bin/{name}")

        def timeout(argv):
            raise subprocess.TimeoutExpired(argv, 30)

        monkeypatch.setattr(space, "_run", timeout)

        rows = space._cache_rows(space.cache_registry())

        assert isinstance(rows, list)

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("Images 2 1 1.5GB 1.0GB (66%) reclaimable", 1024**3),
            ("Build Cache 2 2 10MiB 10MiB reclaimable", 10 * 1024**2),
            ("Containers 0 0 0B 0B reclaimable", 0),
        ],
    )
    def test_docker_system_df_reclaimable_parser(self, line, expected):
        assert space._docker_size(line) == expected

    def test_docker_system_df_malformed_output_is_zero(self):
        assert space._docker_size("TYPE TOTAL ACTIVE SIZE") == 0

    def test_apt_entry_is_marked_requires_sudo(self):
        apt = {cache.name: cache for cache in space.cache_registry()}["apt"]

        assert apt.requires_sudo is True


# --- Summary and output ----------------------------------------------------


class TestSummaryAndOutputAreScriptableAndReadable:
    """The command's output is consumed both by humans under stress and scripts."""

    def test_summary_json_has_expected_types(self, runner, fake_home, monkeypatch):
        write(fake_home / ".cache" / "pip" / "wheel", 12)
        patch_registry(monkeypatch, [one_cache("pip", fake_home / ".cache" / "pip")])
        part = SimpleNamespace(device="/dev/sda1", mountpoint="/", fstype="ext4")
        usage = SimpleNamespace(total=1000, used=400, free=600, percent=40.0)
        monkeypatch.setattr(space.psutil, "disk_partitions", lambda all=False: [part])
        monkeypatch.setattr(space.psutil, "disk_usage", lambda _mount: usage)

        data = json.loads(invoke(runner, ["--json"]).output)

        assert isinstance(data["filesystems"], list)
        assert isinstance(data["caches"], list)
        assert isinstance(data["reclaimable_bytes"], int)
        assert isinstance(data["filesystems"][0]["used"], int)
        assert isinstance(data["caches"][0]["paths"], list)

    def test_caches_json_has_expected_types(self, runner, fake_home, monkeypatch):
        write(fake_home / ".npm" / "_cacache" / "x", 20)
        patch_registry(monkeypatch, [one_cache("npm", fake_home / ".npm" / "_cacache")])

        data = json.loads(invoke(runner, ["caches", "--json"]).output)

        assert isinstance(data["reclaimable_bytes"], int)
        assert data["caches"][0]["name"] == "npm"
        assert isinstance(data["caches"][0]["bytes"], int)
        assert isinstance(data["caches"][0]["requires_sudo"], bool)

    def test_pseudo_loop_snap_and_tmpfs_excluded_by_default_and_included_with_all(
        self, runner, monkeypatch
    ):
        parts = [
            SimpleNamespace(device="tmpfs", mountpoint="/run", fstype="tmpfs"),
            SimpleNamespace(
                device="/dev/loop0", mountpoint="/snap/core", fstype="squashfs"
            ),
            SimpleNamespace(device="/dev/sda1", mountpoint="/data", fstype="ext4"),
        ]
        usage = SimpleNamespace(total=1, used=1, free=0, percent=100.0)
        monkeypatch.setattr(space.psutil, "disk_partitions", lambda all=False: parts)
        monkeypatch.setattr(space.psutil, "disk_usage", lambda _mount: usage)
        patch_registry(monkeypatch, [])

        default = json.loads(invoke(runner, ["--json"]).output)
        all_rows = json.loads(invoke(runner, ["--all", "--json"]).output)

        assert [row["mount"] for row in default["filesystems"]] == ["/data"]
        assert {row["mount"] for row in all_rows["filesystems"]} == {
            "/run",
            "/snap/core",
            "/data",
        }

    def test_zero_reclaimable_caches_has_sensible_message(self, runner, monkeypatch):
        patch_registry(monkeypatch, [])

        result = invoke(runner, ["caches"])

        assert result.exit_code == 0
        assert "No known reclaimable caches found" in result.output

    @pytest.mark.parametrize("width", [40, 60, 80, 120, 200])
    @pytest.mark.parametrize(
        "command", [[], ["caches"], ["top"], ["reclaim", "--dry-run"]]
    )
    def test_table_commands_do_not_crash_or_wrap_key_rows(
        self, command, width, runner, fake_home, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("COLUMNS", str(width))
        write(fake_home / ".cache" / "pip" / "wheel", 10)
        top_root = tmp_path / "toproot"
        write(top_root / "entry", 20)
        patch_registry(monkeypatch, [one_cache("pip", fake_home / ".cache" / "pip")])
        part = SimpleNamespace(device="/dev/sda1", mountpoint="/", fstype="ext4")
        usage = SimpleNamespace(total=100, used=40, free=60, percent=40.0)
        monkeypatch.setattr(space.psutil, "disk_partitions", lambda all=False: [part])
        monkeypatch.setattr(space.psutil, "disk_usage", lambda _mount: usage)
        args = command
        if command == ["top"]:
            args = ["top", str(top_root), "-n", "1"]

        result = invoke(runner, args)

        assert result.exit_code == 0
        assert "Traceback" not in result.output
        if "pip" in result.output:
            assert (
                len([line for line in result.output.splitlines() if "pip" in line]) == 1
            )
        if "entry" in result.output:
            assert (
                len([line for line in result.output.splitlines() if "entry" in line])
                == 1
            )

    def test_help_works_on_group_and_every_subcommand(self, runner):
        assert invoke(runner, ["-h"]).exit_code == 0
        assert invoke(runner, ["top", "-h"]).exit_code == 0
        assert invoke(runner, ["caches", "-h"]).exit_code == 0
        assert invoke(runner, ["reclaim", "-h"]).exit_code == 0
