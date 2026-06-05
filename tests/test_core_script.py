"""Tests for usmo.core.Script and Script.build_argv."""

from __future__ import annotations

import shutil
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
        assert s.build_argv(Path("/tmp/x.sh"), ["a", "b"]) == [
            "bash", "/tmp/x.sh", "a", "b",
        ]

    def test_python_script_no_requirements(self):
        s = Script(name="x", path="x.py")
        assert s.build_argv(Path("/tmp/x.py"), ["a"]) == [
            sys.executable, "/tmp/x.py", "a",
        ]

    def test_python_script_with_requirements(self, monkeypatch):
        monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/uv")
        s = Script(name="x", path="x.py", requirements=("foo", "bar"), python="3.11")
        argv = s.build_argv(Path("/tmp/x.py"), ["a"])
        assert argv[:6] == ["uv", "run", "--no-project", "--quiet", "--python", "3.11"]
        assert "--with" in argv and "foo" in argv and "bar" in argv
        assert argv[-3:] == ["python", "/tmp/x.py", "a"]

    def test_python_version_defaults_to_runtime(self, monkeypatch):
        monkeypatch.setattr(core.shutil, "which", lambda _: "/usr/bin/uv")
        s = Script(name="x", path="x.py", requirements=("foo",))
        argv = s.build_argv(Path("/tmp/x.py"), [])
        expected = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert expected in argv

    def test_missing_uv_raises(self, monkeypatch):
        monkeypatch.setattr(core.shutil, "which", lambda _: None)
        s = Script(name="x", path="x.py", requirements=("foo",))
        with pytest.raises(MissingUv) as excinfo:
            s.build_argv(Path("/tmp/x.py"), [])
        assert excinfo.value.requirements == ("foo",)

    def test_shell_script_with_requirements_warns_but_runs(self, capsys):
        s = Script(name="x", path="x.sh", requirements=("foo",))
        assert s.build_argv(Path("/tmp/x.sh"), []) == ["bash", "/tmp/x.sh"]
