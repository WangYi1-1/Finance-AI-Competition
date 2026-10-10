"""生成可移植的Excel结果快照。仅用标准库，无Node/办公软件运行依赖。

精确元金额按文本保存以避开Excel十五位有效数字限制；阅读列为亿元两位。
所有外部文本写作inlineStr，防止公司名、引文等被当成公式执行。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import math
import unicodedata
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


def _col(number):
    output = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _sheet(rows):
    columns = len(rows[0])
    last = f"{_col(columns)}{len(rows)}"
    column_widths = [min(80, max(25, max((len(str(row[c])) * 1.4 if row[c] is not None else 0 for row in rows), default=25))) for c in range(columns)]
    widths = "".join(f'<col min="{c}" max="{c}" width="{column_widths[c-1]}" customWidth="1"/>' for c in range(1, columns + 1))
    cells = []
    for row_index, row in enumerate(rows, 1):
        parts = []
        for column, value in enumerate(row, 1):
            reference = f"{_col(column)}{row_index}"
            style = 1 if row_index == 1 else 2
            # 导出仅展示的金额列使用Decimal数字，其余精确值为明确标识的文本。
            if isinstance(value, Decimal) and value.is_finite():
                parts.append(f'<c r="{reference}" s="3"><v>{value}</v></c>')
            elif isinstance(value, date):
                serial = (value - date(1899,12,30)).days
                parts.append(f'<c r="{reference}" s="4"><v>{serial}</v></c>')
            elif isinstance(value, int) and not isinstance(value, bool):
                parts.append(f'<c r="{reference}" s="2"><v>{value}</v></c>')
            else:
                content = escape("" if value is None else str(value))
                content = "".join(c for c in content if ord(c) >= 32 or c in "\t\n\r")
                parts.append(f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{content}</t></is></c>')
        # 中文占约两个列宽单位；显式换行需逐行计数，避免长引文末行被遮住。
        lines = max(sum(max(1, math.ceil(sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
            for c in part) / max(1, column_widths[i] - 2))) for part in str(v or '').split('\n'))
            for i, v in enumerate(row))
        height = min(409, max(30, lines * 17 + 8))
        cells.append(f'<row r="{row_index}" ht="{height}" customHeight="1">{"".join(parts)}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last}"/><sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
        f'<sheetFormatPr defaultRowHeight="22"/><cols>{widths}</cols><sheetData>{"".join(cells)}</sheetData>'
        f'<autoFilter ref="A1:{last}"/></worksheet>')


def export_workbook(result, path: Path):
    from .reporting import METRIC_LABELS, METRIC_STATUS, STATUS, BASIS_LABELS, SCOPE_LABELS, metric_label
    categories = {"nonrecurring": "非经常性损益", "accounting_change": "会计口径变化",
                  "cashflow_divergence": "利润与经营现金流背离", "cashflow_bridge": "净利润至经营现金流调节"}
    detail_status = {"reconciled": "合计勾稽通过", "missing": "未取得明细", "incomplete": "明细不完整", "inconsistent": "合计勾稽不符"}
    topics = {**METRIC_LABELS, "profit": "利润"}
    def money(value):
        return (Decimal(value) / Decimal(100000000)).quantize(Decimal("0.01")) if value is not None else "未取得"
    metrics = [["公司", "指标", "起始日", "截止日", "期间类型", "报表口径", "金额（亿元）", "精确金额（元文本）", "原始金额", "原始单位", "核验状态", "来源页码"]]
    def refs(ids):
        return "；".join(dict.fromkeys(f"{result.evidence[e].document_name} 第{result.evidence[e].pdf_page}页" for e in ids if e in result.evidence))
    for report in result.reports:
        for m in report.metrics:
            metrics.append([m.company, metric_label(m.name, m.scope), m.period_start, m.period_end, BASIS_LABELS[m.basis], SCOPE_LABELS[m.scope],
                money(m.value), str(m.value), m.raw_value, m.unit, METRIC_STATUS.get(m.status, m.status), refs(m.evidence_ids)])
    comparisons = [["公司", "指标", "同比/环比", "期间", "口径", "状态", "本期（亿元）", "基期（亿元）", "增减（亿元）", "变化率（%）", "依据与提示"]]
    for c in result.analysis.comparisons:
        comparisons.append([c.company, metric_label(c.metric, c.scope), "同比" if c.kind == "yoy" else "环比",
            f"{c.current_period_start}至{c.period_end}", f"{BASIS_LABELS.get(c.basis, '')}/{SCOPE_LABELS.get(c.scope, '')}",
            STATUS[c.status], money(c.current_value), money(c.base_value), money(c.delta),
            c.percent.quantize(Decimal("0.01")) if c.percent is not None else "不适用/未取得", c.explanation + " ".join(c.cautions)])
    signals = [["公司", "截止日", "类别", "状态", "关注事项", "判断依据", "来源页码"]]
    signals.extend([s.company, s.period_end, categories.get(s.category, s.category), STATUS[s.status], s.title, s.message, refs(s.evidence_ids)] for s in result.analysis.signals)
    reasons = [["公司", "报告截止日（具体适用期见引文）", "指标类别", "公司原文解释", "状态", "来源页码"]]
    details = [["公司", "起始日", "截止日", "明细类别", "项目", "口径", "列示金额（亿元）", "计算贡献（亿元）", "精确元文本", "原始单位", "合计勾稽状态", "来源页码"]]
    for report in result.reports:
        reasons.extend([report.company, report.period_end, "/".join(topics.get(t, t) for t in r["topics"]), r["quote"],
                        "公司原文披露，未独立验证因果" if r["attribution"] == "company_disclosure" else r["attribution"], refs(r["evidence_ids"])] for r in report.performance_reasons)
        for item in report.financial_details:
            details.append([report.company, item["period_start"], item["period_end"], categories.get(item["category"], item["category"]), item["item"], SCOPE_LABELS.get(item["scope"], item["scope"]),
                money(item["value_yuan"]), money(item["contribution_yuan"]), item["value_yuan"], item["raw_unit"],
                detail_status.get(report.detail_checks.get(item["category"], {}).get("status", "missing"), "待核对"), refs(item["evidence_ids"])])
    sources = [["文件", "原始PDF SHA256", "页数", "官方链接", "原始下载时间", "本次核对时间UTC", "运行编号", "程序指纹"]]
    sources.extend([s["name"], s["sha256"], s["pages"], s.get("official_url"), s.get("downloaded_at_utc") or "未记录",
                    s["checked_at_utc"], result.run_id, result.provenance.get("code", {}).get("sha256", "未记录")]
                   for s in result.provenance.get("sources", []))
    sheets = [("财务指标", metrics), ("期间比较", comparisons), ("关注信号", signals), ("变化原因", reasons), ("财务调节明细", details), ("来源与运行", sources)]
    ns = "http://schemas.openxmlformats.org"
    styles = f'''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{ns}/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1C4935"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="5"><xf/><xf fontId="1" fillId="2" applyFill="1" applyFont="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="4" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center" horizontal="right"/></xf><xf numFmtId="164" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1)) + '</Types>')
        archive.writestr("_rels/.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}/spreadsheetml/2006/main" xmlns:r="{ns}/officeDocument/2006/relationships"><sheets>' + "".join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(f'<Relationship Id="rId{i}" Type="{ns}/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1)) + f'<Relationship Id="rId{len(sheets)+1}" Type="{ns}/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/styles.xml", styles)
        for index, (_, rows) in enumerate(sheets, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet(rows))
