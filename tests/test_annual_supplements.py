"""年度原表补查：公开数据独立参考只作断言，产品算法不读取参考答案。"""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from finance_agent.annual_supplements import supplement_annual_tables
from finance_agent.extraction import validate_payload
from finance_agent.llm import DeepSeekClient
from finance_agent.schemas import Evidence, ParsedDocument


ROOT = Path(__file__).resolve().parents[1]
COMPANY = "测试食品股份有限公司"
LABELS = {"revenue": "营业收入", "parent_net_profit": "归属于上市公司股东的净利润",
          "adjusted_parent_net_profit": "归属于上市公司股东的扣除非经常性损益的净利润",
          "operating_cashflow": "经营活动产生的现金流量净额"}


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Annual supplements must not call a model")
    monkeypatch.setattr(DeepSeekClient, "complete_json", fail)


def fixture(*, name="parent_net_profit", quarters=("10.00", "20.00", "30.00", "-5.00"),
            annual="55.00", unit="元"):
    blocks = []

    def text(identifier, page, content):
        block = Evidence(id=identifier, document_id="doc", document_name="测试年报.pdf",
                         pdf_page=page, text=content)
        blocks.append(block)
        return block

    def table(identifier, page, cells, refs):
        block = Evidence(id=identifier, document_id="doc", document_name="测试年报.pdf",
                         pdf_page=page, kind="table", cells=cells,
                         text="\n".join(" | ".join(row) for row in cells), context_evidence_ids=refs)
        blocks.append(block)
        return block

    text("cover", 1, COMPANY + "2024年年度报告")
    text("annual_title", 2, "主要会计数据")
    text("annual_unit", 2, f"单位：{unit} 币种：人民币")
    annual_table = table("annual", 2, [["项目", "2024年度"], [LABELS[name], annual]],
                         ["annual_title", "annual_unit"])
    text("quarter_title", 3, "九、2024年分季度主要财务数据")
    text("quarter_unit", 3, f"单位：{unit} 币种：人民币")
    quarter_table = table("quarter", 3,
        [["项目", "第一季度（1-3月份）", "第二季度（4-6月份）", "第三季度（7-9月份）", "第四季度（10-12月份）"],
         [LABELS[name], *quarters]], ["quarter_title", "quarter_unit"])
    text("asset_title", 4, "合并资产负债表")
    text("asset_unit", 4, f"单位：{unit} 币种：人民币")
    asset_table = table("assets", 4,
        [["项目", "2024年12月31日", "2023年12月31日"], ["应收账款", "10.00", "8.00"],
         ["存货", "40.00", "30.00"], ["资产总计", "100.00", "80.00"]],
        ["asset_title", "asset_unit"])
    doc = ParsedDocument(id="doc", name="测试年报.pdf", sha256="0" * 64, page_count=4, blocks=blocks)
    metrics = []
    for table_block, row, metric_name, start, basis in (
            (annual_table, 1, name, "2024-01-01", "ytd"),
            (asset_table, 3, "total_assets", "2024-12-31", "point")):
        metrics.append(dict(name=metric_name, company=COMPANY, value="0", raw_value=table_block.cells[row][1],
            unit=unit, period_start=start, period_end="2024-12-31", basis=basis, scope="consolidated",
            evidence_ids=[table_block.id], row_quote=" | ".join(table_block.cells[row]),
            source_row_index=row, source_column_index=1))
    report = validate_payload(dict(company=COMPANY, report_type="annual", period_end="2024-12-31",
                                  metrics=metrics), doc, blocks)
    assert len(report.metrics) == 2
    return report, doc, quarter_table, asset_table


def update_table(table):
    table.text = "\n".join(" | ".join(row) for row in table.cells)


def quarter_metrics(report):
    return [metric for metric in report.metrics if metric.basis == "quarter"]


def balances(report):
    return [metric for metric in report.metrics if metric.name in ("accounts_receivable", "inventory")]


