"""可复算的财务规则。所有金额已经统一为元，比例使用 Decimal。

阈值是项目演示的关注规则，并非会计准则、监管认定或舞弊判定。
本模块只消费经过证据核验的数据；缺少资料不会被解释为正常。
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import re
from typing import Iterable, Literal

from .schemas import AnalysisResult, Comparison, ExtractedReport, Metric, Signal


@dataclass(frozen=True)
class RuleConfig:
    # 统一单位为元后，正基期不超过 1 元时不输出增长率。
    near_zero_base: Decimal = Decimal("1")
    nonrecurring_ratio: Decimal = Decimal("0.20")
    cashflow_profit_ratio: Decimal = Decimal("0.80")
    reconciliation_absolute_tolerance: Decimal = Decimal("1")
    reconciliation_relative_tolerance: Decimal = Decimal("0.000001")
    # 项目关注参数，不是会计准则。只有金额与同期间收入规模同时很小时才抑制百分比。
    small_base_amount: Decimal = Decimal("50000")
    small_base_revenue_ratio: Decimal = Decimal("0.0001")
    large_growth_percent: Decimal = Decimal("1000")


DEFAULT_RULES = RuleConfig()
FLOW_METRICS = frozenset({
    "revenue", "net_profit", "parent_net_profit", "adjusted_parent_net_profit",
    "operating_cashflow", "nonrecurring_total_parent",
})
STOCK_METRICS = frozenset({"accounts_receivable", "inventory", "total_assets"})
PROFIT_METRICS = frozenset({"net_profit", "parent_net_profit", "adjusted_parent_net_profit"})


def _ids(*groups: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for group in groups for item in group))


def _month_end(day: date) -> bool:
    return day.day == monthrange(day.year, day.month)[1]


def _same_calendar_position(a: date, b: date) -> bool:
    return (a.month, a.day) == (b.month, b.day) or (
        a.month == b.month and _month_end(a) and _month_end(b)
    )


def _is_quarter(metric: Metric) -> bool:
    start, end = metric.period_start, metric.period_end
    return (
        metric.basis == "quarter" and start.year == end.year
        and start.day == 1 and start.month in (1, 4, 7, 10)
        and end.month == start.month + 2 and _month_end(end)
    )


def _identity_problem(current: Metric, base: Metric, *, same_metric: bool = True,
                      restatement_aligned: bool = False) -> str | None:
    if current.status != "verified" or base.status != "verified":
        return "至少一个数值未经证据核验，不能用于运算。"
    if current.company != base.company:
        return "公司不同，不能比较。"
    if current.currency != base.currency:
        return "币种不同，不能比较。"
    if current.scope != base.scope:
        return "合并报表与母公司报表口径不同，不能比较。"
    if same_metric and current.name != base.name:
        return "指标不同，不能比较。"
    if current.restated != base.restated and not restatement_aligned:
        return "追溯调整标记不同，资料尚未证实基期已经按本期口径重述，暂不比较。"
    if current.period_start > current.period_end or base.period_start > base.period_end:
        return "报告期间起止日期不合法。"
    if not current.value.is_finite() or not base.value.is_finite():
        return "数值不是有限金额，不能用于运算。"
    for metric in (current, base):
        if metric.name in STOCK_METRICS and metric.basis != "point":
            return "资产、存货及应收账款应使用期末时点余额，不能按累计或单季度流量比较。"
        if metric.name in FLOW_METRICS and metric.basis == "point":
            return "收入、利润及现金流应使用期间流量，不能按期末时点余额比较。"
        if metric.basis == "point" and metric.period_start != metric.period_end:
            return "时点余额的起止日期必须是同一个期末日期。"
        if metric.basis == "quarter" and not _is_quarter(metric):
            return "单季度口径需要完整的一个日历季度。"
        if metric.basis == "ytd" and metric.period_start != date(metric.period_end.year, 1, 1):
            return "年初累计口径必须从同一年的 1 月 1 日开始。"
    return None


def compare_metrics(current: Metric, base: Metric | None,
                    kind: Literal["yoy", "qoq"], *,
                    restatement_aligned: bool = False,
                    config: RuleConfig = DEFAULT_RULES) -> Comparison:
    """计算同比或环比。盈亏或现金流方向切换、非正/近零基期只返回差额和状态。

    restatement_aligned 只能在调用方已定位明确可比口径的披露时传 True。
    年度同比按日历期间匹配，因此跨闰年的年度数据依然可比。
    """
    result = dict(
        id=f"{current.company}:{current.name}:{current.scope}:{current.currency}:{current.basis}:{current.period_start}:{current.period_end}:{kind}",
        metric=current.name, company=current.company, kind=kind,
        period_end=current.period_end, current_value=current.value,
        current_period_start=current.period_start, basis=current.basis, scope=current.scope,
        base_period_start=base.period_start if base is not None else None,
        base_period_end=base.period_end if base is not None else None,
        base_value=base.value if base is not None else None,
        evidence_ids=_ids(current.evidence_ids, base.evidence_ids if base else []),
    )
    current_problem = _identity_problem(current, current)
    if current_problem:
        return Comparison(**result, status="incomparable", explanation=current_problem)
    if base is None:
        if kind == "yoy":
            def previous_year(day: date) -> date:
                year = day.year - 1
                return date(year, day.month, min(day.day, monthrange(year, day.month)[1]))
            target_start = previous_year(current.period_start)
            target_end = previous_year(current.period_end)
        elif current.basis == "quarter":
            if not _is_quarter(current):
                return Comparison(**result, status="incomparable", explanation="本期起止日期不是完整单季度，不能计算季度环比。")
            target_end = current.period_start - timedelta(days=1)
            target_start = date(target_end.year, target_end.month - 2, 1)
        elif current.basis == "point":
            if current.period_end.month not in (3, 6, 9, 12) or not _month_end(current.period_end):
                return Comparison(**result, status="incomparable", explanation="时点季度环比需要季度末余额。")
            absolute_month = current.period_end.year * 12 + current.period_end.month - 1 - 3
            year, month = divmod(absolute_month, 12)
            target_end = date(year, month + 1, monthrange(year, month + 1)[1])
            target_start = target_end
        else:
            return Comparison(**result, status="missing", explanation="累计金额需先转换为单季度，再取得上一季度金额计算环比。")
        basis_label = {"ytd": "累计", "quarter": "单季度", "point": "期末时点"}[current.basis]
        scope_label = "合并报表" if current.scope == "consolidated" else "母公司报表"
        return Comparison(**result, status="missing", explanation=(
            f"尚未取得已核验的{target_start}至{target_end}{basis_label}金额（{scope_label}），"
            "缺少可比金额不等于该期间金额为零。"))
    problem = _identity_problem(current, base, restatement_aligned=restatement_aligned)
    if problem is None and current.basis != base.basis:
        problem = "单季度、年初累计或时点口径不同，不能比较。"
    if problem is None:
        if kind == "yoy":
            comparable = (
                current.period_end.year == base.period_end.year + 1
                and current.period_start.year == base.period_start.year + 1
                and _same_calendar_position(current.period_start, base.period_start)
                and _same_calendar_position(current.period_end, base.period_end)
            )
            if not comparable:
                problem = "同比需要上一年度相同日历期间，当前两个期间不匹配。"
        elif current.basis == "quarter":
            if not (_is_quarter(current) and _is_quarter(base)
                    and current.period_start == base.period_end + timedelta(days=1)):
                problem = "季度环比需要两个相邻的完整单季度。"
        elif current.basis == "point":
            month_gap = (current.period_end.year - base.period_end.year) * 12 + current.period_end.month - base.period_end.month
            if not (month_gap == 3 and current.period_end.month in (3, 6, 9, 12)
                    and base.period_end.month in (3, 6, 9, 12)
                    and _month_end(current.period_end) and _month_end(base.period_end)):
                problem = "时点环比需要相邻季度末的余额。"
        else:
            problem = "累计金额不能直接做季度环比，需先得到可比单季度金额。"
    if problem:
        return Comparison(**result, status="incomparable", explanation=problem)
    delta = current.value - base.value
    warning = " 本期与基期已依据同一报告的明确追溯调整披露对齐，仍须保留调整说明。" if current.restated != base.restated else ""
    if current.value < 0 < base.value or base.value < 0 < current.value:
        if current.name in PROFIT_METRICS:
            status = "profit_to_loss" if current.value < 0 else "loss_to_profit"
            transition = "由盈转亏" if current.value < 0 else "扭亏为盈"
            return Comparison(**result, status=status, delta=delta,
                              explanation=f"本期与基期利润跨越零点，{transition}；为避免常规增长率掩盖盈亏变化，仅展示增减金额。" + warning)
        if current.name == "operating_cashflow":
            status = "cashflow_to_outflow" if current.value < 0 else "cashflow_to_inflow"
            transition = "由净流入转为净流出" if current.value < 0 else "由净流出转为净流入"
            return Comparison(**result, status=status, delta=delta,
                              explanation=f"经营现金流{transition}；为避免常规增长率掩盖现金流方向变化，仅展示增减金额。" + warning)
    if base.value == 0:
        return Comparison(**result, status="zero_base", delta=delta,
                          explanation="基期为零，增长率无定义；展示增减金额。" + warning)
    if base.value < 0:
        if current.name == "operating_cashflow":
            transition = "本期经营现金流转为净流入。" if current.value > 0 else (
                "本期经营现金流收支平衡。" if current.value == 0 else (
                    "本期经营现金净流出收窄。" if delta > 0 else "本期经营现金净流出扩大。" if delta < 0 else "本期经营现金净流出不变。"
                )
            )
        elif current.name in PROFIT_METRICS:
            transition = "本期扭亏为盈。" if current.value > 0 else (
                "本期亏损收窄至零。" if current.value == 0 else (
                    "本期亏损收窄。" if delta > 0 else "本期亏损扩大。" if delta < 0 else "本期亏损金额不变。"
                )
            )
        else:
            transition = "本期金额增加。" if delta > 0 else "本期金额减少。" if delta < 0 else "本期金额不变。"
        return Comparison(**result, status="negative_base", delta=delta,
                          explanation="基期为负，不输出常规增长率；" + transition + warning)
    if base.value <= config.near_zero_base:
        return Comparison(**result, status="near_zero_base", delta=delta,
                          explanation=f"正基期不超过 {config.near_zero_base} 元，增长率容易失真；展示增减金额。" + warning)
    return Comparison(**result, status="ok", delta=delta,
                      percent=delta / base.value * Decimal("100"),
                      cautions=[f"增长率绝对值达到项目关注阈值{config.large_growth_percent}%；应结合基期规模和增减金额解读，不能仅凭百分比认定异常。"]
                          if abs(delta / base.value * Decimal("100")) >= config.large_growth_percent else [],
                      explanation="增长率 =（本期－基期）÷基期 × 100%；已核对公司、期间和报表口径。" + warning)


def _comparison_target_period(comparison: Comparison) -> tuple[date, date] | None:
    """缺失金额所需的日历期间，不把缺失基期伪装成已提取金额。"""
    start, end = comparison.current_period_start, comparison.period_end
    if comparison.basis not in {"ytd", "quarter", "point"} or start is None:
        return None
    if comparison.kind == "yoy":
        def previous_year(day: date) -> date:
            year = day.year - 1
            return date(year, day.month, min(day.day, monthrange(year, day.month)[1]))
        return previous_year(start), previous_year(end)
    if end.month not in (3, 6, 9, 12) or not _month_end(end):
        return None
    absolute_month = end.year * 12 + end.month - 1 - 3
    year, month = divmod(absolute_month, 12)
    target_end = date(year, month + 1, monthrange(year, month + 1)[1])
    if comparison.basis == "point":
        return target_end, target_end
    return date(target_end.year, target_end.month - 2, 1), target_end


def _report_title(end: date) -> str:
    suffix = {3: "第一季度报告", 6: "半年度报告", 9: "第三季度报告", 12: "年度报告"}
    return f"{end.year}年{suffix[end.month]}" if end.month in suffix and _month_end(end) else f"截至{end}的报告"


def _quarter_material_ends(end: date) -> tuple[date, ...]:
    if end.month == 3:
        return (end,)
    earlier_month = end.month - 3
    return end, date(end.year, earlier_month, monthrange(end.year, earlier_month)[1])


def comparison_material_hint(comparison: Comparison,
                             reports: Iterable[ExtractedReport] = ()) -> str:
    """给出具体补料或复核步骤；已上传报告不会被要求无条件重复上传。

    优先核对本期报告的比较列。单季可以直接披露，也可以由同年相邻累计
    金额差分取得；Q4 需要年度累计和三季累计，不能用年度金额代替单季。
    本函数只提示材料，不新增金额、不放宽核验或追溯调整要求。
    """
    if comparison.status != "missing":
        return ""
    target = _comparison_target_period(comparison)
    if target is None:
        return "需先明确本期和基期的起止日期及累计、单季或时点口径，再核对报告比较列或补充对应期间报告。"
    start, end = target
    own_reports = [report for report in reports if report.company == comparison.company]
    has_current = any(report.period_end == comparison.period_end for report in own_reports)
    prefix = ("请优先复核已上传本期报告的上年同期比较列；" if comparison.kind == "yoy" and has_current
              else "请先核对本期报告中是否已披露对应基期金额；")
    basis = "quarter" if comparison.kind == "qoq" and comparison.basis == "ytd" else comparison.basis
    label = (f"{end}期末余额" if basis == "point" else
             f"{start}至{end}{'单季' if basis == 'quarter' else '累计'}金额")
    records = [metric for report in own_reports for metric in report.metrics
               if metric.company == comparison.company and metric.name == comparison.metric
               and metric.scope == comparison.scope and (metric.period_start, metric.period_end) == (start, end)
               and (metric.basis == basis or (end.month == 3 and start == date(end.year, 1, 1)
                                             and metric.basis == "ytd" and basis == "quarter"))]
    if records:
        return (prefix + f"上传材料中已有{label}记录，但尚未形成通过核验的可比金额。"
                "请复核对应行、列、单位、报表口径和追溯调整标记，不必重复上传同一文件。")
    required_ends = _quarter_material_ends(end) if basis == "quarter" else (end,)
    uploaded_ends = {report.period_end for report in own_reports}
    present = [_report_title(day) for day in required_ends if day in uploaded_ends]
    missing = [_report_title(day) for day in required_ends if day not in uploaded_ends]
    present_text = ("、".join(present) + "已上传，但尚未取得该指标的已核验可比金额，请先复核原文。") if present else ""
    if basis == "quarter" and end.month != 3:
        method = (f"基期需{label}：可使用已核验的原文单季披露；若只有累计金额，"
                  f"应以{_report_title(required_ends[0])}累计减{_report_title(required_ends[1])}累计取得，且口径须一致。")
        missing_text = ("若现有报告未直接披露该单季金额，需上传" + "、".join(missing) + "。") if missing else "不必重复上传现有文件，应复核差分所需金额及口径。"
    else:
        method = f"基期需{label}。"
        missing_text = ("若本期报告未披露可比列，需上传" + "、".join(missing) + "。") if missing else "不必重复上传现有文件，应复核所需金额及口径。"
    return prefix + method + present_text + missing_text


def derive_quarter(later_ytd: Metric, earlier_ytd: Metric, *,
                   restatement_aligned: bool = False) -> Metric:
    """以相邻累计期间差分得到季度流量；资产、应收账款等余额禁止差分。"""
    problem = _identity_problem(later_ytd, earlier_ytd, restatement_aligned=restatement_aligned)
    if problem:
        raise ValueError(problem)
    if later_ytd.name not in FLOW_METRICS:
        raise ValueError("时点余额禁止累计差分；仅流量指标可转换为单季度。")
    a, b = later_ytd, earlier_ytd
    if not (a.basis == b.basis == "ytd" and a.period_start == b.period_start
            and a.period_start.month == a.period_start.day == 1
            and a.period_start.year == a.period_end.year == b.period_end.year
            and a.period_end.month in (6, 9, 12)
            and b.period_end.month == a.period_end.month - 3
            and _month_end(a.period_end) and _month_end(b.period_end)):
        raise ValueError("差分需要同一年从 1 月 1 日开始、相邻季度末的累计期间。")
    value = a.value - b.value
    return a.model_copy(update={
        "value": value, "raw_value": f"{a.value} - ({b.value}) = {value}",
        "unit": "元", "period_start": b.period_end + timedelta(days=1),
        "basis": "quarter", "evidence_ids": _ids(a.evidence_ids, b.evidence_ids),
        "checks": _ids(a.checks, b.checks, ["由相邻累计流量差分得到季度值，计算单位为元"]),
    })


def _same_period(a: Metric, b: Metric) -> bool:
    if (a.period_start, a.period_end) != (b.period_start, b.period_end):
        return False
    return a.basis == b.basis or (
        a.name in FLOW_METRICS and b.name in FLOW_METRICS
        and {a.basis, b.basis} == {"ytd", "quarter"}
        and a.period_start == date(a.period_end.year, 1, 1)
        and a.period_end == date(a.period_end.year, 3, 31)
    )


def _unique(metrics: Iterable[Metric]) -> Metric | None:
    items = [metric for metric in metrics if metric.status == "verified" and metric.value.is_finite()]
    if not items:
        return None
    fingerprints = {
        (m.name, m.company, m.currency, m.scope, m.period_start, m.period_end,
         m.basis, m.value, m.restated) for m in items
    }
    if len(fingerprints) != 1:
        return None
    return items[0].model_copy(update={"evidence_ids": _ids(*(m.evidence_ids for m in items))})


def _metric(report: ExtractedReport, name: str, *, match: Metric | None = None,
            scope: str = "consolidated") -> Metric | None:
    items = [m for m in report.metrics if m.company == report.company
             and m.name == name and m.scope == scope and m.period_end == report.period_end]
    if match is not None:
        return _unique(m for m in items if _same_period(m, match) and m.currency == match.currency
                       and m.restated == match.restated)
    # 对一个报告期优先分析年初累计，避免同一季度与累计金额混合。
    for basis in ("ytd", "quarter", "point"):
        candidates = [m for m in items if m.basis == basis]
        if candidates:
            return _unique(candidates)
    return None


def _reviewed(report: ExtractedReport, topic: str) -> list[str]:
    return _ids(*(c.evidence_ids for c in report.coverage
                  if c.topic == topic and c.status == "reviewed" and c.evidence_ids))


def _signal(report: ExtractedReport, category: str, status: str, title: str,
            message: str, *, severity: str = "unknown", evidence: Iterable[str] = (),
            details: dict | None = None) -> Signal:
    return Signal(
        id=f"{report.document_id}:{report.period_end}:{category}",
        company=report.company, period_end=report.period_end, category=category,
        status=status, severity=severity, title=title, message=message,
        evidence_ids=list(evidence), details=details or {},
    )


def detect_nonrecurring(report: ExtractedReport, *, config: RuleConfig = DEFAULT_RULES) -> Signal:
    coverage = _reviewed(report, "nonrecurring")
    parent = _metric(report, "parent_net_profit")
    adjusted = _metric(report, "adjusted_parent_net_profit", match=parent) if parent else None
    if not coverage or parent is None or adjusted is None:
        return _signal(report, "nonrecurring", "insufficient_evidence", "非经常性损益证据待补充",
                       "需要已核验的同期归母净利润、扣非归母净利润，以及已定位的非经常性损益披露；存在数值冲突时也暂不判断。",
                       evidence=_ids(coverage, *(m.evidence_ids for m in report.metrics
                           if m.company == report.company and m.period_end == report.period_end
                           and m.name in ("parent_net_profit", "adjusted_parent_net_profit", "nonrecurring_total_parent"))))
    amount = parent.value - adjusted.value
    total = _metric(report, "nonrecurring_total_parent", match=parent)
    evidence = _ids(coverage, parent.evidence_ids, adjusted.evidence_ids,
                    total.evidence_ids if total else [])
    details = {"parent_net_profit": str(parent.value), "adjusted_parent_net_profit": str(adjusted.value),
               "derived_nonrecurring": str(amount), "threshold_ratio": str(config.nonrecurring_ratio),
               "rule_type": "heuristic", "formula": "归母净利润－扣非归母净利润"}
    if total is None:
        return _signal(report, "nonrecurring", "insufficient_evidence", "非经常性损益总额尚待核对",
                       "已计算归母与扣非归母的差额，但尚缺同口径的税后归母非经常性损益披露总额，暂不判断影响比例。",
                       evidence=evidence, details=details)
    tolerance = max(config.reconciliation_absolute_tolerance,
                    max(abs(total.value), abs(amount)) * config.reconciliation_relative_tolerance)
    details.update(disclosed_total_parent=str(total.value), reconciliation_tolerance=str(tolerance))
    if abs(amount - total.value) > tolerance:
        details["reconciliation_difference"] = str(amount - total.value)
        return _signal(report, "nonrecurring", "insufficient_evidence", "非经常性损益金额需要复核",
                       "归母与扣非归母差额和披露的税后归母总额不一致。需检查期间、单位及税项/少数股东扣减；系统未直接加总明细。",
                       evidence=evidence, details=details)
    ratio = abs(amount) / abs(parent.value) if abs(parent.value) > config.near_zero_base else None
    details["absolute_profit_ratio"] = str(ratio) if ratio is not None else None
    turns_positive = parent.value > 0 and adjusted.value <= 0
    detected = turns_positive or (ratio is not None and ratio >= config.nonrecurring_ratio)
    if ratio is None and amount != 0:
        return _signal(report, "nonrecurring", "insufficient_evidence", "归母利润基数过小",
                       "非经常性损益总额已核对，但归母利润接近零，影响比例缺少稳定的解释；请人工检查绝对金额和业务背景。",
                       evidence=evidence, details=details)
    if detected:
        message = "扣非归母净利润非正，非经常性损益使归母净利润转正。" if turns_positive else (
            f"非经常性损益绝对额达到归母净利润绝对值的 {ratio * 100:.2f}%，达到演示关注阈值 {config.nonrecurring_ratio * 100}%。"
        )
        return _signal(report, "nonrecurring", "detected", "非经常性损益对利润影响较大",
                       message + " 该结果是关注线索，需要结合披露性质与持续性解释。",
                       severity="watch", evidence=evidence, details=details)
    return _signal(report, "nonrecurring", "not_detected", "已检查非经常性损益影响",
                   "披露总额已核对，在当前演示阈值下未发现较大影响；结论只覆盖本次已核验期间。",
                   severity="info", evidence=evidence, details=details)


_NEGATIVE_CHANGE_PHRASES = (
    "无变更", "无变化", "未变更", "不存在变更", "无追溯调整", "未进行追溯", "无差错更正",
)

# 年初未分配利润附注常逐项列示这些原因，即使每项影响都为零。
# 只排除固定因由行；“本次变更”“本期执行新准则”等实际事件不在模板内。
_ZERO_OPENING_PROFIT_TEMPLATE = re.compile(
    r"(?:^|(?<=[。；;：:]))(?:[（(]\d+[）)]|\d+[、.．])?"
    r"由于(?:《企业会计准则》(?:及其相关新规定)?(?:进行)?追溯调整"
    r"|会计政策变更|重大会计差错更正|同一控制(?:导致)?的?合并范围变更)"
    r"[，,]?影响(?:期初|年初)未分配利润(?:金额)?(?:均)?(?:为|是)?"
    r"(?:人民币)?(?:[+-]?0+(?:\.0+)?|零)(?:亿元|百万元|万元|千元|元)[。；;]?"
)


def _positive_change_after_negation(text: str) -> bool:
    """否定一个事件的句子可能同时披露另一个事件，不能整段当作未变化。"""
    if re.search(r"[√☑■]适用", text) and not re.search(r"[√☑■]不适用", text):
        return True
    # “进行追溯调整，影响期初未分配利润0元”单独来自模板时，
    # 不能确认发生实际变化；也不能据此确认所有会计口径均未变。
    remaining = _ZERO_OPENING_PROFIT_TEMPLATE.sub("", text)
    for phrase in _NEGATIVE_CHANGE_PHRASES:
        remaining = remaining.replace(phrase, "")
    remaining = re.sub(
        r"(?:未|没有)发生(?:相关|重要|重大)?(?:会计政策|会计估计|合并范围)?(?:变更|变化|追溯调整|差错更正)"
        r"|无(?:重要|重大)?(?:会计政策|会计估计|合并范围)(?:的)?(?:变更|变化)",
        "", remaining,
    )
    return bool(re.search(
        r"(?:发生|进行了|进行|实施|本次|已经|已|开始|新增|新纳入).{0,25}"
        r"(?:会计政策变更|会计估计变更|合并范围变化|合并范围变更|变更|追溯调整|重述|差错更正)"
        r"|(?:会计政策|会计估计|合并范围).{0,12}(?:变更为|变更了|发生变化|发生变更|新增|增加|减少|不再纳入)"
        r"|(?:会计政策变更|会计估计变更|追溯调整|差错更正)(?:增加|减少|导致|影响)"
        r"|(?:自|于|从).{0,24}(?:开始|首次|执行|施行|实施|适用).{0,24}(?:新准则|会计准则|解释第|新政策)"
        r"|(?:已经采用|已采用|公司采用|公司实施|进行了|已进行).{0,12}追溯调整"
        r"|(?:已|并)重述.{0,20}(?:比较期间|上年同期|以前年度)"
        r"|(?:公司|本公司).{0,40}(?:计入|列入).{0,30}(?:不再计入|不再列入)",
        remaining,
    ))


def _explicit_no_change(text: str) -> bool:
    compact = "".join(text.split())
    if _positive_change_after_negation(compact):
        return False
    if re.search(r"[√☑■]不适用", compact) and not re.search(r"[√☑■]适用", compact):
        return True
    # “变更没有发生重大影响”仍然发生了变更，不能按否定事件解释。
    if any(phrase in compact for phrase in _NEGATIVE_CHANGE_PHRASES):
        return True
    return bool(re.search(
        r"(?:未|没有)发生(?:相关|重要|重大)?(?:会计政策|会计估计|合并范围)?(?:变更|变化|追溯调整|差错更正)"
        r"|无(?:重要|重大)?(?:会计政策|会计估计|合并范围)(?:的)?(?:变更|变化)(?:[。；，、]|$)",
        compact,
    )) or compact.rstrip("。；;.") == "不适用"


def detect_accounting_change(report: ExtractedReport) -> Signal:
    coverage = _reviewed(report, "accounting_change")
    disclosures = [d for d in report.disclosures if d.kind in (
        "accounting_policy", "accounting_estimate", "consolidation", "restatement",
    )]
    evidence = _ids(coverage, *(d.evidence_ids for d in disclosures))
    per_kind = {}
    for kind in ("accounting_policy", "accounting_estimate", "consolidation", "restatement"):
        items = [d for d in disclosures if d.kind == kind]
        changed = [d for d in items if not _explicit_no_change(d.quote)
                   and _positive_change_after_negation("".join(d.quote.split()))]
        no_change = [d for d in items if _explicit_no_change(d.quote)]
        per_kind[kind] = {"status": "detected" if changed else "not_detected" if no_change else "insufficient_evidence",
                          "quotes": [d.quote for d in changed or no_change],
                          "evidence_ids": _ids(*(d.evidence_ids for d in changed or no_change))}
    if not coverage:
        return _signal(report, "accounting_change", "insufficient_evidence", "会计口径披露待核查",
                       "尚未定位会计政策、会计估计、合并范围和追溯调整的检查范围，不能据此认定未发生变化。",
                       evidence=evidence, details={"categories": per_kind})
    positive = [d for d in disclosures if not _explicit_no_change(d.quote)
                and _positive_change_after_negation("".join(d.quote.split()))]
    if positive:
        return _signal(report, "accounting_change", "detected", "会计或合并口径存在变化",
                       "已定位变化披露；变化本身需要说明，是否对财务比较产生重大影响仍需结合原文。",
                       severity="watch", evidence=evidence,
                       details={"changes": [{"kind": d.kind, "summary": d.summary, "quote": d.quote}
                                            for d in positive], "rule_type": "disclosure", "categories": per_kind})
    kinds = {d.kind for d in disclosures if _explicit_no_change(d.quote)}
    required = {"accounting_policy", "accounting_estimate", "consolidation", "restatement"}
    if not required.issubset(kinds):
        return _signal(report, "accounting_change", "insufficient_evidence", "会计口径检查尚未完整",
                       "已定位部分披露，但尚缺四项中明确的未变化说明；标题或提及关键词不能证明发生变化，缺少说明也不能等同于未发生变化。",
                       evidence=evidence, details={"reviewed_kinds": sorted(kinds), "missing_kinds": sorted(required - kinds), "categories": per_kind})
    return _signal(report, "accounting_change", "not_detected", "已检查会计与合并口径",
                   "已定位会计政策、估计、合并范围和追溯调整的未变化披露，在这些范围内未发现变化。",
                   severity="info", evidence=evidence, details={"reviewed_kinds": sorted(kinds), "categories": per_kind})


def detect_cashflow_divergence(report: ExtractedReport, *, prior_reports: Iterable[ExtractedReport] = (),
                               config: RuleConfig = DEFAULT_RULES) -> Signal:
    profit = _metric(report, "net_profit")
    cash = _metric(report, "operating_cashflow", match=profit) if profit else None
    if profit is None or cash is None:
        return _signal(report, "cashflow_divergence", "insufficient_evidence", "利润与现金流口径待补齐",
                       "需要已核验、同期间的合并净利润和合并经营现金流；归母净利润不能替代合并净利润。",
                       evidence=_ids(*(m.evidence_ids for m in report.metrics
                           if m.company == report.company and m.period_end == report.period_end
                           and m.name in ("net_profit", "parent_net_profit", "operating_cashflow"))))
    reasons = [d for d in report.disclosures if d.kind == "cashflow_reason"]
    evidence = _ids(profit.evidence_ids, cash.evidence_ids, *(d.evidence_ids for d in reasons))
    details = {"net_profit": str(profit.value), "operating_cashflow": str(cash.value),
               "threshold_ratio": str(config.cashflow_profit_ratio), "rule_type": "heuristic",
               "formula": "合并经营现金流净额÷合并净利润",
               "reasons": [d.summary for d in reasons], "reason_reviewed": bool(_reviewed(report, "cashflow_reason"))}
    if profit.value <= config.near_zero_base:
        return _signal(report, "cashflow_divergence", "insufficient_evidence", "利润现金流比例暂不适用",
                       "合并净利润为负、为零或接近零，本规则的正利润现金转化比例无法可靠解释；已保留原始金额。",
                       evidence=evidence, details=details)
    ratio = cash.value / profit.value
    details["cashflow_profit_ratio"] = str(ratio)
    detected = ratio < config.cashflow_profit_ratio
    trend_divergence = False
    previous_reports = list(prior_reports)
    if previous_reports:
        profit_base, profit_source = _find_yoy_base(profit, report, previous_reports)
        cash_base, cash_source = _find_yoy_base(cash, report, previous_reports)
        profit_change = compare_metrics(profit, profit_base, "yoy",
            restatement_aligned=profit_base is not None and profit_source is not None
            and _restatement_aligned(report, profit, profit_base, profit_source), config=config)
        cash_change = compare_metrics(cash, cash_base, "yoy",
            restatement_aligned=cash_base is not None and cash_source is not None
            and _restatement_aligned(report, cash, cash_base, cash_source), config=config)
        comparable_states = {"ok", "zero_base", "negative_base", "near_zero_base",
                             "profit_to_loss", "loss_to_profit", "cashflow_to_outflow", "cashflow_to_inflow"}
        trend_divergence = (profit_change.status in comparable_states and cash_change.status in comparable_states
                            and profit_change.delta is not None and cash_change.delta is not None
                            and profit_change.delta > 0 and cash_change.delta < 0)
        details.update(profit_yoy_status=profit_change.status, cashflow_yoy_status=cash_change.status,
                       profit_yoy_delta=str(profit_change.delta) if profit_change.delta is not None else None,
                       cashflow_yoy_delta=str(cash_change.delta) if cash_change.delta is not None else None,
                       trend_divergence=trend_divergence)
        if trend_divergence:
            detected = True
            evidence = _ids(evidence, profit_change.evidence_ids, cash_change.evidence_ids)
    message = (f"经营现金流与合并净利润之比为 {ratio:.4f}，低于演示关注阈值 {config.cashflow_profit_ratio}。"
               if ratio < config.cashflow_profit_ratio else f"经营现金流与合并净利润之比为 {ratio:.4f}，未低于演示关注阈值 {config.cashflow_profit_ratio}。")
    if trend_divergence:
        message += " 同口径同比显示合并净利润增加而经营现金流下降。"
    if detected:
        message += " 需进一步核对营运资金、季节性与非现金项目。" if not reasons else " 已附相关原因披露，需核对其对差异的解释。"
    return _signal(report, "cashflow_divergence", "detected" if detected else "not_detected",
                   "利润与经营现金流存在背离线索" if detected else "已检查利润与经营现金流",
                   message, severity=("high" if cash.value < 0 else "watch") if detected else "info",
                   evidence=evidence, details=details)


def _restatement_aligned(report: ExtractedReport, current: Metric, base: Metric,
                         base_report: ExtractedReport) -> bool:
    if current.restated == base.restated:
        return True
    # 重述标记必须落在基期金额上。仅有本期重述不能证明未调整的基期已经对齐。
    if not base.restated:
        return False
    if (report.company, report.period_end) != (base_report.company, base_report.period_end):
        return False
    # 合并同公司同期的多个文件后，说明仍须来自同时载有本期及基期金额的原文件。
    if not any(_metric_fingerprint(m) == _metric_fingerprint(current) for m in base_report.metrics):
        return False
    if not any(_metric_fingerprint(m) == _metric_fingerprint(base) for m in base_report.metrics):
        return False
    for disclosure in base_report.disclosures:
        if disclosure.kind != "restatement":
            continue
        for clause in re.split(r"[。；;]", disclosure.quote):
            if (_explicit_no_change(clause)
                    or re.search(r"(?:未|不|无需|无须|尚未|没有).{0,15}(?:重述|追溯调整|调整)", clause)):
                continue
            if re.search(r"(?:上年同期|比较期间|基期|上年期末|以前年度|上年度).{0,40}"
                         r"(?:重述|追溯调整|调整后的可比口径|可比口径.{0,10}调整)"
                         r"|(?:重述|追溯调整).{0,20}(?:上年同期|比较期间|基期|上年期末|以前年度|上年度)", clause):
                return True
    return False


def _metric_key(metric: Metric) -> tuple:
    return (metric.name, metric.company, metric.scope, metric.currency,
            metric.period_start, metric.period_end, metric.basis)


def _metric_fingerprint(metric: Metric) -> tuple:
    return (*_metric_key(metric), metric.value, metric.restated)


def _same_report_period(a: ExtractedReport, b: ExtractedReport) -> bool:
    return (a.company, a.period_end) == (b.company, b.period_end)


def _aligned_bases(current: Metric, report: ExtractedReport,
                   candidates: list[tuple[Metric, ExtractedReport]]) -> list[tuple[Metric, ExtractedReport]]:
    """原报告明确重述比较期间时使用调整后列，调整前记录保留供核对。"""
    adjusted = [(m, r) for m, r in candidates if m.status == "verified" and m.restated
                and _restatement_aligned(report, current, m, r)]
    return adjusted or candidates


def _find_yoy_base(current: Metric, report: ExtractedReport,
                   reports: Iterable[ExtractedReport]) -> tuple[Metric | None, ExtractedReport | None]:
    bases = [(m, r) for r in reports for m in r.metrics
             if m.name == current.name and m.company == current.company
             and m.scope == current.scope and m.currency == current.currency and m.basis == current.basis
             and m.period_end.year == current.period_end.year - 1
             and _same_calendar_position(m.period_end, current.period_end)
             and _same_calendar_position(m.period_start, current.period_start)
             and m.period_start.year == current.period_start.year - 1]
    preferred = [(m, r) for m, r in bases if _same_report_period(r, report)]
    chosen = _aligned_bases(current, report, preferred or bases)
    base = _unique(m for m, _ in chosen)
    source = next((r for _, r in chosen if base is not None
                   and _restatement_aligned(report, current, base, r)), chosen[0][1] if chosen else None)
    return base, source


def _combine_same_period_reports(reports: list[ExtractedReport]) -> list[ExtractedReport]:
    """把同公司同期数据一起核验，避免上传顺序决定结果。保留原文件供口径证明。"""
    groups: dict[tuple[str, date], list[ExtractedReport]] = {}
    for report in sorted(reports, key=lambda r: (r.company, r.period_end, r.document_id)):
        groups.setdefault((report.company, report.period_end), []).append(report)
    return [items[0].model_copy(update={
        "metrics": [m for r in items for m in r.metrics],
        "disclosures": [d for r in items for d in r.disclosures],
        "coverage": [c for r in items for c in r.coverage],
        "warnings": _ids(*(r.warnings for r in items)),
    }) for items in groups.values()]


def _conflicting_comparison(items: list[Metric], kind: Literal["yoy", "qoq"],
                            *, base_conflict: bool = False) -> Comparison:
    comparison = compare_metrics(items[0], None, kind)
    return comparison.model_copy(update={
        "status": "incomparable",
        "current_value": items[0].value if base_conflict else None,
        "percent": None, "delta": None,
        "explanation": ("基期" if base_conflict else "本期")
            + "相同期间和口径的已核验金额或追溯调整标记存在冲突，需核对原报告或更正公告；未按文件顺序挑选数值。",
        "evidence_ids": _ids(*(m.evidence_ids for m in items)),
    })


def _has_verified_conflict(items: Iterable[Metric]) -> bool:
    verified = [m for m in items if m.status == "verified" and m.value.is_finite()]
    return len({_metric_fingerprint(m) for m in verified}) > 1


def analyze(reports: list[ExtractedReport], *, config: RuleConfig = DEFAULT_RULES) -> AnalysisResult:
    """为每个报告期输出三类信号和可比指标；所有失败都保留可解释状态。"""
    result = AnalysisResult(warnings=_ids(*(r.warnings for r in reports)))
    if not reports:
        result.warnings.append("未提供可分析的报告。")
        return result
    reports = sorted(reports, key=lambda r: (r.company, r.period_end, r.document_id))
    indexed = [(metric, report) for report in reports for metric in report.metrics
               if metric.company == report.company]
    quarters: list[tuple[Metric, ExtractedReport]] = []
    for metric, report in indexed:
        if metric.status != "verified":
            continue
        if metric.name in FLOW_METRICS and metric.basis == "quarter" and _is_quarter(metric):
            quarters.append((metric, report))
        elif metric.name in FLOW_METRICS and metric.basis == "ytd":
            if metric.period_start == date(metric.period_end.year, 1, 1) and metric.period_end == date(metric.period_end.year, 3, 31):
                quarters.append((metric.model_copy(update={"basis": "quarter"}), report))
            else:
                candidates = [(m, r) for m, r in indexed if m.name == metric.name
                              and m.company == metric.company and m.scope == metric.scope
                              and m.currency == metric.currency and m.basis == "ytd"
                              and m.period_start == metric.period_start
                              and m.period_end.month == metric.period_end.month - 3
                              and m.period_end.year == metric.period_end.year]
                preferred = [(m, r) for m, r in candidates if r.document_id == report.document_id]
                chosen = preferred or candidates
                earlier = _unique(m for m, _ in chosen)
                if earlier is not None:
                    earlier_report = chosen[0][1]
                    try:
                        quarters.append((derive_quarter(metric, earlier,
                                         restatement_aligned=_restatement_aligned(report, metric, earlier, earlier_report)), report))
                    except ValueError as exc:
                        result.warnings.append(f"{metric.company} {metric.period_end} {metric.name} 季度转换：{exc}")
    comparison_ids: set[str] = set()
    for report in _combine_same_period_reports(reports):
        result.signals.extend((detect_nonrecurring(report, config=config),
                               detect_accounting_change(report),
                               detect_cashflow_divergence(report, prior_reports=reports, config=config)))
        current_groups: dict[tuple, list[Metric]] = {}
        for metric in report.metrics:
            if metric.company != report.company or metric.period_end != report.period_end:
                continue
            group = (metric.name, metric.scope, metric.currency, metric.period_start, metric.period_end, metric.basis)
            current_groups.setdefault(group, []).append(metric)
        for items in current_groups.values():
            current = _unique(items)
            if current is None:
                result.warnings.append(f"{report.company} {report.period_end} {items[0].name} 存在冲突或未经核验，不参与计算。")
                verified = [m for m in items if m.status == "verified" and m.value.is_finite()]
                if verified:
                    result.comparisons.append(_conflicting_comparison(verified, "yoy"))
                continue
            if current.basis == "quarter":
                quarter_sources = [m for m, r in quarters if _same_report_period(r, report)
                                   and _metric_key(m) == _metric_key(current)]
                if _has_verified_conflict(quarter_sources):
                    comparison = _conflicting_comparison(quarter_sources, "yoy")
                    result.comparisons.append(comparison)
                    result.warnings.append(comparison.explanation)
                    continue
            bases = [(m, r) for m, r in indexed if m.name == current.name and m.company == current.company
                     and m.scope == current.scope and m.currency == current.currency and m.basis == current.basis
                     and m.period_end.year == current.period_end.year - 1
                     and _same_calendar_position(m.period_end, current.period_end)
                     and _same_calendar_position(m.period_start, current.period_start)
                     and m.period_start.year == current.period_start.year - 1]
            preferred = [(m, r) for m, r in bases if _same_report_period(r, report)]
            chosen = _aligned_bases(current, report, preferred or bases)
            base = _unique(m for m, _ in chosen)
            if chosen and base is None:
                result.warnings.append(f"{current.company} {current.period_end} {current.name} 同比基期有冲突或未经核验。")
            comparison = (compare_metrics(current, base, "yoy",
                          restatement_aligned=base is not None and any(_restatement_aligned(report, current, base, r)
                                                                      for _, r in chosen), config=config)
                          if base is not None or not _has_verified_conflict(m for m, _ in chosen) else _conflicting_comparison(
                              [current, *(m for m, _ in chosen)], "yoy", base_conflict=True))
            if comparison.id not in comparison_ids:
                comparison_ids.add(comparison.id)
                result.comparisons.append(comparison)
            if comparison.status == "incomparable" or (base is not None and current.restated != base.restated):
                result.warnings.append(comparison.explanation)
        q_current = [(m, r) for m, r in quarters if _same_report_period(r, report)
                     and m.period_end == report.period_end]
        # 余额可比较相邻季度末；仅季度流量会参与累计差分。
        q_current += [(m, report) for m in report.metrics if m.name in STOCK_METRICS
                      and m.basis == "point" and m.company == report.company
                      and m.period_end == report.period_end and m.status == "verified"]
        quarter_groups: dict[tuple, list[Metric]] = {}
        for metric, _ in q_current:
            quarter_groups.setdefault(_metric_key(metric), []).append(metric)
        for quarter_items in quarter_groups.values():
            current = _unique(quarter_items)
            if current is None:
                comparison = _conflicting_comparison(quarter_items, "qoq")
                result.comparisons.append(comparison)
                result.warnings.append(comparison.explanation)
                continue
            candidates = quarters if current.basis == "quarter" else indexed
            bases = [(m, r) for m, r in candidates if m.name == current.name and m.company == current.company
                     and m.scope == current.scope and m.currency == current.currency and m.basis == current.basis
                     and ((m.period_end + timedelta(days=1) == current.period_start) if current.basis == "quarter"
                          else (current.period_end.year - m.period_end.year) * 12 + current.period_end.month - m.period_end.month == 3)]
            preferred = [(m, r) for m, r in bases if _same_report_period(r, report)]
            chosen = _aligned_bases(current, report, preferred or bases)
            base = _unique(m for m, _ in chosen)
            comparison = (compare_metrics(current, base, "qoq",
                          restatement_aligned=base is not None and any(_restatement_aligned(report, current, base, r)
                                                                      for _, r in chosen), config=config)
                          if base is not None or not _has_verified_conflict(m for m, _ in chosen) else _conflicting_comparison(
                              [current, *(m for m, _ in chosen)], "qoq", base_conflict=True))
            if comparison.id not in comparison_ids:
                comparison_ids.add(comparison.id)
                result.comparisons.append(comparison)
            if comparison.status == "incomparable" or (base is not None and current.restated != base.restated):
                result.warnings.append(comparison.explanation)
    for comparison in result.comparisons:
        if comparison.status != "ok" or comparison.metric not in (PROFIT_METRICS | {"operating_cashflow", "nonrecurring_total_parent"}):
            continue
        revenue = _unique(m for m, _ in indexed + quarters if m.name == "revenue"
                          and m.company == comparison.company and m.scope == comparison.scope
                          and m.basis == comparison.basis and m.period_start == comparison.current_period_start
                          and m.period_end == comparison.period_end)
        if revenue is None or revenue.value <= 0 or comparison.base_value is None:
            continue
        cutoff = min(config.small_base_amount, revenue.value * config.small_base_revenue_ratio)
        if 0 < comparison.base_value <= cutoff:
            comparison.status = "near_zero_base"
            comparison.percent = None
            comparison.explanation = (f"正基期同时不超过{config.small_base_amount}元和同期间营业收入的"
                f"{config.small_base_revenue_ratio * 100}%（本次阈值{cutoff}元）；"
                "按项目小基期关注规则只展示增减金额。该阈值不是会计准则或监管标准。")
    result.warnings = _ids(result.warnings)
    return result
