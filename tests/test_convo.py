"""Tests for scripts/convo.py — packing and restoring agent sessions.

The interesting failures here are not "the archive was wrong" but "restoring
someone's backup wrote outside their home", "the database came back
corrupt", and "the backup silently dropped half the sessions". So the bulk
of this file is about refusing hostile archive members, snapshotting live
SQLite, and skipping only what genuinely cannot be restored.

Nothing touches the real home directory: every test builds a fake one.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tarfile
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import convo
from convo import (
    TOOLS_BY_NAME,
    UnsafeMember,
    is_secret,
    is_sqlite,
    matches_any,
    pack,
    read_manifest,
    resolve_tools,
    restore,
    safe_member_path,
    snapshot_sqlite,
    walk_tool,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home with nothing in it; tests add what they need."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(convo.Path, "home", staticmethod(lambda: fake))
    return fake


def make_tree(root: Path, files: dict[str, bytes | str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_bytes(content)
    return root


def make_db(path: Path, rows: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE session (id INTEGER PRIMARY KEY, body TEXT)")
    con.executemany(
        "INSERT INTO session (body) VALUES (?)", [(f"row {i}",) for i in range(rows)]
    )
    con.commit()
    con.close()
    return path


def copilot_home(home: Path, **files) -> Path:
    return make_tree(
        home / ".copilot",
        files or {"session-state/a/events.jsonl": '{"a": 1}\n', "config.json": "{}"},
    )


# -- the registry ----------------------------------------------------------


class TestToolRegistry:
    def test_the_three_agents_are_known(self):
        assert set(TOOLS_BY_NAME) == {"copilot", "codex", "claude"}

    @pytest.mark.parametrize(
        "name,root",
        [("copilot", ".copilot"), ("codex", ".codex"), ("claude", ".claude")],
    )
    def test_each_points_at_its_state_directory(self, name, root):
        assert TOOLS_BY_NAME[name].relative_root == root

    def test_resolve_is_case_insensitive(self):
        assert resolve_tools(["CoPilot"])[0].name == "copilot"

    def test_resolve_deduplicates(self):
        assert len(resolve_tools(["codex", "codex"])) == 1

    def test_an_unknown_tool_lists_the_known_ones(self):
        with pytest.raises(Exception, match="claude"):
            resolve_tools(["emacs"])

    def test_exists_follows_the_home(self, home):
        assert TOOLS_BY_NAME["copilot"].exists(home) is False
        copilot_home(home)
        assert TOOLS_BY_NAME["copilot"].exists(home) is True

    def test_a_file_where_the_root_should_be_is_not_a_tool(self, home):
        (home / ".codex").write_text("not a directory")
        assert TOOLS_BY_NAME["codex"].exists(home) is False


# -- selection -------------------------------------------------------------


class TestSelection:
    def _walk(self, home, **kwargs):
        return walk_tool(TOOLS_BY_NAME["copilot"], home=home, **kwargs)

    def test_an_absent_tool_selects_nothing(self, home):
        selection = self._walk(home)
        assert selection.files == [] and selection.total_bytes == 0

    def test_files_are_named_under_the_tool(self, home):
        copilot_home(home, **{"a/b.jsonl": "x"})
        assert self._walk(home).files[0][1] == "copilot/a/b.jsonl"

    def test_sizes_are_summed(self, home):
        copilot_home(home, **{"a.txt": "12345", "b.txt": "123"})
        assert self._walk(home).total_bytes == 8

    def test_nested_directories_are_walked(self, home):
        copilot_home(home, **{"a/b/c/d/deep.jsonl": "x"})
        assert "copilot/a/b/c/d/deep.jsonl" in [n for _, n in self._walk(home).files]

    def test_an_empty_directory_contributes_nothing(self, home):
        (home / ".copilot" / "empty").mkdir(parents=True)
        assert self._walk(home).files == []

    def test_the_listing_is_deterministic(self, home):
        copilot_home(home, **{f"f{i}.txt": "x" for i in range(20)})
        assert [n for _, n in self._walk(home).files] == [
            n for _, n in self._walk(home).files
        ]

    @pytest.mark.parametrize(
        "rel", ["session-store.db-wal", "session-store.db-shm", "tmp/x", "a/.DS_Store"]
    )
    def test_junk_is_excluded_by_default(self, home, rel):
        copilot_home(home, **{rel: "x", "keep.jsonl": "y"})
        names = [n for _, n in self._walk(home).files]
        assert "copilot/keep.jsonl" in names
        assert f"copilot/{rel}" not in names

    def test_junk_can_be_kept(self, home):
        copilot_home(home, **{"tmp/x": "x"})
        names = [n for _, n in self._walk(home, include_junk=True).files]
        assert "copilot/tmp/x" in names

    @pytest.mark.parametrize("rel", ["logs/today.log", "a.log", "logs/nested/b.txt"])
    def test_logs_are_excluded_by_default(self, home, rel):
        copilot_home(home, **{rel: "x"})
        assert self._walk(home).files == []

    def test_logs_can_be_kept(self, home):
        copilot_home(home, **{"logs/today.log": "x"})
        assert len(self._walk(home, include_logs=True).files) == 1

    def test_extra_excludes_are_honoured(self, home):
        copilot_home(home, **{"drop/me.txt": "x", "keep.txt": "y"})
        names = [n for _, n in self._walk(home, extra_excludes=["drop/*"]).files]
        assert names == ["copilot/keep.txt"]

    def test_a_socket_is_skipped_not_archived(self, home, monkeypatch):
        """Codex leaves a unix socket in ~/.codex/ipc; tar cannot carry it."""
        copilot_home(home, **{"keep.txt": "x"})
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # AF_UNIX paths are capped near 108 bytes and pytest's tmp_path is
        # deep, so bind relative to the directory itself.
        monkeypatch.chdir(home / ".copilot")
        try:
            server.bind("agent.sock")
            selection = self._walk(home, include_junk=True)
            assert selection.skipped_special >= 1
            assert "copilot/agent.sock" not in [n for _, n in selection.files]
        finally:
            server.close()

    def test_a_fifo_is_skipped(self, home):
        copilot_home(home, **{"keep.txt": "x"})
        fifo = home / ".copilot" / "pipe"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError):  # pragma: no cover
            pytest.skip("no fifo support here")
        selection = self._walk(home, include_junk=True)
        assert selection.skipped_special >= 1

    def test_a_symlink_to_a_file_is_skipped(self, home):
        """A link is machine-specific; restoring it would point at nothing."""
        copilot_home(home, **{"real.txt": "x"})
        (home / ".copilot" / "link.txt").symlink_to(home / ".copilot" / "real.txt")
        selection = self._walk(home)
        assert [n for _, n in selection.files] == ["copilot/real.txt"]
        assert selection.skipped_special == 1

    def test_a_symlinked_directory_is_not_descended(self, home, tmp_path):
        """Otherwise a link to / walks the whole filesystem."""
        outside = tmp_path / "outside"
        make_tree(outside, {"secret.txt": "x"})
        copilot_home(home, **{"keep.txt": "y"})
        (home / ".copilot" / "escape").symlink_to(outside)
        names = [n for _, n in self._walk(home).files]
        assert names == ["copilot/keep.txt"]

    def test_an_unreadable_file_is_counted_not_fatal(self, home):
        copilot_home(home, **{"secret.bin": "x", "ok.txt": "y"})
        target = home / ".copilot" / "secret.bin"
        os.chmod(target, 0o000)
        try:
            if os.access(target, os.R_OK):  # pragma: no cover - root
                pytest.skip("cannot make a file unreadable as this user")
            selection = self._walk(home)
            assert selection.skipped_unreadable == 1
            assert [n for _, n in selection.files] == ["copilot/ok.txt"]
        finally:
            os.chmod(target, 0o644)

    def test_secrets_are_counted_but_still_packed(self, home):
        copilot_home(home, **{"auth.json": "{}", "notes.txt": "x"})
        selection = self._walk(home)
        assert selection.secrets == 1
        assert len(selection.files) == 2

    def test_secrets_can_be_dropped(self, home):
        copilot_home(home, **{"auth.json": "{}", "notes.txt": "x"})
        names = [n for _, n in self._walk(home, exclude_secrets=True).files]
        assert names == ["copilot/notes.txt"]

    @pytest.mark.parametrize(
        "name",
        [
            "auth.json",
            "id.key",
            "cert.pem",
            "my-token.txt",
            "API_KEY.txt",
            "credentials.json",
        ],
    )
    def test_secret_names_are_recognised(self, name):
        assert is_secret(name) is True

    @pytest.mark.parametrize("name", ["events.jsonl", "config.json", "notes.md"])
    def test_ordinary_names_are_not_secrets(self, name):
        assert is_secret(name) is False


class TestGlobMatching:
    @pytest.mark.parametrize(
        "rel,pattern,expected",
        [
            ("a.log", "*.log", True),
            ("logs/a.txt", "logs/*", True),
            ("x/logs/a.txt", "*/logs/*", True),
            ("events.jsonl", "*.log", False),
            ("db.sqlite-wal", "*-wal", True),
        ],
    )
    def test_patterns(self, rel, pattern, expected):
        assert matches_any(rel, [pattern]) is expected

    def test_no_patterns_matches_nothing(self):
        assert matches_any("anything", []) is False


# -- SQLite ----------------------------------------------------------------


class TestSqliteHandling:
    def test_detects_a_real_database(self, tmp_path):
        assert is_sqlite(make_db(tmp_path / "a.db")) is True

    def test_a_text_file_named_db_is_not_one(self, tmp_path):
        (tmp_path / "fake.db").write_text("definitely not sqlite")
        assert is_sqlite(tmp_path / "fake.db") is False

    def test_a_database_with_the_wrong_suffix_is_ignored(self, tmp_path):
        renamed = tmp_path / "data.bin"
        make_db(tmp_path / "a.db").replace(renamed)
        assert is_sqlite(renamed) is False

    def test_an_empty_file_is_not_a_database(self, tmp_path):
        (tmp_path / "empty.db").touch()
        assert is_sqlite(tmp_path / "empty.db") is False

    def test_a_missing_file_is_not_a_database(self, tmp_path):
        assert is_sqlite(tmp_path / "nope.db") is False

    def test_a_snapshot_is_readable_and_complete(self, tmp_path):
        src = make_db(tmp_path / "src.db", rows=25)
        dst = tmp_path / "out" / "snap.db"
        assert snapshot_sqlite(src, dst) is True
        con = sqlite3.connect(str(dst))
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM session").fetchone()[0] == 25
        con.close()

    def test_a_snapshot_captures_uncommitted_wal_content(self, tmp_path):
        """With WAL on, recent commits live outside the .db file itself."""
        src = tmp_path / "wal.db"
        con = sqlite3.connect(str(src))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES ('in-wal')")
        con.commit()
        try:
            dst = tmp_path / "snap.db"
            assert snapshot_sqlite(src, dst) is True
            other = sqlite3.connect(str(dst))
            assert other.execute("SELECT v FROM t").fetchone()[0] == "in-wal"
            other.close()
        finally:
            con.close()

    def test_a_snapshot_survives_a_concurrent_writer(self, tmp_path):
        """The whole reason not to use a plain file copy."""
        src = make_db(tmp_path / "busy.db")
        con = sqlite3.connect(str(src))
        con.execute("PRAGMA journal_mode=WAL")
        con.commit()
        stop = threading.Event()

        def churn():
            writer = sqlite3.connect(str(src))
            while not stop.is_set():
                writer.execute("INSERT INTO session (body) VALUES ('x')")
                writer.commit()
            writer.close()

        thread = threading.Thread(target=churn)
        thread.start()
        try:
            time.sleep(0.05)
            dst = tmp_path / "snap.db"
            assert snapshot_sqlite(src, dst) is True
            other = sqlite3.connect(str(dst))
            assert other.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            other.close()
        finally:
            stop.set()
            thread.join(timeout=5)
            con.close()

    def test_snapshotting_a_non_database_reports_failure(self, tmp_path):
        (tmp_path / "junk.db").write_text("not sqlite")
        assert snapshot_sqlite(tmp_path / "junk.db", tmp_path / "out.db") is False

    def test_snapshotting_a_missing_file_reports_failure(self, tmp_path):
        assert snapshot_sqlite(tmp_path / "gone.db", tmp_path / "out.db") is False

    def test_databases_are_listed_in_the_selection(self, home):
        copilot_home(home, **{"notes.txt": "x"})
        make_db(home / ".copilot" / "session-store.db")
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        assert len(selection.databases) == 1


# -- archive safety --------------------------------------------------------


class TestSafeMemberPath:
    """An archive is untrusted input even when you made it."""

    @pytest.mark.parametrize(
        "name",
        [
            "../escape.txt",
            "../../etc/passwd",
            "a/../../b.txt",
            "a/../b.txt",
            "./../x",
            "/etc/passwd",
            "/absolute.txt",
            "..",
            "",
            ".",
            "/",
        ],
    )
    def test_hostile_names_are_refused(self, tmp_path, name):
        with pytest.raises(UnsafeMember):
            safe_member_path(name, tmp_path)

    @pytest.mark.parametrize(
        "name", ["a.txt", "a/b.txt", "a/b/c/d.jsonl", "with space.txt", "dot.name.txt"]
    )
    def test_ordinary_names_are_allowed(self, tmp_path, name):
        assert safe_member_path(name, tmp_path) == (tmp_path / name).resolve()

    def test_a_windows_drive_is_refused(self, tmp_path):
        with pytest.raises(UnsafeMember):
            safe_member_path("C:/windows/system32", tmp_path)

    def test_a_name_that_only_looks_like_traversal_is_fine(self, tmp_path):
        assert safe_member_path("..hidden/x.txt", tmp_path)

    def test_a_member_escaping_through_a_symlinked_subdirectory_is_refused(
        self, tmp_path
    ):
        """No ".." anywhere, yet it still lands outside: only resolution catches it."""
        dest = tmp_path / "dest"
        dest.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (dest / "sub").symlink_to(outside)
        with pytest.raises(UnsafeMember):
            safe_member_path("sub/pwned.txt", dest)

    def test_a_symlinked_destination_is_resolved_consistently(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert safe_member_path("a.txt", link) == (real / "a.txt").resolve()


class TestRestoreRefusesHostileArchives:
    def _archive(self, tmp_path, members: dict[str, bytes], manifest=True):
        path = tmp_path / "eco.tar"
        with tarfile.open(path, "w") as archive:
            if manifest:
                payload = json.dumps(
                    {"manifest_version": 1, "tools": {"copilot": {}}}
                ).encode()
                info = tarfile.TarInfo(convo.MANIFEST_NAME)
                info.size = len(payload)
                archive.addfile(info, convo.io_bytes(payload))
            for name, body in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, convo.io_bytes(body))
        return path

    def test_a_traversing_member_is_refused_and_writes_nothing(self, tmp_path):
        archive = self._archive(tmp_path, {"copilot/../../pwned.txt": b"x"})
        dest = tmp_path / "dest"
        report = restore(archive, into=dest)
        assert report.written == 0
        assert report.refused
        assert not (tmp_path.parent / "pwned.txt").exists()

    def test_an_absolute_member_is_refused(self, tmp_path):
        archive = self._archive(tmp_path, {"copilot//etc/passwd": b"x"})
        report = restore(archive, into=tmp_path / "dest")
        assert report.written == 0 and report.refused

    def test_a_member_for_an_unknown_tool_is_refused(self, tmp_path):
        archive = self._archive(tmp_path, {"evil/x.txt": b"x"})
        report = restore(archive, into=tmp_path / "dest")
        assert report.written == 0 and report.refused == ["evil/x.txt"]

    def test_a_top_level_member_is_refused(self, tmp_path):
        archive = self._archive(tmp_path, {"loose.txt": b"x"})
        report = restore(archive, into=tmp_path / "dest")
        assert report.written == 0 and report.refused == ["loose.txt"]

    def test_a_symlink_member_is_refused(self, tmp_path):
        """A link member is the other half of the traversal problem."""
        path = tmp_path / "link.tar"
        with tarfile.open(path, "w") as archive:
            payload = json.dumps({"manifest_version": 1, "tools": {}}).encode()
            info = tarfile.TarInfo(convo.MANIFEST_NAME)
            info.size = len(payload)
            archive.addfile(info, convo.io_bytes(payload))
            link = tarfile.TarInfo("copilot/evil")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        report = restore(path, into=tmp_path / "dest")
        assert report.refused == ["copilot/evil"]
        assert not (tmp_path / "dest" / "copilot" / "evil").exists()

    def test_a_device_member_is_refused(self, tmp_path):
        path = tmp_path / "dev.tar"
        with tarfile.open(path, "w") as archive:
            payload = json.dumps({"manifest_version": 1, "tools": {}}).encode()
            info = tarfile.TarInfo(convo.MANIFEST_NAME)
            info.size = len(payload)
            archive.addfile(info, convo.io_bytes(payload))
            node = tarfile.TarInfo("copilot/null")
            node.type = tarfile.CHRTYPE
            node.devmajor, node.devminor = 1, 3
            archive.addfile(node)
        assert restore(path, into=tmp_path / "dest").refused == ["copilot/null"]

    def test_good_members_still_restore_alongside_bad_ones(self, tmp_path):
        archive = self._archive(
            tmp_path, {"copilot/../evil": b"x", "copilot/good.txt": b"fine"}
        )
        dest = tmp_path / "dest"
        report = restore(archive, into=dest)
        assert report.written == 1 and report.refused
        assert (dest / "copilot" / "good.txt").read_bytes() == b"fine"


# -- round trip ------------------------------------------------------------


class TestRoundTrip:
    def _pack(self, home, tmp_path, **kwargs):
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home, **kwargs)
        archive = tmp_path / "out.tar.gz"
        manifest = pack({"copilot": selection}, archive, compress="gz")
        return archive, manifest, selection

    def test_content_survives(self, home, tmp_path):
        copilot_home(home, **{"session-state/a/events.jsonl": "line one\nline two\n"})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        assert (
            dest / "copilot" / "session-state" / "a" / "events.jsonl"
        ).read_text() == "line one\nline two\n"

    def test_a_database_round_trips_intact(self, home, tmp_path):
        copilot_home(home, **{"notes.txt": "x"})
        make_db(home / ".copilot" / "session-store.db", rows=7)
        archive, manifest, _ = self._pack(home, tmp_path)
        assert manifest["tools"]["copilot"]["databases"] == 1
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        con = sqlite3.connect(str(dest / "copilot" / "session-store.db"))
        assert con.execute("SELECT count(*) FROM session").fetchone()[0] == 7
        con.close()

    def test_directory_structure_is_recreated(self, home, tmp_path):
        copilot_home(home, **{"a/b/c/deep.txt": "x"})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        assert (dest / "copilot" / "a" / "b" / "c" / "deep.txt").exists()

    def test_binary_content_is_byte_exact(self, home, tmp_path):
        blob = bytes(range(256)) * 40
        copilot_home(home, **{"blob.bin": blob})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        assert (dest / "copilot" / "blob.bin").read_bytes() == blob

    def test_unicode_paths_and_content_survive(self, home, tmp_path):
        copilot_home(home, **{"会话/记录.jsonl": "内容 ✓\n"})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        assert (dest / "copilot" / "会话" / "记录.jsonl").read_text() == "内容 ✓\n"

    def test_an_empty_file_survives(self, home, tmp_path):
        copilot_home(home, **{"empty.txt": ""})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        assert (dest / "copilot" / "empty.txt").read_bytes() == b""

    @pytest.mark.parametrize("compress", ["gz", "xz", "bz2", "none"])
    def test_every_compressor_round_trips(self, home, tmp_path, compress):
        copilot_home(home, **{"a.txt": "hello"})
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        archive = tmp_path / f"out.{compress}"
        pack({"copilot": selection}, archive, compress=compress)
        dest = tmp_path / f"dest-{compress}"
        restore(archive, into=dest)
        assert (dest / "copilot" / "a.txt").read_text() == "hello"

    def test_existing_files_are_kept_unless_merging(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "new"})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        (dest / "copilot").mkdir(parents=True)
        (dest / "copilot" / "a.txt").write_text("existing")
        report = restore(archive, into=dest)
        assert report.skipped_existing == 1 and report.written == 0
        assert (dest / "copilot" / "a.txt").read_text() == "existing"

    def test_merge_overwrites(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "new"})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        (dest / "copilot").mkdir(parents=True)
        (dest / "copilot" / "a.txt").write_text("existing")
        restore(archive, into=dest, merge=True)
        assert (dest / "copilot" / "a.txt").read_text() == "new"

    def test_dry_run_writes_nothing(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        archive, _, _ = self._pack(home, tmp_path)
        dest = tmp_path / "dest"
        report = restore(archive, into=dest, dry_run=True)
        assert report.written == 1
        assert not dest.exists() or list(dest.rglob("*")) == []

    def test_restoring_one_tool_only(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        make_tree(home / ".codex", {"b.txt": "y"})
        selections = {
            name: walk_tool(TOOLS_BY_NAME[name], home=home)
            for name in ("copilot", "codex")
        }
        archive = tmp_path / "both.tar.gz"
        pack(selections, archive)
        dest = tmp_path / "dest"
        report = restore(archive, into=dest, only=["codex"])
        assert report.tools == {"codex"}
        assert not (dest / "copilot").exists()

    def test_restore_into_the_home_lands_in_the_tool_root(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        archive, _, _ = self._pack(home, tmp_path)
        fresh = tmp_path / "fresh-home"
        fresh.mkdir()
        restore(archive, home=fresh)
        assert (fresh / ".copilot" / "a.txt").read_text() == "x"

    def test_a_file_vanishing_mid_pack_is_survivable(self, home, tmp_path):
        copilot_home(home, **{"stays.txt": "x", "goes.txt": "y"})
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        (home / ".copilot" / "goes.txt").unlink()
        archive = tmp_path / "out.tar.gz"
        pack({"copilot": selection}, archive)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        assert (dest / "copilot" / "stays.txt").exists()

    def test_an_interrupted_pack_leaves_no_archive(self, home, tmp_path, monkeypatch):
        """A half-written file must not look like a finished backup."""
        copilot_home(home, **{"a.txt": "x"})
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        archive = tmp_path / "out.tar.gz"

        def boom(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(convo, "build_manifest", boom)
        with pytest.raises(KeyboardInterrupt):
            pack({"copilot": selection}, archive)
        assert not archive.exists()
        assert not list(tmp_path.glob("*.partial"))


# -- manifest --------------------------------------------------------------


class TestManifest:
    def test_records_what_was_packed(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "12345"})
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        archive = tmp_path / "out.tar.gz"
        pack({"copilot": selection}, archive)
        manifest = read_manifest(archive)
        assert manifest["manifest_version"] == convo.MANIFEST_VERSION
        assert manifest["tools"]["copilot"]["files"] == 1
        assert manifest["tools"]["copilot"]["bytes"] == 5
        assert manifest["tools"]["copilot"]["root"] == ".copilot"

    def test_the_manifest_comes_first_in_the_archive(self, home, tmp_path):
        """So `info` can read it without decompressing the whole thing."""
        copilot_home(home, **{f"f{i}.txt": "x" for i in range(10)})
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        archive = tmp_path / "out.tar.gz"
        pack({"copilot": selection}, archive)
        with tarfile.open(archive) as handle:
            assert handle.next().name == convo.MANIFEST_NAME

    def test_an_archive_without_a_manifest_is_rejected(self, tmp_path):
        path = tmp_path / "plain.tar"
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo("copilot/a.txt")
            info.size = 1
            archive.addfile(info, convo.io_bytes(b"x"))
        with pytest.raises(Exception, match="manifest"):
            read_manifest(path)

    def test_a_corrupt_manifest_is_reported(self, tmp_path):
        path = tmp_path / "bad.tar"
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo(convo.MANIFEST_NAME)
            info.size = 3
            archive.addfile(info, convo.io_bytes(b"{;;"))
        with pytest.raises(Exception, match="corrupt"):
            read_manifest(path)

    def test_a_file_that_is_not_a_tar_is_reported(self, tmp_path):
        path = tmp_path / "notatar.tar.gz"
        path.write_text("definitely not a tar")
        with pytest.raises(Exception, match="cannot read"):
            read_manifest(path)


# -- CLI -------------------------------------------------------------------


class TestCommandLine:
    def invoke(self, runner, *args):
        return runner.invoke(convo.cli, list(args))

    def test_ls_on_an_empty_home(self, home, runner):
        result = self.invoke(runner, "ls")
        assert result.exit_code == 0
        assert "Nothing to pack" in result.output

    def test_ls_shows_a_present_tool(self, home, runner):
        copilot_home(home)
        result = self.invoke(runner, "ls")
        assert result.exit_code == 0 and "copilot" in result.output

    def test_the_bare_group_lists(self, home, runner):
        copilot_home(home)
        assert "copilot" in self.invoke(runner).output

    def test_pack_with_nothing_present_is_a_clean_error(self, home, runner):
        result = self.invoke(runner, "pack")
        assert result.exit_code != 0 and "No agent state" in result.output
        assert "Traceback" not in result.output

    def test_pack_writes_an_archive(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "out.tar.gz"
        result = self.invoke(runner, "pack", "-o", str(target))
        assert result.exit_code == 0, result.output
        assert target.exists() and tarfile.is_tarfile(target)

    def test_pack_accepts_a_positional_output(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "positional.tar.gz"
        assert self.invoke(runner, "pack", str(target)).exit_code == 0
        assert target.exists()

    def test_pack_dry_run_writes_nothing(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "out.tar.gz"
        result = self.invoke(runner, "pack", "-o", str(target), "--dry-run")
        assert result.exit_code == 0 and not target.exists()
        assert "Would pack" in result.output

    def test_pack_warns_about_credentials(self, home, runner, tmp_path):
        copilot_home(home, **{"auth.json": "{}", "a.txt": "x"})
        result = self.invoke(
            runner, "pack", "-o", str(tmp_path / "o.tar.gz"), "--dry-run"
        )
        assert "credentials" in result.output

    def test_pack_reports_skipped_specials(self, home, runner, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        fifo = home / ".copilot" / "pipe"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError):  # pragma: no cover
            pytest.skip("no fifo support here")
        result = self.invoke(
            runner, "pack", "-o", str(tmp_path / "o.tar.gz"), "--dry-run"
        )
        assert "skipped" in result.output

    def test_pack_refuses_an_unknown_tool(self, home, runner):
        copilot_home(home)
        result = self.invoke(runner, "pack", "--tool", "nano")
        assert result.exit_code != 0 and "Unknown tool" in result.output

    def test_pack_with_everything_excluded_is_a_clean_error(
        self, home, runner, tmp_path
    ):
        copilot_home(home, **{"a.txt": "x"})
        result = self.invoke(
            runner, "pack", "-o", str(tmp_path / "o.tar.gz"), "--exclude", "*"
        )
        assert result.exit_code != 0 and "Nothing to pack" in result.output

    def test_info_summarises(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "out.tar.gz"
        self.invoke(runner, "pack", "-o", str(target))
        result = self.invoke(runner, "info", str(target))
        assert result.exit_code == 0 and "copilot" in result.output

    def test_info_json_parses(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "out.tar.gz"
        self.invoke(runner, "pack", "-o", str(target))
        data = json.loads(self.invoke(runner, "info", str(target), "--json").output)
        assert data["tools"]["copilot"]["files"] >= 1

    def test_info_on_a_missing_file(self, home, runner, tmp_path):
        assert self.invoke(runner, "info", str(tmp_path / "nope.tar.gz")).exit_code != 0

    def test_restore_asks_before_writing(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "out.tar.gz"
        self.invoke(runner, "pack", "-o", str(target))
        result = runner.invoke(
            convo.cli,
            ["restore", str(target), "--into", str(tmp_path / "d")],
            input="n\n",
        )
        assert result.exit_code != 0
        assert not (tmp_path / "d").exists()

    def test_restore_with_yes_proceeds(self, home, runner, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        target = tmp_path / "out.tar.gz"
        self.invoke(runner, "pack", "-o", str(target))
        dest = tmp_path / "d"
        result = self.invoke(
            runner, "restore", str(target), "--into", str(dest), "--yes"
        )
        assert result.exit_code == 0
        assert (dest / "copilot" / "a.txt").read_text() == "x"

    def test_restore_dry_run_needs_no_confirmation(self, home, runner, tmp_path):
        copilot_home(home)
        target = tmp_path / "out.tar.gz"
        self.invoke(runner, "pack", "-o", str(target))
        result = self.invoke(
            runner, "restore", str(target), "--into", str(tmp_path / "d"), "--dry-run"
        )
        assert result.exit_code == 0 and "would restore" in result.output

    @pytest.mark.parametrize("args", [[], ["ls"], ["pack"], ["info"], ["restore"]])
    def test_help_works_everywhere(self, runner, args):
        result = runner.invoke(convo.cli, args + ["-h"])
        assert result.exit_code == 0 and "Usage:" in result.output

    @pytest.mark.parametrize("width", ["40", "60", "80", "120", "200"])
    def test_ls_survives_any_width(self, home, runner, monkeypatch, width):
        monkeypatch.setenv("COLUMNS", width)
        copilot_home(home)
        assert self.invoke(runner, "ls").exit_code == 0


class TestArchiveNaming:
    def test_the_default_name_carries_host_and_extension(self):
        name = convo.default_archive_name("gz")
        assert name.startswith("usm-convo-") and name.endswith(".tar.gz")

    @pytest.mark.parametrize(
        "compress,suffix",
        [("gz", ".tar.gz"), ("xz", ".tar.xz"), ("bz2", ".tar.bz2"), ("none", ".tar")],
    )
    def test_each_compressor_has_its_suffix(self, compress, suffix):
        assert convo.default_archive_name(compress).endswith(suffix)


class TestLiveDatabasesArePackedConsistently:
    """A plain file copy is not good enough, and this proves it.

    With WAL enabled, a freshly committed row lives in the -wal file rather
    than the .db. Since the -wal is deliberately not archived (it is only
    meaningful next to the exact .db it came from), copying the .db alone
    would silently lose recent history. The backup API folds the WAL in.
    """

    def _pack_and_read(self, home, tmp_path, table_query):
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        archive = tmp_path / "out.tar.gz"
        pack({"copilot": selection}, archive)
        dest = tmp_path / "dest"
        restore(archive, into=dest)
        con = sqlite3.connect(str(dest / "copilot" / "session-store.db"))
        try:
            return con.execute(table_query).fetchall()
        finally:
            con.close()

    def test_rows_living_in_the_wal_survive_the_round_trip(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        db = home / ".copilot" / "session-store.db"
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE session (body TEXT)")
        con.execute("INSERT INTO session VALUES ('only-in-wal')")
        con.commit()
        try:
            assert (db.with_name(db.name + "-wal")).exists(), "expected a -wal file"
            rows = self._pack_and_read(home, tmp_path, "SELECT body FROM session")
            assert rows == [("only-in-wal",)]
        finally:
            con.close()

    def test_the_wal_and_shm_are_not_shipped(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        db = home / ".copilot" / "session-store.db"
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v TEXT)")
        con.commit()
        try:
            selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
            archive = tmp_path / "out.tar.gz"
            pack({"copilot": selection}, archive)
            with tarfile.open(archive) as handle:
                names = handle.getnames()
            assert not [n for n in names if n.endswith(("-wal", "-shm"))]
        finally:
            con.close()

    def test_the_restored_database_passes_an_integrity_check(self, home, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        make_db(home / ".copilot" / "session-store.db", rows=50)
        rows = self._pack_and_read(home, tmp_path, "PRAGMA integrity_check")
        assert rows == [("ok",)]


class TestDegradedFilesystems:
    """Backups run on machines that are already in trouble."""

    def test_a_directory_that_cannot_be_listed_is_skipped(self, home):
        copilot_home(home, **{"ok.txt": "x"})
        locked = home / ".copilot" / "locked"
        locked.mkdir()
        (locked / "inside.txt").write_text("y")
        os.chmod(locked, 0o000)
        try:
            if os.access(locked, os.R_OK):  # pragma: no cover - root
                pytest.skip("cannot make a directory unreadable as this user")
            names = [n for _, n in walk_tool(TOOLS_BY_NAME["copilot"], home=home).files]
            assert names == ["copilot/ok.txt"]
        finally:
            os.chmod(locked, 0o700)

    def test_a_file_that_vanishes_between_listing_and_stat(self, home, monkeypatch):
        copilot_home(home, **{"a.txt": "x", "b.txt": "y"})
        real_lstat = Path.lstat

        def flaky(self):
            if self.name == "b.txt":
                raise OSError("vanished")
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", flaky)
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        assert selection.skipped_unreadable == 1
        assert [n for _, n in selection.files] == ["copilot/a.txt"]

    def test_newest_mtime_ignores_unstatable_entries(self, home, monkeypatch):
        copilot_home(home, **{"a.txt": "x"})
        monkeypatch.setattr(
            Path, "lstat", lambda self: (_ for _ in ()).throw(OSError("nope"))
        )
        assert convo.newest_mtime(home / ".copilot") is None

    def test_newest_mtime_on_an_empty_directory(self, home):
        (home / ".copilot").mkdir()
        assert convo.newest_mtime(home / ".copilot") is None

    def test_newest_mtime_reports_the_latest(self, home):
        copilot_home(home, **{"old.txt": "x", "new.txt": "y"})
        newest = convo.newest_mtime(home / ".copilot")
        assert newest is not None and newest > 0

    def test_a_sqlite_file_that_cannot_be_opened_is_still_packed(
        self, home, tmp_path, monkeypatch
    ):
        """A failed snapshot must fall back, not drop the file."""
        copilot_home(home, **{"a.txt": "x"})
        make_db(home / ".copilot" / "session-store.db")
        monkeypatch.setattr(convo, "snapshot_sqlite", lambda src, dst: False)
        selection = walk_tool(TOOLS_BY_NAME["copilot"], home=home)
        archive = tmp_path / "out.tar.gz"
        pack({"copilot": selection}, archive)
        with tarfile.open(archive) as handle:
            assert "copilot/session-store.db" in handle.getnames()

    def test_is_sqlite_on_an_unreadable_file(self, home):
        copilot_home(home, **{"x.db": "y"})
        target = home / ".copilot" / "x.db"
        os.chmod(target, 0o000)
        try:
            if os.access(target, os.R_OK):  # pragma: no cover - root
                pytest.skip("cannot make a file unreadable as this user")
            assert is_sqlite(target) is False
        finally:
            os.chmod(target, 0o644)


class TestPackReporting:
    def test_the_database_count_is_reported(self, home, runner, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        make_db(home / ".copilot" / "session-store.db")
        result = runner.invoke(
            convo.cli, ["pack", "-o", str(tmp_path / "o.tar.gz"), "--dry-run"]
        )
        assert "snapshotted consistently" in result.output

    def test_unreadable_files_are_reported(self, home, runner, tmp_path):
        copilot_home(home, **{"a.txt": "x", "locked.bin": "y"})
        target = home / ".copilot" / "locked.bin"
        os.chmod(target, 0o000)
        try:
            if os.access(target, os.R_OK):  # pragma: no cover - root
                pytest.skip("cannot make a file unreadable as this user")
            result = runner.invoke(
                convo.cli, ["pack", "-o", str(tmp_path / "o.tar.gz"), "--dry-run"]
            )
            assert "unreadable skipped" in result.output
        finally:
            os.chmod(target, 0o644)

    def test_restore_reports_skipped_existing(self, home, runner, tmp_path):
        copilot_home(home, **{"a.txt": "x"})
        target = tmp_path / "o.tar.gz"
        runner.invoke(convo.cli, ["pack", "-o", str(target)])
        dest = tmp_path / "d"
        (dest / "copilot").mkdir(parents=True)
        (dest / "copilot" / "a.txt").write_text("mine")
        result = runner.invoke(
            convo.cli, ["restore", str(target), "--into", str(dest), "--yes"]
        )
        assert "--merge to overwrite" in result.output

    def test_restore_reports_refused_members(self, home, runner, tmp_path):
        path = tmp_path / "bad.tar"
        with tarfile.open(path, "w") as archive:
            payload = json.dumps({"manifest_version": 1, "tools": {}}).encode()
            info = tarfile.TarInfo(convo.MANIFEST_NAME)
            info.size = len(payload)
            archive.addfile(info, convo.io_bytes(payload))
            for name in (
                "copilot/../a",
                "copilot/../b",
                "copilot/../c",
                "copilot/../d",
            ):
                member = tarfile.TarInfo(name)
                member.size = 0
                archive.addfile(member, convo.io_bytes(b""))
        result = runner.invoke(
            convo.cli, ["restore", str(path), "--into", str(tmp_path / "d"), "--yes"]
        )
        assert "refused as unsafe" in result.output and "…" in result.output
