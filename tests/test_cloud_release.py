"""发布选择与校验使用合成文件，不访问 API 或开发者配置。"""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from scripts import prepare_cloud_release as release


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def source_fixture(root: Path, label: str, manifest_name: str, pointer_name: str, output_prefix: str):
    content = f"%PDF-1.4\nSynthetic public report {label}\n%%EOF\n".encode()
    full_hash = hashlib.sha256(content).hexdigest()
    document_id = full_hash[:16]
    sample_dir = root / Path(manifest_name).parent
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample = {"file": f"{label}.pdf", "sha256": full_hash}
    (sample_dir / sample["file"]).write_bytes(content)
    write_json(root / manifest_name, [sample] if label == "yili" else {"samples": [sample]})
    run_id = f"latest_{label}"
    run = root / output_prefix / run_id
    (run / "sources").mkdir(parents=True)
    (run / "sources" / f"{document_id}.pdf").write_bytes(content)
    write_json(run / "result.json", {"run_id": run_id, "mode": "live",
               "reports": [{"document_id": document_id}],
               "provenance": {"sources": [{"document_id": document_id, "sha256": full_hash}]}})
    for name in ("report.md", "metrics.csv", "trace.jsonl"):
        (run / name).write_text("Synthetic public data\n", encoding="utf-8")
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    (run / "analysis.xlsx").write_bytes(workbook.getvalue())
    (run / "history").mkdir()
    (run / "history" / "original_trace.jsonl").write_text('{"event":"model_response","synthetic":true}\n')
    seal_fixture(run, run_id)
    write_json(root / pointer_name, {"run_id": run_id, "result_path": f"{output_prefix}/{run_id}/result.json"})
    return run


