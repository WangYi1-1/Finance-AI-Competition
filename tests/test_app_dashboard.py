"""Real Streamlit interactions for the dashboard; model calls are forbidden."""
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
import pytest
from streamlit.testing.v1 import AppTest

from finance_agent.config import Settings
from finance_agent.analysis import analyze
from finance_agent.llm import DeepSeekClient
from finance_agent.schemas import Evidence, ExtractedReport, Metric, RunResult


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def saved_yili(monkeypatch):
    """Use isolated local fixtures; never replace the member's real index or call a model."""
    result_root = ROOT / "output/member_yili"
    result_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=result_root, prefix="_ui_test_") as directory:
        run_dir = Path(directory)
        sources = run_dir / "sources"
        sources.mkdir()
        company = "内蒙古伊利实业集团股份有限公司"
        reports, evidence = [], {}
        for year in (2024, 2025):
            with pymupdf.open() as document:
                document.new_page().insert_text((60, 70), f"Yili {year} annual report fixture")
                content = document.tobytes()
            document_id = hashlib.sha256(content).hexdigest()[:16]
            (sources / f"{document_id}.pdf").write_bytes(content)
            identifier = f"{document_id}:p1:table0"
            name = f"伊利{year}年报测试占位.pdf"
            evidence[identifier] = Evidence(id=identifier, document_id=document_id,
                document_name=name, pdf_page=1, kind="table", text="UI test values")
            amounts = {"revenue": 1000000000 if year == 2024 else 1100000000,
                       "parent_net_profit": 100000000 if year == 2024 else 120000000,
                       "net_profit": 90000000 if year == 2024 else 110000000,
                       "operating_cashflow": 200000000 if year == 2024 else 250000000}
            metrics = [Metric(name=key, company=company, value=Decimal(amount), raw_value=str(amount),
                              unit="元", period_start=date(year, 1, 1), period_end=date(year, 12, 31),
                              basis="ytd", scope="consolidated", evidence_ids=[identifier])
                       for key, amount in amounts.items()]
            reports.append(ExtractedReport(document_id=document_id, document_name=name, company=company,
                report_type="annual", period_end=date(year, 12, 31), metrics=metrics))
        result = RunResult(run_id=run_dir.name, mode="live", model="saved-fixture", reports=reports,
                           analysis=analyze(reports), evidence=evidence, run_dir=str(run_dir),
                           usage={"model_calls": 0})
        result_path = run_dir / "result.json"
        result_path.write_text(result.model_dump_json(), encoding="utf-8")
        index = {"run_id": result.run_id, "result_path": result_path.relative_to(ROOT).as_posix()}
        member_index = ROOT / "samples/member/yili/latest_run.json"
        original_read, original_is_file = Path.read_text, Path.is_file

        def read_text(path, *args, **kwargs):
            return json.dumps(index) if path == member_index else original_read(path, *args, **kwargs)

        def is_file(path):
            return True if path == member_index else original_is_file(path)

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(Path, "is_file", is_file)
        yield result, result_path, index


@pytest.fixture
def app(monkeypatch):
    def no_model_call(*args, **kwargs):
        raise AssertionError("Dashboard tests must not call a model")

    monkeypatch.setattr(DeepSeekClient, "complete_json", no_model_call)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, *args, **kwargs: Settings(api_key="")))
    view = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
    assert not view.exception
    return view


def values(app):
    return {item.label: item.value for item in app.metric}


def test_dashboard_opens_saved_real_results_without_api(app):
    assert app.radio(key="run_mode").value == "成果展示"
    assert values(app)["营业收入"] == "105.02"
    assert values(app)["归母净利润"] == "5.78"
    assert values(app)["合并净利润"] == "-3.28"
    assert values(app)["待核实"] == "2"
    assert any("打开与切换结果不调用 API" in item.value for item in app.caption)
    assert app.selectbox(key="showcase_dataset").value == "已有公开样本（6份）"


def test_new_reason_view_and_execution_page_open_without_model(app):
    app.selectbox(key='dashboard_company').set_value('TCL科技集团股份有限公司').run()
    app.radio(key='result_view').set_value('原因与明细').run()
    assert not app.exception
    assert any('合计已核对' in item.value for item in app.markdown)
    app.radio(key='result_view').set_value('执行过程').run()
    assert not app.exception and len(app.dataframe) >= 2
    app.button(key='verify_artifacts').click().run()
    assert not app.exception and any('哈希一致' in item.value for item in app.success)
    app.radio(key='result_view').set_value('报告导出').run()
    buttons=app.get('download_button')
    assert len(buttons)==6


