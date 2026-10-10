"""补齐模型漏提的同表对比金额；每个金额仍经过原文行列核验。

只以已经核验的本期指标为锚点。列头必须明确说明期间，本期单元格
必须与锚点金额相同；同比增长率不能用于倒算上年金额。
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

from .schemas import Coverage, Disclosure, Evidence, ExtractedReport, Metric, ParsedDocument


@dataclass(frozen=True)
class _Period:
    start: date
    end: date
    basis: str
    restated: bool


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _year_shift(value: date, year: int) -> date:
    return date(year, value.month, min(value.day, monthrange(year, value.month)[1]))


def _column_header(table: Evidence, row: int, column: int,
                   by_id: dict[str, Evidence]) -> str:
    """沿用解析器明确绑定的跨页表头，排除纯金额数据行。"""
    from .extraction import column_header_parts

    return " ".join(column_header_parts(table, row, column, by_id))


def _period(header: str, seed: Metric, report: ExtractedReport, *, context: str = "",
            cover_text: str = "") -> _Period | None:
    """将金额列头解释为完整期间；年报裸年份须有封面和局部年度表佐证。"""
    from .extraction import compact
    from .parser import annual_quarter_column_year, annual_year_column_year, statement_scopes

    text = compact(header)
    if not text or any(x in text for x in ("增减", "变动幅度", "%", "比例", "增长率")):
        return None
    restated = seed.restated
    if "调整" in text or "重述" in text:
        before = bool(re.search(r"(?:调整|重述)前", text))
        after = bool(re.search(r"(?:调整|重述)后", text))
        if before == after:
            return None
        restated = after
    date_parts = re.findall(r"((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日", text)
    date_parts += re.findall(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
    try:
        dates = [date(int(y), int(m), int(d)) for y, m, d in date_parts]
        if dates:
            if seed.basis == "point" and len(dates) == 1:
                return _Period(dates[0], dates[0], "point", restated)
            if len(dates) == 2 and dates[0] <= dates[1]:
                basis = "ytd" if (dates[0].month, dates[0].day) == (1, 1) else "quarter"
                return _Period(dates[0], dates[1], basis, restated)
            if len(dates) == 1 and "年初至" in text:
                return _Period(date(dates[0].year, 1, 1), dates[0], "ytd", restated)
            return None

        years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?=年|-)", text)
        if len(set(years)) > 1:
            return None
        relative_prior = any(x in text for x in ("上年", "上期", "去年"))
        year = int(years[0]) if years else report.period_end.year - int(relative_prior)
        if not years and "季度" in text and report.report_type == "annual":
            quarter_year = annual_quarter_column_year(header, context, report_end=report.period_end,
                                                       report_type=report.report_type, cover_text=cover_text)
            if quarter_year is None:
                return None
            year = quarter_year

        if "上年度末" in text or "上年末" in text or "上年期末" in text:
            if seed.basis != "point":
                return None
            end = date(year, 12, 31)
            return _Period(end, end, "point", restated)
        if seed.basis == "point":
            if "上期" in text:
                # “上期末”可能指上季末或上个年度末，不能猜成上一年同日。
                return None
            if years and any(x in text for x in ("年期末", "年末", "年度末")):
                end = date(year, 12, 31)
                return _Period(end, end, "point", restated)
            if any(x in text for x in ("本报告期末", "本期末", "本期期末", "上年同期")) or (
                    text in ("期末", "期末余额")):
                end = _year_shift(seed.period_end, year)
                return _Period(end, end, "point", restated)
            return None

        months = re.search(r"(?<!\d)(\d{1,2})[—–－\-至](\d{1,2})月", text)
        if months:
            start_month, end_month = map(int, months.groups())
            if not 1 <= start_month <= end_month <= 12:
                return None
            basis = "ytd" if start_month == 1 else "quarter"
            return _Period(date(year, start_month, 1), _month_end(year, end_month), basis, restated)
        if "前三季度" in text:
            return _Period(date(year, 1, 1), date(year, 9, 30), "ytd", restated)
        if "半年度" in text or "上半年" in text:
            return _Period(date(year, 1, 1), date(year, 6, 30), "ytd", restated)
        quarter = re.search(r"第?([一二三四1234])季度", text)
        if quarter:
            q = int(quarter.group(1)) if quarter.group(1).isdigit() else "一二三四".index(quarter.group(1)) + 1
            return _Period(date(year, 3 * q - 2, 1), _month_end(year, 3 * q), "quarter", restated)
        if "年度" in text or "全年" in text:
            return _Period(date(year, 1, 1), date(year, 12, 31), "ytd", restated)
        annual_year = annual_year_column_year(header, context, report_end=report.period_end,
                                             report_type=report.report_type, cover_text=cover_text)
        if annual_year is not None:
            return _Period(date(annual_year, 1, 1), date(annual_year, 12, 31), "ytd", restated)
        if "年初至" in text:
            return _Period(date(year, 1, 1), _year_shift(report.period_end, year), "ytd", restated)
        if text == "本期金额" and report.report_type == "q3" and "非经常性损益" in compact(context) and "年初至" in compact(context):
            # 非经常明细同时列本期/累计：本期是第三季度，不能跟随累计锚点。
            return _Period(date(year, 7, 1), date(year, 9, 30), "quarter", restated)
        if "本报告期" in text:
            # 三季报摘要中的“本报告期”特指第三季度。
            basis = "quarter" if report.report_type in ("q1", "q3") else "ytd"
            month = report.period_end.month - 2 if basis == "quarter" else 1
            return _Period(date(year, month, 1), _year_shift(report.period_end, year), basis, restated)
        if any(x in text for x in ("上年同期", "本期", "本年")) or (
                "上年" in text and report.report_type == "annual"):
            return _Period(_year_shift(seed.period_start, year), _year_shift(seed.period_end, year),
                           seed.basis, restated)
        if text == "上期发生额" and seed.basis == "ytd" and statement_scopes(context) == {seed.scope}:
            # 已明确绑定到A股累计财务报表：上期发生额为上年同长度累计，不能当上一季。
            return _Period(_year_shift(seed.period_start, year), _year_shift(seed.period_end, year),
                           "ytd", restated)
    except ValueError:
        return None
    return None


def _refs(table: Evidence, seed: Metric, doc: ParsedDocument,
          by_id: dict[str, Evidence]) -> list[str]:
    refs = [table.id]
    if table.header_evidence_id in by_id:
        refs.append(table.header_evidence_id)
        refs.extend(by_id[table.header_evidence_id].context_evidence_ids)
    refs.extend(table.context_evidence_ids)
    relevant_pages = {by_id[x].pdf_page for x in refs if x in by_id}
    for identifier in seed.evidence_ids:
        block = by_id.get(identifier)
        if block and block.kind == "text" and block.pdf_page in relevant_pages:
            if "单位" in block.text or any(x in block.text for x in ("利润表", "现金流量表", "资产负债表")):
                refs.append(identifier)
    # 未绑定上下文的简单表格，只补充同页邻近的标题及单位。
    if not table.context_evidence_ids:
        for block in doc.blocks:
            if block.kind != "text" or block.pdf_page != table.pdf_page:
                continue
            if table.bbox and block.bbox and not 0 <= table.bbox[1] - block.bbox[1] <= 300:
                continue
            if "单位" in block.text or any(x in block.text for x in ("利润表", "现金流量表", "资产负债表")):
                refs.append(block.id)
    return list(dict.fromkeys(x for x in refs if x in by_id))


def _key(metric: Metric) -> tuple:
    return (metric.name, metric.company, metric.period_start, metric.period_end,
            metric.basis, metric.scope, metric.currency, metric.restated)


def _main_net_profit(label: str) -> bool:
    from .extraction import compact

    return bool(re.match(r"^(?:[一二三四五六七八九十]+[、.．])?净利润(?:[（(]|$)", compact(label)))


def _same_current_period(period: _Period, seed: Metric, report: ExtractedReport) -> bool:
    if (period.start, period.end, period.restated) != (seed.period_start, seed.period_end, seed.restated):
        return False
    if period.basis == seed.basis:
        return True
    # 第一季度的单季与年初累计是同一完整期间；此等价不适用于其他季度。
    return (report.report_type == "q1" and {period.basis, seed.basis} == {"quarter", "ytd"}
            and period.start == date(period.end.year, 1, 1)
            and period.end == date(period.end.year, 3, 31))


def _supplement_explicit_restatements(report: ExtractedReport, doc: ParsedDocument) -> None:
    """补查明确已经重述比较财报的句子，标题、提问、意向和否定句都不足。"""
    from .extraction import compact

    existing = {compact(d.quote) for d in report.disclosures if d.kind == "restatement"}
    for block in doc.blocks:
        if block.kind != "text":
            continue
        for clause in re.split(r"[。；;]", block.text):
            text = compact(clause)
            if (not text or text in existing or re.search(
                    r"(?:未|没有|无需|无须|尚未|不|拟|将|计划|若|如果|可能|是否).{0,24}(?:重述|追溯调整)", text)):
                continue
            if not re.search(r"(?:公司|本集团).{0,40}(?:已经重述|已重述|并重述|进行了重述|已进行追溯调整)"
                             r".{0,20}(?:比较期间|上年同期|以前年度).{0,12}(?:财务报表|会计数据)", text):
                continue
            report.disclosures.append(Disclosure(kind="restatement",
                summary="原文明确披露已重述比较期间财务报表或会计数据。",
                quote=clause.strip(), evidence_ids=[block.id]))
            existing.add(text)
            coverage = next((c for c in report.coverage if c.topic == "accounting_change"), None)
            if coverage is None:
                report.coverage.append(Coverage(topic="accounting_change", status="reviewed", evidence_ids=[block.id]))
            else:
                coverage.status = "reviewed"
                coverage.evidence_ids = list(dict.fromkeys(coverage.evidence_ids + [block.id]))


def supplement_comparatives(report: ExtractedReport, doc: ParsedDocument) -> ExtractedReport:
    """返回副本，补充明确披露且与已核验本期金额配对的对比金额。"""
    from .extraction import _label_matches, _units, compact, validate_payload, is_consolidated_profit_statement
    from .parser import statement_scopes

    result = report.model_copy(deep=True)
    if report.document_id != doc.id:
        raise ValueError("对比金额补充必须使用同一来源文件")
    from .annual_supplements import supplement_annual_tables

    result = supplement_annual_tables(result, doc)
    _supplement_explicit_restatements(result, doc)
    by_id = {block.id: block for block in doc.blocks}
    cover_text = "\n".join(block.text for block in doc.blocks if block.pdf_page == 1)
    candidates: dict[tuple, list[Metric]] = defaultdict(list)
    warnings = []

    def warn(message: str) -> None:
        if message not in warnings and message not in result.warnings:
            warnings.append(message)

    def validate_cell(seed: Metric, table: Evidence, row_index: int, column: int,
                      period: _Period, ids: list[str], unit: str,
                      *, name: str | None = None) -> Metric | None:
        row = table.cells[row_index]
        item = seed.model_dump(mode="json")
        item.update(name=name or seed.name, raw_value=row[column], unit=unit,
                    period_start=period.start.isoformat(), period_end=period.end.isoformat(),
                    basis=period.basis, restated=period.restated, evidence_ids=ids,
                    row_quote=" | ".join(row), source_row_index=row_index, source_column_index=column)
        body = {"company": report.company, "report_type": report.report_type,
                "period_end": report.period_end.isoformat(), "metrics": [item]}
        validated = validate_payload(body, doc, doc.blocks)
        return validated.metrics[0] if len(validated.metrics) == 1 else None

    seeds = [metric for metric in result.metrics if metric.status == "verified"
             and metric.company == report.company and metric.period_end == report.period_end]
    # 模型可能漏提合并净利润。仅在同一明确合并利润表中，用已核验的
    # 本期归母金额确认列，再读取独立的净利润总计行；绝不用归母值替代。
    net_profit_candidates: dict[tuple, list[Metric]] = defaultdict(list)
    for seed in seeds:
        if seed.name != "parent_net_profit" or seed.scope != "consolidated":
            continue
        if any(metric.name == "net_profit" and metric.company == seed.company
               and metric.scope == seed.scope and metric.basis == seed.basis
               and metric.period_start == seed.period_start and metric.period_end == seed.period_end
               and metric.currency == seed.currency and metric.restated == seed.restated
               and metric.status == "verified"
               for metric in result.metrics):
            continue
        for table in doc.blocks:
            if table.kind != "table" or not table.cells:
                continue
            ids = _refs(table, seed, doc, by_id)
            context = "\n".join(by_id[x].text for x in ids)
            if not is_consolidated_profit_statement(context) or "母公司利润表" in compact(context):
                continue
            units = _units(context)
            if len(units) != 1:
                continue
            unit = next(iter(units))
            matching_columns: set[int] = set()
            for row_index, row in enumerate(table.cells):
                if len(row) < 2 or not _label_matches("parent_net_profit", row[0], context=context):
                    continue
                for column in range(1, len(row)):
                    period = _period(_column_header(table, row_index, column, by_id), seed, report,
                                     context=context, cover_text=cover_text)
                    if period is None or not _same_current_period(period, seed, report):
                        continue
                    anchor = validate_cell(seed, table, row_index, column, period, ids, unit)
                    if anchor is not None and anchor.value == seed.value:
                        matching_columns.add(column)
            if not matching_columns:
                continue
            for row_index, row in enumerate(table.cells):
                if not row or not _main_net_profit(row[0]):
                    continue
                for column in matching_columns:
                    if column >= len(row):
                        continue
                    period = _period(_column_header(table, row_index, column, by_id), seed, report,
                                     context=context, cover_text=cover_text)
                    if period is None or not _same_current_period(period, seed, report):
                        continue
                    metric = validate_cell(seed, table, row_index, column, period, ids, unit, name="net_profit")
                    if metric is not None:
                        metric.checks.append("同一合并利润表本期归母金额已核对；合并净利润来自独立总计行")
                        net_profit_candidates[_key(metric)].append(metric)
    for group in net_profit_candidates.values():
        if len({metric.value for metric in group}) != 1:
            warn("合并净利润补查存在多个冲突金额，未补充本期及对应比较金额。")
            continue
        first = group[0]
        first.evidence_ids = list(dict.fromkeys(identifier for metric in group for identifier in metric.evidence_ids))
        result.metrics.append(first)
        seeds.append(first)
    # 归母与扣非差额只用于核对合计，不作为原始披露金额。没有税后归母
    # 明细合计行的摘要，不能通过减法“补出”一个已经披露的数值。
    nonrecurring_candidates: dict[tuple, list[Metric]] = defaultdict(list)
    nonrecurring_conflicts: set[tuple] = set()
    paired: dict[tuple, dict[str, list[Metric]]] = defaultdict(lambda: defaultdict(list))
    for seed in seeds:
        if seed.name not in ("parent_net_profit", "adjusted_parent_net_profit") or seed.scope != "consolidated":
            continue
        pair_key = (seed.company, seed.period_start, seed.period_end, seed.basis,
                    seed.scope, seed.currency, seed.restated)
        paired[pair_key][seed.name].append(seed)
    for group in paired.values():
        parents = group["parent_net_profit"]
        adjusted = group["adjusted_parent_net_profit"]
        if not parents or not adjusted:
            continue
        if len({m.value for m in parents}) != 1 or len({m.value for m in adjusted}) != 1:
            warn("非经常性损益补查的归母或扣非锚点存在冲突，未补充。")
            continue
        seed, adjusted_seed = parents[0], adjusted[0]
        expected_value = seed.value - adjusted_seed.value
        for table in doc.blocks:
            if table.kind != "table" or not table.cells:
                continue
            ids = _refs(table, seed, doc, by_id)
            context = "\n".join(by_id[x].text for x in ids)
            if "非经常性损益" not in compact(context):
                continue
            units = _units(context)
            if len(units) != 1:
                continue
            unit = next(iter(units))
            for row_index, row in enumerate(table.cells):
                if len(row) < 2 or not _label_matches("nonrecurring_total_parent", row[0], context=context):
                    continue
                if not any(i < row_index and cells and "所得税" in compact(cells[0])
                           for i, cells in enumerate(table.cells)) or not any(
                        i < row_index and cells and "少数股东" in compact(cells[0])
                        for i, cells in enumerate(table.cells)):
                    continue
                for column in range(1, len(row)):
                    period = _period(_column_header(table, row_index, column, by_id), seed, report,
                                     context=context, cover_text=cover_text)
                    if period is None or not _same_current_period(period, seed, report):
                        continue
                    metric = validate_cell(seed, table, row_index, column, period, ids, unit,
                                           name="nonrecurring_total_parent")
                    if metric is None:
                        continue
                    if metric.value != expected_value:
                        nonrecurring_conflicts.add(_key(metric))
                        warn("非经常性损益原文合计与同期间归母减扣非金额不一致，未配对补充。")
                        continue
                    metric.checks.append("税后归母非经常性损益合计来自原文行列；与同期间已核验归母减扣非金额一致")
                    nonrecurring_candidates[_key(metric)].append(metric)
    for key, group in nonrecurring_candidates.items():
        if key in nonrecurring_conflicts:
            warn("非经常性损益补查存在多个冲突金额，未选择补充值。")
            continue
        existing_verified = [m for m in result.metrics if m.status == "verified" and _key(m) == key]
        if len({m.value for m in group + existing_verified}) != 1:
            warn("非经常性损益补查存在多个冲突金额，未选择补充值。")
            continue
        if existing_verified:
            continue
        first = group[0]
        first.evidence_ids = list(dict.fromkeys(identifier for metric in group for identifier in metric.evidence_ids))
        result.metrics.append(first)
        seeds.append(first)
    for seed in seeds:
        for table in doc.blocks:
            if table.kind != "table" or not table.cells:
                continue
            ids = _refs(table, seed, doc, by_id)
            context = "\n".join(by_id[x].text for x in ids)
            # 新来源表必须有相符的报表口径；原锚点表已做过核验。
            if table.id not in seed.evidence_ids:
                # 与解析器、金额核验共用标题识别，兼容“合并年初到/至报告
                # 期末现金流量表”；仅有“合并”字样或混合口径不能证明来源。
                if statement_scopes(context) != {seed.scope}:
                    continue
            for row_index, row in enumerate(table.cells):
                if len(row) < 3 or not _label_matches(seed.name, row[0], context=context):
                    continue
                if seed.name == "net_profit" and not _main_net_profit(row[0]):
                    continue
                quote = " | ".join(row)
                headers = {column: _column_header(table, row_index, column, by_id)
                           for column in range(1, len(row))}
                periods = {column: _period(header, seed, report, context=context, cover_text=cover_text)
                           for column, header in headers.items()}
                if any(("调整" in header or "重述" in header) and periods[column] is None
                       for column, header in headers.items()):
                    warn(f"{seed.name}对比列追溯调整口径未明确，未补充该列。")
                current_columns = [column for column, period in periods.items() if period is not None
                                   and (period.start, period.end, period.basis, period.restated)
                                   == (seed.period_start, seed.period_end, seed.basis, seed.restated)]
                prior_columns = [column for column, period in periods.items() if period is not None
                                 and period.end < seed.period_end and period.basis == seed.basis]
                if not current_columns or not prior_columns:
                    continue
                units = _units(quote) or _units(context)
                if len(units) != 1:
                    warn(f"{seed.name}对比金额的单位不能唯一绑定，未补充。")
                    continue
                unit = next(iter(units))

                matching_current = [value for column in current_columns
                                    if (value := validate_cell(seed, table, row_index, column,
                                                              periods[column], ids, unit)) is not None
                                    and value.value == seed.value]
                if not matching_current:
                    warn(f"{seed.name}对比表本期金额或口径与已核验指标不一致，未配对补充。")
                    continue
                for column in prior_columns:
                    metric = validate_cell(seed, table, row_index, column, periods[column], ids, unit)
                    if metric is None:
                        warn(f"{seed.name}对比金额未通过原文行列及期间核验，未补充。")
                        continue
                    metric.checks.append("同表本期金额与已核验指标一致；对比期间由明确列头确定")
                    candidates[_key(metric)].append(metric)
    existing: dict[tuple, list[Metric]] = defaultdict(list)
    for metric in result.metrics:
        existing[_key(metric)].append(metric)
    for key, group in candidates.items():
        values = {metric.value for metric in group + existing.get(key, [])}
        first = group[0]
        if len(values) != 1:
            warn(f"{first.name} {first.period_end}同期间同口径对比金额存在冲突，未选择补充值。")
            continue
        if key in existing:
            continue
        first.evidence_ids = list(dict.fromkeys(identifier for metric in group for identifier in metric.evidence_ids))
        result.metrics.append(first)
    # 归母盈利与合并亏损可以同时成立。只用同一张明确合并利润表、同一
    # 列的三条原始金额核对加总；缺少少数股东损益时不凭财务印象改数。
    from .extraction import UNIT_FACTORS, parse_amount

    profit_pairs: dict[tuple, dict[str, list[Metric]]] = defaultdict(lambda: defaultdict(list))
    for metric in result.metrics:
        if (metric.status == "verified" and metric.company == report.company and metric.scope == "consolidated"
                and metric.name in ("net_profit", "parent_net_profit")):
            profit_pairs[_key(metric)[1:]][metric.name].append(metric)
    for pair in profit_pairs.values():
        totals, parents = pair["net_profit"], pair["parent_net_profit"]
        if not totals or not parents or len({m.value for m in totals}) != 1 or len({m.value for m in parents}) != 1:
            continue
        total, parent = totals[0], parents[0]
        outcomes = []
        for table in doc.blocks:
            if table.kind != "table" or not table.cells:
                continue
            ids = _refs(table, total, doc, by_id)
            context = "\n".join(by_id[x].text for x in ids)
            if not is_consolidated_profit_statement(context) or statement_scopes(context) != {"consolidated"}:
                continue
            units = _units(context)
            if len(units) != 1:
                continue
            unit = next(iter(units))
            total_rows = [i for i, row in enumerate(table.cells) if row and _main_net_profit(row[0])]
            parent_rows = [i for i, row in enumerate(table.cells)
                           if row and _label_matches("parent_net_profit", row[0], context=context)]
            minority_rows = [i for i, row in enumerate(table.cells) if row and re.match(
                r"^(?:[一二三四五六七八九十\d]+[、.．])?少数股东损益(?:[（(]|$)", compact(row[0]))]
            if any(len(rows) != 1 for rows in (total_rows, parent_rows, minority_rows)):
                continue
            row_indices = (total_rows[0], parent_rows[0], minority_rows[0])
            for column in range(1, min(len(table.cells[i]) for i in row_indices)):
                periods = [_period(_column_header(table, i, column, by_id), total, report,
                                   context=context, cover_text=cover_text) for i in row_indices]
                if any(period is None or not _same_current_period(period, total, report) for period in periods):
                    continue
                checked_total = validate_cell(total, table, row_indices[0], column, periods[0], ids, unit)
                checked_parent = validate_cell(parent, table, row_indices[1], column, periods[1], ids, unit)
                if (checked_total is None or checked_total.value != total.value
                        or checked_parent is None or checked_parent.value != parent.value):
                    continue
                minority_row = table.cells[row_indices[2]]
                if (_units(" | ".join(minority_row)) or units) != {unit}:
                    continue
                try:
                    raw_values = [parse_amount(table.cells[i][column]) for i in row_indices]
                except ValueError:
                    continue
                factor = UNIT_FACTORS[unit]
                minority_value = raw_values[2] * factor
                difference = total.value - parent.value - minority_value
                # 三项分别四舍五入，最大加总误差为各原始精度半个单位之和。
                tolerance = sum((Decimal(1).scaleb(value.as_tuple().exponent)
                                 for value in raw_values), Decimal(0)) * factor / 2
                outcomes.append((abs(difference) <= tolerance, difference, tolerance, minority_value, ids, unit))
        if not outcomes:
            continue
        failures = [outcome for outcome in outcomes if not outcome[0]]
        if failures:
            _, difference, tolerance, _, _, unit = failures[0]
            warn(f"{total.company} {total.period_end}合并利润勾稽不一致：合并净利润与归母净利润加少数股东损益"
                 f"差额为{difference}元，超过原表{unit}精度容差{tolerance}元；请人工核对，未自动改数。")
            continue
        _, _, tolerance, minority_value, _, unit = outcomes[0]
        message = ("同一合并利润表勾稽核验通过：合并净利润 = 归母净利润 + 少数股东损益"
                   f"（少数股东损益{minority_value}元；原表单位{unit}，精度容差{tolerance}元）")
        for metric in totals + parents:
            if message not in metric.checks:
                metric.checks.append(message)
            metric.evidence_ids = list(dict.fromkeys(metric.evidence_ids + [
                identifier for outcome in outcomes for identifier in outcome[4]]))
    result.warnings.extend(warnings)
    return result