@pytest.mark.parametrize("name", list(LABELS))
def test_complete_quarter_table_reconciles_to_annual_anchor_and_keeps_real_negative_sign(name):
    report, doc, _, _ = fixture(name=name)
    before = report.model_dump_json()
    result = supplement_annual_tables(report, doc)
    quarters = quarter_metrics(result)
    assert len(quarters) == 4
    assert [metric.value for metric in quarters] == list(map(Decimal, ("10", "20", "30", "-5")))
    assert [(metric.period_start, metric.period_end) for metric in quarters] == [
        (date(2024, 1, 1), date(2024, 3, 31)), (date(2024, 4, 1), date(2024, 6, 30)),
        (date(2024, 7, 1), date(2024, 9, 30)), (date(2024, 10, 1), date(2024, 12, 31))]
    assert all(metric.status == "verified" and metric.source_row_index == 1 for metric in quarters)
    assert [metric.source_column_index for metric in quarters] == [1, 2, 3, 4]
    assert all("quarter_title" in metric.evidence_ids for metric in quarters)
    assert all(any("四季金额之和" in check for check in metric.checks) for metric in quarters)
    assert report.model_dump_json() == before
    assert supplement_annual_tables(result, doc).metrics == result.metrics


@pytest.mark.parametrize("unit,factor", [("元", 1), ("千元", 1000), ("万元", 10000), ("百万元", 1000000), ("亿元", 100000000)])
def test_quarter_and_balance_cells_use_same_explicit_unit_as_verified_anchor(unit, factor):
    report, doc, _, _ = fixture(unit=unit)
    result = supplement_annual_tables(report, doc)
    assert quarter_metrics(result)[3].value == Decimal(-5 * factor)
    assert {metric.name: metric.value for metric in balances(result)} == {
        "accounts_receivable": Decimal(10 * factor), "inventory": Decimal(40 * factor)}


@pytest.mark.parametrize("problem", ["no_anchor", "unverified", "wrong_company", "parent_scope",
                                      "wrong_year", "wrong_basis", "anchor_conflict"])
def test_quarter_table_requires_unique_verified_same_company_annual_anchor(problem):
    report, doc, _, _ = fixture()
    seed = report.metrics[0]
    if problem == "no_anchor":
        report.metrics.remove(seed)
    elif problem == "unverified":
        seed.status = "unverified"
    elif problem == "wrong_company":
        seed.company = "其他公司"
    elif problem == "parent_scope":
        seed.scope = "parent"
    elif problem == "wrong_year":
        seed.period_end = date(2023, 12, 31)
    elif problem == "wrong_basis":
        seed.basis = "point"
    else:
        report.metrics.append(seed.model_copy(update={"value": Decimal(54)}))
    result = supplement_annual_tables(report, doc)
    assert not quarter_metrics(result)
    assert any("锚点" in warning for warning in result.warnings)


@pytest.mark.parametrize("problem", ["not_annual", "not_year_end", "cover_year", "cover_type", "cover_missing",
                                      "cover_company", "quarter_title_year", "quarter_title_missing"])
def test_annual_and_bound_same_year_quarter_title_are_mandatory(problem):
    report, doc, table, _ = fixture()
    by_id = {block.id: block for block in doc.blocks}
    if problem == "not_annual":
        report.report_type = "q3"
    elif problem == "not_year_end":
        report.period_end = date(2024, 9, 30)
    elif problem == "cover_year":
        by_id["cover"].text = COMPANY + "2023年年度报告"
    elif problem == "cover_type":
        by_id["cover"].text = COMPANY + "2024年半年度报告"
    elif problem == "cover_missing":
        by_id["cover"].text = COMPANY
    elif problem == "cover_company":
        by_id["cover"].text = "其他公司2024年年度报告"
    elif problem == "quarter_title_year":
        by_id["quarter_title"].text = "九、2023年分季度主要财务数据"
    else:
        table.context_evidence_ids.remove("quarter_title")
    assert not quarter_metrics(supplement_annual_tables(report, doc))


@pytest.mark.parametrize("problem", ["missing_cell", "dash_cell", "bad_sign", "wrong_sum", "missing_quarter",
                                      "duplicated_quarter", "mismatched_months", "rate_column", "different_unit",
                                      "multiple_units", "parent_table", "restated_without_column"])
