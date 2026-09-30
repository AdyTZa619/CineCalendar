from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v11 import FastRecommendationEngineV11
from cinecalendar.recommender_v16 import FastRecommendationEngineV16


def _movie(*, votes: int, overview: str = "") -> Movie:
    return Movie(
        id=1, imdb_id="tt0000001", title="Film", title_type="movie",
        imdb_rating=9.9, num_votes=votes, overview=overview,
        directors=["Director"], semantic={"drama": 1.0},
    )


def test_cold_start_does_not_trust_tiny_imdb_samples_with_only_genre_tags():
    thin = _movie(votes=396)
    assert not FastRecommendationEngineV11._cold_start_eligible(thin)
    assert FastRecommendationEngineV11._cold_start_eligible(_movie(votes=2500))
    assert FastRecommendationEngineV11._cold_start_eligible(
        _movie(votes=396, overview="A meaningful plot about family and difficult choices.")
    )


def test_visible_trust_gate_flags_thin_title_even_with_high_predicted_rating():
    thin = _movie(votes=396)
    rec = Recommendation(
        thin,
        ScoreBreakdown(predicted_rating=8.1, confidence=0.85, final=0.80),
    )
    flagged, reason = FastRecommendationEngineV16._red_flag(rec)
    assert flagged
    assert "puține voturi" in reason
    assert FastRecommendationEngineV16._red_flag(
        Recommendation(_movie(votes=3000), rec.score)
    )[0] is False
