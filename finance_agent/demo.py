"""虚构样例和透明的演示提取器；不调用模型、不支持任意真实PDF。"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from .config import ROOT
from .extraction import validate_payload
from .schemas import ExtractedReport, ParsedDocument

COMPANY = "虚构示范制造股份有限公司"
LABELS = {
    "revenue": "营业收入", "net_profit": "净利润", "parent_net_profit": "归母净利润",
    "adjusted_parent_net_profit": "扣非归母净利润", "operating_cashflow": "经营现金流净额",
    "accounts_receivable": "应收账款", "inventory": "存货", "total_assets": "资产总额",
    "nonrecurring_total_parent": "非经常性损益税后归母合计",
}
VALUES = {
    2024: ["100,000.00", "4,500.00", "4,000.00", "3,800.00", "4,200.00", "18,000.00", "20,000.00", "200,000.00", "200.00"],
    2025: ["120,000.00", "5,000.00", "4,500.00", "3,000.00", "-2,000.00", "35,000.00", "32,000.00", "220,000.00", "1,500.00"],
}


def ensure_demo_files() -> list[Path]:
    folder = ROOT / "samples" / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for year, values in VALUES.items():
        path = folder / f"虚构示范制造_{year}年报.pdf"
        paths.append(path)
        if path.exists():
            continue
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        lines = [
            f"{COMPANY} {year}年度报告",
            "全部数值均为虚构演示样本，不代表任何上市公司的真实财务信息。",
            f"报告期：{year}-01-01 至 {year}-12-31；币种：CNY；单位：万元。",
            "合并利润表、合并现金流量表与合并资产负债表摘录（演示）。",
        ]
        for name, amount in zip(LABELS, values):
            start = f"{year}-12-31" if name in ("accounts_receivable", "inventory", "total_assets") else f"{year}-01-01"
            basis = "期末时点" if start.endswith("12-31") else "年初至今累计"
            lines.append(f"{LABELS[name]}：{amount}；{start}至{year}-12-31；{basis}；合并；单位：万元。")
        lines.extend([
            "非经常性损益说明：以披露的税后归母合计核对，不直接加总税前项目。",
            ("本期非经常性损益主要为资产处置收益；税后归母合计为1,500.00万元。" if year == 2025 else
             "本期非经常性损益税后归母合计为200.00万元。"),
            "会计政策、会计估计与合并范围变化：",
            ("2025年1月1日起，部分设备折旧年限由10年调整为20年，影响金额未披露。" if year == 2025 else
             "本期会计政策、会计估计与合并范围未发生变化。"),
            "经营现金流解释：",
            ("本期应收账款与存货增加，经营活动现金流量净额为负。" if year == 2025 else
             "本期经营活动现金流量净额为正，利润与现金流存在正常时间差。"),
        ])
        for index, line in enumerate(lines):
            page.insert_text((35, 45 + index * 30), line, fontname="china-s", fontsize=9)
        doc.set_metadata({"title": f"虚构演示财报{year}", "author": "竞赛原型演示数据"})
        doc.save(path, garbage=4, deflate=True, no_new_id=True)
        doc.close()
    return paths


def extract_demo_report(doc: ParsedDocument) -> ExtractedReport:
    text = "\n".join(b.text for b in doc.blocks)
    if "全部数值均为虚构演示样本" not in text or COMPANY not in text:
        raise ValueError("演示模式只支持系统提供的虚构样例；真实财报请切换到 DeepSeek 模式。")
    match = re.search(r"(2024|2025)年度报告", text)
    if not match:
        raise ValueError("虚构样例报告年度缺失。")
    year = int(match.group(1))
    end = f"{year}-12-31"
    title = next(b for b in doc.blocks if "合并利润表" in b.text)
    metrics = []
    for name, label in LABELS.items():
        source = next(b for b in doc.blocks if b.text.startswith(label + "："))
        amount = re.search(re.escape(label) + r"：([\d,.-]+)", source.text).group(1)
        point = name in ("accounts_receivable", "inventory", "total_assets")
        metrics.append({"name": name, "company": COMPANY, "raw_value": amount, "unit": "万元", "currency": "CNY",
                        "period_start": end if point else f"{year}-01-01", "period_end": end,
                        "basis": "point" if point else "ytd", "scope": "consolidated", "restated": False,
                        "row_quote": source.text,
                        "evidence_ids": [source.id, title.id] if name == "net_profit" else [source.id]})
    nr = next(b for b in doc.blocks if b.text.startswith("本期非经常性损益"))
    accounting = next(b for b in doc.blocks if "折旧年限" in b.text or "未发生变化" in b.text)
    cf = next(b for b in doc.blocks if b.text.startswith("本期应收账款") or b.text.startswith("本期经营活动"))
    disclosures = [{"kind": "nonrecurring", "summary": nr.text, "quote": nr.text, "evidence_ids": [nr.id]},
                   {"kind": "cashflow_reason", "summary": cf.text, "quote": cf.text, "evidence_ids": [cf.id]}]
    if year == 2025:
        disclosures.append({"kind": "accounting_estimate", "summary": "设备折旧年限延长，需核实对利润的影响。",
                            "quote": accounting.text, "evidence_ids": [accounting.id]})
    else:
        for kind in ("accounting_policy", "accounting_estimate", "consolidation"):
            disclosures.append({"kind": kind, "summary": accounting.text, "quote": accounting.text,
                                "evidence_ids": [accounting.id]})
    accounting_heading = next(b for b in doc.blocks if b.text.startswith("会计政策、会计估计"))
    payload = {"company": COMPANY, "report_type": "annual", "period_end": end,
               "metrics": metrics, "disclosures": disclosures,
               "coverage": [{"topic": "nonrecurring", "status": "reviewed", "evidence_ids": [nr.id]},
                            {"topic": "accounting_change", "status": "reviewed", "evidence_ids": [accounting_heading.id, accounting.id]},
                            {"topic": "cashflow_reason", "status": "reviewed", "evidence_ids": [cf.id]}]}
    result = validate_payload(payload, doc, doc.blocks)
    result.warnings.append("虚构演示样本；指标由透明的样例提取器读取，未调用DeepSeek。")
    return result
