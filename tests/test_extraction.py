"""原文与金额关联的回归测试；人工答案独立于模型返回金额。"""
from copy import deepcopy
from decimal import Decimal

import pytest

from finance_agent.analysis import detect_accounting_change
from finance_agent.extraction import validate_payload
from finance_agent.schemas import Evidence, ParsedDocument


COMPANY = "测试制造股份有限公司"
DOC_ID = "test-document"
TEXT_ROW = "营业收入：200.00；2025-01-01至2025-12-31；年初至今累计；合并；单位：万元。"


def evidence(identifier, text, **kwargs):
    return Evidence(id=identifier, document_id=DOC_ID, document_name="测试2025年报.pdf",
                    pdf_page=1, text=text, **kwargs)


def document(blocks):
    return ParsedDocument(id=DOC_ID, name="测试2025年报.pdf", sha256="0" * 64,
                          page_count=1, blocks=blocks)


def payload(metrics=(), disclosures=(), coverage=()):
    return {"company": COMPANY, "report_type": "annual", "period_end": "2025-12-31",
            "metrics": list(metrics), "disclosures": list(disclosures), "coverage": list(coverage)}


def text_case():
    blocks = [evidence("meta", COMPANY + " 2025年度报告；2024年可比信息。"),
              evidence("row", TEXT_ROW)]
    item = {"name": "revenue", "company": COMPANY, "raw_value": "200.00", "value": "999",
            "unit": "万元", "currency": "CNY", "period_start": "2025-01-01",
            "period_end": "2025-12-31", "basis": "ytd", "scope": "consolidated",
            "restated": False, "evidence_ids": ["row"], "row_quote": TEXT_ROW}
    return blocks, item


def table_case():
    cells = [["项目", "2025年度", "2024年度"],
             ["营业收入", "200.00", "100.00"],
             ["净利润", "20.00", "10.00"],
             ["归属于母公司股东的净利润", "18.00", "9.00"],
             ["归属于母公司股东的净资产", "800.00", "700.00"]]
    table_text = "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)
    blocks = [evidence("meta", COMPANY + " 2025年度报告；2024年可比信息。"),
              evidence("table", table_text, kind="table", cells=cells)]
    _, item = text_case()
    item.update(evidence_ids=["table"], row_quote=" | ".join(cells[1]),
                source_row_index=1, source_column_index=1)
    return blocks, item


def validate(blocks, item):
    return validate_payload(payload([item]), document(blocks), blocks)


def test_text_amount_is_recomputed_from_raw_value_and_unit():
    blocks, item = text_case()
    result = validate(blocks, item)
    assert len(result.metrics) == 1
    assert result.metrics[0].value == Decimal("2000000.00")
    assert result.metrics[0].raw_value == "200.00"
    assert result.metrics[0].status == "verified"
    assert result.metrics[0].value != Decimal(item["value"])


def test_disclosure_quote_can_span_adjacent_original_text_blocks():
    blocks = [evidence("meta", COMPANY + " 2025年度报告"),
              evidence("a", "根据新会计政策规定，公司计提的保证类质保费用"),
              evidence("b", "计入主营业务成本，不再计入销售费用。")]
    quote = blocks[1].text + blocks[2].text
    result = validate_payload(payload(disclosures=[{"kind": "accounting_policy", "summary": "费用列报变更",
        "quote": quote, "evidence_ids": ["b", "a"]}], coverage=[{
        "topic": "accounting_change", "status": "reviewed", "evidence_ids": ["a", "b"]}]), document(blocks), blocks)
    assert len(result.disclosures) == 1
    assert detect_accounting_change(result).status == "detected"