def seal_fixture(folder, run_id):
    files = [{"file": path.relative_to(folder).as_posix(), "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in folder.rglob("*") if path.is_file() and path.name != "artifacts.json"]
    write_json(folder / "artifacts.json", {"schema_version": 1, "run_id": run_id, "files": files})


@pytest.fixture
def project(tmp_path):
    for name in release.ROOT_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Synthetic project content\n", encoding="utf-8")
    (tmp_path / ".streamlit/secrets.example.toml").write_text('DEEPSEEK_API_KEY = ""\nTEAM_ACCESS_CODE = ""\n')
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    (tmp_path / "finance_agent").mkdir()
    (tmp_path / "finance_agent/module.py").write_text("VALUE = 1\n")
    runs = {label: source_fixture(tmp_path, label, manifest, pointer, prefix)
            for label, manifest, pointer, prefix in release.DATASETS}
    return tmp_path, runs


def test_release_copies_only_allowlisted_code_and_latest_sealed_showcases(project, monkeypatch):
    root, runs = project
    private = [".env", ".streamlit/secrets.toml", ".venv/python", "uploads/private.pdf", "tmp/private.txt",
               "output/old_run/result.json", "samples/public/unlisted.pdf"]
    for name in private:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Do not read or publish\n")
    original_read = Path.read_bytes

    def guarded_read(path):
        if path in {root / name for name in private}:
            pytest.fail("Release attempted to read an excluded file")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    folder = release.prepare(root)
    assert release.verify_snapshot(folder) == []
    assert not any((folder / name).exists() for name in private)
    assert (folder / "finance_agent/module.py").is_file()
    assert (folder / "cloud_app.py").is_file()
    for run in runs.values():
        assert (folder / run.relative_to(root) / "history/original_trace.jsonl").is_file()
    snapshot = release.read_json(folder, "cloud_snapshot.json")
    assert set(snapshot["showcase"]) == {"public", "yili"}
    assert snapshot["status"] == "ready_for_review"


def test_repeat_release_never_overwrites_a_previous_reviewable_copy(project):
    root, _ = project
    first = release.prepare(root)
    original = (first / "app.py").read_bytes()
    (root / "app.py").write_text("# next revision\n")
    second = release.prepare(root)
    assert first != second
    assert (first / "app.py").read_bytes() == original
    assert (second / "app.py").read_text() == "# next revision\n"


def test_pointer_cannot_include_a_different_run_or_external_path(project):
    root, _ = project
    pointer = root / "samples/public/latest_verified_run.json"
    value = json.loads(pointer.read_text())
    value["result_path"] = "output/old_run/result.json"
    write_json(pointer, value)
    with pytest.raises(ValueError, match="对应运行目录"):
        release.prepare(root)


@pytest.mark.parametrize("which", ["trace.jsonl", "history/original_trace.jsonl"])
def test_changed_sealed_core_or_history_fails_before_copy(project, which):
    root, runs = project
    (runs["public"] / which).write_text("changed\n")
    with pytest.raises(ValueError, match="封存文件校验失败"):
        release.prepare(root)
    assert not (root / "deploy/releases").exists()


def test_saved_pdf_full_hash_must_match_original_manifest_even_if_resigned(project):
    root, runs = project
    run = runs["public"]
    pdf = next((run / "sources").glob("*.pdf"))
    pdf.write_bytes(b"%PDF-1.4\nsubstituted\n%%EOF\n")
    seal_fixture(run, "latest_public")
    with pytest.raises(ValueError, match="完整来源哈希不符"):
        release.prepare(root)


def test_source_provenance_hash_cannot_be_missing(project):
    root, runs = project
    run = runs["yili"]
    result = json.loads((run / "result.json").read_text())
    result["provenance"]["sources"][0]["sha256"] = "0" * 64
    write_json(run / "result.json", result)
    seal_fixture(run, "latest_yili")
    with pytest.raises(ValueError, match="完整来源哈希"):
        release.prepare(root)


def test_secret_scan_rejects_but_does_not_echo_a_key(project):
    root, runs = project
    key = "sk-" + "x" * 32
    run = runs["public"]
    (run / "report.md").write_text(key)
    seal_fixture(run, "latest_public")
    with pytest.raises(ValueError, match="疑似包含密钥") as caught:
        release.prepare(root)
    assert key not in str(caught.value)
    assert not (root / "deploy/releases").exists()


def test_filled_secret_example_is_never_published(project):
    root, _ = project
    (root / ".streamlit/secrets.example.toml").write_text('TEAM_ACCESS_CODE = "not-empty"\n')
    with pytest.raises(ValueError, match="必须为空"):
        release.prepare(root)


def test_nonstandard_key_in_local_example_is_rejected_without_echo(project):
    root, _ = project
    fake = "plain-credential-without-provider-prefix"
    (root / ".env.example").write_text(f"DEEPSEEK_API_KEY={fake}\n")
    with pytest.raises(ValueError, match="必须为空") as caught:
        release.prepare(root)
    assert fake not in str(caught.value)


def test_blank_local_key_is_not_confused_with_the_next_configuration_line():
    release.check_content(".env.example", b"DEEPSEEK_API_KEY=\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n")


def test_github_file_limit_is_checked_before_reading(tmp_path, monkeypatch):
    file = tmp_path / "oversize.py"
    file.write_bytes(b"A" * 16)
    monkeypatch.setattr(release, "MAX_GITHUB_FILE_BYTES", 15)
    with pytest.raises(ValueError, match="单文件大小限制"):
        release.read_file(tmp_path, "oversize.py")


def test_verification_detects_additional_private_files_without_reading_them(project, monkeypatch):
    root, _ = project
    folder = release.prepare(root)
    private = folder / ".env"
    private.write_text("Do not read\n")
    original_read = Path.read_bytes

    def guarded_read(path):
        if path == private:
            pytest.fail("Verification attempted to read an unlisted secret")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    assert any("未列出的文件" in error for error in release.verify_snapshot(folder))


def test_repo_metadata_can_be_added_without_changing_the_publishable_snapshot(project):
    root, _ = project
    folder = release.prepare(root)
    metadata = folder / ".git/config"
    metadata.parent.mkdir()
    metadata.write_text("[core]\nrepositoryformatversion = 0\n")
    assert release.verify_snapshot(folder) == []


@pytest.mark.parametrize("name", ["../private.json", "sources/../result.json", "C:/private.json",
                                  "sources\\private.pdf", ".env", "history/.env", "history/secrets.toml"])
def test_artifact_paths_never_escape_or_pick_private_files(name):
    with pytest.raises(ValueError):
        release.allowed_artifact(name)


def test_history_allowlist_does_not_accept_unrelated_data_just_because_it_was_sealed():
    assert not release.allowed_artifact("history/secrets.json")
    assert not release.allowed_artifact("history/private_notes.json")
    assert release.allowed_artifact("history/old_run/trace.jsonl")


def test_gitignore_permits_selected_showcases_but_not_arbitrary_new_output(project):
    root, _ = project
    folder = release.prepare(root)
    rules = (folder / ".gitignore").read_text(encoding="utf-8")
    assert "/output/*\n!/output/latest_public/" in rules
    assert "/output/member_yili/*\n!/output/member_yili/latest_yili/" in rules
    assert ".streamlit/secrets.toml" in rules
