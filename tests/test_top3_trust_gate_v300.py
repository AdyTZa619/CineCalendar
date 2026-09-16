from __future__ import annotations

from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.service import CineCalendarService


class _Collaborative:
    def __init__(self, scores=None, ready=True):
        self.scores = dict(scores or {})
        self.ready = ready

    def is_ready(self):
        return self.ready

    def score_candidates(self, imdb_ids):
        return ({iid: self.scores[iid] for iid in imdb_ids if iid in self.scores}, {}, 500)


def _engine(scores=None, ready=True):
    engine = object.__new__(FastRecommendationEngineV16)
    engine.collaborative = _Collaborative(scores=scores, ready=ready)
    engine._quality_gate_stats = {}
    return engine


def _rec(
    idx: int,
    *,
    final: float,
    predicted: float,
    confidence: float,
    evidence: float = 2.5,
    startability: float = 0.68,
    imdb_rating: float = 7.0,
    votes: int = 20_000,
):
    return Recommendation(
        movie=Movie(
            id=idx,
            imdb_id=f"tt{8_800_000 + idx:07d}",
            title=f"Movie {idx}",
            year=2020,
            title_type="movie",
            runtime_min=105,
            genres=["Drama"],
            directors=["Director"],
            imdb_rating=imdb_rating,
            num_votes=votes,
        ),
        score=ScoreBreakdown(
            final=final,
            predicted_rating=predicted,
            confidence=confidence,
            evidence=evidence,
            startability=startability,
            personal_reason="base",
        ),
    )


def test_gate_prefers_multi_signal_trust_over_slightly_higher_weak_candidate():
    weak = _rec(1, final=0.80, predicted=6.0, confidence=0.35, evidence=0.2, startability=0.52, imdb_rating=6.0, votes=300)
    trusted = _rec(2, final=0.79, predicted=8.2, confidence=0.82, evidence=4.0, startability=0.74, imdb_rating=7.4, votes=80_000)
    third = _rec(3, final=0.77, predicted=7.8, confidence=0.72, evidence=3.2, startability=0.66, imdb_rating=7.1, votes=30_000)
    engine = _engine({trusted.movie.imdb_id: 0.94, third.movie.imdb_id: 0.88, weak.movie.imdb_id: 0.40})

    selected = engine._quality_gate([weak, trusted, third], 2)

    assert [rec.movie.id for rec in selected] == [2, 3]
    assert selected[0].score.predicted_rating == 8.2
    assert engine.quality_gate_status()["trusted"] >= 2


def test_gate_demotes_clear_public_or_personal_red_flags_until_last_resort():
    public_bad = _rec(1, final=0.82, predicted=7.0, confidence=0.55, imdb_rating=4.9, votes=50_000)
    personal_bad = _rec(2, final=0.81, predicted=5.2, confidence=0.90, imdb_rating=7.0, votes=50_000)
    safe = _rec(3, final=0.78, predicted=7.5, confidence=0.70, imdb_rating=6.9, votes=15_000)
    engine = _engine({safe.movie.imdb_id: 0.86})

    selected = engine._quality_gate([public_bad, personal_bad, safe], 2)

    assert selected[0].movie.id == 3
    assert engine.quality_gate_status()["red_flags"] == 2
    assert len(selected) == 2


def test_gate_does_not_call_a_far_weaker_candidate_trusted_even_with_good_metadata():
    best = _rec(1, final=0.88, predicted=8.4, confidence=0.82)
    far = _rec(2, final=0.69, predicted=9.1, confidence=0.95, evidence=8.0, startability=0.90, imdb_rating=8.0, votes=200_000)
    engine = _engine({best.movie.imdb_id: 0.90, far.movie.imdb_id: 0.99})

    payload = engine._trust_payload(far, best_final=best.score.final, als_score=0.99)

    assert payload["gap"] > engine.TRUST_RELATIVE_MARGIN
    assert payload["trusted"] is False


def test_gate_preserves_requested_count_with_conservative_backfill():
    items = [
        _rec(1, final=0.78, predicted=7.2, confidence=0.60),
        _rec(2, final=0.76, predicted=6.6, confidence=0.48, evidence=0.8, startability=0.55),
        _rec(3, final=0.74, predicted=6.5, confidence=0.45, evidence=0.6, startability=0.53),
    ]
    engine = _engine(ready=False)

    selected = engine._quality_gate(items, 3)

    assert len(selected) == 3
    assert engine.quality_gate_status()["returned"] == 3
    assert engine.quality_gate_status()["fallback"] >= 1


def test_production_service_uses_v16_top3_gate():
    source_names = CineCalendarService.__init__.__code__.co_names
    assert "FastRecommendationEngineV16" in source_names
