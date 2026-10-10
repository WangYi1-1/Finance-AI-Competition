"""Cloud boundaries and real Streamlit sessions, with all model calls forbidden."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import time
from zipfile import ZipFile

import pymupdf
import pytest
from streamlit.testing.v1 import AppTest

from finance_agent.cloud import (
    CloudConfig, CloudError, analysis_slot, build_run_archive,
    clean_expired_sessions, is_cloud, session_paths,
)
from finance_agent.config import Settings
from finance_agent.llm import DeepSeekClient


ROOT = Path(__file__).resolve().parents[1]
ACCESS_CODE = "test-team-access-2026"


@pytest.fixture
def cloud_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_CLOUD", "1")
    monkeypatch.setenv("TEAM_ACCESS_CODE", ACCESS_CODE)
    monkeypatch.setenv("FINANCE_RUNTIME_DIR", str(tmp_path / "runtime"))
    # Test-owned values replace environment configuration before any loader runs.
    values = {
        "DEEPSEEK_API_KEY": "test-only-never-sent",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_TIMEOUT": "120", "MAX_MODEL_CALLS": "12",
        "MAX_OUTPUT_TOKENS": "6500", "DEEPSEEK_TEMPERATURE": "0",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Cloud tests must not read dotenv or call a model")

    monkeypatch.setattr("finance_agent.config.dotenv_values", forbidden)
    monkeypatch.setattr(DeepSeekClient, "complete_json", forbidden)
    yield CloudConfig.load()


@pytest.mark.parametrize("code", [None, "", "12345678901", " " * 12, "\t" * 12, " \t" * 12])
def test_missing_or_short_access_code_fails_closed(cloud_env, monkeypatch, code):
    if code is None:
        monkeypatch.delenv("TEAM_ACCESS_CODE")
    else:
        monkeypatch.setenv("TEAM_ACCESS_CODE", code)
    with pytest.raises(CloudError, match="至少12位"):
        CloudConfig.load()


def test_credential_comparison_rotation_and_no_secret_repr(cloud_env):
    assert is_cloud()
    assert cloud_env.accepts(ACCESS_CODE)
    assert not cloud_env.accepts(ACCESS_CODE + " ")
    assert not cloud_env.accepts("不同的口令")
    assert ACCESS_CODE not in repr(cloud_env)
    changed = CloudConfig("changed-team-code-2026", cloud_env.runtime_root)
    assert changed.credential_version != cloud_env.credential_version


@pytest.mark.parametrize("session_id", ["../outside", "a" * 31, "A" * 32, "g" * 32, ""])
def test_session_path_rejects_invalid_identifiers(cloud_env, session_id):
    with pytest.raises(CloudError):
        session_paths(cloud_env, session_id)
    assert not cloud_env.runtime_root.exists()


def test_sessions_store_identical_uploads_separately(cloud_env):
    from app import store_uploads

    class Upload:
        name = "../annual.pdf"
        content = b"%PDF-1.7\npublic test fixture"
        size = len(content)

        def getvalue(self):
            return self.content

    first_uploads, first_output = session_paths(cloud_env, "a" * 32)
    second_uploads, second_output = session_paths(cloud_env, "b" * 32)
    first = store_uploads([Upload()], first_uploads)[0]
    second = store_uploads([Upload()], second_uploads)[0]
    assert first != second and first.read_bytes() == second.read_bytes()
    assert first.parent == first_uploads and second.parent == second_uploads
    assert first_output != second_output
    (first_output / "result.json").write_text('{"only":"first-session"}')
    assert not (second_output / "result.json").exists()
    assert (first_uploads.parent / "last_active").is_file()


def test_invalid_batch_does_not_write_any_upload(cloud_env, tmp_path):
    from app import store_uploads

    class Upload:
        name, size = "annual.pdf", 10

        def getvalue(self):
            return b"%PDF-1.7\n"

    target = tmp_path / "not-created"
    with pytest.raises(ValueError, match="1至6份"):
        store_uploads([Upload()] * 7, target)
    assert not target.exists()


def test_retention_only_removes_marked_expired_owned_sessions(cloud_env):
    current, expired, recent, unmarked = "a" * 32, "b" * 32, "c" * 32, "d" * 32
    folders = {}
    for identifier in (current, expired, recent, unmarked):
        uploads, _ = session_paths(cloud_env, identifier)
        folders[identifier] = uploads.parent
        (uploads / "keep-or-expire.pdf").write_bytes(b"public fixture")
    old_time = time.time() - 25 * 3600
    for identifier in (current, expired):
        os.utime(folders[identifier] / "last_active", (old_time, old_time))
    (folders[unmarked] / "last_active").unlink()
    foreign = cloud_env.runtime_root / "sessions" / "other-project"
    foreign.mkdir()
    (foreign / "last_active").touch()
    os.utime(foreign / "last_active", (old_time, old_time))
    outside = cloud_env.runtime_root / "unrelated-data.txt"
    outside.write_text("unrelated project data")
    clean_expired_sessions(cloud_env, current)
    assert not folders[expired].exists()
    assert all(folders[x].exists() for x in (current, recent, unmarked))
    assert foreign.is_dir() and outside.read_text() == "unrelated project data"


def test_failed_analysis_releases_lock_for_next_session(cloud_env):
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with analysis_slot(cloud_env):
            raise RuntimeError("synthetic failure")
    # A new configuration object represents another session/restarted loader.
    reloaded = CloudConfig.load()
    with analysis_slot(reloaded):
        pass
    with analysis_slot(reloaded, mode="demo"):
        pass
    assert not cloud_env.runtime_root.exists()


def test_concurrent_session_is_rejected_but_next_task_can_start(cloud_env):
    def second_session():
        with pytest.raises(CloudError, match="其他组员正在分析"):
            with analysis_slot(CloudConfig.load()):
                pytest.fail("Concurrent job must not begin")

    with analysis_slot(cloud_env):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(second_session).result(timeout=10)
    with analysis_slot(cloud_env):
        pass
    assert not cloud_env.runtime_root.exists()


def test_sequential_tasks_have_no_daily_or_cumulative_limit(cloud_env):
    completed = []
    for index in range(25):
        with analysis_slot(CloudConfig.load()):
            completed.append(index)
    assert len(completed) == 25
    # Concurrency control does not create persistent task counters.
    assert not cloud_env.runtime_root.exists()


def test_cloud_settings_never_reads_dotenv_and_uses_test_environment(cloud_env):
    settings = Settings.load(ROOT)
    assert settings.api_key == "test-only-never-sent"
    assert settings.max_model_calls == 12 and settings.max_output_tokens == 6500
    assert settings.timeout == 120 and settings.temperature == 0
    assert settings.api_key not in repr(settings)


@pytest.mark.parametrize("key,value", [
    ("MAX_MODEL_CALLS", "0"), ("MAX_MODEL_CALLS", "13"),
    ("MAX_OUTPUT_TOKENS", "0"), ("MAX_OUTPUT_TOKENS", "6501"),
    ("DEEPSEEK_TIMEOUT", "0"), ("DEEPSEEK_TIMEOUT", "121"),
])
def test_cloud_settings_rejects_excessive_or_disabled_per_run_limits(cloud_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="云端"):
        Settings.load(ROOT)


def cloud_view(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Viewing cloud samples must not load API configuration")

    monkeypatch.setattr(Settings, "load", classmethod(forbidden))
    view = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not view.exception
    return view


def log_in(view, supplied=ACCESS_CODE):
    view.text_input(key="cloud_access_input").set_value(supplied)
    next(button for button in view.button if button.label == "进入工作台").click().run()
    assert not view.exception
    return view


def test_unauthenticated_app_exposes_only_login(cloud_env, monkeypatch):
    view = cloud_view(monkeypatch)
    assert [item.label for item in view.text_input] == ["组员访问口令"]
    assert not view.radio and not view.metric and not view.selectbox
    assert not view.get("file_uploader") and not view.get("download_button")
    assert "analysis_result" not in view.session_state
    assert not cloud_env.runtime_root.exists()


def test_correct_login_loads_samples_without_api_and_logout_clears_results(cloud_env, monkeypatch):
    view = log_in(cloud_view(monkeypatch))
    assert view.radio(key="run_mode").value == "成果展示"
    assert any(item.label == "营业收入" for item in view.metric)
    assert "analysis_result" in view.session_state
    assert ACCESS_CODE not in str(view.session_state)
    assert "cloud_access_input" not in view.session_state
    identifier = view.session_state["cloud_session_id"]
    assert (cloud_env.runtime_root / "sessions" / identifier).is_dir()
    assert {item.name for item in cloud_env.runtime_root.iterdir()} == {"sessions"}
    next(button for button in view.button if button.label == "退出组员测试").click().run()
    assert not view.exception and not view.metric
    assert "analysis_result" not in view.session_state
    assert "cloud_session_id" not in view.session_state
    assert "cloud_access_version" not in view.session_state
    assert view.text_input(key="cloud_access_input").value == ""


def test_wrong_code_cannot_reveal_saved_or_forged_results(cloud_env, monkeypatch):
    view = cloud_view(monkeypatch)
    view.session_state["analysis_result"] = "forged-result-must-not-display"
    view.session_state["cloud_access_version"] = "not-the-server-credential-version"
    log_in(view, "wrong-team-password")
    assert not view.metric and not view.radio
    assert "analysis_result" not in view.session_state
    assert "cloud_session_id" not in view.session_state
    assert any("访问口令不正确" in item.value for item in view.warning)


def test_access_code_rotation_revokes_existing_browser_session(cloud_env, monkeypatch):
    view = log_in(cloud_view(monkeypatch))
    assert view.metric
    monkeypatch.setenv("TEAM_ACCESS_CODE", "new-team-access-code")
    view.run()
    assert not view.exception and not view.metric
    assert "analysis_result" not in view.session_state
    assert "cloud_session_id" not in view.session_state
    assert view.text_input(key="cloud_access_input").value == ""


def test_missing_cloud_configuration_does_not_expose_workbench(cloud_env, monkeypatch):
    monkeypatch.delenv("TEAM_ACCESS_CODE")
    view = cloud_view(monkeypatch)
    assert view.error and "TEAM_ACCESS_CODE" in view.error[0].value
    assert not view.metric and not view.radio and not view.text_input
    assert not cloud_env.runtime_root.exists()


def test_cloud_entrypoint_reads_only_explicit_platform_settings(cloud_env, monkeypatch):
    import cloud_app
    import streamlit as st

    monkeypatch.delenv("TEAM_ACCESS_CODE")
    monkeypatch.setenv("FINANCE_CLOUD", "0")
    monkeypatch.setattr(st, "secrets", {
        "TEAM_ACCESS_CODE": ACCESS_CODE, "MAX_MODEL_CALLS": 99,
        "ARBITRARY_UNRELATED_SECRET": "do-not-import-this-setting",
    })
    cloud_app.load_platform_settings()
    assert is_cloud() and CloudConfig.load().accepts(ACCESS_CODE)
    # Existing process values take precedence; arbitrary secrets are never imported.
    assert os.environ["MAX_MODEL_CALLS"] == "12"
    assert "ARBITRARY_UNRELATED_SECRET" not in os.environ


def test_cloud_entrypoint_handles_unconfigured_platform_secrets(cloud_env, monkeypatch):
    import cloud_app
    import streamlit as st

    class MissingSecrets:
        def __contains__(self, key):
            raise FileNotFoundError("test-owned missing platform configuration")

    monkeypatch.delenv("TEAM_ACCESS_CODE")
    monkeypatch.setattr(st, "secrets", MissingSecrets())
    cloud_app.load_platform_settings()
    assert is_cloud()
    with pytest.raises(CloudError, match="至少12位"):
        CloudConfig.load()


def test_cloud_entrypoint_keeps_cloud_mode_after_platform_environment_export(cloud_env, monkeypatch):
    import cloud_app
    import streamlit as st

    class PlatformSecrets(dict):
        loaded = False

        def __contains__(self, key):
            # Streamlit lazily reads Secrets, exporting root values into the
            # process environment. Reproduce an accidental FINANCE_CLOUD="0".
            if not self.loaded:
                os.environ["FINANCE_CLOUD"] = "0"
                self.loaded = True
            return super().__contains__(key)

    monkeypatch.delenv("TEAM_ACCESS_CODE")
    monkeypatch.setenv("FINANCE_CLOUD", "0")
    monkeypatch.setattr(st, "secrets", PlatformSecrets({
        "TEAM_ACCESS_CODE": ACCESS_CODE, "FINANCE_CLOUD": "0",
    }))
    cloud_app.load_platform_settings()
    assert is_cloud()
    assert CloudConfig.load().accepts(ACCESS_CODE)
    # A platform setting must not make this dedicated cloud entry read dotenv.
    assert Settings.load(ROOT).api_key == "test-only-never-sent"


@pytest.fixture
def sealed_run(tmp_path):
    folder = tmp_path / "sealed_run"
    folder.mkdir()
    (folder / "sources").mkdir()
    (folder / "history/original-run").mkdir(parents=True)
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 60), "Public financial report fixture")
        (folder / "sources/public.pdf").write_bytes(document.tobytes())
    contents = {
        "result.json": json.dumps({"run_id": "sealed_run"}),
        "report.md": "Public test report", "metrics.csv": "name,value\nrevenue,12.30\n",
        "trace.jsonl": '{"tool":"run_complete"}\n',
        "history/original-run/result.json": '{"run_id":"original-run"}',
        "history/original-run/trace.jsonl": '{"tool":"model_response","output":{"content":"{}"}}\n',
    }
    for name, content in contents.items():
        (folder / name).write_text(content, encoding="utf-8")
    records = [{"file": path.relative_to(folder).as_posix(), "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in folder.rglob("*") if path.is_file()]
    (folder / "artifacts.json").write_text(json.dumps({"run_id": "sealed_run", "files": records}), encoding="utf-8")
    return folder


def test_archive_preserves_sealed_sources_history_and_original_bytes(sealed_run):
    from finance_agent.provenance import verify_artifacts

    # Unlisted files must not be exposed by a recursive directory zip.
    (sealed_run / "unlisted-private-note.txt").write_text("not part of this run")
    with ZipFile(BytesIO(build_run_archive(sealed_run))) as archive:
        names = set(archive.namelist())
        assert "sealed_run/sources/public.pdf" in names
        assert "sealed_run/history/original-run/trace.jsonl" in names
        assert "sealed_run/artifacts.json" in names
        assert not any("unlisted-private" in name for name in names)
        for name in names:
            assert archive.read(name) == (sealed_run.parent / name).read_bytes()
        restore = sealed_run.parent / "restore"
        archive.extractall(restore)
    assert verify_artifacts(restore / "sealed_run") == []


def test_archive_rejects_tampered_source_before_download(sealed_run):
    (sealed_run / "sources/public.pdf").write_bytes(b"tampered-public-fixture")
    with pytest.raises(CloudError, match="完整性检查失败"):
        build_run_archive(sealed_run)


def test_archive_manifest_cannot_target_files_outside_run(sealed_run):
    outside = sealed_run.parent / "outside.txt"
    outside.write_text("must not leave its directory")
    manifest_path = sealed_run / "artifacts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"file": "../outside.txt", "bytes": outside.stat().st_size,
                              "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CloudError, match="完整性检查失败"):
        build_run_archive(sealed_run)


def test_archive_rejects_unsafe_run_id_even_when_files_are_consistent(sealed_run):
    manifest_path = sealed_run / "artifacts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "../escaped-run"
    result_path = sealed_run / "result.json"
    result_path.write_text(json.dumps({"run_id": manifest["run_id"]}), encoding="utf-8")
    for record in manifest["files"]:
        if record["file"] == "result.json":
            record.update(bytes=result_path.stat().st_size,
                          sha256=hashlib.sha256(result_path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CloudError, match="运行编号不合法"):
        build_run_archive(sealed_run)
