"""Tests for scripts/usm_blocks.py — editing a file somebody else owns.

Four commands write into hand-maintained files (shell rc files, ~/.ssh/config,
~/.tmux.conf). The failure that matters is not "the block was wrong" but "the
user's file was damaged", so these tests are written around preservation:
what is outside the markers must survive byte for byte, a malformed block must
leave the file untouched, and an interrupted write must not truncate anything.

Edge cases get the bulk of the space: CRLF, missing final newline, markers at
either end of the file, markers that look like ours but are not, duplicated
and nested markers, non-UTF8 bytes, symlinks, read-only files, and leftovers
from a previous crashed run.
"""

from __future__ import annotations

import os
import stat

import pytest

from usm_blocks import BlockError, ManagedBlock

BEGIN = "# >>> usm test >>>"
END = "# <<< usm test <<<"


@pytest.fixture
def block():
    return ManagedBlock(BEGIN, END, label="usm test block")


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "config"


def write(path, text, mode=0o644):
    path.write_text(text)
    os.chmod(path, mode)
    return path


# -- construction ----------------------------------------------------------


class TestMarkerValidation:
    """A bad marker pair silently eats the wrong region; refuse it early."""

    @pytest.mark.parametrize("begin,end", [("", "x"), ("x", ""), ("  ", "x")])
    def test_empty_markers_are_refused(self, begin, end):
        with pytest.raises(ValueError, match="non-empty"):
            ManagedBlock(begin, end)

    def test_identical_markers_are_refused(self):
        with pytest.raises(ValueError, match="differ"):
            ManagedBlock("# X", "# X")

    def test_markers_differing_only_in_whitespace_are_refused(self):
        with pytest.raises(ValueError, match="differ"):
            ManagedBlock("# X", "# X  ")

    @pytest.mark.parametrize("bad", ["a\nb", "a\rb"])
    def test_multiline_markers_are_refused(self, bad):
        with pytest.raises(ValueError, match="single line"):
            ManagedBlock(bad, "# END")

    def test_label_defaults_but_is_used_in_errors(self):
        plain = ManagedBlock("# A", "# B")
        assert "managed block" in plain.label
        with pytest.raises(BlockError, match="managed block"):
            plain.split("# B\n")


# -- splitting -------------------------------------------------------------


class TestSplitting:
    def test_absent_block(self, block):
        split = block.split("hello\n")
        assert split.found is False
        assert split.before == "hello\n"
        assert split.block == "" and split.after == ""

    def test_empty_content(self, block):
        assert block.split("").found is False

    def test_block_only(self, block):
        split = block.split(f"{BEGIN}\nx\n{END}\n")
        assert split.found and split.before == "" and split.after == ""
        assert split.block == f"{BEGIN}\nx\n{END}\n"

    def test_block_at_the_start(self, block):
        split = block.split(f"{BEGIN}\nx\n{END}\ntail\n")
        assert split.before == "" and split.after == "tail\n"

    def test_block_at_the_end(self, block):
        split = block.split(f"head\n{BEGIN}\nx\n{END}\n")
        assert split.before == "head\n" and split.after == ""

    def test_block_in_the_middle(self, block):
        split = block.split(f"head\n{BEGIN}\nx\n{END}\ntail\n")
        assert split.before == "head\n" and split.after == "tail\n"
        assert split.without_block == "head\ntail\n"

    def test_an_empty_block_is_still_a_block(self, block):
        split = block.split(f"{BEGIN}\n{END}\n")
        assert split.found and block.body(f"{BEGIN}\n{END}\n") == ""

    def test_body_is_none_without_a_block(self, block):
        assert block.body("nothing here\n") is None

    def test_contains(self, block):
        assert block.contains(f"{BEGIN}\n{END}\n") is True
        assert block.contains("no\n") is False


