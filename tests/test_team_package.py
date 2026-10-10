"""保存结果须跟随最新指针，并校验来源身份，不能借指针带入任意目录。"""
import hashlib
import json

import pytest

from scripts import build_team_package


def set_pointer(tmp_path, run_id="20261009T000000Z_public_verified", *, result_path=None, mode="live"):
    pointer_dir = tmp_path / "samples/public"
    pointer_dir.mkdir(parents=True)
    pointer = {"run_id": run_id, "result_path": result_path or f"output/{run_id}/result.json"}
    (pointer_dir / "latest_verified_run.json").write_text(json.dumps(pointer), encoding="utf-8")
    result_dir = tmp_path / "output" / run_id
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(json.dumps({"run_id": run_id, "mode": mode}), encoding="utf-8")
    return result_dir


def test_latest_run_is_included_without_hardcoding_it(tmp_path, monkeypatch):
    latest = set_pointer(tmp_path)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    included = build_team_package.public_run_paths()
    assert latest in included
    assert len(included) == len(build_team_package.PUBLIC_RUNS) + 1


def test_pointer_path_outside_matching_run_is_rejected(tmp_path, monkeypatch):
    set_pointer(tmp_path, result_path="../private/result.json")
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="对应output"):
        build_team_package.public_run_paths()


def test_fictional_result_cannot_be_packaged_as_public_replay(tmp_path, monkeypatch):
    set_pointer(tmp_path, mode="demo")
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="真实分析模式"):
        build_team_package.public_run_paths()


def test_missing_current_result_fails_instead_of_packaging_old_fallback(tmp_path, monkeypatch):
    latest = set_pointer(tmp_path)
    (latest / "result.json").unlink()
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="最新公开样本结果不存在"):
        build_team_package.public_run_paths()


def set_yili_pointer(tmp_path, run_id="20261009T060012Z_revalidated_verified"):
    """仅用合成来源字节测试打包边界，不解析PDF或读取真实配置。"""
    pointer_dir = tmp_path / "samples/member/yili"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    result_dir = tmp_path / "output/member_yili" / run_id
    source_dir = result_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for year in (2024, 2025):
        content = f"%PDF-1.4\nSynthetic public annual source {year}\n%%EOF\n".encode("ascii")
        document_id = hashlib.sha256(content).hexdigest()[:16]
        (source_dir / f"{document_id}.pdf").write_bytes(content)
        reports.append({
            "document_id": document_id,
            "filename": f"yili_{year}.pdf",
            "company": "内蒙古伊利实业集团股份有限公司",
            "report_type": "annual",
            "period_end": f"{year}-12-31",
        })
    result = {"run_id": run_id, "mode": "live", "reports": reports}
    result_path = result_dir / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    pointer = {"run_id": run_id, "result_path": f"output/member_yili/{run_id}/result.json"}
    (pointer_dir / "latest_run.json").write_text(json.dumps(pointer), encoding="utf-8")
    return result_dir, result, pointer


def write_yili_result(result_dir, result):
    (result_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def write_yili_pointer(tmp_path, pointer):
    (tmp_path / "samples/member/yili/latest_run.json").write_text(json.dumps(pointer), encoding="utf-8")


def test_yili_uses_only_current_pointer_instead_of_hardcoded_or_previous_run(tmp_path, monkeypatch):
    previous, _, _ = set_yili_pointer(tmp_path, "20261008T000000Z_previous")
    current, _, _ = set_yili_pointer(tmp_path, "20261009T999999Z_latest")
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    assert build_team_package.yili_run_paths() == [current]
    assert previous not in build_team_package.yili_run_paths()


@pytest.mark.parametrize("path", [
    "../private/result.json",
    "output/private/result.json",
    "output/member_yili/other_run/result.json",
    "output/member_yili/20261009T060012Z_revalidated_verified/../private/result.json",
    "C:/private/result.json",
])
def test_yili_rejects_pointer_paths_outside_exact_matching_run(tmp_path, monkeypatch, path):
    _, _, pointer = set_yili_pointer(tmp_path)
    pointer["result_path"] = path
    write_yili_pointer(tmp_path, pointer)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()


@pytest.mark.parametrize("run_id", ["", "../private", "2026/private", None])
def test_yili_rejects_unsafe_run_identifiers(tmp_path, monkeypatch, run_id):
    _, _, pointer = set_yili_pointer(tmp_path)
    pointer["run_id"] = run_id
    write_yili_pointer(tmp_path, pointer)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()


@pytest.mark.parametrize(("field", "value"), [("mode", "demo"), ("mode", None), ("run_id", "other_run")])
def test_yili_rejects_fictional_or_mismatched_result(tmp_path, monkeypatch, field, value):
    result_dir, result, _ = set_yili_pointer(tmp_path)
    result[field] = value
    write_yili_result(result_dir, result)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()


@pytest.mark.parametrize(("field", "value"), [
    ("report_type", "q3"),
    ("period_end", "2023-12-31"),
    ("period_end", "2024-09-30"),
    ("company", "其他公司"),
    ("document_id", "z" * 16),
    ("document_id", "a" * 15),
])
def test_yili_rejects_wrong_report_identity(tmp_path, monkeypatch, field, value):
    result_dir, result, _ = set_yili_pointer(tmp_path)
    result["reports"][0][field] = value
    write_yili_result(result_dir, result)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()


@pytest.mark.parametrize("duplicate", ["period_end", "document_id"])
def test_yili_requires_distinct_years_and_source_documents(tmp_path, monkeypatch, duplicate):
    result_dir, result, _ = set_yili_pointer(tmp_path)
    result["reports"][1][duplicate] = result["reports"][0][duplicate]
    write_yili_result(result_dir, result)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()


def test_yili_rejects_changed_source_bytes(tmp_path, monkeypatch):
    result_dir, result, _ = set_yili_pointer(tmp_path)
    source_path = result_dir / "sources" / f"{result['reports'][0]['document_id']}.pdf"
    source_path.write_bytes(b"%PDF-1.4\nReplaced source\n%%EOF\n")
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()


@pytest.mark.parametrize("missing", ["result", "source", "report"])
def test_yili_missing_result_source_or_second_report_fails(tmp_path, monkeypatch, missing):
    result_dir, result, _ = set_yili_pointer(tmp_path)
    if missing == "result":
        (result_dir / "result.json").unlink()
    elif missing == "source":
        (result_dir / "sources" / f"{result['reports'][1]['document_id']}.pdf").unlink()
    else:
        result["reports"].pop()
        write_yili_result(result_dir, result)
    monkeypatch.setattr(build_team_package, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_team_package.yili_run_paths()
