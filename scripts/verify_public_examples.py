"""比对保存的真实分析结果与人工核对金额，不调用模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finance_agent.schemas import RunResult
from finance_agent.reporting import METRIC_LABELS


def verify(result_path: Path) -> dict:
    manifest = json.loads((ROOT / "samples/public/manifest.json").read_text(encoding="utf-8"))
    result = RunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    amounts = []
    for sample in manifest["samples"]:
        source = ROOT / "samples/public" / sample["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError(f"来源文件摘要不符：{sample['file']}")
        report = next((r for r in result.reports if r.document_id == sample["sha256"][:16]), None)
        for name, expected in sample["expected_metrics_yuan"].items():
            matches = [] if report is None else [m for m in report.metrics
                if m.name == name and m.status == "verified" and m.scope == sample["scope"]
                and m.currency == sample["currency"] and str(m.period_start) == sample["period_start"]
                and str(m.period_end) == sample["period_end"] and m.basis in ("ytd", "quarter")]
            values = {m.value for m in matches}
            state = "正确" if values == {Decimal(expected)} else "缺失" if not values else "错误或冲突"
            amounts.append({"sample": sample["id"], "file": sample["file"], "metric": name,
                            "expected_yuan": expected, "actual_yuan": sorted(str(v) for v in values),
                            "status": state, "source_url": sample["url"]})
    quarters = []
    target = manifest["huahong_2024q3_qoq"]
    for name, expected in target["expected"].items():
        matches = [c for c in result.analysis.comparisons if c.company == "华虹半导体有限公司"
            and c.metric == name and c.kind == "qoq" and c.basis == "quarter"
            and str(c.current_period_start) == target["current_period_start"]
            and str(c.period_end) == target["current_period_end"]]
        c = matches[0] if len(matches) == 1 else None
        expected_status = expected.get("status", "ok")
        valid = c is not None and c.status == expected_status and c.current_value == Decimal(expected["current_yuan"])
        valid = valid and c.base_value == Decimal(expected["base_yuan"])
        valid = valid and str(c.base_period_start) == target["base_period_start"]
        valid = valid and str(c.base_period_end) == target["base_period_end"]
        if expected["percent_rounded"] is None:
            valid = valid and c.percent is None
        else:
            valid = valid and c.percent is not None and c.percent.quantize(Decimal("0.01")) == Decimal(expected["percent_rounded"])
        if "delta_yuan" in expected:
            valid = valid and c.delta == Decimal(expected["delta_yuan"])
        quarters.append({"metric": name, "status": "正确" if valid else "缺失或不一致",
                         "expected_percent": expected["percent_rounded"],
                         "expected_status": expected_status, "actual_status": c.status if c is not None else None,
                         "actual_percent": str(c.percent) if c is not None and c.percent is not None else None})
    checks = []
    for company in ("TCL科技集团股份有限公司", "科大讯飞股份有限公司"):
        nonrec = next((s for s in result.analysis.signals if s.company.replace(" ", "") == company
                      and s.category == "nonrecurring" and str(s.period_end) == "2024-09-30"), None)
        cash = next((s for s in result.analysis.signals if s.company.replace(" ", "") == company
                    and s.category == "cashflow_divergence" and str(s.period_end) == "2024-09-30"), None)
        checks.append({"company": company, "check": "非经常性损益达到项目20%关注阈值",
                       "status": "正确" if nonrec is not None and nonrec.status == "detected" else "缺失或不一致"})
        checks.append({"company": company, "check": "合并净利润为负时，不误报正利润现金转化比例正常",
                       "status": "正确" if cash is not None and cash.status == "insufficient_evidence" else "缺失或不一致"})
    inspur_signals = {s.category: s for s in result.analysis.signals
                      if s.company == "浪潮电子信息产业股份有限公司" and str(s.period_end) == "2024-09-30"}
    for category, expected, label in [("cashflow_divergence", "detected", "正合并利润与负经营现金流触发背离线索"),
                                       ("nonrecurring", "not_detected", "非经常性损益低于项目20%关注阈值"),
                                       ("accounting_change", "detected", "明确会计政策变更与比较期间重述")]:
        signal = inspur_signals.get(category)
        checks.append({"company": "浪潮电子信息产业股份有限公司", "check": label,
                       "status": "正确" if signal is not None and signal.status == expected else "缺失或不一致"})
    adjusted_bases = {"revenue": "48256015730.22", "parent_net_profit": "774773148.36",
                      "adjusted_parent_net_profit": "417628799.62", "operating_cashflow": "-5601729446.48"}
    inspur_yoy = {c.metric: c for c in result.analysis.comparisons
                  if c.company == "浪潮电子信息产业股份有限公司" and c.kind == "yoy"
                  and c.basis == "ytd" and str(c.period_end) == "2024-09-30"}
    aligned = all(name in inspur_yoy and inspur_yoy[name].base_value == Decimal(value)
                  and inspur_yoy[name].status in {"ok", "negative_base"} for name, value in adjusted_bases.items())
    checks.append({"company": "浪潮电子信息产业股份有限公司", "check": "4项同比明确使用重述后的基期金额",
                   "status": "正确" if aligned else "缺失或不一致"})
    cash_yoy = inspur_yoy.get("operating_cashflow")
    checks.append({"company": "浪潮电子信息产业股份有限公司", "check": "负现金流基期不展示普通增长率，描述净流出扩大",
                   "status": "正确" if cash_yoy is not None and cash_yoy.status == "negative_base"
                       and cash_yoy.percent is None and "净流出扩大" in cash_yoy.explanation else "缺失或不一致"})
    tcl_yoy = {c.metric: c for c in result.analysis.comparisons
               if c.company == "TCL科技集团股份有限公司" and c.kind == "yoy"
               and c.basis == "ytd" and str(c.period_end) == "2024-09-30"}
    tcl_cash = tcl_yoy.get("operating_cashflow")
    checks.append({"company": "TCL科技集团股份有限公司", "check": "现金流比较列恢复，220.01亿元对161.44亿元，同比增长36.28%",
        "status": "正确" if tcl_cash is not None and tcl_cash.status == "ok"
        and tcl_cash.current_value == Decimal("22000714536") and tcl_cash.base_value == Decimal("16144013013")
        and tcl_cash.percent is not None and tcl_cash.percent.quantize(Decimal("0.01")) == Decimal("36.28") else "缺失或不一致"})
    tcl_profit = tcl_yoy.get("net_profit")
    checks.append({"company": "TCL科技集团股份有限公司", "check": "由盈转亏仅展示减少73.97亿元，保留真实负数",
        "status": "正确" if tcl_profit is not None and tcl_profit.status == "profit_to_loss"
        and tcl_profit.percent is None and tcl_profit.current_value == Decimal("-1829006223")
        and tcl_profit.base_value == Decimal("5568444023") and tcl_profit.delta == Decimal("-7397450246") else "缺失或不一致"})
    summary = {"run_id": result.run_id, "mode": result.mode, "amount_total": len(amounts),
               "amount_correct": sum(x["status"] == "正确" for x in amounts),
               "quarter_total": len(quarters), "quarter_correct": sum(x["status"] == "正确" for x in quarters),
               "rule_checks": checks, "amount_checks": amounts, "quarter_checks": quarters,
               "usage": result.usage, "scope": f"仅验证这{len(manifest['samples'])}份财报的人工核对字段和项目规则，不能推算整体准确率"}
    (result_path.parent / "public_check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 公开财报样本核对", "", f"运行编号：{result.run_id}", "", summary["scope"], "",
             f"金额字段：{summary['amount_correct']}/{summary['amount_total']}；华虹第三季度环比：{summary['quarter_correct']}/{summary['quarter_total']}。", "",
             "|样本|指标|人工核对金额（元）|程序金额（元）|结果|", "|---|---|---:|---:|---|"]
    for x in amounts:
        lines.append(f"|[{x['file']}]({x['source_url']})|{METRIC_LABELS[x['metric']]}|{x['expected_yuan']}|{', '.join(x['actual_yuan']) or '未提取'}|{x['status']}|")
    lines += ["", "|华虹第三季度环比指标|人工预期|程序结果|核对|", "|---|---:|---:|---|"]
    for x in quarters:
        actual = f"{Decimal(x['actual_percent']):.2f}%" if x["actual_percent"] is not None else x["actual_status"] or "未计算"
        expected = f"{x['expected_percent']}%" if x["expected_percent"] is not None else "由净流入转为净流出，不提供增长率"
        lines.append(f"|{METRIC_LABELS[x['metric']]}|{expected}|{actual}|{x['status']}|")
    lines += ["", "## 规则检查", ""] + [f"- {x['company']}：{x['check']}——{x['status']}。" for x in checks]
    (result_path.parent / "public_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if (result.mode == "live" and summary["amount_correct"] == summary["amount_total"]
            and summary["quarter_correct"] == summary["quarter_total"]
            and all(c["status"] == "正确" for c in checks)
            and result_path.parent.is_relative_to(ROOT / "output")):
        index = {"run_id": result.run_id, "result_path": result_path.relative_to(ROOT).as_posix(),
                 "amounts_checked": len(amounts), "qoq_checked": len(quarters), "rules_checked": len(checks)}
        (ROOT / "samples/public/latest_verified_run.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True, help="真实运行生成的result.json")
    args = parser.parse_args()
    report = verify(args.result.resolve())
    print(f"金额核对 {report['amount_correct']}/{report['amount_total']}，环比核对 {report['quarter_correct']}/{report['quarter_total']}")
    print(f"核对报告：{args.result.resolve().parent / 'public_check.md'}")
    if report["amount_correct"] != report["amount_total"] or report["quarter_correct"] != report["quarter_total"] or any(c["status"] != "正确" for c in report["rule_checks"]):
        raise SystemExit(1)
