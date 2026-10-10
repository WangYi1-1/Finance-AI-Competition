"""财务口径边界与缺证据行为测试，含人工构造样例和已保存真实运行。"""
from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from finance_agent.analysis import (
    analyze, compare_metrics, comparison_material_hint, derive_quarter, detect_accounting_change,
    detect_cashflow_divergence, detect_nonrecurring,
)
from finance_agent.schemas import Coverage, Disclosure, ExtractedReport, Metric, RunResult


def metric(name="revenue", value="100", *, start="2025-01-01", end="2025-06-30", **kwargs):
    return Metric(name=name, value=Decimal(value), company="测试公司", raw_value=value,
                  unit="元", period_start=date.fromisoformat(start), period_end=date.fromisoformat(end),
                  basis=kwargs.pop("basis", "ytd"), scope=kwargs.pop("scope", "consolidated"),
                  evidence_ids=kwargs.pop("evidence_ids", [f"e:{name}:{end}:{value}"]), **kwargs)


def report(metrics=(), coverage=(), disclosures=(), *, end="2025-06-30", document_id="doc"):
    return ExtractedReport(document_id=document_id, document_name=f"{document_id}.pdf", company="测试公司",
                           report_type="half_year", period_end=date.fromisoformat(end), metrics=list(metrics),
                           coverage=list(coverage), disclosures=list(disclosures))


