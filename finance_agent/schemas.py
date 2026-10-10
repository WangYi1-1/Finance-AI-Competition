"""模块之间的数据契约。金额统一为元，原始数值与证据必须保留。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Record):
    id: str
    document_id: str
    document_name: str
    pdf_page: int = Field(ge=1)
    printed_page: str | None = None
    text: str
    bbox: list[float] = Field(default_factory=list)
    kind: Literal["text", "table"] = "text"
    cells: list[list[str]] = Field(default_factory=list)
    header_evidence_id: str | None = None
    context_evidence_ids: list[str] = Field(default_factory=list)


class ParsedDocument(Record):
    id: str
    name: str
    sha256: str
    page_count: int
    blocks: list[Evidence]
    warnings: list[str] = Field(default_factory=list)


MetricName = Literal[
    "revenue", "net_profit", "parent_net_profit", "adjusted_parent_net_profit",
    "operating_cashflow", "accounts_receivable", "inventory", "total_assets",
    "nonrecurring_total_parent",
]


class Metric(Record):
    name: MetricName
    company: str
    value: Decimal
    raw_value: str
    unit: Literal["元", "千元", "万元", "百万元", "亿元"]
    currency: Literal["CNY"] = "CNY"
    period_start: date
    period_end: date
    basis: Literal["ytd", "quarter", "point"]
    scope: Literal["consolidated", "parent"]
    restated: bool = False
    evidence_ids: list[str] = Field(min_length=1)
    status: Literal["verified", "unverified"] = "verified"
    checks: list[str] = Field(default_factory=list)
    row_quote: str = ""
    source_row_index: int | None = Field(default=None, ge=0)
    source_column_index: int | None = Field(default=None, ge=0)


class Disclosure(Record):
    kind: Literal[
        "nonrecurring", "accounting_policy", "accounting_estimate", "consolidation",
        "restatement", "cashflow_reason",
    ]
    summary: str
    quote: str
    evidence_ids: list[str] = Field(min_length=1)


class Coverage(Record):
    topic: Literal["nonrecurring", "accounting_change", "cashflow_reason"]
    status: Literal["reviewed", "missing"]
    evidence_ids: list[str] = Field(default_factory=list)


class ExtractedReport(Record):
    document_id: str
    document_name: str
    company: str
    report_type: Literal["annual", "half_year", "q1", "q3", "unknown"]
    period_end: date
    metrics: list[Metric]
    disclosures: list[Disclosure] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    performance_reasons: list[dict[str, Any]] = Field(default_factory=list)
    financial_details: list[dict[str, Any]] = Field(default_factory=list)
    detail_checks: dict[str, Any] = Field(default_factory=dict)


class Comparison(Record):
    id: str
    metric: MetricName
    company: str
    kind: Literal["yoy", "qoq"]
    period_end: date
    current_period_start: date | None = None
    basis: Literal["ytd", "quarter", "point"] | None = None
    scope: Literal["consolidated", "parent"] | None = None
    base_period_start: date | None = None
    base_period_end: date | None = None
    status: Literal[
        "ok", "missing", "incomparable", "zero_base", "negative_base", "near_zero_base",
        "profit_to_loss", "loss_to_profit", "cashflow_to_outflow", "cashflow_to_inflow",
    ]
    current_value: Decimal | None = None
    base_value: Decimal | None = None
    delta: Decimal | None = None
    percent: Decimal | None = None
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class Signal(Record):
    id: str
    company: str
    period_end: date
    category: Literal["nonrecurring", "accounting_change", "cashflow_divergence"]
    status: Literal["detected", "not_detected", "insufficient_evidence"]
    severity: Literal["info", "watch", "high", "unknown"]
    title: str
    message: str
    evidence_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class AnalysisResult(Record):
    comparisons: list[Comparison] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RunResult(Record):
    run_id: str
    mode: Literal["demo", "live"]
    model: str
    reports: list[ExtractedReport]
    analysis: AnalysisResult
    evidence: dict[str, Evidence]
    run_dir: str
    warnings: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
