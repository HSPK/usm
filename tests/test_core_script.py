"""Tests for usmo.core.Script and Script.build_argv."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from usmo import core
from usmo.core import MissingUv, Script


class TestFromConfig:
    def test_minimal(self):
        s = Script.from_config("foo", {"path": "foo.sh"})
        assert s.name == "foo"
        assert s.path == "foo.sh"
        assert s.description == ""
        assert s.requirements == ()
        assert s.python is None
        assert s.version is None
        assert s.hash is None

    def test_full(self):
        s = Script.from_config(
            "x",
            {
                "path": "x.py",
                "description": "X",
                "requirements": ["a", "b"],
                "python": "3.11",
                "version": "2.0.0",
                "hash": "sha256:abc",
            },
        )
        assert s.description == "X"
        assert s.requirements == ("a", "b")
        assert s.python == "3.11"
        assert s.version == "2.0.0"
        assert s.hash == "sha256:abc"

    def test_missing_path_raises(self):
        with pytest.raises(KeyError):
            Script.from_config("foo", {})


class TestProperties:
    @pytest.mark.parametrize(
        "path,is_py",
        [("foo.py", True), ("foo.PY", True), ("foo.sh", False), ("foo", False)],
    )
    def test_is_python(self, path, is_py):
        assert Script(name="x", path=path).is_python is is_py

    def test_uses_uv_requires_python_and_requirements(self):
        assert not Script(name="x", path="x.py").uses_uv
        assert not Script(name="x", path="x.sh", requirements=("a",)).uses_uv
        assert Script(name="x", path="x.py", requirements=("a",)).uses_uv

    def test_cached_path(self, tmp_cache):
        s = Script(name="x", path="sub/x.py")
        assert s.cached_path == tmp_cache / "scripts" / "sub" / "x.py"

    def test_local_path_debug(self, tmp_path, monkeypatch):
        (tmp_path / "scripts").mkdir()
        monkeypatch.chdir(tmp_path)
        s = Script(name="x", path="x.py")
        assert s.local_path(debug=True) == Path.cwd() / "scripts" / "x.py"

    def test_local_path_cached(self, tmp_cache):
        s = Script(name="x", path="x.py")
        assert s.local_path(debug=False) == tmp_cache / "scripts" / "x.py"


class TestBuildArgv:
    def test_shell_script(self):
        s = Script(name="x", path="x.sh")
        assert s.build_argv(Path("/tmp/x.sh"), ["a", "b"], python="/py") == [
            "bash",
            "/tmp/x.sh",
            "a",
            "b",
        ]

    def test_python_script(self):
        s = Script(name="x", path="x.py")
        assert s.build_argv(Path("/tmp/x.py"), ["a"], python=sys.executable) == [
            sys.executable,
            "/tmp/x.py",
            "a",
        ]

    def test_python_script_uses_given_interpreter(self):
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        argv = s.build_argv(Path("/tmp/x.py"), ["a"], python="/envs/x/bin/python")
        assert argv == ["/envs/x/bin/python", "/tmp/x.py", "a"]

    def test_shell_script_ignores_python(self):
        s = Script(name="x", path="x.sh", requirements=("foo",))
        assert s.build_argv(Path("/tmp/x.sh"), [], python="/envs/x/bin/python") == [
            "bash",
            "/tmp/x.sh",
        ]


class TestInterpreterVersion:
    def test_explicit(self):
        assert (
            Script(name="x", path="x.py", python="3.12").interpreter_version() == "3.12"
        )

    def test_defaults_to_runtime(self):
        expected = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert Script(name="x", path="x.py").interpreter_version() == expected


class TestEnvDir:
    def test_env_dir(self, tmp_cache):
        s = Script(name="clash", path="clash.py")
        assert s.env_dir == tmp_cache / "envs" / "clash"


class TestEnvReady:
    def test_no_requirements_always_ready(self, tmp_cache):
        assert core.env_ready(Script(name="x", path="x.py"))

    def test_missing_env_not_ready(self, tmp_cache):
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        assert not core.env_ready(s)

    def test_ready_when_marker_matches(self, tmp_cache):
        s = Script(name="x", path="x.py", requirements=("foo", "bar"), python="3.11")
        py = core._env_python(s.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (s.env_dir / core.ENV_MARKER_NAME).write_text(
            '{"requirements": ["foo", "bar"], "python": "3.11"}'
        )
        assert core.env_ready(s)

    def test_stale_when_requirements_change(self, tmp_cache):
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        py = core._env_python(s.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (s.env_dir / core.ENV_MARKER_NAME).write_text(
            '{"requirements": ["foo", "bar"], "python": "3.11"}'
        )
        assert not core.env_ready(s)


class TestEnsureEnv:
    def test_no_requirements_returns_runtime(self, tmp_cache):
        s = Script(name="x", path="x.py")
        assert core.ensure_env(s) == sys.executable

    def test_missing_uv_raises(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.environments.shutil, "which", lambda _: None)
        s = Script(name="x", path="x.py", requirements=("foo",))
        with pytest.raises(MissingUv) as excinfo:
            core.ensure_env(s)
        assert excinfo.value.requirements == ("foo",)

    def test_returns_existing_env(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.environments.shutil, "which", lambda _: "/usr/bin/uv")
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        py = core._env_python(s.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (s.env_dir / core.ENV_MARKER_NAME).write_text(
            '{"requirements": ["foo"], "python": "3.11"}'
        )
        assert core.ensure_env(s) == str(py)

    def test_builds_when_missing(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.environments.shutil, "which", lambda _: "/usr/bin/uv")
        built: list[Script] = []
        monkeypatch.setattr(
            core.environments,
            "_build_env",
            lambda s, debug=False, on_progress=core._null_hook: (
                built.append(s) or Path("/envs/x/bin/python")
            ),
        )
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        assert core.ensure_env(s) == "/envs/x/bin/python"
        assert built == [s]

    def test_upgrade_rebuilds_even_if_ready(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.environments.shutil, "which", lambda _: "/usr/bin/uv")
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        py = core._env_python(s.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (s.env_dir / core.ENV_MARKER_NAME).write_text(
            '{"requirements": ["foo"], "python": "3.11"}'
        )
        built: list[Script] = []
        monkeypatch.setattr(
            core.environments,
            "_build_env",
            lambda s, debug=False, on_progress=core._null_hook: (
                built.append(s) or Path("/new/python")
            ),
        )
        assert core.ensure_env(s, upgrade=True) == "/new/python"
        assert built == [s]


class TestBuildEnv:
    def test_writes_marker_on_success(self, tmp_cache, monkeypatch):
        calls: list[list[str]] = []
        s = Script(name="x", path="x.py", requirements=("foo", "bar"), python="3.11")

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["uv", "venv"]:
                s.env_dir.mkdir(parents=True, exist_ok=True)
            return None

        monkeypatch.setattr(core.environments.subprocess, "run", fake_run)
        py = core._build_env(s)
        assert py == core._env_python(s.env_dir)
        marker = (s.env_dir / core.ENV_MARKER_NAME).read_text()
        assert '"requirements"' in marker and "foo" in marker
        assert calls[0][:3] == ["uv", "venv", "--python"]
        assert calls[1][:3] == ["uv", "pip", "install"]

    def test_raises_env_build_error_on_failure(self, tmp_cache, monkeypatch):
        def fake_run(argv, **kwargs):
            raise core.environments.subprocess.CalledProcessError(
                1, argv, output="", stderr="tls handshake eof"
            )

        monkeypatch.setattr(core.environments.subprocess, "run", fake_run)
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        with pytest.raises(core.EnvBuildError) as excinfo:
            core._build_env(s)
        assert excinfo.value.name == "x"
        assert "tls handshake eof" in excinfo.value.detail
        assert not s.env_dir.exists()


class TestSharedModules:
    """Scripts can declare sibling .py modules that ship alongside them."""

    def test_modules_parsed_and_defaulted(self):
        assert Script.from_config("a", {"path": "a.py"}).modules == ()
        script = Script.from_config(
            "a", {"path": "a.py", "modules": ["shared.py", "other.py"]}
        )
        assert script.modules == ("shared.py", "other.py")

    def test_files_lists_the_script_first(self):
        script = Script.from_config("a", {"path": "a.py", "modules": ["m.py"]})
        assert script.files == ("a.py", "m.py")

    def test_files_without_modules(self):
        assert Script.from_config("a", {"path": "a.sh"}).files == ("a.sh",)

    def test_modules_land_next_to_the_script(self, tmp_cache, monkeypatch):
        """Same directory, so Python's sys.path[0] makes the import work."""
        from usmo.core import catalog

        downloaded = []

        def fake_download(filename, *, on_progress=None):
            path = tmp_cache / "scripts" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# stub\n")
            downloaded.append(filename)
            return path

        monkeypatch.setattr(catalog, "download_file", fake_download)
        script = Script.from_config("a", {"path": "a.py", "modules": ["shared.py"]})
        resolved = catalog.ensure_script_file(script)
        assert downloaded == ["shared.py", "a.py"]
        assert resolved.parent == (tmp_cache / "scripts")
        assert (resolved.parent / "shared.py").exists()

    def test_cached_script_still_fetches_a_missing_module(self, tmp_cache, monkeypatch):
        from usmo.core import catalog

        scripts_dir = tmp_cache / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "a.py").write_text("# cached\n")
        downloaded = []

        def fake_download(filename, *, on_progress=None):
            path = scripts_dir / filename
            path.write_text("# stub\n")
            downloaded.append(filename)
            return path

        monkeypatch.setattr(catalog, "download_file", fake_download)
        script = Script.from_config("a", {"path": "a.py", "modules": ["shared.py"]})
        catalog.ensure_script_file(script)
        assert downloaded == ["shared.py"]

    def test_force_redownloads_everything(self, tmp_cache, monkeypatch):
        from usmo.core import catalog

        scripts_dir = tmp_cache / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "a.py").write_text("# cached\n")
        (scripts_dir / "shared.py").write_text("# cached\n")
        downloaded = []

        def fake_download(filename, *, on_progress=None):
            path = scripts_dir / filename
            path.write_text("# fresh\n")
            downloaded.append(filename)
            return path

        monkeypatch.setattr(catalog, "download_file", fake_download)
        script = Script.from_config("a", {"path": "a.py", "modules": ["shared.py"]})
        catalog.ensure_script_file(script, force=True)
        assert downloaded == ["shared.py", "a.py"]

    def test_updates_refresh_modules_too(self, tmp_cache, monkeypatch):
        from usmo.core import catalog

        scripts_dir = tmp_cache / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "a.py").write_text("# cached\n")
        downloaded = []

        def fake_download(filename, *, on_progress=None):
            path = scripts_dir / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
            downloaded.append(filename)
            return path

        monkeypatch.setattr(catalog, "download_file", fake_download)
        monkeypatch.setattr(
            catalog,
            "load_scripts",
            lambda **_kw: {
                "a": Script.from_config("a", {"path": "a.py", "modules": ["shared.py"]})
            },
        )
        results = list(catalog.iter_updates(refresh_config=False))
        assert results == [("a", True)]
        assert downloaded == ["a.py", "shared.py"]