class ComparisonTests(unittest.TestCase):
    def test_decimal_yoy_and_calendar_leap_year(self):
        current = metric(value="0.3", start="2024-01-01", end="2024-12-31")
        base = metric(value="0.2", start="2023-01-01", end="2023-12-31")
        # 自定义近零阈值，验证 Decimal 不受浮点误差影响。
        from finance_agent.analysis import RuleConfig
        result = compare_metrics(current, base, "yoy", config=RuleConfig(near_zero_base=Decimal("0.01")))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.delta, Decimal("0.1"))
        self.assertEqual(result.percent, Decimal("50"))

    def test_zero_negative_near_zero_bases(self):
        current = metric(value="100", start="2026-01-01", end="2026-06-30")
        for value, expected in [("0", "zero_base"), ("-50", "negative_base"), ("0.01", "near_zero_base")]:
            with self.subTest(value=value):
                result = compare_metrics(current, metric(value=value), "yoy")
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.percent)
                self.assertEqual(result.delta, Decimal("100") - Decimal(value))
        self.assertIn("扭亏", compare_metrics(metric("net_profit", "100", start="2026-01-01", end="2026-06-30"),
                                              metric("net_profit", "-50"), "yoy").explanation)

    def test_negative_operating_cashflow_is_not_described_as_a_profit_loss(self):
        current = metric("operating_cashflow", "-200", start="2026-01-01", end="2026-06-30")
        result = compare_metrics(current, metric("operating_cashflow", "-100"), "yoy")
        self.assertEqual(result.status, "negative_base")
        self.assertIsNone(result.percent)
        self.assertEqual(result.delta, Decimal("-100"))
        self.assertIn("净流出扩大", result.explanation)
        self.assertNotIn("亏损", result.explanation)

    def test_profit_sign_changes_have_transition_status_and_only_amount_delta(self):
        for name in ("net_profit", "parent_net_profit", "adjusted_parent_net_profit"):
            for current_value, base_value, status, label in (
                ("-1829000000", "5568000000", "profit_to_loss", "由盈转亏"),
                ("100", "-50", "loss_to_profit", "扭亏为盈"),
            ):
                with self.subTest(name=name, status=status):
                    current = metric(name, current_value, start="2026-01-01", end="2026-06-30")
                    result = compare_metrics(current, metric(name, base_value), "yoy")
                    self.assertEqual(result.status, status)
                    self.assertIsNone(result.percent)
                    self.assertEqual(result.delta, Decimal(current_value) - Decimal(base_value))
                    self.assertIn(label, result.explanation)
                    self.assertNotIn("错误", result.explanation)

    def test_cashflow_sign_changes_use_cash_direction_not_profit_terms(self):
        for current_value, base_value, status in (
            ("-100", "50", "cashflow_to_outflow"),
            ("100", "-50", "cashflow_to_inflow"),
        ):
            with self.subTest(status=status):
                current = metric("operating_cashflow", current_value, start="2026-01-01", end="2026-06-30")
                result = compare_metrics(current, metric("operating_cashflow", base_value), "yoy")
                self.assertEqual(result.status, status)
                self.assertIsNone(result.percent)
                self.assertEqual(result.delta, Decimal(current_value) - Decimal(base_value))
                self.assertNotIn("盈", result.explanation)
                self.assertNotIn("亏", result.explanation)

    def test_sign_change_never_bypasses_scope_or_date_validation(self):
        current = metric("net_profit", "-100", start="2026-01-01", end="2026-06-30")
        result = compare_metrics(current, metric("net_profit", "50", scope="parent"), "yoy")
        self.assertEqual(result.status, "incomparable")
        self.assertIsNone(result.delta)
        self.assertIsNone(result.percent)

    def test_positive_parent_profit_does_not_override_consolidated_loss(self):
        r = report([metric("parent_net_profit", "15"), metric("net_profit", "-18"),
                    metric("net_profit", "55", start="2024-01-01", end="2024-06-30")])
        consolidated = next(item for item in analyze([r]).comparisons if item.metric == "net_profit")
        self.assertEqual(consolidated.status, "profit_to_loss")
        self.assertEqual(consolidated.current_value, Decimal("-18"))
        self.assertEqual(consolidated.delta, Decimal("-73"))

    def test_mismatched_scope_and_unverified_are_rejected(self):
        current = metric(start="2026-01-01", end="2026-06-30")
        self.assertEqual(compare_metrics(current, metric(scope="parent"), "yoy").status, "incomparable")
        self.assertEqual(compare_metrics(current, metric(status="unverified"), "yoy").status, "incomparable")

    def test_different_duration_and_ytd_qoq_are_rejected(self):
        self.assertEqual(compare_metrics(metric(start="2026-01-01", end="2026-06-30"),
                                         metric(end="2025-03-31"), "yoy").status, "incomparable")
        self.assertEqual(compare_metrics(metric(), metric(end="2025-03-31"), "qoq").status, "incomparable")

    def test_restatement_requires_proven_alignment(self):
        current = metric(start="2026-01-01", end="2026-06-30")
        base = metric(restated=True)
        self.assertEqual(compare_metrics(current, base, "yoy").status, "incomparable")
        aligned = compare_metrics(current, base, "yoy", restatement_aligned=True)
        self.assertEqual(aligned.status, "ok")
        self.assertIn("追溯", aligned.explanation)

    def test_quarter_flow_subtraction_preserves_evidence(self):
        derived = derive_quarter(metric(value="300"), metric(value="100", end="2025-03-31"))
        self.assertEqual(derived.value, Decimal("200"))
        self.assertEqual(derived.period_start, date(2025, 4, 1))
        self.assertEqual(derived.basis, "quarter")
        self.assertEqual(len(derived.evidence_ids), 2)
        self.assertEqual(compare_metrics(derived, metric(value="100", end="2025-03-31", basis="quarter"), "qoq").percent,
                         Decimal("100"))

    def test_stock_subtraction_and_nonadjacent_ytd_are_rejected(self):
        with self.assertRaises(ValueError):
            derive_quarter(metric(name="total_assets"), metric(name="total_assets", end="2025-03-31"))
        with self.assertRaises(ValueError):
            derive_quarter(metric(end="2025-09-30"), metric(end="2025-03-31"))

    def test_point_qoq_requires_calendar_quarter_end(self):
        current = metric("total_assets", "200", start="2025-05-31", end="2025-05-31", basis="point")
        base = metric("total_assets", "100", start="2025-02-28", end="2025-02-28", basis="point")
        self.assertEqual(compare_metrics(current, base, "qoq").status, "incomparable")
        self.assertEqual(compare_metrics(current, None, "qoq").status, "incomparable")

    def test_missing_base_does_not_mask_invalid_current_period(self):
        bad = metric(start="2025-04-01", end="2025-06-30", basis="ytd")
        self.assertEqual(compare_metrics(bad, None, "yoy").status, "incomparable")
        self.assertEqual(compare_metrics(metric(status="unverified"), None, "yoy").status, "incomparable")