@pytest.mark.parametrize("problem", ["skipped", "reversed", "cross_page", "missing_ref"])
def test_disclosure_quote_cannot_join_unrelated_or_unquoted_text(problem):
    blocks = [evidence("meta", COMPANY + " 2025年度报告"),
              evidence("a", "公司采用追溯调整法进行会计处理，"),
              evidence("b", "并重述了比较期间财务报表。")]
    quote = blocks[1].text + blocks[2].text
    refs = ["a", "b"]
    if problem == "skipped":
        blocks.insert(2, evidence("between", "上述为示例，实际本年未调整。"))
    elif problem == "reversed":
        quote = blocks[2].text + blocks[1].text
    elif problem == "cross_page":
        blocks[2].pdf_page = 2
    else:
        refs = ["a"]
    result = validate_payload(payload(disclosures=[{"kind": "restatement", "summary": "重述",
        "quote": quote, "evidence_ids": refs}]), document(blocks), blocks)
    assert result.disclosures == []


@pytest.mark.parametrize("problem", ["missing_reference", "wrong_sign", "wrong_unit", "missing_period"])
def test_unproven_text_amounts_are_excluded(problem):
    blocks, item = text_case()
    if problem == "missing_reference":
        item["evidence_ids"] = ["does-not-exist"]
    elif problem == "wrong_sign":
        blocks[1] = blocks[1].model_copy(update={"text": TEXT_ROW.replace("200.00", "-200.00")})
        item["row_quote"] = blocks[1].text
    elif problem == "wrong_unit":
        item["unit"] = "元"
    else:
        blocks[1] = blocks[1].model_copy(update={"text": TEXT_ROW.replace("2025-12-31", "期末")})
        item["row_quote"] = blocks[1].text
    result = validate(blocks, item)
    assert result.metrics == []
    assert result.warnings


def test_text_quote_cannot_borrow_another_metrics_amount():
    blocks, item = text_case()
    other_row = TEXT_ROW.replace("营业收入：200.00", "净利润：20.00")
    combined = TEXT_ROW + "\n" + other_row
    blocks[1] = blocks[1].model_copy(update={"text": combined})
    item.update(raw_value="20.00", row_quote=combined)
    result = validate(blocks, item)
    assert result.metrics == [], "跨两条指标行拼接的引用不能证明营业收入金额"


def test_table_amount_is_bound_to_actual_cell():
    blocks, item = table_case()
    result = validate(blocks, item)
    assert len(result.metrics) == 1
    assert result.metrics[0].source_column_index == 1
    assert result.metrics[0].value == Decimal("2000000.00")


@pytest.mark.parametrize("problem", ["other_column", "wrong_year", "other_metric_row"])
def test_table_mismatched_cell_or_period_is_excluded(problem):
    blocks, item = table_case()
    if problem == "other_column":
        item["source_column_index"] = 2
    elif problem == "wrong_year":
        item.update(period_start="2024-01-01", period_end="2024-12-31")
    else:
        item.update(raw_value="20.00", source_row_index=2,
                    row_quote=" | ".join(blocks[1].cells[2]))
    result = validate(blocks, item)
    assert result.metrics == []
    assert result.warnings


def test_relative_previous_year_header_maps_to_previous_period():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0] = ["项目", "本年", "上年"]
    text = "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)
    blocks[1] = blocks[1].model_copy(update={"text": text, "cells": cells})
    item.update(raw_value="100.00", source_column_index=2,
                period_start="2024-01-01", period_end="2024-12-31")
    result = validate(blocks, item)
    assert len(result.metrics) == 1
    assert result.metrics[0].value == Decimal("1000000.00")


@pytest.mark.parametrize("column,year,value", [(1, 2025, "200.00"), (2, 2024, "100.00")])
def test_verified_annual_summary_bare_year_columns_cover_the_full_calendar_year(column, year, value):
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0] = ["主要会计数据", "2025年", "2024年"]
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(raw_value=value, source_column_index=column,
                period_start=f"{year}-01-01", period_end=f"{year}-12-31")
    result = validate(blocks, item)
    assert len(result.metrics) == 1
    assert result.metrics[0].value == Decimal(value) * Decimal(10000)


@pytest.mark.parametrize("problem", ["quarter_report", "missing_annual_cover", "note_only",
                                      "quarter_context", "wrong_period", "wrong_basis"])
