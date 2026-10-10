"""年报期间、正式报表检索与跨页上下文的回归。"""
from datetime import date
from pathlib import Path

import pytest

from finance_agent.parser import (annual_quarter_column_year, annual_year_column_year,
                                  parse_financial_doc, retrieve_blocks)
from finance_agent.schemas import Evidence, ParsedDocument


@pytest.mark.parametrize("context", ["主要会计数据 | 2025年 | 2024年\n营业收入 | 100 | 90",
                                    "合并利润表\n单位：元\n项目 | 2025年 | 2024年",
                                    "合并现金流量表\n2025年1—12月\n单位：元",
                                    "非经常性损益项目 | 2025年金额 | 2024年金额"])
def test_annual_year_helper_requires_known_annual_table(context):
    assert annual_year_column_year("2024年", context, report_end=date(2025, 12, 31),
                                   report_type="annual", cover_text="测试公司2025年年度报告") == 2024


@pytest.mark.parametrize("context", ["主要会计数据\n第一季度（1-3月份）", "合并利润表\n2025年1—9月",
                                    "会计政策说明提到合并利润表", "项目 | 2025年 | 2024年"])
def test_annual_cover_does_not_turn_short_periods_or_notes_into_full_years(context):
    assert annual_year_column_year("2024年", context, report_end=date(2025, 12, 31),
                                   report_type="annual", cover_text="2025年年度报告") is None


@pytest.mark.parametrize("header", ["第一季度（1-3月份）", "第二季度（4-6月）", "第三季度", "第四季度（10-12月份）"])
def test_annual_quarter_header_gets_year_only_from_its_explicit_same_year_table_title(header):
    assert annual_quarter_column_year(header, "九、2025 年分季度主要财务数据\n单位：元", report_end=date(2025, 12, 31),
                                      report_type="annual", cover_text="2025年度报告") == 2025


@pytest.mark.parametrize("header,context", [
    ("第三季度（1-9月份）", "九、2025年分季度主要财务数据"),
    ("第三季度", "九、2024年分季度主要财务数据"),
    ("第三季度", "九、2025年分季度主要财务数据\n2024年分季度主要财务数据"),
    ("第三季度", "这份2025年年报提到分季度主要财务数据"),
    ("第三季度", "单位：元"),
])
def test_quarter_table_year_cannot_be_guessed_from_the_report_year(header, context):
    assert annual_quarter_column_year(header, context, report_end=date(2025, 12, 31),
                                      report_type="annual", cover_text="2025年度报告") is None


def make_block(eid, page, text, **kwargs):
    return Evidence(id=eid, document_id="doc", document_name="report.pdf", pdf_page=page,
                    text=text, **kwargs)


def test_retrieval_prioritizes_real_consolidated_statement_title_and_its_continuation():
    blocks = [make_block("cover", 1, "测试公司2025年年度报告")]
    for page in range(2, 25):
        blocks.append(make_block(f"note{page}", page, "合并利润表和合并现金流量表的会计政策说明。" * 8))
    blocks += [make_block("title", 50, "合并利润表"),
               make_block("table", 50, "项目 | 2025年度 | 2024年度\n营业收入 | 100 | 90",
                          kind="table", cells=[["项目", "2025年度", "2024年度"], ["营业收入", "100", "90"]],
                          context_evidence_ids=["title"]),
               make_block("continuation", 51, "净利润 | 20 | 15", kind="table", cells=[["净利润", "20", "15"]],
                          header_evidence_id="table", context_evidence_ids=["title"])]
    doc = ParsedDocument(id="doc", name="report.pdf", sha256="0" * 64, page_count=51, blocks=blocks)
    retrieved, _ = retrieve_blocks(doc, max_chars=1600)
    ids = {b.id for b in retrieved}
    assert {"cover", "title", "table", "continuation"} <= ids


@pytest.mark.parametrize("filename,start,end,cash", [
    ("伊利_组员财报_2025发布.pdf", 91, 92, 94),
    ("伊利_组员财报_2026发布.pdf", 86, 87, 89),
])
def test_real_yili_annual_first_retrieval_includes_the_consolidated_statement_pages(filename, start, end, cash):
    source = Path(__file__).resolve().parents[1] / "samples/member/yili" / filename
    if not source.exists():
        pytest.skip("本地组员真实样本未随源码提供")
    doc = parse_financial_doc(source)
    retrieved, _ = retrieve_blocks(doc)
    tables = [b for b in retrieved if b.kind == "table"]
    assert {start, end, cash} <= {b.pdf_page for b in tables}
    nonrecurring = next(b for b in doc.blocks if b.kind == "table" and b.pdf_page == 8
                        and b.cells and b.cells[0][0] == "非经常性损益项目")
    by_id = {b.id: b for b in doc.blocks}
    context = "\n".join(by_id[eid].text for eid in nonrecurring.context_evidence_ids)
    assert "第一季度" not in context, "非经常表不应继承同页上方季度表的期间"
    continuation = next(b for b in doc.blocks if b.kind == "table" and b.pdf_page == end)
    assert continuation.header_evidence_id
    assert "合并利润表" in "\n".join(by_id[eid].text for eid in continuation.context_evidence_ids)
    quarter = next(b for b in doc.blocks if b.kind == "table" and b.pdf_page == 8
                   and b.cells and "第一季度" in " ".join(b.cells[0]))
    assert any("分季度主要财务数据" in by_id[eid].text for eid in quarter.context_evidence_ids)
