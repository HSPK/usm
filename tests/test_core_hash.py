"""Tests for hash + manifest sync helpers in usmo.core."""

from __future__ import annotations

import hashlib
import json

import pytest

from usmo.core import (
    HASH_PREFIX,
    _bump_version,
    audit_manifest,
    compute_entry_hash,
    compute_script_hash,
    sync_manifest,
)


class TestBumpVersion:
    @pytest.mark.parametrize(
        "version,level,expected",
        [
            ("1.0.0", "patch", "1.0.1"),
            ("1.2.3", "patch", "1.2.4"),
            ("1.2.3", "minor", "1.3.0"),
            ("1.2.3", "major", "2.0.0"),
            (None, "patch", "1.0.0"),
            ("", "patch", "1.0.0"),
            ("invalid", "patch", "1.0.0"),
            ("1.2", "patch", "1.0.0"),
            ("a.b.c", "patch", "1.0.0"),
            ("1.0.0", "minor", "1.1.0"),
            ("0.0.0", "major", "1.0.0"),
        ],
    )
    def test_bump(self, version, level, expected):
        assert _bump_version(version, level) == expected


class TestComputeScriptHash:
    def test_format_and_value(self, tmp_path):
        f = tmp_path / "x.sh"
        f.write_bytes(b"hello")
        digest = compute_script_hash(f)
        assert digest.startswith(HASH_PREFIX)
        # sha256("hello") is well-known.
        expected = hashlib.sha256(b"hello").hexdigest()
        assert digest == HASH_PREFIX + expected

    def test_changes_with_content(self, tmp_path):
        f = tmp_path / "x.sh"
        f.write_bytes(b"a")
        h1 = compute_script_hash(f)
        f.write_bytes(b"b")
        h2 = compute_script_hash(f)
        assert h1 != h2


@pytest.fixture
def manifest(tmp_path):
    """Two-script manifest with matching hashes; modify files to create drift."""
    (tmp_path / "foo.sh").write_text("echo foo\n")
    (tmp_path / "bar.py").write_text("print('bar')\n")
    cfg = tmp_path / "_config.json"
    cfg.write_text(
        json.dumps(
            {
                "scripts": {
                    "foo": {
                        "path": "foo.sh",
                        "version": "1.0.0",
                        "hash": compute_script_hash(tmp_path / "foo.sh"),
                    },
                    "bar": {
                        "path": "bar.py",
                        "version": "1.0.0",
                        "hash": compute_script_hash(tmp_path / "bar.py"),
                    },
                }
            },
            indent=2,
        )
    )
    return cfg


class TestSyncManifest:
    def test_clean_no_changes(self, manifest):
        assert sync_manifest(manifest) == []

    def test_drift_bumps_patch_and_writes(self, manifest, tmp_path):
        (tmp_path / "foo.sh").write_text("echo updated\n")
        changes = sync_manifest(manifest)
        assert [c.name for c in changes] == ["foo"]
        assert changes[0].old_version == "1.0.0"
        assert changes[0].new_version == "1.0.1"
        # File on disk reflects the update.
        data = json.loads(manifest.read_text())
        assert data["scripts"]["foo"]["version"] == "1.0.1"
        assert data["scripts"]["foo"]["hash"] != changes[0].old_hash

    def test_check_only_does_not_write(self, manifest, tmp_path):
        (tmp_path / "foo.sh").write_text("echo updated\n")
        original = manifest.read_text()
        changes = sync_manifest(manifest, check_only=True)
        assert len(changes) == 1
        assert manifest.read_text() == original

    def test_names_filter_restricts_scope(self, manifest, tmp_path):
        (tmp_path / "foo.sh").write_text("echo updated\n")
        (tmp_path / "bar.py").write_text("print('updated')\n")
        changes = sync_manifest(manifest, names=["bar"])
        assert [c.name for c in changes] == ["bar"]
        data = json.loads(manifest.read_text())
        # foo wasn't touched even though it has drift.
        assert data["scripts"]["foo"]["version"] == "1.0.0"
        assert data["scripts"]["bar"]["version"] == "1.0.1"

    def test_force_bump_without_drift(self, manifest):
        changes = sync_manifest(manifest, names=["foo"], bump="major", force=True)
        assert len(changes) == 1
        assert changes[0].old_version == "1.0.0"
        assert changes[0].new_version == "2.0.0"

    def test_unknown_name_raises(self, manifest):
        with pytest.raises(KeyError, match="nonexistent"):
            sync_manifest(manifest, names=["nonexistent"])

    def test_unknown_name_lists_known_ones(self, manifest):
        with pytest.raises(KeyError, match="foo, bar|bar, foo"):
            sync_manifest(manifest, names=["nonexistent"])

    def test_missing_version_sets_1_0_0(self, manifest):
        data = json.loads(manifest.read_text())
        del data["scripts"]["foo"]["version"]
        manifest.write_text(json.dumps(data))
        changes = sync_manifest(manifest)
        assert any(c.name == "foo" and c.new_version == "1.0.0" for c in changes)

    def test_missing_file_skipped(self, manifest, tmp_path):
        # Add a phantom entry that points to a missing file.
        data = json.loads(manifest.read_text())
        data["scripts"]["ghost"] = {"path": "nonexistent.sh", "version": "1.0.0"}
        manifest.write_text(json.dumps(data))
        changes = sync_manifest(manifest)
        # ghost is silently skipped; only real drift would appear.
        assert all(c.name != "ghost" for c in changes)

    def test_check_only_does_not_raise_for_synced_unknown(self, manifest):
        """Sanity: --check on a fully-synced manifest is a no-op."""
        assert sync_manifest(manifest, check_only=True) == []


