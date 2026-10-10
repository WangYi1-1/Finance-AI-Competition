"""中文展示入口：上传财报、运行分析、核对页面证据并下载结果。"""
from __future__ import annotations

import hashlib
from html import escape
import json
import math
import re
import time
import uuid
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymupdf
import altair as alt
import pandas as pd
import streamlit as st

from finance_agent.config import Settings
from finance_agent.analysis import comparison_material_hint
from finance_agent.demo import ensure_demo_files
from finance_agent.dashboard import (
    MONEY_SCALES, comparison_chart_rows, filter_result, format_display_money,
    quarter_series, select_amount, signal_detail_rows,
)
from finance_agent.llm import ModelError
from finance_agent.pipeline import run_analysis
from finance_agent.schemas import Comparison, Evidence, RunResult, Signal
from finance_agent.cloud import (CloudConfig, CloudError, analysis_slot, build_run_archive,
                                clean_expired_sessions, is_cloud, session_paths)


ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / "data" / "uploads"
OUTPUTS = ROOT / "output"
YILI_RESULTS = OUTPUTS / "member_yili"
YILI_RESULT_INDEX = ROOT / "samples/member/yili/latest_run.json"
PUBLIC_SHOWCASE = "已有公开样本（6份）"
YILI_SHOWCASE = "组员新增样本：伊利2024/2025年报"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
PUBLIC_SAMPLE_GROUPS = {
    "华虹2024三期财报（检验季度环比）": ["华虹公司_2024第一季度报告.pdf", "华虹公司_2024半年度报告摘要.pdf", "华虹公司_2024第三季度报告.pdf"],
    "TCL科技2024三季报（归母盈利、合并亏损）": ["TCL科技_2024第三季度报告.pdf"],
    "科大讯飞2024三季报（亏损与负现金流）": ["科大讯飞_2024第三季度报告.pdf"],
    "浪潮信息2024三季报（盈利但现金流为负）": ["浪潮信息_2024第三季度报告.pdf"],
}
PUBLIC_SAMPLE_GROUPS["全部6份公开财报（多公司测试）"] = [
    name for group in list(PUBLIC_SAMPLE_GROUPS.values()) for name in group
]
METRIC_NAMES = {
    "revenue": "营业收入",
    "net_profit": "合并净利润",
    "parent_net_profit": "归母净利润",
    "adjusted_parent_net_profit": "扣非归母净利润",
    "operating_cashflow": "经营活动现金流量净额",
    "accounts_receivable": "应收账款",
    "inventory": "存货",
    "total_assets": "资产总额",
    "nonrecurring_total_parent": "归属于母公司的非经常性损益",
}
BASIS_NAMES = {"ytd": "年初至今累计", "quarter": "单季度", "point": "期末时点"}
SCOPE_NAMES = {"consolidated": "合并报表", "parent": "母公司报表"}
CATEGORY_NAMES = {
    "nonrecurring": "非经常性损益",
    "accounting_change": "会计口径变化",
    "cashflow_divergence": "利润与经营现金流背离",
}
SIGNAL_STATUS = {
    "detected": "发现关注信号",
    "not_detected": "在已检查范围内未发现信号",
    "insufficient_evidence": "证据不足，待核实",
}
COMPARISON_STATUS = {
    "ok": "已计算",
    "missing": "缺少对比数据",
    "incomparable": "暂不可比较",
    "zero_base": "基期为零",
    "negative_base": "基期为负",
    "near_zero_base": "基期接近零",
    "profit_to_loss": "不适用（由盈转亏）",
    "loss_to_profit": "不适用（扭亏为盈）",
    "cashflow_to_outflow": "不适用（由净流入转为净流出）",
    "cashflow_to_inflow": "不适用（由净流出转为净流入）",
}

DASHBOARD_KEYS = (
    "dashboard_company", "dashboard_period", "dashboard_basis", "dashboard_unit", "result_view",
    "chart_metric", "chart_quarter", "evidence_document", "evidence_reference", "focused_only",
    "focus_evidence_ids", "comparison_reference",
    "comparison_chart_reference", "profit_chart_metric", "show_raw_metrics",
)

LEAF_MARK = """<svg viewBox="0 0 70 84" fill="none" aria-hidden="true">
<path d="M34 76V26M34 56C16 56 8 43 8 27c18 0 26 11 26 29Z
M34 43c18 0 28-13 28-30-18 0-28 12-28 30Z
M34 26c-9-8-10-18-3-25 10 6 11 16 3 25Z"
stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
<path d="m14 34 20 22m22-35L34 43M21 76h26"
stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>"""


def apply_styles() -> None:
    css = (ROOT / "assets/workbench.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def themed_chart(chart):
    """Use the same restrained forest palette for all financial charts."""
    return (chart.configure(background="transparent", font="Microsoft YaHei")
            .configure_view(stroke=None)
            .configure_axis(labelColor="#65776a", titleColor="#465c4e", gridColor="#e3ebe1",
                            domain=False, tickColor="#ccd9cb", titleFontWeight=500,
                            labelFontSize=11, titleFontSize=12, labelPadding=7)
            .configure_text(color="#244b36", fontSize=12))


def reset_result_controls() -> None:
    for key in DASHBOARD_KEYS:
        st.session_state.pop(key, None)


def assign_result(result: RunResult, source_files: dict[str, str], origin: str,
                  saved_dataset: str | None = None) -> None:
    reset_result_controls()
    st.session_state["analysis_result"] = result
    st.session_state["source_files"] = source_files
    st.session_state["result_origin"] = origin
    st.session_state["result_origin_run_id"] = result.run_id
    st.session_state["result_saved_dataset"] = saved_dataset


def reset_focus() -> None:
    for key in ("focus_evidence_ids", "evidence_document", "evidence_reference", "focused_only"):
        st.session_state.pop(key, None)


def change_company() -> None:
    st.session_state.pop("dashboard_period", None)
    st.session_state.pop("chart_quarter", None)
    st.session_state.pop("comparison_reference", None)
    st.session_state.pop("comparison_chart_reference", None)
    reset_focus()
    st.session_state["result_view"] = "分析总览"


def change_period() -> None:
    st.session_state.pop("chart_quarter", None)
    st.session_state.pop("comparison_reference", None)
    st.session_state.pop("comparison_chart_reference", None)
    reset_focus()
    st.session_state["result_view"] = "分析总览"


def ensure_choice(key: str, options: list, default=None) -> None:
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0] if default is None else default


def focus_evidence(ids: list[str] | tuple[str, ...], result: RunResult) -> None:
    available = list(dict.fromkeys(item for item in ids if item in result.evidence))
    if not available:
        return
    # 金额核对优先定位表格；框选的是引用区域，不是精确金额单元格。
    first = next((result.evidence[item] for item in available if result.evidence[item].kind == "table"),
                 result.evidence[available[0]])
    st.session_state["focus_evidence_ids"] = available
    st.session_state["evidence_document"] = first.document_id
    st.session_state["evidence_reference"] = first.id
    st.session_state["focused_only"] = True
    st.session_state["result_view"] = "原文核对"


def focus_comparison(comparison: Comparison, result: RunResult) -> None:
    # 优先定位明确基期记录所在表，方便一次核对比较列；派生季度保留原证据顺序。
    base_ids = [identifier for report in result.reports for metric in report.metrics
                if metric.status == "verified" and metric.company == comparison.company
                and metric.name == comparison.metric and metric.scope == comparison.scope
                and metric.basis == comparison.basis
                and metric.period_start == comparison.base_period_start
                and metric.period_end == comparison.base_period_end
                for identifier in metric.evidence_ids if identifier in comparison.evidence_ids]
    focus_evidence(list(dict.fromkeys(base_ids + comparison.evidence_ids)), result)