def test_bare_year_flow_columns_need_a_verified_annual_table_context(problem):
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0] = ["主要会计数据", "2025年", "2024年"]
    title = ""
    body = payload([item])
    if problem == "quarter_report":
        body.update(report_type="q3", period_end="2025-09-30")
        blocks[0] = blocks[0].model_copy(update={"text": COMPANY + "2025年第三季度报告"})
        item.update(period_end="2025-09-30")
    elif problem == "missing_annual_cover":
        blocks[0] = blocks[0].model_copy(update={"text": COMPANY + "2025年财务数据"})
    elif problem == "note_only":
        cells[0][0] = "项目"
        title = "以下说明合并利润表中的某项目。\n"
    elif problem == "quarter_context":
        title = "第三季度（7-9月）\n"
    elif problem == "wrong_period":
        item.update(period_start="2025-07-01", period_end="2025-09-30", basis="quarter")
    else:
        item.update(basis="quarter")
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": title + "单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    body["metrics"] = [item]
    assert validate_payload(body, document(blocks), blocks).metrics == []


@pytest.mark.parametrize("column,start,end", [(1, "2025-01-01", "2025-03-31"),
                                            (2, "2025-04-01", "2025-06-30"),
                                            (3, "2025-07-01", "2025-09-30"),
                                            (4, "2025-10-01", "2025-12-31")])
