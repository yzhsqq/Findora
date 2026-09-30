import pytest

from scripts.eval.compare_cj_baseline import compare
from scripts.eval.run_cj_baseline import summarize


def test_known_item_and_empty_cases_have_separate_denominators():
    observations = [
        {"kind": "lexical", "expected_empty": False, "pass": True, "rank": 2,
         "failure_type": None, "latency_ms": 10.0},
        {"kind": "paraphrase", "expected_empty": False, "pass": False, "rank": None,
         "failure_type": "no_candidates", "latency_ms": 20.0},
        {"kind": "empty", "expected_empty": True, "pass": True, "rank": None,
         "failure_type": None, "latency_ms": 30.0},
    ]

    result = summarize(observations)

    assert result["known_item_hit_at_5"] == 0.5
    assert result["known_item_mrr"] == 0.25
    assert result["empty_accuracy"] == 1.0
    assert result["failure_types"] == {"no_candidates": 1}


def test_comparison_rejects_changed_snapshot_before_claiming_improvement():
    observation = {"id": "case-1", "kind": "exact", "expected_empty": False,
                   "anchor_id": "product-1", "pass": False, "rank": None, "retrieved": []}
    report = {
        "split": "all", "cases_sha256": "cases", "top_k": 5,
        "snapshot": {"sha256": "frozen-a"}, "observations": [observation],
    }
    changed = {**report, "snapshot": {"sha256": "frozen-b"}}

    with pytest.raises(ValueError, match="frozen CJ snapshot"):
        compare(report, changed)
