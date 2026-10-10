"""从年报正式原表补查季度金额、应收账款和存货，不使用模型或人工答案。

季度金额必须四季完整并与已核验年度金额勾稽；资产行必须与同一合并
资产负债表的本期资产总计锚点绑定。每个新增单元格仍经过统一原文校验。
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal
import re

from .extraction import UNIT_FACTORS, _units, compact, parse_amount, validate_payload
from .parser import statement_scopes
from .schemas import Evidence, ExtractedReport, Metric, ParsedDocument


QUARTER_METRICS = ("revenue", "parent_net_profit", "adjusted_parent_net_profit", "operating_cashflow")
BALANCE_METRICS = {"应收账款": "accounts_receivable", "存货": "inventory"}
_PREFIX = r"(?:(?:[一二三四五六七八九十百]+|\d+)[、.．）)]?)?"


def _flow_label(name: str, label: str) -> bool:
    """只认金额项目整行标签，排除同比率、比例、预算和附注子项。"""
    labels = {
        "revenue": r"(?:营业收入|营业总收入)",
        "parent_net_profit": r"(?:归属于(?:上市公司|母公司|公司)(?:股东|所有者)?的?净利润|归母净利润)",
        "adjusted_parent_net_profit": r"(?:(?:归属于(?:上市公司|母公司|公司)(?:股东|所有者)?的?)?扣除非经常(?:性)?损益(?:后)?的净利润|扣非归母净利润|扣非净利润)",
        "operating_cashflow": r"(?:经营活动产生的现金流量净额|经营现金流净额)",
    }
    amount_unit = r"(?:[（(](?:人民币)?(?:百万元|千元|万元|亿元|元)[）)])?"
    pattern = labels.get(name)
    return bool(pattern and re.fullmatch(_PREFIX + pattern + amount_unit, compact(label)))


def _key(metric: Metric) -> tuple:
    return (metric.name, metric.company, metric.period_start, metric.period_end,
            metric.basis, metric.scope, metric.currency, metric.restated)


def _rounding_margin(raw: str, unit: str) -> Decimal:
    """一个原始金额最后披露位的半个单位，绝不额外放大到固定元数。"""
    return Decimal(10) ** parse_amount(raw).as_tuple().exponent * UNIT_FACTORS[unit] / 2


def _cover_matches(report: ExtractedReport, doc: ParsedDocument) -> bool:
    cover = compact("\n".join(block.text for block in doc.blocks if block.pdf_page == 1))
    periods = set(re.findall(r"((?:19|20)\d{2})年?(年度|半年度|第一季度|一季度|第三季度|三季度)报告", cover))
    return periods == {(str(report.period_end.year), "年度")} and compact(report.company) in cover


def _refs(table: Evidence, seed: Metric, doc: ParsedDocument,
          by_id: dict[str, Evidence]) -> list[str]:
    # 延迟导入，允许 comparatives 先调用本模块再进行比较列补查。
    from .comparatives import _refs as comparative_refs

    return comparative_refs(table, seed, doc, by_id)


def _quarter_title_ids(table: Evidence, doc: ParsedDocument, year: int) -> list[str]:
    expected = re.compile(_PREFIX + rf"{year}年分季度主要(?:财务|会计)数据")
    prior_bottom = max((block.bbox[3] for block in doc.blocks
                        if block.kind == "table" and block.pdf_page == table.pdf_page
                        and block.id != table.id and len(block.bbox) == 4 and len(table.bbox) == 4
                        and block.bbox[3] <= table.bbox[1]), default=0)
    titles = []
    for block in doc.blocks:
        if block.kind != "text" or block.pdf_page != table.pdf_page:
            continue
        if not expected.fullmatch(compact(block.text)):
            continue
        bound = block.id in table.context_evidence_ids
        nearby = (len(block.bbox) == len(table.bbox) == 4
                  and prior_bottom <= block.bbox[1] <= table.bbox[1]
                  and 0 <= table.bbox[1] - block.bbox[3] <= 300)
        if bound or nearby:
            titles.append(block.id)
    return titles


def _quarter_columns(table: Evidence, row_index: int, year: int,
                     restated: bool) -> dict[int, int] | None:
    """原表必须逐列明确四季度；不根据金额所在位置猜季度或累计期间。"""
    columns = {}
    for column in range(1, len(table.cells[row_index])):
        header_pieces = [compact(row[column]) for row in table.cells[:row_index]
                         if column < len(row) and "季度" in compact(row[column])]
        if not header_pieces:
            continue
        header = " ".join(header_pieces)
        if any(word in header for word in ("增减", "增长", "%", "比例", "累计", "上年", "去年", "前三")):
            return None
        explicit_years = set(re.findall(r"((?:19|20)\d{2})年", header))
        if explicit_years and explicit_years != {str(year)}:
            return None
        matches = re.findall(r"第([一二三四1234])季度", header)
        quarters = {int(value) if value.isdigit() else "一二三四".index(value) + 1 for value in matches}
        if len(quarters) != 1:
            return None
        quarter = quarters.pop()
        month_ranges = re.findall(r"(?<!\d)(\d{1,2})月?[-—–－~～至](\d{1,2})月份?", header)
        if month_ranges and any((int(start), int(end)) != (3 * quarter - 2, 3 * quarter)
                                for start, end in month_ranges):
            return None
        adjustments = compact(" ".join(row[column] for row in table.cells[:row_index]
                                        if column < len(row) and any(x in row[column] for x in ("调整", "重述"))))
        if adjustments:
            before = bool(re.search(r"(?:调整|重述)前", adjustments))
            after = bool(re.search(r"(?:调整|重述)后", adjustments))
            if before == after or after != restated:
                return None
        elif restated:
            return None
        if quarter in columns:
            return None
        columns[quarter] = column
    return columns if set(columns) == {1, 2, 3, 4} else None


def _validate_cell(report: ExtractedReport, doc: ParsedDocument, seed: Metric,
                   table: Evidence, row_index: int, column: int, *, name: str,
                   start: date, end: date, basis: str, refs: list[str]) -> Metric | None:
    row = table.cells[row_index]
    item = seed.model_dump(mode="json")
    item.update(name=name, raw_value=row[column], period_start=start.isoformat(), period_end=end.isoformat(),
                basis=basis, evidence_ids=refs, row_quote=" | ".join(row),
                source_row_index=row_index, source_column_index=column)
    payload = {"company": report.company, "report_type": report.report_type,
               "period_end": report.period_end.isoformat(), "metrics": [item]}
    checked = validate_payload(payload, doc, doc.blocks)
    return checked.metrics[0] if len(checked.metrics) == 1 else None


def _table_root(table: Evidence, by_id: dict[str, Evidence]) -> str | None:
    """仅沿解析器已有跨页表头链接，不把相邻页或相似金额视为同一表。"""
    seen = {table.id}
    current = table
    while current.header_evidence_id:
        header = by_id.get(current.header_evidence_id)
        if (header is None or header.id in seen or header.document_id != table.document_id
                or header.kind != "table" or not header.cells or header.pdf_page >= current.pdf_page
                or len(header.cells[0]) != len(current.cells[0])):
            return None
        seen.add(header.id)
        current = header
    return current.id


def _balance_context(refs: list[str], by_id: dict[str, Evidence], unit: str) -> bool:
    blocks = [by_id[identifier] for identifier in refs if identifier in by_id]
    context = "\n".join(block.text for block in blocks)
    title = re.compile(_PREFIX + "合并资产负债表")
    explicit_title = any(title.fullmatch(compact(line)) for block in blocks for line in block.text.splitlines())
    return explicit_title and statement_scopes(context) == {"consolidated"} and _units(context) == {unit}


def supplement_annual_tables(report: ExtractedReport, doc: ParsedDocument) -> ExtractedReport:
    """返回副本；所有补查金额来自可定位的原始单元格和已有核验锚点。"""
    if report.document_id != doc.id:
        raise ValueError("年度金额补查必须使用同一来源文件")
    result = report.model_copy(deep=True)
    if report.report_type != "annual" or (report.period_end.month, report.period_end.day) != (12, 31):
        return result

    def warn(message: str) -> None:
        if message not in result.warnings:
            result.warnings.append(message)

    if not _cover_matches(report, doc):
        warn("年度原表补查未执行：封面不能唯一证明同公司同年度完整年报。")
        return result

    by_id = {block.id: block for block in doc.blocks}
    tables = [block for block in doc.blocks if block.kind == "table" and block.cells
              and block.document_id == doc.id]
    year = report.period_end.year

    def merge(candidates_by_key: dict[tuple, list[Metric]]) -> None:
        existing: dict[tuple, list[Metric]] = defaultdict(list)
        for metric in result.metrics:
            existing[_key(metric)].append(metric)
        for key, candidates in candidates_by_key.items():
            if len({metric.value for metric in candidates}) != 1:
                warn(f"{candidates[0].name}年度原表补查存在多个冲突金额，未补充。")
                continue
            if key in existing:
                if any(metric.value != candidates[0].value or metric.status != "verified" for metric in existing[key]):
                    warn(f"{candidates[0].name}年度原表补查与已有记录冲突或已有记录待核实，保留原记录待复核。")
                continue
            first = candidates[0]
            first.evidence_ids = list(dict.fromkeys(identifier for metric in candidates for identifier in metric.evidence_ids))
            result.metrics.append(first)

    from .comparatives import _column_header, _period

    cover_text = "\n".join(block.text for block in doc.blocks if block.pdf_page == 1)
    summary_pending: dict[tuple, list[Metric]] = defaultdict(list)
    summary_seeds = [metric for metric in report.metrics if metric.status == "verified"
                     and metric.company == report.company and metric.scope == "consolidated"
                     and metric.currency == "CNY" and metric.name in ("revenue", "parent_net_profit")
                     and metric.basis == "ytd" and metric.period_start == date(year, 1, 1)
                     and metric.period_end == report.period_end]
    summary_tables = [table for table in tables if table.cells[0]
                      and compact(table.cells[0][0]) == "主要会计数据"]
    for table in summary_tables:
        matching: dict[int, list[tuple[Metric, list[str]]]] = defaultdict(list)
        for seed in summary_seeds:
            if any(other.name == seed.name and (other.value, other.unit, other.restated)
                   != (seed.value, seed.unit, seed.restated) for other in summary_seeds):
                warn(f"{seed.name}主要会计数据补查锚点存在冲突，未使用该锚点。")
                continue
            refs = _refs(table, seed, doc, by_id)
            context = "\n".join(by_id[identifier].text for identifier in refs)
            if statement_scopes(context) - {"consolidated"} or _units(context) != {seed.unit}:
                continue
            for row_index, row in enumerate(table.cells):
                if not row or not _flow_label(seed.name, row[0]):
                    continue
                for column in range(1, len(row)):
                    period = _period(_column_header(table, row_index, column, by_id), seed, report,
                                     context=context, cover_text=cover_text)
                    if (period is None or period.basis != "ytd" or period.start != date(year, 1, 1)
                            or period.end != report.period_end or period.restated != seed.restated):
                        continue
                    anchor = _validate_cell(report, doc, seed, table, row_index, column, name=seed.name,
                        start=period.start, end=period.end, basis="ytd", refs=refs)
                    if anchor is not None and anchor.value == seed.value:
                        matching[column].append((seed, refs))
        if len(matching) != 1:
            warn("主要会计数据本期列缺少唯一的已核验收入或归母利润锚点，未补查现金流与资产总额。")
            continue
        column, anchors = next(iter(matching.items()))
        if len({(seed.unit, seed.restated) for seed, _ in anchors}) != 1:
            warn("主要会计数据本期列的单位或重述锚点不一致，未补查现金流与资产总额。")
            continue
        seed, refs = anchors[0]
        context = "\n".join(by_id[identifier].text for identifier in refs)
        for row_index, row in enumerate(table.cells):
            if not row or column >= len(row):
                continue
            name = "operating_cashflow" if _flow_label("operating_cashflow", row[0]) else (
                "total_assets" if re.fullmatch(_PREFIX + r"(?:资产总计|资产总额|总资产)", compact(row[0])) else None)
            if name is None:
                continue
            point = name == "total_assets"
            template = seed.model_copy(update={"name": name, "basis": "point" if point else "ytd",
                                              "period_start": report.period_end if point else date(year, 1, 1)})
            period = _period(_column_header(table, row_index, column, by_id), template, report,
                             context=context, cover_text=cover_text)
            expected_start = report.period_end if point else date(year, 1, 1)
            if (period is None or period.basis != template.basis or period.start != expected_start
                    or period.end != report.period_end or period.restated != seed.restated):
                warn(f"{name}主要会计数据表的本期列不能唯一证明年度或年末口径，未补充。")
                continue
            metric = _validate_cell(report, doc, template, table, row_index, column, name=name,
                start=period.start, end=period.end, basis=period.basis, refs=refs)
            if metric is None:
                warn(f"{name}主要会计数据原表单元格核验未通过，未补充。")
                continue
            metric.checks.append("同一主要会计数据表本期收入或归母利润金额与已核验年度锚点一致")
            metric.evidence_ids = list(dict.fromkeys(metric.evidence_ids + seed.evidence_ids))
            summary_pending[_key(metric)].append(metric)
    merge(summary_pending)

    current_seeds = [metric for metric in result.metrics if metric.status == "verified"
                     and metric.company == report.company and metric.scope == "consolidated"
                     and metric.currency == "CNY" and metric.period_end == report.period_end]
    pending: dict[tuple, list[Metric]] = defaultdict(list)
    quarter_groups: dict[tuple, list[list[Metric]]] = defaultdict(list)
    blocked_quarter_groups = set()

    for name in QUARTER_METRICS:
        seeds = [metric for metric in current_seeds if metric.name == name and metric.basis == "ytd"
                 and metric.period_start == date(year, 1, 1)]
        if len({(seed.value, seed.unit, seed.restated) for seed in seeds}) > 1:
            warn(f"{name}季度原表补查未执行：已核验年度锚点存在冲突。")
            continue
        if not seeds:
            if any(_quarter_title_ids(table, doc, year) for table in tables):
                warn(f"{name}季度原表补查缺少同口径已核验年度金额锚点，未补充季度金额。")
            continue
        seed = seeds[0]
        if parse_amount(seed.raw_value) * UNIT_FACTORS[seed.unit] != seed.value:
            warn(f"{name}季度原表补查未执行：年度锚点原始金额与元金额不一致。")
            continue
        group_key = _key(seed)
        for table in tables:
            titles = _quarter_title_ids(table, doc, year)
            if not titles:
                continue
            refs = list(dict.fromkeys(_refs(table, seed, doc, by_id) + titles))
            context = "\n".join(by_id[identifier].text for identifier in refs)
            if statement_scopes(context) - {"consolidated"} or _units(context) != {seed.unit}:
                warn(f"{name}季度原表的报表口径或单位不能与年度锚点唯一对应，未补充季度金额。")
                continue
            for row_index, row in enumerate(table.cells):
                if len(row) < 5 or not _flow_label(name, row[0]):
                    continue
                columns = _quarter_columns(table, row_index, year, seed.restated)
                if columns is None:
                    warn(f"{name}季度原表列头不完整或期间含混，未补充季度金额。")
                    blocked_quarter_groups.add(group_key)
                    continue
                try:
                    raw_numbers = [parse_amount(row[columns[quarter]]) for quarter in range(1, 5)]
                    total = sum(raw_numbers, Decimal(0)) * UNIT_FACTORS[seed.unit]
                    tolerance = _rounding_margin(seed.raw_value, seed.unit) + sum(
                        (_rounding_margin(row[columns[quarter]], seed.unit) for quarter in range(1, 5)), Decimal(0))
                except ValueError:
                    warn(f"{name}季度原表金额缺失或不是完整数值，未补充季度金额。")
                    blocked_quarter_groups.add(group_key)
                    continue
                if abs(total - seed.value) > tolerance:
                    warn(f"{name}四个季度原金额之和与同口径年度金额不一致，未补充季度金额。")
                    blocked_quarter_groups.add(group_key)
                    continue
                validated = []
                for quarter in range(1, 5):
                    start = date(year, 3 * quarter - 2, 1)
                    end = date(year, 3 * quarter, monthrange(year, 3 * quarter)[1])
                    metric = _validate_cell(report, doc, seed, table, row_index, columns[quarter],
                        name=name, start=start, end=end, basis="quarter", refs=refs)
                    if metric is None:
                        break
                    metric.checks.append("同一年度分季度原表四季金额之和已按原数披露精度与核验年度金额勾稽")
                    metric.evidence_ids = list(dict.fromkeys(metric.evidence_ids + seed.evidence_ids))
                    validated.append(metric)
                if len(validated) != 4:
                    warn(f"{name}季度原表逐单元格核验未通过，未补充季度金额。")
                    blocked_quarter_groups.add(group_key)
                else:
                    quarter_groups[group_key].append(validated)

    for group_key, rows in quarter_groups.items():
        if group_key in blocked_quarter_groups:
            continue
        if any(len({row[quarter].value for row in rows}) != 1 for quarter in range(4)):
            warn(f"{rows[0][0].name}季度原表存在冲突金额，未补充四个季度金额。")
            continue
        for row in rows:
            for metric in row:
                pending[_key(metric)].append(metric)

    asset_seeds = [metric for metric in current_seeds if metric.name == "total_assets"
                   and metric.basis == "point" and metric.period_start == report.period_end]
    if len({(seed.value, seed.unit, seed.restated) for seed in asset_seeds}) > 1:
        warn("应收账款与存货原表补查未执行：已核验资产总计锚点存在冲突。")
        asset_seeds = []
    if not asset_seeds and any(re.search(r"合并资产负债表", block.text) for block in doc.blocks):
        warn("应收账款与存货原表补查缺少同口径已核验本期资产总计锚点，未补充。")
    families: dict[tuple[str, int], Metric] = {}
    for seed in asset_seeds[:1]:
        for table in tables:
            refs = _refs(table, seed, doc, by_id)
            if not _balance_context(refs, by_id, seed.unit):
                continue
            for row_index, row in enumerate(table.cells):
                if not row or not re.fullmatch(_PREFIX + r"(?:资产总计|资产总额|总资产)", compact(row[0])):
                    continue
                for column in range(1, len(row)):
                    period = _period(_column_header(table, row_index, column, by_id), seed, report,
                                     context="\n".join(by_id[identifier].text for identifier in refs))
                    if (period is None or period.basis != "point" or period.start != report.period_end
                            or period.end != report.period_end or period.restated != seed.restated):
                        continue
                    anchor = _validate_cell(report, doc, seed, table, row_index, column, name="total_assets",
                        start=period.start, end=period.end, basis="point", refs=refs)
                    if anchor is None or anchor.value != seed.value:
                        warn("合并资产负债表本期资产总计与已核验锚点不一致，未从该表补查应收账款与存货。")
                        continue
                    root_id = _table_root(table, by_id)
                    if root_id:
                        families[(root_id, column)] = seed

    for (root_id, column), seed in families.items():
        for table in tables:
            if _table_root(table, by_id) != root_id:
                continue
            refs = _refs(table, seed, doc, by_id)
            if not _balance_context(refs, by_id, seed.unit):
                continue
            for row_index, row in enumerate(table.cells):
                label = compact(row[0]) if row else ""
                name = BALANCE_METRICS.get(label)
                if name is None or column >= len(row):
                    continue
                period = _period(_column_header(table, row_index, column, by_id), seed, report,
                                 context="\n".join(by_id[identifier].text for identifier in refs))
                if (period is None or period.basis != "point" or period.start != report.period_end
                        or period.end != report.period_end or period.restated != seed.restated):
                    warn(f"{name}合并资产负债表的本期列期间不明确，未补充。")
                    continue
                metric = _validate_cell(report, doc, seed, table, row_index, column, name=name,
                    start=period.start, end=period.end, basis="point", refs=refs)
                if metric is None:
                    warn(f"{name}合并资产负债表原始单元格核验未通过，未补充。")
                    continue
                metric.checks.append("同一合并资产负债表本期资产总计与已核验锚点金额一致")
                metric.evidence_ids = list(dict.fromkeys(metric.evidence_ids + seed.evidence_ids))
                pending[_key(metric)].append(metric)

    merge(pending)
    return result
