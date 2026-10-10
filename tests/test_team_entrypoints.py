"""Exercise the Windows handoff entrypoints with cmd.exe and Windows PowerShell."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows handoff entrypoints")


def project_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "小组测试 & (含空格)"
    folder.mkdir()
    return folder


def run_entry(folder: Path, entry: str, timeout: int = 30) -> subprocess.CompletedProcess:
    shutil.copyfile(ROOT / entry, folder / entry)
    return subprocess.run(
        ["cmd.exe", "/d", "/c", entry], cwd=folder,
        input=b"\r\n", capture_output=True, timeout=timeout,
    )


@pytest.mark.parametrize("return_code", [0, 37])
def test_first_install_reaches_powershell_and_preserves_return_code(tmp_path, return_code):
    folder = project_folder(tmp_path)
    (folder / "setup_team.ps1").write_text(
        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'helper-reached.txt') -Value 'OK'\n"
        "Write-Host 'INSTALL_HELPER_REACHED'\n"
        f"exit {return_code}\n", encoding="utf-8-sig",
    )
    result = run_entry(folder, "首次安装.cmd")
    assert result.returncode == return_code
    assert (folder / "helper-reached.txt").is_file()
    assert b"INSTALL_HELPER_REACHED" in result.stdout
    assert b"This window will stay open" in result.stdout
    assert (b"Installation failed" in result.stdout) == bool(return_code)


def test_first_install_missing_helper_reports_failure(tmp_path):
    result = run_entry(project_folder(tmp_path), "首次安装.cmd")
    assert result.returncode != 0
    assert b"Installation failed" in result.stdout
    assert b"This window will stay open" in result.stdout


def test_start_without_environment_reports_installation_needed(tmp_path):
    result = run_entry(project_folder(tmp_path), "启动财报分析.cmd")
    assert result.returncode == 1
    assert b"Project environment is missing" in result.stdout


def test_start_without_app_reports_incomplete_extraction(tmp_path):
    folder = project_folder(tmp_path)
    python = folder / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    result = run_entry(folder, "启动财报分析.cmd")
    assert result.returncode == 1
    assert b"app.py is missing" in result.stdout


def test_installation_failure_saves_log_and_keeps_existing_config(tmp_path):
    folder = project_folder(tmp_path)
    shutil.copyfile(ROOT / "setup_team.ps1", folder / "setup_team.ps1")
    (folder / ".venv").mkdir()
    config = folder / ".env"
    config.write_text("TEAM_TEST_SETTING=keep\n", encoding="utf-8")
    before = config.read_bytes()
    result = run_entry(folder, "首次安装.cmd")
    assert result.returncode == 1
    assert (folder / "install.log").stat().st_size > 0
    assert config.read_bytes() == before
    assert b"Installation failed" in result.stdout


def test_installer_creates_local_environment_and_preserves_config(tmp_path):
    folder = project_folder(tmp_path)
    shutil.copyfile(ROOT / "setup_team.ps1", folder / "setup_team.ps1")
    # Empty dependencies exercise the real installer without downloading packages.
    (folder / "requirements.lock.txt").write_text("", encoding="utf-8")
    (folder / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    config = folder / ".env"
    config.write_text("TEAM_TEST_SETTING=keep\n", encoding="utf-8")
    before = config.read_bytes()
    result = run_entry(folder, "首次安装.cmd", timeout=90)
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert (folder / ".venv" / "Scripts" / "python.exe").is_file()
    assert (folder / "install.log").stat().st_size > 0
    assert config.read_bytes() == before