class TestMalformedBlocksAreRefused:
    """Every one of these must raise and leave the caller's text alone."""

    def test_begin_without_end(self, block):
        with pytest.raises(BlockError, match="incomplete"):
            block.split(f"a\n{BEGIN}\nb\n")

    def test_end_without_begin(self, block):
        with pytest.raises(BlockError, match="incomplete"):
            block.split(f"a\n{END}\nb\n")

    def test_end_before_begin(self, block):
        with pytest.raises(BlockError, match="incomplete"):
            block.split(f"{END}\nx\n{BEGIN}\n")

    def test_duplicate_begin(self, block):
        with pytest.raises(BlockError, match="duplicate begin"):
            block.split(f"{BEGIN}\n{BEGIN}\nx\n{END}\n")

    def test_duplicate_end(self, block):
        with pytest.raises(BlockError, match="duplicate end"):
            block.split(f"{BEGIN}\nx\n{END}\n{END}\n")

    def test_nested_blocks(self, block):
        text = f"{BEGIN}\n{BEGIN}\nx\n{END}\n{END}\n"
        with pytest.raises(BlockError):
            block.split(text)

    def test_two_complete_blocks(self, block):
        """Appending instead of replacing is exactly the bug this prevents."""
        text = f"{BEGIN}\na\n{END}\n{BEGIN}\nb\n{END}\n"
        with pytest.raises(BlockError, match="duplicate"):
            block.split(text)


class TestMarkersAreMatchedAsWholeLines:
    """Substring matching eats the wrong region when a marker is quoted."""

    def test_a_marker_inside_a_longer_line_is_not_a_marker(self, block):
        text = f'echo "{BEGIN}"\n'
        assert block.split(text).found is False

    def test_a_marker_with_a_prefix_is_not_a_marker(self, block):
        assert block.split(f"x {BEGIN}\n").found is False

    def test_a_marker_with_a_suffix_is_not_a_marker(self, block):
        assert block.split(f"{BEGIN} extra\n").found is False

    def test_indented_markers_are_not_ours(self, block):
        assert block.split(f"    {BEGIN}\n    {END}\n").found is False

    def test_a_quoted_marker_survives_an_update(self, block):
        """The mention must still be there afterwards, unharmed."""
        original = f'echo "{BEGIN}"\n'
        updated = block.apply(original, "mine\n")
        assert f'echo "{BEGIN}"' in updated
        assert block.body(updated) == "mine\n"


# -- line endings and whitespace -------------------------------------------


class TestLineEndings:
    def test_crlf_outside_the_block_is_preserved(self, block):
        original = "alpha\r\nbeta\r\n"
        updated = block.apply(original, "mine\n")
        assert updated.startswith("alpha\r\nbeta\r\n")

    def test_crlf_markers_are_recognised(self, block):
        text = f"{BEGIN}\r\nx\r\n{END}\r\n"
        assert block.split(text).found is True

    def test_crlf_content_around_a_crlf_block(self, block):
        text = f"head\r\n{BEGIN}\r\nold\r\n{END}\r\ntail\r\n"
        updated = block.apply(text, "new\n")
        assert updated.startswith("head\r\n")
        assert updated.endswith("tail\r\n")
        assert block.body(updated) == "new\n"

    def test_a_file_without_a_trailing_newline_gains_one(self, block):
        updated = block.apply("no newline", "mine\n")
        assert updated == f"no newline\n\n{BEGIN}\nmine\n{END}\n"

    def test_a_block_body_without_a_trailing_newline_gets_one(self, block):
        assert block.render("x") == f"{BEGIN}\nx\n{END}\n"

    def test_an_empty_body_renders_an_empty_block(self, block):
        assert block.render("") == f"{BEGIN}\n{END}\n"

    def test_a_multiline_body_is_kept_verbatim(self, block):
        assert block.body(block.render("a\n\nb\n")) == "a\n\nb\n"

    def test_trailing_content_after_the_end_marker_survives(self, block):
        text = f"{BEGIN}\nx\n{END}\nafter"
        assert block.split(text).after == "after"


# -- applying --------------------------------------------------------------