def test_quarter_cells_require_four_complete_unambiguous_columns_and_reconciliation(problem):
    report, doc, table, _ = fixture()
    by_id = {block.id: block for block in doc.blocks}
    if problem == "missing_cell":
        table.cells[1][4] = ""
    elif problem == "dash_cell":
        table.cells[1][4] = "—"
    elif problem == "bad_sign":
        table.cells[1][4] = "+-5.00"
    elif problem == "wrong_sum":
        table.cells[1][4] = "5.00"
    elif problem == "missing_quarter":
        table.cells[0][4] = "本期"
    elif problem == "duplicated_quarter":
        table.cells[0][4] = "第三季度"
    elif problem == "mismatched_months":
        table.cells[0][4] = "第四季度（7-9月份）"
    elif problem == "rate_column":
        table.cells[0][4] = "第四季度同比增长率（%）"
    elif problem == "different_unit":
        by_id["quarter_unit"].text = "单位：万元 币种：人民币"
    elif problem == "multiple_units":
        by_id["quarter_unit"].text = "单位：元 单位：万元 币种：人民币"
    elif problem == "parent_table":
        by_id["quarter_title"].text += "\n母公司利润表"
        table.context_evidence_ids.append("asset_title")
    else:
        report.metrics[0].restated = True
    update_table(table)
    result = supplement_annual_tables(report, doc)
    assert not quarter_metrics(result)


@pytest.mark.parametrize("annual,accept", [("55.01", True), ("55.02", True), ("55.03", False)])
def test_reconciliation_tolerance_is_derived_from_original_decimal_precision(annual, accept):
    report, doc, _, _ = fixture(annual=annual)
    result = supplement_annual_tables(report, doc)
    assert bool(quarter_metrics(result)) == accept


def test_quarter_duplicate_rows_with_different_distribution_are_not_selected_by_order():
    report, doc, table, _ = fixture()
    table.cells.append([LABELS["parent_net_profit"], "11.00", "19.00", "30.00", "-5.00"])
    update_table(table)
    result = supplement_annual_tables(report, doc)
    assert not quarter_metrics(result)
    assert any("冲突" in warning for warning in result.warnings)


def test_balance_rows_are_read_from_same_consolidated_table_with_assets_anchor():
    report, doc, _, _ = fixture()
    result = supplement_annual_tables(report, doc)
    added = balances(result)
    assert [metric.name for metric in added] == ["accounts_receivable", "inventory"]
    assert [metric.value for metric in added] == [Decimal(10), Decimal(40)]
    assert [metric.source_row_index for metric in added] == [1, 2]
    assert all(metric.source_column_index == 1 and metric.period_start == date(2024, 12, 31)
               and metric.period_end == date(2024, 12, 31) and metric.status == "verified" for metric in added)


@pytest.mark.parametrize("problem", ["no_anchor", "unverified", "mismatched_total", "parent_table", "no_title",
                                      "ambiguous_unit", "wrong_date", "bare_year", "different_currency"])
def test_balance_table_requires_verified_assets_anchor_scope_unit_currency_and_point_column(problem):
    report, doc, _, table = fixture()
    by_id = {block.id: block for block in doc.blocks}
    seed = report.metrics[1]
    if problem == "no_anchor":
        report.metrics.remove(seed)
    elif problem == "unverified":
        seed.status = "unverified"
    elif problem == "mismatched_total":
        table.cells[3][1] = "101.00"
    elif problem == "parent_table":
        by_id["asset_title"].text = "母公司资产负债表"
    elif problem == "no_title":
        by_id["asset_title"].text = "资产负债表附注：应收账款"
    elif problem == "ambiguous_unit":
        by_id["asset_unit"].text = "单位：元 单位：万元"
    elif problem == "wrong_date":
        table.cells[0][1] = "2023年12月31日"
    elif problem == "bare_year":
        table.cells[0][1] = "2024年"
    else:
        by_id["asset_unit"].text = "单位：元 币种：美元"
    update_table(table)
    assert not balances(supplement_annual_tables(report, doc))


def test_balance_note_rows_and_rounded_ratios_cannot_replace_statement_amounts():
    report, doc, _, table = fixture()
    table.cells[1][0] = "应收账款坏账准备"
    table.cells[2][0] = "存货跌价准备"
    update_table(table)
    assert not balances(supplement_annual_tables(report, doc))


def test_explicit_parser_header_link_allows_cross_page_balance_rows():
    report, doc, _, table = fixture()
    continuation = table.model_copy(deep=True, update={"id": "assets_next", "pdf_page": 5,
        "cells": [table.cells[2]], "header_evidence_id": table.id,
        "context_evidence_ids": list(table.context_evidence_ids)})
    table.cells.pop(2)
    update_table(table)
    update_table(continuation)
    doc.blocks.append(continuation)
    doc.page_count = 5
    result = supplement_annual_tables(report, doc)
    assert len(balances(result)) == 2
    inventory = next(metric for metric in balances(result) if metric.name == "inventory")
    assert inventory.source_row_index == 0 and "assets_next" in inventory.evidence_ids
    assert "assets" in inventory.evidence_ids


