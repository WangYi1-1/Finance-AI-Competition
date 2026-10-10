"""从原表补充明细和公司解释；不让模型推测原因或金额。

非经常性项目沿用已核验合计的行列和期间锚点；跨页只跟随解析器的表头链接。
现金流桥接只接受明确的调节表，不能用资产负债表余额替代调节项目。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .extraction import UNIT_FACTORS as UNIT_SCALE, compact, parse_amount, validate_payload, _units
from .schemas import ExtractedReport, ParsedDocument


def _root(table, evidence):
    return table.header_evidence_id or table.id


def _amount(raw):
    # 空白与横线并不自动等于0；仅在原表合计闭合后说明未列金额项目。
    if not re.fullmatch(r"[+\-−]?[\d,，]+(?:\.\d+)?|[（(][\d,，]+(?:\.\d+)?[）)]", compact(raw)):
        return None
    try:
        value = parse_amount(raw)
        return value if value.is_finite() else None
    except ValueError:
        return None


def _nonrecurring(report, doc):
    by_id = {b.id: b for b in doc.blocks}
    seeds = [m for m in report.metrics if m.name == "nonrecurring_total_parent"
             and m.status == "verified" and m.period_end == report.period_end
             and m.scope == "consolidated" and m.source_column_index is not None
             and m.source_row_index is not None]
    if not seeds:
        return [], {"status": "missing", "message": "需补充有期间、单位和税后归母合计的非经常性损益明细表。"}
    seeds = [m for m in seeds if m.basis == "ytd"] or seeds
    # 不选择冲突的期间/口径/值。
    if len({(m.period_start, m.basis, m.value, m.restated) for m in seeds}) != 1:
        return [], {"status": "inconsistent", "message": "非经常性合计存在不同期间或冲突金额，不补列明细。"}
    seed = seeds[0]
    totals = [by_id[eid] for eid in seed.evidence_ids if eid in by_id
              and by_id[eid].kind == "table" and len(by_id[eid].cells) > seed.source_row_index
              and len(by_id[eid].cells[seed.source_row_index]) > seed.source_column_index
              and compact(by_id[eid].cells[seed.source_row_index][0]) in ("合计", "非经常性损益合计")
              and _amount(by_id[eid].cells[seed.source_row_index][seed.source_column_index]) == seed.value / UNIT_SCALE[seed.unit]]
    if len(totals) != 1:
        return [], {"status": "missing", "message": "合计无法唯一绑定原表行列，不推断项目金额。"}
    total_table = totals[0]
    root_id = _root(total_table, by_id)
    root = by_id.get(root_id)
    if root is None or "非经常性损益" not in compact(root.text + "".join(by_id[x].text for x in root.context_evidence_ids if x in by_id)):
        return [], {"status": "missing", "message": "未定位非经常性明细表的表头。"}
    rows, blanks = [], []
    finished = False
    for table in sorted(doc.blocks, key=lambda b: (b.pdf_page, b.id)):
        if table.kind != "table" or _root(table, by_id) != root_id:
            continue
        for index, cells in enumerate(table.cells):
            if table.id == total_table.id and index >= seed.source_row_index:
                finished = True
                break
            if len(cells) <= seed.source_column_index:
                continue
            label = compact(cells[0])
            if not label or label.startswith(("非经常性损益项目", "项目", "（如适用")):
                continue
            raw = cells[seed.source_column_index]
            value = _amount(raw)
            if value is None:
                blanks.append(label)
                continue
            kind = ("tax_adjustment" if "所得税影响" in label else
                    "minority_adjustment" if "少数股东" in label else "pretax_item")
            value *= UNIT_SCALE[seed.unit]
            rows.append({"category": "nonrecurring", "item": label, "kind": kind,
                         "period_start": seed.period_start.isoformat(), "period_end": seed.period_end.isoformat(),
                         "basis": seed.basis, "scope": seed.scope, "currency": seed.currency,
                         "value_yuan": str(value), "raw_value": raw, "raw_unit": seed.unit,
                         "contribution_yuan": str(-value if kind != "pretax_item" else value),
                         "evidence_ids": list(dict.fromkeys([table.id, root_id] + seed.evidence_ids)),
                         "source_row_index": index, "source_column_index": seed.source_column_index,
                         "status": "row_verified"})
        if finished:
            break
    roles = {r["kind"] for r in rows}
    calculated = sum((Decimal(r["contribution_yuan"]) for r in rows), Decimal(0))
    residual = seed.value - calculated
    complete = {"tax_adjustment", "minority_adjustment"} <= roles
    # 原表精确列示元小数，给出合计的最末显示位容差，而非宽泛百分比。
    tolerance = UNIT_SCALE[seed.unit] * Decimal("0.01") * max(1, len(rows))
    status = "reconciled" if complete and abs(residual) <= tolerance else "incomplete" if not complete else "inconsistent"
    return rows, {"status": status, "formula": "税前项目合计−所得税影响额−少数股东权益影响额（税后）",
                  "calculated_yuan": str(calculated), "reported_yuan": str(seed.value),
                  "residual_yuan": str(residual), "tolerance_yuan": str(tolerance),
                  "blank_items": blanks, "evidence_ids": seed.evidence_ids,
                  "message": "列示金额与税后归母合计已勾稽，未列金额项不单独认定为零。" if status == "reconciled"
                  else "明细尚未完整还原披露合计，保留差额，不将其当成完整解释。"}


def _reasons(report, doc):
    reasons = []
    seen = set()
    terms = {"revenue": ("营业收入",), "profit": ("净利润", "营业利润", "利润总额"),
             "operating_cashflow": ("经营活动产生的现金流量净额", "经营现金流")}
    causal = re.compile(r"主要(?:系|是|由于|因为|原因|受)|原因(?:是|为|：|:)|由于|导致|受.{1,30}影响")
    tables = [b for b in doc.blocks if b.kind == "table" and b.bbox]
    for block in doc.blocks:
        candidates = []
        quote_ids = [block.id]
        if block.kind == "table":
            for row in block.cells:
                if any("原因" in cell for cell in row) or (len(row) >= 2 and causal.search(" ".join(row))):
                    candidates.append(" | ".join(row))
        else:
            if len(block.bbox) == 4 and any(t.pdf_page == block.pdf_page
                    and t.bbox[0] - 3 <= block.bbox[0] and t.bbox[1] - 3 <= block.bbox[1]
                    and t.bbox[2] + 3 >= block.bbox[2] and t.bbox[3] + 3 >= block.bbox[3] for t in tables):
                continue
            paragraph = block.text
            tail = block
            if '变动原因说明' in compact(paragraph) and not paragraph.rstrip().endswith(('。','；')) and block.bbox:
                for following in doc.blocks[doc.blocks.index(block)+1:doc.blocks.index(block)+4]:
                    if following.kind != 'text' or following.pdf_page != block.pdf_page or not following.bbox:
                        break
                    if not (0 <= following.bbox[1] - tail.bbox[3] <= 35 and abs(following.bbox[0]-tail.bbox[0]) <= 25):
                        break
                    if '变动原因说明' in following.text or re.match(r'\s*(?:\d+[、.]|[一二三四五六七八九十]+[、])',following.text):
                        break
                    paragraph += '\n' + following.text
                    quote_ids.append(following.id)
                    tail = following
                    if paragraph.rstrip().endswith(('。','；')):
                        break
            candidates = [sentence.strip() for sentence in re.split(r"(?<=[。；])", paragraph)]
        for quote in candidates:
            normalized = compact(quote)
            if not 12 <= len(normalized) <= 1400 or not causal.search(normalized):
                continue
            before = normalized[:causal.search(normalized).start()]
            subject = compact(quote.split("|", 1)[0]) if block.kind == "table" else before
            matched = [name for name, words in terms.items() if any(w in subject for w in words)]
            if not matched or normalized in seen:
                continue
            # 这类模板描述或会计方法不是本期经营变动解释。
            if any(x in normalized for x in ("由于会计政策变更", "由于重大会计差错", "由于《企业会计准则》", "因无法合理估计",
                    "关键审计事项", "我们将", "不确定性", "重要会计政策", "敏感性", "将增加或减少", "假设")):
                continue
            if block.kind != "table" and not re.search(r"同比|上年同期|较上|较期|本期|报告期|增加|下降|减少|增长|变动原因", before):
                continue
            # 一段中主要谈应收账款变化时，即便提到收入也不能列为收入变化解释。
            if re.match(r"(?:\d+[、,.])?(?:应收账款|存货|所得税费用|营业外支出)", subject):
                continue
            seen.add(normalized)
            reasons.append({"topics": matched, "quote": quote, "evidence_ids": quote_ids,
                            "status": "company_disclosure", "attribution": "报告中的公司解释，非因果验证",
                            "context_period_end": report.period_end.isoformat(),
                            "period_note": "来自本份报告；具体期间及适用指标以引文为准，不自动解释其他季度。"})
    return reasons


def _cashflow_bridge(report, doc):
    missing = {"status": "missing", "message": "尚未核验完整间接法调节表；利润与现金流差额不自动归因于应收或存货变化。"}
    if report.report_type != "annual":
        return [], missing
    by_id = {b.id: b for b in doc.blocks}
    anchors = {}
    for name in ("net_profit", "operating_cashflow"):
        metrics = [m for m in report.metrics if m.name == name and m.status == "verified"
                   and m.period_end == report.period_end and m.scope == "consolidated"
                   and m.basis == "ytd" and m.period_start.isoformat() == f"{report.period_end.year}-01-01"]
        if len({(m.value, m.restated) for m in metrics}) != 1:
            return [], missing
        anchors[name] = metrics[0]
    if anchors["net_profit"].restated != anchors["operating_cashflow"].restated:
        return [], {"status": "inconsistent", "message": "利润与现金流重述标记不一致，暂不桥接。"}
    candidates = []
    for root in doc.blocks:
        if root.kind != "table" or "将净利润调节为经营活动现金流量" not in compact(root.text):
            continue
        if not root.cells or compact(root.cells[0][0]) != "补充资料":
            continue
        cols = [i for i, cell in enumerate(root.cells[0]) if compact(cell) == "本期金额"]
        if len(cols) != 1:
            continue
        col = cols[0]
        local = [b for b in doc.blocks if b.kind == "text" and b.pdf_page == root.pdf_page
                 and b.bbox and root.bbox and root.bbox[1] - 160 <= b.bbox[1] <= root.bbox[1]]
        context = compact("".join(b.text for b in local))
        units = [unit for unit in UNIT_SCALE if f"单位:{unit}" in context or f"单位：{unit}" in context]
        if len(units) != 1 or "人民币" not in context or "母公司" in context:
            continue
        unit = units[0]
        rows, profit, cash, stop = [], None, None, False
        tables = sorted([t for t in doc.blocks if t.kind == "table" and _root(t, by_id) == root.id],
                        key=lambda t: (t.pdf_page, t.id))
        for table in tables:
            for index, cells in enumerate(table.cells):
                if len(cells) <= col:
                    continue
                label = compact(cells[0])
                if label.startswith("2.") or label.startswith("2．"):
                    stop = True
                    break
                if label == "经营活动产生的现金流量净额":
                    raw_cash = _amount(cells[col])
                    cash = raw_cash * UNIT_SCALE[unit] if raw_cash is not None else None
                    stop = True
                    break
                value = _amount(cells[col])
                if value is None or not label:
                    continue
                value *= UNIT_SCALE[unit]
                if label == "净利润":
                    profit = value
                rows.append({"category": "cashflow_bridge", "item": label,
                    "kind": "profit_start" if label == "净利润" else "adjustment",
                    "value_yuan": str(value), "contribution_yuan": str(value), "raw_value": cells[col],
                    "raw_unit": unit, "period_start": anchors["net_profit"].period_start.isoformat(),
                    "period_end": report.period_end.isoformat(), "scope": "consolidated", "basis": "ytd",
                    "currency": "CNY", "source_row_index": index, "source_column_index": col,
                    "evidence_ids": list(dict.fromkeys([table.id, root.id] + [b.id for b in local])),
                    "status": "row_verified"})
            if stop:
                break
        if profit is None or cash is None:
            continue
        # 表头本期金额需同时与已核验的同年合并净利润、经营现金流核对，不能仅凭“净利润”行认定口径。
        if profit != anchors["net_profit"].value or cash != anchors["operating_cashflow"].value:
            candidates.append(([], {"status": "inconsistent", "message": "调节表起终金额与同期间合并报表不一致，未采用。"}))
            continue
        total = sum((Decimal(row["contribution_yuan"]) for row in rows), Decimal(0))
        residual = cash - total
        tolerance = UNIT_SCALE[unit] * Decimal("0.01") * max(1, len(rows))
        candidates.append((rows, {"status": "reconciled" if abs(residual) <= tolerance else "inconsistent",
            "formula": "合并净利润+原表有符号调节项=经营活动现金流量净额",
            "profit_yuan": str(profit), "reported_yuan": str(cash), "calculated_yuan": str(total),
            "residual_yuan": str(residual), "tolerance_yuan": str(tolerance),
            "evidence_ids": list(dict.fromkeys([r for row in rows for r in row["evidence_ids"]]
                            + anchors["net_profit"].evidence_ids + anchors["operating_cashflow"].evidence_ids)),
            "message": "已核对起终金额、期间、人民币单位和有符号调节项；原表空白不单独认定为零。" if abs(residual) <= tolerance
                       else "调节项尚未闭合，保留未解释差额；不输出完整桥接结论。"}))
    if len(candidates) == 1:
        return candidates[0]
    return [], missing if not candidates else {"status": "inconsistent", "message": "存在多个调节表候选，未自动选择。"}


def _parent_metrics(report, doc):
    """年报正式母公司流量表也必须经过统一行列验证器，不复制合并金额。"""
    if report.report_type != "annual" or not report.metrics:
        return report
    by_id = {b.id: b for b in doc.blocks}
    candidates = []
    for table in doc.blocks:
        if table.kind != 'table' or not table.cells:
            continue
        root = by_id.get(table.header_evidence_id or table.id)
        if root is None:
            continue
        refs = [table.id, root.id] + root.context_evidence_ids + table.context_evidence_ids
        refs = list(dict.fromkeys(refs))
        context = '\n'.join(by_id[e].text for e in refs if e != table.id and e != root.id)
        title = compact(context)
        if not re.search(r'母公司(?:利润表|现金流量表)', title) or '合并利润表' in title or '合并现金流量表' in title:
            continue
        units = _units(context)
        if len(units) != 1 or '人民币' not in context:
            continue
        unit = next(iter(units))
        for row_index, row in enumerate(table.cells):
            if not row:
                continue
            label = compact(row[0])
            name = ('revenue' if re.fullmatch(r'(?:[一二三四五六七八九十]+[、.])?营业收入',label) else
                    'net_profit' if re.fullmatch(r'(?:[一二三四五六七八九十]+[、.])?净利润(?:\(净亏损以.*填列\))?',label) else
                    'operating_cashflow' if label == '经营活动产生的现金流量净额' else None)
            if name is None:
                continue
            for column, cell in enumerate(root.cells[0]):
                match = re.fullmatch(r'((?:19|20)\d{2})年度',compact(cell))
                if not match or column >= len(row) or _amount(row[column]) is None:
                    continue
                year = int(match.group(1))
                if year not in (report.period_end.year, report.period_end.year-1):
                    continue
                item = report.metrics[0].model_dump(mode='json')
                item.update(name=name, scope='parent', raw_value=row[column], unit=unit, restated=False,
                    period_start=f'{year}-01-01', period_end=f'{year}-12-31', basis='ytd',
                    evidence_ids=refs, row_quote=' | '.join(row), source_row_index=row_index, source_column_index=column)
                checked = validate_payload({'company': report.company, 'report_type':report.report_type,
                    'period_end': report.period_end.isoformat(), 'metrics':[item]},doc,doc.blocks)
                candidates.extend(checked.metrics)
    def key(metric):
        return (metric.name,metric.company,metric.scope,metric.basis,metric.period_start,metric.period_end)
    existing = {key(m): m for m in report.metrics}
    for metric in candidates:
        if key(metric) not in existing:
            existing[key(metric)] = metric
        elif existing[key(metric)].value != metric.value:
            # 保留冲突两份给分析器拒绝，不能自动覆盖模型抽取的母公司金额。
            existing[(key(metric),str(metric.value))] = metric
    return report.model_copy(update={'metrics':list(existing.values())})


def enrich_report(report: ExtractedReport, doc: ParsedDocument) -> ExtractedReport:
    report = _parent_metrics(report, doc)
    rows, check = _nonrecurring(report, doc)
    bridge_rows, bridge_check = _cashflow_bridge(report, doc)
    return report.model_copy(update={"performance_reasons": _reasons(report, doc),
        "financial_details": rows + bridge_rows, "detail_checks": {"nonrecurring": check,
        "cashflow_bridge": bridge_check}})