def test_yili_saved_dataset_switch_uses_existing_dashboard_and_resets_period_controls(saved_yili, app):
    app.selectbox(key="dashboard_company").set_value("TCL科技集团股份有限公司").run()
    app.radio(key="result_view").set_value("原文核对").run()
    app.selectbox(key="showcase_dataset").set_value("组员新增样本：伊利2024/2025年报").run()
    assert not app.exception
    assert app.session_state["analysis_result"].run_id == saved_yili[0].run_id
    assert app.selectbox(key="dashboard_company").value == "内蒙古伊利实业集团股份有限公司"
    assert app.selectbox(key="dashboard_period").value == date(2025, 12, 31)
    assert app.radio(key="result_view").value == "分析总览"
    assert values(app)["营业收入"] == "11.00"
    app.radio(key="result_view").set_value("图表分析").run()
    assert not app.exception and len(app.get("vega_lite_chart")) == 2
    app.button(key="profit_chart_evidence").click().run()
    assert not app.exception and app.radio(key="result_view").value == "原文核对"
    assert app.selectbox(key="evidence_document").value == saved_yili[0].reports[1].document_id
    app.selectbox(key="showcase_dataset").set_value("已有公开样本（6份）").run()
    assert not app.exception
    assert app.selectbox(key="dashboard_company").value == "华虹半导体有限公司"


def test_selected_saved_sample_survives_a_fresh_browser_session(saved_yili, app):
    app.selectbox(key="showcase_dataset").set_value("组员新增样本：伊利2024/2025年报").run()
    assert app.query_params["dataset"] == ["yili"]
    refreshed = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25)
    refreshed.query_params.update(app.query_params)
    refreshed.run()
    assert not refreshed.exception
    assert refreshed.selectbox(key="showcase_dataset").value == "组员新增样本：伊利2024/2025年报"
    assert refreshed.session_state["analysis_result"].run_id == saved_yili[0].run_id
    assert refreshed.selectbox(key="dashboard_period").value == date(2025, 12, 31)
    assert values(refreshed)["营业收入"] == "11.00"
    assert any("伊利2024 / 2025年报" in item.value for item in refreshed.markdown)
    refreshed.selectbox(key="showcase_dataset").set_value("已有公开样本（6份）").run()
    assert refreshed.query_params["dataset"] == ["public"]
    assert refreshed.selectbox(key="dashboard_company").value == "华虹半导体有限公司"


def test_unknown_sample_link_uses_public_results(app):
    linked = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25)
    linked.query_params["dataset"] = "unknown"
    linked.run()
    assert not linked.exception
    assert linked.query_params["dataset"] == ["public"]
    assert linked.selectbox(key="showcase_dataset").value == "已有公开样本（6份）"
    assert values(linked)["营业收入"] == "105.02"


def test_missing_yili_index_keeps_requested_sample_and_reports_failure(app, monkeypatch):
    original_read = Path.read_text
    member_index = ROOT / "samples/member/yili/latest_run.json"

    def missing_index(path, *args, **kwargs):
        if path == member_index:
            raise FileNotFoundError("missing test index")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", missing_index)
    linked = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25)
    linked.query_params["dataset"] = "yili"
    linked.run()
    assert not linked.exception
    assert linked.selectbox(key="showcase_dataset").value == "组员新增样本：伊利2024/2025年报"
    assert not linked.metric
    assert any("读取失败" in item.value for item in linked.error)
    assert "analysis_result" not in linked.session_state


def test_yili_load_failure_clears_previous_dataset_instead_of_showing_public_results(saved_yili, app):
    saved_yili[2]["result_path"] = "output/not_the_yili_directory/result.json"
    app.selectbox(key="showcase_dataset").set_value("组员新增样本：伊利2024/2025年报").run()
    assert not app.exception
    assert "analysis_result" not in app.session_state
    assert not app.metric
    assert any("读取失败" in item.value for item in app.error)
    assert app.selectbox(key="showcase_dataset").value == "组员新增样本：伊利2024/2025年报"


@pytest.mark.parametrize("problem", ["mode", "run_id", "company", "metric_company", "period", "type",
                                      "source", "index_type"])
