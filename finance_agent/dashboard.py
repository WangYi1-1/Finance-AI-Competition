"""展示层数据选择：保留期间、口径、冲突与原文依据，不新增财务计算。"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable

from finance_agent.schemas import Comparison, Metric, RunResult, Signal


MONEY_SCALES = {"亿元": Decimal("100000000"), "万元": Decimal("10000"), "元": Decimal("1")}


def format_display_money(value: Decimal | None, unit: str = "亿元", *,
                         include_unit: bool = True, exact_small: bool = True) -> str:
    """仅换算展示单位，保留非零小额与真实负号；计算仍使用原始元金额。"""
    if unit not in MONEY_SCALES:
        raise ValueError("不支持的展示单位")
    if value is None:
        return "—"
    if not value.is_finite():
        return "待核实"
    scaled = value / MONEY_SCALES[unit]
    small = value != 0 and abs(scaled) < Decimal("0.005")
    number = "≈0.00" if small else f"{scaled:,.2f}"
    label = number + (f" {unit}" if include_unit else "")
    if small and exact_small:
        label += f"（非零小额，精确值 {value:,f} 元）"
    return label


def comparison_chart_rows(comparison: Comparison, unit: str = "亿元") -> list[dict]:
    """可比金额转为图表数据。缺失基期不补零，不可比的两列不并列成比较图。"""
    if unit not in MONEY_SCALES:
        raise ValueError("不支持的展示单位")
    if comparison.status == "incomparable":
        return []
    rows = []
    amounts = [("基期", comparison.base_value, comparison.base_period_start, comparison.base_period_end),
               ("本期", comparison.current_value, comparison.current_period_start, comparison.period_end)]
    for label, value, start, end in amounts:
        if value is None or not value.is_finite():
            continue
        rows.append({"期间": label, "起止日期": f"{start} 至 {end}" if start else str(end),
                     "金额": float(value / MONEY_SCALES[unit]),
                     "金额标签": format_display_money(value, unit, include_unit=False, exact_small=False),
                     "金额显示": format_display_money(value, unit),
                     "期间类型": comparison.basis or "未注明", "报表口径": comparison.scope or "未注明"})
    return rows


@dataclass(frozen=True)
class DisplayAmount:
    value: Decimal | None
    period_start: date | None
    period_end: date
    basis: str
    evidence_ids: tuple[str, ...]
    note: str
    origin: str


@dataclass(frozen=True)
class _QuarterCandidate:
    amount: DisplayAmount
    blocked: bool = False


def _ids(groups: Iterable[Iterable[str]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _verified(metric: Metric, company: str, name: str) -> bool:
    return (metric.company == company and metric.name == name
            and metric.status == "verified" and metric.scope == "consolidated"
            and metric.currency == "CNY" and metric.value.is_finite())


def _full_quarter(start: date | None, end: date | None) -> bool:
    return bool(start is not None and end is not None
                and end.month in (3, 6, 9, 12)
                and end.day == calendar.monthrange(end.year, end.month)[1]
                and start == date(end.year, end.month - 2, 1))


def _metrics(result: RunResult, company: str, name: str) -> list[Metric]:
    return [metric for report in result.reports if report.company == company
            for metric in report.metrics if _verified(metric, company, name)]


def _quarter_origin(result: RunResult, company: str, name: str,
                    start: date | None, end: date, value: Decimal) -> str:
    """只有保留的原始记录支持时，才称季度金额为直接披露或累计差分。"""
    metrics = _metrics(result, company, name)
    if any(m.basis == "quarter" and (m.period_start, m.period_end, m.value) == (start, end, value)
           for m in metrics):
        return "原文披露单季"
    if end.month == 3 and start == date(end.year, 1, 1) and any(
            m.basis == "ytd" and (m.period_start, m.period_end, m.value) == (start, end, value)
            for m in metrics):
        return "第一季度累计与单季等价"
    for later in metrics:
        if (later.basis != "ytd" or later.period_end != end
                or later.period_start != date(end.year, 1, 1)):
            continue
        for earlier in metrics:
            if (earlier.basis == "ytd" and earlier.period_start == later.period_start
                    and earlier.period_end.year == end.year
                    and earlier.period_end.month == end.month - 3
                    and earlier.period_end.day == calendar.monthrange(end.year, earlier.period_end.month)[1]
                    and earlier.restated == later.restated
                    and later.value - earlier.value == value):
                return "相邻累计差分"
    return "程序核验季度"


def _quarter_candidates(result: RunResult, company: str, name: str,
                        through: date, *, include_bases: bool) -> dict[date, list[_QuarterCandidate]]:
    groups: dict[date, list[_QuarterCandidate]] = {}
    direct = [metric for metric in _metrics(result, company, name)
              if metric.basis == "quarter" and metric.period_end <= through
              and _full_quarter(metric.period_start, metric.period_end)]
    for metric in direct:
        restatement_conflict = len({other.restated for other in direct
                                    if (other.period_start, other.period_end)
                                    == (metric.period_start, metric.period_end)}) > 1
        note = ("同季度追溯调整口径冲突，金额暂不展示。" if restatement_conflict
                else "原文披露单季；金额来自已核验原表单元格。")
        amount = DisplayAmount(None if restatement_conflict else metric.value,
                               metric.period_start, metric.period_end, "quarter",
                               tuple(metric.evidence_ids), note, "原文披露单季")
        groups.setdefault(metric.period_end, []).append(_QuarterCandidate(amount, blocked=restatement_conflict))
    for comparison in result.analysis.comparisons:
        if (comparison.company != company or comparison.metric != name
                or comparison.kind != "qoq" or comparison.basis != "quarter"
                or comparison.scope != "consolidated"):
            continue
        periods = [(comparison.current_period_start, comparison.period_end, comparison.current_value)]
        if include_bases and comparison.status != "incomparable":
            periods.append((comparison.base_period_start, comparison.base_period_end, comparison.base_value))
        for start, end, value in periods:
            if end is None or end > through:
                continue
            valid = _full_quarter(start, end)
            if not valid:
                # 未注明完整起止日期的当前季度保留为缺口，不能画成有金额的季度。
                if end != comparison.period_end:
                    continue
                value = None
                note = "季度起止日期不完整或不是完整日历季度，金额暂不展示。"
                origin = "待核实"
            elif comparison.status == "incomparable":
                value = None
                note = "该季度比较暂不可比，金额暂不展示：" + comparison.explanation
                origin = "待核实"
            elif value is None or not value.is_finite():
                value = None
                note = "未取得已核验的单季度金额；缺失不等于零。 " + comparison.explanation
                origin = "待核实"
            else:
                origin = _quarter_origin(result, company, name, start, end, value)
                note = origin + "；金额来自程序核验的季度比较。"
                if comparison.status != "ok":
                    note += " " + comparison.explanation
            amount = DisplayAmount(
                value=value, period_start=start, period_end=end, basis="quarter",
                evidence_ids=tuple(comparison.evidence_ids), note=note, origin=origin,
            )
            groups.setdefault(end, []).append(_QuarterCandidate(
                amount, blocked=not valid or comparison.status == "incomparable"))
    return groups


def _merge_quarter(candidates: list[_QuarterCandidate]) -> DisplayAmount:
    items = [candidate.amount for candidate in candidates]
    first = items[0]
    evidence_ids = _ids(item.evidence_ids for item in items)
    notes = "；".join(dict.fromkeys(item.note for item in items if item.note))
    values = {item.value for item in items if item.value is not None}
    starts = {item.period_start for item in items if item.value is not None}
    blocked = any(candidate.blocked for candidate in candidates)
    if len(values) > 1 or len(starts) > 1 or blocked:
        return DisplayAmount(None, first.period_start, first.period_end, "quarter", evidence_ids,
                             "同一季度金额存在冲突或不可比记录，暂不展示。 " + notes, "待核实")
    if not values:
        return DisplayAmount(None, first.period_start, first.period_end, "quarter", evidence_ids,
                             notes or "未取得已核验单季度金额；缺失不等于零。", "待核实")
    value = next(iter(values))
    valid = [item for item in items if item.value == value]
    rank = {"原文披露单季": 0, "第一季度累计与单季等价": 1, "相邻累计差分": 2, "程序核验季度": 3}
    chosen = min(valid, key=lambda item: rank.get(item.origin, 4))
    return DisplayAmount(value, chosen.period_start, chosen.period_end, "quarter", evidence_ids,
                         notes, chosen.origin)


def select_amount(result: RunResult, company: str, end: date, name: str, basis: str) -> DisplayAmount:
    """选择当前报告期金额；重复同值合并证据，冲突及不匹配口径均留空。"""
    if basis == "quarter":
        groups = _quarter_candidates(result, company, name, end, include_bases=False)
        if end in groups:
            return _merge_quarter(groups[end])
        return DisplayAmount(None, None, end, basis, (),
                             "未取得经过程序核验的单季度金额，不使用累计金额替代。", "待核实")
    if basis not in ("ytd", "point"):
        return DisplayAmount(None, None, end, basis, (), "不支持的金额期间类型。", "待核实")
    candidates = [metric for report in result.reports
                  if report.company == company and report.period_end == end
                  for metric in report.metrics
                  if _verified(metric, company, name) and metric.period_end == end and metric.basis == basis]
    evidence_ids = _ids(metric.evidence_ids for metric in candidates)
    if not candidates:
        return DisplayAmount(None, None, end, basis, evidence_ids,
                             "缺少本报告期已核验的合并报表人民币金额；缺失不等于零。", "待核实")
    fingerprints = {(m.period_start, m.period_end, m.basis, m.restated, m.value) for m in candidates}
    if len(fingerprints) != 1:
        return DisplayAmount(None, None, end, basis, evidence_ids,
                             "同期间金额、起止日期或追溯调整口径存在冲突，暂不展示。", "待核实")
    metric = candidates[0]
    valid_period = (metric.period_start == date(end.year, 1, 1) if basis == "ytd"
                    else metric.period_start == end)
    if not valid_period:
        return DisplayAmount(None, metric.period_start, end, basis, evidence_ids,
                             "起止日期与累计／期末时点期间类型不一致，暂不展示。", "待核实")
    period_label = "年初至今累计" if basis == "ytd" else "期末时点"
    adjustment = "；已标记追溯调整" if metric.restated else ""
    return DisplayAmount(metric.value, metric.period_start, end, basis, evidence_ids,
                         f"{period_label}；合并报表；人民币元{adjustment}。", "原文核验金额")


def quarter_series(result: RunResult, company: str, name: str, through: date) -> list[DisplayAmount]:
    """从核验后的原文单季及分析层季度比较生成趋势，缺失不填零。"""
    groups = _quarter_candidates(result, company, name, through, include_bases=True)
    return [_merge_quarter(groups[end]) for end in sorted(groups)]


def filter_result(result: RunResult, company: str, end: date) -> RunResult:
    """筛选当前结果视图，保留该公司跨期证据；导出仍应使用完整原结果。"""
    document_ids = {report.document_id for report in result.reports if report.company == company}
    return result.model_copy(deep=True, update={
        "reports": [report.model_copy(deep=True) for report in result.reports
                    if report.company == company and report.period_end == end],
        # 复用已核验模型的类型，避免开发热更新后的旧会话对象与新类身份冲突。
        "analysis": result.analysis.model_copy(deep=True, update={
            "comparisons": [item.model_copy(deep=True) for item in result.analysis.comparisons
                            if item.company == company and item.period_end == end],
            "signals": [item.model_copy(deep=True) for item in result.analysis.signals
                        if item.company == company and item.period_end == end],
            "warnings": list(result.analysis.warnings),
        }),
        "evidence": {key: item.model_copy(deep=True) for key, item in result.evidence.items()
                     if item.document_id in document_ids},
    })


_KINDS = {"nonrecurring": "非经常性损益", "accounting_policy": "会计政策", "accounting_estimate": "会计估计",
          "consolidation": "合并范围", "restatement": "追溯调整", "cashflow_reason": "现金流原因"}
_STATUSES = {"ok": "已计算", "missing": "缺少对比数据", "incomparable": "暂不可比较", "zero_base": "基期为零",
             "negative_base": "基期为负，不提供常规增长率", "near_zero_base": "基期接近零",
             "profit_to_loss": "不适用（由盈转亏）", "loss_to_profit": "不适用（扭亏为盈）",
             "cashflow_to_outflow": "不适用（由净流入转为净流出）",
             "cashflow_to_inflow": "不适用（由净流出转为净流入）"}
_MONEY_FIELDS = {
    "parent_net_profit": "归母净利润", "adjusted_parent_net_profit": "扣非归母净利润",
    "derived_nonrecurring": "归母与扣非归母差额", "disclosed_total_parent": "披露的税后归母非经常性损益总额",
    "reconciliation_tolerance": "金额核对允许误差", "reconciliation_difference": "差额与披露总额的差异",
    "net_profit": "合并净利润", "operating_cashflow": "合并经营现金流净额",
    "profit_yoy_delta": "合并净利润同比增减金额", "cashflow_yoy_delta": "经营现金流同比增减金额",
}


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def signal_detail_rows(signal: Signal, unit: str = "亿元") -> list[dict[str, str]]:
    """将既有信号明细变成中文说明，金额与比例仅格式化，不重算规则。"""
    rows: list[dict[str, str]] = []

    def append(label: str, content: str) -> None:
        rows.append({"项目": label, "内容": content})

    for key, value in signal.details.items():
        number = _decimal(value)
        if key in _MONEY_FIELDS:
            if key in {"reconciliation_tolerance", "reconciliation_difference"}:
                content = "待核实" if number is None else f"{number:,f} 元（核对精度）"
            else:
                content = "待核实" if number is None else format_display_money(number, unit)
            append(_MONEY_FIELDS[key], content)
        elif key == "absolute_profit_ratio":
            append("非经常性损益／归母利润绝对值", "暂不适用" if number is None else f"{number * 100:.2f}%")
        elif key == "cashflow_profit_ratio":
            append("经营现金流／合并净利润", "暂不适用" if number is None else f"{number:.4f} 倍")
        elif key == "threshold_ratio":
            content = (f"{number * 100:.2f}%" if signal.category == "nonrecurring" else f"{number:.4f} 倍") if number is not None else "待核实"
            append("演示关注阈值", content)
        elif key == "formula":
            append("计算公式", str(value))
        elif key == "rule_type":
            append("判断方式", {"heuristic": "基于演示阈值的关注规则", "disclosure": "核查原文明确披露"}.get(str(value), str(value)))
        elif key in ("reviewed_kinds", "missing_kinds"):
            names = [_KINDS.get(str(item), str(item)) for item in value] if isinstance(value, list) else []
            append("已核对的披露类型" if key == "reviewed_kinds" else "尚缺的披露类型", "、".join(names) or "未注明")
        elif key == "changes" and isinstance(value, list):
            for change in value:
                if isinstance(change, dict):
                    description = str(change.get("summary") or change.get("quote") or "待核实")
                    append(_KINDS.get(str(change.get("kind")), "会计口径变化") + "披露", description)
        elif key == "reasons":
            descriptions = [str(item) for item in value] if isinstance(value, list) else []
            append("公司披露的原因（仍需核对）", "；".join(descriptions) or "尚未保留明确原因说明")
        elif key == "reason_reviewed":
            append("原因披露核对范围", "已定位相关披露" if value is True else "相关披露尚未充分覆盖")
        elif key in ("profit_yoy_status", "cashflow_yoy_status"):
            append("合并净利润同比状态" if key == "profit_yoy_status" else "经营现金流同比状态", _STATUSES.get(str(value), "未注明"))
        elif key == "trend_divergence":
            append("同比趋势核验", "合并净利润增加而经营现金流下降" if value is True else "当前可比数据未同时满足利润增加、现金流下降")
        else:
            content = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            append(f"补充信息（{key}）", content)
    return rows