class TestApplying:
    def test_appends_to_an_empty_file(self, block):
        assert block.apply("", "x\n") == f"{BEGIN}\nx\n{END}\n"

    def test_exactly_one_blank_line_separates_us_from_their_content(self, block):
        """Flush against a hand-written config, our block reads as theirs."""
        assert block.apply("theirs\n", "x\n") == f"theirs\n\n{BEGIN}\nx\n{END}\n"

    def test_an_existing_blank_line_is_not_doubled(self, block):
        assert block.apply("theirs\n\n", "x\n") == f"theirs\n\n{BEGIN}\nx\n{END}\n"

    def test_several_trailing_blank_lines_are_left_alone(self, block):
        out = block.apply("theirs\n\n\n", "x\n")
        assert out.startswith("theirs\n\n\n") and BEGIN in out

    def test_replaces_in_place(self, block):
        original = f"head\n{BEGIN}\nold\n{END}\ntail\n"
        updated = block.apply(original, "new\n")
        assert updated == f"head\n{BEGIN}\nnew\n{END}\ntail\n"

    def test_is_idempotent(self, block):
        once = block.apply("head\n", "x\n")
        assert block.apply(once, "x\n") == once

    def test_repeated_applies_never_add_a_second_block(self, block):
        text = ""
        for i in range(5):
            text = block.apply(text, f"body {i}\n")
        assert text.count(BEGIN) == 1 and text.count(END) == 1
        assert block.body(text) == "body 4\n"

    def test_surrounding_content_is_untouched(self, block):
        original = "a\nb\n" + f"{BEGIN}\nold\n{END}\n" + "c\nd\n"
        updated = block.apply(original, "new\n")
        split = block.split(updated)
        assert split.before == "a\nb\n" and split.after == "c\nd\n"

    def test_applying_to_malformed_content_raises_and_changes_nothing(self, block):
        original = f"{BEGIN}\nunterminated\n"
        with pytest.raises(BlockError):
            block.apply(original, "x\n")


class TestRemoving:
    def test_removes_the_block(self, block):
        text = f"head\n{BEGIN}\nx\n{END}\ntail\n"
        out, existed = block.remove(text)
        assert existed and out == "head\ntail\n"

    def test_reports_nothing_to_remove(self, block):
        out, existed = block.remove("head\n")
        assert not existed and out == "head\n"

    def test_removing_the_only_content_empties_the_file(self, block):
        out, existed = block.remove(f"{BEGIN}\nx\n{END}\n")
        assert existed and out == ""

    def test_does_not_leave_a_double_blank_line(self, block):
        text = f"head\n\n{BEGIN}\nx\n{END}\n\ntail\n"
        out, _ = block.remove(text)
        assert "\n\n\n" not in out

    def test_apply_then_remove_round_trips(self, block):
        original = "keep\n"
        assert block.remove(block.apply(original, "x\n"))[0] == original

    def test_removing_from_malformed_content_raises(self, block):
        with pytest.raises(BlockError):
            block.remove(f"{END}\n")


# -- reading files ---------------------------------------------------------


