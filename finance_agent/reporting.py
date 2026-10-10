"""用确定性模板生成报告；金额与计算结果不经过模型改写。"""
from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from .analysis import comparison_material_hint
from .dashboard import format_display_money, signal_detail_rows
from .schemas import RunResult

METRIC_LABELS = {
    "revenue": "营业收入", "net_profit": "合并净利润", "parent_net_profit": "归母净利润",
    "adjusted_parent_net_profit": "扣非归母净利润", "operating_cashflow": "经营现金流净额",
    "accounts_receivable": "应收账款", "inventory": "存货", "total_assets": "资产总额",
    "nonrecurring_total_parent": "非经常性损益税后归母合计",
}
STATUS = {"detected": "发现关注信号", "not_detected": "已查范围未发现信号",
          "insufficient_evidence": "证据不足", "ok": "已计算", "missing": "缺少可比数据",
          "incomparable": "暂不可比较", "zero_base": "基期为零", "negative_base": "基期为负",
          "near_zero_base": "基期接近零", "profit_to_loss": "不适用（由盈转亏）",
          "loss_to_profit": "不适用（扭亏为盈）", "cashflow_to_outflow": "不适用（由净流入转为净流出）",
          "cashflow_to_inflow": "不适用（由净流出转为净流入）"}
BASIS_LABELS = {"ytd": "年初至今累计", "quarter": "单季度", "point": "期末时点"}
SCOPE_LABELS = {"consolidated": "合并报表", "parent": "母公司报表"}
METRIC_STATUS = {"verified": "已核验", "unverified": "待核验"}


def metric_label(name: str, scope: str | None = None):
    return "母公司净利润" if name == "net_profit" and scope == "parent" else METRIC_LABELS[name]