def money_in_unit(value: Decimal | None, unit: str) -> str:
    return format_display_money(value, unit, include_unit=False, exact_small=False)


def format_money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def format_percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


def source_label(evidence: Evidence) -> str:
    label = f"{evidence.document_name} · PDF 第 {evidence.pdf_page} 页"
    if evidence.printed_page:
        label += f"（印刷页码 {evidence.printed_page}）"
    return label


def evidence_sources(ids: list[str], evidence: dict[str, Evidence]) -> str:
    labels = [source_label(evidence[item]) for item in ids if item in evidence]
    return "；".join(dict.fromkeys(labels)) or "待核实"


def validate_uploads(uploaded_files: list[Any]) -> None:
    if not 1 <= len(uploaded_files) <= 6:
        raise ValueError("请一次上传1至6份财报，文件较多请分批分析。")
    total = 0
    for uploaded in uploaded_files:
        content = uploaded.getvalue()
        if uploaded.size > MAX_UPLOAD_BYTES or len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("有文件超过 30 MB，请压缩或拆分后重新上传。")
        if b"%PDF-" not in content[:1024]:
            raise ValueError("有文件不符合 PDF 格式，请重新选择财报 PDF。")
        total += len(content)
    if is_cloud() and total > 80 * 1024 * 1024:
        raise ValueError("云端每批文件总大小不能超过80 MB，请分批上传。")


def store_uploads(uploaded_files: list[Any], upload_root: Path | None = None) -> list[Path]:
    """仅保留合法 PDF；保存路径由内容摘要和安全文件名确定。"""
    validate_uploads(uploaded_files)
    upload_root = upload_root or UPLOADS
    upload_root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for uploaded in uploaded_files:
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise ValueError("有文件超过 30 MB，请压缩或拆分后重新上传。")
        content = uploaded.getvalue()
        if b"%PDF-" not in content[:1024]:
            raise ValueError("有文件不符合 PDF 格式，请重新选择财报 PDF。")
        digest = hashlib.sha256(content).hexdigest()
        safe_stem = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", Path(uploaded.name).stem)[:90]
        safe_stem = safe_stem.strip(". ") or "financial_report"
        path = upload_root / f"{digest[:16]}_{safe_stem}.pdf"
        if not path.exists():
            path.write_bytes(content)
        if path not in paths:
            paths.append(path)
    return paths


def map_source_files(paths: list[Path]) -> dict[str, str]:
    return {hashlib.sha256(path.read_bytes()).hexdigest()[:16]: str(path) for path in paths}


def load_verified_public_result() -> tuple[RunResult, dict[str, str]]:
    return load_saved_result(ROOT / "samples/public/latest_verified_run.json", OUTPUTS)


def load_saved_result(index_path: Path, allowed_root: Path) -> tuple[RunResult, dict[str, str]]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if (not isinstance(index, dict) or not isinstance(index.get("result_path"), str)
            or not isinstance(index.get("run_id"), str)):
        raise ValueError("样本索引格式无效。")
    result_path = (ROOT / index["result_path"].replace("\\", "/")).resolve()
    if not result_path.is_relative_to(allowed_root.resolve()):
        raise ValueError("样本结果目录无效。")
    result = RunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    if (result_path.parent / "artifacts.json").is_file():
        from finance_agent.provenance import verify_artifacts
        if verify_artifacts(result_path.parent):
            raise ValueError("保存的成果文件完整性校验失败。")
    if result.mode != "live" or result.run_id != index["run_id"]:
        raise ValueError("样本结果与核对记录不一致。")
    result.run_dir = str(result_path.parent)
    files = {}
    for report in result.reports:
        if not re.fullmatch(r"[0-9a-f]{16}", report.document_id):
            raise ValueError("样本文件编号无效。")
        path = (result_path.parent / "sources" / f"{report.document_id}.pdf").resolve()
        if not path.is_relative_to(result_path.parent):
            raise ValueError("样本来源文件目录无效。")
        if hashlib.sha256(path.read_bytes()).hexdigest()[:16] != report.document_id:
            raise ValueError("样本来源文件与核对记录不一致。")
        files[report.document_id] = str(path)
    return result, files


def load_yili_result() -> tuple[RunResult, dict[str, str]]:
    result, files = load_saved_result(YILI_RESULT_INDEX, YILI_RESULTS)
    expected_periods = {date(2024, 12, 31), date(2025, 12, 31)}
    companies = {re.sub(r"\s+", "", report.company) for report in result.reports}
    if (len(result.reports) != 2
            or {report.period_end for report in result.reports} != expected_periods
            or any(report.report_type != "annual" for report in result.reports)
            or companies != {"内蒙古伊利实业集团股份有限公司"}
            or any(re.sub(r"\s+", "", metric.company) not in companies
                   for report in result.reports for metric in report.metrics)
            or len(files) != 2):
        raise ValueError("伊利样本应为同一家公司的2024和2025年度报告。")
    return result, files


def clear_result() -> None:
    reset_result_controls()
    for key in ("analysis_result", "source_files", "result_origin", "result_origin_run_id",
                "result_saved_dataset"):
        st.session_state.pop(key, None)


@st.cache_data(show_spinner=False, max_entries=20)
def render_pdf_page(path_string: str, pdf_page: int, file_digest: str,
                    highlight_bbox: tuple[float, ...] = ()) -> bytes:
    """pdf_page 是给用户展示的一基页码；摘要用于缓存失效。"""
    del file_digest
    with pymupdf.open(path_string) as document:
        if not 1 <= pdf_page <= len(document):
            raise ValueError("页码超出文档范围。")
        page = document[pdf_page - 1]
        if len(highlight_bbox) == 4 and all(math.isfinite(x) for x in highlight_bbox):
            rectangle = pymupdf.Rect(highlight_bbox)
            if not rectangle.is_empty and not rectangle.is_infinite:
                # 只在内存中标出解析时记录的引用区域，不保存或改写来源PDF。
                page.draw_rect(rectangle, color=(0.9, 0.47, 0.12), fill=(1, 0.91, 0.60),
                               width=1.8, fill_opacity=0.22, overlay=True)
        return page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False).tobytes("png")


def show_evidence(ids: list[str], result: RunResult) -> None:
    found = False
    for evidence_id in dict.fromkeys(ids):
        item = result.evidence.get(evidence_id)
        if item is None:
            continue
        found = True
        st.caption(source_label(item))
        st.text(item.text)
    if not found:
        st.caption("未找到可展示的引用片段。")


def metric_label(name: str, scope: str | None = None) -> str:
    return '母公司净利润' if name == 'net_profit' and scope == 'parent' else METRIC_NAMES[name]


