"""模型负责语义提取；程序核验引用、金额和单位，并限制补查次数。"""
from __future__ import annotations

import json
import re
import unicodedata
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .audit import AuditLog
from .llm import DeepSeekClient, ModelError
from .parser import (annual_quarter_column_year, annual_year_column_year,
                     is_consolidated_profit_statement, retrieve_blocks, statement_scopes)
from .schemas import Coverage, Disclosure, Evidence, ExtractedReport, Metric, ParsedDocument

UNIT_FACTORS = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000),
                "百万元": Decimal(1000000), "亿元": Decimal(100000000)}
METRIC_LABELS = {
    "revenue": ("营业收入", "营业总收入"),
    "net_profit": ("净利润",),
    "parent_net_profit": ("归属于", "归母净利润"),
    "adjusted_parent_net_profit": ("扣除非经常性损益", "扣非"),
    "operating_cashflow": ("经营活动产生的现金流量净额", "经营现金流净额"),
    "accounts_receivable": ("应收账款",), "inventory": ("存货",),
    "total_assets": ("资产总额", "资产总计", "总资产"),
    "nonrecurring_total_parent": ("非经常性损益",),
}
COVERAGE_LABELS = {"nonrecurring": ("非经常性损益",),
                   "accounting_change": ("会计政策", "会计估计", "合并范围", "追溯调整"),
                   "cashflow_reason": ("现金流", "经营活动")}
FOREIGN_STANDARDS = re.compile(r"(?:境外|国际)(?:会计准则|准则|财务报告准则)|IFRS|按境外调整", re.I)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).replace("−", "-").replace("－", "-")


def parse_amount(value: str) -> Decimal:
    text = compact(str(value)).replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        raise ValueError("金额不是完整数值")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError("金额非有限值")
    return number


def numeric_tokens(text: str) -> list[Decimal]:
    text = unicodedata.normalize("NFKC", text).replace("−", "-").replace("－", "-")
    tokens = re.findall(r"\(\s*[+-]?\d[\d,]*(?:\.\d+)?\s*\)|[+-]?\d[\d,]*(?:\.\d+)?", text)
    result = []
    for token in tokens:
        try:
            result.append(parse_amount(token))
        except ValueError:
            pass
    return result


def _evidence(ids: list[str], evidence: dict[str, Evidence], document_id: str) -> list[Evidence]:
    if not ids or any(x not in evidence or evidence[x].document_id != document_id for x in ids):
        raise ValueError("引用不存在或属于其他文件")
    expanded = list(dict.fromkeys(ids))
    for eid in list(expanded):
        block = evidence[eid]
        related = block.context_evidence_ids + ([block.header_evidence_id] if block.header_evidence_id else [])
        for related_id in related:
            if related_id in evidence and evidence[related_id].document_id == document_id and related_id not in expanded:
                expanded.append(related_id)
    return [evidence[x] for x in expanded]


def _units(text: str) -> set[str]:
    # 最长单位优先，避免将“万元”误认为“元”。
    normalized = compact(text)
    return set(re.findall(r"(?:单位[:：]?(?:人民币)?[(:：]?)(百万元|千元|万元|亿元|元)", normalized)) | set(
        re.findall(r"\((?:人民币)?(百万元|千元|万元|亿元|元)\)", normalized))


def _label_matches(name: str, text: str, *, context: str = "") -> bool:
    line = compact(text)
    if name == "net_profit":
        # 持续/终止经营净利润与少数股东损益是子项，不能替代总净利润。
        return bool(re.match(r"^(?:(?:[一二三四五六七八九十]+|\d+)[、.])?(?:合并)?净利润(?:\(|[:：|]|$)", line))
    if name == "parent_net_profit":
        return "净利润" in line and any(word in line for word in ("归属于", "归母")) and not any(
            word in line for word in ("扣非", "扣除非经常"))
    if name == "adjusted_parent_net_profit":
        return "净利润" in line and any(word in line for word in ("扣非", "扣除非经常"))
    if name == "nonrecurring_total_parent" and line.startswith("合计"):
        # 只能接受扣除所得税与少数股东影响后的非经常性损益明细表合计。
        full = compact(context)
        return "非经常性损益" in full and "所得税" in full and "少数股东" in full
    if name == "nonrecurring_total_parent":
        explicit_net_parent = "税后" in line and any(word in line for word in ("归母", "归属于", "母公司"))
        adjustment_context = "所得税" in compact(context) and "少数股东" in compact(context)
        return ("非经常性损益" in line and any(word in line for word in ("合计", "总额"))
                and "扣除非经常" not in line and (explicit_net_parent or adjustment_context))
    return any(compact(label) in line for label in METRIC_LABELS.get(name, ()))