def clean(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def period_label(start: date | None, end: date, basis: str | None) -> str:
    """把累计、单季度和时点区分开，避免同一截止日看起来重复。"""
    if basis == "point":
        return f"{end.isoformat()}期末时点"
    if start is None:
        return f"截至{end.isoformat()}（期间信息未记录）"
    if basis == "ytd" and start.year == end.year and start.month == 1:
        return f"{end.year}年1—{end.month}月累计"
    if basis == "quarter" and start.year == end.year:
        quarter = (end.month - 1) // 3 + 1
        return f"{end.year}年第{'一二三四'[quarter - 1]}季度（{start.month}—{end.month}月）"
    return f"{start.isoformat()}至{end.isoformat()}（{BASIS_LABELS.get(basis, '期间口径未记录')}）"


def amount_label(value: Decimal | None) -> str:
    """亿元两位仅用于阅读，非零小额额外给出精确元，避免误认为零。"""
    if value is None:
        return "未取得可比金额"
    return format_display_money(value, unit="亿元", include_unit=True)


def _link_label(value: str) -> str:
    return clean(value).replace("[", "\\[").replace("]", "\\]")


def evidence_links(result: RunResult, ids: list[str], anchors: dict[str, str]) -> str:
    links = []
    for eid in dict.fromkeys(ids):
        evidence = result.evidence.get(eid)
        if evidence is None:
            continue
        kind = "表格" if evidence.kind == "table" else "原文段落"
        label = _link_label(f"{evidence.document_name} · PDF第{evidence.pdf_page}页（{kind}）")
        links.append(f"[{label}](#{anchors[eid]})")
    return "；".join(links) or "尚未取得可定位的原文证据"


def generate_report(result: RunResult) -> None:
    folder = Path(result.run_dir)
    used = {x for r in result.reports for m in r.metrics for x in m.evidence_ids}
    used |= {x for signal in result.analysis.signals for x in signal.evidence_ids}
    used |= {x for comparison in result.analysis.comparisons for x in comparison.evidence_ids}
    used |= {x for r in result.reports for item in r.performance_reasons + r.financial_details for x in item["evidence_ids"]}
    used |= {x for r in result.reports for check in r.detail_checks.values() for x in check.get("evidence_ids", [])}
    anchors = {eid: f"evidence-{index}" for index, eid in enumerate(sorted(used), start=1)}
    rows = []
    for report in result.reports:
        for metric in report.metrics:
            rows.append({"company": metric.company, "metric": metric_label(metric.name, metric.scope),
                         "period_start": metric.period_start, "period_end": metric.period_end,
                         "basis": metric.basis, "scope": metric.scope, "restated": metric.restated,
                         "raw_value": metric.raw_value, "raw_unit": metric.unit, "value_yuan": metric.value,
                         "status": metric.status, "evidence_ids": ";".join(metric.evidence_ids)})
    columns = ["company", "metric", "period_start", "period_end", "basis", "scope", "restated",
               "raw_value", "raw_unit", "value_yuan", "status", "evidence_ids"]
    with (folder / "metrics.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    (folder / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    mode_label = "虚构演示，不调用模型" if result.mode == "demo" else (
        "已保存的DeepSeek真实输出重新核验，本次不调用API" if "replay_model_calls" in result.usage else "DeepSeek实时分析")
    lines = ["# 财务报告异常信号分析", "", f"运行编号：{result.run_id}",
             f"运行模式：{mode_label}；模型：{result.model}", "",
             "本报告用于整理财务异常线索。关注信号需要结合原始披露和业务背景复核，不能据此认定财务造假。", "",
             "## 财务指标", "", "金额默认以人民币亿元展示，保留两位小数；CSV与result.json仍保存精确元金额及原始单位。"
             "非零小额若舍入为0.00亿元，会额外标注精确元金额。", "",
             "|公司|指标|期间|口径|金额 亿元|校验状态|证据|", "|---|---|---|---|---:|---|---|"]
    for row in rows:
        lines.append("|" + "|".join(clean(x) for x in (row["company"], row["metric"],
                     period_label(row["period_start"], row["period_end"], row["basis"]),
                     f'{BASIS_LABELS[row["basis"]]}/{SCOPE_LABELS[row["scope"]]}',
                     amount_label(row["value_yuan"]), METRIC_STATUS[row["status"]],
                     evidence_links(result, row["evidence_ids"].split(";"), anchors))) + "|")
    lines += ["", "## 同比与环比", ""]
    if any(c.status == "missing" for c in result.analysis.comparisons):
        lines += ["“缺少可比数据”表示当前金额已经取得，但没有经过核验、期间和口径相符的基期金额。"
                  "同一指标的季度值与累计值分别比较；未提供增长率不等于增长率为零。", ""]
    for comparison in result.analysis.comparisons:
        change = f"{comparison.percent:.2f}%" if comparison.percent is not None else "不提供增长率"
        period = period_label(getattr(comparison, "current_period_start", None), comparison.period_end,
                              getattr(comparison, "basis", None))
        scope = SCOPE_LABELS.get(getattr(comparison, "scope", None), "报表口径未记录")
        base_start = getattr(comparison, "base_period_start", None)
        base_end = getattr(comparison, "base_period_end", None)
        base_period = (f"（{period_label(base_start, base_end, getattr(comparison, 'basis', None))}）"
                       if base_end is not None else "")
        lines += [f"### {comparison.company} · {period} · {metric_label(comparison.metric, comparison.scope)} · "
                  f"{'同比' if comparison.kind == 'yoy' else '环比'}（{scope}）", "",
                  f"状态：{STATUS[comparison.status]}；{change}。", "",
                  f"本期金额：{amount_label(comparison.current_value)}；基期金额{base_period}：{amount_label(comparison.base_value)}。", ""]
        if comparison.delta is not None:
            lines += [f"增减金额：{amount_label(comparison.delta)}。", ""]
        lines += [comparison.explanation, "",
                  f"证据：{evidence_links(result, comparison.evidence_ids, anchors)}", ""]
        lines += [f"关注提示：{clean(caution)}" for caution in comparison.cautions]
        material_hint = comparison_material_hint(comparison, result.reports)
        if material_hint:
            lines += [f"补充或复核材料：{material_hint}", ""]
    lines += ["", "## 异常线索", ""]
    for signal in result.analysis.signals:
        lines += [f"### {signal.company} {signal.period_end} {signal.title}", "",
                  f"状态：{STATUS[signal.status]}。", "", signal.message, "",
                  "计算或判断依据：", "", "|项目|内容|", "|---|---|"]
        for row in signal_detail_rows(signal):
            lines.append(f"|{clean(row['项目'])}|{clean(row['内容'])}|")
        lines += ["", "<details><summary>查看原始计算明细（金额为元）</summary>", "",
                  "```json", json.dumps(signal.details, ensure_ascii=False, indent=2, default=str),
                  "```", "", "</details>", "", f"证据：{evidence_links(result, signal.evidence_ids, anchors)}", ""]
    warnings = list(dict.fromkeys(result.warnings + result.analysis.warnings +
                                [w for report in result.reports for w in report.warnings]))
    lines += ["## 公司披露的变化原因", "", "以下为报告原文解释，不代表已验证因果关系。具体指标、比较期间及累计/单季范围以引文为准。", ""]
    for report in result.reports:
        lines += [f"### {report.company} · {report.period_end}", ""]
        if not report.performance_reasons:
            lines += ["未定位可核实的本期收入或利润变化原因；需补充经营讨论、变动原因或相关附注。", ""]
        for reason in report.performance_reasons:
            lines += [f"- {clean(reason['quote'])}", f"  原文：{evidence_links(result, reason['evidence_ids'], anchors)}", ""]
    lines += ["## 非经常性损益与现金流调节明细", ""]
    for report in result.reports:
        lines += [f"### {report.company} · {report.period_end}", ""]
        for category, title in (("nonrecurring", "非经常性损益"), ("cashflow_bridge", "净利润到经营现金流")):
            check = report.detail_checks.get(category, {})
            lines += [f"#### {title}", "", f"合计核验：{check.get('status', 'missing')}。{check.get('message', '尚未补充核验明细。')}", ""]
            if "residual_yuan" in check:
                lines += [f"公式：{check['formula']}。程序合计：{amount_label(Decimal(check['calculated_yuan']))}；披露合计："
                          f"{amount_label(Decimal(check['reported_yuan']))}；未解释差额：{amount_label(Decimal(check['residual_yuan']))}。", ""]
            items = [r for r in report.financial_details if r["category"] == category]
            if items:
                lines += ["|项目|列示金额 亿元|计算贡献 亿元|期间|证据|", "|---|---:|---:|---|---|"]
                for item in items:
                    lines += ["|" + "|".join(clean(x) for x in (item["item"], amount_label(Decimal(item["value_yuan"])),
                        amount_label(Decimal(item["contribution_yuan"])), f"{item['period_start']}至{item['period_end']}",
                        evidence_links(result, item["evidence_ids"], anchors))) + "|"]
            lines += [""]
    lines += ["## 数据来源与运行版本", "", f"生成时间（UTC）：{result.provenance.get('generated_at_utc', '历史结果未记录')}。",
              f"代码指纹：{result.provenance.get('code', {}).get('sha256', '历史结果未绑定指纹')}。", "",
              "|文件|PDF SHA256|页数|来源|原始下载时间|本次核对时间 UTC|", "|---|---|---:|---|---|---|"]
    for source in result.provenance.get("sources", []):
        url = source.get("official_url")
        link = f"[官方PDF]({url})" if url else "用户指定本地PDF；未提供官方链接"
        lines += ["|" + "|".join(clean(x) for x in (source["name"], source["sha256"], source["pages"], link,
                     source.get("downloaded_at_utc") or "未记录", source["checked_at_utc"])) + "|"]
    lines += ["", "下载时间未知时不会使用文件修改时间代替。artifacts.json将本报告、结构化数据、表格、日志和来源PDF的哈希绑定到运行编号。", "",
              "## 方法与局限", "",
              "- 目前支持可复制文本的中文人民币财报，未执行OCR；扫描页、其他币种或准则需补充处理。",
              "- 指标须通过原文行列、期间、单位和报表口径核验；无法定位或冲突的金额不进入计算。",
              "- 公司原因披露是解释线索，不能视为独立因果证明；未匹配具体期间的引文不解释其他季度。",
              "- 归母利润跨零、基期非正或过小时不输出常规增长率；小基期规则同时参考绝对金额和同期间收入规模。",
              "- 项目关注参数：非经常性影响比例20%、现金流/正利润80%；小基期同时不超过5万元和同期间收入0.01%。这些参数不是监管或舞弊标准。",
              "- 会计政策、估计、合并范围、重述分项保留未查状态，缺少披露不等于无变化。现金流明细未闭合时保留残差。",
              "- 已有八份报告参与开发校验，不构成独立留出集，尚不能宣称整体准确率。模型实时重跑可能变化；离线回放重算已保存输入。", ""]
    lines += ["## 核验范围和待处理事项", ""] + ["- " + warning for warning in warnings]
    lines += ["", "## 原文证据", "", "页码使用PDF实际页序，从1开始，可能与纸面页码不同。", ""]
    for eid in sorted(used):
        evidence = result.evidence.get(eid)
        if evidence:
            kind = "表格" if evidence.kind == "table" else "原文段落"
            lines += [f'<a id="{anchors[eid]}"></a>', "",
                      f"### {_link_label(evidence.document_name)} · PDF第{evidence.pdf_page}页（{kind}）", "",
                      "```text", evidence.text, "```", "",
                      "<details><summary>查看证据编号</summary>", "", f"`{eid}`", "", "</details>", ""]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    from .workbook import export_workbook
    export_workbook(result, folder / "analysis.xlsx")