class ComparisonMaterialTests(unittest.TestCase):
    def test_ytd_yoy_recommends_specific_previous_report_after_current_column(self):
        current = metric(end="2025-09-30")
        comparison = compare_metrics(current, None, "yoy")
        hint = comparison_material_hint(comparison, [report([current], end="2025-09-30")])
        self.assertIn("已上传本期报告的上年同期比较列", hint)
        self.assertIn("2024-01-01至2024-09-30累计金额", hint)
        self.assertIn("需上传2024年第三季度报告", hint)
        self.assertNotIn("2024年年度报告", hint)

    def test_uploaded_baseline_record_requires_reverification_not_reupload(self):
        current = metric(start="2026-01-01", end="2026-06-30")
        baseline = metric(status="unverified")
        comparison = compare_metrics(current, None, "yoy")
        hint = comparison_material_hint(comparison, [report([current, baseline], end="2026-06-30")])
        self.assertIn("已有", hint)
        self.assertIn("不必重复上传", hint)
        self.assertNotIn("需上传", hint)

    def test_uploaded_report_without_required_metric_is_not_requested_again(self):
        comparison = compare_metrics(metric(start="2026-01-01", end="2026-06-30"), None, "yoy")
        hint = comparison_material_hint(comparison, [report()])
        self.assertIn("2025年半年度报告已上传", hint)
        self.assertIn("尚未取得", hint)
        self.assertIn("不必重复上传", hint)
        self.assertNotIn("需上传", hint)

    def test_second_quarter_base_needs_half_year_minus_first_quarter(self):
        current = metric(start="2025-07-01", end="2025-09-30", basis="quarter")
        hint = comparison_material_hint(compare_metrics(current, None, "qoq"))
        self.assertIn("2025-04-01至2025-06-30单季金额", hint)
        self.assertIn("2025年半年度报告累计减2025年第一季度报告累计", hint)
        self.assertIn("需上传2025年半年度报告、2025年第一季度报告", hint)

    def test_fourth_quarter_base_crosses_year_and_requires_annual_minus_q3(self):
        current = metric(start="2025-01-01", end="2025-03-31", basis="quarter")
        comparison = compare_metrics(current, None, "qoq")
        hint = comparison_material_hint(comparison)
        self.assertIn("2024-10-01至2024-12-31单季金额", hint)
        self.assertIn("2024年年度报告累计减2024年第三季度报告累计", hint)
        self.assertNotIn("2024年第四季度报告", hint)

    def test_partial_quarter_materials_only_recommend_absent_file(self):
        current = metric(start="2025-07-01", end="2025-09-30", basis="quarter")
        hint = comparison_material_hint(compare_metrics(current, None, "qoq"), [report()])
        self.assertIn("2025年半年度报告已上传", hint)
        self.assertIn("需上传2025年第一季度报告", hint)
        self.assertNotIn("需上传2025年半年度报告", hint)

    def test_point_yoy_requires_same_prior_quarter_end_not_year_end(self):
        current = metric("total_assets", start="2025-09-30", end="2025-09-30", basis="point")
        hint = comparison_material_hint(compare_metrics(current, None, "yoy"))
        self.assertIn("2024-09-30期末余额", hint)
        self.assertIn("需上传2024年第三季度报告", hint)
        self.assertNotIn("年度报告", hint)

    def test_point_qoq_uses_prior_quarter_report_without_flow_subtraction(self):
        current = metric("total_assets", start="2025-09-30", end="2025-09-30", basis="point")
        hint = comparison_material_hint(compare_metrics(current, None, "qoq"))
        self.assertIn("2025-06-30期末余额", hint)
        self.assertIn("需上传2025年半年度报告", hint)
        self.assertNotIn("累计减", hint)

    def test_nonmissing_state_does_not_recommend_new_materials(self):
        current = metric(start="2026-01-01", end="2026-06-30")
        self.assertEqual(comparison_material_hint(compare_metrics(current, metric(), "yoy")), "")
        self.assertEqual(comparison_material_hint(compare_metrics(current, metric(scope="parent"), "yoy")), "")