class TestReading:
    def test_a_missing_file_reads_as_empty(self, block, cfg):
        assert block.read(cfg) == ""

    def test_reads_utf8(self, block, cfg):
        write(cfg, "héllo ✓\n")
        assert block.read(cfg) == "héllo ✓\n"

    def test_non_utf8_is_a_clear_error(self, block, cfg):
        cfg.write_bytes(b"\xff\xfe not utf-8\n")
        with pytest.raises(BlockError, match="not valid UTF-8"):
            block.read(cfg)

    def test_an_unreadable_file_is_a_clear_error(self, block, cfg):
        write(cfg, "x\n", mode=0o000)
        try:
            if os.access(cfg, os.R_OK):  # pragma: no cover - running as root
                pytest.skip("cannot make a file unreadable as this user")
            with pytest.raises(BlockError, match="cannot read"):
                block.read(cfg)
        finally:
            os.chmod(cfg, 0o644)

    def test_a_directory_in_place_of_the_file(self, block, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        with pytest.raises(BlockError):
            block.read(target)


# -- writing files ---------------------------------------------------------


class TestWriting:
    def test_creates_the_file_and_parents(self, block, tmp_path):
        target = tmp_path / "deep" / "nested" / "config"
        block.update(target, "x\n")
        assert block.body(target.read_text()) == "x\n"

    def test_reports_whether_anything_changed(self, block, cfg):
        assert block.update(cfg, "x\n") is True
        assert block.update(cfg, "x\n") is False
        assert block.update(cfg, "y\n") is True

    def test_preserves_surrounding_bytes_exactly(self, block, cfg):
        original = "# mine\nexport A=1\n"
        write(cfg, original)
        block.update(cfg, "x\n")
        assert cfg.read_text().startswith(original)

    def test_an_existing_mode_is_preserved(self, block, cfg):
        write(cfg, "x\n", mode=0o600)
        block.update(cfg, "y\n")
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_a_mode_cap_does_not_widen_an_existing_file(self, block, cfg):
        write(cfg, "x\n", mode=0o600)
        block.update(cfg, "y\n", mode=0o644)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_a_mode_cap_tightens_a_too_open_file(self, block, cfg):
        write(cfg, "x\n", mode=0o666)
        block.update(cfg, "y\n", mode=0o600)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_a_new_file_gets_the_requested_mode(self, block, cfg):
        block.update(cfg, "x\n", mode=0o600)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_the_directory_mode_can_be_pinned(self, block, tmp_path):
        target = tmp_path / "ssh" / "config"
        block.update(target, "x\n", mode=0o600, dir_mode=0o700)
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700

    def test_no_temp_files_are_left_behind(self, block, cfg):
        block.update(cfg, "x\n")
        assert [p.name for p in cfg.parent.iterdir() if ".tmp" in p.name] == []

    def test_a_stale_temp_file_is_cleared(self, block, cfg):
        """A crashed run must not leave something that outlives it."""
        write(cfg, "x\n")
        stale = cfg.parent / f".{cfg.name}.usm.9999.tmp"
        stale.write_text("garbage")
        block.update(cfg, "y\n")
        assert not stale.exists()

    def test_a_backup_is_written_when_asked(self, block, cfg):
        write(cfg, "original\n")
        block.update(cfg, "x\n", backup=True)
        assert (cfg.parent / f"{cfg.name}.usm.bak").read_text() == "original\n"

    def test_no_backup_by_default(self, block, cfg):
        write(cfg, "original\n")
        block.update(cfg, "x\n")
        assert not (cfg.parent / f"{cfg.name}.usm.bak").exists()

    def test_backup_is_skipped_for_a_new_file(self, block, cfg):
        block.update(cfg, "x\n", backup=True)
        assert not (cfg.parent / f"{cfg.name}.usm.bak").exists()

    def test_a_read_only_file_is_refused(self, block, cfg):
        write(cfg, "x\n", mode=0o400)
        try:
            if os.access(cfg, os.W_OK):  # pragma: no cover - running as root
                pytest.skip("cannot make a file read-only as this user")
            with pytest.raises(BlockError, match="read-only"):
                block.update(cfg, "y\n")
            assert cfg.read_text() == "x\n"
        finally:
            os.chmod(cfg, 0o644)

    def test_a_non_regular_file_is_refused(self, block, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        with pytest.raises(BlockError):
            block.update(target, "x\n")

    def test_malformed_content_leaves_the_file_untouched(self, block, cfg):
        original = f"head\n{BEGIN}\nunterminated\n"
        write(cfg, original)
        with pytest.raises(BlockError):
            block.update(cfg, "x\n")
        assert cfg.read_text() == original

    def test_non_utf8_leaves_the_file_untouched(self, block, cfg):
        original = b"\xff\xfe\n"
        cfg.write_bytes(original)
        with pytest.raises(BlockError):
            block.update(cfg, "x\n")
        assert cfg.read_bytes() == original


class TestSymlinkPolicy:
    def test_a_symlink_is_refused_by_default(self, block, tmp_path):
        real = write(tmp_path / "real", "x\n")
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(BlockError, match="symlink"):
            block.update(link, "y\n")
        assert real.read_text() == "x\n"

    def test_following_a_symlink_edits_the_target(self, block, tmp_path):
        real = write(tmp_path / "real", "x\n")
        link = tmp_path / "link"
        link.symlink_to(real)
        block.update(link, "y\n", symlinks="follow")
        assert block.body(real.read_text()) == "y\n"
        assert link.is_symlink(), "the link itself must survive"

    def test_a_broken_symlink_when_following(self, block, tmp_path):
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "missing")
        block.update(link, "y\n", symlinks="follow")
        assert (tmp_path / "missing").exists()

    def test_a_symlink_loop_is_reported(self, block, tmp_path):
        """3.12 raises here, 3.13 silently says the path does not exist.

        Left to the standard library, 3.13 would have us replace the link
        with a regular file; the loop has to be detected explicitly.
        """
        a, b = tmp_path / "a", tmp_path / "b"
        a.symlink_to(b)
        b.symlink_to(a)
        with pytest.raises(BlockError, match="loop"):
            block.update(a, "y\n", symlinks="follow")
        assert a.is_symlink(), "the link must survive being refused"

    def test_an_unknown_policy_is_a_programming_error(self, block, tmp_path):
        link = tmp_path / "link"
        link.symlink_to(write(tmp_path / "real", "x\n"))
        with pytest.raises(ValueError, match="unknown symlink policy"):
            block.update(link, "y\n", symlinks="nonsense")

    def test_a_plain_file_ignores_the_policy(self, block, cfg):
        block.update(cfg, "x\n", symlinks="follow")
        assert cfg.exists() and not cfg.is_symlink()


class TestRemovingFromFiles:
    def test_removes_and_reports(self, block, cfg):
        block.update(cfg, "x\n")
        assert block.remove_from(cfg) is True
        assert BEGIN not in cfg.read_text()

    def test_a_missing_file_is_not_an_error(self, block, cfg):
        assert block.remove_from(cfg) is False

    def test_a_file_without_a_block_is_untouched(self, block, cfg):
        write(cfg, "mine\n")
        assert block.remove_from(cfg) is False
        assert cfg.read_text() == "mine\n"

    def test_surrounding_content_survives(self, block, cfg):
        write(cfg, "a\n")
        block.update(cfg, "x\n")
        block.remove_from(cfg)
        assert cfg.read_text() == "a\n"

    def test_a_symlink_is_refused(self, block, tmp_path):
        real = tmp_path / "real"
        ManagedBlock(BEGIN, END).update(real, "x\n")
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(BlockError, match="symlink"):
            block.remove_from(link)

    def test_mode_is_preserved_on_removal(self, block, cfg):
        block.update(cfg, "x\n", mode=0o600)
        block.remove_from(cfg, mode=0o600)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


# -- independence ----------------------------------------------------------


class TestBlocksDoNotInterfere:
    """Several commands write into the same rc file."""

    def test_two_blocks_coexist(self, cfg):
        one = ManagedBlock("# A>", "# <A")
        two = ManagedBlock("# B>", "# <B")
        one.update(cfg, "alpha\n")
        two.update(cfg, "beta\n")
        assert one.body(cfg.read_text()) == "alpha\n"
        assert two.body(cfg.read_text()) == "beta\n"

    def test_updating_one_leaves_the_other(self, cfg):
        one = ManagedBlock("# A>", "# <A")
        two = ManagedBlock("# B>", "# <B")
        one.update(cfg, "alpha\n")
        two.update(cfg, "beta\n")
        one.update(cfg, "alpha2\n")
        assert two.body(cfg.read_text()) == "beta\n"

    def test_removing_one_leaves_the_other(self, cfg):
        one = ManagedBlock("# A>", "# <A")
        two = ManagedBlock("# B>", "# <B")
        one.update(cfg, "alpha\n")
        two.update(cfg, "beta\n")
        one.remove_from(cfg)
        assert two.body(cfg.read_text()) == "beta\n"
        assert "# A>" not in cfg.read_text()

    def test_a_malformed_other_block_does_not_block_ours(self, cfg):
        """Our own edits must not depend on someone else's mess."""
        write(cfg, "# B>\nunterminated\n")
        one = ManagedBlock("# A>", "# <A")
        one.update(cfg, "alpha\n")
        assert one.body(cfg.read_text()) == "alpha\n"
        assert "# B>\nunterminated\n" in cfg.read_text()


class TestRealisticFiles:
    """Shapes taken from the files the callers actually edit."""

    def test_a_bashrc(self, block, cfg):
        original = (
            "# ~/.bashrc\n"
            "case $- in\n"
            "    *i*) ;;\n"
            "      *) return;;\n"
            "esac\n"
            "\n"
            "export PATH=$HOME/.local/bin:$PATH\n"
        )
        write(cfg, original)
        block.update(cfg, 'alias usm="usm"\n')
        assert cfg.read_text().startswith(original)
        block.remove_from(cfg)
        assert cfg.read_text() == original

    def test_an_ssh_config(self, block, cfg):
        original = (
            "Host bastion\n"
            "    HostName bastion.example.com\n"
            "    User me\n"
            "\n"
            "Host *\n"
            "    ServerAliveInterval 60\n"
        )
        write(cfg, original, mode=0o600)
        block.update(cfg, "Host box\n    HostName 10.0.0.1\n", mode=0o600)
        assert cfg.read_text().startswith(original)
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_a_tmux_conf_with_no_trailing_newline(self, block, cfg):
        write(cfg, "set -g mouse on")
        block.update(cfg, "set -g history-limit 10000\n")
        text = cfg.read_text()
        assert text.startswith("set -g mouse on\n\n")
        assert block.body(text) == "set -g history-limit 10000\n"

    def test_a_file_that_is_only_our_block(self, block, cfg):
        block.update(cfg, "x\n")
        block.update(cfg, "y\n")
        assert cfg.read_text() == f"{BEGIN}\ny\n{END}\n"
        block.remove_from(cfg)
        assert cfg.read_text() == ""


class TestFilesystemFailures:
    """The OS says no partway through; say so, don't half-write."""

    def test_an_unwritable_parent_directory(self, block, tmp_path):
        parent = tmp_path / "locked"
        parent.mkdir()
        os.chmod(parent, 0o500)
        try:
            if os.access(parent, os.W_OK):  # pragma: no cover - running as root
                pytest.skip("cannot make a directory unwritable as this user")
            with pytest.raises(BlockError, match="cannot write"):
                block.update(parent / "config", "x\n")
        finally:
            os.chmod(parent, 0o700)

    def test_writing_over_a_directory_is_refused(self, block, tmp_path):
        """update() is guarded by read(); write() must guard itself too."""
        target = tmp_path / "adir"
        target.mkdir()
        with pytest.raises(BlockError, match="not a regular file"):
            block.write(target, "x\n")

    def test_writing_over_a_fifo_is_refused(self, block, tmp_path):
        fifo = tmp_path / "afifo"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError):  # pragma: no cover - no fifo support
            pytest.skip("cannot create a fifo here")
        with pytest.raises(BlockError, match="not a regular file"):
            block.write(fifo, "x\n")

    def test_a_parent_that_is_a_file(self, block, tmp_path):
        notadir = write(tmp_path / "notadir", "x\n")
        with pytest.raises(BlockError, match="cannot prepare"):
            block.update(notadir / "config", "y\n")

    def test_a_failing_backup_is_reported(self, block, cfg, monkeypatch):
        write(cfg, "original\n")

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("usm_blocks.shutil.copy2", boom)
        with pytest.raises(BlockError, match="cannot back up"):
            block.update(cfg, "x\n", backup=True)
        assert cfg.read_text() == "original\n", "the original must survive"

    def test_a_failing_write_leaves_the_original(self, block, cfg, monkeypatch):
        write(cfg, "original\n")
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr("usm_blocks.os.replace", boom)
        with pytest.raises(BlockError, match="cannot write"):
            block.update(cfg, "x\n")
        assert cfg.read_text() == "original\n"
        assert [p.name for p in cfg.parent.iterdir() if ".tmp" in p.name] == []
        monkeypatch.setattr("usm_blocks.os.replace", real_replace)

    def test_a_dir_mode_that_cannot_be_set(self, block, tmp_path, monkeypatch):
        def boom(path, mode):
            raise OSError("operation not permitted")

        monkeypatch.setattr("usm_blocks.os.chmod", boom)
        with pytest.raises(BlockError, match="cannot prepare"):
            block.update(tmp_path / "sub" / "config", "x\n", dir_mode=0o700)
