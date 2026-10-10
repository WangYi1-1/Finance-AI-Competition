"""用临时本地 Git 远端验证发布；不访问真实 GitHub，不读取真实配置。"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import prepare_cloud_release as release
from scripts import publish_cloud_update as publish
from test_cloud_release import project, seal_fixture


def local_git(arguments, cwd=None, *, binary=False):
    result = subprocess.run(["git", "-c", "core.autocrlf=false", *arguments], cwd=cwd,
                            capture_output=True, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8")


@pytest.fixture
def git_project(project, tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("Git is unavailable")
    root, runs = project
    remote = tmp_path / "synthetic_remote.git"
    local_git(["init", "--bare", "--initial-branch", "main", str(remote)])
    monkeypatch.setattr(publish, "REMOTE", remote.as_posix())
    return root, runs, remote


def first_publish(git_project):
    root, _, _ = git_project
    folder = release.prepare(root)
    result = publish.sync_and_publish(root, folder)
    assert result["published"]
    return folder, result


def test_first_dry_run_does_not_initialize_copy_commit_or_push(git_project):
    root, _, remote = git_project
    folder = release.prepare(root)
    result = publish.sync_and_publish(root, folder, dry_run=True)
    assert result["first_publish"] and result["dry_run"] and not result["published"]
    assert not (root / "deploy/github").exists()
    assert publish.remote_heads() == {}


def test_first_publish_tracks_only_reviewed_files_and_preserves_crlf_bytes(git_project):
    root, runs, remote = git_project
    trace = runs["public"] / "trace.jsonl"
    trace.write_bytes(b'{"synthetic":true}\r\n')
    seal_fixture(runs["public"], "latest_public")
    folder, result = first_publish(git_project)
    repo = root / "deploy/github"
    assert release.verify_snapshot(repo) == []
    trace_name = trace.relative_to(root).as_posix()
    stored = local_git(["--git-dir", str(remote), "show", f"main:{trace_name}"], binary=True)
    assert stored == trace.read_bytes()
    assert local_git(["status", "--porcelain=v1"], repo) == ""
    tracked = set(local_git(["ls-files", "-z"], repo).split("\0")) - {""}
    snapshot = json.loads((folder / "cloud_snapshot.json").read_text(encoding="utf-8"))
    assert tracked == {item["file"] for item in snapshot["files"]} | {"cloud_snapshot.json"}
    assert not {".env", ".streamlit/secrets.toml"} & tracked
    assert publish.remote_heads()["main"] == result["commit"]


def test_second_publish_removes_only_obsolete_whitelisted_files_and_updates_source(git_project):
    root, _, _ = git_project
    retired = root / "finance_agent/retired.py"
    retired.write_text("# old module\n")
    first_publish(git_project)
    retired.unlink()
    (root / "app.py").write_text("# source revision 2\n")
    folder = release.prepare(root)
    result = publish.sync_and_publish(root, folder)
    assert result["published"] and not result["first_publish"]
    assert "finance_agent/retired.py" in result["removed"]
    assert "app.py" in result["changed"]
    repo = root / "deploy/github"
    assert not (repo / "finance_agent/retired.py").exists()
    assert (repo / "app.py").read_text() == "# source revision 2\n"
    assert release.verify_snapshot(repo) == []


def test_no_change_release_does_not_commit_only_to_refresh_timestamp(git_project):
    root, _, _ = git_project
    _, previous = first_publish(git_project)
    folder = release.prepare(root)
    result = publish.sync_and_publish(root, folder)
    assert result["no_changes"] and not result["published"]
    assert publish.remote_heads()["main"] == previous["commit"]


@pytest.mark.parametrize("extra", [".env", "personal.txt"])
def test_local_extra_or_ignored_file_is_not_overwritten_or_published(git_project, extra):
    root, _, _ = git_project
    first_publish(git_project)
    repo = root / "deploy/github"
    content = "Synthetic private value\n"
    (repo / extra).write_text(content)
    folder = release.prepare(root)
    with pytest.raises(publish.PublishError):
        publish.sync_and_publish(root, folder)
    assert (repo / extra).read_text() == content


def test_local_tracked_modification_blocks_before_overwrite(git_project):
    root, _, _ = git_project
    first_publish(git_project)
    app = root / "deploy/github/app.py"
    app.write_text("# another tool changed this\n")
    folder = release.prepare(root)
    with pytest.raises(publish.PublishError, match="未提交"):
        publish.sync_and_publish(root, folder)
    assert app.read_text() == "# another tool changed this\n"


def test_committed_and_pushed_modification_without_snapshot_update_is_detected(git_project):
    root, _, _ = git_project
    first_publish(git_project)
    repo = root / "deploy/github"
    (repo / "app.py").write_text("# someone changed sealed content\n")
    local_git(["add", "app.py"], repo)
    local_git(["commit", "-m", "Synthetic other change"], repo)
    local_git(["push", "origin", "main"], repo)
    with pytest.raises(publish.PublishError, match="上一份快照"):
        publish.sync_and_publish(root, release.prepare(root))


def test_remote_advance_by_another_checkout_blocks_before_local_sync(git_project, tmp_path):
    root, _, remote = git_project
    first_publish(git_project)
    repo = root / "deploy/github"
    old_content = (repo / "app.py").read_bytes()
    other = tmp_path / "other_public_checkout"
    local_git(["clone", str(remote), str(other)])
    (other / "README.md").write_text("# synthetic remote change\n")
    local_git(["add", "README.md"], other)
    local_git(["-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
               "commit", "-m", "Remote changed"], other)
    local_git(["push", "origin", "main"], other)
    (root / "app.py").write_text("# requested revision\n")
    with pytest.raises(publish.PublishError, match="远端 main"):
        publish.sync_and_publish(root, release.prepare(root))
    assert (repo / "app.py").read_bytes() == old_content


def test_first_publish_never_replaces_a_nonempty_remote(git_project, tmp_path):
    root, _, remote = git_project
    other = tmp_path / "preexisting_public_project"
    local_git(["init", "--initial-branch", "main", str(other)])
    (other / "README.md").write_text("# Existing public project\n")
    local_git(["add", "README.md"], other)
    local_git(["-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
               "commit", "-m", "Original"], other)
    local_git(["remote", "add", "origin", str(remote)], other)
    local_git(["push", "origin", "main"], other)
    with pytest.raises(publish.PublishError, match="空远端"):
        publish.sync_and_publish(root, release.prepare(root))
    assert not (root / "deploy/github").exists()


def test_no_push_option_commits_only_locally_and_requires_explicit_review(git_project):
    root, _, _ = git_project
    folder = release.prepare(root)
    result = publish.sync_and_publish(root, folder, no_push=True)
    assert result["committed"] and not result["published"]
    assert publish.remote_heads() == {}
    with pytest.raises(publish.PublishError, match="远端 main"):
        publish.sync_and_publish(root, folder)


def test_release_must_be_from_fixed_staging_parent(project, tmp_path):
    root, _ = project
    folder = release.prepare(root)
    external = tmp_path / "external_copy"
    shutil.copytree(folder, external)
    with pytest.raises(publish.PublishError, match="deploy/releases"):
        publish.approved_release(root, external)


def test_forged_snapshot_cannot_expand_the_source_whitelist(project):
    root, _ = project
    folder = release.prepare(root)
    extra = folder / "unrelated_private.txt"
    extra.write_text("Synthetic extra value\n")
    snapshot_file = folder / "cloud_snapshot.json"
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    snapshot["files"].append({"file": extra.name, "bytes": extra.stat().st_size,
                              "sha256": release.digest(extra.read_bytes())})
    snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(publish.PublishError, match="白名单"):
        publish.approved_release(root, folder)


def test_git_failures_never_forward_credential_like_stdout_or_stderr(monkeypatch):
    fake_token = "SyntheticToken" * 3

    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "https://user:" + fake_token, fake_token)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(publish.PublishError) as caught:
        publish.git(["push", "origin", "main"])
    assert fake_token not in str(caught.value)


def test_publish_command_never_contains_force(git_project, monkeypatch):
    root, _, _ = git_project
    commands = []
    original = publish.git

    def record(arguments, repository=None):
        commands.append(arguments)
        return original(arguments, repository)

    monkeypatch.setattr(publish, "git", record)
    first_publish(git_project)
    assert all("--force" not in arguments and "-f" not in arguments for arguments in commands)
    assert ["push", "--set-upstream", "origin", "main"] in commands