def test_annual_quarter_summary_columns_bind_to_an_explicit_report_year(column, start, end):
    blocks, item = table_case()
    cells = [["", "第一季度（1-3月份）", "第二季度（4-6月份）", "第三季度（7-9月份）", "第四季度（10-12月份）"],
             ["营业收入", "50.00", "60.00", "70.00", "80.00"]]
    blocks[1] = blocks[1].model_copy(update={"cells": cells,
        "text": "九、2025年分季度主要财务数据\n单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(raw_value=cells[1][column], period_start=start, period_end=end, basis="quarter",
                row_quote=" | ".join(cells[1]), source_column_index=column)
    report = validate(blocks, item)
    assert len(report.metrics) == 1
    assert str(report.metrics[0].period_end) == end


@pytest.mark.parametrize("title,basis", [("2024年分季度主要财务数据", "quarter"),
                                      ("分季度主要财务数据", "quarter"),
                                      ("2025年分季度主要财务数据", "ytd")])
def test_annual_quarter_summary_does_not_invent_year_or_cumulative_basis(title, basis):
    blocks, item = table_case()
    cells = [["", "第三季度（7-9月份）"], ["营业收入", "70.00"]]
    blocks[1] = blocks[1].model_copy(update={"cells": cells,
        "text": title + "\n单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(raw_value="70.00", period_start="2025-07-01", period_end="2025-09-30", basis=basis,
                row_quote=" | ".join(cells[1]))
    assert validate(blocks, item).metrics == []


def test_parent_profit_cannot_replace_consolidated_net_profit():
    blocks, item = table_case()
    item.update(name="net_profit", raw_value="18.00", source_row_index=3,
                row_quote=" | ".join(blocks[1].cells[3]))
    assert validate(blocks, item).metrics == [], "合并利润表标题不能把归母利润变成合并净利润"


def test_parent_equity_cannot_replace_parent_net_profit():
    blocks, item = table_case()
    item.update(name="parent_net_profit", raw_value="800.00", source_row_index=4,
                row_quote=" | ".join(blocks[1].cells[4]))
    assert validate(blocks, item).metrics == [], "归属于母公司净资产与归母净利润是不同指标"


def test_standard_adjusted_profit_label_is_not_mistaken_for_nonrecurring_total():
    blocks, item = text_case()
    row = TEXT_ROW.replace("营业收入", "扣除非经常性损益后的归属于上市公司股东的净利润")
    blocks[1] = blocks[1].model_copy(update={"text": row})
    item.update(name="adjusted_parent_net_profit", row_quote=row)
    result = validate(blocks, item)
    assert len(result.metrics) == 1
    assert result.metrics[0].name == "adjusted_parent_net_profit"
    assert result.metrics[0].value == Decimal("2000000.00")


def test_explicit_no_change_disclosures_survive_extraction():
    blocks = [evidence("meta", COMPANY + " 2025年度报告。")]
    disclosures = []
    for index, (kind, label) in enumerate([
        ("accounting_policy", "会计政策"), ("accounting_estimate", "会计估计"),
        ("consolidation", "合并范围"), ("restatement", "追溯调整"),
    ]):
        identifier = f"no-change-{index}"
        quote = f"本报告期未发生{label}变更。"
        blocks.append(evidence(identifier, quote))
        disclosures.append({"kind": kind, "summary": "明确未变更", "quote": quote,
                            "evidence_ids": [identifier]})
    body = payload(disclosures=disclosures, coverage=[{
        "topic": "accounting_change", "status": "reviewed",
        "evidence_ids": [x.id for x in blocks[1:]],
    }])
    result = validate_payload(body, document(blocks), blocks)
    assert len(result.disclosures) == 4
    assert detect_accounting_change(result).status == "not_detected"

def test_change_without_material_effect_is_still_a_change():
    quote = "本次会计政策变更未发生重大影响。"
    blocks = [evidence("meta", COMPANY + " 2025年度报告。"), evidence("change", quote)]
    body = payload(disclosures=[{"kind": "accounting_policy", "summary": "影响不重大",
                                 "quote": quote, "evidence_ids": ["change"]}],
                   coverage=[{"topic": "accounting_change", "status": "reviewed", "evidence_ids": ["change"]}])
    result = validate_payload(body, document(blocks), blocks)
    assert len(result.disclosures) == 1
    assert detect_accounting_change(result).status == "detected"


@pytest.mark.parametrize("quote,accepted", [
    ("公司对重大会计差错更正原因及影响的分析说明：□适用 √不适用", False),
    ("公司对会计估计变更原因及影响的分析说明：□适用 √不适用", True),
    ("公司对会计估计变更或重大会计差错更正原因及影响的分析说明：□适用 √不适用", True),
])
def test_accounting_error_correction_cannot_be_misclassified_as_estimate_change(quote, accepted):
    blocks = [evidence("meta", COMPANY + "2025年度报告"), evidence("disclosure", quote)]
    body = payload(disclosures=[{"kind": "accounting_estimate", "summary": "该事项不适用",
                                 "quote": quote, "evidence_ids": ["disclosure"]}])
    report = validate_payload(body, document(blocks), blocks)
    assert bool(report.disclosures) is accepted


def test_checked_not_applicable_disclosures_are_not_positive_changes():
    blocks = [evidence("meta", COMPANY + " 2025年度报告。")]
    disclosures = []
    for index, (kind, label) in enumerate([
        ("accounting_policy", "会计政策变更"), ("accounting_estimate", "会计估计变更"),
        ("consolidation", "合并范围变化"), ("restatement", "追溯调整"),
    ]):
        identifier = f"checkbox-{index}"
        quote = f"{label}：□适用 √不适用"
        blocks.append(evidence(identifier, quote))
        disclosures.append({"kind": kind, "summary": "该事项明确不适用", "quote": quote,
                            "evidence_ids": [identifier]})
    body = payload(disclosures=disclosures, coverage=[{
        "topic": "accounting_change", "status": "reviewed",
        "evidence_ids": [x.id for x in blocks[1:]],
    }])
    result = validate_payload(body, document(blocks), blocks)
    assert len(result.disclosures) == 4
    assert detect_accounting_change(result).status == "not_detected"

    # 同一组选项改为勾选适用，不能因为仍含“不适用”文字而误判为无变化。
    positive_blocks = [block.model_copy(update={"text": block.text.replace("□适用 √不适用", "√适用 □不适用")})
                       for block in blocks]
    positive_body = deepcopy(body)
    for item in positive_body["disclosures"]:
        item["quote"] = item["quote"].replace("□适用 √不适用", "√适用 □不适用")
    positive = validate_payload(positive_body, document(positive_blocks), positive_blocks)
    assert detect_accounting_change(positive).status == "detected"
def test_partial_retrieval_does_not_retry_complete_verified_metrics(tmp_path, monkeypatch):
    from finance_agent.audit import AuditLog
    from finance_agent.demo import ensure_demo_files, extract_demo_report
    from finance_agent.extraction import extract_metrics
    from finance_agent.parser import parse_financial_doc

    doc = parse_financial_doc(ensure_demo_files()[0])
    payload = extract_demo_report(doc).model_dump(mode="json")
    monkeypatch.setattr("finance_agent.extraction.retrieve_blocks", lambda *a, **k: (doc.blocks, True))

    class StubClient:
        calls = 0

        def complete_json(self, *args, **kwargs):
            self.calls += 1
            return payload

    client = StubClient()
    report = extract_metrics(doc, client, AuditLog(tmp_path / "trace.jsonl"))
    assert client.calls == 1
    assert len(report.metrics) == 9
    assert any("未完整审阅" in warning for warning in report.warnings)
    assert not any("仍缺失" in warning for warning in report.warnings)


@pytest.mark.parametrize("headers,start,end,basis", [
    (("2025年前三季度（1-9月）", "2024年前三季度（1-9月）"), "2025-07-01", "2025-09-30", "quarter"),
    (("2025年第三季度（7-9月）", "2024年第三季度（7-9月）"), "2025-01-01", "2025-09-30", "ytd"),
    (("2025年1-6月", "2024年1-6月"), "2025-01-01", "2025-09-30", "ytd"),
    (("2025-01-01至2025-09-30", "2024-01-01至2024-09-30"), "2025-07-01", "2025-09-30", "quarter"),
])
def test_same_year_does_not_prove_the_wrong_quarter_or_duration(headers, start, end, basis):
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0] = ["项目", *headers]
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(period_start=start, period_end=end, basis=basis)
    body = payload([item])
    body.update(report_type="q3", period_end="2025-09-30")
    result = validate_payload(body, document(blocks), blocks)
    assert result.metrics == [], "同比分母不能将累计金额误标为同年单季度"
    assert result.warnings


@pytest.mark.parametrize("label", ["持续经营净利润（净亏损以负号填列）", "终止经营净利润", "少数股东净利润"])
def test_consolidated_profit_subitems_cannot_replace_total_net_profit(label):
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[2][0] = label
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(name="net_profit", raw_value="20.00", source_row_index=2, row_quote=" | ".join(cells[2]))
    assert validate(blocks, item).metrics == []


@pytest.mark.parametrize("scope,title", [("parent", "合并利润表"), ("consolidated", "母公司利润表")])
def test_statement_title_and_report_scope_must_agree(scope, title):
    blocks, item = table_case()
    blocks[1] = blocks[1].model_copy(update={"text": blocks[1].text.replace("合并利润表", title)})
    item["scope"] = scope
    assert validate(blocks, item).metrics == []


@pytest.mark.parametrize("currency", ["美元", "港元", "USD", "欧元"])
def test_explicit_foreign_currency_is_never_defaulted_to_cny(currency):
    blocks, item = table_case()
    blocks[1] = blocks[1].model_copy(update={"text": blocks[1].text + f"\n币种：{currency}"})
    assert validate(blocks, item).metrics == []


def test_numeric_year_in_text_cannot_replace_the_amount_after_the_label():
    blocks, item = text_case()
    item["raw_value"] = "2025"
    assert validate(blocks, item).metrics == []


def test_percentage_inside_a_date_column_is_not_an_amount():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[1][1] = "200.00%"
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item["row_quote"] = " | ".join(cells[1])
    assert validate(blocks, item).metrics == []


def test_total_revenue_cannot_replace_separately_reported_operating_revenue():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells.insert(1, ["营业总收入", "220.00", "110.00"])
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(raw_value="220.00", row_quote=" | ".join(cells[1]))
    assert validate(blocks, item).metrics == []
    item.update(raw_value="200.00", source_row_index=2, row_quote=" | ".join(cells[2]))
    assert validate(blocks, item).metrics[0].value == Decimal("2000000")


@pytest.mark.parametrize("foreign_location", ["row", "column", "table_title"])
def test_foreign_accounting_standards_are_rejected_per_selected_row_and_column(foreign_location):
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    extra = ""
    if foreign_location == "row":
        cells[1][0] = "按境外会计准则营业收入"
    elif foreign_location == "column":
        cells[0][1] += "（国际财务报告准则）"
    else:
        cells.insert(0, ["国际财务报告准则合并利润表", "", ""])
        item["source_row_index"] = 2
    row = item["source_row_index"]
    item["row_quote"] = " | ".join(cells[row])
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。" + extra + "\n" + "\n".join(" | ".join(r) for r in cells)})
    assert validate(blocks, item).metrics == []


def test_chinese_standard_row_remains_valid_next_to_foreign_standard_row():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[1][0] = "按中国会计准则营业收入"
    cells.append(["按境外会计准则营业收入", "250.00", "150.00"])
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item["row_quote"] = " | ".join(cells[1])
    assert validate(blocks, item).metrics[0].value == Decimal("2000000")


def test_chinese_standard_amount_column_remains_valid_next_to_foreign_column():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0] = ["项目", "2025年度（中国会计准则）", "2025年度（境外会计准则）"]
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    assert validate(blocks, item).metrics[0].value == Decimal("2000000")
    item.update(raw_value="100.00", source_column_index=2)
    assert validate(blocks, item).metrics == []


