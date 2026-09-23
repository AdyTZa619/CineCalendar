from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.candidate_metadata_v48 import CandidateMetadataPreflight
from cinecalendar.metadata_provenance import metadata_sources_for_movie, record_metadata_source
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.premium_ui import PremiumDecisionWindow
from cinecalendar.qt_ui import CineCalendarWindow
from cinecalendar.tmdb import TmdbProvider
from cinecalendar.util import json_dumps, utcnow_iso
from cinecalendar.watch_success_ui_patch import structured_watch_reason


def _insert_movie(db: Database, overview: str = "") -> int:
    stamp = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,poster_url,semantic_json,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt0123456", "movie|film|2000|movie", "Film", "Film", "film", "film",
                2000, "movie", 121, '["Drama"]', '["Regizor"]', '["United States of America"]',
                overview, "[]", "https://example.test/poster.jpg", json_dumps({"theme:test": .7}),
                "test", stamp, stamp,
            ),
        )
        return int(cur.lastrowid)


def _movie(mid: int, overview: str = "") -> Movie:
    return Movie(
        id=mid, imdb_id="tt0123456", title="Film", year=2000, runtime_min=121,
        genres=["Drama"], directors=["Regizor"], countries=["United States of America"],
        overview=overview, poster_url="https://example.test/poster.jpg",
        semantic={"theme:test": .7}, imdb_rating=7.2,
    )


def _fake_tmdb(provider: TmdbProvider, *, romanian: str, english: str = "English overview") -> None:
    def fake_get(path, params=None, cache_hours=168):
        if path.startswith("/find/"):
            return {"movie_results": [{"id": 99}]}
        language = (params or {}).get("language")
        return {
            "overview": romanian if language == "ro-RO" else english,
            "runtime": 121,
            "production_countries": [{"name": "United States of America"}],
            "genres": [{"name": "Drama"}],
            "credits": {"crew": [{"job": "Director", "name": "Regizor"}]},
            "keywords": {"keywords": []},
            "images": {"posters": []},
            "poster_path": "/poster.jpg",
        }

    provider._get = fake_get


def test_structured_reason_uses_one_final_score_and_labels_low_intent():
    score = ScoreBreakdown(
        predicted_rating=7.1, confidence=.95, startability=.58,
        contributions=[(
            "Intenție de vizionare acum", 0.0,
            "Semnal de intenție de vizionare scăzut; folosit doar la departajare.",
        )],
    )
    text = structured_watch_reason(Recommendation(_movie(1, "Premisă"), score))

    assert text.splitlines() == [
        "Potrivire: 7.1/10 estimare finală • 95% încredere.",
        "Pentru acum: ușurință medie — 121 min, premisă disponibilă, IMDb solid.",
        "Rezervă: intenția recentă de pornire este scăzută; contează doar la departajare.",
    ]
    assert "7.5" not in text


def test_tmdb_upgrades_its_english_overview_to_romanian_without_changing_semantics(tmp_path):
    db = Database(tmp_path / "localize.db")
    mid = _insert_movie(db, "English overview")
    with db.tx() as con:
        record_metadata_source(con, mid, "overview", "tmdb")
    provider = TmdbProvider(db, "token")
    _fake_tmdb(provider, romanian="Descriere în română")
    movie = _movie(mid, "English overview")

    provider.enrich_by_imdb(movie)

    assert movie.overview == "Descriere în română"
    assert movie.semantic == {"theme:test": .7}
    assert metadata_sources_for_movie(db, mid)["overview"] == "tmdb-ro"


def test_tmdb_uses_english_fallback_and_records_it(tmp_path):
    db = Database(tmp_path / "fallback.db")
    mid = _insert_movie(db)
    provider = TmdbProvider(db, "token")
    _fake_tmdb(provider, romanian="", english="English fallback")
    movie = _movie(mid)

    provider.enrich_by_imdb(movie)

    assert movie.overview == "English fallback"
    assert metadata_sources_for_movie(db, mid)["overview"] == "tmdb-en"


def test_tmdb_does_not_replace_an_overview_owned_by_another_source(tmp_path):
    db = Database(tmp_path / "source-safe.db")
    mid = _insert_movie(db, "Descriere Wikipedia")
    with db.tx() as con:
        record_metadata_source(con, mid, "overview", "wikimedia")
    provider = TmdbProvider(db, "token")
    _fake_tmdb(provider, romanian="Descriere TMDb")
    movie = _movie(mid, "Descriere Wikipedia")

    provider.enrich_by_imdb(movie)

    assert movie.overview == "Descriere Wikipedia"
    assert metadata_sources_for_movie(db, mid)["overview"] == "wikimedia"


def test_preflight_localizes_a_complete_tmdb_title_without_requesting_a_rerank(tmp_path):
    db = Database(tmp_path / "preflight-localize.db")
    mid = _insert_movie(db, "English overview")
    with db.tx() as con:
        record_metadata_source(con, mid, "overview", "tmdb-en")
    rec = Recommendation(_movie(mid, "English overview"), ScoreBreakdown(final=.8))

    class LocalizingTmdb:
        last_status = "success"

        def __init__(self, _db, _token, request_timeout=None):
            pass

        def enrich_by_imdb(self, movie):
            movie.overview = "Descriere în română"
            return movie

    class NoopOpen:
        def __init__(self, _db, **_kwargs):
            pass

        def enrich_by_imdb(self, movie):
            return movie

    report = CandidateMetadataPreflight(
        db, "token", tmdb_factory=LocalizingTmdb, open_factory=NoopOpen,
    ).run([rec])

    assert rec.movie.overview == "Descriere în română"
    assert report["changed_titles"] == 1
    assert report["selected_ranks"] == [1]
    assert report["ranking_change"] is False


def test_country_and_metadata_sources_are_localized_for_the_ui():
    assert PremiumDecisionWindow.localized_country("United States of America") == "SUA"
    assert PremiumDecisionWindow.localized_country("Romania") == "România"
    assert PremiumDecisionWindow.metadata_source_text({
        "overview": "tmdb-ro", "poster_url": "tmdb",
    }) == "Surse • descriere: TMDb (română) • poster: TMDb • cache local"


class _SettingsDb:
    def __init__(self):
        self.values = {"tmdb_token": "old"}

    def get_setting(self, name, default=None):
        return self.values.get(name, default)

    def set_setting(self, name, value):
        self.values[name] = value


def test_saving_a_new_tmdb_token_activates_it_and_reopens_metadata_attempts():
    class Window:
        db = _SettingsDb()
        metadata_attempted = {10, 20}
        status = ""

        def set_status(self, message, _busy):
            self.status = message

    window = Window()

    assert CineCalendarWindow.save_tmdb_token(window, " new-token ", validated=True)
    assert window.db.values["tmdb_token"] == "new-token"
    assert window.metadata_attempted == set()
    assert "activ imediat" in window.status
