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