def test_nonrecurring_pre_tax_total_cannot_replace_after_tax_parent_total():
    blocks, item = table_case()
    cells = [["项目", "2025年度", "2024年度"],
             ["非经常性损益合计", "20.00", "10.00"],
             ["减：所得税影响额", "2.00", "1.00"],
             ["少数股东权益影响额", "1.00", "0.50"],
             ["合计", "17.00", "8.50"]]
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "非经常性损益项目和金额；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(name="nonrecurring_total_parent", raw_value="20.00", row_quote=" | ".join(cells[1]))
    assert validate(blocks, item).metrics == []
    item.update(raw_value="17.00", source_row_index=4, row_quote=" | ".join(cells[4]))
    assert validate(blocks, item).metrics[0].value == Decimal("170000")


def test_repair_with_more_noncore_records_cannot_drop_current_core_metrics(tmp_path, monkeypatch):
    from finance_agent.audit import AuditLog
    from finance_agent.demo import ensure_demo_files, extract_demo_report
    from finance_agent.extraction import extract_metrics
    from finance_agent.parser import parse_financial_doc

    doc = parse_financial_doc(ensure_demo_files()[0])
    original = extract_demo_report(doc)
    repaired = original.model_copy(deep=True)
    repaired.metrics = [metric for metric in repaired.metrics if metric.name != "net_profit"]
    for year in range(2015, 2020):
        repaired.metrics.append(repaired.metrics[0].model_copy(update={
            "period_start": repaired.metrics[0].period_start.replace(year=year),
            "period_end": repaired.metrics[0].period_end.replace(year=year)}))
    monkeypatch.setattr("finance_agent.extraction.retrieve_blocks", lambda *a, **k: (doc.blocks, True))
    monkeypatch.setattr("finance_agent.extraction.validate_payload",
                        lambda body, *a, **k: original.model_copy(deep=True) if body["round"] == 1 else repaired.model_copy(deep=True))

    class StubClient:
        calls = 0

        def complete_json(self, *args, **kwargs):
            self.calls += 1
            return {"round": self.calls}

    client = StubClient()
    result = extract_metrics(doc, client, AuditLog(tmp_path / "trace.jsonl"))
    assert client.calls == 2
    assert len(result.metrics) == len(original.metrics)
    assert any(metric.name == "net_profit" for metric in result.metrics)