def show_metrics(result: RunResult, unit: str = "亿元") -> None:
    show_raw = st.checkbox("显示原始金额与单位（核对用）", key="show_raw_metrics")
    rows = []
    for report in result.reports:
        for metric in report.metrics:
            row = {
                "公司": metric.company,
                "来源文件": report.document_name,
                "指标": metric_label(metric.name, metric.scope),
                "期间": f"{metric.period_start} 至 {metric.period_end}",
                "期间类型": BASIS_NAMES[metric.basis],
                "报表口径": SCOPE_NAMES[metric.scope],
                f"金额（{unit}）": format_display_money(metric.value, unit, include_unit=False),
                "追溯调整": "是" if metric.restated else "否",
                "校验状态": "通过程序校验" if metric.status == "verified" else "待核实",
                "来源": evidence_sources(metric.evidence_ids, result.evidence),
            }
            if show_raw:
                row.update({"原始数值": metric.raw_value, "原单位": metric.unit,
                            "精确金额（元）": f"{metric.value:,f}"})
            rows.append(row)
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
        st.caption(f"金额以人民币{unit}显示，保留两位小数；计算仍使用精确元金额。通过程序校验仍需结合原文复核。")
    else:
        st.info("未提取到可展示的财务指标，请查看待核实事项。")
    for report in result.reports:
        items = [metric for metric in report.metrics if metric.checks]
        if items:
            with st.expander(f"{report.document_name} 的指标校验说明"):
                for metric in items:
                    st.write(f"**{METRIC_NAMES[metric.name]}**：{'；'.join(metric.checks)}")


def show_comparisons(result: RunResult, unit: str = "亿元", *, full_reports=None) -> None:
    reports = result.reports if full_reports is None else full_reports
    rows = [{
        "公司": comparison.company,
        "指标": metric_label(comparison.metric, comparison.scope),
        "比较方式": "同比" if comparison.kind == "yoy" else "环比",
        "本期期间": (f"{comparison.current_period_start} 至 {comparison.period_end}"
                     if comparison.current_period_start else str(comparison.period_end)),
        "期间类型": BASIS_NAMES.get(comparison.basis, "未注明"),
        "报表口径": SCOPE_NAMES.get(comparison.scope, "未注明"),
        "基期期间": (f"{comparison.base_period_start} 至 {comparison.base_period_end}"
                     if comparison.base_period_start and comparison.base_period_end else "未取得可比金额"),
        f"本期金额（{unit}）": format_display_money(comparison.current_value, unit, include_unit=False),
        f"基期金额（{unit}）": format_display_money(comparison.base_value, unit, include_unit=False),
        f"增减金额（{unit}）": format_display_money(comparison.delta, unit, include_unit=False),
        "变化率": format_percent(comparison.percent),
        "状态": COMPARISON_STATUS[comparison.status],
        "说明": comparison.explanation,
        "补充材料建议": comparison_material_hint(comparison, reports),
        "来源": evidence_sources(comparison.evidence_ids, result.evidence),
    } for comparison in result.analysis.comparisons]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("本次没有可展示的期间比较。可上传同一公司的多期财报。")
    st.caption(f"金额单位：人民币{unit}。盈亏或现金流方向切换只展示增减金额；负、零或近零基期不提供常规增长率。")
    if result.analysis.comparisons:
        options = list(range(len(result.analysis.comparisons)))
        ensure_choice("comparison_reference", options)
        selected = st.selectbox("选择一项比较核对依据", options, key="comparison_reference",
            format_func=lambda i: (
                f"{METRIC_NAMES[result.analysis.comparisons[i].metric]} · "
                f"{BASIS_NAMES.get(result.analysis.comparisons[i].basis, '未注明')} · "
                f"{'同比' if result.analysis.comparisons[i].kind == 'yoy' else '环比'} · "
                f"{COMPARISON_STATUS[result.analysis.comparisons[i].status]}"))
        comparison = result.analysis.comparisons[selected]
        render_comparison_chart(comparison, unit)
        st.write(comparison.explanation)
        for caution in comparison.cautions:
            st.warning(caution)
        hint = comparison_material_hint(comparison, reports)
        if hint:
            st.info(hint)
        st.button("定位比较原文", key="comparison_evidence", disabled=not comparison.evidence_ids,
                  on_click=focus_comparison, args=(comparison, result))


def show_signal_card(signal: Signal, result: RunResult, *, compact: bool = False, unit: str = "亿元") -> None:
    with st.container(border=True):
        st.markdown(f"**{escape(signal.title)}**")
        st.caption(f"{CATEGORY_NAMES[signal.category]} · {SIGNAL_STATUS[signal.status]}")
        if signal.status == "detected":
            st.warning(signal.message)
        elif signal.status == "insufficient_evidence":
            st.info(signal.message)
        else:
            st.write(signal.message)
        if not compact:
            rows = signal_detail_rows(signal, unit)
            if rows:
                with st.expander("计算与判断依据", expanded=signal.status == "detected"):
                    st.dataframe(rows, hide_index=True, width="stretch")
                    st.caption(f"金额以{unit}显示；金额核对误差保留元以显示精度。关注阈值属于项目规则，需要结合披露复核。")
            with st.expander("引用内容"):
                show_evidence(signal.evidence_ids, result)
        st.button("查看原文", key=f"{'overview' if compact else 'detail'}_{signal.id}",
                  disabled=not signal.evidence_ids, on_click=focus_evidence,
                  args=(signal.evidence_ids, result))


def show_signals(result: RunResult, unit: str = "亿元") -> None:
    st.caption("信号沿用报告的规则判断，主要按累计口径评估；切换单季金额不会重新评估这些信号。")
    for signal in result.analysis.signals:
        show_signal_card(signal, result, unit=unit)
    if not result.analysis.signals:
        st.info("当前公司与报告期间没有信号评估结果。")


def show_source_pages(result: RunResult, source_files: dict[str, str]) -> None:
    if not result.evidence:
        st.info("本次没有保留下来的证据片段。")
        return
    focused = [item for item in st.session_state.get("focus_evidence_ids", []) if item in result.evidence]
    if focused:
        st.caption(f"已从所选金额或结论定位到 {len(focused)} 个引用片段，可以在来源文件之间切换核对。")
        st.button("查看本公司全部引用", on_click=reset_focus)
        focused_only = st.checkbox("仅显示本次定位的引用", value=True, key="focused_only")
    else:
        focused_only = False
    pool = [result.evidence[item] for item in focused] if focused_only else list(result.evidence.values())
    document_ids = list(dict.fromkeys(item.document_id for item in pool))
    document_names = {item.document_id: item.document_name for item in result.evidence.values()}
    ensure_choice("evidence_document", document_ids)
    document_id = st.selectbox(
        "选择来源文件", document_ids, format_func=lambda item: document_names[item], key="evidence_document",
    )
    references = [item for item in pool if item.document_id == document_id]
    references.sort(key=lambda item: (item.pdf_page, item.id))
    evidence_ids = [item.id for item in references]
    ensure_choice("evidence_reference", evidence_ids)
    selected_id = st.selectbox(
        "选择引用片段", evidence_ids,
        format_func=lambda item: f"PDF 第 {result.evidence[item].pdf_page} 页 · {result.evidence[item].text[:75].replace(chr(10), ' ')}",
        key="evidence_reference",
    )
    selected = result.evidence[selected_id]
    left, right = st.columns([1, 1.65], gap="large")
    with left:
        st.subheader("引用内容")
        st.caption(source_label(selected))
        st.text(selected.text)
    path_string = source_files.get(selected.document_id)
    if path_string is None or not Path(path_string).is_file():
        right.info("来源文件当前不可用，可以依据文件名和 PDF 页码在原财报中核对。")
        return
    path = Path(path_string)
    with right:
        if st.toggle("显示原 PDF 页面", value=True, key="preview_pdf_page"):
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                page_image = render_pdf_page(str(path), selected.pdf_page, digest, tuple(selected.bbox))
                st.image(page_image, caption=f"{selected.document_name} · PDF 第 {selected.pdf_page} 页", width="stretch")
                if selected.bbox:
                    st.caption("橙色框标出引用区域，可能覆盖整段文字或整张表，并非精确金额单元格。")
            except (OSError, ValueError, RuntimeError):
                st.warning("这一页暂时无法生成预览，请下载来源 PDF 后按页码核对。")
        st.download_button("下载这一份来源 PDF", path.read_bytes(), file_name=path.name, mime="application/pdf")


