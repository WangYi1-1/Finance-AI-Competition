"""对人工标签评分。没有标签时返回未评测，不拿开发自检冒充独立准确率。"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .schemas import RunResult


def _rate(correct, total):
    return round(correct / total, 4) if total else None


def evaluate(result: RunResult, labels: dict):
    required = {"fields": ("company", "name", "period_start", "period_end", "basis", "scope"),
                "comparisons": ("company", "metric", "current_period_start", "period_end", "basis", "scope", "kind"),
                "signals": ("company", "period_end", "category")}
    outputs = {"fields": [m.model_dump(mode="json") for r in result.reports for m in r.metrics],
               "comparisons": [c.model_dump(mode="json") for c in result.analysis.comparisons],
               "signals": [s.model_dump(mode="json") for s in result.analysis.signals]}
    scores = {}
    failures = []
    tp = fp = fn = abstained = signal_total = 0
    citations_correct = citations_total = 0
    for group, keys in required.items():
        correct = covered = 0
        cases = labels.get(group, [])
        seen = set()
        for index, case in enumerate(cases):
            identity = case["match"]
            if set(identity) != set(keys) or not case.get("expected"):
                raise ValueError(f"{group}标签需包含完整期间/口径匹配键和人工预期结果。")
            key = tuple(identity[k] for k in keys)
            if key in seen:
                raise ValueError("人工标签存在重复匹配键。")
            seen.add(key)
            candidates = [row for row in outputs[group] if all(row[k] == identity[k] for k in keys)]
            # 同值重复金额可去重，冲突不能任选一个去匹配答案。
            relevant = set(identity) | set(case['expected']) | {'status'}
            if group == 'fields':
                relevant |= {'value', 'restated', 'currency'}
            unique = {json.dumps({k: v for k, v in row.items() if k in relevant}, sort_keys=True): row for row in candidates}
            row = next(iter(unique.values())) if len(unique) == 1 else None
            covered += row is not None
            numeric = {"value", "percent", "delta", "base_value", "current_value"}
            def equal(field, expected):
                actual = row.get(field) if row else None
                if actual is None or expected is None:
                    return actual is None and expected is None and row is not None
                if field in numeric:
                    return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal(str(case.get("tolerance", "0.01")))
                return actual == expected
            passed = row is not None and all(equal(k, v) for k, v in case["expected"].items())
            if group == 'fields' and row and row.get('status') != 'verified':
                passed = False
            correct += passed
            if not passed:
                failures.append({"group": group, "index": index, "match": identity, "expected": case["expected"], "actual": row})
            # 引用需要人工确认的文档ID+页码标签，不能用“存在证据”替代引用正确性。
            if case.get("accepted_citations"):
                citations_total += 1
                actual_refs = {(result.evidence[e].document_id, result.evidence[e].pdf_page)
                               for e in (row or {}).get("evidence_ids", []) if e in result.evidence}
                accepted = {(item["document_id"], item["pdf_page"]) for item in case["accepted_citations"]}
                citations_correct += bool(actual_refs & accepted)
            if group == "signals" and case["expected"].get("status") in ("detected", "not_detected"):
                signal_total += 1
                expected_positive = case["expected"]["status"] == "detected"
                predicted = row.get("status") if row else None
                abstained += predicted in (None, "insufficient_evidence")
                tp += expected_positive and predicted == "detected"
                fp += not expected_positive and predicted == "detected"
                fn += expected_positive and predicted != "detected"
        scores[group] = {"correct": correct, "total": len(cases), "agreement": _rate(correct, len(cases)),
                         "matched": covered, "coverage": _rate(covered, len(cases))}
    scores["citations"] = {"correct": citations_correct, "total": citations_total, "accuracy": _rate(citations_correct, citations_total),
                           "criterion": "人工接受的文档与页码至少命中一项；不证明整段引文语义完整。"}
    scores["signal_detection"] = {"tp": tp, "fp": fp, "fn": fn, "precision": _rate(tp, tp + fp),
                                  "recall": _rate(tp, tp + fn), "abstention_rate": _rate(abstained, signal_total)}
    return {"run_id": result.run_id, "dataset_role": labels.get("dataset_role", "unspecified"),
            "frozen_at_utc": labels.get("frozen_at_utc"), "scores": scores, "failures": failures,
            "independence_note": "独立性由人工标注与冻结记录确认，程序不能证明样本从未参与调试。",
            "evaluation_status": "scored" if sum(scores[g]["total"] for g in required) else "not_evaluated"}


def main():
    parser = argparse.ArgumentParser(description="按独立人工标签评分，不调用模型")
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = RunResult.model_validate_json(args.result.read_text(encoding="utf-8"))
    report = evaluate(result, json.loads(args.labels.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评分完成：{report['evaluation_status']}。详见 {args.output}")


if __name__ == "__main__":
    main()