def test_real_tcl_colored_cells_and_cumulative_cross_page_title_are_parsed_correctly():
    from pathlib import Path
    from finance_agent.parser import is_consolidated_profit_statement, parse_financial_doc

    path = Path(__file__).resolve().parents[1] / "samples/public/TCL科技_2024第三季度报告.pdf"
    if not path.exists():
        pytest.skip("公开 TCL 样本未下载")
    doc = parse_financial_doc(path)
    by_id = {block.id: block for block in doc.blocks}
    table = next(block for block in doc.blocks if block.kind == "table" and block.pdf_page == 16
                 and any(row and row[0].startswith("五、净利润") for row in block.cells))
    assert len(table.cells[0]) == 3, "填色背景不能制造错位的虚假金额列"
    assert table.header_evidence_id and by_id[table.header_evidence_id].pdf_page == 15
    assert any(is_consolidated_profit_statement(by_id[ref].text) for ref in table.context_evidence_ids)
    row_index = next(i for i, row in enumerate(table.cells) if row[0].startswith("五、净利润"))
    row = table.cells[row_index]
    body = {"company": "TCL科技集团股份有限公司", "report_type": "q3", "period_end": "2024-09-30", "metrics": [{
        "name": "net_profit", "company": "TCL科技集团股份有限公司", "raw_value": "-1,829,006,223", "unit": "元",
        "currency": "CNY", "period_start": "2024-01-01", "period_end": "2024-09-30", "basis": "ytd",
        "scope": "consolidated", "restated": False, "evidence_ids": [table.id], "row_quote": " | ".join(row),
        "source_row_index": row_index, "source_column_index": 1}]}
    result = validate_payload(body, doc, doc.blocks)
    assert len(result.metrics) == 1, result.warnings
    assert result.metrics[0].value == Decimal("-1829006223")
    body["metrics"][0].update(period_start="2024-07-01", basis="quarter")
    assert validate_payload(body, doc, doc.blocks).metrics == []