class TestAuditManifest:
    def test_returns_updated_data_without_writing(self, manifest, tmp_path):
        original = manifest.read_text()
        (tmp_path / "foo.sh").write_text("echo updated\n")
        data, changes = audit_manifest(manifest)
        assert len(changes) == 1
        assert data["scripts"]["foo"]["version"] == "1.0.1"
        # File untouched.
        assert manifest.read_text() == original


class TestSharedModules:
    """A script may declare sibling .py modules it imports."""

    @pytest.fixture
    def shared(self, tmp_path):
        (tmp_path / "shared.py").write_text("VALUE = 1\n")
        (tmp_path / "a.py").write_text("import shared\n")
        (tmp_path / "b.py").write_text("import shared\n")
        entry_hash = compute_entry_hash([tmp_path / "a.py", tmp_path / "shared.py"])
        cfg = tmp_path / "_config.json"
        cfg.write_text(
            json.dumps(
                {
                    "scripts": {
                        "a": {
                            "path": "a.py",
                            "modules": ["shared.py"],
                            "version": "1.0.0",
                            "hash": entry_hash,
                        },
                        "b": {
                            "path": "b.py",
                            "modules": ["shared.py"],
                            "version": "2.0.0",
                            "hash": compute_entry_hash(
                                [tmp_path / "b.py", tmp_path / "shared.py"]
                            ),
                        },
                    }
                },
                indent=2,
            )
        )
        return cfg

    def test_entry_hash_of_one_file_matches_the_plain_hash(self, tmp_path):
        path = tmp_path / "solo.py"
        path.write_text("x = 1\n")
        assert compute_entry_hash([path]) == compute_script_hash(path)

    def test_entry_hash_covers_the_module(self, tmp_path):
        script = tmp_path / "s.py"
        module = tmp_path / "m.py"
        script.write_text("import m\n")
        module.write_text("A = 1\n")
        first = compute_entry_hash([script, module])
        module.write_text("A = 2\n")
        assert compute_entry_hash([script, module]) != first

    def test_entry_hash_is_order_sensitive_but_stable(self, tmp_path):
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("1")
        b.write_text("2")
        assert compute_entry_hash([a, b]) == compute_entry_hash([a, b])
        assert compute_entry_hash([a, b]) != compute_entry_hash([b, a])

    def test_manifest_in_sync_with_modules(self, shared):
        assert sync_manifest(shared) == []

    def test_editing_a_module_bumps_every_dependent(self, shared, tmp_path):
        (tmp_path / "shared.py").write_text("VALUE = 2\n")
        changes = sync_manifest(shared)
        assert {c.name for c in changes} == {"a", "b"}
        data = json.loads(shared.read_text())
        assert data["scripts"]["a"]["version"] == "1.0.1"
        assert data["scripts"]["b"]["version"] == "2.0.1"

    def test_editing_one_script_leaves_the_sibling_alone(self, shared, tmp_path):
        (tmp_path / "a.py").write_text("import shared  # tweak\n")
        assert [c.name for c in sync_manifest(shared)] == ["a"]

    def test_missing_module_is_skipped_not_crashed(self, shared, tmp_path):
        (tmp_path / "shared.py").unlink()
        assert sync_manifest(shared) == []