def show_downloads(result: RunResult) -> None:
    run_dir = Path(result.run_dir).resolve()
    allowed_roots = [OUTPUTS.resolve()]
    if is_cloud() and st.session_state.get("cloud_session_id"):
        _, session_output = session_paths(CloudConfig.load(), st.session_state["cloud_session_id"])
        allowed_roots.append(session_output.resolve())
    if not any(run_dir.is_relative_to(root) for root in allowed_roots):
        st.info("本次结果目录不可用。")
        return
    files = [
        ("分析报告", "report.md", "text/markdown"),
        ("结构化结果", "result.json", "application/json"),
        ("指标表格", "metrics.csv", "text/csv"),
        ("运行记录", "trace.jsonl", "application/x-ndjson"),
        ("Excel成果", "analysis.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("文件校验清单", "artifacts.json", "application/json"),
    ]
    columns = st.columns(3)
    for index, (label, name, mime) in enumerate(files):
        column = columns[index % len(columns)]
        path = run_dir / name
        if path.is_file():
            column.download_button(label, path.read_bytes(), file_name=f"{result.run_id}_{name}", mime=mime)
    if is_cloud():
        if (run_dir / 'artifacts.json').is_file():
            try:
                st.download_button("完整成果与原文备份", build_run_archive(run_dir),
                    file_name=f"{result.run_id}_完整成果.zip", mime="application/zip")
            except (CloudError, OSError, ValueError):
                st.warning("完整备份暂不可用，请先下载单项成果，并联系管理员检查文件完整性。")
        st.caption("云端新分析结果暂存于当前会话；请下载完整备份。闲置24小时后可清理，平台重启或更新也可能丢失临时文件。")
    else:
        st.caption(f"本次结果已保存在本地 output/{run_dir.name}。")
    st.caption("这里导出完整运行结果，包含本次所有公司和报告；页面筛选只影响显示。")


def show_explanations(result: RunResult, unit: str) -> None:
    st.caption("只展示可定位的公司原文解释和原表明细。原因披露不代表独立因果验证，具体适用期间以引文为准。")
    for report in result.reports:
        st.subheader(f"{report.company} · {report.period_end}")
        with st.expander("公司披露的变化原因", expanded=True):
            if not report.performance_reasons:
                st.info("未定位明确的收入或利润变化原因；需补充经营讨论或变动原因附注。")
            for index, reason in enumerate(report.performance_reasons):
                st.write(reason["quote"])
                st.button("核对这段原文", key=f"reason_{report.document_id}_{index}",
                          on_click=focus_evidence, args=(reason["evidence_ids"], result))
        for category, title in (("nonrecurring", "非经常性损益拆解"), ("cashflow_bridge", "利润到经营现金流调节")):
            with st.expander(title, expanded=True):
                check = report.detail_checks.get(category, {})
                labels = {"reconciled": "合计已核对", "missing": "待补充", "incomplete": "明细未完整", "inconsistent": "存在差异"}
                st.write(f"**{labels.get(check.get('status'), '尚未补查')}** · {check.get('message', '此历史结果尚未补充明细。')}")
                if "residual_yuan" in check:
                    st.caption(check["formula"])
                    st.write(f"计算合计：{format_display_money(Decimal(check['calculated_yuan']), unit)} · "
                             f"披露合计：{format_display_money(Decimal(check['reported_yuan']), unit)} · "
                             f"未解释差额：{format_display_money(Decimal(check['residual_yuan']), unit)}")
                rows = [{"项目": item["item"], f"原表金额（{unit}）": format_display_money(Decimal(item["value_yuan"]), unit),
                         f"计算贡献（{unit}）": format_display_money(Decimal(item["contribution_yuan"]), unit),
                         "期间": f"{item['period_start']}至{item['period_end']}"}
                        for item in report.financial_details if item["category"] == category]
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                    ids = list(dict.fromkeys(x for item in report.financial_details if item["category"] == category for x in item["evidence_ids"]))
                    st.button("查看明细原表", key=f"detail_{category}_{report.document_id}", on_click=focus_evidence, args=(ids, result))
    for signal in result.analysis.signals:
        if signal.category == "accounting_change" and signal.details.get("categories"):
            st.markdown("**会计口径分项核查**")
            names = {"accounting_policy": "会计政策", "accounting_estimate": "会计估计", "consolidation": "合并范围", "restatement": "追溯调整"}
            st.dataframe(pd.DataFrame([{"项目": names[k], "状态": SIGNAL_STATUS[v['status']],
                "原文": "；".join(v['quotes']) or "需补充该项披露"} for k, v in signal.details['categories'].items()]), hide_index=True)


def show_execution(result: RunResult) -> None:
    st.caption("完整运行的执行记录。只展示操作与验证结果，不记录模型隐藏思维。")
    st.write(f"运行编号：{result.run_id}")
    provenance = result.provenance
    st.write(f"生成时间 UTC：{provenance.get('generated_at_utc', '历史结果未记录')}")
    st.caption(f"程序指纹：{provenance.get('code', {}).get('sha256', '历史运行未绑定指纹')}")
    source_rows = [{"文件": s['name'], "页数": s['pages'], "官方来源": s.get('official_url') or '用户本地上传',
                   "SHA256": s['sha256'], "本次核对时间 UTC": s['checked_at_utc'], "原始下载时间": s.get('downloaded_at_utc') or '未记录'} for s in provenance.get('sources', [])]
    if source_rows:
        st.dataframe(pd.DataFrame(source_rows), hide_index=True, width="stretch")
    run_dir = Path(result.run_dir).resolve()
    if not run_dir.is_relative_to(OUTPUTS.resolve()):
        return
    path = run_dir / "trace.jsonl"
    stages = {"run_start": "开始运行", "file_read_requested": "请求读取PDF", "file_access": "核对输入哈希", "parse_financial_doc": "解析文字与表格",
              "retrieve_evidence": "检索并选择证据", "model_request": "发送模型请求", "model_response": "保存模型输出", "validate_metrics": "核验行列、期间和单位",
              "supplement_comparatives": "补查原表可比金额", "enrich_financial_details": "核对原因与调节明细", "financial_analysis": "计算同比环比及关注信号",
              "generate_report": "生成报告与表格", "run_complete": "封存产物清单", "replay_start": "从保存数据离线重算"}
    if path.is_file():
        rows = []
        requests = []
        retrievals = []
        names = {r.document_id: r.document_name for r in result.reports}
        for line in path.read_text(encoding='utf-8').splitlines():
            event = json.loads(line)
            inputs = event.get('input') or {}
            outputs = event.get('output') or {}
            if event['tool'] == 'model_request':
                requests.append({"任务": inputs.get('task'), "尝试": inputs.get('attempt'), **inputs.get('parameters', {})})
            if event['tool'] == 'retrieve_evidence' and outputs.get('decisions'):
                decision = outputs['decisions']
                retrievals.append({"文件": names.get(inputs.get('document_id'), ''), "轮次": inputs.get('round'),
                                   "字符预算": decision.get('max_chars'), "实际字符": decision.get('used_chars'),
                                   "入选页": ', '.join(str(p['pdf_page']) for p in decision.get('pages', []) if p['selected_blocks']),
                                   "补查依据": outputs.get('repair_feedback') or '首次提取'})
            if event['tool'] in stages or event.get('status') != 'ok':
                rows.append({"时间 UTC": event.get('time_utc', ''), "阶段": stages.get(event['tool'], event['tool']),
                             "状态": event.get('status', 'ok'),
                             "文件/任务": inputs.get('file') or outputs.get('name') or names.get(inputs.get('document_id'), '') or inputs.get('task', '')})
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if requests:
            with st.expander("实际模型请求参数"):
                st.json(requests, expanded=False)
                st.caption("未设置未支持的seed参数。temperature=0也不保证实时调用逐字一致；已保存的金额可离线重算。")
        elif provenance.get('execution_kind') == 'validated_data_replay':
            st.info("本次为保存金额的离线重算，模型调用为0。下方为随成果保留的原始模型记录。")
            model_history = provenance.get('replay', {}).get('model_history', [])
            if not model_history:
                st.caption("当前成果未收录原始模型响应记录，不能以本次重算日志替代。")
            for item in model_history:
                history_trace = (run_dir / item['trace_path']).resolve()
                if history_trace.is_relative_to((run_dir / 'history').resolve()) and history_trace.is_file():
                    st.caption(f"原运行 {item['run_id']} · 请求 {item['model_requests']} 次 · 保存响应 {item['model_responses']} 次")
                    st.download_button('下载原始模型运行记录', history_trace.read_bytes(),
                        file_name=f"{item['run_id']}_trace.jsonl", mime='application/x-ndjson', key=f"history_{item['run_id']}")
        if retrievals:
            with st.expander("证据检索与补查决策"):
                st.dataframe(pd.DataFrame(retrievals), hide_index=True)
    if (run_dir / 'artifacts.json').is_file() and st.button("核验本次成果文件完整性", key="verify_artifacts"):
        from finance_agent.provenance import verify_artifacts
        errors = verify_artifacts(run_dir)
        if errors:
            st.error("；".join(errors))
        else:
            st.success("清单中的报告、数据、日志及来源PDF均与封存哈希一致。")


def render_comparison_chart(comparison: Comparison, unit: str) -> None:
    rows = comparison_chart_rows(comparison, unit)
    st.markdown(f"**{metric_label(comparison.metric, comparison.scope)} · {COMPARISON_STATUS[comparison.status]}**")
    st.caption(f"{'同比' if comparison.kind == 'yoy' else '环比'} · "
               f"{BASIS_NAMES.get(comparison.basis, '期间未注明')} · "
               f"{SCOPE_NAMES.get(comparison.scope, '口径未注明')}")
    if not rows:
        st.info("当前金额或比较口径尚未核验，不绘制并列比较图。")
        return
    for row in rows:
        row["颜色"] = "#bc7058" if row["金额"] < 0 else "#327151" if row["期间"] == "本期" else "#a1b993"
    frame = pd.DataFrame(rows)
    source = alt.Chart(frame).encode(
        x=alt.X("期间:N", sort=["基期", "本期"], title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("金额:Q", title=f"金额（{unit}）", scale=alt.Scale(zero=True)),
        tooltip=[alt.Tooltip("期间:N"), alt.Tooltip("起止日期:N"), alt.Tooltip("金额显示:N")],
    )
    bars = source.mark_bar(size=62, cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        color=alt.Color("颜色:N", scale=None, legend=None))
    positive = source.transform_filter(alt.datum["金额"] >= 0).mark_text(dy=-10).encode(text=alt.Text("金额标签:N"))
    negative = source.transform_filter(alt.datum["金额"] < 0).mark_text(dy=14).encode(text=alt.Text("金额标签:N"))
    zero = alt.Chart(pd.DataFrame({"零线": [0]})).mark_rule(color="#9daf98").encode(y="零线:Q")
    st.altair_chart(themed_chart((bars + positive + negative + zero).properties(
        height=260, padding={"left": 10, "right": 20, "top": 20, "bottom": 10})), width="stretch")
    st.caption("；".join(f"{row['期间']}：{row['起止日期']}" for row in rows))
    if comparison.base_value is None:
        st.caption("基期金额尚未核验，图中仅展示本期；没有将缺失基期画成零。")
    if comparison.delta is not None:
        st.write(f"增减金额：**{format_display_money(comparison.delta, unit)}**"
                 + (f" · 变化率：**{comparison.percent:.2f}%**" if comparison.percent is not None else " · 不提供常规增长率"))


def show_visualizations(selected: RunResult, full: RunResult, company: str, end: date,
                        basis: str, unit: str) -> None:
    st.caption(f"图表金额单位：人民币{unit} · {BASIS_NAMES[basis]} · 合并报表。悬停可查看期间和金额。")
    left, right = st.columns([1, 1.1], gap="large")
    with left:
        st.subheader("本期利润对比")
        names = ["parent_net_profit", "adjusted_parent_net_profit", "net_profit"]
        amounts = {name: select_amount(full, company, end, name, basis) for name in names}
        rows = [{"指标": METRIC_NAMES[name], "金额": float(amount.value / MONEY_SCALES[unit]),
                 "金额标签": money_in_unit(amount.value, unit),
                 "金额显示": format_display_money(amount.value, unit),
                 "期间": f"{amount.period_start} 至 {amount.period_end}", "来源方式": amount.origin}
                for name, amount in amounts.items() if amount.value is not None]
        if rows:
            source = alt.Chart(pd.DataFrame(rows)).encode(
                y=alt.Y("指标:N", sort=[METRIC_NAMES[name] for name in names], title=None),
                x=alt.X("金额:Q", title=f"金额（{unit}）", scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("指标:N"), alt.Tooltip("金额显示:N"),
                         alt.Tooltip("期间:N"), alt.Tooltip("来源方式:N")],
            )
            bars = source.mark_bar(size=35).encode(
                color=alt.condition(alt.datum["金额"] < 0, alt.value("#bc7058"), alt.value("#327151")))
            positive = source.transform_filter(alt.datum["金额"] >= 0).mark_text(align="left", dx=6).encode(text=alt.Text("金额标签:N"))
            negative = source.transform_filter(alt.datum["金额"] < 0).mark_text(align="left", dx=4, dy=-24).encode(text=alt.Text("金额标签:N"))
            zero = alt.Chart(pd.DataFrame({"零线": [0]})).mark_rule(color="#9daf98").encode(x="零线:Q")
            st.altair_chart(themed_chart((bars + positive + negative + zero).properties(
                height=260, padding={"left": 10, "right": 35, "top": 15, "bottom": 10})), width="stretch")
            st.caption("三种利润分别列示，不相加。利润为负表示亏损，现金流为负表示净流出；负数本身不代表提取错误。")
            table = [{"指标": METRIC_NAMES[name], f"金额（{unit}）": format_display_money(amount.value, unit, include_unit=False),
                      "说明": amount.note} for name, amount in amounts.items()]
            with st.expander("利润图金额与校验说明"):
                st.dataframe(table, hide_index=True, width="stretch")
                shown_checks = set()
                for report in selected.reports:
                    for metric in report.metrics:
                        if metric.name in names and metric.period_end == end and metric.basis == basis:
                            for check in metric.checks:
                                if "少数股东" in check and check not in shown_checks:
                                    st.write(check)
                                    shown_checks.add(check)
            ensure_choice("profit_chart_metric", names, default="net_profit")
            chosen = st.selectbox("选择利润核对原文", names, format_func=lambda name: METRIC_NAMES[name], key="profit_chart_metric")
            st.button("定位利润原文", key="profit_chart_evidence", disabled=not amounts[chosen].evidence_ids,
                      on_click=focus_evidence, args=(amounts[chosen].evidence_ids, selected))
        else:
            st.info("没有已核验的同期间利润金额；缺失金额不画成零。")
        missing = [METRIC_NAMES[name] for name, amount in amounts.items() if amount.value is None]
        if missing:
            st.caption("未绘制：" + "、".join(missing) + "。可到财务明细和原文核对补查。")
    with right:
        st.subheader("本期与基期金额")
        options = [i for i, item in enumerate(selected.analysis.comparisons)
                   if item.basis == basis and item.scope == "consolidated"]
        if options:
            preferred = next((i for i in options if selected.analysis.comparisons[i].metric == "operating_cashflow"), options[0])
            ensure_choice("comparison_chart_reference", options, default=preferred)
            index = st.selectbox("选择图表比较指标", options, key="comparison_chart_reference",
                format_func=lambda i: (f"{METRIC_NAMES[selected.analysis.comparisons[i].metric]} · "
                    f"{'同比' if selected.analysis.comparisons[i].kind == 'yoy' else '环比'}"))
            comparison = selected.analysis.comparisons[index]
            render_comparison_chart(comparison, unit)
            st.write(comparison.explanation)
            hint = comparison_material_hint(comparison, full.reports)
            if hint:
                st.info(hint)
            st.button("定位图表比较原文", key="comparison_chart_evidence", disabled=not comparison.evidence_ids,
                      on_click=focus_comparison, args=(comparison, selected))
        else:
            st.info("当前资料没有这一期间口径的比较记录。可切换累计／单季或补充对应材料。")
    st.caption("季度趋势位于“分析总览”，可切换收入、利润和现金流。表格与导出保留精确金额及原文依据。")


def show_overview(selected: RunResult, full: RunResult, company: str, end: date,
                  basis: str, unit: str) -> None:
    st.caption(f"金额单位：人民币{unit} · {BASIS_NAMES[basis]} · 合并报表")
    with st.container(key="amount_cards"):
        cards = st.columns(4, gap="small")
        for column, name in zip(cards, ("revenue", "parent_net_profit", "net_profit", "operating_cashflow")):
            amount = select_amount(full, company, end, name, basis)
            with column:
                st.metric(METRIC_NAMES[name], money_in_unit(amount.value, unit),
                          help=amount.note + (f" 精确金额：{amount.value:,f} 元。" if amount.value is not None else ""))
                if amount.period_start:
                    st.caption(f"{amount.period_start} 至 {amount.period_end} · {amount.origin}")
                else:
                    st.caption(amount.origin if amount.value is not None else amount.note)
                if amount.value is not None and "追溯调整" in amount.note:
                    st.caption("已标记追溯调整，请核对调整说明。")
                st.button("核对金额", key=f"amount_source_{name}", disabled=not amount.evidence_ids,
                          on_click=focus_evidence, args=(amount.evidence_ids, selected))
    st.caption("归母净利润与合并净利润分别展示；缺少同口径金额时留空，不用其他期间或指标替代。")
    with st.container(key="overview_panels"):
        left, right = st.columns([1.4, 1], gap="medium")
    with left:
        st.subheader("季度趋势")
        names = ["revenue", "parent_net_profit", "adjusted_parent_net_profit", "net_profit", "operating_cashflow"]
        ensure_choice("chart_metric", names)
        name = st.selectbox("趋势指标", names, format_func=lambda item: METRIC_NAMES[item], key="chart_metric")
        series = quarter_series(full, company, name, through=end)
        if series:
            rows = [{"季度": f"{item.period_end.year} Q{(item.period_end.month - 1) // 3 + 1}",
                     "金额": float(item.value / MONEY_SCALES[unit]) if item.value is not None else None,
                     "金额显示": format_display_money(item.value, unit),
                     "来源方式": item.origin, "状态": item.note} for item in series]
            frame = pd.DataFrame(rows)
            chart = alt.Chart(frame).mark_bar(width=alt.RelativeBandSize(band=.62), cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X("季度:N", sort=[row["季度"] for row in rows], title=None,
                        axis=alt.Axis(labelAngle=-35)),
                y=alt.Y("金额:Q", title=f"{METRIC_NAMES[name]}（{unit}）", scale=alt.Scale(zero=True)),
                color=alt.condition(alt.datum["金额"] < 0, alt.value("#bc7058"), alt.value("#327151")),
                tooltip=[alt.Tooltip("季度:N"), alt.Tooltip("金额显示:N"),
                         alt.Tooltip("来源方式:N"), alt.Tooltip("状态:N")],
            ).properties(height=270)
            st.altair_chart(themed_chart(chart), width="stretch")
            st.caption("趋势图固定显示单季度，来自核验后的季度分析；累计差分标注来源，缺失或冲突不填零。")
            table = [{"季度": row["季度"], f"金额（{unit}）": money_in_unit(item.value, unit),
                      "来源方式": item.origin, "说明": item.note} for row, item in zip(rows, series)]
            with st.expander("季度金额与计算来源"):
                st.dataframe(table, hide_index=True, width="stretch")
                options = list(range(len(series)))
                ensure_choice("chart_quarter", options)
                index = st.selectbox("选择季度核对原文", options,
                    format_func=lambda i: f"{rows[i]['季度']} · {series[i].origin}", key="chart_quarter")
                st.button("定位季度依据", key="quarter_evidence", disabled=not series[index].evidence_ids,
                          on_click=focus_evidence, args=(series[index].evidence_ids, selected))
        else:
            st.info("当前资料没有可核验的单季度金额。年度累计金额不会被直接画成季度趋势。")
        with st.expander("参与本期分析的材料"):
            for report in selected.reports:
                st.write(report.document_name)
            st.caption("同公司同期材料共同参与核验，冲突值不会按上传顺序任选。")
    with right:
        st.subheader("本期关注事项")
        st.caption("主要按本报告累计口径评估，与上方单季金额切换独立。")
        with st.container(key="signal_counts"):
            counts = st.columns(3, gap="small")
            for column, status, label in zip(counts, ("detected", "not_detected", "insufficient_evidence"),
                                             ("关注信号", "已检未发现", "待核实")):
                column.metric(label, sum(signal.status == status for signal in selected.analysis.signals))
        for signal in sorted(selected.analysis.signals, key=lambda s: (s.status != "detected", s.category)):
            show_signal_card(signal, selected, compact=True, unit=unit)


def show_result(result: RunResult, source_files: dict[str, str]) -> None:
    origin = st.session_state.get("result_origin") if st.session_state.get("result_origin_run_id") == result.run_id else None
    if result.mode == "demo":
        label = "虚构样本 · 本地演示"
        description = "当前是虚构数据，用于展示流程；没有调用 DeepSeek。"
    elif origin == "saved" or "replay_model_calls" in result.usage:
        label = "真实公开样本 · 已保存结果"
        if origin == "saved":
            dataset = st.session_state.get("result_saved_dataset")
            if dataset == YILI_SHOWCASE:
                label = "真实公开样本 · 伊利2024 / 2025年报 · 已保存结果"
            elif dataset == PUBLIC_SHOWCASE:
                label = "真实公开样本 · 四家公司 / 六份财报 · 已保存结果"
        description = f"打开与切换结果不调用 API；原始提取共调用模型 {result.usage.get('model_calls', 0)} 次。"
    else:
        label = "真实财报 · 本次分析结果"
        description = f"本次实际模型调用 {result.usage.get('model_calls', 0)} 次。"
    st.markdown(f'<span class="source-tag">{escape(label)}</span>', unsafe_allow_html=True)
    companies = list(dict.fromkeys(report.company for report in result.reports))
    if not companies:
        st.info("本次没有可展示的报告。")
        return
    with st.container(key="report_filters"):
        controls = st.columns([2.1, 1.25, 1.25, 1], gap="small")
        ensure_choice("dashboard_company", companies)
        company = controls[0].selectbox("公司", companies, key="dashboard_company", on_change=change_company)
        periods = sorted({report.period_end for report in result.reports if report.company == company}, reverse=True)
        ensure_choice("dashboard_period", periods)
        end = controls[1].selectbox("报告截止日", periods, format_func=str, key="dashboard_period", on_change=change_period)
        basis_label = controls[2].radio("金额口径", ["累计", "单季"], horizontal=True, key="dashboard_basis")
        unit = controls[3].selectbox("展示单位", list(MONEY_SCALES), key="dashboard_unit")
    basis = "ytd" if basis_label == "累计" else "quarter"
    selected = filter_result(result, company, end)
    views = ["分析总览", "图表分析", "期间比较", "关注信号", "原因与明细", "财务明细", "原文核对", "执行过程", "报告导出"]
    ensure_choice("result_view", views)
    view = st.radio("查看内容", views, horizontal=True, label_visibility="collapsed", key="result_view")
    if view == "分析总览":
        show_overview(selected, result, company, end, basis, unit)
    elif view == "图表分析":
        show_visualizations(selected, result, company, end, basis, unit)
    elif view == "期间比较":
        show_comparisons(selected, unit, full_reports=result.reports)
    elif view == "关注信号":
        show_signals(selected, unit)
    elif view == "财务明细":
        show_metrics(selected, unit)
    elif view == "原因与明细":
        show_explanations(selected, unit)
    elif view == "执行过程":
        show_execution(result)
    elif view == "原文核对":
        show_source_pages(selected, source_files)
    else:
        show_downloads(result)
    warnings = list(dict.fromkeys(result.warnings + result.analysis.warnings + [
        warning for report in selected.reports for warning in report.warnings
    ]))
    if warnings:
        with st.expander(f"本次运行的待核实事项（{len(warnings)} 条）"):
            for warning in warnings:
                st.write(warning)
    with st.expander("分析记录"):
        st.caption(f"{description} 本次共 {len(result.reports)} 份报告 · 模型：{result.model}")


def cloud_login(config: CloudConfig) -> bool:
    if (st.session_state.get('cloud_access_version') == config.credential_version
            and re.fullmatch(r'[a-f0-9]{32}', str(st.session_state.get('cloud_session_id', '')))):
        return True
    clear_result()
    st.session_state.pop('cloud_session_id', None)
    st.subheader("组员测试入口")
    st.caption("使用管理员提供的组员访问口令进入。此处不要填写DeepSeek API密钥。")

    def submit():
        supplied = st.session_state.pop('cloud_access_input', '')
        if time.time() < st.session_state.get('cloud_retry_after', 0):
            return
        if config.accepts(supplied):
            st.session_state['cloud_access_version'] = config.credential_version
            st.session_state['cloud_session_id'] = uuid.uuid4().hex
            st.session_state.pop('cloud_login_failures', None)
        else:
            failures = st.session_state.get('cloud_login_failures', 0) + 1
            st.session_state['cloud_login_failures'] = failures
            if failures % 5 == 0:
                st.session_state['cloud_retry_after'] = time.time() + 60

    with st.form('cloud_login_form'):
        st.text_input('组员访问口令', type='password', key='cloud_access_input')
        st.form_submit_button('进入工作台', on_click=submit)
    if time.time() < st.session_state.get('cloud_retry_after', 0):
        st.warning("输入次数较多，请稍候一分钟再试。")
    elif st.session_state.get('cloud_login_failures'):
        st.warning("访问口令不正确，请核对管理员提供的口令。")
    st.info("云端仅测试公开财报。上传文件存放在云端临时会话目录，分析所需片段会发送至DeepSeek。")
    return False


def cloud_logout():
    clear_result()
    for key in ('cloud_access_version', 'cloud_session_id', 'cloud_access_input'):
        st.session_state.pop(key, None)


def main() -> None:
    st.set_page_config(page_title="财报核验工作台 · 北林学生项目", page_icon="🌿", layout="wide")
    apply_styles()
    st.markdown(f"""<div class="workbench-header"><div>
    <div class="project-credit">北京林业大学 · 学生竞赛项目</div>
    <h1>财报核验工作台</h1><p>财务数据、期间变化与原文依据</p></div>{LEAF_MARK}</div>""", unsafe_allow_html=True)
    cloud_config = None
    upload_root, analysis_output = UPLOADS, OUTPUTS
    if is_cloud():
        try:
            cloud_config = CloudConfig.load()
        except CloudError as exc:
            st.error(str(exc))
            st.stop()
        if not cloud_login(cloud_config):
            st.stop()
        upload_root, analysis_output = session_paths(cloud_config, st.session_state['cloud_session_id'])
        clean_expired_sessions(cloud_config, st.session_state['cloud_session_id'])
    settings = None
    with st.sidebar:
        st.markdown(f'<div class="sidebar-brand">{LEAF_MARK}<div><strong>财报核验</strong>'
                    '<span>北京林业大学·创新者联盟队</span></div></div>', unsafe_allow_html=True)
        if cloud_config:
            st.caption("组员云端测试 · 固定网址")
            st.button("退出组员测试", on_click=cloud_logout)
        mode_label = st.radio("选择模式", ["成果展示", "真实财报分析", "演示模式"],
                              label_visibility="collapsed", key="run_mode")
        mode = {"成果展示": "showcase", "演示模式": "demo", "真实财报分析": "live"}[mode_label]
        if mode == "demo":
            st.caption("使用虚构样本体验分析流程，不调用 API。")
        elif mode == "live":
            try:
                settings = Settings.load(ROOT)
            except (OSError, ValueError):
                st.error("本地配置无法读取，请检查超时、调用次数和输出长度是否填写为数字。")
                st.stop()
            if settings.api_key:
                st.success("DeepSeek 云端配置已就绪" if cloud_config else "DeepSeek 本地配置已读取")
            else:
                st.warning("管理员尚未配置云端DEEPSEEK_API_KEY。" if cloud_config else "请先在项目 .env 中填写 DEEPSEEK_API_KEY，然后重新运行。")
            st.caption(f"模型：{settings.model}")
            st.caption("密钥由服务器配置，组员无需填写；界面不展示密钥。" if cloud_config else "密钥只从本地配置读取，界面不展示密钥。")
            if cloud_config:
                st.caption(f"每次分析最多{settings.max_model_calls}次模型请求；失败重试也计入。")
        else:
            st.markdown('<div class="sidebar-note">浏览已保存的公开财报分析结果。<br>无需密钥，不产生新的模型调用。</div>', unsafe_allow_html=True)
        st.divider()
        with st.expander("使用说明"):
            st.caption("支持可复制文本的中文财报 PDF；扫描件与缺失附注需要补充核对。")
            st.caption("关注信号用于辅助复核，不等于财务造假认定。")

    uploaded_files: list[Any] = []
    preset_paths: list[Path] = []
    allowed = True
    if mode == "showcase":
        requested_dataset = YILI_SHOWCASE if st.query_params.get("dataset") == "yili" else PUBLIC_SHOWCASE
        showcase_options = [PUBLIC_SHOWCASE]
        if (YILI_RESULT_INDEX.is_file() or st.session_state.get("showcase_dataset") == YILI_SHOWCASE
                or requested_dataset == YILI_SHOWCASE):
            showcase_options.append(YILI_SHOWCASE)
        ensure_choice("showcase_dataset", showcase_options, default=requested_dataset)
        dataset = st.sidebar.selectbox("财报样本", showcase_options, key="showcase_dataset",
            format_func=lambda item: "伊利 · 2024 / 2025年报" if item == YILI_SHOWCASE else "公开样本 · 4家公司")
        # A browser refresh starts a fresh session; the URL keeps the selected saved sample.
        dataset_code = "yili" if dataset == YILI_SHOWCASE else "public"
        if st.query_params.get("dataset") != dataset_code:
            st.query_params["dataset"] = dataset_code
        loader = load_yili_result if dataset == YILI_SHOWCASE else load_verified_public_result
        current_result = st.session_state.get("analysis_result")
        pointer_path = YILI_RESULT_INDEX if dataset == YILI_SHOWCASE else ROOT / "samples/public/latest_verified_run.json"
        pointer_run = None
        try:
            pointer_run = json.loads(pointer_path.read_text(encoding='utf-8')).get('run_id')
        except (OSError, ValueError, AttributeError):
            pass
        if (current_result is None or st.session_state.get("result_origin") != "saved"
                or st.session_state.get("result_saved_dataset") != dataset
                or st.session_state.get("result_origin_run_id") != current_result.run_id
                or pointer_run != current_result.run_id or not hasattr(current_result, 'provenance')):
            try:
                result, source_files = loader()
                assign_result(result, source_files, "saved", dataset)
            except (OSError, ValueError, KeyError, TypeError):
                clear_result()
                st.error(f"“{dataset}”读取失败，请检查结果记录和来源 PDF 是否完整。当前没有展示其他数据集。")
        st.sidebar.caption("组员提供的两份完整年报" if dataset == YILI_SHOWCASE
                           else "四家公司，六份公开财报")
        reload_label = "重新加载伊利年报测试结果" if dataset == YILI_SHOWCASE else "重新加载6份公开样本"
        reload_key = "reload_yili" if dataset == YILI_SHOWCASE else "reload_public"
        if st.sidebar.button(reload_label, key=reload_key, help="读取已保存结果，不调用API"):
            try:
                result, source_files = loader()
                assign_result(result, source_files, "saved", dataset)
            except (OSError, ValueError, KeyError, TypeError):
                clear_result()
                st.error(f"“{dataset}”读取失败，请检查结果记录和来源 PDF 是否完整。当前没有展示其他数据集。")
    elif mode == "demo":
        st.info("演示模式使用虚构企业的两期样本，可检查上传后的整个分析和证据核对流程。")
    else:
        sample_group = st.selectbox("选择财报来源", ["上传自己的财报", *PUBLIC_SAMPLE_GROUPS])
        if sample_group != "上传自己的财报":
            preset_paths = [ROOT / "samples" / "public" / name for name in PUBLIC_SAMPLE_GROUPS[sample_group]]
            missing_samples = [path.name for path in preset_paths if not path.is_file()]
            if missing_samples:
                st.error("样本文件缺失：" + "、".join(missing_samples))
                preset_paths = []
            else:
                st.caption("本次使用：" + "、".join(path.name for path in preset_paths))
                if any("摘要" in path.name for path in preset_paths):
                    st.info("华虹半年报使用公开摘要，支持主要金额比较；合并净利润和附注覆盖有限。")
        uploaded_files = st.file_uploader(
            "选择财报 PDF（可多选，每份不超过 30 MB）", type=["pdf"], accept_multiple_files=True,
            help="建议上传同一家公司的年度、半年度或季度报告。文件上传到云端临时会话目录，每批最多6份/80 MB。" if cloud_config else "建议上传同一家公司的年度、半年度或季度报告。来源文件将保存在本地 data/uploads。",
        ) or []
        allowed = st.checkbox(
            "这些是可上传的公开财报，我同意将分析所需的相关片段发送至 DeepSeek。", value=not bool(cloud_config),
        )
        st.caption("解析、计算和结果暂存在云端；完成后请下载完整成果备份。真实分析使用管理员的DeepSeek API额度。" if cloud_config else "解析、指标计算和结果文件保存在本地；真实分析会调用官方 DeepSeek 接口。")
        if (ROOT / "samples/public/latest_verified_run.json").is_file():
            if st.button("查看已核对的6份样本结果（不调用API）", key="show_verified_public"):
                try:
                    result, source_files = load_verified_public_result()
                    assign_result(result, source_files, "saved")
                except (OSError, ValueError, KeyError, TypeError):
                    st.warning("已保存的样本结果当前不可读取，请查看项目中的自查记录，或重新选择公开财报分析。")

    button_disabled = mode == "live" and (settings is None or not settings.api_key or not (uploaded_files or preset_paths) or not allowed)
    if mode != "showcase" and st.button("运行演示" if mode == "demo" else "开始分析", type="primary", disabled=button_disabled):
        st.session_state.pop("analysis_result", None)
        st.session_state.pop("source_files", None)
        with st.status("正在准备财报…", expanded=True) as status:
            def on_progress(message: str) -> None:
                status.write(message)

            try:
                if mode == 'live' and not preset_paths:
                    validate_uploads(uploaded_files)
                with analysis_slot(cloud_config, mode) if cloud_config else nullcontext():
                    paths = ensure_demo_files() if mode == "demo" else (preset_paths or store_uploads(uploaded_files, upload_root))
                    source_files = map_source_files(paths)
                    result = run_analysis(paths, mode=mode, output_root=analysis_output, progress=on_progress)
            except CloudError as error:
                status.update(label="云端任务暂未开始", state="error", expanded=True)
                st.warning(str(error))
            except ModelError as error:
                status.update(label="模型调用未完成", state="error", expanded=True)
                # ModelError 只包含本地定义的说明，不含服务端响应或请求凭据。
                st.error(str(error))
            except ValueError as error:
                status.update(label="输入或配置需要调整", state="error", expanded=True)
                # 只显示本模块定义的输入提示，避免泄露服务端响应与密钥。
                known_messages = {
                    "有文件超过 30 MB，请压缩或拆分后重新上传。",
                    "有文件不符合 PDF 格式，请重新选择财报 PDF。",
                    "请一次上传1至6份财报，文件较多请分批分析。",
                    "云端每批文件总大小不能超过80 MB，请分批上传。",
                }
                text = str(error)
                st.error(text if text in known_messages else "分析未完成，请检查财报是否可提取文本，以及本地模型配置是否正确。")
            except Exception:
                status.update(label="分析未完成", state="error", expanded=True)
                st.error("分析未完成。请检查网络、DeepSeek 账户余额和 API 配置，并尝试文本版财报 PDF。")
            else:
                assign_result(result, source_files, "demo" if mode == "demo" else "live")
                status.update(label="分析完成，可以核对指标和原文证据", state="complete", expanded=False)

    result = st.session_state.get("analysis_result")
    if result is not None:
        show_result(result, st.session_state.get("source_files", {}))


if __name__ == "__main__":
    main()