@pytest.mark.parametrize("keep_verified", [True, False])
def test_failed_optional_repair_keeps_only_previously_verified_metrics(tmp_path, monkeypatch, keep_verified):
    import json
    from finance_agent.audit import AuditLog
    from finance_agent.demo import ensure_demo_files, extract_demo_report
    from finance_agent.extraction import extract_metrics
    from finance_agent.llm import ModelError
    from finance_agent.parser import parse_financial_doc

    doc = parse_financial_doc(ensure_demo_files()[0])
    first = extract_demo_report(doc)
    if not keep_verified:
        first.metrics = []
    monkeypatch.setattr("finance_agent.extraction.retrieve_blocks", lambda *a, **k: (doc.blocks, True))
    monkeypatch.setattr("finance_agent.extraction.validate_payload", lambda *a, **k: first.model_copy(deep=True))

    class StubClient:
        calls = 0

        def complete_json(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("补查时模型连接不可用")
            return {}

    client = StubClient()
    trace_path = tmp_path / "trace.jsonl"
    if not keep_verified:
        with pytest.raises(ModelError, match="模型连接不可用"):
            extract_metrics(doc, client, AuditLog(trace_path))
    else:
        result = extract_metrics(doc, client, AuditLog(trace_path))
        assert result.metrics == first.metrics
        assert any("补查未完成" in warning for warning in result.warnings)
        logs = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        failed = next(item for item in logs if item.get("tool") == "repair_metrics")
        assert failed["status"] == "failed"
        assert failed["output"]["error"] == "model_repair_unavailable"
        assert failed["output"]["kept_verified_metrics"] == len(first.metrics)
    assert client.calls == 2


@pytest.mark.parametrize("label,flag", [("调整前", False), ("调整后", True), ("重述前", False), ("重述后", True)])
def test_explicit_restatement_column_must_match_the_restated_flag(label, flag):
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0][1] += f"（{label}）"
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item["restated"] = flag
    assert len(validate(blocks, item).metrics) == 1
    item["restated"] = not flag
    assert validate(blocks, item).metrics == []


def test_ambiguous_adjustment_header_is_not_treated_as_adjusted_or_original():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells[0][1] += "（追溯调整）"
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    for flag in (True, False):
        item["restated"] = flag
        assert validate(blocks, item).metrics == []


