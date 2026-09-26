from cinecalendar.v5_personal_ranker import PersonalUtilityRankerV5
from cinecalendar.v5_retrieval import UnifiedCandidateRetrieverV5


def test_v5_fusion_is_non_destructive_and_prefers_consensus():
    baseline = [1, 2, 3, 4]
    lanes = {
        "als": [5, 6, 7],
        "favorites": [5, 8],
        "local_content": [5, 9],
        "full_catalog": [10, 5],
    }
    merged, evidence = UnifiedCandidateRetrieverV5.fuse(baseline, lanes, extra_limit=3)
    assert merged[:4] == baseline
    assert len(set(merged)) == len(merged)
    assert merged[4] == 5
    assert evidence[0].support_count == 4


def test_v5_fusion_never_removes_baseline_when_extra_budget_zero():
    baseline = [10, 20, 30]
    merged, evidence = UnifiedCandidateRetrieverV5.fuse(
        baseline,
        {"als": [40], "full_catalog": [50]},
        extra_limit=0,
    )
    assert merged == baseline
    assert evidence == []


def test_v5_auc_and_ndcg_helpers_reward_correct_order():
    auc = PersonalUtilityRankerV5._auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1])
    assert auc == 1.0
    good = PersonalUtilityRankerV5._ndcg25([10, 9, 4, 2], [0.9, 0.8, 0.2, 0.1])
    bad = PersonalUtilityRankerV5._ndcg25([10, 9, 4, 2], [0.1, 0.2, 0.8, 0.9])
    assert good > bad


def test_availability_wrapper_is_idempotent_for_v5_lab():
    from cinecalendar.availability_guard_v37 import availability_engine_class
    from cinecalendar.v5_lab import V5LabRecommendationEngine

    assert availability_engine_class(V5LabRecommendationEngine) is V5LabRecommendationEngine