class TestDebugUsesTheLocalCheckout:
    """`--debug` must exercise the code being edited, not the released wheel.

    Scripts depend on ``usmo`` for :mod:`usmo.ui`; without this, changing the
    shared UI would need a release before any script could see it.
    """

    def _script(self, *requirements):
        return Script(name="x", path="x.py", requirements=tuple(requirements))

    def test_requirements_are_untouched_normally(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        script = self._script("click>=8", "usmo>=0.11")
        assert core.environments.resolve_requirements(script) == [
            "click>=8",
            "usmo>=0.11",
        ]

    def test_usmo_is_redirected_to_the_checkout_in_debug(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "usmo"\n')
        monkeypatch.chdir(tmp_path)
        resolved = core.environments.resolve_requirements(
            self._script("click>=8", "usmo>=0.11"), debug=True
        )
        assert resolved == ["click>=8", "--editable", str(tmp_path)]

    def test_other_requirements_are_left_alone(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "usmo"\n')
        monkeypatch.chdir(tmp_path)
        resolved = core.environments.resolve_requirements(
            self._script("usmocket>=1", "rich>=13"), debug=True
        )
        assert resolved == ["usmocket>=1", "rich>=13"]

    def test_outside_the_checkout_nothing_changes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        script = self._script("usmo>=0.11")
        assert core.environments.resolve_requirements(script, debug=True) == [
            "usmo>=0.11"
        ]

    def test_a_different_project_is_not_mistaken_for_usmo(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        monkeypatch.chdir(tmp_path)
        assert core.environments._local_usmo_root() is None

    def test_env_marker_distinguishes_debug(self, tmp_path, monkeypatch):
        """Switching modes must rebuild, not reuse a venv with the wrong usmo."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "usmo"\n')
        monkeypatch.chdir(tmp_path)
        script = self._script("usmo>=0.11")
        assert core.environments._env_spec(script) != core.environments._env_spec(
            script, debug=True
        )

    def test_env_ready_is_false_after_switching_modes(
        self, tmp_cache, tmp_path, monkeypatch
    ):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "usmo"\n')
        monkeypatch.chdir(tmp_path)
        script = self._script("usmo>=0.11")
        py = core._env_python(script.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (script.env_dir / core.ENV_MARKER_NAME).write_text(
            json.dumps(core.environments._env_spec(script))
        )
        assert core.environments.env_ready(script) is True
        assert core.environments.env_ready(script, debug=True) is False


class TestCatalogFileSkew:
    """A stale manifest must never describe freshly downloaded code.

    Regression for a real failure: `_config.json` is cached per user but
    script files are always fetched from the default branch, so a user who
    had not run `usm update` got new code (importing `usmo.ui`) paired with
    an old requirement list that never mentioned `usmo` -- the venv was built
    without it and the script died with ModuleNotFoundError on import.
    """

    def _write(self, tmp_cache, name, body):
        path = tmp_cache / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def _script(self, tmp_cache, *, hash_of=None, modules=()):
        raw = {
            "path": "demo.py",
            "requirements": ["click>=8"],
            "modules": list(modules),
        }
        if hash_of is not None:
            raw["hash"] = core.compute_entry_hash(hash_of)
        return Script.from_config("demo", raw)

    def test_matching_files_are_left_alone(self, tmp_cache):
        script_path = self._write(tmp_cache, "demo.py", "print('v1')\n")
        script = self._script(tmp_cache, hash_of=[script_path])
        assert core.script_files_match(script) is True

    def test_drifted_files_are_detected(self, tmp_cache):
        script_path = self._write(tmp_cache, "demo.py", "print('v1')\n")
        script = self._script(tmp_cache, hash_of=[script_path])
        script_path.write_text("print('v2 from main')\n")
        assert core.script_files_match(script) is False

    def test_a_changed_shared_module_is_detected(self, tmp_cache):
        script_path = self._write(tmp_cache, "demo.py", "import shared\n")
        module_path = self._write(tmp_cache, "shared.py", "A = 1\n")
        script = self._script(
            tmp_cache, hash_of=[script_path, module_path], modules=["shared.py"]
        )
        assert core.script_files_match(script) is True
        module_path.write_text("from usmo.ui import ok\n")
        assert core.script_files_match(script) is False

    def test_entries_without_a_hash_are_accepted(self, tmp_cache):
        self._write(tmp_cache, "demo.py", "x\n")
        assert core.script_files_match(self._script(tmp_cache)) is True

    def test_absent_files_fall_through_to_the_download_path(self, tmp_cache):
        script_path = self._write(tmp_cache, "demo.py", "x\n")
        script = self._script(tmp_cache, hash_of=[script_path])
        script_path.unlink()
        assert core.script_files_match(script) is True

    def test_a_missing_module_falls_through(self, tmp_cache):
        script_path = self._write(tmp_cache, "demo.py", "import shared\n")
        module_path = self._write(tmp_cache, "shared.py", "A = 1\n")
        script = self._script(
            tmp_cache, hash_of=[script_path, module_path], modules=["shared.py"]
        )
        module_path.unlink()
        assert core.script_files_match(script) is True

    def test_reconcile_refreshes_a_stale_manifest(self, tmp_cache, monkeypatch):
        """The reported bug, end to end: old requirements, new code."""
        script_path = self._write(tmp_cache, "demo.py", "from usmo.ui import ok\n")
        stale = Script.from_config(
            "demo",
            {
                "path": "demo.py",
                "requirements": ["click>=8"],  # no usmo: the venv would break
                "hash": core.compute_entry_hash(
                    [self._write(tmp_cache, "old.py", "print('v1')\n")]
                ),
            },
        )
        fresh = Script.from_config(
            "demo",
            {
                "path": "demo.py",
                "requirements": ["click>=8", "usmo>=0.11.0"],
                "hash": core.compute_entry_hash([script_path]),
            },
        )
        refreshed = []
        monkeypatch.setattr(
            core.environments,
            "reload_script",
            lambda name, on_progress=core._null_hook: (refreshed.append(name) or fresh),
        )
        result = core.environments.reconcile_catalog(stale)
        assert refreshed == ["demo"]
        assert "usmo>=0.11.0" in result.requirements

    def test_reconcile_is_a_noop_when_consistent(self, tmp_cache, monkeypatch):
        script_path = self._write(tmp_cache, "demo.py", "print('v1')\n")
        script = self._script(tmp_cache, hash_of=[script_path])

        def explode(*a, **kw):
            raise AssertionError("must not refresh a consistent catalog")

        monkeypatch.setattr(core.environments, "reload_script", explode)
        assert core.environments.reconcile_catalog(script) is script

    def test_reconcile_redownloads_when_still_mismatched(self, tmp_cache, monkeypatch):
        """If the refreshed entry still disagrees, pull the files again."""
        script_path = self._write(tmp_cache, "demo.py", "new code\n")
        stale = self._script(
            tmp_cache,
            hash_of=[self._write(tmp_cache, "old.py", "old\n")],
        )
        fresh = Script.from_config(
            "demo",
            {
                "path": "demo.py",
                "requirements": ["click>=8"],
                "hash": core.compute_entry_hash(
                    [self._write(tmp_cache, "other.py", "different again\n")]
                ),
            },
        )
        monkeypatch.setattr(
            core.environments,
            "reload_script",
            lambda name, on_progress=core._null_hook: fresh,
        )
        pulled = []
        monkeypatch.setattr(
            core.environments,
            "ensure_script_file",
            lambda s, force=False, on_progress=core._null_hook: (
                pulled.append((s.name, force)) or script_path
            ),
        )
        core.environments.reconcile_catalog(stale)
        assert pulled == [("demo", True)]

    def test_run_script_reconciles_before_building_the_env(
        self, tmp_cache, monkeypatch
    ):
        """The venv must be built from the entry that matches the code."""
        script_path = self._write(tmp_cache, "demo.py", "new code\n")
        stale = Script.from_config(
            "demo",
            {
                "path": "demo.py",
                "requirements": ["click>=8"],
                "hash": core.compute_entry_hash(
                    [self._write(tmp_cache, "old.py", "old\n")]
                ),
            },
        )
        fresh = Script.from_config(
            "demo",
            {
                "path": "demo.py",
                "requirements": ["click>=8", "usmo>=0.11.0"],
                "hash": core.compute_entry_hash([script_path]),
            },
        )
        monkeypatch.setattr(
            core.environments,
            "reload_script",
            lambda name, on_progress=core._null_hook: fresh,
        )
        seen = {}
        monkeypatch.setattr(
            core.environments,
            "ensure_env",
            lambda s, **kw: seen.setdefault("requirements", s.requirements)
            or sys.executable,
        )
        monkeypatch.setattr(core.environments.subprocess, "run", lambda *a, **kw: None)
        core.run_script(stale, [])
        assert "usmo>=0.11.0" in seen["requirements"]

    def test_debug_mode_skips_reconciliation(self, tmp_cache, monkeypatch):
        """--debug runs local files; the cached manifest is irrelevant."""

        def explode(*a, **kw):
            raise AssertionError("debug must not touch the network")

        monkeypatch.setattr(core.environments, "reconcile_catalog", explode)
        monkeypatch.setattr(
            core.environments, "ensure_env", lambda s, **kw: sys.executable
        )
        monkeypatch.setattr(core.environments.subprocess, "run", lambda *a, **kw: None)
        script = Script.from_config("demo", {"path": "demo.py", "hash": "sha256:x"})
        core.run_script(script, [], debug=True)


class TestReloadScript:
    """Refreshing a single entry from the published catalog."""

    def test_returns_the_freshly_published_entry(self, tmp_cache, monkeypatch):
        calls = []
        monkeypatch.setattr(
            core.catalog,
            "download_file",
            lambda name, on_progress=core._null_hook: calls.append(name),
        )
        monkeypatch.setattr(
            core.catalog,
            "load_scripts",
            lambda on_progress=core._null_hook: {
                "demo": Script.from_config(
                    "demo", {"path": "demo.py", "requirements": ["usmo>=0.11.0"]}
                )
            },
        )
        script = core.catalog.reload_script("demo")
        assert calls == [core.constants.CONFIG_FILENAME]
        assert list(script.requirements) == ["usmo>=0.11.0"]

    def test_a_withdrawn_script_reports_clearly(self, tmp_cache, monkeypatch):
        """Upstream may drop a script; say so instead of raising KeyError."""
        monkeypatch.setattr(
            core.catalog,
            "download_file",
            lambda name, on_progress=core._null_hook: None,
        )
        monkeypatch.setattr(
            core.catalog, "load_scripts", lambda on_progress=core._null_hook: {}
        )
        with pytest.raises(core.UnknownCommand):
            core.catalog.reload_script("demo")