def nonrec_case(report_end, start, basis, column):
    company = COMPANY
    cells = [["非经常性损益项目", "本期金额", "年初至报告期末金额", "说明"],
             ["资产处置损益", "25.00", "250.00", ""],
             ["减：所得税影响额", "2.00", "20.00", ""],
             ["少数股东权益影响额（税后）", "3.00", "30.00", ""],
             ["合计", "20.00", "200.00", ""]]
    blocks = [evidence("meta", company + " 2025年第三季度报告" if report_end.endswith("09-30") else company + " 2025年第一季度报告"),
              evidence("table", "非经常性损益；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells), kind="table", cells=cells)]
    _, item = text_case()
    item.update(name="nonrecurring_total_parent", raw_value=cells[4][column], period_start=start,
                period_end=report_end, basis=basis, evidence_ids=["table"], row_quote=" | ".join(cells[4]),
                source_row_index=4, source_column_index=column)
    return blocks, item


def test_q3_nonrecurring_current_amount_is_quarter_when_cumulative_amount_is_separate():
    blocks, item = nonrec_case("2025-09-30", "2025-07-01", "quarter", 1)
    body = payload([item])
    body.update(report_type="q3", period_end="2025-09-30")
    assert validate_payload(body, document(blocks), blocks).metrics[0].value == Decimal("200000")
    item.update(period_start="2025-01-01", basis="ytd")
    assert validate_payload(body, document(blocks), blocks).metrics == []
    item.update(raw_value="200.00", source_column_index=2)
    assert validate_payload(body, document(blocks), blocks).metrics[0].value == Decimal("2000000")


@pytest.mark.parametrize("basis", ["ytd", "quarter"])
def test_q1_single_current_nonrecurring_amount_has_the_same_january_to_march_period(basis):
    blocks, item = nonrec_case("2025-03-31", "2025-01-01", basis, 1)
    cells = [[row[0], row[1]] for row in blocks[1].cells]
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "非经常性损益；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item["row_quote"] = " | ".join(cells[4])
    body = payload([item])
    body.update(report_type="q1", period_end="2025-03-31")
    assert validate_payload(body, document(blocks), blocks).metrics[0].value == Decimal("200000")


def test_foreign_standard_on_a_separate_column_header_row_is_rejected():
    blocks, item = table_case()
    cells = deepcopy(blocks[1].cells)
    cells.insert(1, ["准则", "中国会计准则", "国际财务报告准则"])
    blocks[1] = blocks[1].model_copy(update={
        "cells": cells, "text": "合并利润表；单位：万元。\n" + "\n".join(" | ".join(row) for row in cells)})
    item.update(source_row_index=2, row_quote=" | ".join(cells[2]))
    assert validate(blocks, item).metrics[0].value == Decimal("2000000")
    item.update(raw_value="100.00", source_column_index=2, period_start="2024-01-01", period_end="2024-12-31")
    assert validate(blocks, item).metrics == []


@pytest.mark.parametrize("column,start,end,basis,raw,restated", [
    (5, "2024-01-01", "2024-09-30", "ytd", "83,125,74\n8,346.36", False),
    (3, "2023-07-01", "2023-09-30", "quarter", "23,324,12\n5,769.22", True),
    (7, "2023-01-01", "2023-09-30", "ytd", "48,256,01\n5,730.22", True),
])
def test_real_inspur_split_amount_and_grouped_adjustment_headers_are_verified(column, start, end, basis, raw, restated):
    from pathlib import Path
    from finance_agent.parser import parse_financial_doc

    path = Path(__file__).resolve().parents[1] / "samples/public/浪潮信息_2024第三季度报告.pdf"
    if not path.exists():
        pytest.skip("公开浪潮样本未下载")
    doc = parse_financial_doc(path)
    table = next(block for block in doc.blocks if block.kind == "table" and block.pdf_page == 2)
    company = "浪潮电子信息产业股份有限公司"
    item = {"name": "revenue", "company": company, "raw_value": raw, "unit": "元", "currency": "CNY",
            "period_start": start, "period_end": end, "basis": basis, "scope": "consolidated", "restated": restated,
            "evidence_ids": [table.id], "row_quote": " | ".join(table.cells[2]),
            "source_row_index": 2, "source_column_index": column}
    body = {"company": company, "report_type": "q3", "period_end": "2024-09-30", "metrics": [item]}
    result = validate_payload(body, doc, doc.blocks)
    assert len(result.metrics) == 1, result.warnings
    assert result.metrics[0].value == Decimal(raw.replace("\n", "").replace(",", ""))
    if column in (3, 7):
        item["restated"] = False
        assert validate_payload(body, doc, doc.blocks).metrics == []
