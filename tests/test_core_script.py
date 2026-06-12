"""Tests for usmo.core.Script and Script.build_argv."""

from __future__ import annotations

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
        monkeypatch.setattr(core.shutil, "which", lambda _: None)
        s = Script(name="x", path="x.py", requirements=("foo",))
        with pytest.raises(MissingUv) as excinfo:
            core.ensure_env(s)
        assert excinfo.value.requirements == ("foo",)

    def test_returns_existing_env(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/uv")
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        py = core._env_python(s.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (s.env_dir / core.ENV_MARKER_NAME).write_text(
            '{"requirements": ["foo"], "python": "3.11"}'
        )
        assert core.ensure_env(s) == str(py)

    def test_builds_when_missing(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/uv")
        built: list[Script] = []
        monkeypatch.setattr(
            core,
            "_build_env",
            lambda s, on_progress=core._null_hook: (
                built.append(s) or Path("/envs/x/bin/python")
            ),
        )
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        assert core.ensure_env(s) == "/envs/x/bin/python"
        assert built == [s]

    def test_upgrade_rebuilds_even_if_ready(self, tmp_cache, monkeypatch):
        monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/uv")
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        py = core._env_python(s.env_dir)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (s.env_dir / core.ENV_MARKER_NAME).write_text(
            '{"requirements": ["foo"], "python": "3.11"}'
        )
        built: list[Script] = []
        monkeypatch.setattr(
            core,
            "_build_env",
            lambda s, on_progress=core._null_hook: (
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

        monkeypatch.setattr(core.subprocess, "run", fake_run)
        py = core._build_env(s)
        assert py == core._env_python(s.env_dir)
        marker = (s.env_dir / core.ENV_MARKER_NAME).read_text()
        assert '"requirements"' in marker and "foo" in marker
        assert calls[0][:3] == ["uv", "venv", "--python"]
        assert calls[1][:3] == ["uv", "pip", "install"]

    def test_raises_env_build_error_on_failure(self, tmp_cache, monkeypatch):
        def fake_run(argv, **kwargs):
            raise core.subprocess.CalledProcessError(
                1, argv, output="", stderr="tls handshake eof"
            )

        monkeypatch.setattr(core.subprocess, "run", fake_run)
        s = Script(name="x", path="x.py", requirements=("foo",), python="3.11")
        with pytest.raises(core.EnvBuildError) as excinfo:
            core._build_env(s)
        assert excinfo.value.name == "x"
        assert "tls handshake eof" in excinfo.value.detail
        assert not s.env_dir.exists()
