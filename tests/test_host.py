"""Tests for scripts/host.py.

The host tool edits ``~/.ssh/config``, so these tests bias hard toward file
safety: every write is redirected to a per-test HOME, network and ssh calls are
stubbed, and corrupt or hostile config content is asserted to fail without
changing the original bytes. The test names and docstrings describe the user
risk each case protects against.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from usmo import ui

_HOST_PATH = Path(__file__).resolve().parent.parent / "scripts" / "host.py"
_HOST_SPEC = importlib.util.spec_from_file_location("scripts/host.py", _HOST_PATH)
assert _HOST_SPEC is not None and _HOST_SPEC.loader is not None
host = importlib.util.module_from_spec(_HOST_SPEC)
sys.modules[_HOST_SPEC.name] = host
_HOST_SPEC.loader.exec_module(host)


# --- Fixtures and helpers --------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME so no test can read or write the real SSH config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ui, "_console", None)
    monkeypatch.setattr(ui, "_err_console", None)
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


def invoke(runner, *args):
    return runner.invoke(host.cli, list(args), catch_exceptions=False)


def ssh_dir(home: Path) -> Path:
    return home / ".ssh"


def config(home: Path) -> Path:
    return ssh_dir(home) / "config"


def write_config(home: Path, data: bytes | str, mode: int = 0o600) -> Path:
    ssh_dir(home).mkdir(mode=0o700, exist_ok=True)
    cfg = config(home)
    if isinstance(data, bytes):
        cfg.write_bytes(data)
    else:
        cfg.write_text(data, encoding="utf-8")
    os.chmod(cfg, mode)
    return cfg


def add_box(runner, target: str = "user@example.com:2222", *extra: str):
    return invoke(runner, "add", "box", target, *extra)


def assert_no_temp_files(home: Path) -> None:
    if ssh_dir(home).exists():
        assert not list(ssh_dir(home).glob("*.tmp"))
        assert not list(ssh_dir(home).glob(".*.tmp"))
        assert not list(ssh_dir(home).glob(".config.usm.*.tmp"))


def managed_block_for(alias: str = "box") -> str:
    return (
        f"{host.BEGIN_MARKER}\n"
        f"Host {alias}\n"
        "    HostName example.com\n"
        "    User user\n"
        "    Port 2222\n"
        f"{host.END_MARKER}\n"
    )


def parse_json_result(result):
    assert result.output
    return json.loads(result.output)


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# --- Config file safety ----------------------------------------------------


class TestConfigFileSafety:
    def test_missing_ssh_directory_is_created_with_private_modes(
        self, runner, isolated_home
    ):
        """A first-time user should get safe 0700/0600 SSH paths automatically."""
        result = add_box(runner)

        assert result.exit_code == 0
        assert (ssh_dir(isolated_home).stat().st_mode & 0o777) == 0o700
        assert (config(isolated_home).stat().st_mode & 0o777) == 0o600
        assert_no_temp_files(isolated_home)

    def test_empty_config_gets_only_the_managed_block(self, runner, isolated_home):
        """An empty existing file should not grow leading junk or duplicate markers."""
        write_config(isolated_home, "")

        assert add_box(runner).exit_code == 0

        assert config(isolated_home).read_text() == managed_block_for()
        assert_no_temp_files(isolated_home)

    def test_comments_only_config_is_preserved_before_block(
        self, runner, isolated_home
    ):
        """Comments users keep for context must survive byte-for-byte."""
        before = b"# personal ssh notes\n# another comment\n"
        write_config(isolated_home, before)

        assert add_box(runner).exit_code == 0

        assert config(isolated_home).read_bytes().startswith(before + b"\n")
        assert_no_temp_files(isolated_home)

    def test_config_without_trailing_newline_preserves_original_bytes(
        self, runner, isolated_home
    ):
        """Files without final newlines should not have existing bytes rewritten."""
        before = b"Host hand\n    HostName hand.example"
        write_config(isolated_home, before)

        assert add_box(runner).exit_code == 0

        assert config(isolated_home).read_bytes().startswith(before + b"\n\n")

    def test_crlf_config_keeps_crlf_handwritten_region(self, runner, isolated_home):
        """Windows-style line endings outside the block must not be normalized."""
        before = b"Host hand\r\n    HostName hand.example\r\n"
        write_config(isolated_home, before)

        assert add_box(runner).exit_code == 0

        assert config(isolated_home).read_bytes().startswith(before + b"\n")

    @pytest.mark.parametrize(
        ("content", "prefix", "suffix"),
        [
            (
                managed_block_for() + "Host tail\n    HostName tail\n",
                b"",
                b"Host tail\n    HostName tail\n",
            ),
            (
                b"Host top\n    HostName top\n\n"
                + managed_block_for().encode()
                + b"Host tail\n    HostName tail\n",
                b"Host top\n    HostName top\n\n",
                b"Host tail\n    HostName tail\n",
            ),
            (
                "Host top\n    HostName top\n\n" + managed_block_for(),
                b"Host top\n    HostName top\n\n",
                b"",
            ),
        ],
        ids=["start", "middle", "end"],
    )
    def test_block_position_does_not_change_surrounding_bytes(
        self, runner, isolated_home, content, prefix, suffix
    ):
        """The managed block may be anywhere; only that fenced region is replaceable."""
        data = content if isinstance(content, bytes) else content.encode()
        write_config(isolated_home, data)

        assert invoke(runner, "add", "box", "new@example.net:2200").exit_code == 0

        updated = config(isolated_home).read_bytes()
        assert updated.startswith(prefix)
        assert updated.endswith(suffix)
        assert updated.count(host.BEGIN_MARKER.encode()) == 1

    @pytest.mark.parametrize("operation", ["add", "update", "rm"])
    def test_handwritten_above_and_below_survive_operations(
        self, runner, isolated_home, operation
    ):
        """Add, update, and remove must be safe for real mixed config files."""
        above = b"Host above\n    HostName above.example\n\n"
        below = b"Host below\n    HostName below.example\n"
        write_config(isolated_home, above + managed_block_for().encode() + below)

        if operation == "add":
            result = invoke(runner, "add", "other", "other.example")
        elif operation == "update":
            result = invoke(runner, "add", "box", "new@example.org")
        else:
            result = invoke(runner, "rm", "box")

        assert result.exit_code == 0
        assert config(isolated_home).read_bytes().startswith(above)
        assert config(isolated_home).read_bytes().endswith(below)
        assert_no_temp_files(isolated_home)

    @pytest.mark.parametrize(
        "bad_content, message",
        [
            (b"# >>> usm host >>>\nHost box\n", "incomplete"),
            (b"Host box\n# <<< usm host <<<\n", "incomplete"),
            (
                b"# >>> usm host >>>\nHost a\n# >>> usm host >>>\n# <<< usm host <<<\n",
                "duplicate",
            ),
        ],
        ids=["missing-end", "missing-begin", "duplicate-begin"],
    )
    def test_corrupt_markers_fail_cleanly_and_leave_file_untouched(
        self, runner, isolated_home, bad_content, message
    ):
        """A half-written or nested block should never be guessed at or rewritten."""
        cfg = write_config(isolated_home, bad_content)

        result = runner.invoke(host.cli, ["add", "new", "new.example"])

        assert result.exit_code != 0
        assert message in result.output
        assert "Traceback" not in result.output
        assert cfg.read_bytes() == bad_content
        assert_no_temp_files(isolated_home)

    def test_symlink_config_is_refused_without_touching_target(
        self, runner, isolated_home
    ):
        """Replacing a symlink would surprise users by breaking their chosen target."""
        target = isolated_home / "real_config"
        target.write_text("Host real\n    HostName real.example\n")
        ssh_dir(isolated_home).mkdir(mode=0o700)
        config(isolated_home).symlink_to(target)
        original = target.read_bytes()

        result = runner.invoke(host.cli, ["add", "box", "box.example"])

        assert result.exit_code != 0
        assert "symlink" in result.output
        assert config(isolated_home).is_symlink()
        assert target.read_bytes() == original
        assert_no_temp_files(isolated_home)

    def test_read_only_config_is_refused_without_traceback(self, runner, isolated_home):
        """A read-only config should produce a clear error instead of a partial write."""
        original = b"Host readonly\n    HostName readonly.example\n"
        cfg = write_config(isolated_home, original, mode=0o400)

        result = runner.invoke(host.cli, ["add", "box", "box.example"])

        assert result.exit_code != 0
        assert "read-only" in result.output
        assert "Traceback" not in result.output
        assert cfg.read_bytes() == original
        assert_no_temp_files(isolated_home)

    def test_existing_world_readable_config_is_tightened_to_0600(
        self, runner, isolated_home
    ):
        """Rewrites should not preserve unsafe group/other-readable config bits."""
        cfg = write_config(isolated_home, "Host hand\n    HostName hand\n", mode=0o644)

        assert add_box(runner).exit_code == 0

        assert (cfg.stat().st_mode & 0o777) == 0o600

    def test_existing_0600_mode_is_preserved(self, runner, isolated_home):
        """Already-private configs should remain private after every rewrite."""
        cfg = write_config(isolated_home, "Host hand\n    HostName hand\n", mode=0o600)

        assert add_box(runner).exit_code == 0

        assert (cfg.stat().st_mode & 0o777) == 0o600

    def test_stale_temp_file_from_crash_is_removed_on_next_write(
        self, runner, isolated_home
    ):
        """A previous interrupted run should not poison future host edits."""
        write_config(isolated_home, "Host hand\n    HostName hand\n")
        stale = ssh_dir(isolated_home) / ".config.usm.123.tmp"
        stale.write_text("partial")

        assert add_box(runner).exit_code == 0

        assert not stale.exists()
        assert_no_temp_files(isolated_home)

    def test_backup_is_created_from_previous_config(self, runner, isolated_home):
        """Users need one-step recovery if a managed rewrite is not what they expected."""
        original = "Host hand\n    HostName hand.example\n"
        write_config(isolated_home, original)

        assert add_box(runner).exit_code == 0

        assert (ssh_dir(isolated_home) / "config.usm.bak").read_text() == original

    def test_invalid_utf8_config_fails_without_traceback(self, runner, isolated_home):
        """A non-UTF-8 SSH config should not crash Click or be overwritten."""
        bad = b"Host bad\n  HostName \xff\n"
        cfg = write_config(isolated_home, bad)

        result = runner.invoke(host.cli, ["ls"])

        assert result.exit_code != 0
        assert "UTF-8" in result.output
        assert "Traceback" not in result.output
        assert cfg.read_bytes() == bad


# --- Alias validation and injection ---------------------------------------


class TestValidationAndInjection:
    @pytest.mark.parametrize(
        "alias",
        [
            "bad\nHost x",
            "bad\rHost x",
            "has space",
            "has\ttab",
            "hash#tag",
            'quote"me',
            "'quote'",
            "-option",
            "",
            "x" * 256,
            "snowman☃",
        ],
    )
    def test_bad_aliases_are_rejected_before_writing(
        self, runner, isolated_home, alias
    ):
        """Host aliases are config syntax, so suspicious names must not reach disk."""
        args = (
            ["add", "--", alias, "example.com"]
            if alias.startswith("-")
            else ["add", alias, "example.com"]
        )
        result = runner.invoke(host.cli, args)

        assert result.exit_code != 0
        assert "Invalid alias" in result.output
        assert not config(isolated_home).exists()
        assert_no_temp_files(isolated_home)

    @pytest.mark.parametrize(
        "alias", ["box", "box-1", "box_1", "box.example", "box:edge"]
    )
    def test_safe_alias_characters_are_accepted(self, runner, alias):
        """Common SSH alias styles should remain usable."""
        result = invoke(runner, "add", alias, "example.com")

        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "args",
        [
            ["add", "box", "example.com\nHost hacked"],
            ["add", "box", "example.com", "--identity", "~/.ssh/id\nHost hacked"],
            ["add", "box", "example.com", "--jump", "jump\nHost hacked"],
            ["add", "box", "example.com", "--option", "User yes\nHost hacked"],
            ["add", "box", "example.com", "--tag", "gpu\nHost hacked"],
        ],
        ids=["target", "identity", "jump", "option", "tag"],
    )
    def test_newline_in_values_cannot_inject_stanzas(self, runner, isolated_home, args):
        """Every user-controlled config value must be one physical line."""
        result = runner.invoke(host.cli, args)

        assert result.exit_code != 0
        assert "Invalid" in result.output
        assert not config(isolated_home).exists()

    @pytest.mark.parametrize("target", ["example.com:abc", ":22"])
    def test_bad_targets_have_clear_errors(self, runner, target):
        """Malformed targets should fail before any config rewrite."""
        result = runner.invoke(host.cli, ["add", "box", target])

        assert result.exit_code != 0
        assert "Invalid target" in result.output

    def test_option_must_have_key_and_value(self, runner):
        """A bare ssh option key would render ambiguous config."""
        result = runner.invoke(
            host.cli, ["add", "box", "example.com", "--option", "Compression"]
        )

        assert result.exit_code != 0
        assert "Key Value" in result.output

    def test_tag_cannot_contain_commas_or_spaces(self, runner):
        """Tags are comma-rendered metadata, so separators are reserved."""
        result = runner.invoke(
            host.cli, ["add", "box", "example.com", "--tag", "gpu,prod"]
        )

        assert result.exit_code != 0
        assert "Invalid tag" in result.output


# --- Parsing unmanaged config ---------------------------------------------


class TestUnmanagedParsing:
    def test_all_reads_varied_spacing_casing_and_tabs(self, runner, isolated_home):
        """Real ssh_config files mix casing and whitespace; --all should tolerate it."""
        write_config(
            isolated_home,
            "Host\toutside\n\thostname\toutside.example\n\tUSER\tbob\n\tPort\t2200\n",
        )

        result = invoke(runner, "ls", "--all", "--json")
        data = parse_json_result(result)

        assert data == [
            {
                "alias": "outside",
                "identity": None,
                "managed": False,
                "port": "2200",
                "reachable": None,
                "tags": [],
                "target": "bob@outside.example:2200",
            }
        ]

    def test_multiple_aliases_on_host_line_share_stanza_values(
        self, runner, isolated_home
    ):
        """OpenSSH allows multiple aliases on one Host line; inventory should list them."""
        write_config(
            isolated_home, "Host a b c\n  HostName shared.example\n  User me\n"
        )

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert [row["alias"] for row in data] == ["a", "b", "c"]
        assert {row["target"] for row in data} == {"me@shared.example"}

    def test_wildcard_hosts_are_ignored(self, runner, isolated_home):
        """Wildcard stanzas are policy, not concrete inventory entries."""
        write_config(
            isolated_home,
            "Host *\n  User everyone\nHost real\n  HostName real.example\n",
        )

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert [row["alias"] for row in data] == ["real"]

    def test_match_block_does_not_modify_previous_host(self, runner, isolated_home):
        """Match blocks have different semantics and should not leak into Host parsing."""
        write_config(
            isolated_home,
            "Host real\n  HostName real.example\nMatch user bob\n  HostName wrong.example\n",
        )

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert data[0]["target"] == "real.example"

    def test_include_directive_is_ignored(self, runner, isolated_home):
        """The simple parser intentionally does not chase Include directives."""
        write_config(
            isolated_home, "Include other.conf\nHost real\n  HostName real.example\n"
        )

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert [row["alias"] for row in data] == ["real"]

    def test_comments_mid_stanza_are_ignored(self, runner, isolated_home):
        """Comments should not break collection of later fields in the same stanza."""
        write_config(
            isolated_home,
            "Host real\n  # a note\n  HostName real.example\n  IdentityFile ~/.ssh/id\n",
        )

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert data[0]["identity"] == "~/.ssh/id"

    def test_stanza_without_hostname_uses_alias_as_target(self, runner, isolated_home):
        """A Host alias without HostName is still a useful inventory item."""
        write_config(isolated_home, "Host direct\n  User me\n")

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert data[0]["target"] == "me@"

    def test_managed_entries_override_unmanaged_aliases(self, runner, isolated_home):
        """The block is the source of truth for aliases managed by usm."""
        write_config(isolated_home, "Host box\n  HostName old.example\n")
        assert invoke(runner, "add", "box", "new.example").exit_code == 0

        data = parse_json_result(invoke(runner, "ls", "--all", "--json"))

        assert len(data) == 1
        assert data[0]["target"] == "new.example"
        assert data[0]["managed"] is True


# --- exec ------------------------------------------------------------------


class TestExecCommand:
    def add_hosts(self, runner, *aliases: str, tag: str | None = None):
        for alias in aliases:
            args = ["add", alias, f"{alias}.example"]
            if tag:
                args += ["--tag", tag]
            assert invoke(runner, *args).exit_code == 0

    def test_all_hosts_succeed_with_zero_exit(self, runner, monkeypatch):
        """Successful fan-out should produce JSON rows and a zero process status."""
        self.add_hosts(runner, "a", "b", tag="fleet")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"ok", b""),
        )

        result = runner.invoke(
            host.cli,
            ["exec", "--tag", "fleet", "--json", "--", "uptime"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert [row["ok"] for row in parse_json_result(result)] == [True, True]

    def test_some_hosts_fail_sets_nonzero_exit(self, runner, monkeypatch):
        """Any per-host failure should make the aggregate command fail."""
        self.add_hosts(runner, "a", "b", tag="fleet")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 2 if argv[-2] == "b" else 0, b"", b"bad"
            ),
        )

        result = runner.invoke(
            host.cli,
            ["exec", "--tag", "fleet", "--json", "--", "cmd"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert {row["exit_code"] for row in parse_json_result(result)} == {0, 2}

    def test_all_hosts_fail_sets_nonzero_exit(self, runner, monkeypatch):
        """A total outage should be reflected in every row and the process exit."""
        self.add_hosts(runner, "a", "b")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 9, b"", b"no"),
        )

        result = runner.invoke(
            host.cli, ["exec", "a", "b", "--json", "--", "cmd"], catch_exceptions=False
        )

        assert result.exit_code == 1
        assert all(not row["ok"] for row in parse_json_result(result))

    def test_empty_tag_selection_is_an_error(self, runner):
        """A typo in --tag should not silently succeed after running on nobody."""
        self.add_hosts(runner, "a")

        result = runner.invoke(
            host.cli, ["exec", "--tag", "missing", "--json", "--", "cmd"]
        )

        assert result.exit_code != 0
        assert "No hosts selected" in result.output

    def test_timeout_is_reported_as_exit_124(self, runner, monkeypatch):
        """SSH subprocess timeouts should be summarized like command failures."""
        self.add_hosts(runner, "a")

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1, output=b"partial", stderr=b"late")

        monkeypatch.setattr(host.subprocess, "run", fake_run)

        result = runner.invoke(
            host.cli, ["exec", "a", "--json", "--", "cmd"], catch_exceptions=False
        )
        row = parse_json_result(result)[0]

        assert result.exit_code == 1
        assert row["exit_code"] == 124
        assert "command timed out" in row["output"]

    def test_fail_fast_with_parallel_one_does_not_run_later_hosts(
        self, runner, monkeypatch
    ):
        """Fail-fast should stop scheduling queued hosts when serial execution is requested."""
        self.add_hosts(runner, "a", "b", "c")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv[-2])
            return subprocess.CompletedProcess(argv, 1, b"", b"fail")

        monkeypatch.setattr(host.subprocess, "run", fake_run)

        result = runner.invoke(
            host.cli,
            ["exec", "--all", "--parallel", "1", "--fail-fast", "--json", "--", "cmd"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert calls == ["a"]
        assert [row["alias"] for row in parse_json_result(result)] == ["a"]

    @pytest.mark.parametrize("parallel", [1, 8])
    def test_parallel_values_keep_results_correct(self, runner, monkeypatch, parallel):
        """Changing concurrency must not change which hosts are reported."""
        self.add_hosts(runner, "a", "b", "c", tag="fleet")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, argv[-2].encode(), b""
            ),
        )

        result = runner.invoke(
            host.cli,
            [
                "exec",
                "--tag",
                "fleet",
                "--parallel",
                str(parallel),
                "--json",
                "--",
                "cmd",
            ],
            catch_exceptions=False,
        )

        assert [row["alias"] for row in parse_json_result(result)] == ["a", "b", "c"]

    def test_parallel_bound_is_respected(self, runner, monkeypatch):
        """The thread pool should not exceed the user-specified fan-out."""
        self.add_hosts(runner, "a", "b", "c", "d", tag="fleet")
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_run(argv, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(host.subprocess, "run", fake_run)

        result = runner.invoke(
            host.cli,
            ["exec", "--tag", "fleet", "--parallel", "2", "--json", "--", "cmd"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert max_active <= 2

    def test_result_order_is_deterministic_even_when_completion_order_differs(
        self, runner, monkeypatch
    ):
        """Stable output order makes scripts and humans able to diff results."""
        self.add_hosts(runner, "b", "a", "c", tag="fleet")

        def fake_run(argv, **kwargs):
            if argv[-2] == "a":
                time.sleep(0.02)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(host.subprocess, "run", fake_run)

        data = parse_json_result(
            runner.invoke(
                host.cli,
                ["exec", "--tag", "fleet", "--json", "--", "cmd"],
                catch_exceptions=False,
            )
        )

        assert [row["alias"] for row in data] == ["a", "b", "c"]

    def test_long_output_is_truncated_by_default_and_raw_keeps_full_text(
        self, runner, monkeypatch
    ):
        """Default output should protect terminals while --raw remains scriptable."""
        self.add_hosts(runner, "a")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, b"x" * 5000, b""
            ),
        )

        truncated = parse_json_result(
            runner.invoke(
                host.cli, ["exec", "a", "--json", "--", "cmd"], catch_exceptions=False
            )
        )[0]
        raw = parse_json_result(
            runner.invoke(
                host.cli,
                ["exec", "--raw", "--json", "a", "--", "cmd"],
                catch_exceptions=False,
            )
        )[0]

        assert "truncated" in truncated["output"]
        assert len(raw["output"]) == 5000
        assert "truncated" not in raw["output"]

    def test_command_with_no_output_has_empty_output_field(self, runner, monkeypatch):
        """Quiet remote commands should still produce a complete result row."""
        self.add_hosts(runner, "a")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
        )

        row = parse_json_result(
            runner.invoke(
                host.cli, ["exec", "a", "--json", "--", "true"], catch_exceptions=False
            )
        )[0]

        assert row["output"] == ""

    def test_non_utf8_output_is_replaced_not_crashing_json(self, runner, monkeypatch):
        """Remote programs can emit bytes that are not UTF-8; JSON must still render."""
        self.add_hosts(runner, "a")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"ok\xff", b""),
        )

        row = parse_json_result(
            runner.invoke(
                host.cli, ["exec", "a", "--json", "--", "cmd"], catch_exceptions=False
            )
        )[0]

        assert row["output"] == "ok�"

    def test_oserror_becomes_exit_127(self, runner, monkeypatch):
        """A missing ssh binary or spawn failure should be a per-host failure row."""
        self.add_hosts(runner, "a")
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: (_ for _ in ()).throw(OSError("no ssh")),
        )

        row = parse_json_result(
            runner.invoke(
                host.cli, ["exec", "a", "--json", "--", "cmd"], catch_exceptions=False
            )
        )[0]

        assert row["exit_code"] == 127
        assert "no ssh" in row["output"]


# --- check -----------------------------------------------------------------


class TestCheckCommand:
    def add_host(self, runner, alias="box"):
        assert invoke(runner, "add", alias, f"{alias}.example:22").exit_code == 0

    def test_reachable_host_reports_tcp_and_ssh_ok(self, runner, monkeypatch):
        """The happy path requires both the socket and BatchMode SSH probe."""
        self.add_host(runner)
        monkeypatch.setattr(
            host.socket, "create_connection", lambda *args, **kwargs: FakeConn()
        )
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
        )

        row = parse_json_result(invoke(runner, "check", "--json"))[0]

        assert row["ok"] is True
        assert row["tcp"] is True
        assert row["ssh"] is True

    @pytest.mark.parametrize(
        "exc, expected",
        [
            (ConnectionRefusedError("refused"), "refused"),
            (TimeoutError("timed out"), "timeout"),
            (OSError("Name or service not known"), "Name or service"),
        ],
        ids=["refused", "timeout", "dns"],
    )
    def test_tcp_failures_are_reported_without_ssh_probe(
        self, runner, monkeypatch, exc, expected
    ):
        """DNS, timeout, and refused sockets should not run ssh after TCP fails."""
        self.add_host(runner)
        monkeypatch.setattr(
            host.socket,
            "create_connection",
            lambda *args, **kwargs: (_ for _ in ()).throw(exc),
        )
        calls = []
        monkeypatch.setattr(
            host.subprocess, "run", lambda *args, **kwargs: calls.append(args)
        )

        row = parse_json_result(invoke(runner, "check", "--json"))[0]

        assert row["tcp"] is False
        assert expected in row["message"]
        assert calls == []

    def test_ssh_nonzero_is_reported_after_tcp_success(self, runner, monkeypatch):
        """A reachable port is not enough; BatchMode SSH must also succeed."""
        self.add_host(runner)
        monkeypatch.setattr(
            host.socket, "create_connection", lambda *args, **kwargs: FakeConn()
        )
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 255, b"", b"denied\nmore"
            ),
        )

        row = parse_json_result(invoke(runner, "check", "--json"))[0]

        assert row["tcp"] is True
        assert row["ssh"] is False
        assert row["message"] == "denied"

    def test_ssh_timeout_is_reported(self, runner, monkeypatch):
        """An SSH handshake hang should be distinct from a TCP failure."""
        self.add_host(runner)
        monkeypatch.setattr(
            host.socket, "create_connection", lambda *args, **kwargs: FakeConn()
        )
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(argv, 1)
            ),
        )

        row = parse_json_result(invoke(runner, "check", "--json"))[0]

        assert row["tcp"] is True
        assert row["message"] == "ssh timeout"


# --- Commands, JSON, and table output -------------------------------------


class TestCommandsJsonAndTables:
    def test_add_twice_updates_in_place_without_duplicate_host(
        self, runner, isolated_home
    ):
        """Re-adding an alias is the normal edit path and must be idempotent."""
        assert invoke(runner, "add", "box", "old.example").exit_code == 0
        assert invoke(runner, "add", "box", "new@example.org:2200").exit_code == 0

        text = config(isolated_home).read_text()
        assert text.count("Host box\n") == 1
        assert "HostName example.org" in text
        assert "User new" in text

    def test_rm_nonexistent_alias_errors(self, runner):
        """Removing a typo should not create or rewrite config."""
        result = runner.invoke(host.cli, ["rm", "missing"])

        assert result.exit_code != 0
        assert "Unknown managed host" in result.output

    def test_rm_refuses_unmanaged_alias(self, runner, isolated_home):
        """rm is scoped to the managed block and must not delete hand-written hosts."""
        write_config(isolated_home, "Host outside\n  HostName outside.example\n")

        result = runner.invoke(host.cli, ["rm", "outside"])

        assert result.exit_code != 0
        assert "defined outside" in result.output
        assert "Host outside" in config(isolated_home).read_text()

    def test_show_nonexistent_alias_errors(self, runner):
        """Detail views should fail clearly for typos."""
        result = runner.invoke(host.cli, ["show", "missing"])

        assert result.exit_code != 0
        assert "Unknown host alias" in result.output

    def test_show_renders_detail_for_managed_alias(self, runner):
        """show should expose the fields needed to audit an entry."""
        assert (
            invoke(
                runner, "add", "box", "user@example.com:2222", "--tag", "gpu"
            ).exit_code
            == 0
        )

        result = invoke(runner, "show", "box")

        assert result.exit_code == 0
        assert "user@example.com:2222" in result.output
        assert "gpu" in result.output

    def test_connect_builds_ssh_argv_with_extra_args_after_separator(
        self, runner, monkeypatch
    ):
        """connect delegates to ssh exactly once, preserving user-supplied args."""
        assert (
            invoke(
                runner,
                "add",
                "box",
                "user@example.com:2222",
                "--identity",
                "~/.ssh/id",
                "--jump",
                "jump",
            ).exit_code
            == 0
        )
        called = {}

        def fake_execvp(program, argv):
            called["program"] = program
            called["argv"] = argv
            raise SystemExit(0)

        monkeypatch.setattr(host.os, "execvp", fake_execvp)

        result = invoke(runner, "connect", "box", "--", "-A", "-v")

        assert result.exit_code == 0
        assert called == {
            "program": "ssh",
            "argv": [
                "ssh",
                "-i",
                str(Path.home() / ".ssh/id"),
                "-p",
                "2222",
                "-J",
                "jump",
                "-A",
                "-v",
                "box",
            ],
        }

    def test_copy_id_uses_binary_when_available(self, runner, monkeypatch):
        """The preferred path should call ssh-copy-id with derived ssh options."""
        assert (
            invoke(
                runner, "add", "box", "box.example", "--identity", "~/.ssh/id"
            ).exit_code
            == 0
        )
        monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/bin/ssh-copy-id")
        called = {}

        def fake_call(argv):
            called["argv"] = argv
            return 0

        monkeypatch.setattr(host.subprocess, "call", fake_call)

        result = runner.invoke(host.cli, ["copy-id", "box"], catch_exceptions=False)

        assert result.exit_code == 0
        assert called["argv"] == [
            "ssh-copy-id",
            "-i",
            str(Path.home() / ".ssh/id"),
            "box",
        ]

    def test_copy_id_fallback_streams_default_public_key(
        self, runner, isolated_home, monkeypatch
    ):
        """Systems without ssh-copy-id still get a documented authorized_keys fallback."""
        key = ssh_dir(isolated_home) / "id_rsa.pub"
        key.parent.mkdir(mode=0o700, exist_ok=True)
        key.write_text("ssh-rsa AAA test")
        assert invoke(runner, "add", "box", "box.example").exit_code == 0
        monkeypatch.setattr(host.shutil, "which", lambda name: None)
        calls = []
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: calls.append((argv, kwargs))
            or subprocess.CompletedProcess(argv, 0, b"", b""),
        )

        result = invoke(runner, "copy-id", "box")

        assert result.exit_code == 0
        assert calls[0][0][-2] == "box"
        assert calls[0][1]["input"] == "ssh-rsa AAA test\n"

    def test_ls_json_structure(self, runner):
        """JSON output is a contract for scripts, not just pretty text."""
        assert (
            invoke(
                runner, "add", "box", "user@example.com:2222", "--tag", "gpu"
            ).exit_code
            == 0
        )

        data = parse_json_result(invoke(runner, "ls", "--json"))

        assert data == [
            {
                "alias": "box",
                "identity": None,
                "managed": True,
                "port": "2222",
                "reachable": None,
                "tags": ["gpu"],
                "target": "user@example.com:2222",
            }
        ]

    def test_check_json_structure(self, runner, monkeypatch):
        """check --json should expose booleans and timing for automation."""
        assert invoke(runner, "add", "box", "box.example").exit_code == 0
        monkeypatch.setattr(
            host.socket, "create_connection", lambda *args, **kwargs: FakeConn()
        )
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
        )

        row = parse_json_result(invoke(runner, "check", "--json"))[0]

        assert set(row) == {
            "alias",
            "duration",
            "message",
            "ok",
            "ssh",
            "target",
            "tcp",
        }
        assert row["alias"] == "box"

    def test_exec_json_structure(self, runner, monkeypatch):
        """exec --json should include the per-host status and captured output."""
        assert invoke(runner, "add", "box", "box.example").exit_code == 0
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"ok", b""),
        )

        row = parse_json_result(
            runner.invoke(
                host.cli, ["exec", "box", "--json", "--", "cmd"], catch_exceptions=False
            )
        )[0]

        assert set(row) == {"alias", "duration", "exit_code", "ok", "output"}
        assert row["output"] == "ok"

    @pytest.mark.parametrize("command", ["ls", "check", "exec"])
    def test_table_commands_survive_narrow_terminals(
        self, runner, monkeypatch, command
    ):
        """Columns should drop at narrow widths instead of wrapping unreadably."""
        assert (
            invoke(
                runner,
                "add",
                "box",
                "box.example",
                "--identity",
                "/very/long/path/to/key",
                "--tag",
                "tag",
            ).exit_code
            == 0
        )
        monkeypatch.setenv("COLUMNS", "42")
        monkeypatch.setattr(ui, "_console", None)
        monkeypatch.setattr(
            host,
            "check_one",
            lambda entry, timeout: {
                "alias": entry.alias,
                "target": entry.target,
                "tcp": True,
                "ssh": True,
                "ok": True,
                "message": "ok",
                "duration": 0,
            },
        )
        monkeypatch.setattr(
            host.subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
        )

        args = {
            "ls": ["ls", "--check"],
            "check": ["check"],
            "exec": ["exec", "box", "--", "true"],
        }[command]
        result = runner.invoke(host.cli, args, catch_exceptions=False)

        assert result.exit_code == 0
        assert "box" in result.output
        assert "Traceback" not in result.output
        assert "/very/long/path/to/key" not in result.output


class TestListingRendersEveryField:
    """`ls` once crashed for any host that had a tag.

    The display row and the JSON record shared one dict, keyed by case. When
    the column headers were lowercased, `ui.row_for` began finding the JSON
    "tags" key -- a list, which rich refuses to render -- so `usm host ls`
    raised NotRenderableError for anyone who had ever used --tag. No test
    listed a tagged host, so nothing caught it.
    """

    def _add(self, runner, alias="box", *extra):
        result = invoke(runner, "add", alias, "user@example.com:2222", *extra)
        assert result.exit_code == 0, result.output
        return result

    def test_a_tagged_host_lists_without_error(self, runner):
        self._add(runner, "box", "--tag", "gpu")
        result = invoke(runner, "ls")
        assert result.exit_code == 0, result.output
        assert "gpu" in result.output

    def test_several_tags_are_shown_together(self, runner):
        self._add(runner, "box", "--tag", "gpu", "--tag", "prod")
        result = invoke(runner, "ls")
        assert result.exit_code == 0
        assert "gpu,prod" in result.output.replace(" ", "")

    def test_an_untagged_host_lists_too(self, runner):
        self._add(runner, "plain")
        result = invoke(runner, "ls")
        assert result.exit_code == 0 and "plain" in result.output

    def test_a_host_with_an_identity_lists(self, runner):
        self._add(runner, "keyed", "--identity", "~/.ssh/id_ed25519")
        result = invoke(runner, "ls")
        assert result.exit_code == 0 and "keyed" in result.output

    def test_every_row_value_is_a_string(self, runner):
        """rich renders strings; anything else is a crash waiting to happen."""
        self._add(runner, "box", "--tag", "gpu", "--identity", "~/.ssh/k")
        rows = []
        original = host.print_host_table

        def capture(display, **kwargs):
            rows.extend(display)
            return original(display, **kwargs)

        host.print_host_table = capture
        try:
            assert invoke(runner, "ls").exit_code == 0
        finally:
            host.print_host_table = original
        assert rows, "nothing was rendered"
        for row in rows:
            for key, value in row.items():
                assert isinstance(value, str), f"{key} is {type(value).__name__}"

    def test_json_still_carries_structured_tags(self, runner):
        """The table wants text; JSON consumers want the real list."""
        self._add(runner, "box", "--tag", "gpu", "--tag", "prod")
        data = parse_json_result(invoke(runner, "ls", "--json"))
        assert data[0]["tags"] == ["gpu", "prod"]

    def test_json_has_no_display_only_keys(self, runner):
        self._add(runner, "box", "--tag", "gpu")
        data = parse_json_result(invoke(runner, "ls", "--json"))
        assert "reach" not in data[0], "display fields leaked into the contract"
        assert "reachable" in data[0]

    @pytest.mark.parametrize("width", ["40", "60", "80", "120", "200"])
    def test_a_tagged_host_lists_at_any_width(self, runner, monkeypatch, width):
        monkeypatch.setenv("COLUMNS", width)
        self._add(runner, "box", "--tag", "gpu")
        assert invoke(runner, "ls").exit_code == 0

    def test_check_adds_reachability_without_breaking_the_row(
        self, runner, monkeypatch
    ):
        self._add(runner, "box", "--tag", "gpu")
        monkeypatch.setattr(
            host, "check_one", lambda entry, timeout: {"ok": True, "detail": "ok"}
        )
        result = invoke(runner, "ls", "--check")
        assert result.exit_code == 0 and "ok" in result.output
