from cinecalendar.v5_personal_ranker import PersonalUtilityRankerV5
from cinecalendar.v5_retrieval import UnifiedCandidateRetrieverV5


def test_v5_fusion_is_non_destructive_and_prefers_consensus():
    baseline = [1, 2, 3, 4]
    lanes = {
        "als": [5, 6, 7],
        "favorites": [5, 8],
        "local_content": [5, 9],
        "catalog_content": [10, 5],
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
        {"als": [40], "catalog_content": [50]},
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


def test_v5_ranker_state_token_changes_after_rated_metadata_enrichment(tmp_path):
    from cinecalendar.db import Database
    from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso

    db=Database(tmp_path/"cinecalendar.db")
    now=utcnow_iso()
    with db.tx() as con:
        cur=con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,countries_json,overview,
                keywords_json,semantic_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt8999999",identity_key("Rated metadata","Rated metadata",2020,"movie"),
                "Rated metadata","Rated metadata",normalize_text("Rated metadata"),
                normalize_text("Rated metadata"),2020,"movie",
                json_dumps(["Drama"]),json_dumps(["Director"]),json_dumps([]),"",
                json_dumps([]),json_dumps({}),"test",now,now,
            ),
        )
        movie_id=int(cur.lastrowid)
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id,8,"2026-01-01","test",now,now),
        )

    ranker=PersonalUtilityRankerV5(db)
    before=ranker.state_token()
    with db.tx() as con:
        con.execute(
            "UPDATE movies SET overview=?,updated_at=? WHERE id=?",
            ("New premise","2099-01-01T00:00:00+00:00",movie_id),
        )
    after=ranker.state_token()
    assert after != before
    assert after[-1] == "2099-01-01T00:00:00+00:00"


def test_discovery_consensus_keeps_strong_consensus_and_personal_singles_only():
    baseline = [1, 2]
    lanes = {
        "als": [10, 11, 12],
        "favorites": [10, 20],
        "local_content": [10, 30],
        "catalog_content": [40],
        "online_discovery": [50],
    }
    merged, evidence = UnifiedCandidateRetrieverV5.fuse(
        baseline,
        lanes,
        extra_limit=10,
        minimum_support=2,
        trusted_single_sources=("als", "favorites"),
        trusted_single_rank_limit=2,
    )
    added = merged[len(baseline):]
    assert 10 in added
    assert 11 in added  # top personal ALS signal
    assert 20 in added  # top personal favourite-neighbour signal
    assert 30 not in added
    assert 40 not in added
    assert 50 not in added
    assert all(item.support_count >= 2 or set(item.sources) & {"als", "favorites"} for item in evidence)