def column_header_parts(table: Evidence, row_index: int, column_index: int,
                        by_id: dict[str, Evidence]) -> list[str]:
    """读取指定金额列的期间/调整表头，支持有限的调整前后合并列结构。"""
    parent = by_id.get(table.header_evidence_id or "")
    rows = (parent.cells[:2] if parent else []) + table.cells[:row_index]
    pieces: list[str] = []
    for index, cells in enumerate(rows):
        if column_index >= len(cells):
            continue
        value = compact(cells[column_index])
        next_cells = rows[index + 1] if index + 1 < len(rows) else []
        # 只在上下两行确切形成“上年同期 -> 调整前/调整后”时继承空的
        # 合并列头；不能沿用任意相邻数据列或空金额单元格。
        if (not value and column_index > 0 and column_index < len(next_cells)
                and any(word in compact(next_cells[column_index]) for word in ("调整后", "重述后"))
                and any(word in compact(next_cells[column_index - 1]) for word in ("调整前", "重述前"))):
            previous = compact(cells[column_index - 1])
            if "上年同期" in previous or re.search(r"(?:19|20)\d{2}(?:年|-)", previous):
                value = previous
        if "上年同期" in value and not any(word in value for word in ("年初", "季度", "年度")):
            # 同一摘要可先列单季度、再列累计；基期跟随本组左侧最近的
            # 当前期标题，不能因为同表另外一组出现年初就全部当累计。
            anchors = [compact(cell) for cell in cells[:column_index] if (
                "本报告期" in compact(cell) or "年初至" in compact(cell) or "年初到" in compact(cell))
                and not any(word in compact(cell) for word in ("同比", "增减", "变动", "%", "比例"))]
            if anchors:
                value += " " + anchors[-1]
        if any(word in value for word in ("本期", "本年", "本报告期", "年初", "上年", "上期", "期末", "季度")) or re.search(r"(?:19|20)\d{2}(?:年|-)", value):
            pieces = [value]
        elif any(word in value for word in ("调整", "重述")) and pieces:
            pieces.append(value)
    return pieces


