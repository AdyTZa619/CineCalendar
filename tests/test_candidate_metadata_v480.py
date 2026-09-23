from __future__ import annotations

from pathlib import Path

from cinecalendar.candidate_metadata_v48 import (
    CandidateMetadataPreflight,
    coverage_report,
    ranking_completeness,
    reset_metadata_cache,
)
from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.util import json_dumps, utcnow_iso


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


class _NotFoundTmdbProvider:
    last_status = "not_found"

    def __init__(self, _db, _token):
        pass

    def enrich_by_imdb(self, movie):
        self.last_status = "not_found"
        return movie


class _EmptyOpenProvider:
    last_status = "empty"

    def __init__(self, _db):
        pass

    def enrich_by_imdb(self, movie):
        self.last_status = "empty"
        return movie


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


def test_preflight_is_bounded_to_twelve_titles_and_marks_factual_rerank():
    _FillRankingProvider.calls = 0
    recs = [_rec(index) for index in range(1, 15)]
    attempted: set[int] = set()
    preflight = CandidateMetadataPreflight(
        object(),
        open_factory=_FillRankingProvider,
    )

    report = preflight.run(recs, attempted_ids=attempted, limit=50)

    assert report["attempted"] == 12
    assert report["changed_titles"] == 12
    assert report["ranking_change"] is True
    assert report["io_limit"] == 12
    assert _FillRankingProvider.calls == 12
    assert attempted == set(range(1, 13))
    assert report["after"]["ranking_complete"] == 12


def test_visible_poster_gap_wins_over_sparser_offscreen_candidate():
    complete = dict(
        genres=["Drama"], directors=["Director"], countries=["Romania"],
        overview="Overview", runtime_min=95, poster_url="https://example.test/existing.jpg",
    )
    visible = [_rec(index, **complete) for index in range(1, 13)]
    visible[10].movie.poster_url = None
    offscreen = _rec(13)

    report = CandidateMetadataPreflight(
        object(), open_factory=_PosterOnlyProvider,
    ).run([*visible, offscreen], limit=1)

    assert report["selected_ranks"] == [11]
    assert visible[10].movie.poster_url == "https://example.test/poster.jpg"
    assert offscreen.movie.poster_url is None


def test_preflight_selects_high_impact_gaps_from_the_candidate_pool():
    complete = dict(
        genres=["Drama"], directors=["Director"], countries=["Romania"],
        overview="Overview", runtime_min=95,
    )
    recs = [_rec(index, **complete) for index in range(1, 8)]
    recs.append(_rec(8))

    report = CandidateMetadataPreflight(object(), open_factory=_FillRankingProvider).run(recs, limit=1)

    assert report["selected_ranks"] == [8]
    assert report["attempted"] == 1
    assert recs[7].movie.genres == ["Drama"]


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
    assert report["title_results"][1]["status"] == "error"
    assert "nu a răspuns" in report["title_results"][1]["reason"]


def test_missing_title_is_diagnosed_as_not_found():
    report = CandidateMetadataPreflight(
        object(),
        token="local-token",
        tmdb_factory=_NotFoundTmdbProvider,
        open_factory=_EmptyOpenProvider,
    ).run([_rec(1)])

    result = report["title_results"][1]
    assert result["status"] == "not_found"
    assert result["tmdb"] == "not_found"
    assert result["fallback"] == "empty"
    assert "nu a fost găsit" in result["reason"]


def test_partial_fill_reports_exact_remaining_fields():
    report = CandidateMetadataPreflight(
        object(), open_factory=_PosterOnlyProvider,
    ).run([_rec(1)])

    result = report["title_results"][1]
    assert result["status"] == "partial"
    assert "poster" not in result["missing"]
    assert result["missing"] == ["genuri", "regizor", "țară", "descriere", "durată"]


def test_explicit_retry_clears_only_selected_movie_provider_cache(tmp_path):
    db = Database(tmp_path / "retry.db")
    now = utcnow_iso()
    with db.tx() as con:
        movie_ids = []
        for suffix in (1, 2):
            imdb_id = f"tt900000{suffix}"
            cur = con.execute(
                """INSERT INTO movies(
                    imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                    year,title_type,genres_json,directors_json,countries_json,overview,
                    keywords_json,source,created_at,updated_at,tmdb_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    imdb_id, f"movie|retry-{suffix}", f"Retry {suffix}", "",
                    f"retry {suffix}", "", 2024, "movie", "[]", "[]", "[]", "",
                    "[]", "test", now, now, 800 + suffix,
                ),
            )
            movie_ids.append(int(cur.lastrowid))
            for provider, key in (
                ("tmdb", f'/find/{imdb_id}?{{"external_source": "imdb_id"}}'),
                ("tmdb", f'/movie/{800 + suffix}?{{"language": "ro-RO"}}'),
                ("wikimedia", f"movie:v2:{imdb_id}"),
            ):
                con.execute(
                    "INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at) VALUES(?,?,?,?)",
                    (provider, key, json_dumps({}), now),
                )

    assert reset_metadata_cache(db, [movie_ids[0]]) == 3
    with db.connect() as con:
        remaining = con.execute(
            "SELECT provider,cache_key FROM metadata_cache ORDER BY provider,cache_key"
        ).fetchall()
    assert len(remaining) == 3
    assert all("tt9000002" in row["cache_key"] or "/movie/802?" in row["cache_key"] for row in remaining)


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
    assert "PREFLIGHT_POOL_SIZE" in source
    assert 'choose=QPushButton("Aleg filmul")' in source
    assert "ResponsiveRecommendationGrid" in source
    assert "reason.setMaximumHeight(58)" not in source
    assert 'QPushButton(f"Reîncearcă doar lipsurile ({len(retryable)})")' in source
    assert "reset_metadata_cache(self.db,ids)" in source
    assert 'QLabel("Date: "+str(result.get("reason")' in source
