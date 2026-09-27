from datetime import date

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.v5_visible_trial import (
    AlphaTrialRecommender,
    MODE_V16,
    MODE_V5_20,
)


class FakeEngine:
    def __init__(self, recs):
        self.recs = list(recs)
        self.recommend_calls = 0
        self.decision_calls = 0
        self.romanian_calls = 0

    def recommend(self, **kwargs):
        self.recommend_calls += 1
        count = int(kwargs.get("count", 3))
        return list(self.recs[:count])

    def recommend_romanian(self, when=None, count=9):
        self.romanian_calls += 1
        return list(self.recs[: int(count)])

    def decision_pick(self, when=None, exclude_ids=None, mode="decide", **kwargs):
        self.decision_calls += 1
        recs = list(self.recs[:3])
        return (recs[0] if recs else None, recs[1:3])


def _rec(mid: int, final: float):
    return Recommendation(
        Movie(id=mid, imdb_id=f"tt{mid:07d}", title=f"Movie {mid}"),
        ScoreBreakdown(
            final=final,
            predicted_rating=7.0 + final,
            confidence=0.8,
            score_factors={},
        ),
    )


def _eligible(db):
    db.set_setting(
        "v5_evaluation_report",
        {
            "decision": {
                "eligible_for_visible_alpha_trial": True,
                "selected_variant": "20%",
            }
        },
    )


def test_alpha_trial_defaults_to_v5_when_latest_report_is_eligible(tmp_path):
    db = Database(tmp_path / "trial.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.70), _rec(2, 0.69)])
    v5 = FakeEngine([_rec(2, 0.78), _rec(1, 0.71)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    assert proxy.trial_status()["mode"] == MODE_V5_20
    recs = proxy.recommend(when=date(2026, 9, 27), count=2, slot="browse")
    assert [r.movie.id for r in recs] == [2, 1]
    assert recs[0].score.score_factors["v5_trial_active"] == 1.0
    assert recs[0].score.score_factors["v5_trial_v16_rank"] == 2.0
    assert recs[0].score.score_factors["v5_trial_v5_rank"] == 1.0

    with db.connect() as con:
        rows = con.execute(
            "SELECT active_mode,movie_id,v16_rank,v5_rank,v16_final,v5_final FROM v5_trial_audit ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["active_mode"] == MODE_V5_20
    assert int(rows[0]["movie_id"]) == 2
    assert int(rows[0]["v16_rank"]) == 2
    assert int(rows[0]["v5_rank"]) == 1


def test_alpha_trial_switches_to_v16_instantly(tmp_path):
    db = Database(tmp_path / "trial-switch.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.75), _rec(2, 0.70)])
    v5 = FakeEngine([_rec(2, 0.80), _rec(1, 0.72)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    status = proxy.set_mode(MODE_V16)
    assert status["mode"] == MODE_V16
    recs = proxy.recommend(when=date(2026, 9, 27), count=2)
    assert [r.movie.id for r in recs] == [1, 2]
    assert recs[0].score.score_factors["v5_trial_active"] == 0.0


def test_alpha_trial_refuses_v5_without_eligible_report(tmp_path):
    db = Database(tmp_path / "trial-block.db")
    v16 = FakeEngine([_rec(1, 0.75)])
    v5 = FakeEngine([_rec(1, 0.80)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    assert proxy.trial_status()["mode"] == MODE_V16
    try:
        proxy.set_mode(MODE_V5_20)
    except RuntimeError:
        pass
    else:
        raise AssertionError("V5 trial should stay blocked without an eligible report")


def test_model_switch_reuses_the_exact_same_frozen_pair(tmp_path):
    db = Database(tmp_path / "trial-frozen.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.75), _rec(2, 0.70), _rec(3, 0.68)])
    v5 = FakeEngine([_rec(2, 0.80), _rec(1, 0.72), _rec(3, 0.69)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    # First exposure computes both sides once.
    first = proxy.recommend(
        when=date(2026, 9, 27), count=3, slot="browse", candidate_limit=45000
    )
    assert [r.movie.id for r in first] == [2, 1, 3]
    assert v16.recommend_calls == 1
    assert v5.recommend_calls == 1

    with db.connect() as con:
        first_round = con.execute(
            "SELECT round_id FROM v5_trial_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()["round_id"]

    # Switching to V16 must not run either engine again.
    proxy.set_mode(MODE_V16)
    second = proxy.recommend(
        when=date(2026, 9, 27), count=3, slot="browse", candidate_limit=45000
    )
    assert [r.movie.id for r in second] == [1, 2, 3]
    assert v16.recommend_calls == 1
    assert v5.recommend_calls == 1

    with db.connect() as con:
        second_round = con.execute(
            "SELECT round_id FROM v5_trial_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()["round_id"]
    assert second_round == first_round


def test_explicit_invalidation_forces_a_fresh_pair(tmp_path):
    db = Database(tmp_path / "trial-recalc.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.75), _rec(2, 0.70)])
    v5 = FakeEngine([_rec(2, 0.80), _rec(1, 0.72)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    proxy.recommend(
        when=date(2026, 9, 27), count=2, slot="browse", candidate_limit=45000
    )
    proxy.set_mode(MODE_V16)
    proxy.invalidate_round("browse")
    proxy.recommend(
        when=date(2026, 9, 27), count=2, slot="browse", candidate_limit=45000
    )

    assert v16.recommend_calls == 2
    assert v5.recommend_calls == 2



def test_romanian_lane_uses_same_frozen_v16_v5_pair(tmp_path):
    db = Database(tmp_path / "trial-romanian.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.76), _rec(2, 0.72), _rec(3, 0.68)])
    v5 = FakeEngine([_rec(2, 0.81), _rec(1, 0.74), _rec(3, 0.69)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    first = proxy.recommend_romanian(when=date(2026, 9, 28), count=3)
    assert [r.movie.id for r in first] == [2, 1, 3]
    assert v16.romanian_calls == 1
    assert v5.romanian_calls == 1
    assert first[0].score.score_factors["v5_trial_v16_rank"] == 2.0
    assert first[0].score.score_factors["v5_trial_v5_rank"] == 1.0

    proxy.set_mode(MODE_V16)
    second = proxy.recommend_romanian(when=date(2026, 9, 28), count=3)
    assert [r.movie.id for r in second] == [1, 2, 3]
    assert v16.romanian_calls == 1
    assert v5.romanian_calls == 1

    with db.connect() as con:
        rounds = con.execute(
            "SELECT DISTINCT round_id FROM v5_trial_audit WHERE slot='romanian'"
        ).fetchall()
    assert len(rounds) == 1
