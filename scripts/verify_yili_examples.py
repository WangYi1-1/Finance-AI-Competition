"""Compare a saved Yili test with independently transcribed original-PDF amounts. No API calls."""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finance_agent.schemas import RunResult


def verify(result_path: Path, reference_path: Path) -> dict:
    result = RunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    reports = {report.document_id:report for report in result.reports}
    checks = []
    for record in reference["records"]:
        report = reports.get(record["document_id"])
        matches = [m for m in report.metrics if m.name==record["metric"]
            and m.period_start.isoformat()==record["period_start"]
            and m.period_end.isoformat()==record["period_end"] and m.basis==record["basis"]
            and m.scope==record["scope"] and m.company==record["company"] and m.status=="verified"] if report else []
        expected = Decimal(record["value_yuan"])
        values = {m.value for m in matches}
        status = "pass" if values=={expected} else "missing" if not matches else "mismatch"
        checks.append({"document":record["document_name"],"metric":record["metric"],
            "period_end":record["period_end"],"basis":record["basis"],"status":status,
            "expected_yuan":str(expected),"actual_yuan":sorted(str(v) for v in values),
            "reference_pdf_page":record["pdf_page"],
            "actual_pdf_pages":sorted({result.evidence[i].pdf_page for m in matches for i in m.evidence_ids if i in result.evidence})})
    comparisons = []
    for expected in reference["annual_comparisons"]:
        matches = [c for c in result.analysis.comparisons if c.company==reference["company"]
            and c.metric==expected["metric"] and c.kind=="yoy" and c.period_end.isoformat()==expected["period_end"]
            and c.basis==expected["basis"] and c.scope=="consolidated"]
        passed = len(matches)==1 and matches[0].status=="ok" and matches[0].percent is not None
        if passed:
            c = matches[0]
            passed = c.current_value==Decimal(expected["current_value_yuan"]) and c.base_value==Decimal(expected["base_value_yuan"])
            passed = passed and c.delta==Decimal(expected["delta_yuan"]) and str(c.percent.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))==expected["percent_2dp"]
        comparisons.append({"metric":expected["metric"],"period_end":expected["period_end"],
            "status":"pass" if passed else "missing_or_mismatch","expected_percent":expected["percent_2dp"],
            "actual_status":[c.status for c in matches],"actual_percent":[str(c.percent) for c in matches]})
    transitions = []
    for metric in ("parent_net_profit","adjusted_parent_net_profit"):
        matches = [c for c in result.analysis.comparisons if c.metric==metric and c.kind=="yoy"
            and c.basis=="quarter" and c.period_end.isoformat()=="2025-12-31" and c.scope=="consolidated"]
        passed = len(matches)==1 and matches[0].status=="loss_to_profit" and matches[0].percent is None and matches[0].delta is not None
        transitions.append({"metric":metric,"period_end":"2025-12-31","status":"pass" if passed else "missing_or_mismatch",
            "actual_status":[c.status for c in matches],"actual_percent":[str(c.percent) for c in matches]})
    signal_checks = []
    for end,category,expected_status in [
        ("2024-12-31","nonrecurring","detected"),
        ("2025-12-31","nonrecurring","not_detected"),
        ("2024-12-31","cashflow_divergence","not_detected"),
        ("2025-12-31","cashflow_divergence","detected"),
    ]:
        matches = [s for s in result.analysis.signals if s.company==reference["company"]
                   and s.period_end.isoformat()==end and s.category==category]
        passed = len(matches)==1 and matches[0].status==expected_status
        signal_checks.append({"period_end":end,"category":category,"expected_status":expected_status,
            "actual_status":[s.status for s in matches],"status":"pass" if passed else "missing_or_mismatch"})
    summary = {"run_id":result.run_id,"reference_method":reference["method"],"model_calls_for_verification":0,
        "amounts_total":len(checks),"amounts_pass":sum(c["status"]=="pass" for c in checks),
        "amounts_missing":sum(c["status"]=="missing" for c in checks),"amounts_mismatch":sum(c["status"]=="mismatch" for c in checks),
        "annual_amounts_pass":sum(c["status"]=="pass" for c in checks if c["basis"]!="quarter"),
        "quarter_amounts_pass":sum(c["status"]=="pass" for c in checks if c["basis"]=="quarter"),
        "annual_comparisons_total":len(comparisons),"annual_comparisons_pass":sum(c["status"]=="pass" for c in comparisons),
        "transition_checks_pass":sum(c["status"]=="pass" for c in transitions),
        "financial_signal_checks_pass":sum(c["status"]=="pass" for c in signal_checks),
        "usage":result.usage,"warnings":result.warnings,
        "amount_checks":checks,"annual_comparison_checks":comparisons,"transition_checks":transitions,
        "financial_signal_checks":signal_checks}
    folder = result_path.parent
    (folder / "yili_check.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    lines = ["# 伊利两份年报测试核对", "", f"运行：{result.run_id}",
        "独立参考先从原始PDF抄录并检查关键页面，再与模型运行结果比对。此核对不调用API。", "",
        f"- 原文金额：{summary['amounts_pass']}/{len(checks)}通过；缺失{summary['amounts_missing']}；错误{summary['amounts_mismatch']}。",
        f"- 年度本期/基期金额：{summary['annual_amounts_pass']}/36；原文单季度金额：{summary['quarter_amounts_pass']}/32。",
        f"- 年度同比：{summary['annual_comparisons_pass']}/{len(comparisons)}通过。",
        f"- Q4扭亏状态：{summary['transition_checks_pass']}/2通过。",
        f"- 财务关注规则：{summary['financial_signal_checks_pass']}/4通过。", "",
        "金额核对通过仅针对这些已标注字段，不代表附注已完整审阅或全部财报的准确率。", "",
        "| 财报 | 指标 | 截止日 | 口径 | 结果 | 参考原文PDF页 |", "| --- | --- | --- | --- | --- | --- |"]
    lines.extend(f"| {c['document']} | {c['metric']} | {c['period_end']} | {c['basis']} | {c['status']} | {c['reference_pdf_page']} |" for c in checks)
    (folder / "yili_check.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result",type=Path,required=True)
    parser.add_argument("--reference",type=Path,default=ROOT / "output/yili_validation_20261009/manual_reference.json")
    args = parser.parse_args()
    summary = verify(args.result.resolve(),args.reference.resolve())
    print(json.dumps({k:v for k,v in summary.items() if not k.endswith("checks")},ensure_ascii=False,indent=2))
    raise SystemExit(0 if summary["amounts_pass"]==summary["amounts_total"] and summary["annual_comparisons_pass"]==summary["annual_comparisons_total"] and summary["transition_checks_pass"]==2 and summary["financial_signal_checks_pass"]==4 else 1)