def test_yili_loader_rejects_unrelated_or_incomplete_saved_data(saved_yili, problem):
    from app import load_yili_result
    result, result_path, index = saved_yili
    changed = result.model_copy(deep=True)
    if problem == "mode":
        changed.mode = "demo"
    elif problem == "run_id":
        index["run_id"] = "different-run"
    elif problem == "company":
        changed.reports[0].company = "其他公司"
    elif problem == "metric_company":
        changed.reports[0].metrics[0].company = "其他公司"
    elif problem == "period":
        changed.reports[0].period_end = date(2023, 12, 31)
    elif problem == "type":
        changed.reports[0].report_type = "q3"
    elif problem == "index_type":
        index["result_path"] = 123
    else:
        (result_path.parent / "sources" / f"{changed.reports[0].document_id}.pdf").write_bytes(b"wrong PDF")
    result_path.write_text(changed.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError):
        load_yili_result()


def test_quarter_cards_do_not_borrow_cumulative_profit(app):
    app.radio(key="dashboard_basis").set_value("单季").run()
    assert not app.exception
    assert values(app)["营业收入"] == "37.70"
    assert values(app)["归母净利润"] == "3.13"
    assert values(app)["合并净利润"] == "—"
    assert values(app)["经营活动现金流量净额"] == "-0.33"
    assert any("主要按本报告累计口径" in item.value for item in app.caption)


def test_report_period_switch_preserves_missing_amounts_and_chart_dates(app):
    app.selectbox(key="dashboard_period").set_value(date(2024, 6, 30)).run()
    assert not app.exception
    assert values(app)["营业收入"] == "67.32"
    assert values(app)["合并净利润"] == "—"
    app.radio(key="dashboard_basis").set_value("单季").run()
    assert values(app)["营业收入"] == "34.35"
    assert values(app)["合并净利润"] == "—"


def test_tcl_keeps_parent_and_consolidated_profit_separate(app):
    app.selectbox(key="dashboard_company").set_value("TCL科技集团股份有限公司").run()
    assert not app.exception
    assert values(app)["归母净利润"] == "15.25"
    assert values(app)["合并净利润"] == "-18.29"
    assert any("比例暂不适用" in item.value for item in app.markdown)


def test_tcl_comparison_restores_cashflow_base_and_labels_profit_loss_transition(app):
    app.selectbox(key="dashboard_company").set_value("TCL科技集团股份有限公司").run()
    app.radio(key="result_view").set_value("期间比较").run()
    assert not app.exception
    rows = app.dataframe[0].value
    profit = rows[(rows["指标"] == "合并净利润") & (rows["期间类型"] == "年初至今累计")].iloc[0]
    assert profit["本期金额（亿元）"] == "-18.29"
    assert profit["基期金额（亿元）"] == "55.68"
    assert profit["增减金额（亿元）"] == "-73.97"
    assert profit["状态"] == "不适用（由盈转亏）" and profit["变化率"] == "—"
    cash = rows[(rows["指标"] == "经营活动现金流量净额") & (rows["比较方式"] == "同比")].iloc[0]
    assert cash["本期金额（亿元）"] == "220.01" and cash["基期金额（亿元）"] == "161.44"
    assert cash["变化率"] == "36.28%"
    missing = rows[(rows["指标"] == "扣非归母净利润") & (rows["比较方式"] == "同比")].iloc[0]
    assert "2023年第三季度报告" in missing["补充材料建议"]
    assert "比较列" in missing["补充材料建议"]


def test_visualizations_are_rendered_and_link_back_to_profit_source(app):
    app.selectbox(key="dashboard_company").set_value("TCL科技集团股份有限公司").run()
    app.radio(key="result_view").set_value("图表分析").run()
    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 2
    assert any("36.28%" in item.value for item in app.markdown)
    frames = [item.value.to_string() for item in app.dataframe]
    assert any("15.25" in item and "-18.29" in item for item in frames)
    app.button(key="profit_chart_evidence").click().run()
    assert not app.exception and app.radio(key="result_view").value == "原文核对"
    evidence = app.session_state["analysis_result"].evidence[app.selectbox(key="evidence_reference").value]
    assert "TCL" in evidence.document_name and evidence.pdf_page == 16