def _check_column_period(headers: str, item: dict, report_end: str, *, context: str,
                         all_headers: str, report_type: str = "unknown", cover_text: str = "") -> None:
    """由列头的实际期间核验日期；不能仅凭同一年份接受另一季度。"""
    end = date.fromisoformat(report_end)
    actual_start = date.fromisoformat(item["period_start"])
    actual_end = date.fromisoformat(item["period_end"])
    basis = item.get("basis")
    full_dates = re.findall(r"((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日", headers)
    full_dates += re.findall(r"((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})", headers)
    explicit_dates = list(dict.fromkeys(date(int(y), int(m), int(d)) for y, m, d in full_dates))
    if explicit_dates:
        if basis == "point" and len(explicit_dates) == 1:
            expected_start = expected_end = explicit_dates[0]
        elif len(explicit_dates) == 2:
            expected_start, expected_end = explicit_dates
            expected_basis = "ytd" if (expected_start.month, expected_start.day) == (1, 1) else "quarter"
            first_quarter = (expected_start.month, expected_start.day, expected_end.month, expected_end.day) == (1, 1, 3, 31)
            if basis != expected_basis and not (first_quarter and basis in ("ytd", "quarter")):
                raise ValueError("完整日期范围与累计/季度口径不匹配")
        else:
            raise ValueError("日期列不能唯一证明所提取期间")
        if (actual_start, actual_end) != (expected_start, expected_end):
            raise ValueError("表格列头完整日期与期间起止不匹配")
        return

    years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?=年|-|$)", headers)
    if len(set(years)) > 1:
        raise ValueError("列头包含多个年度，无法唯一核实期间")
    previous = any(word in headers for word in ("上年", "上期", "去年"))
    quarter_year = annual_quarter_column_year(headers, context, report_end=end,
                                              report_type=report_type, cover_text=cover_text)
    year = int(years[0]) if years else quarter_year if quarter_year is not None else end.year - int(previous)
    if not years and quarter_year is None and not any(word in headers for word in (
            "本期", "本年", "本报告期", "年初", "上年", "上期", "期末")):
        raise ValueError("无法在指定列核实期间，需人工复核或补查")

    if basis == "point":
        if re.search(r"(?:年度?末|年末|上年期末|(?:19|20)\d{2}年期末)", headers):
            expected_end = date(year, 12, 31)
        elif "上期末" in headers:
            raise ValueError("上期末未明确是年末还是上一季末")
        elif "上年同期" in headers or ("本报告期末" in headers or "本期末" in headers):
            expected_end = date(year, end.month, min(end.day, monthrange(year, end.month)[1]))
        else:
            raise ValueError("时点列没有明确期末日期")
        if actual_start != expected_end or actual_end != expected_end:
            raise ValueError("期末列对应时点与提取日期不匹配")
        return

    expected_basis = "ytd"
    first_month, last_month = 1, None
    month_range = re.search(r"(?<!\d)(\d{1,2})月?[-—~～至](\d{1,2})月", headers)
    quarter = re.search(r"第?([一二三四1234])季度", headers)
    if month_range:
        first_month, last_month = map(int, month_range.groups())
        if not 1 <= first_month <= last_month <= 12:
            raise ValueError("列头月份范围无效")
        expected_basis = "ytd" if first_month == 1 else "quarter"
    elif "前三季度" in headers:
        last_month = 9
    elif "半年度" in headers or "上半年" in headers or "前两季度" in headers:
        last_month = 6
    elif quarter:
        quarter_no = int(quarter.group(1)) if quarter.group(1).isdigit() else "一二三四".index(quarter.group(1)) + 1
        first_month, last_month = quarter_no * 3 - 2, quarter_no * 3
        expected_basis = "quarter"
    elif "年度" in headers or (("本年" in headers or "上年" == headers) and end.month == 12):
        last_month = 12
    elif annual_year_column_year(headers, context, report_end=end, report_type=report_type,
                                 cover_text=cover_text) is not None:
        last_month = 12
    elif "年初至" in headers or "年初到" in headers:
        last_month = end.month
    elif "本报告期" in headers:
        if end.month == 9:
            first_month, last_month, expected_basis = 7, 9, "quarter"
        else:
            last_month = end.month
    elif any(word in headers for word in ("本期", "上期", "上年同期")):
        # A 股财务报表的本期/上期发生额是本年/上年同期累计；摘要中的
        # 上年同期必须有其余列头佐证累计或单季，避免把“上期”猜成上一季。
        if ("非经常性损益" in context and "本期金额" in headers
                and "年初" in all_headers and end.month == 9):
            # 非经常性损益明细常把“本期金额”和“年初至期末金额”并列；
            # 这个本期是单季，不能因旁列有“年初”就标成累计。
            first_month, last_month, expected_basis = 7, 9, "quarter"
        elif "年初" in all_headers or "前三季度" in all_headers or "半年度" in all_headers:
            last_month = end.month
        elif "本报告期" in all_headers and "年初" not in all_headers and end.month == 9:
            first_month, last_month, expected_basis = 7, 9, "quarter"
        elif statement_scopes(context) or end.month == 12 or (end.month == 3 and "非经常性损益" in context):
            if previous and "上期末" in headers:
                raise ValueError("上期列期间不明确")
            last_month = end.month
        else:
            raise ValueError("相对期间列缺少累计或单季度佐证")
    if last_month is None:
        raise ValueError("列头只给年度，没有明确完整期间")
    expected_start = date(year, first_month, 1)
    expected_end = date(year, last_month, monthrange(year, last_month)[1])
    first_quarter = (expected_start.month, expected_start.day, expected_end.month, expected_end.day) == (1, 1, 3, 31)
    valid_basis = basis == expected_basis or (first_quarter and basis in ("ytd", "quarter"))
    if (actual_start, actual_end) != (expected_start, expected_end) or not valid_basis:
        raise ValueError("列头累计/季度范围与所提取期间不匹配")