class SignalTests(unittest.TestCase):
    def nonrecurring_report(self, parent="100", adjusted="70", total="30"):
        return report([metric("parent_net_profit", parent), metric("adjusted_parent_net_profit", adjusted),
                       metric("nonrecurring_total_parent", total)],
                      [Coverage(topic="nonrecurring", status="reviewed", evidence_ids=["nonrecurring-table"])])

    def test_nonrecurring_reconciliation_and_threshold(self):
        detected = detect_nonrecurring(self.nonrecurring_report())
        self.assertEqual(detected.status, "detected")
        self.assertEqual(detected.details["derived_nonrecurring"], "30")
        self.assertEqual(detected.details["rule_type"], "heuristic")
        self.assertEqual(detect_nonrecurring(self.nonrecurring_report(adjusted="90", total="10")).status,
                         "not_detected")

    def test_nonrecurring_tax_parent_total_mismatch_is_not_fabricated(self):
        result = detect_nonrecurring(self.nonrecurring_report(total="40"))
        self.assertEqual(result.status, "insufficient_evidence")
        self.assertIn("少数股东", result.message)
        self.assertEqual(result.details["reconciliation_difference"], "-10")

    def test_missing_coverage_or_total_is_insufficient(self):
        normal = self.nonrecurring_report()
        self.assertEqual(detect_nonrecurring(normal.model_copy(update={"coverage": []})).status, "insufficient_evidence")
        self.assertEqual(detect_nonrecurring(normal.model_copy(update={"metrics": normal.metrics[:2]})).status,
                         "insufficient_evidence")

    def test_cashflow_uses_consolidated_profit(self):
        r = report([metric("parent_net_profit", "100"), metric("operating_cashflow", "-20")])
        self.assertEqual(detect_cashflow_divergence(r).status, "insufficient_evidence")
        r.metrics.append(metric("net_profit", "100"))
        result = detect_cashflow_divergence(r)
        self.assertEqual(result.status, "detected")
        self.assertEqual(result.severity, "high")
        self.assertEqual(result.details["cashflow_profit_ratio"], "-0.2")

    def test_cashflow_mismatched_period_negative_profit_and_normal(self):
        bad = report([metric("net_profit", "100"), metric("operating_cashflow", "50", start="2025-04-01", basis="quarter")])
        self.assertEqual(detect_cashflow_divergence(bad).status, "insufficient_evidence")
        self.assertEqual(detect_cashflow_divergence(report([metric("net_profit", "-100"), metric("operating_cashflow", "50")])).status,
                         "insufficient_evidence")
        self.assertEqual(detect_cashflow_divergence(report([metric("net_profit", "100"), metric("operating_cashflow", "80")])).status,
                         "not_detected")

    def test_first_quarter_ytd_and_quarter_are_equivalent_for_signal_matching(self):
        r = report([metric("net_profit", "100", end="2025-03-31", basis="quarter"),
                    metric("operating_cashflow", "120", end="2025-03-31", basis="ytd")], end="2025-03-31")
        result = detect_cashflow_divergence(r)
        self.assertEqual(result.status, "not_detected")
        self.assertEqual(result.details["cashflow_profit_ratio"], "1.2")

    def test_profit_increase_cashflow_decrease_detects_even_high_cash_ratio(self):
        current = report([metric("net_profit", "120", start="2026-01-01", end="2026-06-30"),
                          metric("operating_cashflow", "200", start="2026-01-01", end="2026-06-30")], end="2026-06-30")
        previous = report([metric("net_profit", "100"), metric("operating_cashflow", "250")], document_id="prior")
        result = detect_cashflow_divergence(current, prior_reports=[previous])
        self.assertEqual(result.status, "detected")
        self.assertEqual(result.details["profit_yoy_delta"], "20")
        self.assertEqual(result.details["cashflow_yoy_delta"], "-50")
        self.assertTrue(result.details["trend_divergence"])
        self.assertEqual(len(result.evidence_ids), 4)

    def test_cashflow_transition_and_profit_recovery_still_use_absolute_trend(self):
        current = report([metric("net_profit", "120", start="2026-01-01", end="2026-06-30"),
                          metric("operating_cashflow", "-200", start="2026-01-01", end="2026-06-30")], end="2026-06-30")
        previous = report([metric("net_profit", "-100"), metric("operating_cashflow", "250")], document_id="prior")
        result = detect_cashflow_divergence(current, prior_reports=[previous])
        self.assertEqual(result.details["profit_yoy_status"], "loss_to_profit")
        self.assertEqual(result.details["cashflow_yoy_status"], "cashflow_to_outflow")
        self.assertTrue(result.details["trend_divergence"])
        self.assertEqual(result.details["profit_yoy_delta"], "220")
        self.assertEqual(result.details["cashflow_yoy_delta"], "-450")

    def test_accounting_change_needs_located_coverage_and_quotes(self):
        self.assertEqual(detect_accounting_change(report()).status, "insufficient_evidence")
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["accounting-section"])]
        change = Disclosure(kind="accounting_policy", summary="实施新准则", quote="自本年开始执行新准则，变更会计政策。", evidence_ids=["policy"])
        self.assertEqual(detect_accounting_change(report(coverage=coverage, disclosures=[change])).status, "detected")
        no_changes = [Disclosure(kind=kind, summary="未变化", quote="本报告期未发生相关变更。", evidence_ids=[kind])
                      for kind in ("accounting_policy", "accounting_estimate", "consolidation", "restatement")]
        self.assertEqual(detect_accounting_change(report(coverage=coverage, disclosures=no_changes)).status, "not_detected")
        self.assertEqual(detect_accounting_change(report(coverage=coverage, disclosures=no_changes[:1])).status, "insufficient_evidence")

    def test_accounting_change_with_no_material_effect_is_still_a_change(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["accounting-section"])]
        change = Disclosure(kind="accounting_policy", summary="无重大影响", quote="本次会计政策变更未发生重大影响。", evidence_ids=["policy"])
        self.assertEqual(detect_accounting_change(report(coverage=coverage, disclosures=[change])).status, "detected")

    def test_negative_clause_does_not_hide_another_affirmative_change(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["accounting-section"])]
        change = Disclosure(kind="consolidation", summary="合并范围变化",
                            quote="本期会计政策无变更，但新增子公司导致合并范围发生变化。", evidence_ids=["scope"])
        self.assertEqual(detect_accounting_change(report(coverage=coverage, disclosures=[change])).status, "detected")

    def test_accounting_heading_is_not_treated_as_an_event(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["heading"])]
        heading = Disclosure(kind="accounting_policy", summary="会计政策变更",
                             quote="重要会计政策变更和会计估计变更的原因及影响", evidence_ids=["heading"])
        self.assertEqual(detect_accounting_change(report(coverage=coverage, disclosures=[heading])).status,
                         "insufficient_evidence")

    def test_zero_opening_profit_template_does_not_prove_change_or_no_change(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["section"])]
        quote = (
            "调整期初未分配利润明细：\n"
            "（1）由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。\n"
            "（2）由于会计政策变更，影响期初未分配利润0元。\n"
            "（3）由于重大会计差错更正，影响期初未分配利润0元。\n"
            "（4）由于同一控制导致的合并范围变更，影响期初未分配利润0元。\n"
            "（5）其他调整合计影响期初未分配利润0元。"
        )
        disclosure = Disclosure(kind="restatement", summary="期初未分配利润各项调整均为零",
                                quote=quote, evidence_ids=["opening-adjustments"])
        signal = detect_accounting_change(report(coverage=coverage, disclosures=[disclosure]))
        self.assertEqual(signal.status, "insufficient_evidence")
        self.assertNotIn("restatement", signal.details["reviewed_kinds"])
        self.assertIn("consolidation", signal.details["missing_kinds"])

    def test_zero_opening_profit_template_matches_only_exact_zero_amounts(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["section"])]
        for amount, expected in [("0元", "insufficient_evidence"), ("0.00元", "insufficient_evidence"),
                                 ("零元", "insufficient_evidence"), ("0.01元", "detected"),
                                 ("10元", "detected")]:
            with self.subTest(amount=amount):
                disclosure = Disclosure(kind="restatement", summary="期初调整金额",
                    quote=f"由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润{amount}。",
                    evidence_ids=["adjustment"])
                self.assertEqual(detect_accounting_change(report(coverage=coverage,
                    disclosures=[disclosure])).status, expected)

    def test_actual_policy_change_is_not_suppressed_by_zero_opening_profit_effect(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["section"])]
        for quote in (
            "本次会计政策变更影响期初未分配利润0元。",
            "本公司自2025年1月1日起执行《企业会计准则解释第18号》，影响期初未分配利润0元。",
            "本期已经实施会计政策变更，影响期初未分配利润0.00元。",
            "由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。"
            "本公司自本年开始执行新准则，变更会计政策。",
        ):
            with self.subTest(quote=quote):
                disclosure = Disclosure(kind="accounting_policy", summary="当年实际政策变化",
                                        quote=quote, evidence_ids=["actual-policy"])
                self.assertEqual(detect_accounting_change(report(coverage=coverage,
                    disclosures=[disclosure])).status, "detected")

    def test_zero_template_does_not_hide_actual_scope_change_in_same_quote(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["section"])]
        disclosure = Disclosure(kind="consolidation", summary="本期取得控制权并新纳入合并范围",
            quote="由于会计政策变更，影响期初未分配利润0元。"
                  "本期新增子公司，合并范围发生变化。", evidence_ids=["actual-scope"])
        self.assertEqual(detect_accounting_change(report(coverage=coverage,
            disclosures=[disclosure])).status, "detected")

    def test_generic_policy_does_not_become_actual_event(self):
        coverage = [Coverage(topic="accounting_change", status="reviewed", evidence_ids=["section"])]
        disclosures = [
            Disclosure(kind="accounting_policy", summary="会计期间",
                       quote="本公司会计年度自公历1月1日起至12月31日止。", evidence_ids=["calendar"]),
            Disclosure(kind="consolidation", summary="合并方法",
                       quote="本公司合并财务报表的范围以控制为基础确定，所有子公司均纳入合并财务报表。",
                       evidence_ids=["scope-policy"]),
        ]
        self.assertEqual(detect_accounting_change(report(coverage=coverage,
            disclosures=disclosures)).status, "insufficient_evidence")

    def test_saved_yili_zero_adjustment_event_is_reclassified_without_mutating_run(self):
        fixture = (Path(__file__).resolve().parents[1] / "output/member_yili"
                   / "20261009T055330Z_revalidated_09a532b6/result.json")
        if not fixture.exists():
            self.skipTest("本机真实运行追踪未包含在分发环境中。")
        raw = fixture.read_bytes()
        saved = RunResult.model_validate_json(raw)
        before = saved.model_dump(mode="json")
        original = next(s for s in saved.analysis.signals
                        if s.category == "accounting_change" and s.period_end == date(2025, 12, 31))
        self.assertEqual(original.status, "detected")
        result = analyze(saved.reports)
        accounting = {s.period_end: s for s in result.signals if s.category == "accounting_change"}
        self.assertEqual(accounting[date(2024, 12, 31)].status, "insufficient_evidence")
        self.assertEqual(accounting[date(2025, 12, 31)].status, "insufficient_evidence")
        self.assertIn("consolidation", accounting[date(2025, 12, 31)].details["missing_kinds"])
        self.assertEqual(next(s for s in result.signals if s.category == "cashflow_divergence"
                             and s.period_end == date(2025, 12, 31)).status, "detected")
        self.assertEqual(saved.model_dump(mode="json"), before)
        self.assertEqual(fixture.read_bytes(), raw)

    def test_conflicting_verified_values_are_not_cherry_picked(self):
        r = self.nonrecurring_report()
        r.metrics.append(metric("parent_net_profit", "200"))
        self.assertEqual(detect_nonrecurring(r).status, "insufficient_evidence")