@pytest.mark.parametrize("problem", ["no_link", "bad_link", "foreign_document", "not_next_period", "cycle"])
def test_adjacent_or_unrelated_balance_table_is_not_assumed_to_be_same_statement(problem):
    report, doc, _, table = fixture()
    continuation = table.model_copy(deep=True, update={"id": "assets_next", "pdf_page": 5,
        "cells": [table.cells[2]], "header_evidence_id": table.id,
        "context_evidence_ids": list(table.context_evidence_ids)})
    table.cells.pop(2)
    update_table(table)
    if problem == "no_link":
        continuation.header_evidence_id = None
    elif problem == "bad_link":
        continuation.header_evidence_id = "missing"
    elif problem == "foreign_document":
        continuation.document_id = "other"
    elif problem == "not_next_period":
        continuation.pdf_page = 3
    else:
        continuation.header_evidence_id = continuation.id
    update_table(continuation)
    doc.blocks.append(continuation)
    result = supplement_annual_tables(report, doc)
    assert not any(metric.name == "inventory" for metric in balances(result))


def test_conflicting_balance_rows_remain_missing_and_existing_metric_is_not_overwritten():
    report, doc, _, table = fixture()
    table.cells.append(["应收账款", "11.00", "8.00"])
    update_table(table)
    result = supplement_annual_tables(report, doc)
    assert [metric.name for metric in balances(result)] == ["inventory"]
    assert any("冲突" in warning for warning in result.warnings)


def test_annual_supplement_rejects_another_source_document():
    report, doc, _, _ = fixture()
    report.document_id = "other"
    with pytest.raises(ValueError, match="同一来源"):
        supplement_annual_tables(report, doc)


def summary_fixture():
    report, doc, quarter, _ = fixture(name="revenue")
    report.metrics = [report.metrics[0]]
    table = next(block for block in doc.blocks if block.id == "annual")
    table.cells = [["主要会计数据", "2024年", "2023年", "同比增减（%）"],
                   ["营业收入", "55.00", "50.00", "10"],
                   ["经营活动产生的现金流量净额", "110.00", "100.00", "10"],
                   ["", "2024年末", "2023年末", "期末增减（%）"],
                   ["总资产", "100.00", "80.00", "25"]]
    update_table(table)
    quarter.cells.append([LABELS["operating_cashflow"], "20.00", "40.00", "60.00", "-10.00"])
    update_table(quarter)
    return report, doc, table


def test_summary_current_revenue_anchor_recovers_cashflow_assets_and_enables_downstream_tables():
    report, doc, _ = summary_fixture()
    result = supplement_annual_tables(report, doc)
    cash = next(metric for metric in result.metrics if metric.name == "operating_cashflow" and metric.basis == "ytd")
    asset = next(metric for metric in result.metrics if metric.name == "total_assets")
    assert cash.value == Decimal(110) and cash.source_row_index == 2 and cash.source_column_index == 1
    assert asset.value == Decimal(100) and asset.source_row_index == 4 and asset.basis == "point"
    assert len(quarter_metrics(result)) == 8
    assert len(balances(result)) == 2
    assert not any(metric.period_end.year == 2023 for metric in result.metrics)
    assert all(metric.status == "verified" for metric in result.metrics)


@pytest.mark.parametrize("problem", ["no_anchor", "unverified", "wrong_anchor_amount", "prior_anchor",
                                      "no_summary_label", "note_title", "rate_column", "ambiguous_current_column",
                                      "parent_scope", "foreign_currency", "different_unit"])