def _check_row_and_column(item: dict, refs: list[Evidence], raw_number: Decimal,
                          report_end: str, *, report_type: str = "unknown", cover_text: str = "") -> None:
    """金额与标签绑定同一原文行；复杂表格再绑定单元格及列头。"""
    quote = item.get("row_quote", "")
    if not quote or len(quote) > 400:
        raise ValueError("需要完整且短的原始指标行")
    context = "\n".join(b.text for b in refs)
    if not _label_matches(item.get("name"), quote, context=context):
        raise ValueError("指标标签不在引用行")
    sources = [b for b in refs if compact(quote) in compact(b.text)]
    if not sources:
        raise ValueError("引用行不属于所引证据")
    table = next((b for b in sources if b.kind == "table" and b.cells), None)
    if table:
        r, c = item.get("source_row_index"), item.get("source_column_index")
        if r is None or c is None or r >= len(table.cells) or c >= len(table.cells[r]):
            raise ValueError("表格金额必须绑定实际行列索引")
        row = table.cells[r]
        if not _label_matches(item.get("name"), row[0], context=context):
            raise ValueError("表格指定行指标不匹配")
        if parse_amount(row[c]) != raw_number:
            raise ValueError("表格指定单元格金额不匹配")
        if item.get("name") == "revenue" and "营业总收入" in compact(row[0]):
            for other_row in table.cells:
                if (other_row and "营业收入" in compact(other_row[0])
                        and "营业总收入" not in compact(other_row[0]) and c < len(other_row)):
                    try:
                        actual_revenue = parse_amount(other_row[c])
                    except ValueError:
                        continue
                    if actual_revenue != raw_number:
                        raise ValueError("营业总收入另含其他收入，不能替代明确披露的营业收入")
        if item.get("name") == "nonrecurring_total_parent":
            label = compact(row[0])
            explicit_net_parent = "税后" in label and any(word in label for word in ("归母", "归属于", "母公司"))
            if not explicit_net_parent:
                tax_rows = [i for i, other_row in enumerate(table.cells)
                            if other_row and "所得税" in compact(other_row[0])]
                minority_rows = [i for i, other_row in enumerate(table.cells)
                                 if other_row and "少数股东" in compact(other_row[0])]
                if not tax_rows or not minority_rows or max(tax_rows + minority_rows) >= r:
                    raise ValueError("非经常性损益合计没有证明在所得税和少数股东扣减之后")
        header_table = next((b for b in refs if b.id == table.header_evidence_id), None)
        header_rows = (header_table.cells[:2] if header_table else []) + table.cells[:r]
        raw_column_headers = [previous[c] for previous in header_rows if c < len(previous)]
        possible_headers = column_header_parts(table, r, c, {b.id: b for b in refs})
        adjustment_headers = compact(" ".join(h for h in possible_headers if "调整" in h or "重述" in h))
        if adjustment_headers:
            before = bool(re.search(r"(?:调整|重述)前", adjustment_headers))
            after = bool(re.search(r"(?:调整|重述)后", adjustment_headers))
            if before == after:
                raise ValueError("调整列未唯一标明调整前还是调整后，需人工复核")
            if item.get("restated", False) is not after:
                raise ValueError("调整前/后列头与restated标记不匹配")
        # 同一个对照表中可以同时列中国与境外准则：只检验选中的指标行和
        # 金额列，不能因为旁边有一列境外数据就排除清楚的中国准则金额。
        table_titles = [previous[0] for previous in table.cells[:r]
                        if previous and not any(cell.strip() for cell in previous[1:])]
        external_titles = [b.text for b in refs if b.kind == "text"
                           and b.id in table.context_evidence_ids]
        if FOREIGN_STANDARDS.search(row[0]) or any(FOREIGN_STANDARDS.search(h) for h in raw_column_headers + possible_headers):
            raise ValueError("该指标行或金额列属于境外准则，当前仅支持中国会计准则")
        if any(FOREIGN_STANDARDS.search(title) and not any(x in compact(title) for x in (
                "中国", "境内", "企业会计准则")) for title in table_titles + external_titles):
            raise ValueError("所选表为境外准则报表，不能混入中国准则指标")
        # 仅使用列头，不能让其他数据行的金额被误读为年份。
        headers = compact(" ".join(possible_headers))
        if any(word in headers for word in ("增减", "变动幅度", "%", "比例")):
            raise ValueError("增长率列不是金额列")
        all_headers = compact(" ".join(cell for previous in header_rows for cell in previous))
        _check_column_period(headers, item, report_end, context=context, all_headers=all_headers,
                             report_type=report_type, cover_text=cover_text)
        scopes = statement_scopes(context)
        if scopes and scopes != {item.get("scope")}:
            raise ValueError("报表标题与合并/母公司口径不匹配或含混")
        if not scopes and item.get("scope") == "parent":
            raise ValueError("母公司单体口径没有对应报表标题佐证")
    else:
        # 无结构化单元格时，只接受原文行直接带明示期间的记录。
        # 不能依赖同页另一段的年份为此数字“证明”期间。
        if len(quote.splitlines()) > 1:
            raise ValueError("文字金额引用须为单一原始指标行，不可混引多行")
        if FOREIGN_STANDARDS.search(quote):
            raise ValueError("文字指标属于境外准则，当前仅支持中国会计准则")
        # 同一行不能混入另一指标，把跨字段金额当成目标数值。
        other_names = {name for name in METRIC_LABELS if name != item.get("name") and _label_matches(name, quote)}
        if other_names:
            raise ValueError("原文行混有多个指标，无法证明标签金额绑定")
        # 年份也出现在原文；必须取标签后的金额，不能把 2025 当作营业收入。
        label_and_amount = re.split(r"[:：|]", quote, maxsplit=1)
        number = re.match(r"\s*(\([+-]?\d[\d,]*(?:\.\d+)?\)|[+-]?\d[\d,]*(?:\.\d+)?)", label_and_amount[-1])
        if len(label_and_amount) != 2 or not _label_matches(item.get("name"), label_and_amount[0], context=context) or not number:
            raise ValueError("文字行金额没有直接绑定指标标签")
        if parse_amount(number.group(1)) != raw_number:
            raise ValueError("文字行标签后金额与候选金额不匹配")
        if item["period_end"] not in quote:
            raise ValueError("文字行没有明确期间，无法证明金额与年度列对应")
        if item["period_start"] not in quote:
            raise ValueError("文字行没有明确开始期间")
        if item.get("basis") == "ytd" and not any(x in quote for x in ("累计", "年度", "全年")):
            raise ValueError("文字行无法核实累计口径")
        if item.get("scope") == "consolidated" and "合并" not in quote:
            raise ValueError("文字行无法核实合并口径")
        if item.get("scope") == "parent" and "母公司" not in quote:
            raise ValueError("文字行无法核实母公司口径")


