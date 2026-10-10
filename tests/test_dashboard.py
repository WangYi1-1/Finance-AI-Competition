"""展示金额、季度趋势、口径选择和证据联动的数据边界。全部测试不调用 API。"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_agent.analysis import analyze
from finance_agent.dashboard import (
    comparison_chart_rows, filter_result, format_display_money, quarter_series,
    select_amount, signal_detail_rows,
)
from finance_agent.schemas import AnalysisResult, Comparison, Evidence, ExtractedReport, Metric, RunResult, Signal


ROOT = Path(__file__).resolve().parents[1]
COMPANY = "测试公司"
Q3 = date(2024, 9, 30)


def metric(value="100", *, name="revenue", start=date(2024, 1, 1), end=Q3, basis="ytd", evidence="doc:p1", **kwargs):
    return Metric(name=name, company=kwargs.pop("company", COMPANY), value=Decimal(value), raw_value=value,
                  unit=kwargs.pop("unit", "元"), period_start=start, period_end=end, basis=basis,
                  scope=kwargs.pop("scope", "consolidated"), evidence_ids=[evidence], **kwargs)


def report(metrics, *, document_id="doc", company=COMPANY, end=Q3):
    return ExtractedReport(document_id=document_id, document_name=document_id + ".pdf", company=company,
                           report_type="q3", period_end=end, metrics=list(metrics))


def result(reports=(), comparisons=None, signals=(), evidence=None):
    return RunResult(run_id="dashboard-test", mode="demo", model="offline", reports=list(reports),
                     analysis=analyze(list(reports)) if comparisons is None else AnalysisResult(
                         comparisons=list(comparisons), signals=list(signals)),
                     evidence=evidence or {}, run_dir="output/dashboard-test")


def comparison(value="100", *, end=Q3, start=date(2024, 7, 1), base="80", status="ok", **kwargs):
    return Comparison(id=kwargs.pop("id", "qoq"), metric=kwargs.pop("metric", "revenue"),
                      company=kwargs.pop("company", COMPANY), kind=kwargs.pop("kind", "qoq"),
                      period_end=end, current_period_start=start, basis=kwargs.pop("basis", "quarter"),
                      scope=kwargs.pop("scope", "consolidated"),
                      base_period_start=kwargs.pop("base_start", date(2024, 4, 1)),
                      base_period_end=kwargs.pop("base_end", date(2024, 6, 30)), status=status,
                      current_value=Decimal(value) if value is not None else None,
                      base_value=Decimal(base) if base is not None else None,
                      explanation="人工样例比较说明", evidence_ids=kwargs.pop("evidence_ids", ["doc:p1"]))


def signal(category="nonrecurring", **details):
    return Signal(id="test-signal", company=COMPANY, period_end=Q3, category=category,
                  status="detected", severity="watch", title="人工测试", message="人工测试", details=details)


def test_same_date_cumulative_and_quarter_are_distinct_and_missing_is_not_zero():
    cumulative = metric("300")
    quarter = metric("120", start=date(2024, 7, 1), basis="quarter")
    run = result([report([cumulative, quarter])])
    assert select_amount(run, COMPANY, Q3, "revenue", "ytd").value == Decimal("300")
    amount = select_amount(run, COMPANY, Q3, "revenue", "quarter")
    assert amount.value == Decimal("120")
    assert amount.origin == "原文披露单季"
    missing = select_amount(run, COMPANY, Q3, "net_profit", "quarter")
    assert missing.value is None
    assert "累计" in missing.note


def test_ytd_scope_verified_company_and_report_cutoff_filters():
    metrics = [metric("123.45", unit="万元"), metric("999", scope="parent"),
               metric("888", status="unverified"), metric("777", company="别家公司")]
    later_report = report([metric("666")], document_id="later", end=date(2024, 12, 31))
    run = result([report(metrics), later_report])
    assert select_amount(run, COMPANY, Q3, "revenue", "ytd").value == Decimal("123.45")
    # value 已经由分析层统一为元，展示层不能再次按原单位乘一万。
    assert "人民币元" in select_amount(run, COMPANY, Q3, "revenue", "ytd").note


def test_duplicate_same_value_merges_evidence_but_restatement_conflicts_are_not_dropped():
    first = metric("100", evidence="one:p1")
    second = metric("100", evidence="two:p2")
    run = result([report([first], document_id="one"), report([second], document_id="two")])
    chosen = select_amount(run, COMPANY, Q3, "revenue", "ytd")
    assert chosen.value == Decimal("100")
    assert chosen.evidence_ids == ("one:p1", "two:p2")
    run.reports[1].metrics[0].restated = True
    blocked = select_amount(run, COMPANY, Q3, "revenue", "ytd")
    assert blocked.value is None
    assert "追溯调整" in blocked.note
    assert blocked.evidence_ids == chosen.evidence_ids


def test_different_amount_or_start_period_is_a_conflict_and_zero_is_preserved():
    for other in [metric("101"), metric("100", start=date(2024, 4, 1))]:
        run = result([report([metric("100"), other])])
        assert select_amount(run, COMPANY, Q3, "revenue", "ytd").value is None
    assert select_amount(result([report([metric("0")])]), COMPANY, Q3, "revenue", "ytd").value == 0


def test_invalid_period_shapes_are_rejected_for_amount_cards():
    run = result([report([metric("100", start=date(2024, 4, 1))])])
    assert select_amount(run, COMPANY, Q3, "revenue", "ytd").value is None
    run = result([report([metric("200", name="total_assets", start=Q3, basis="point")])])
    assert select_amount(run, COMPANY, Q3, "total_assets", "point").value == Decimal("200")
    assert select_amount(run, COMPANY, Q3, "total_assets", "quarter").value is None


def test_quarter_missing_base_keeps_verified_current_and_incomparable_never_displays_amount():
    run = result(comparisons=[comparison("100", base=None, base_start=None, base_end=None, status="missing")])
    current = select_amount(run, COMPANY, Q3, "revenue", "quarter")
    assert current.value == Decimal("100")
    assert current.origin == "程序核验季度"
    run = result(comparisons=[comparison("100", status="incomparable")])
    assert select_amount(run, COMPANY, Q3, "revenue", "quarter").value is None
    series = quarter_series(run, COMPANY, "revenue", Q3)
    assert [item.value for item in series] == [None]
    assert "不可比" in series[0].note


def test_quarter_series_same_period_conflicts_are_blank_and_retain_all_evidence():
    run = result(comparisons=[comparison("100", evidence_ids=["first:p1"]),
                              comparison("101", id="second", evidence_ids=["second:p3"])])
    series = quarter_series(run, COMPANY, "revenue", Q3)
    assert series[-1].value is None
    assert series[-1].evidence_ids == ("first:p1", "second:p3")
    assert "冲突" in series[-1].note


def test_annual_report_direct_quarters_appear_without_comparison_records():
    quarters = [metric(str(value), start=date(2024, month - 2, 1),
                       end=date(2024, month, day), basis="quarter", evidence=f"annual:p8:q{index}")
                for index, (month, day, value) in enumerate([(3,31,20),(6,30,30),(9,30,40),(12,31,-10)],1)]
    annual = report(quarters + [metric("80",end=date(2024,12,31))],end=date(2024,12,31))
    annual.report_type = "annual"
    run = result([annual],comparisons=[])
    series = quarter_series(run,COMPANY,"revenue",date(2024,12,31))
    assert [item.value for item in series] == [Decimal("20"),Decimal("30"),Decimal("40"),Decimal("-10")]
    assert all(item.origin=="原文披露单季" for item in series)
    assert select_amount(run,COMPANY,date(2024,12,31),"revenue","quarter").value==Decimal("-10")
    assert [item.value for item in quarter_series(run,COMPANY,"revenue",date(2024,6,30))]==[Decimal("20"),Decimal("30")]


def test_direct_quarter_cannot_override_conflicting_comparison_or_restatement():
    direct = metric("100",start=date(2024,7,1),basis="quarter",evidence="raw:p8")
    run = result([report([direct])],comparisons=[comparison("101")])
    assert select_amount(run,COMPANY,Q3,"revenue","quarter").value is None
    run = result([report([direct,direct.model_copy(update={"restated":True,"evidence_ids":["restated:p8"]})])],comparisons=[])
    assert select_amount(run,COMPANY,Q3,"revenue","quarter").value is None


def test_quarter_series_filters_scope_cumulative_other_company_and_future_points():
    run = result(comparisons=[comparison("100"), comparison("999", scope="parent", id="parent"),
                              comparison("888", basis="ytd", id="ytd"),
                              comparison("777", company="别家公司", id="foreign"),
                              comparison("666", end=date(2024, 12, 31), start=date(2024, 10, 1),
                                         base_start=date(2024, 7, 1), base_end=Q3, base="100", id="future")])
    points = quarter_series(run, COMPANY, "revenue", date(2024, 6, 30))
    assert [(point.period_end, point.value) for point in points] == [(date(2024, 6, 30), Decimal("80"))]
    assert len(quarter_series(run, COMPANY, "revenue", Q3)) == 2


def test_quarter_partial_period_and_missing_value_do_not_become_chart_zero():
    run = result(comparisons=[comparison("100", start=date(2024, 8, 1)),
                              comparison(None, id="missing")])
    current = select_amount(run, COMPANY, Q3, "revenue", "quarter")
    assert current.value is None
    assert "完整日历季度" in current.note


def test_filter_result_keeps_cross_period_company_evidence_and_isolated_copies():
    q1 = date(2024, 3, 31)
    evidence = {doc + ":p1": Evidence(id=doc + ":p1", document_id=doc, document_name=doc + ".pdf",
                                     pdf_page=1, text="明确的人工测试证据") for doc in ("q1", "q3", "other")}
    reports = [report([metric("10", end=q1)], document_id="q1", end=q1),
               report([metric("30")], document_id="q3"),
               report([metric("99", company="别家公司")], document_id="other", company="别家公司")]
    run = result(reports, comparisons=[comparison("30"), comparison("99", company="别家公司", id="other")],
                 signals=[signal()], evidence=evidence)
    before = run.model_dump_json()
    view = filter_result(run, COMPANY, Q3)
    assert [r.document_id for r in view.reports] == ["q3"]
    assert set(view.evidence) == {"q1:p1", "q3:p1"}
    assert len(view.analysis.comparisons) == len(view.analysis.signals) == 1
    view.reports[0].metrics[0].value = Decimal("123")
    view.evidence["q1:p1"].text = "展示副本变更"
    assert run.model_dump_json() == before


def test_signal_detail_money_ratio_times_and_negative_base_are_readable():
    nonrec = signal(parent_net_profit="1525319763", absolute_profit_ratio="0.522285", threshold_ratio="0.20")
    rows = {row["项目"]: row["内容"] for row in signal_detail_rows(nonrec)}
    assert rows["归母净利润"] == "15.25 亿元"
    assert rows["非经常性损益／归母利润绝对值"] == "52.23%"
    assert rows["演示关注阈值"] == "20.00%"
    cash = signal("cashflow_divergence", cashflow_profit_ratio="-7.343174", threshold_ratio="0.80",
                  cashflow_yoy_status="negative_base", reason_reviewed=False)
    rows = {row["项目"]: row["内容"] for row in signal_detail_rows(cash)}
    assert rows["经营现金流／合并净利润"] == "-7.3432 倍"
    assert rows["演示关注阈值"] == "0.8000 倍"
    assert "不提供常规增长率" in rows["经营现金流同比状态"]


def test_display_unit_conversion_keeps_sign_precision_and_small_nonzero_values():
    assert format_display_money(Decimal("-1829006223")) == "-18.29 亿元"
    assert format_display_money(Decimal("1525319763"), "万元") == "152,531.98 万元"
    assert format_display_money(Decimal("0")) == "0.00 亿元"
    assert format_display_money(None) == "—"
    assert "非零小额" in format_display_money(Decimal("-1.25"))
    assert "-1.25 元" in format_display_money(Decimal("-1.25"))
    rows = {row["项目"]: row["内容"] for row in signal_detail_rows(signal(reconciliation_tolerance="0.01"))}
    assert rows["金额核对允许误差"] == "0.01 元（核对精度）"


def test_comparison_chart_preserves_signed_amounts_and_never_fills_missing_base_with_zero():
    item = comparison("-1829006223", base="5568444023", status="profit_to_loss", metric="net_profit")
    rows = comparison_chart_rows(item)
    assert [row["金额"] for row in rows] == [55.68444023, -18.29006223]
    assert [row["金额显示"] for row in rows] == ["55.68 亿元", "-18.29 亿元"]
    missing = comparison("22000714536", base=None, status="missing")
    rows = comparison_chart_rows(missing)
    assert len(rows) == 1 and rows[0]["期间"] == "本期"
    assert rows[0]["金额"] == 220.00714536
    assert comparison_chart_rows(item.model_copy(update={"status": "incomparable"})) == []


def test_signal_detail_changes_missing_topics_and_unusable_numbers():
    accounting = signal("accounting_change", reviewed_kinds=["consolidation"],
                        missing_kinds=["accounting_policy", "accounting_estimate", "restatement"],
                        changes=[{"kind": "accounting_policy", "summary": "人工构造政策说明"}])
    rows = {row["项目"]: row["内容"] for row in signal_detail_rows(accounting)}
    assert rows["已核对的披露类型"] == "合并范围"
    assert rows["尚缺的披露类型"] == "会计政策、会计估计、追溯调整"
    assert rows["会计政策披露"] == "人工构造政策说明"
    rows = signal_detail_rows(signal(parent_net_profit="NaN", absolute_profit_ratio=None))
    assert rows[0]["内容"] == "待核实"
    assert rows[1]["内容"] == "暂不适用"


@pytest.fixture(scope="module")
def public_result():
    path = ROOT / "output/20261003T122508Z_public_0b0c7447/result.json"
    if not path.is_file():
        pytest.skip("当前分发包未包含已保存的真实样本结果")
    return RunResult.model_validate_json(path.read_text(encoding="utf-8"))


def test_saved_huahong_result_cumulative_quarter_difference_and_missing_profit(public_result):
    company = next(report.company for report in public_result.reports if "华虹" in report.document_name)
    assert select_amount(public_result, company, Q3, "revenue", "ytd").value == Decimal("10502282877.64")
    assert select_amount(public_result, company, Q3, "revenue", "quarter").value == Decimal("3770124116.54")
    assert select_amount(public_result, company, Q3, "parent_net_profit", "quarter").value == Decimal("313011678.51")
    assert select_amount(public_result, company, Q3, "net_profit", "quarter").value is None
    series = quarter_series(public_result, company, "revenue", Q3)
    assert [item.value for item in series] == [Decimal("3297290706.10"), Decimal("3434868055.00"), Decimal("3770124116.54")]
    assert [item.origin for item in series] == ["第一季度累计与单季等价", "相邻累计差分", "原文披露单季"]
    assert select_amount(public_result, company, date(2024, 6, 30), "net_profit", "ytd").value is None


def test_saved_tcl_profit_scopes_and_inspur_disclosed_reasons(public_result):
    tcl = next(report.company for report in public_result.reports if "TCL" in report.document_name)
    assert select_amount(public_result, tcl, Q3, "parent_net_profit", "ytd").value == Decimal("1525319763")
    assert select_amount(public_result, tcl, Q3, "net_profit", "ytd").value == Decimal("-1829006223")
    inspur = next(report.company for report in public_result.reports if "浪潮" in report.document_name)
    cash = next(item for item in public_result.analysis.signals
                if item.company == inspur and item.category == "cashflow_divergence")
    rows = {row["项目"]: row["内容"] for row in signal_detail_rows(cash)}
    assert rows["经营现金流／合并净利润"] == "-7.3432 倍"
    assert "采购" in rows["公司披露的原因（仍需核对）"]
    assert "不提供常规增长率" in rows["经营现金流同比状态"]
