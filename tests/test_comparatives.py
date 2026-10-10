"""公开披露比较列的回归测试，预期金额由人工固定。"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_agent.analysis import analyze
from finance_agent.comparatives import supplement_comparatives
from finance_agent.extraction import validate_payload
from finance_agent.parser import parse_financial_doc
from finance_agent.schemas import Evidence, ExtractedReport, Metric, ParsedDocument


COMPANY = "测试制造股份有限公司"


def fixture(headers=("2023年前三季度（1-9月）", "2022年前三季度（1-9月）"),
            values=("200.00", "100.00"), *, unit="万元", name="revenue",
            label="营业收入", basis="ytd", start="2023-01-01", restated=False,
            end="2023-09-30", report_type="q3"):
    cells = [["项目", *headers], [label, *values]]
    table = Evidence(id="doc:p2:table0", document_id="doc", document_name="测试季报.pdf",
                     pdf_page=2, kind="table", cells=cells,
                     text="\n".join(" | ".join(row) for row in cells),
                     context_evidence_ids=["title", "unit"])
    blocks = [Evidence(id="meta", document_id="doc", document_name=table.document_name,
                       pdf_page=1, text=COMPANY + " 2023年第三季度报告，2022年可比金额。"),
              Evidence(id="title", document_id="doc", document_name=table.document_name,
                       pdf_page=2, text="合并资产负债表" if basis == "point" else "合并利润表"),
              Evidence(id="unit", document_id="doc", document_name=table.document_name,
                       pdf_page=2, text=f"单位：{unit} 币种：人民币"), table]
    doc = ParsedDocument(id="doc", name=table.document_name, sha256="0" * 64, page_count=2, blocks=blocks)
    item = dict(name=name, company=COMPANY, value="0", raw_value=values[0], unit=unit,
                period_start=start, period_end=end, basis=basis, scope="consolidated",
                restated=restated, evidence_ids=[table.id], row_quote=" | ".join(cells[1]),
                source_row_index=1, source_column_index=1)
    report = validate_payload(dict(company=COMPANY, report_type=report_type, period_end=end,
                                   metrics=[item]), doc, blocks)
    assert len(report.metrics) == 1
    return report, doc


def previous(report):
    return [metric for metric in report.metrics if metric.period_end.year == 2022]


def test_explicit_same_period_amount_is_added_and_converted_to_yuan():
    report, doc = fixture()
    result = supplement_comparatives(report, doc)
    prior = previous(result)
    assert len(prior) == 1
    assert prior[0].value == Decimal("1000000")
    assert prior[0].period_start == date(2022, 1, 1)
    assert prior[0].period_end == date(2022, 9, 30)
    assert prior[0].source_row_index == 1 and prior[0].source_column_index == 2
    assert prior[0].status == "verified"
    assert len(report.metrics) == 1, "输入报告不被修改"
    comparison = next(item for item in analyze([result]).comparisons if item.kind == "yoy")
    assert comparison.status == "ok" and comparison.percent == Decimal("100")


@pytest.mark.parametrize("unit, factor", [("元", 1), ("千元", 1000), ("万元", 10000), ("百万元", 1000000)])
def test_comparative_amount_uses_its_table_unit(unit, factor):
    report, doc = fixture(unit=unit)
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].value == Decimal(100 * factor)


def test_disclosed_percentage_is_not_back_solved_into_prior_amount():
    report, doc = fixture(headers=("年初至报告期末", "年初至报告期末比上年同期增减（%）"),
                          values=("200.00", "100.00"))
    result = supplement_comparatives(report, doc)
    assert result.metrics == report.metrics
    assert analyze([result]).comparisons[0].status == "missing"


def test_current_amount_must_match_verified_anchor():
    report, doc = fixture()
    report.metrics[0].value = Decimal("3000000")
    result = supplement_comparatives(report, doc)
    assert not previous(result)
    assert any("本期金额" in warning for warning in result.warnings)


def test_current_and_prior_different_length_periods_do_not_pair():
    report, doc = fixture(headers=("2023年前三季度（1-9月）", "2022年第三季度（7-9月）"))
    assert not previous(supplement_comparatives(report, doc))


def test_previous_year_end_is_preserved_as_point_and_never_used_as_yoy():
    report, doc = fixture(headers=("2023年9月30日", "2022年12月31日"), name="total_assets",
                          label="资产总计", basis="point", start="2023-09-30")
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].period_start == date(2022, 12, 31)
    assert previous(result)[0].period_end == date(2022, 12, 31)
    assert all(item.status == "missing" for item in analyze([result]).comparisons)


def test_relative_prior_year_end_is_not_relabelled_same_quarter_end():
    report, doc = fixture(headers=("本报告期末", "上年度末"), name="total_assets",
                          label="资产总计", basis="point", start="2023-09-30")
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].period_end == date(2022, 12, 31)


@pytest.mark.parametrize("header", ["上年期末", "2022年期末", "2022年末"])
def test_prior_annual_end_does_not_become_previous_september(header):
    report, doc = fixture(headers=("本报告期末", header), name="total_assets",
                          label="资产总计", basis="point", start="2023-09-30")
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].period_end == date(2022, 12, 31)
    assert all(item.status == "missing" for item in analyze([result]).comparisons)


def test_ambiguous_previous_point_period_is_not_guessed():
    report, doc = fixture(headers=("本报告期末", "上期末"), name="total_assets",
                          label="资产总计", basis="point", start="2023-09-30")
    assert not previous(supplement_comparatives(report, doc))


def test_full_date_range_binds_start_and_end():
    report, doc = fixture(headers=("2023-01-01至2023-09-30", "2022-01-01至2022-06-30"))
    result = supplement_comparatives(report, doc)
    # 金额可保留为其实际披露期间；不会被重标为上年九个月累计。
    assert previous(result)[0].period_end == date(2022, 6, 30)
    assert analyze([result]).comparisons[0].status == "missing"


def test_bare_prior_year_does_not_prove_period():
    report, doc = fixture(headers=("2023年前三季度（1-9月）", "2022年"))
    assert not previous(supplement_comparatives(report, doc))


@pytest.mark.parametrize("header", ["上期", "上年"])
def test_ambiguous_relative_period_in_quarterly_report_is_not_invented(header):
    report, doc = fixture(headers=("年初至报告期末", header))
    assert not previous(supplement_comparatives(report, doc))


def test_explicit_relative_same_period_is_supported():
    report, doc = fixture(headers=("年初至报告期末", "上年同期"))
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].period_start == date(2022, 1, 1)
    assert previous(result)[0].period_end == date(2022, 9, 30)


def test_previous_occurrence_amount_in_bound_cumulative_statement_is_yoy():
    report, doc = fixture(headers=("本期发生额", "上期发生额"))
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].period_start == date(2022, 1, 1)
    assert previous(result)[0].period_end == date(2022, 9, 30)
    assert analyze([result]).comparisons[0].status == "ok"


def cashflow_statement_fixture(title="合并年初到报告期末现金流量表"):
    """摘要仅有本期值/增长率；另一张有完整比较列的现金流量表。"""
    report, doc = fixture(
        headers=("年初至报告期末", "年初至报告期末比上年同期增减（%）"),
        name="operating_cashflow", label="经营活动产生的现金流量净额")
    next(block for block in doc.blocks if block.id == "title").text = "主要财务数据"
    statement_title = Evidence(id="cashflow-title", document_id=doc.id,
        document_name=doc.name, pdf_page=3, text=title)
    statement_unit = Evidence(id="cashflow-unit", document_id=doc.id,
        document_name=doc.name, pdf_page=3, text="单位：万元 币种：人民币")
    cells = [["项目", "本期发生额", "上期发生额"],
             ["经营活动产生的现金流量净额", "200.00", "100.00"]]
    table = Evidence(id="doc:p3:table0", document_id=doc.id,
        document_name=doc.name, pdf_page=3, kind="table", cells=cells,
        text="\n".join(" | ".join(row) for row in cells),
        context_evidence_ids=[statement_title.id, statement_unit.id])
    doc.blocks.extend([statement_title, statement_unit, table])
    doc.page_count = 3
    return report, doc


@pytest.mark.parametrize("title", ["合并现金流量表", "合并年初到报告期末现金流量表",
                                  "合并年初至报告期末现金流量表"])
def test_cumulative_cashflow_statement_can_supply_summary_missing_comparative(title):
    report, doc = cashflow_statement_fixture(title)
    result = supplement_comparatives(report, doc)
    prior = previous(result)
    assert len(prior) == 1 and prior[0].value == Decimal("1000000")
    assert prior[0].basis == "ytd" and prior[0].scope == "consolidated"
    assert (prior[0].period_start, prior[0].period_end) == (date(2022, 1, 1), date(2022, 9, 30))
    assert prior[0].source_row_index == 1 and prior[0].source_column_index == 2
    assert "doc:p3:table0" in prior[0].evidence_ids and "cashflow-title" in prior[0].evidence_ids
    assert prior[0].status == "verified"
    assert supplement_comparatives(result, doc).model_dump() == result.model_dump()


@pytest.mark.parametrize("problem", ["parent_statement", "ambiguous_scope", "missing_scope",
                                      "current_mismatch", "wrong_unit", "wrong_period", "wrong_row"])
def test_cashflow_comparative_refuses_unproven_anchor_unit_period_or_scope(problem):
    report, doc = cashflow_statement_fixture()
    title = next(block for block in doc.blocks if block.id == "cashflow-title")
    table = doc.blocks[-1]
    if problem == "parent_statement":
        title.text = "母公司年初到报告期末现金流量表"
    elif problem == "ambiguous_scope":
        title.text += "；母公司现金流量表"
    elif problem == "missing_scope":
        title.text = "现金流量情况说明，合并范围发生变化"
    elif problem == "current_mismatch":
        table.cells[1][1] = "201.00"
    elif problem == "wrong_unit":
        next(block for block in doc.blocks if block.id == "cashflow-unit").text = "单位：元 币种：人民币"
    elif problem == "wrong_period":
        table.cells[0] = ["项目", "2023年第三季度", "2022年第三季度"]
    else:
        table.cells[1][0] = "投资活动产生的现金流量净额"
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    assert not previous(supplement_comparatives(report, doc))


@pytest.mark.parametrize("headers", [("2023年度", "2022年度"), ("本年", "上年")])
def test_annual_comparative_columns_keep_full_year_period(headers):
    annual, doc = fixture(headers=headers, end="2023-12-31", report_type="annual")
    result = supplement_comparatives(annual, doc)
    assert previous(result)[0].period_end == date(2022, 12, 31)
    assert analyze([result]).comparisons[0].status == "ok"


def test_annual_year_only_column_requires_cover_and_statement_context():
    annual, doc = fixture(headers=("2023年度", "2022年"), end="2023-12-31", report_type="annual")
    next(block for block in doc.blocks if block.id == "meta").text = COMPANY + " 2023年年度报告"
    result = supplement_comparatives(annual, doc)
    assert previous(result)[0].period_start == date(2022, 1, 1)
    assert previous(result)[0].period_end == date(2022, 12, 31)
    assert previous(result)[0].value == Decimal("1000000")


@pytest.mark.parametrize("problem", ["quarter_cover", "wrong_cover_year", "narrative_title"])
def test_annual_year_only_column_does_not_guess_from_metadata_or_policy(problem):
    annual, doc = fixture(headers=("2023年度", "2022年"), end="2023-12-31", report_type="annual")
    meta = next(block for block in doc.blocks if block.id == "meta")
    meta.text = COMPANY + " 2023年年度报告"
    if problem == "quarter_cover":
        meta.text = COMPANY + " 2023年第三季度报告"
    elif problem == "wrong_cover_year":
        meta.text = COMPANY + " 2024年年度报告"
    else:
        next(block for block in doc.blocks if block.id == "title").text = "本集团会计政策要求编制合并利润表。"
    assert not previous(supplement_comparatives(annual, doc))


@pytest.mark.parametrize("header, expected", [("2022年前三季度（调整前）", False),
                                               ("2022年前三季度（调整后）", True)])
def test_explicit_restatement_header_controls_prior_flag(header, expected):
    report, doc = fixture(headers=("2023年前三季度", header), restated=True)
    result = supplement_comparatives(report, doc)
    assert previous(result)[0].restated is expected


def test_restatement_flag_is_inherited_without_adjustment_header():
    report, doc = fixture(restated=True)
    assert previous(supplement_comparatives(report, doc))[0].restated is True


def test_ambiguous_restatement_column_is_not_added():
    report, doc = fixture(headers=("2023年前三季度", "2022年前三季度（追溯调整）"))
    result = supplement_comparatives(report, doc)
    assert not previous(result)
    assert any("追溯调整口径" in warning for warning in result.warnings)


def test_supplement_is_idempotent_and_existing_metrics_are_preserved():
    report, doc = fixture()
    first = supplement_comparatives(report, doc)
    second = supplement_comparatives(first, doc)
    assert second.model_dump() == first.model_dump()
    assert second.metrics[0] == report.metrics[0]


def test_conflicting_comparative_tables_are_not_selected():
    report, doc = fixture()
    other = doc.blocks[-1].model_copy(deep=True)
    other.id = "doc:p2:table1"
    other.cells[1][2] = "99.00"
    other.text = "\n".join(" | ".join(row) for row in other.cells)
    doc.blocks.append(other)
    result = supplement_comparatives(report, doc)
    assert not previous(result)
    assert any("存在冲突" in warning for warning in result.warnings)


def test_equity_row_cannot_supply_parent_profit():
    report, doc = fixture(name="parent_net_profit", label="归属于母公司股东的净利润")
    for block in doc.blocks:
        if block.kind == "table":
            block.cells[1][0] = "归属于母公司股东的净资产"
            block.text = "\n".join(" | ".join(row) for row in block.cells)
    assert not previous(supplement_comparatives(report, doc))


def profit_fixture():
    report, doc = fixture(name="parent_net_profit", label="归属于母公司股东的净利润")
    table = doc.blocks[-1]
    table.cells.append(["五、净利润（净亏损以负号填列）", "180.00", "80.00"])
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    return report, doc


def test_missing_consolidated_profit_is_read_from_its_own_row():
    report, doc = profit_fixture()
    result = supplement_comparatives(report, doc)
    profits = [m for m in result.metrics if m.name == "net_profit"]
    assert len(profits) == 2
    current = next(m for m in profits if m.period_end.year == 2023)
    prior = next(m for m in profits if m.period_end.year == 2022)
    assert current.value == Decimal("1800000")
    assert prior.value == Decimal("800000")
    assert current.value != report.metrics[0].value
    assert current.source_row_index == 2 and current.source_column_index == 1
    assert all(m.status == "verified" for m in profits)
    assert supplement_comparatives(result, doc).model_dump() == result.model_dump()


def test_unverified_existing_profit_does_not_prevent_independent_recheck():
    report, doc = profit_fixture()
    completed = supplement_comparatives(report, doc)
    current = next(m for m in completed.metrics if m.name == "net_profit" and m.period_end.year == 2023)
    report.metrics.append(current.model_copy(update={"value": Decimal("9990000"), "status": "unverified"}))
    result = supplement_comparatives(report, doc)
    verified = [m for m in result.metrics if m.name == "net_profit" and m.status == "verified"]
    assert len(verified) == 2
    comparison = next(c for c in analyze([result]).comparisons if c.metric == "net_profit" and c.kind == "yoy")
    assert comparison.status == "ok"
    assert comparison.current_value == Decimal("1800000")
    assert comparison.base_value == Decimal("800000")


def reconciled_profit_fixture():
    report, doc = profit_fixture()
    table = doc.blocks[-1]
    table.cells.append(["2.少数股东损益（净亏损以负号填列）", "-20.00", "-20.00"])
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    return report, doc


def test_profit_reconciliation_uses_same_statement_minority_row_without_changing_values():
    report, doc = reconciled_profit_fixture()
    result = supplement_comparatives(report, doc)
    assert len(result.metrics) == 4
    assert all(any("勾稽核验通过" in check for check in metric.checks) for metric in result.metrics)
    assert next(m for m in result.metrics if m.name == "net_profit" and m.period_end.year == 2023).value == Decimal("1800000")
    assert next(m for m in result.metrics if m.name == "parent_net_profit" and m.period_end.year == 2023).value == Decimal("2000000")
    assert not any("勾稽不一致" in warning for warning in result.warnings)
    assert supplement_comparatives(result, doc).model_dump() == result.model_dump()


@pytest.mark.parametrize("total, passed", [("180.01", True), ("180.02", False)])
def test_profit_reconciliation_tolerance_comes_from_original_decimal_places_and_unit(total, passed):
    report, doc = reconciled_profit_fixture()
    table = doc.blocks[-1]
    table.cells[2][1] = total
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    result = supplement_comparatives(report, doc)
    current = [m for m in result.metrics if m.period_end.year == 2023]
    assert all(any("勾稽核验通过" in check for check in metric.checks) == passed for metric in current)
    assert any("精度容差150.00元" in warning for warning in result.warnings) == (not passed)
    assert next(m for m in current if m.name == "net_profit").value == Decimal(total) * 10000


def test_reconciliation_conflict_warns_without_correcting_verified_amounts():
    report, doc = reconciled_profit_fixture()
    table = doc.blocks[-1]
    table.cells[3][1] = "-19.00"
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    result = supplement_comparatives(report, doc)
    current = [m for m in result.metrics if m.period_end.year == 2023]
    assert all(m.status == "verified" and not any("勾稽核验通过" in check for check in m.checks) for m in current)
    assert next(m for m in current if m.name == "net_profit").value == Decimal("1800000")
    assert next(m for m in current if m.name == "parent_net_profit").value == Decimal("2000000")
    assert any("勾稽不一致" in warning and "请人工核对，未自动改数" in warning for warning in result.warnings)


@pytest.mark.parametrize("problem", ["comprehensive_income_row", "missing_amount", "different_unit",
                                      "parent_scope", "ambiguous_scope"])
def test_reconciliation_refuses_other_comprehensive_income_or_unbound_scope_or_unit(problem):
    report, doc = reconciled_profit_fixture()
    table = doc.blocks[-1]
    if problem == "comprehensive_income_row":
        table.cells[3][0] = "归属于少数股东的其他综合收益"
    elif problem == "missing_amount":
        table.cells[3][1] = "-"
    elif problem == "different_unit":
        table.cells[3][0] += "（元）"
    else:
        next(block for block in doc.blocks if block.id == "title").text = (
            "母公司利润表" if problem == "parent_scope" else "合并利润表；母公司利润表")
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    result = supplement_comparatives(report, doc)
    current = [m for m in result.metrics if m.period_end.year == 2023]
    assert not any("勾稽核验通过" in check for metric in current for check in metric.checks)
    assert not any("勾稽不一致" in warning for warning in result.warnings)


@pytest.mark.parametrize("problem", ["anchor_mismatch", "missing_title", "parent_statement",
                                      "subtotal_row", "parent_scope"])
def test_profit_completion_refuses_wrong_anchor_or_scope(problem):
    report, doc = profit_fixture()
    table = doc.blocks[-1]
    if problem == "anchor_mismatch":
        table.cells[1][1] = "201.00"
    elif problem == "missing_title":
        next(b for b in doc.blocks if b.id == "title").text = "主要财务数据"
    elif problem == "parent_statement":
        next(b for b in doc.blocks if b.id == "title").text = "母公司利润表"
    elif problem == "subtotal_row":
        table.cells[2][0] = "（一）持续经营净利润"
    else:
        report.metrics[0].scope = "parent"
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    result = supplement_comparatives(report, doc)
    assert not any(m.name == "net_profit" for m in result.metrics)


def test_conflicting_current_profit_rows_are_not_selected():
    report, doc = profit_fixture()
    other = doc.blocks[-1].model_copy(deep=True)
    other.id = "doc:p2:table1"
    other.cells[2][1] = "181.00"
    other.text = "\n".join(" | ".join(row) for row in other.cells)
    doc.blocks.append(other)
    result = supplement_comparatives(report, doc)
    assert not any(m.name == "net_profit" for m in result.metrics)
    assert any("合并净利润补查存在多个冲突金额" in w for w in result.warnings)


def test_q1_cumulative_anchor_can_confirm_same_dates_quarter_profit_column():
    report, doc = fixture(name="parent_net_profit", label="归属于母公司股东的净利润",
                          headers=("本期发生额", "上期发生额"), end="2023-03-31", report_type="q1")
    source = doc.blocks[-1].model_copy(deep=True)
    source.id = "doc:p2:table1"
    source.cells[0] = ["项目", "2023年第一季度", "2022年第一季度"]
    source.cells.append(["五、净利润（净亏损以负号填列）", "180.00", "80.00"])
    source.text = "\n".join(" | ".join(row) for row in source.cells)
    doc.blocks.append(source)
    result = supplement_comparatives(report, doc)
    profits = [m for m in result.metrics if m.name == "net_profit"]
    assert len(profits) == 2
    current = next(m for m in profits if m.period_end.year == 2023)
    assert current.period_start == date(2023, 1, 1) and current.period_end == date(2023, 3, 31)
    assert current.basis == "quarter" and current.value == Decimal("1800000")


def test_q3_cumulative_anchor_cannot_confirm_single_quarter_profit_column():
    report, doc = profit_fixture()
    table = doc.blocks[-1]
    table.cells[0] = ["项目", "2023年第三季度", "2022年第三季度"]
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    assert not any(m.name == "net_profit" for m in supplement_comparatives(report, doc).metrics)


def nonrecurring_fixture(*, basis="ytd"):
    quarter = basis == "quarter"
    start = "2023-07-01" if quarter else "2023-01-01"
    headers = ("2023年第三季度", "2022年第三季度") if quarter else (
        "2023年前三季度", "2022年前三季度")
    report, doc = fixture(name="parent_net_profit", label="归属于母公司股东的净利润",
                          headers=headers, basis=basis, start=start)
    core = doc.blocks[-1]
    core.cells.append(["扣除非经常性损益的归母净利润", "160.00", "90.00"])
    core.text = "\n".join(" | ".join(row) for row in core.cells)
    adjusted = report.metrics[0].model_dump(mode="json")
    adjusted.update(name="adjusted_parent_net_profit", raw_value="160.00", value="0",
                    row_quote=" | ".join(core.cells[-1]), source_row_index=2)
    report = validate_payload(dict(company=COMPANY, report_type="q3", period_end="2023-09-30",
                                   metrics=[report.metrics[0].model_dump(mode="json"), adjusted]), doc, doc.blocks)
    assert len(report.metrics) == 2
    title = Evidence(id="nrtitle", document_id=doc.id, document_name=doc.name, pdf_page=3,
                     text="非经常性损益项目和金额")
    unit = Evidence(id="nrunit", document_id=doc.id, document_name=doc.name, pdf_page=3,
                    text="单位：万元 币种：人民币")
    cells = [["非经常性损益项目", "本期金额", "年初至报告期末金额", "说明"],
             ["政府补助", "50.00", "70.00", ""],
             ["减：所得税影响额", "5.00", "10.00", ""],
             ["少数股东权益影响额（税后）", "5.00", "20.00", ""],
             ["合计", "40.00", "40.00", ""]]
    table = Evidence(id="doc:p3:table0", document_id=doc.id, document_name=doc.name,
                     pdf_page=3, kind="table", cells=cells,
                     text="\n".join(" | ".join(row) for row in cells),
                     context_evidence_ids=[title.id, unit.id])
    doc.blocks.extend([title, unit, table])
    doc.page_count = 3
    return report, doc


@pytest.mark.parametrize("basis,column,start", [("ytd", 2, date(2023, 1, 1)),
                                                 ("quarter", 1, date(2023, 7, 1))])
def test_nonrecurring_is_read_from_original_net_parent_total(basis, column, start):
    report, doc = nonrecurring_fixture(basis=basis)
    completed = supplement_comparatives(report, doc)
    metrics = [m for m in completed.metrics if m.name == "nonrecurring_total_parent"]
    assert len(metrics) == 1
    metric = metrics[0]
    assert metric.value == Decimal("400000") and metric.status == "verified"
    assert metric.period_start == start and metric.basis == basis
    assert metric.source_row_index == 4 and metric.source_column_index == column
    assert metric.raw_value == "40.00"
    assert doc.blocks[-1].id in metric.evidence_ids
    assert supplement_comparatives(completed, doc).model_dump() == completed.model_dump()


@pytest.mark.parametrize("problem", ["wrong_amount", "missing_category", "pretax_subtotal",
                                      "missing_tax", "missing_minority", "different_period",
                                      "different_scope", "different_restated", "unverified_anchor",
                                      "wrong_currency"])
def test_nonrecurring_completion_rejects_unproven_or_incompatible_totals(problem):
    report, doc = nonrecurring_fixture()
    table = doc.blocks[-1]
    if problem == "wrong_amount":
        table.cells[4][2] = "39.00"
    elif problem == "missing_category":
        table.cells[0][0] = "项目"
        next(b for b in doc.blocks if b.id == "nrtitle").text = "其他金额"
    elif problem == "pretax_subtotal":
        table.cells = [table.cells[0], table.cells[4], *table.cells[1:4]]
    elif problem == "missing_tax":
        table.cells[2][0] = "其他扣减"
    elif problem == "missing_minority":
        table.cells[3][0] = "其他扣减"
    elif problem == "different_period":
        report.metrics[1].period_start = date(2023, 7, 1)
        report.metrics[1].basis = "quarter"
    elif problem == "different_scope":
        report.metrics[1].scope = "parent"
    elif problem == "different_restated":
        report.metrics[1].restated = True
    elif problem == "unverified_anchor":
        report.metrics[1].status = "unverified"
    else:
        report.metrics[1].currency = "USD"
    table.text = "\n".join(" | ".join(row) for row in table.cells)
    result = supplement_comparatives(report, doc)
    assert not any(m.name == "nonrecurring_total_parent" for m in result.metrics)


def test_conflicting_original_nonrecurring_totals_are_not_selected():
    report, doc = nonrecurring_fixture()
    other = doc.blocks[-1].model_copy(deep=True)
    other.id = "doc:p3:table1"
    other.cells[4][2] = "39.00"
    other.text = "\n".join(" | ".join(row) for row in other.cells)
    doc.blocks.append(other)
    result = supplement_comparatives(report, doc)
    assert not any(m.name == "nonrecurring_total_parent" for m in result.metrics)
    assert any("存在多个冲突金额" in w for w in result.warnings)


def test_nonrecurring_difference_cannot_create_an_undisclosed_total():
    report, doc = nonrecurring_fixture()
    doc.blocks = [b for b in doc.blocks if b.pdf_page != 3]
    assert not any(m.name == "nonrecurring_total_parent" for m in supplement_comparatives(report, doc).metrics)


def test_explicit_restatement_is_supplemented_from_its_exact_source_sentence():
    report, doc = nonrecurring_fixture()
    block = doc.blocks[0].model_copy(update={"id": "restatement-note", "kind": "text", "cells": [],
        "text": "公司采用追溯调整法进行会计处理，并重述了比较期间财务报表。变更没有重大影响。"})
    doc.blocks.append(block)
    result = supplement_comparatives(report, doc)
    disclosure = next(d for d in result.disclosures if d.kind == "restatement")
    assert disclosure.quote == "公司采用追溯调整法进行会计处理，并重述了比较期间财务报表"
    assert disclosure.evidence_ids == [block.id]
    signal = next(s for s in analyze([result]).signals if s.category == "accounting_change")
    assert signal.status == "detected"
    assert supplement_comparatives(result, doc).model_dump() == result.model_dump()


@pytest.mark.parametrize("text", ["公司是否需要重述以前年度会计数据", "公司尚未重述比较期间财务报表。",
    "公司拟重述比较期间财务报表。", "如果公司已经重述比较期间财务报表，应说明原因。",
    "会计政策变更及追溯调整的情况", "公司并未重述比较期间财务报表。"])
def test_restatement_completion_rejects_titles_negation_and_plans(text):
    report, doc = nonrecurring_fixture()
    doc.blocks.append(doc.blocks[0].model_copy(update={"id": "not-proof", "kind": "text", "cells": [], "text": text}))
    result = supplement_comparatives(report, doc)
    assert not any(d.kind == "restatement" for d in result.disclosures)


def test_public_huahong_q3_comparative_columns_and_page_continuation():
    path = Path(__file__).resolve().parents[1] / "samples/public/华虹公司_2023第三季度报告.pdf"
    if not path.exists():
        pytest.skip("公开财报样本未包含在本地环境")
    doc = parse_financial_doc(path)
    by_page = {block.pdf_page: block for block in doc.blocks if block.kind == "table" and block.id.endswith("table0")}
    company = "华虹半导体有限公司"
    cases = [("revenue", 12, "12952608201.89", "12260872870.42", "5.64"),
             ("net_profit", 13, "823071432.87", "1433790911.26", "-42.59"),
             ("parent_net_profit", 13, "1684552665.08", "1905779004.96", "-11.61"),
             ("operating_cashflow", 15, "3462387309.64", "3927193140.99", "-11.84")]
    metrics = []
    for name, page, current_value, prior_value, percent in cases:
        table = by_page[page]
        index = next(i for i, row in enumerate(table.cells) if len(row) > 1
                     and row[1].replace(",", "") == current_value)
        metrics.append(Metric(name=name, company=company, value=Decimal(current_value),
                              raw_value=table.cells[index][1], unit="元", period_start=date(2023, 1, 1),
                              period_end=date(2023, 9, 30), basis="ytd", scope="consolidated",
                              evidence_ids=[table.id, *table.context_evidence_ids],
                              row_quote=" | ".join(table.cells[index]),
                              source_row_index=index, source_column_index=1))
    report = ExtractedReport(document_id=doc.id, document_name=doc.name, company=company,
                             report_type="q3", period_end=date(2023, 9, 30), metrics=metrics)
    result = supplement_comparatives(report, doc)
    assert len(previous(result)) == 4
    for name, page, current_value, prior_value, percent in cases:
        prior = next(metric for metric in previous(result) if metric.name == name)
        assert prior.value == Decimal(prior_value)
        comparison = next(item for item in analyze([result]).comparisons
                          if item.metric == name and item.kind == "yoy")
        assert comparison.status == "ok"
        assert comparison.percent.quantize(Decimal("0.01")) == Decimal(percent)
    net_prior = next(metric for metric in previous(result) if metric.name == "net_profit")
    assert by_page[12].id in net_prior.evidence_ids
    assert any("合并利润表" in block.text for block in doc.blocks if block.id in net_prior.evidence_ids)

    # 实际模型曾漏提合并净利润；只保留其余已核验金额，仍应从原表补齐。
    missing_profit = report.model_copy(deep=True)
    missing_profit.metrics = [m for m in report.metrics if m.name != "net_profit"]
    completed = supplement_comparatives(missing_profit, doc)
    profit_comparison = next(c for c in analyze([completed]).comparisons
                             if c.metric == "net_profit" and c.kind == "yoy")
    assert profit_comparison.status == "ok"
    assert profit_comparison.current_value == Decimal("823071432.87")
    assert profit_comparison.base_value == Decimal("1433790911.26")
    assert profit_comparison.percent.quantize(Decimal("0.01")) == Decimal("-42.59")


def test_public_tcl_cashflow_comparative_is_recovered_from_original_statement():
    path = Path(__file__).resolve().parents[1] / "samples/public/TCL科技_2024第三季度报告.pdf"
    if not path.exists():
        pytest.skip("公开财报样本未包含在本地环境")
    doc = parse_financial_doc(path)
    company = "TCL科技集团股份有限公司"
    summary = next(block for block in doc.blocks if block.kind == "table" and block.pdf_page == 5
                   and any(row and "经营活动产生的现金流" in row[0] for row in block.cells))
    row_index = next(index for index, row in enumerate(summary.cells)
                     if row and "经营活动产生的现金流" in row[0])
    row = summary.cells[row_index]
    # 真实保存结果的本期锚点来自摘要第5页，那里只披露金额和增长率。
    item = dict(name="operating_cashflow", company=company, value="0", raw_value=row[3],
        unit="元", currency="CNY", period_start="2024-01-01", period_end="2024-09-30",
        basis="ytd", scope="consolidated", restated=False, evidence_ids=[summary.id],
        row_quote=" | ".join(row), source_row_index=row_index, source_column_index=3)
    report = validate_payload(dict(company=company, report_type="q3", period_end="2024-09-30",
                                   metrics=[item]), doc, doc.blocks)
    assert len(report.metrics) == 1 and report.metrics[0].status == "verified"
    assert report.metrics[0].value == Decimal("22000714536")
    result = supplement_comparatives(report, doc)
    prior = next(metric for metric in result.metrics if metric.period_end.year == 2023)
    assert prior.value == Decimal("16144013013")
    assert (prior.period_start, prior.period_end) == (date(2023, 1, 1), date(2023, 9, 30))
    assert prior.scope == "consolidated" and prior.basis == "ytd" and prior.status == "verified"
    statement = next(block for block in doc.blocks if block.kind == "table" and block.pdf_page == 17)
    assert statement.id in prior.evidence_ids
    assert statement.cells[prior.source_row_index][prior.source_column_index] == "16,144,013,013"
    assert any("合并年初到报告期末现金流量表" in block.text
               for block in doc.blocks if block.id in prior.evidence_ids)
    comparison = next(item for item in analyze([result]).comparisons
                      if item.metric == "operating_cashflow" and item.kind == "yoy")
    assert comparison.status == "ok"
    assert comparison.current_value == Decimal("22000714536")
    assert comparison.base_value == Decimal("16144013013")
    assert comparison.percent.quantize(Decimal("0.01")) == Decimal("36.28")
    assert supplement_comparatives(result, doc).model_dump() == result.model_dump()


@pytest.mark.parametrize("filename,total_value,parent_value,minority_value", [
    ("TCL科技_2024第三季度报告.pdf", "-1829006223", "1525319763", "-3354325986"),
    ("华虹公司_2024第三季度报告.pdf", "-327805225.06", "577927957.36", "-905733182.42"),
    ("浪潮信息_2024第三季度报告.pdf", "1278368505.95", "1294269240.41", "-15900734.46"),
    ("科大讯飞_2024第三季度报告.pdf", "-423226905.19", "-343704233.43", "-79522671.76"),
])
def test_public_profit_reconciliation_preserves_negative_and_opposite_sign_values(
        filename, total_value, parent_value, minority_value):
    path = Path(__file__).resolve().parents[1] / "samples/public" / filename
    if not path.exists():
        pytest.skip("公开财报样本未包含在本地环境")
    companies = {"TCL": "TCL科技集团股份有限公司", "华虹": "华虹半导体有限公司",
                 "浪潮": "浪潮电子信息产业股份有限公司", "科大": "科大讯飞股份有限公司"}
    company = next(value for prefix, value in companies.items() if filename.startswith(prefix))
    doc = parse_financial_doc(path)
    items = []
    for name, expected in (("net_profit", total_value), ("parent_net_profit", parent_value)):
        for table in doc.blocks:
            if table.kind != "table":
                continue
            matching_rows = [index for index, row in enumerate(table.cells)
                             if len(row) > 1 and row[1].replace(",", "") == expected
                             and ((name == "net_profit" and row[0].startswith("五、净利润"))
                                  or (name == "parent_net_profit" and "归属于母公司" in row[0]))]
            if not matching_rows:
                continue
            index = matching_rows[0]
            row = table.cells[index]
            items.append(dict(name=name, company=company, value="0", raw_value=row[1], unit="元",
                currency="CNY", period_start="2024-01-01", period_end="2024-09-30", basis="ytd",
                scope="consolidated", restated=False, evidence_ids=[table.id],
                row_quote=" | ".join(row), source_row_index=index, source_column_index=1))
            break
    report = validate_payload(dict(company=company, report_type="q3", period_end="2024-09-30",
                                   metrics=items), doc, doc.blocks)
    assert len(report.metrics) == 2, report.warnings
    result = supplement_comparatives(report, doc)
    current = [metric for metric in result.metrics if metric.period_end.year == 2024]
    assert {metric.name: metric.value for metric in current} == {
        "net_profit": Decimal(total_value), "parent_net_profit": Decimal(parent_value)}
    assert all(any("勾稽核验通过" in check and f"少数股东损益{minority_value}元" in check
                   for check in metric.checks) for metric in current)
    assert not any("勾稽不一致" in warning for warning in result.warnings)
