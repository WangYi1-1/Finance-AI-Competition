"""审核白名单发布副本后更新固定 GitHub 仓库；拒绝覆盖本地或远端的额外改动。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_cloud_release import (check_content, digest, prepare, read_file, read_json,
                                          safe_relative, verify_snapshot)

REMOTE = "https://github.com/WangYi1-1/Finance-AI-Competition.git"
BRANCH = "main"
AUTHOR_NAME = "WangYi1-1"
AUTHOR_EMAIL = "WangYi1-1@users.noreply.github.com"


class PublishError(ValueError):
    pass


def git(arguments: list[str], repository: Path | None = None) -> str:
    command = ["git", "-c", "core.autocrlf=false"]
    if repository is not None:
        command.extend(["-C", str(repository)])
    command.extend(arguments)
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}, check=False)
    except OSError:
        raise PublishError("未能启动 Git，请安装 Git for Windows 后重试。") from None
    if result.returncode:
        # Git 错误可能包含带认证信息的 URL，不转发 stdout/stderr。
        operation = arguments[0] if arguments else "操作"
        raise PublishError(f"Git 的 {operation} 操作未完成，请检查网络、GitHub 登录或仓库状态；未显示认证信息。")
    return result.stdout


def fixed_repository(root: Path) -> Path:
    root = root.resolve()
    expected = root / "deploy/github"
    if expected.resolve() != expected or not expected.resolve().is_relative_to(root):
        raise PublishError("固定发布仓库路径存在链接或越界，已停止。")
    if expected.exists() and not expected.is_dir():
        raise PublishError("固定发布仓库路径不是文件夹。")
    return expected


def approved_release(root: Path, folder: Path) -> dict[str, bytes]:
    root = root.resolve()
    absolute = folder.absolute()
    folder = folder.resolve()
    expected_parent = root / "deploy/releases"
    if (folder != absolute or folder.parent != expected_parent or not folder.is_relative_to(root)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", folder.name)):
        raise PublishError("只能发布本项目 deploy/releases 中的白名单发布副本。")
    errors = verify_snapshot(folder)
    if errors:
        raise PublishError("发布副本核验未通过：" + "; ".join(errors))
    snapshot = read_json(folder, "cloud_snapshot.json")
    payloads = {}
    for record in snapshot["files"]:
        name = record["file"]
        content = read_file(folder, name)
        check_content(name, content)
        if digest(content) != record["sha256"] or len(content) != record["bytes"]:
            raise PublishError("发布文件在审核后发生变化，已停止。")
        payloads[name] = content
    payloads["cloud_snapshot.json"] = read_file(folder, "cloud_snapshot.json")
    if payloads.get(".gitattributes") != b"# Preserve sealed artifact bytes across Windows and Linux.\n* -text\n":
        raise PublishError("发布副本缺少封存文件的 Git 字节保留规则。")
    return payloads


def remote_heads(repository: Path | None = None) -> dict[str, str]:
    text = git(["ls-remote", "--heads", "origin" if repository else REMOTE], repository)
    heads = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if (len(fields) != 2 or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", fields[0])
                or not fields[1].startswith("refs/heads/")):
            raise PublishError("远端分支信息格式异常，已停止。")
        heads[fields[1][len("refs/heads/"):]] = fields[0]
    return heads


def repository_state(root: Path) -> dict:
    repository = fixed_repository(root)
    if not repository.exists() or not any(repository.iterdir()):
        if remote_heads():
            raise PublishError("首次发布要求空远端仓库；远端已有分支，请先人工核对。")
        return {"repository": repository, "first_publish": True, "head": None, "files": {}}
    if not (repository / ".git").is_dir():
        raise PublishError("固定目录已有内容但不是发布仓库，已停止以免覆盖文件。")
    if git(["symbolic-ref", "--quiet", "--short", "HEAD"], repository).strip() != BRANCH:
        raise PublishError("本地发布仓库不在 main 分支，已停止。")
    remotes = git(["remote"], repository).splitlines()
    if remotes != ["origin"]:
        raise PublishError("本地发布仓库的远端设置与固定目标不一致。")
    for arguments in (["remote", "get-url", "--all", "origin"],
                      ["remote", "get-url", "--push", "--all", "origin"]):
        if git(arguments, repository).splitlines() != [REMOTE]:
            raise PublishError("本地发布仓库的拉取或推送地址不是固定 GitHub 目标。")
    if git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], repository):
        raise PublishError("本地发布仓库有未提交或新增文件，已停止；请先人工核对这些改动。")
    errors = verify_snapshot(repository)
    if errors:
        raise PublishError("本地发布仓库与上一份快照不一致，已停止以免覆盖：" + "; ".join(errors))
    previous = read_json(repository, "cloud_snapshot.json")
    tracked = {name for name in git(["ls-files", "-z"], repository).split("\0") if name}
    expected = {item["file"] for item in previous["files"]} | {"cloud_snapshot.json"}
    if tracked != expected:
        raise PublishError("Git 跟踪文件与上一份发布快照不一致，已停止。")
    head = git(["rev-parse", "HEAD"], repository).strip()
    if remote_heads(repository).get(BRANCH) != head:
        raise PublishError("远端 main 与本地提交不一致，可能有其他人更新或尚未推送的提交；请先人工核对。")
    return {"repository": repository, "first_publish": False, "head": head,
            "files": {item["file"]: item["sha256"] for item in previous["files"]}}


def release_plan(root: Path, folder: Path):
    payloads = approved_release(root, folder)
    state = repository_state(root)
    old = state["files"]
    new = {name: digest(content) for name, content in payloads.items() if name != "cloud_snapshot.json"}
    summary = {"first_publish": state["first_publish"], "added": sorted(set(new) - set(old)),
               "changed": sorted(name for name in set(new) & set(old) if new[name] != old[name]),
               "removed": sorted(set(old) - set(new)), "no_changes": old == new}
    return summary, payloads, state


def target_file(repository: Path, name: str) -> Path:
    path = repository / safe_relative(name)
    if path.resolve() != path or not path.resolve().is_relative_to(repository):
        raise PublishError("发布文件路径存在链接或越界，已停止。")
    return path


def sync_and_publish(root: Path, folder: Path, *, dry_run=False, no_push=False,
                     author_name=AUTHOR_NAME, author_email=AUTHOR_EMAIL):
    summary, payloads, state = release_plan(root, folder)
    if dry_run or summary["no_changes"]:
        return {**summary, "published": False, "dry_run": dry_run, "release": str(folder)}
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", author_name) or not re.fullmatch(
            r"[A-Za-z0-9+._-]+@users\.noreply\.github\.com", author_email):
        raise PublishError("提交身份须使用已核实的 GitHub 用户名和 noreply 邮箱。")
    repository = state["repository"]
    if state["first_publish"]:
        repository.mkdir(parents=True, exist_ok=True)
        git(["init", "--initial-branch", BRANCH, str(repository)])
        git(["remote", "add", "origin", REMOTE], repository)
        git(["config", "--local", "user.name", author_name], repository)
        git(["config", "--local", "user.email", author_email], repository)
        git(["config", "--local", "core.autocrlf", "false"], repository)
    else:
        # 再读一次全部旧文件：计划生成与实际替换之间若发生写入，不覆盖。
        current = repository_state(root)
        if current["head"] != state["head"] or current["files"] != state["files"]:
            raise PublishError("发布审核期间本地或远端状态发生变化，已停止。")
    for name in summary["removed"]:
        target_file(repository, name).unlink()
    for name, content in payloads.items():
        target = target_file(repository, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".publish_" + uuid.uuid4().hex[:8])
        temporary.write_bytes(content)
        os.replace(temporary, target)
    errors = verify_snapshot(repository)
    if errors:
        raise PublishError("同步后发布副本核验未通过；未提交或推送：" + "; ".join(errors))
    git(["add", "--all", "--", "."], repository)
    tracked = {name for name in git(["ls-files", "-z"], repository).split("\0") if name}
    if tracked != set(payloads):
        raise PublishError("待提交文件与白名单不一致；未提交或推送。")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    git(["commit", "-m", f"Update financial report workbench ({stamp})"], repository)
    commit = git(["rev-parse", "HEAD"], repository).strip()
    if no_push:
        return {**summary, "published": False, "committed": True, "commit": commit,
                "repository": str(repository), "release": str(folder)}
    # 普通 push 由 Git 保证不会覆盖突然出现的远端提交；从不使用 force。
    git(["push", "--set-upstream", "origin", BRANCH], repository)
    if remote_heads(repository).get(BRANCH) != commit:
        raise PublishError("推送后的远端提交核验未通过，请人工检查。")
    return {**summary, "published": True, "committed": True, "commit": commit,
            "repository": str(repository), "release": str(folder)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, help="已审核的 deploy/releases 发布目录；省略时重新准备")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="只生成干净发布副本，不接触 GitHub")
    mode.add_argument("--dry-run", action="store_true", help="只检查副本、本地快照与远端状态，不写仓库或推送")
    mode.add_argument("--no-push", action="store_true", help="同步并生成本地提交，交由人工审阅后推送")
    parser.add_argument("--author-name", default=AUTHOR_NAME)
    parser.add_argument("--author-email", default=AUTHOR_EMAIL)
    args = parser.parse_args()
    try:
        folder = args.release.resolve() if args.release else prepare(ROOT)
        if args.prepare_only:
            approved_release(ROOT, folder)
            print(f"已准备云端发布副本：{folder}\n未修改固定仓库，未推送。")
            return
        result = sync_and_publish(ROOT, folder, dry_run=args.dry_run, no_push=args.no_push,
                                  author_name=args.author_name, author_email=args.author_email)
        print(f"发布副本：{folder}")
        print(f"新增 {len(result['added'])} 个、更新 {len(result['changed'])} 个、移除 {len(result['removed'])} 个文件。")
        if result.get("published"):
            print("已推送到 GitHub 的 main 分支；组员在本地 git pull 即可取得最新版。")
        elif result.get("committed"):
            print("已生成本地提交，尚未推送。请审核后在固定仓库执行正常 git push。")
        elif result.get("no_changes"):
            print("与已发布版本一致，没有需要推送的变更。")
        else:
            print("检查通过；未修改固定仓库，未提交或推送。")
    except (ValueError, OSError) as error:
        if isinstance(error, OSError):
            # 路径异常可能包含平台附加文本，仅报告类别。
            message = "本地文件操作未完成，请检查发布目录权限；未显示系统认证信息。"
        else:
            message = str(error)
        print("GitHub 发布未完成：" + message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