def _continuous_disclosure_quote(quote: str, refs: list[Evidence], doc: ParsedDocument) -> bool:
    """允许原段落被PDF拆成相邻文本块；不能跳过中间正文或倒序拼接。"""
    target = compact(quote)
    if not target:
        return False
    if any(target in compact(block.text) for block in refs):
        return True
    text_refs = {block.id for block in refs if block.kind == "text"}
    # 依据原文顺序，而不是模型给出的引用顺序。跨页段落保守保留为待核验。
    for page in {block.pdf_page for block in refs}:
        chain = ""
        for block in doc.blocks:
            if block.pdf_page != page or block.kind != "text":
                continue
            if block.id not in text_refs:
                chain = ""
                continue
            chain += compact(block.text)
            if target in chain:
                return True
    return False


def validate_payload(payload: dict[str, Any], doc: ParsedDocument,
                     sent_blocks: list[Evidence]) -> ExtractedReport:
    evidence = {b.id: b for b in sent_blocks}
    warnings: list[str] = []
    metrics: list[Metric] = []
    company = str(payload.get("company", "")).strip()
    all_text = compact("\n".join(b.text for b in sent_blocks))
    if not company or compact(company) not in all_text:
        raise ValueError("模型公司名称不能在所提供原文中核实")
    report_end = date.fromisoformat(payload["period_end"])
    expected_ends = {"annual": (12, 31), "half_year": (6, 30), "q1": (3, 31), "q3": (9, 30)}
    expected_end = expected_ends.get(payload.get("report_type"))
    if expected_end and (report_end.month, report_end.day) != expected_end:
        raise ValueError("报告类型与报告期末不一致")
    first_page = compact("\n".join(b.text for b in sent_blocks if b.pdf_page == 1))
    cover_periods = set(re.findall(r"((?:19|20)\d{2})年?(年度|半年度|第一季度|一季度|第三季度|三季度)报告", first_page))
    if cover_periods and str(report_end.year) not in {year for year, _ in cover_periods}:
        raise ValueError("报告年度与封面披露不一致")
    for index, item in enumerate(payload.get("metrics", [])):
        try:
            item = dict(item)
            refs = _evidence(item.get("evidence_ids", []), evidence, doc.id)
            item["evidence_ids"] = [b.id for b in refs]
            texts = "\n".join(b.text for b in refs)
            raw_number = parse_amount(item["raw_value"])
            # 表格的换行数字必须在具体单元格内还原；拼接整段原文可能
            # 错连不同指标的金额。下方行列核验直接比较真实单元格及符号。
            if not _label_matches(item.get("name"), item.get("row_quote", ""), context=texts):
                raise ValueError("引用没有覆盖指标名称")
            found_units = _units(texts)
            if item.get("unit") not in found_units:
                raise ValueError("引用中找不到明确的对应单位，请同时引用单位表头")
            row_sources = [b for b in refs if compact(item.get("row_quote", "")) in compact(b.text)]
            row_units = _units(item.get("row_quote", ""))
            inherited_header_pages = {b.pdf_page for source in row_sources if source.header_evidence_id
                                      for b in refs if b.id == source.header_evidence_id}
            same_page_units = set().union(*(_units(b.text) for b in refs
                                           if any(b.pdf_page == s.pdf_page for s in row_sources)
                                           or b.pdf_page in inherited_header_pages))
            bound_units = row_units or same_page_units
            if bound_units != {item.get("unit")}:
                raise ValueError("单位不能与数值所在行或同页表头唯一绑定")
            currency_context = compact("\n".join(b.text for b in refs
                if any(b.pdf_page == source.pdf_page for source in row_sources)
                or b.pdf_page in inherited_header_pages))
            if re.search(r"币种[:：]?(?:美元|港币|港元|欧元|日元|USD|HKD|EUR|JPY)|(?:美元|港元|港币|欧元|日元)(?:千元|万元|百万元)?", currency_context, re.I):
                raise ValueError("原表为外币，当前仅支持人民币，不能默认为CNY")
            _check_row_and_column(item, refs, raw_number, payload["period_end"],
                                  report_type=payload.get("report_type", "unknown"), cover_text=first_page)
            item["value"] = raw_number * UNIT_FACTORS[item["unit"]]
            item["status"] = "verified"
            item["checks"] = ["数值及正负号可在原文定位", "金额与指标行绑定", "列头完整期间或原文期间核验", "单位可定位", "程序换算为元"]
            metric = Metric.model_validate(item)
            if metric.company != company:
                raise ValueError("指标所属公司与报告不一致")
            if metric.period_start > metric.period_end:
                raise ValueError("期间起点晚于终点")
            # 日期已按该金额列的完整日期或明确“上年同期”等相对表头核验。
            # 相对表头不一定重复打印基期年份，不能据此再排除已证明的基期。
            if metric.name in ("accounts_receivable", "inventory", "total_assets"):
                if metric.basis != "point" or metric.period_start != metric.period_end:
                    raise ValueError("存量指标必须使用期末时点口径")
            elif metric.basis == "point":
                raise ValueError("流量指标不能使用时点口径")
            if metric.basis == "ytd" and (metric.period_start.month, metric.period_start.day) != (1, 1):
                raise ValueError("累计指标必须从当年1月1日起")
            if metric.basis == "ytd" and metric.period_start.year != metric.period_end.year:
                raise ValueError("累计指标不能跨越两个年度")
            if metric.basis == "quarter":
                start = metric.period_start
                if (start.month not in (1, 4, 7, 10) or start.day != 1
                        or metric.period_end != date(start.year, start.month + 2, monthrange(start.year, start.month + 2)[1])):
                    raise ValueError("单季度指标必须覆盖完整自然季度")
            if metric.name in ("parent_net_profit", "adjusted_parent_net_profit", "nonrecurring_total_parent") and metric.scope != "consolidated":
                raise ValueError("合并净利润与税后归母指标必须使用合并口径")
            if metric.name == "net_profit" and not any(
                    is_consolidated_profit_statement(b.text) if metric.scope == 'consolidated' else
                    re.fullmatch(r"(?:(?:[一二三四五六七八九十]+|\d+)[、.])?母公司利润表", compact(b.text)) for b in refs):
                # 净利润必须来自合并利润表，不允许仅引用摘要中的归母数字。
                raise ValueError("净利润需要同时引用对应合并/母公司利润表标题")
            metrics.append(metric)
        except (ValueError, KeyError, TypeError, ValidationError, InvalidOperation) as exc:
            reason = "数据字段格式不符合约定" if isinstance(exc, ValidationError) else str(exc)
            warnings.append(f"第{index + 1}个候选指标校验失败（{reason}），已排除。")

    grouped: dict[tuple, list[Metric]] = defaultdict(list)
    for metric in metrics:
        key = (metric.name, metric.period_start, metric.period_end, metric.basis,
               metric.scope, metric.currency, metric.restated)
        grouped[key].append(metric)
    metrics = []
    for group in grouped.values():
        if len({m.value for m in group}) > 1:
            warnings.append(f"{group[0].name}同期间同口径存在冲突数值，全部标记待核实。")
            for metric in group:
                metrics.append(metric.model_copy(update={"status": "unverified"}))
        else:
            first = group[0]
            metrics.append(first.model_copy(update={"evidence_ids": list(dict.fromkeys(
                x for m in group for x in m.evidence_ids))}))

    disclosures = []
    for item in payload.get("disclosures", []):
        try:
            disclosure = Disclosure.model_validate(item)
            refs = _evidence(disclosure.evidence_ids, evidence, doc.id)
            disclosure.evidence_ids = [b.id for b in refs]
            if not _continuous_disclosure_quote(disclosure.quote, refs, doc):
                raise ValueError("事件引用不是原文连续片段")
            if (disclosure.kind == "accounting_estimate" and "会计差错更正" in compact(disclosure.quote)
                    and "会计估计" not in compact(disclosure.quote)):
                raise ValueError("会计差错更正不能归类为会计估计变更")
            disclosures.append(disclosure)
        except (ValueError, TypeError, ValidationError):
            warnings.append("一条附注事件无法核实原文或语义冲突，已排除。")
    coverage = []
    for topic in COVERAGE_LABELS:
        entry = next((x for x in payload.get("coverage", []) if x.get("topic") == topic), None)
        try:
            item = Coverage.model_validate(entry) if entry else Coverage(topic=topic, status="missing")
            if item.status == "reviewed":
                refs = _evidence(item.evidence_ids, evidence, doc.id)
                item.evidence_ids = [b.id for b in refs]
                if not any(word in compact("\n".join(b.text for b in refs)) for word in COVERAGE_LABELS[topic]):
                    raise ValueError("章节引用不相关")
            coverage.append(item)
        except (ValueError, TypeError, ValidationError):
            coverage.append(Coverage(topic=topic, status="missing"))
    report = ExtractedReport(
        document_id=doc.id, document_name=doc.name, company=company,
        report_type=payload.get("report_type", "unknown"), period_end=payload["period_end"],
        metrics=metrics, disclosures=disclosures, coverage=coverage, warnings=warnings,
    )
    if str(report.period_end.year) not in all_text:
        raise ValueError("报告年度无法核实")
    return report


