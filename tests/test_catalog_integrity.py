"""The catalog must describe the scripts it ships.

Scripts are fetched from the default branch while `_config.json` is cached
per user, so any disagreement between the two reaches users as a runtime
error rather than a failed build. Two such bugs have already shipped: a
script that imported `usmo.ui` while its requirements never mentioned usmo,
and four scripts that imported `usm_blocks` without declaring it as a module.
Both were ModuleNotFoundError in someone's terminal.

These tests derive the truth from the source and compare, so the next one is
caught here instead.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CONFIG = json.loads((SCRIPTS / "_config.json").read_text())["scripts"]

#: Modules that live beside a script rather than coming from PyPI.
SHARED_MODULES = {p.stem for p in SCRIPTS.glob("usm_*.py")}

PY_ENTRIES = sorted(
    name for name, entry in CONFIG.items() if entry["path"].endswith(".py")
)


def script_path(name: str) -> Path:
    return SCRIPTS / CONFIG[name]["path"]


def _root(node) -> set[str]:
    if isinstance(node, ast.ImportFrom):
        return {node.module.split(".")[0]} if node.level == 0 and node.module else set()
    return {alias.name.split(".")[0] for alias in node.names}


def imported_names(path: Path) -> set[str]:
    """Every module name the file imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= _root(node)
    return found


def optional_names(path: Path) -> set[str]:
    """Packages the file demonstrates it can live without.

    An import inside a ``try:`` is how this codebase spells "optional" --
    watchdog falls back to a polling watcher, for instance. Once a package is
    guarded anywhere, later unguarded uses are already behind that check, so
    the whole package counts as optional rather than required.
    """
    optional: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    optional |= _root(inner)
    return optional


def shared_closure(name: str) -> set[str]:
    """The sibling modules a script needs, following module-to-module imports."""
    needed: set[str] = set()
    pending = [script_path(name)]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for imported in imported_names(current) & SHARED_MODULES:
            if imported not in needed:
                needed.add(imported)
                pending.append(SCRIPTS / f"{imported}.py")
    return needed


class TestDeclaredModules:
    @pytest.mark.parametrize("name", PY_ENTRIES)
    def test_every_shared_import_is_declared(self, name):
        """An undeclared module is not downloaded, so the script cannot run."""
        declared = {m[:-3] for m in CONFIG[name].get("modules", [])}
        assert shared_closure(name) <= declared, (
            f"{name} imports modules it does not declare: "
            f"{sorted(shared_closure(name) - declared)}"
        )

    @pytest.mark.parametrize("name", PY_ENTRIES)
    def test_nothing_superfluous_is_declared(self, name):
        """A stale declaration downloads a file nobody imports."""
        declared = {m[:-3] for m in CONFIG[name].get("modules", [])}
        assert declared <= shared_closure(name), (
            f"{name} declares modules it does not import: "
            f"{sorted(declared - shared_closure(name))}"
        )

    @pytest.mark.parametrize("name", PY_ENTRIES)
    def test_declared_modules_exist(self, name):
        for module in CONFIG[name].get("modules", []):
            assert (SCRIPTS / module).is_file(), f"{name} declares missing {module}"

    def test_shared_modules_are_not_catalog_entries(self):
        """usm_* files are imported, never dispatched as commands."""
        entry_paths = {entry["path"] for entry in CONFIG.values()}
        for module in SHARED_MODULES:
            assert f"{module}.py" not in entry_paths


class TestDeclaredRequirements:
    """A third-party import that nothing installs is a crash at first run."""

    #: import name -> the distribution that provides it
    DISTRIBUTIONS = {
        "click": "click",
        "rich": "rich",
        "usmo": "usmo",
        "psutil": "psutil",
        "requests": "requests",
        "yaml": "pyyaml",
        "httpx": "httpx",
        "starlette": "starlette",
        "uvicorn": "uvicorn",
        "watchdog": "watchdog",
        "cryptography": "cryptography",
        "azure": "azure-identity",
        "websockets": "websockets",
    }

    @pytest.mark.parametrize("name", PY_ENTRIES)
    def test_third_party_imports_are_declared(self, name):
        declared = {
            req.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
            for req in CONFIG[name].get("requirements", [])
        }
        sources = {script_path(name)} | {
            SCRIPTS / f"{m}.py" for m in shared_closure(name)
        }
        imports: set[str] = set()
        optional: set[str] = set()
        for module in sources:
            imports |= imported_names(module)
            optional |= optional_names(module)
        imports -= optional
        missing = {
            self.DISTRIBUTIONS[imported]
            for imported in imports & set(self.DISTRIBUTIONS)
            if self.DISTRIBUTIONS[imported].lower() not in declared
        }
        assert not missing, f"{name} imports {sorted(missing)} without declaring it"


class TestEntryShape:
    @pytest.mark.parametrize("name", sorted(CONFIG))
    def test_the_file_exists(self, name):
        assert (SCRIPTS / CONFIG[name]["path"]).is_file()

    @pytest.mark.parametrize("name", sorted(CONFIG))
    def test_has_a_description_and_version(self, name):
        entry = CONFIG[name]
        assert entry.get("description", "").strip()
        assert entry.get("version", "").strip()

    @pytest.mark.parametrize("name", sorted(CONFIG))
    def test_has_a_recorded_hash(self, name):
        """The hash is what lets usm notice a stale catalog and self-heal."""
        assert CONFIG[name].get("hash", "").startswith("sha256:")

    def test_names_are_sorted(self):
        assert list(CONFIG) == sorted(CONFIG), "keep _config.json sorted"