class PipelineTests(unittest.TestCase):
    def test_analyze_emits_three_states_per_report_and_quarter_comparison(self):
        first = report([metric(value="100", end="2025-03-31")], end="2025-03-31", document_id="q1")
        second = report([metric(value="300")])
        result = analyze([first, second])
        self.assertEqual(len(result.signals), 6)
        qoq = next(c for c in result.comparisons if c.kind == "qoq" and c.period_end == date(2025, 6, 30))
        self.assertEqual(qoq.percent, Decimal("100"))
        self.assertEqual(qoq.current_value, Decimal("200"))

    def test_latest_report_restatement_alignment_and_warning(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120")
        base = metric(value="100", restated=True)
        disclosure = Disclosure(kind="restatement", summary="同期重述", quote="上年同期已按调整后的可比口径重述。", evidence_ids=["restatement"])
        result = analyze([report([current, base], disclosures=[disclosure], end="2026-06-30")])
        yoy = next(c for c in result.comparisons if c.kind == "yoy")
        self.assertEqual(yoy.status, "ok")
        self.assertEqual(yoy.percent, Decimal("20"))
        self.assertTrue(any("追溯" in warning for warning in result.warnings))

    def test_proven_adjusted_base_is_selected_over_unadjusted_column(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120")
        before = metric(value="80")
        after = metric(value="100", restated=True)
        disclosure = Disclosure(kind="restatement", summary="比较期间重述",
                                quote="公司采用追溯调整法，并重述了比较期间财务报表。", evidence_ids=["restatement"])
        result = analyze([report([current, before, after], disclosures=[disclosure], end="2026-06-30")])
        yoy = next(c for c in result.comparisons if c.kind == "yoy")
        self.assertEqual(yoy.status, "ok")
        self.assertEqual(yoy.base_value, Decimal("100"))
        self.assertEqual(yoy.percent, Decimal("20"))

    def test_before_and_after_columns_without_alignment_proof_stay_incomparable(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120")
        result = analyze([report([current, metric(value="80"), metric(value="100", restated=True)], end="2026-06-30")])
        self.assertEqual(next(c for c in result.comparisons if c.kind == "yoy").status, "incomparable")

    def test_empty_input_and_unverified_are_explicit(self):
        self.assertTrue(analyze([]).warnings)
        result = analyze([report([metric(status="unverified")])])
        self.assertEqual(result.comparisons, [])
        self.assertTrue(result.warnings)
        self.assertTrue(all(s.status == "insufficient_evidence" for s in result.signals))

    def test_cross_file_current_conflict_is_rejected_independent_of_upload_order(self):
        first = report([metric(value="100"), metric(value="50", start="2024-01-01", end="2024-06-30")], document_id="a")
        second = report([metric(value="200")], document_id="b")
        for reports in ([first, second], [second, first]):
            result = analyze(reports)
            yoy = next(c for c in result.comparisons if c.kind == "yoy")
            self.assertEqual(yoy.status, "incomparable")
            self.assertIsNone(yoy.current_value)
            self.assertIsNone(yoy.percent)
            self.assertEqual(len(result.signals), 3)
            self.assertEqual(set(yoy.evidence_ids), set(first.metrics[0].evidence_ids + second.metrics[0].evidence_ids))

    def test_cross_file_signal_metrics_are_checked_together(self):
        coverage = [Coverage(topic="nonrecurring", status="reviewed", evidence_ids=["table"])]
        first = report([metric("parent_net_profit", "100"), metric("adjusted_parent_net_profit", "70"),
                        metric("nonrecurring_total_parent", "30")], coverage=coverage, document_id="a")
        second = report([metric("parent_net_profit", "200")], document_id="b")
        result = analyze([first, second])
        signal = next(s for s in result.signals if s.category == "nonrecurring")
        self.assertEqual(signal.status, "insufficient_evidence")

    def test_direct_quarter_and_derived_quarter_conflict_is_rejected(self):
        first = report([metric(value="100", end="2025-03-31")], end="2025-03-31", document_id="q1")
        second = report([metric(value="300"), metric(value="220", start="2025-04-01", basis="quarter")])
        result = analyze([first, second])
        qoq = next(c for c in result.comparisons if c.kind == "qoq" and c.period_end == date(2025, 6, 30))
        self.assertEqual(qoq.status, "incomparable")
        self.assertIsNone(qoq.percent)
        self.assertIsNone(qoq.current_value)
        self.assertEqual(set(qoq.evidence_ids), set(m.evidence_ids[0] for r in (first, second) for m in r.metrics))
        quarter_yoy = next(c for c in result.comparisons if c.kind == "yoy" and c.basis == "quarter")
        self.assertEqual(quarter_yoy.status, "incomparable")
        self.assertIsNone(quarter_yoy.percent)

    def test_conflicting_qoq_base_is_not_reported_as_missing(self):
        a = report([metric(value="100", end="2025-03-31", basis="quarter")], end="2025-03-31", document_id="a")
        b = report([metric(value="110", end="2025-03-31", basis="quarter")], end="2025-03-31", document_id="b")
        current = report([metric(value="200", start="2025-04-01", basis="quarter")], document_id="q2")
        qoq = next(c for c in analyze([a, b, current]).comparisons
                   if c.kind == "qoq" and c.period_end == date(2025, 6, 30))
        self.assertEqual(qoq.status, "incomparable")
        self.assertEqual(qoq.current_value, Decimal("200"))
        self.assertIsNone(qoq.percent)
        self.assertEqual(len(qoq.evidence_ids), 3)

    def test_unrelated_adjustment_quote_cannot_align_restatement(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120")
        base = metric(value="100", restated=True)
        disclosure = Disclosure(kind="restatement", summary="调整后处理", quote="本期固定资产已按调整后的估计寿命计提折旧。", evidence_ids=["estimate"])
        result = analyze([report([current, base], disclosures=[disclosure], end="2026-06-30")])
        self.assertEqual(next(c for c in result.comparisons if c.kind == "yoy").status, "incomparable")

    def test_only_current_restatement_cannot_align_unadjusted_base(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120", restated=True)
        base = metric(value="100")
        disclosure = Disclosure(kind="restatement", summary="同期重述", quote="上年同期已按调整后的可比口径重述。", evidence_ids=["restatement"])
        result = analyze([report([current, base], disclosures=[disclosure], end="2026-06-30")])
        self.assertEqual(next(c for c in result.comparisons if c.kind == "yoy").status, "incomparable")

    def test_negated_restatement_quote_does_not_prove_alignment(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120")
        base = metric(value="100", restated=True)
        disclosure = Disclosure(kind="restatement", summary="尚未重述",
                                quote="上年同期尚未按调整后的可比口径重述。", evidence_ids=["restatement"])
        result = analyze([report([current, base], disclosures=[disclosure], end="2026-06-30")])
        self.assertEqual(next(c for c in result.comparisons if c.kind == "yoy").status, "incomparable")

    def test_other_file_adjustment_note_cannot_prove_baseline_alignment(self):
        current = metric(start="2026-01-01", end="2026-06-30", value="120")
        base = metric(value="100", restated=True)
        disclosure = Disclosure(kind="restatement", summary="同期重述",
                                quote="上年同期已按调整后的可比口径重述。", evidence_ids=["restatement"])
        values = report([current, base], end="2026-06-30", document_id="a")
        unrelated = report(disclosures=[disclosure], end="2026-06-30", document_id="b")
        result = analyze([values, unrelated])
        self.assertEqual(next(c for c in result.comparisons if c.kind == "yoy").status, "incomparable")


if __name__ == "__main__":
    unittest.main()