def extract_metrics(doc: ParsedDocument, client: DeepSeekClient, audit: AuditLog,
                    progress=None) -> ExtractedReport:
    system = (Path(__file__).resolve().parent.parent / "prompts" / "extract.txt").read_text(encoding="utf-8")
    skill = (Path(__file__).resolve().parent.parent / "prompts" / "domain_skill.txt").read_text(encoding="utf-8")
    system = skill + "\n\n" + system
    audit.record("domain_skill", outputs={"file": "prompts/domain_skill.txt", "constraints": "期间口径、原文证据、输入指令隔离、禁止推测金额"})
    report = None
    failure = ""
    required = {"revenue", "parent_net_profit", "adjusted_parent_net_profit", "net_profit", "operating_cashflow"}

    def quality(candidate: ExtractedReport) -> tuple[int, int, int]:
        verified = [metric for metric in candidate.metrics if metric.status == "verified"]
        current = [metric for metric in verified if metric.period_end == candidate.period_end]
        return (len({metric.name for metric in current} & required), len(current), len(verified))

    for attempt in range(2):
        decisions = {}
        blocks, partial = retrieve_blocks(doc, repair=attempt > 0, diagnostics=decisions)
        audit.record("retrieve_evidence", inputs={"document_id": doc.id, "round": attempt + 1},
                     outputs={"evidence_ids": [b.id for b in blocks], "partial_document": partial,
                              "decisions": decisions, "repair_feedback": failure})
        user = json.dumps({"document": doc.name, "repair_feedback": failure,
                           "evidence": [{"id": b.id, "pdf_page": b.pdf_page, "kind": b.kind,
                                         "text": b.text if b.kind != "table" else "原始表格请见cells，行内容用 | 连接。", "cells": b.cells,
                                         "header_evidence_id": b.header_evidence_id, "context_evidence_ids": b.context_evidence_ids}
                                        for b in blocks]}, ensure_ascii=False)
        try:
            payload = client.complete_json(system, user, task="extract_metrics" if not attempt else "repair_metrics")
        except ModelError:
            if attempt and report is not None and any(metric.status == "verified" for metric in report.metrics):
                # 补查失败不能删除已通过原文核验的数据；未补齐的字段继续
                # 保持缺失。本层日志使用固定原因，不记录请求头或密钥。
                audit.record("repair_metrics", outputs={"document_id": doc.id,
                    "error": "model_repair_unavailable", "kept_verified_metrics": sum(
                        metric.status == "verified" for metric in report.metrics)}, status="failed")
                report.warnings.append("定向补查未完成，已保留第一轮通过原文核验的指标；缺失或冲突字段不参与计算。")
                failure = ""
                break
            raise
        try:
            candidate = validate_payload(payload, doc, blocks)
            validation_warnings = list(candidate.warnings)
            audit.record("validate_metrics", outputs=candidate.model_dump(mode="json"))
            # 第二次完整提取替换第一次结果，避免把两次互相矛盾的答案拼在一起。
            if report is None or quality(candidate) >= quality(report):
                report = candidate
            if partial:
                note = "本次仅检索相关页面，未完整审阅所有附注；未发现信号不等于全报告无异常。"
                if note not in report.warnings:
                    report.warnings.append(note)
            current = {m.name for m in candidate.metrics if m.period_end == candidate.period_end and m.status == "verified"}
            if required <= current and not validation_warnings:
                failure = ""
                break
            failure = "缺少当前期核心指标：" + ", ".join(sorted(required - current)) + "。" + "；".join(validation_warnings)
        except (ValueError, ValidationError, KeyError, TypeError) as exc:
            audit.exception("validation_failure", exc, context={"document_id": doc.id, "round": attempt + 1})
            failure = "首次返回结构或公司/期间不可靠。重新依据引用原文按规定JSON结构提取。"
            audit.record("validate_metrics", outputs={"document_id": doc.id, "error": failure}, status="rejected")
        if attempt == 0 and progress:
            progress(f"{doc.name}：发现缺失或校验问题，正在定向补查一次。")
    if report is None:
        raise ValueError(f"{doc.name} 的提取结果无法核验，请换用清晰文字版报告。")
    if failure:
        report.warnings.append("有限补查完成；仍缺失或冲突的指标不会进入计算。")
    return report