def test_summary_requires_explicit_main_data_table_same_column_annual_anchor_and_unit(problem):
    report, doc, table = summary_fixture()
    by_id = {block.id: block for block in doc.blocks}
    if problem == "no_anchor":
        report.metrics.clear()
    elif problem == "unverified":
        report.metrics[0].status = "unverified"
    elif problem == "wrong_anchor_amount":
        table.cells[1][1] = "56.00"
    elif problem == "prior_anchor":
        report.metrics[0].period_start = date(2023, 1, 1)
        report.metrics[0].period_end = date(2023, 12, 31)
    elif problem == "no_summary_label":
        table.cells[0][0] = "主要财务指标"
    elif problem == "note_title":
        table.cells[0][0] = "附注主要会计数据"
    elif problem == "rate_column":
        table.cells[0][1] = "2024年增长率（%）"
    elif problem == "ambiguous_current_column":
        table.cells[0][2] = "2024年"
        table.cells[1][2] = "55.00"
    elif problem == "parent_scope":
        by_id["annual_title"].text = "母公司利润表"
    elif problem == "foreign_currency":
        by_id["annual_unit"].text = "单位：元 币种：美元"
    else:
        by_id["annual_unit"].text = "单位：万元 币种：人民币"
    update_table(table)
    result = supplement_annual_tables(report, doc)
    assert not any(metric.name in ("operating_cashflow", "total_assets") for metric in result.metrics)
    assert not balances(result)


@pytest.mark.parametrize("problem", ["wrong_point_period", "missing_amount", "cash_ratio_row", "cash_budget_row"])
def test_summary_target_row_must_be_original_amount_and_correct_flow_or_stock_period(problem):
    report, doc, table = summary_fixture()
    if problem == "wrong_point_period":
        table.cells[3][1] = "2024年"
    elif problem == "missing_amount":
        table.cells[2][1] = "—"
    elif problem == "cash_ratio_row":
        table.cells[2][0] = "经营活动产生的现金流量净额占营业收入比例"
    else:
        table.cells[2][0] = "经营活动产生的现金流量净额预算"
    update_table(table)
    result = supplement_annual_tables(report, doc)
    rejected = "total_assets" if problem == "wrong_point_period" else "operating_cashflow"
    assert not any(metric.name == rejected for metric in result.metrics)


def test_summary_recovery_uses_correct_original_row_and_keeps_model_wrong_row_rejection():
    report, doc, table = summary_fixture()
    item = report.metrics[0].model_dump(mode="json")
    item.update(name="operating_cashflow", raw_value="110.00", evidence_ids=[table.id],
                row_quote=" | ".join(table.cells[2]), source_row_index=1, source_column_index=1)
    rejected = validate_payload(dict(company=COMPANY, report_type="annual", period_end="2024-12-31",
                                      metrics=[item]), doc, doc.blocks)
    assert not rejected.metrics and any("指定行指标不匹配" in warning for warning in rejected.warnings)
    report.warnings.extend(rejected.warnings)
    result = supplement_annual_tables(report, doc)
    cash = next(metric for metric in result.metrics if metric.name == "operating_cashflow" and metric.basis == "ytd")
    assert cash.source_row_index == 2 and cash.raw_value == table.cells[2][1]
    assert set(rejected.warnings).issubset(result.warnings)


def test_summary_conflicting_rows_are_not_resolved_by_order_or_used_as_quarter_anchor():
    report, doc, table = summary_fixture()
    table.cells.insert(3, [LABELS["operating_cashflow"], "111.00", "100.00", "11"])
    update_table(table)
    result = supplement_annual_tables(report, doc)
    assert not any(metric.name == "operating_cashflow" for metric in result.metrics)
    assert any("冲突" in warning for warning in result.warnings)


def test_existing_conflicting_verified_amount_is_kept_with_explicit_warning():
    report, doc, table = summary_fixture()
    existing = report.metrics[0].model_copy(update={"name": "operating_cashflow", "value": Decimal(109),
                                                  "raw_value": "109.00"})
    report.metrics.append(existing)
    result = supplement_annual_tables(report, doc)
    cash = [metric for metric in result.metrics if metric.name == "operating_cashflow" and metric.basis == "ytd"]
    assert len(cash) == 1 and cash[0].value == Decimal(109)
    assert not any(metric.name == "operating_cashflow" and metric.basis == "quarter" for metric in result.metrics)
    assert any("已有记录冲突" in warning for warning in result.warnings)


@pytest.mark.parametrize("suffix", ["同比增长率", "占比", "预算", "变动额"])
def test_quarter_ratio_and_other_derived_rows_are_not_accepted_even_when_numbers_coincidentally_sum(suffix):
    report, doc, table, _ = fixture(name="revenue")
    table.cells[1][0] += suffix
    update_table(table)
    assert not quarter_metrics(supplement_annual_tables(report, doc))


