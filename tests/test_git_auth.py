"""Tests for the directory-aware Git identity manager."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import git_auth


@pytest.fixture
def auth_home(tmp_path, monkeypatch):
    root = tmp_path / "config" / "usm" / "git"
    monkeypatch.setattr(git_auth, "ROOT", root)
    return root


@pytest.fixture
def runner():
    return CliRunner()


def invoke_ok(runner: CliRunner, *args: str):
    result = runner.invoke(git_auth.cli, list(args))
    assert result.exit_code == 0, result.output
    return result


def add_profile(runner: CliRunner, alias: str, email: str | None = None):
    return invoke_ok(
        runner,
        "add",
        alias,
        f"{alias.title()} User",
        email or f"{alias}@example.com",
    )


def make_key(directory, name="id_test"):
    path = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
    )
    return path


def test_add_writes_source_and_generated_profile(auth_home, runner):
    add_profile(runner, "work")

    profile = json.loads((auth_home / "profiles" / "work" / "profile.json").read_text())
    rendered = (auth_home / "profiles" / "work" / "gitconfig").read_text()

    assert profile["name"] == "Work User"
    assert profile["email"] == "work@example.com"
    assert "[user]" in rendered
    assert 'name = "Work User"' in rendered
    assert stat.S_IMODE((auth_home / "profiles" / "work").stat().st_mode) == 0o700


@pytest.mark.parametrize("alias", [".", "..", "1work", "_work", "-work"])
def test_add_rejects_alias_that_does_not_start_with_letter(
    auth_home, runner, alias
):
    add_profile(runner, "work")

    result = runner.invoke(
        git_auth.cli,
        ["add", "--", alias, "Invalid User", "invalid@example.com"],
    )

    assert result.exit_code != 0
    assert "must start with a letter" in result.output
    assert (auth_home / "profiles" / "work" / "profile.json").is_file()


def test_add_imports_private_key_and_sets_permissions(auth_home, tmp_path, runner):
    source = make_key(tmp_path)
    source.chmod(0o644)

    invoke_ok(
        runner,
        "add",
        "work",
        "Work User",
        "work@example.com",
        str(source),
    )

    imported = auth_home / "profiles" / "work" / "identity"
    rendered = (imported.parent / "gitconfig").read_text()
    assert imported.read_text() == source.read_text()
    assert stat.S_IMODE(imported.stat().st_mode) == 0o600
    assert "IdentitiesOnly=yes" in rendered
    assert str(imported) in rendered


def test_nested_mapping_order_and_effective_git_profile(auth_home, tmp_path, runner):
    parent = tmp_path / "projects"
    child = parent / "company"
    repo = child / "service"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    add_profile(runner, "personal")
    add_profile(runner, "work")

    invoke_ok(runner, "use", "personal", str(parent))
    invoke_ok(runner, "use", "work", str(child))

    generated = (auth_home / "generated.gitconfig").read_text()
    assert generated.index("-> personal") < generated.index("-> work")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"include.path={auth_home / 'generated.gitconfig'}",
            "config",
            "user.email",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert result.stdout.strip() == "work@example.com"


def test_use_rejects_a_subdirectory_inside_one_repository(auth_home, tmp_path, runner):
    repo = tmp_path / "repo"
    child = repo / "subdir"
    child.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    add_profile(runner, "work")

    result = runner.invoke(git_auth.cli, ["use", "work", str(child)])
    assert result.exit_code != 0
    assert "Bind the repository root" in result.output
    mappings_path = auth_home / "mappings.json"
    assert (
        not mappings_path.exists()
        or json.loads(mappings_path.read_text())["mappings"] == []
    )


def test_tree_mapping_does_not_leak_to_external_linked_worktree(
    auth_home, tmp_path, runner
):
    main = tmp_path / "company" / "main"
    outside = tmp_path / "personal" / "side"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    outside.parent.mkdir()
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-qb", "side", str(outside)],
        check=True,
    )
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(main.parent))
    subprocess.run(
        ["git", "-C", str(outside), "config", "user.email", "outside@example.com"],
        check=True,
    )

    result = subprocess.run(
        [
            "git",
            "-C",
            str(outside),
            "-c",
            f"include.path={auth_home / 'generated.gitconfig'}",
            "config",
            "user.email",
        ],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert result.stdout.strip() == "outside@example.com"


def test_tree_mapping_applies_to_submodule_inside_tree(auth_home, tmp_path, runner):
    source = tmp_path / "source"
    parent = tmp_path / "projects" / "parent"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "child",
        ],
        check=True,
    )
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(parent.parent))

    result = subprocess.run(
        [
            "git",
            "-C",
            str(parent / "child"),
            "-c",
            f"include.path={auth_home / 'generated.gitconfig'}",
            "config",
            "user.email",
        ],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert result.stdout.strip() == "work@example.com"


def test_explicit_linked_worktree_mapping_uses_exact_gitdir(
    auth_home, tmp_path, runner
):
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-qb", "linked", str(linked)],
        check=True,
    )
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(linked))

    mapping = json.loads((auth_home / "mappings.json").read_text())["mappings"][0]
    assert mapping["scope"] == "worktree"
    assert mapping["git_dirs"]
    result = subprocess.run(
        [
            "git",
            "-C",
            str(linked),
            "-c",
            f"include.path={auth_home / 'generated.gitconfig'}",
            "config",
            "user.email",
        ],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert result.stdout.strip() == "work@example.com"


def test_explicit_bare_repository_mapping_uses_exact_gitdir(
    auth_home, tmp_path, runner, monkeypatch
):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    bare_repo = tmp_path / "repository.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare_repo)], check=True)
    add_profile(runner, "work")

    invoke_ok(runner, "use", "work", str(bare_repo))

    mapping = json.loads((auth_home / "mappings.json").read_text())["mappings"][0]
    assert mapping["scope"] == "repository"
    assert mapping["git_dirs"] == [str(bare_repo.resolve())]
    nested_repo = bare_repo / "nested"
    subprocess.run(["git", "init", "-q", str(nested_repo)], check=True)
    for repository in (bare_repo, nested_repo):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                f"include.path={auth_home / 'generated.gitconfig'}",
                "config",
                "user.email",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        assert result.stdout.strip() == "work@example.com"


def test_tree_mapping_escapes_gitdir_glob_characters(auth_home, tmp_path, runner):
    tree = tmp_path / "projects-[team]*"
    repo = tree / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(tree))

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"include.path={auth_home / 'generated.gitconfig'}",
            "config",
            "user.email",
        ],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert result.stdout.strip() == "work@example.com"


def test_use_requires_force_to_replace_exact_mapping(auth_home, tmp_path, runner):
    directory = tmp_path / "project"
    directory.mkdir()
    add_profile(runner, "one")
    add_profile(runner, "two")
    invoke_ok(runner, "use", "one", str(directory))

    refused = runner.invoke(git_auth.cli, ["use", "two", str(directory)])
    assert refused.exit_code != 0
    assert "--force" in refused.output

    invoke_ok(runner, "use", "two", str(directory), "--force")
    data = json.loads((auth_home / "mappings.json").read_text())
    assert len(data["mappings"]) == 1
    assert data["mappings"][0]["alias"] == "two"


def test_reset_exact_mapping_reveals_inherited_profile(auth_home, tmp_path, runner):
    parent = tmp_path / "projects"
    child = parent / "company"
    child.mkdir(parents=True)
    add_profile(runner, "personal")
    add_profile(runner, "work")
    invoke_ok(runner, "use", "personal", str(parent))
    invoke_ok(runner, "use", "work", str(child))

    result = invoke_ok(runner, "reset", str(child))
    assert "inherited" in result.output
    status = invoke_ok(runner, "status", str(child), "--json")
    assert json.loads(status.output)["profile"]["alias"] == "personal"


def test_reset_does_not_remove_an_inherited_mapping(auth_home, tmp_path, runner):
    parent = tmp_path / "projects"
    child = parent / "child"
    child.mkdir(parents=True)
    add_profile(runner, "personal")
    invoke_ok(runner, "use", "personal", str(parent))

    result = invoke_ok(runner, "reset", str(child))
    assert "No mapping is defined directly" in result.output
    assert len(json.loads((auth_home / "mappings.json").read_text())["mappings"]) == 1


def test_set_unset_and_rename_update_derived_state(auth_home, tmp_path, runner):
    directory = tmp_path / "work"
    directory.mkdir()
    add_profile(runner, "old")
    invoke_ok(runner, "use", "old", str(directory))

    invoke_ok(
        runner,
        "set",
        "old",
        "--key",
        "name",
        "New Name",
        "--key",
        "git.commit.gpgsign",
        "true",
    )
    rendered = (auth_home / "profiles" / "old" / "gitconfig").read_text()
    assert 'name = "New Name"' in rendered
    assert "gpgsign" in rendered

    invoke_ok(runner, "unset", "old", "--key", "git.commit.gpgsign")
    assert "gpgsign" not in (auth_home / "profiles" / "old" / "gitconfig").read_text()

    invoke_ok(runner, "rename", "old", "new")
    assert not (auth_home / "profiles" / "old").exists()
    assert (auth_home / "profiles" / "new").exists()
    mappings = json.loads((auth_home / "mappings.json").read_text())["mappings"]
    assert mappings[0]["alias"] == "new"
    assert "/profiles/new/gitconfig" in (auth_home / "generated.gitconfig").read_text()


def test_rm_refuses_used_profile_without_force(auth_home, tmp_path, runner):
    directory = tmp_path / "work"
    directory.mkdir()
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(directory))

    refused = runner.invoke(git_auth.cli, ["rm", "work"])
    assert refused.exit_code != 0
    assert "--force" in refused.output

    invoke_ok(runner, "rm", "work", "--force")
    assert not (auth_home / "profiles" / "work").exists()
    assert json.loads((auth_home / "mappings.json").read_text())["mappings"] == []


def test_enable_and_disable_manage_only_marker_block(auth_home, tmp_path, runner):
    profile_file = tmp_path / ".zshrc"
    profile_file.write_text("export KEEP_ME=yes\n")

    invoke_ok(
        runner,
        "enable",
        "--shell",
        "zsh",
        "--file",
        str(profile_file),
    )
    content = profile_file.read_text()
    assert "export KEEP_ME=yes" in content
    assert git_auth.BEGIN_MARKER in content
    assert str(auth_home / "shell" / "git-auth.zsh") in content

    # Re-enabling is idempotent.
    invoke_ok(
        runner,
        "enable",
        "--shell",
        "zsh",
        "--file",
        str(profile_file),
    )
    assert profile_file.read_text().count(git_auth.BEGIN_MARKER) == 1

    invoke_ok(runner, "disable", "--shell", "zsh")
    assert profile_file.read_text() == "export KEEP_ME=yes\n"


def test_enable_and_disable_preserve_symlinked_shell_profile(
    auth_home, tmp_path, runner
):
    target = tmp_path / "dotfiles" / ".zshrc"
    target.parent.mkdir()
    target.write_text("export KEEP_ME=yes\n")
    profile_file = tmp_path / ".zshrc"
    profile_file.symlink_to(target)

    invoke_ok(
        runner,
        "enable",
        "--shell",
        "zsh",
        "--file",
        str(profile_file),
    )

    assert profile_file.is_symlink()
    assert git_auth.BEGIN_MARKER in target.read_text()

    invoke_ok(runner, "disable", "--shell", "zsh")
    assert profile_file.is_symlink()
    assert target.read_text() == "export KEEP_ME=yes\n"


def test_enable_restores_symlink_target_when_config_write_fails(
    auth_home, tmp_path, runner, monkeypatch
):
    add_profile(runner, "work")
    target = tmp_path / "dotfiles" / ".zshrc"
    target.parent.mkdir()
    target.write_text("export KEEP_ME=yes\n")
    profile_file = tmp_path / ".zshrc"
    profile_file.symlink_to(target)
    real_write_json = git_auth._write_json

    def fail_config_write(path, data):
        if path == git_auth._config_path():
            raise OSError("simulated config write failure")
        return real_write_json(path, data)

    monkeypatch.setattr(git_auth, "_write_json", fail_config_write)
    result = runner.invoke(
        git_auth.cli,
        [
            "enable",
            "--shell",
            "zsh",
            "--file",
            str(profile_file),
        ],
    )

    assert result.exit_code != 0
    assert profile_file.is_symlink()
    assert target.read_text() == "export KEEP_ME=yes\n"


def test_global_enable_and_disable_manage_one_include(auth_home, tmp_path, runner):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}

    result = runner.invoke(git_auth.cli, ["enable", "--global"], env=env)
    assert result.exit_code == 0, result.output
    global_config = home / ".gitconfig"
    assert str(auth_home / "generated.gitconfig") in global_config.read_text()

    result = runner.invoke(git_auth.cli, ["disable", "--global"], env=env)
    assert result.exit_code == 0, result.output
    assert str(auth_home / "generated.gitconfig") not in global_config.read_text()


def test_global_disable_keeps_state_when_git_update_fails(
    auth_home, tmp_path, runner, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    result = runner.invoke(git_auth.cli, ["enable", "--global"], env=env)
    assert result.exit_code == 0, result.output
    real_run = git_auth.subprocess.run

    def failing_unset(argv, *args, **kwargs):
        if "--unset-all" in argv:
            return subprocess.CompletedProcess(argv, 1, stderr="simulated failure")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(git_auth.subprocess, "run", failing_unset)
    result = runner.invoke(git_auth.cli, ["disable", "--global"], env=env)
    assert result.exit_code != 0
    assert "simulated failure" in result.output
    config = json.loads((auth_home / "config.json").read_text())
    assert any(item["type"] == "global" for item in config["installations"])


def test_shell_helper_preserves_existing_runtime_config(auth_home, runner):
    add_profile(runner, "work")
    helper = auth_home / "shell" / "git-auth.bash"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"export GIT_CONFIG_COUNT=2; source {str(helper)!r}; "
            'printf \'%s|%s|%s\' "$GIT_CONFIG_COUNT" "$GIT_CONFIG_KEY_2" "$GIT_CONFIG_VALUE_2"',
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert result.stdout == f"3|include.path|{auth_home / 'generated.gitconfig'}"


def test_exec_forces_profile_without_a_mapping(auth_home, runner):
    add_profile(runner, "work")
    invoke_ok(
        runner,
        "exec",
        "work",
        "--",
        "sh",
        "-c",
        'test "$(git config user.email)" = work@example.com',
    )


def test_status_and_list_json(auth_home, tmp_path, runner):
    directory = tmp_path / "project"
    directory.mkdir()
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(directory))

    profiles = json.loads(invoke_ok(runner, "list", "--json").output)
    assert profiles[0]["alias"] == "work"
    assert profiles[0]["mappings"] == 1

    status_data = json.loads(
        invoke_ok(runner, "status", str(directory), "--json").output
    )
    assert status_data["enabled"] is False
    assert status_data["profile"]["email"] == "work@example.com"


def test_status_parses_git_origin_when_global_path_contains_spaces(
    auth_home, tmp_path, runner, monkeypatch
):
    home = tmp_path / "home with space"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    repo = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "config", "--global", "user.name", "Space User"],
        check=True,
    )
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(repo))

    status_data = json.loads(
        invoke_ok(runner, "status", str(repo), "--json").output
    )

    actual = status_data["actual"]["user.name"]
    assert actual["value"] == "Space User"
    assert "home with space/.gitconfig" in actual["origin"]


def test_resolve_explains_parent_to_child_precedence(auth_home, tmp_path, runner):
    parent = tmp_path / "projects"
    child = parent / "company"
    child.mkdir(parents=True)
    add_profile(runner, "personal")
    add_profile(runner, "work")
    invoke_ok(runner, "use", "personal", str(parent))
    invoke_ok(runner, "use", "work", str(child))

    data = json.loads(invoke_ok(runner, "resolve", str(child), "--json").output)
    assert [item["alias"] for item in data["matches"]] == ["personal", "work"]
    assert data["selected"]["alias"] == "work"


def test_ssh_test_validates_profile_without_network(auth_home, tmp_path, runner):
    key = make_key(tmp_path, "ssh-test")
    invoke_ok(runner, "add", "work", "Work User", "work@example.com", str(key))
    result = invoke_ok(runner, "test", "work", "github.com")
    assert "SSH configuration is valid" in result.output
    assert "github.com" in result.output


def test_disabled_status_separates_actual_and_expected(auth_home, tmp_path, runner):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(repo))

    status_data = json.loads(invoke_ok(runner, "status", str(repo), "--json").output)
    assert status_data["enabled"] is False
    assert status_data["actual"]["user.email"]["value"] != "work@example.com"
    assert status_data["expected"]["user.email"]["value"] == "work@example.com"
    assert status_data["matches_expected"] is False


def test_doctor_fixes_private_key_permissions(auth_home, tmp_path, runner):
    source = make_key(tmp_path, "key")
    invoke_ok(runner, "add", "work", "Work User", "work@example.com", str(source))
    identity = auth_home / "profiles" / "work" / "identity"
    identity.chmod(0o644)

    result = invoke_ok(runner, "doctor", "--fix")
    assert "repaired and is healthy" in result.output
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600


def test_key_validation_rejects_public_or_unrelated_file(auth_home, tmp_path, runner):
    bad = tmp_path / "not-a-key"
    bad.write_text("ssh-ed25519 AAAA public@example\n")
    result = runner.invoke(
        git_auth.cli,
        ["add", "work", "Work User", "work@example.com", str(bad)],
    )
    assert result.exit_code != 0
    assert "does not look like an SSH private key" in result.output


def test_key_validation_rejects_corrupt_private_key_header(auth_home, tmp_path, runner):
    bad = tmp_path / "corrupt"
    bad.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    bad.chmod(0o600)
    result = runner.invoke(
        git_auth.cli,
        ["add", "work", "Work User", "work@example.com", str(bad)],
    )
    assert result.exit_code != 0
    assert "invalid or unreadable SSH private key" in result.output


def test_force_add_validates_new_key_before_replacing_profile(
    auth_home, tmp_path, runner
):
    add_profile(runner, "work")
    bad = tmp_path / "bad-key"
    bad.write_text("not a private key")

    result = runner.invoke(
        git_auth.cli,
        [
            "add",
            "work",
            "Replacement",
            "replacement@example.com",
            str(bad),
            "--force",
        ],
    )
    assert result.exit_code != 0
    profile = json.loads((auth_home / "profiles" / "work" / "profile.json").read_text())
    assert profile["name"] == "Work User"


def test_status_outside_repository_does_not_report_global_values(
    auth_home, tmp_path, runner
):
    directory = tmp_path / "not-a-repository"
    directory.mkdir()
    add_profile(runner, "work")
    invoke_ok(runner, "use", "work", str(directory))

    status_data = json.loads(
        invoke_ok(runner, "status", str(directory), "--json").output
    )
    assert all(item["value"] is None for item in status_data["effective"].values())


def test_set_is_transactional_when_a_later_field_is_invalid(
    auth_home, tmp_path, runner
):
    old_key = make_key(tmp_path, "old")
    new_key = make_key(tmp_path, "new")
    invoke_ok(runner, "add", "work", "Work User", "work@example.com", str(old_key))
    managed = auth_home / "profiles" / "work" / "identity"
    before = managed.read_bytes()

    result = runner.invoke(
        git_auth.cli,
        [
            "set",
            "work",
            "--key",
            "ssh.identity",
            str(new_key),
            "--key",
            "unsupported",
            "value",
        ],
    )
    assert result.exit_code != 0
    assert managed.read_bytes() == before


def test_reimport_without_public_key_replaces_stale_public_key(
    auth_home, tmp_path, runner
):
    old_key = make_key(tmp_path, "old-pub")
    new_key = make_key(tmp_path, "new-pub")
    invoke_ok(runner, "add", "work", "Work User", "work@example.com", str(old_key))
    old_public = (auth_home / "profiles" / "work" / "identity.pub").read_text()
    Path(str(new_key) + ".pub").unlink()

    invoke_ok(
        runner,
        "set",
        "work",
        "--key",
        "ssh.identity",
        str(new_key),
    )
    new_public = (auth_home / "profiles" / "work" / "identity.pub").read_text()
    assert new_public != old_public
    assert new_public.startswith("ssh-ed25519 ")


def test_keygen_force_failure_keeps_existing_key(
    auth_home, tmp_path, runner, monkeypatch
):
    source = make_key(tmp_path, "existing")
    invoke_ok(runner, "add", "work", "Work User", "work@example.com", str(source))
    managed = auth_home / "profiles" / "work" / "identity"
    before = managed.read_bytes()
    real_run = git_auth.subprocess.run

    def failing_keygen(argv, *args, **kwargs):
        if argv and argv[0] == "ssh-keygen" and "-q" in argv:
            return subprocess.CompletedProcess(argv, 1)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(git_auth.subprocess, "run", failing_keygen)
    result = runner.invoke(git_auth.cli, ["keygen", "work", "--force"])
    assert result.exit_code != 0
    assert managed.read_bytes() == before


def test_doctor_detects_and_repairs_stale_profile_render(auth_home, runner):
    add_profile(runner, "work")
    profile_path = auth_home / "profiles" / "work" / "profile.json"
    profile = json.loads(profile_path.read_text())
    profile["name"] = "Edited By Hand"
    profile_path.write_text(json.dumps(profile))

    result = runner.invoke(git_auth.cli, ["doctor"])
    assert result.exit_code == 0
    assert "rendered gitconfig is missing or stale" in result.output

    result = invoke_ok(runner, "doctor", "--fix")
    assert "repaired and is healthy" in result.output
    assert (
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(auth_home / "profiles" / "work" / "gitconfig"),
                "user.name",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        == "Edited By Hand"
    )


@pytest.mark.parametrize("command", ["doctor", "sync"])
def test_commands_report_invalid_persisted_git_key(
    auth_home, runner, command
):
    add_profile(runner, "work")
    profile_path = auth_home / "profiles" / "work" / "profile.json"
    profile = json.loads(profile_path.read_text())
    profile["git"]["broken"] = "value"
    profile_path.write_text(json.dumps(profile))

    result = runner.invoke(git_auth.cli, [command])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "invalid profile" in result.output
    assert "Git keys must use" in result.output


def test_doctor_fix_does_not_mutate_when_dangling_mapping_exists(
    auth_home, tmp_path, runner
):
    directory = tmp_path / "project"
    directory.mkdir()
    add_profile(runner, "work")
    mappings_path = auth_home / "mappings.json"
    mappings = json.loads(mappings_path.read_text())
    mappings["mappings"].append(
        {
            "path": str(directory),
            "real_path": str(directory.resolve()),
            "alias": "missing",
        }
    )
    mappings_path.write_text(json.dumps(mappings))
    before = mappings_path.read_bytes()

    result = runner.invoke(git_auth.cli, ["doctor", "--fix"])
    assert result.exit_code != 0
    assert "No changes were made" in result.output
    assert mappings_path.read_bytes() == before


def test_zsh_profile_respects_zdotdir(auth_home, tmp_path, monkeypatch):
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh"))
    assert git_auth._profile_target("zsh") == tmp_path / "zsh" / ".zshrc"
