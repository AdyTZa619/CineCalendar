from __future__ import annotations

from pathlib import Path

from cinecalendar.candidate_metadata_v48 import (
    CandidateMetadataPreflight,
    coverage_report,
    ranking_completeness,
)
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown


def _rec(index: int, **movie_values) -> Recommendation:
    movie = Movie(
        id=index,
        imdb_id=f"tt{index:07d}",
        title=f"Movie {index}",
        year=2020,
        **movie_values,
    )
    return Recommendation(movie, ScoreBreakdown(final=.75))


class _FillRankingProvider:
    calls = 0

    def __init__(self, _db):
        pass

    def enrich_by_imdb(self, movie):
        type(self).calls += 1
        movie.genres = movie.genres or ["Drama"]
        movie.directors = movie.directors or ["Director"]
        movie.countries = movie.countries or ["Romania"]
        movie.overview = movie.overview or "A factual overview."
        movie.runtime_min = movie.runtime_min or 100
        movie.poster_url = movie.poster_url or "https://example.test/poster.jpg"
        return movie


class _PosterOnlyProvider:
    def __init__(self, _db):
        pass

    def enrich_by_imdb(self, movie):
        movie.poster_url = "https://example.test/poster.jpg"
        return movie


class _FailingTmdbProvider:
    def __init__(self, _db, _token):
        pass

    def enrich_by_imdb(self, _movie):
        raise RuntimeError("temporary TMDb failure")


class _FailingOpenProvider:
    def __init__(self, _db):
        pass

    def enrich_by_imdb(self, _movie):
        raise RuntimeError("temporary Wikimedia failure")


def test_coverage_is_explicit_and_does_not_invent_completeness():
    full = _rec(
        1,
        genres=["Drama"],
        directors=["Director"],
        countries=["Romania"],
        overview="Overview",
        runtime_min=95,
        poster_url="poster",
    )
    sparse = _rec(2, genres=["Drama"])

    report = coverage_report([full, sparse])

    assert ranking_completeness(full.movie) == 1.0
    assert ranking_completeness(sparse.movie) == .2
    assert report == {
        "total": 2,
        "ranking_complete": 1,
        "average_percent": 60,
        "poster_complete": 1,
        "missing": {
            "genres": 0,
            "directors": 1,
            "countries": 1,
            "semantic_text": 1,
            "runtime": 1,
            "poster": 1,
        },
    }


def test_preflight_is_bounded_to_six_titles_and_marks_factual_rerank():
    _FillRankingProvider.calls = 0
    recs = [_rec(index) for index in range(1, 10)]
    attempted: set[int] = set()
    preflight = CandidateMetadataPreflight(
        object(),
        open_factory=_FillRankingProvider,
    )

    report = preflight.run(recs, attempted_ids=attempted, limit=50)

    assert report["attempted"] == 6
    assert report["changed_titles"] == 6
    assert report["ranking_change"] is True
    assert report["io_limit"] == 6
    assert _FillRankingProvider.calls == 6
    assert attempted == {1, 2, 3, 4, 5, 6}
    assert report["after"]["ranking_complete"] == 6


def test_visual_only_metadata_never_requests_a_rerank():
    rec = _rec(
        1,
        genres=["Drama"],
        directors=["Director"],
        countries=["Romania"],
        overview="Overview",
        runtime_min=95,
    )
    report = CandidateMetadataPreflight(
        object(),
        open_factory=_PosterOnlyProvider,
    ).run([rec])

    assert report["visual_added"] == 1
    assert report["ranking_fields_added"] == {}
    assert report["ranking_change"] is False


def test_wikimedia_fallback_still_runs_when_tmdb_fails():
    rec = _rec(1)
    report = CandidateMetadataPreflight(
        object(),
        token="local-token",
        tmdb_factory=_FailingTmdbProvider,
        open_factory=_FillRankingProvider,
    ).run([rec])

    assert report["ranking_change"] is True
    assert report["after"]["ranking_complete"] == 1
    assert report["failed"] == 0


def test_provider_failure_is_reported_instead_of_claiming_completed():
    rec = _rec(1)
    report = CandidateMetadataPreflight(
        object(),
        token="local-token",
        tmdb_factory=_FailingTmdbProvider,
        open_factory=_FailingOpenProvider,
    ).run([rec])

    assert report["state"] == "failed"
    assert report["attempted"] == 1
    assert report["failed"] == 1
    assert report["changed_titles"] == 0
    assert report["ranking_change"] is False


def test_recommendations_ui_exposes_coverage_and_reranks_only_after_new_facts():
    source = (Path(__file__).parents[1] / "cinecalendar" / "premium_ui.py").read_text(encoding="utf-8")
    assert "DATELE RECOMANDĂRILOR" in source
    assert "verificări per listă" in source
    assert 'if report.get("ranking_change"):' in source
    assert 'report["reranked"]=True' in source
    assert "Recalculez ordinea cu metadatele factuale noi" in source
    assert 'elif state == "partial":' in source
    success = source.split("def success(recs):", 1)[1].split("def failure(message):", 1)[0]
    assert "_ensure_metadata" in success
    assert "_render_browse" not in success