def test_cashflow_comparison_source_prioritizes_the_table_with_comparable_base(app):
    app.selectbox(key="dashboard_company").set_value("TCL科技集团股份有限公司").run()
    app.radio(key="result_view").set_value("图表分析").run()
    app.button(key="comparison_chart_evidence").click().run()
    assert not app.exception and app.radio(key="result_view").value == "原文核对"
    evidence = app.session_state["analysis_result"].evidence[app.selectbox(key="evidence_reference").value]
    assert "TCL" in evidence.document_name and evidence.pdf_page == 17


def test_selected_display_unit_applies_to_comparison_and_metric_details_without_mutating_amounts(app):
    app.selectbox(key="dashboard_company").set_value("TCL科技集团股份有限公司").run()
    original = app.session_state["analysis_result"].model_dump_json()
    app.selectbox(key="dashboard_unit").set_value("万元").run()
    app.radio(key="result_view").set_value("期间比较").run()
    assert not app.exception
    rows = app.dataframe[0].value
    profit = rows[(rows["指标"] == "合并净利润") & (rows["期间类型"] == "年初至今累计")].iloc[0]
    assert profit["本期金额（万元）"] == "-182,900.62"
    app.radio(key="result_view").set_value("财务明细").run()
    assert "金额（万元）" in app.dataframe[0].value.columns
    assert "原始数值" not in app.dataframe[0].value.columns
    app.checkbox(key="show_raw_metrics").check().run()
    assert "精确金额（元）" in app.dataframe[0].value.columns
    assert app.session_state["analysis_result"].model_dump_json() == original


def test_quarter_visualization_does_not_replace_missing_profit_with_cumulative(app):
    app.radio(key="dashboard_basis").set_value("单季").run()
    app.radio(key="result_view").set_value("图表分析").run()
    assert not app.exception
    assert any("未绘制：合并净利润" in item.value for item in app.caption)
    table = app.dataframe[0].value
    assert table[table["指标"] == "合并净利润"].iloc[0]["金额（亿元）"] == "—"


def test_amount_click_opens_matching_pdf_and_company_switch_clears_focus(app):
    app.button(key="amount_source_revenue").click().run()
    assert not app.exception
    assert app.radio(key="result_view").value == "原文核对"
    selected = app.session_state["analysis_result"].evidence[app.selectbox(key="evidence_reference").value]
    assert selected.document_id == app.selectbox(key="evidence_document").value
    assert selected.pdf_page >= 1
    assert any("橙色框标出引用区域" in item.value for item in app.caption)
    app.selectbox(key="dashboard_company").set_value("浪潮电子信息产业股份有限公司").run()
    assert not app.exception
    assert app.radio(key="result_view").value == "分析总览"
    assert values(app)["合并净利润"] == "12.78"


def test_signal_detail_and_comparison_evidence_are_readable(app):
    app.selectbox(key="dashboard_company").set_value("浪潮电子信息产业股份有限公司").run()
    app.radio(key="result_view").set_value("关注信号").run()
    assert not app.exception
    frames = [item.value.to_string() for item in app.dataframe]
    assert any("-7.3432 倍" in frame for frame in frames)
    assert any("采购" in frame for frame in frames)
    app.radio(key="result_view").set_value("期间比较").run()
    assert not app.exception
    app.button(key="comparison_evidence").click().run()
    assert not app.exception
    assert app.radio(key="result_view").value == "原文核对"


def test_sidebar_mode_does_not_mislabel_loaded_results_and_demo_runs(app):
    app.radio(key="run_mode").set_value("演示模式").run()
    assert not app.exception
    assert any("打开与切换结果不调用 API" in item.value for item in app.caption)
    next(button for button in app.button if button.label == "运行演示").click().run(timeout=30)
    assert not app.exception
    assert app.session_state["analysis_result"].mode == "demo"
    assert any("当前是虚构数据" in item.value for item in app.caption)
    app.radio(key="run_mode").set_value("成果展示").run()
    assert app.session_state["analysis_result"].mode == "live"
    assert app.session_state["result_origin"] == "saved"
    app.button(key="reload_public").click().run()
    assert not app.exception
    assert app.selectbox(key="dashboard_company").value == "华虹半导体有限公司"


def test_pdf_highlight_does_not_change_original_file(tmp_path):
    from app import render_pdf_page
    path = tmp_path / "source.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 70), "Revenue 123")
        doc.save(path)
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    original = render_pdf_page(str(path), 1, digest)
    highlighted = render_pdf_page(str(path), 1, digest, (50, 50, 180, 90))
    assert highlighted != original
    assert path.read_bytes() == before
