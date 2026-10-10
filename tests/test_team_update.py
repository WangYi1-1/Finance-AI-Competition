"""Exercise teammate updates against an offline Git origin on real Windows."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/WangYi1-1/Finance-AI-Competition.git"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows teammate updater")


def execute(args, folder, env, *, check=True, input=None):
    result = subprocess.run(
        args, cwd=folder, env=env, input=input, capture_output=True, timeout=30,
    )
    if check:
        assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", "replace")
    return result


def git(folder, env, *args):
    return execute(["git", *args], folder, env)


@pytest.fixture
def repository(tmp_path):
    base = tmp_path / "组员测试 & (空格)"
    base.mkdir()
    bare = base / "origin.git"
    seed = base / "source"
    clone = base / "clone"
    seed.mkdir()
    # An isolated Git config prevents this test from consulting credential helpers.
    config = base / "test-git-config"
    config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        "GIT_EDITOR": "true",
    })
    git(base, env, "init", "--bare", "--initial-branch=main", str(bare))
    git(seed, env, "init", "--initial-branch=main")
    git(seed, env, "config", "user.name", "Offline updater test")
    git(seed, env, "config", "user.email", "updater-test@example.invalid")
    git(seed, env, "config", "core.autocrlf", "false")
    git(seed, env, "config", "commit.gpgsign", "false")
    shutil.copyfile(ROOT / "update_team.ps1", seed / "update_team.ps1")
    shutil.copyfile(ROOT / "更新到最新版.cmd", seed / "更新到最新版.cmd")
    (seed / ".gitignore").write_text(".env\noutput/\nuploads/\n.venv/\n", encoding="utf-8")
    (seed / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    (seed / "requirements.txt").write_text("# First version\n", encoding="utf-8")
    git(seed, env, "add", ".")
    git(seed, env, "commit", "-m", "Initial offline fixture")
    git(seed, env, "remote", "add", "origin", str(bare))
    git(seed, env, "push", "-u", "origin", "main")
    git(base, env, "clone", str(bare), str(clone))
    git(clone, env, "config", "core.autocrlf", "false")
    git(clone, env, "remote", "set-url", "origin", REMOTE)
    git(base, env, "config", "--global", f"url.{bare.as_uri()}.insteadOf", REMOTE)
    return seed, clone, env


def update(clone, env):
    # UTF-8 output makes actual Chinese diagnostics testable on Windows PowerShell 5.1.
    return execute([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; & './update_team.ps1'",
    ], clone, env, check=False)


def message(result):
    return (result.stdout + result.stderr).decode("utf-8", "replace")


def advance(seed, env, *, dependency=False):
    (seed / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    if dependency:
        (seed / "requirements.txt").write_text("# Updated dependencies\n", encoding="utf-8")
    git(seed, env, "add", ".")
    git(seed, env, "commit", "-m", "Advance offline fixture")
    git(seed, env, "push", "origin", "main")


def test_fast_forward_keeps_local_settings_and_results(repository):
    seed, clone, env = repository
    # These are fabricated test settings; the real project .env is never read.
    local_config = clone / ".env"
    local_config.write_bytes(b"DEEPSEEK_API_KEY=fake-for-local-test-only\r\n")
    result_file = clone / "output" / "member-test" / "result.json"
    result_file.parent.mkdir(parents=True)
    result_file.write_bytes(b'{"local_result": true}\n')
    untracked = clone / "personal-notes.txt"
    untracked.write_bytes(b"keep local notes\n")
    before = {path: path.read_bytes() for path in (local_config, result_file, untracked)}
    advance(seed, env)
    result = update(clone, env)
    assert result.returncode == 0, message(result)
    assert "代码更新完成" in message(result)
    assert (clone / "app.py").read_text(encoding="utf-8") == "VERSION = 2\n"
    assert all(path.read_bytes() == value for path, value in before.items())
    assert git(clone, env, "rev-parse", "HEAD").stdout == git(seed, env, "rev-parse", "HEAD").stdout


def test_up_to_date_does_not_request_reinstall(repository):
    _, clone, env = repository
    result = update(clone, env)
    assert result.returncode == 0, message(result)
    assert "已经是最新版" in message(result)
    assert "依赖版本有更新" not in message(result)


def test_dependency_change_requests_installation(repository):
    seed, clone, env = repository
    advance(seed, env, dependency=True)
    result = update(clone, env)
    assert result.returncode == 0, message(result)
    assert "依赖版本有更新" in message(result)
    assert "首次安装.cmd" in message(result)


@pytest.mark.parametrize("staged", [False, True])
def test_modified_tracked_source_stops_before_pull(repository, staged):
    seed, clone, env = repository
    (clone / "app.py").write_text("LOCAL_CHANGE = True\n", encoding="utf-8")
    if staged:
        git(clone, env, "add", "app.py")
    head = git(clone, env, "rev-parse", "HEAD").stdout
    advance(seed, env)
    result = update(clone, env)
    assert result.returncode == 1
    assert "你修改过项目代码" in message(result)
    assert git(clone, env, "rev-parse", "HEAD").stdout == head
    assert (clone / "app.py").read_text(encoding="utf-8") == "LOCAL_CHANGE = True\n"


def test_wrong_remote_stops_without_network(repository):
    _, clone, env = repository
    git(clone, env, "remote", "set-url", "origin", "https://example.invalid/unrelated.git")
    result = update(clone, env)
    assert result.returncode == 1
    assert "来源不是指定项目" in message(result)
    assert "正在获取" not in message(result)


def test_other_branch_stops_before_pull(repository):
    _, clone, env = repository
    git(clone, env, "checkout", "-b", "member-experiment")
    result = update(clone, env)
    assert result.returncode == 1
    assert "不在 main 分支" in message(result)
    assert "正在获取" not in message(result)


def test_diverged_commits_are_not_merged_or_reset(repository):
    seed, clone, env = repository
    git(clone, env, "config", "user.name", "Offline teammate")
    git(clone, env, "config", "user.email", "teammate-test@example.invalid")
    git(clone, env, "config", "commit.gpgsign", "false")
    (clone / "app.py").write_text("MEMBER_VERSION = 1\n", encoding="utf-8")
    git(clone, env, "add", "app.py")
    git(clone, env, "commit", "-m", "Keep local teammate commit")
    local_head = git(clone, env, "rev-parse", "HEAD").stdout
    advance(seed, env)
    result = update(clone, env)
    assert result.returncode == 1
    assert "同步未完成" in message(result)
    assert git(clone, env, "rev-parse", "HEAD").stdout == local_head
    assert (clone / "app.py").read_text(encoding="utf-8") == "MEMBER_VERSION = 1\n"


def test_non_clone_folder_stops(tmp_path):
    folder = tmp_path / "旧压缩包"
    folder.mkdir()
    shutil.copyfile(ROOT / "update_team.ps1", folder / "update_team.ps1")
    result = update(folder, dict(os.environ))
    assert result.returncode == 1
    assert "不是克隆的仓库" in message(result)


@pytest.mark.parametrize("exit_code", [0, 37])
def test_cmd_preserves_helper_exit_code(tmp_path, exit_code):
    folder = tmp_path / "成员 & (中文空格)"
    folder.mkdir()
    shutil.copyfile(ROOT / "更新到最新版.cmd", folder / "更新到最新版.cmd")
    (folder / "update_team.ps1").write_text(
        f"Write-Host 'UPDATER_HELPER_REACHED'\nexit {exit_code}\n", encoding="utf-8-sig",
    )
    result = execute(
        ["cmd.exe", "/d", "/c", "更新到最新版.cmd"], folder,
        dict(os.environ), check=False, input=b"\r\n",
    )
    assert result.returncode == exit_code
    assert b"UPDATER_HELPER_REACHED" in result.stdout


def test_entrypoint_encodings_are_compatible_with_windows_powershell():
    cmd = (ROOT / "更新到最新版.cmd").read_bytes()
    ps = (ROOT / "update_team.ps1").read_bytes()
    assert not cmd.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in cmd and b"\n" not in cmd.replace(b"\r\n", b"")
    assert ps.startswith(b"\xef\xbb\xbf")