@pytest.fixture(scope="module")
def real_yili():
    trace = ROOT / "output/member_yili/20261009T053605Z_3063c786/trace.jsonl"
    reference = ROOT / "output/yili_validation_20261009/manual_reference.json"
    if not trace.is_file() or not reference.is_file():
        pytest.skip("Real original Yili run and independently locked reference are not present")
    docs, reports = {}, {}
    active, sent = None, []
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["tool"] == "parse_financial_doc":
            doc = ParsedDocument.model_validate(event["output"])
            docs[doc.id] = doc
        elif event["tool"] == "retrieve_evidence":
            active = docs[event["input"]["document_id"]]
            by_id = {block.id: block for block in active.blocks}
            sent = [by_id[identifier] for identifier in event["output"]["evidence_ids"]]
        elif event["tool"] == "model_response" and active is not None:
            checked = validate_payload(json.loads(event["output"]["content"]), active, sent)
            previous = reports.get(active.id)
            if previous is None or len(checked.metrics) > len(previous.metrics):
                reports[active.id] = checked
    # The manual reference is read only after replaying original model outputs.
    expected = json.loads(reference.read_text(encoding="utf-8"))["records"]
    return docs, reports, expected


@pytest.mark.parametrize("identifier", ["9c4f934fd1253ae8", "412c9bf0ad96d7f8"])
def test_real_yili_quarters_and_balance_lines_match_independent_reference(real_yili, identifier):
    docs, reports, expected = real_yili
    report, doc = reports[identifier], docs[identifier]
    result = supplement_annual_tables(report, doc)
    quarters = quarter_metrics(result)
    assert len(quarters) == 16
    relevant = [row for row in expected if row["document_id"] == identifier
                and (row["basis"] == "quarter" or (row["metric"] in ("accounts_receivable", "inventory")
                    and row["period_end"] == report.period_end.isoformat()))]
    assert len(relevant) == 18
    for row in relevant:
        found = [metric for metric in result.metrics if metric.name == row["metric"]
                 and metric.basis == row["basis"] and metric.scope == row["scope"]
                 and metric.period_start.isoformat() == row["period_start"]
                 and metric.period_end.isoformat() == row["period_end"]]
        assert len(found) == 1 and found[0].status == "verified"
        assert found[0].value == Decimal(row["value_yuan"])
        block = next(block for block in doc.blocks if block.id in found[0].evidence_ids
                     and block.kind == "table" and block.pdf_page == row["pdf_page"])
        assert found[0].raw_value == block.cells[found[0].source_row_index][found[0].source_column_index]
    assert len(result.metrics) - len(report.metrics) == 18


def test_second_real_run_wrong_model_rows_stay_rejected_while_raw_summary_restores_missing_cells():
    from finance_agent.comparatives import supplement_comparatives
    from finance_agent.schemas import RunResult

    path = ROOT / "output/member_yili/20261009T054845Z_8a0979fc/result.json"
    reference = ROOT / "output/yili_validation_20261009/manual_reference.json"
    if not path.is_file() or not reference.is_file():
        pytest.skip("Second real Yili run and independently locked reference are not present")
    saved = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
    expected = json.loads(reference.read_text(encoding="utf-8"))["records"]
    report = next(report for report in saved.reports if report.period_end.year == 2024)
    doc = ParsedDocument(id=report.document_id, name=report.document_name, sha256="0" * 64,
        page_count=max(block.pdf_page for block in saved.evidence.values() if block.document_id == report.document_id),
        blocks=[block for block in saved.evidence.values() if block.document_id == report.document_id])
    original = report.model_dump_json()
    result = supplement_comparatives(report, doc)
    assert report.model_dump_json() == original
    assert set(report.warnings).issubset(result.warnings)
    relevant = [row for row in expected if row["document_id"] == report.document_id]
    assert len(relevant) == 34
    for row in relevant:
        found = [metric for metric in result.metrics if metric.name == row["metric"]
                 and metric.basis == row["basis"] and metric.scope == row["scope"]
                 and metric.period_start.isoformat() == row["period_start"]
                 and metric.period_end.isoformat() == row["period_end"]]
        assert len(found) == 1 and found[0].status == "verified"
        assert found[0].value == Decimal(row["value_yuan"])
