from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import requests

from cinecalendar import app as app_module
from cinecalendar import ui_composition as ui_composition_module
from cinecalendar.db import Database
from cinecalendar.recommender_v12 import FastRecommendationEngineV12
from cinecalendar.romanian_cinema import CACHE_KEY, RomanianCinemaProvider
from cinecalendar.romanian_cinema_ui_patch import install_romanian_cinema_ui_patch
from cinecalendar.util import json_dumps, utcnow_iso


def _insert_movie(db: Database, imdb_id: str, title: str, countries=None) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                 imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                 genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                 imdb_rating,num_votes,release_date,poster_url,source,created_at,updated_at,
                 title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, f"{imdb_id}:movie", title, title, 2020, "movie", 100,
                json_dumps(["Drama"]), json_dumps([]), json_dumps(countries or []), "",
                json_dumps([]), json_dumps({}), 7.0, 1000, None, None, "test", now, now,
                title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def test_wikidata_parser_accepts_only_title_imdb_ids():
    payload = {
        "results": {
            "bindings": [
                {"imdb": {"value": "tt1234567"}},
                {"imdb": {"value": "nm1234567"}},
                {"imdb": {"value": "tt7654321"}},
                {"imdb": {"value": "bad"}},
            ]
        }
    }
    assert RomanianCinemaProvider._extract_wikidata_ids(payload) == {"tt1234567", "tt7654321"}


def test_wikidata_query_requires_romanian_original_language_and_romania_origin():
    query = RomanianCinemaProvider.wikidata_query()
    assert "wdt:P364 wd:Q7913" in query
    assert "wdt:P495 wd:Q218" in query
    assert "wdt:P31/wdt:P279* wd:Q11424" in query
    assert "UNION" not in query
    assert "FILTER NOT EXISTS" not in query


def test_new_cache_key_invalidates_single_source_results():
    assert CACHE_KEY.endswith("v5")
    assert "multisource" in CACHE_KEY


def test_country_only_local_metadata_never_qualifies_without_language_verification(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    _insert_movie(db, "tt0000001", "Film RO", ["România"])
    _insert_movie(db, "tt0000002", "Film US", ["United States"])
    _insert_movie(db, "tt0000003", "Coproducție", ["Romania", "France"])
    provider = RomanianCinemaProvider(db)
    assert provider.local_imdb_ids() == set()


def test_language_verified_cache_is_reused_when_all_live_sources_are_down(tmp_path, monkeypatch):
    db = Database(tmp_path / "cinecalendar.db")
    provider = RomanianCinemaProvider(db)
    provider._store({"tt0000101", "tt0000102"}, days=-1)

    def fail():
        raise requests.RequestException("offline")

    monkeypatch.setattr(provider, "_fetch_wikidata_ids", fail)
    monkeypatch.setattr(provider, "_fetch_imdb_ids", fail)
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    assert provider.imdb_ids(refresh=True) == {"tt0000101", "tt0000102"}
    assert provider.status()["source"] == "romanian-multisource-stale-cache"


def test_without_verified_language_data_provider_fails_closed(tmp_path, monkeypatch):
    db = Database(tmp_path / "cinecalendar.db")
    _insert_movie(db, "tt0000201", "Țară RO dar limbă necunoscută", ["Romania"])
    provider = RomanianCinemaProvider(db)

    def fail():
        raise requests.RequestException("offline")

    monkeypatch.setattr(provider, "_fetch_wikidata_ids", fail)
    monkeypatch.setattr(provider, "_fetch_imdb_ids", fail)
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    assert provider.imdb_ids(refresh=True) == set()
    assert provider.status()["source"] == "language-unverified-empty"
    assert provider.status()["local_count"] == 1


def test_imdb_query_requires_romanian_language_and_romania_origin():
    query = RomanianCinemaProvider.imdb_query(2000, 2010)
    assert 'originCountryConstraint: {anyCountries:["RO"]}' in query
    assert 'languageConstraint: {anyLanguages:["ro"]}' in query


def test_imdb_discovery_splits_saturated_date_windows(tmp_path, monkeypatch):
    db = Database(tmp_path / "discovery.db")
    provider = RomanianCinemaProvider(db)
    queries = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def post(_url, *, json, **_kwargs):
        query = json["query"]
        queries.append(query)
        if 'start:"2000-01-01" end:"2019-12-31"' in query:
            edges = [{"node": {"title": {"id": f"tt{i:07d}"}}} for i in range(999)]
        elif 'start:"2000-01-01" end:"2009-12-31"' in query:
            edges = [{"node": {"title": {"id": "tt9990001"}}}]
        elif 'start:"2010-01-01" end:"2019-12-31"' in query:
            edges = [{"node": {"title": {"id": "tt9990002"}}}]
        else:
            edges = []
        return Response({"data": {"advancedTitleSearch": {"edges": edges}}})

    monkeypatch.setattr(provider.session, "post", post)
    assert provider._fetch_imdb_ids() == {"tt9990001", "tt9990002"}
    assert any('start:"2000-01-01" end:"2009-12-31"' in q for q in queries)
    assert any('start:"2010-01-01" end:"2019-12-31"' in q for q in queries)


def test_multisource_verification_unions_independent_strong_sources(tmp_path, monkeypatch):
    db = Database(tmp_path / "cinecalendar.db")
    _insert_movie(db, "tt0000304", "Suport local", ["Romania"])
    provider = RomanianCinemaProvider(db)

    monkeypatch.setattr(provider, "_fetch_wikidata_ids", lambda: {"tt0000301", "tt0000302"})
    monkeypatch.setattr(provider, "_fetch_imdb_ids", lambda: {"tt0000302", "tt0000303"})
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: {"tt0000305"})
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: {"tt0000306"})

    assert provider.imdb_ids(refresh=True) == {
        "tt0000301", "tt0000302", "tt0000305", "tt0000306"
    }
    assert provider.evidence_for("tt0000303")["decision"] == "needs-original-language"
    status = provider.status()
    assert status["source"] == "romanian-multisource-verified"
    assert status["source_counts"] == {
        "wikidata": 2,
        "imdb": 2,
        "curated": 1,
        "tmdb": 1,
    }
    assert status["local_count"] == 1
    assert "tt0000304" not in provider.imdb_ids()


def test_evidence_counts_independent_sources_and_survives_cache(tmp_path, monkeypatch):
    db = Database(tmp_path / "evidence.db")
    provider = RomanianCinemaProvider(db)
    monkeypatch.setattr(provider, "_fetch_wikidata_ids", lambda: {"tt0000401"})
    monkeypatch.setattr(provider, "_fetch_imdb_ids", lambda: {"tt0000401", "tt0000402"})
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    assert provider.imdb_ids(refresh=True) == {"tt0000401"}
    assert provider.evidence_for("tt0000401")["source_count"] == 2
    assert provider.evidence_for("tt0000402")["source_count"] == 1
    assert RomanianCinemaProvider(db).evidence_for("tt0000401")["sources"] == ["imdb", "wikidata"]


def test_explicit_tmdb_conflicts_hold_titles_but_missing_details_do_not(tmp_path, monkeypatch):
    db = Database(tmp_path / "conflicts.db")
    ids = ["tt0000501", "tt0000502", "tt0000503", "tt0000504"]
    for i, imdb_id in enumerate(ids, start=501):
        _insert_movie(db, imdb_id, f"Film {i}")
        with db.tx() as con:
            con.execute("UPDATE movies SET tmdb_id=? WHERE imdb_id=?", (i, imdb_id))
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    details = {
        501: {"id": 501, "original_language": "en", "production_countries": [{"iso_3166_1": "RO"}]},
        502: {"id": 502, "original_language": "ro", "production_countries": [{"iso_3166_1": "FR"}]},
        503: {"id": 503, "external_ids": {"imdb_id": "tt9999999"}, "original_language": "ro", "production_countries": [{"iso_3166_1": "RO"}]},
    }
    with db.tx() as con:
        for tmdb_id, payload in details.items():
            con.execute(
                "INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at) VALUES(?,?,?,?,?)",
                ("tmdb", f'/movie/{tmdb_id}?{{"language": "ro-RO"}}', json_dumps(payload), utcnow_iso(), future),
            )
    provider = RomanianCinemaProvider(db)
    monkeypatch.setattr(provider, "_fetch_wikidata_ids", lambda: set(ids))
    monkeypatch.setattr(provider, "_fetch_imdb_ids", lambda: set(ids))
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    assert provider.imdb_ids(refresh=True) == {"tt0000504"}
    assert provider.status()["conflict_count"] == 3
    assert provider.evidence_for("tt0000501")["decision"] == "review"
    assert provider.evidence_for("tt0000502")["conflicts"] == ["TMDb: țara de producție fără RO"]
    assert provider.evidence_for("tt0000503")["conflicts"] == ["TMDb: IMDb ID diferit"]
    assert RomanianCinemaProvider(db).imdb_ids() == {"tt0000504"}


def test_imdb_search_alone_does_not_claim_original_language(tmp_path, monkeypatch):
    db = Database(tmp_path / "imdb-only.db")
    _insert_movie(db, "tt0000551", "Coproducție", ["Romania"])
    provider = RomanianCinemaProvider(db)
    monkeypatch.setattr(provider, "_fetch_wikidata_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_imdb_ids", lambda: {"tt0000551"})
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    assert provider.imdb_ids(refresh=True) == set()
    assert provider.evidence_for("tt0000551")["decision"] == "needs-original-language"


def test_conflict_cannot_resurrect_from_stale_positive_cache(tmp_path, monkeypatch):
    db = Database(tmp_path / "held.db")
    _insert_movie(db, "tt0000601", "Conflict")
    with db.tx() as con:
        con.execute("UPDATE movies SET tmdb_id=601 WHERE imdb_id='tt0000601'")
        con.execute(
            "INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at) VALUES(?,?,?,?,?)",
            ("tmdb", '/movie/601?{}', json_dumps({"id": 601, "original_language": "en"}),
             utcnow_iso(), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )
    provider = RomanianCinemaProvider(db)
    provider._store({"tt0000601"}, days=-1)
    monkeypatch.setattr(provider, "_fetch_wikidata_ids", lambda: {"tt0000601"})
    monkeypatch.setattr(provider, "_fetch_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    assert provider.imdb_ids(refresh=True) == set()
    assert provider.status()["source"] == "romanian-conflicts-held-for-review"
    assert RomanianCinemaProvider(db).imdb_ids() == set()


def test_new_tmdb_detail_invalidates_romanian_snapshot(tmp_path, monkeypatch):
    db = Database(tmp_path / "new-detail.db")
    _insert_movie(db, "tt0000701", "Film")
    with db.tx() as con:
        con.execute("UPDATE movies SET tmdb_id=701 WHERE imdb_id='tt0000701'")
    provider = RomanianCinemaProvider(db)
    monkeypatch.setattr(provider, "_fetch_wikidata_ids", lambda: {"tt0000701"})
    monkeypatch.setattr(provider, "_fetch_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    assert provider.imdb_ids(refresh=True) == {"tt0000701"}
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with db.tx() as con:
        con.execute(
            "INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at) VALUES(?,?,?,?,?)",
            ("tmdb", '/movie/701?{}', json_dumps({"id": 701, "original_language": "en"}),
             utcnow_iso(), future),
        )
    assert provider.imdb_ids() == set()
    assert provider.evidence_for("tt0000701")["decision"] == "review"


def test_offline_stale_cache_still_holds_new_explicit_conflict(tmp_path, monkeypatch):
    db = Database(tmp_path / "offline-detail.db")
    _insert_movie(db, "tt0000801", "Conflict")
    with db.tx() as con:
        con.execute("UPDATE movies SET tmdb_id=801 WHERE imdb_id='tt0000801'")
        con.execute(
            "INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at) VALUES(?,?,?,?,?)",
            ("tmdb", '/movie/801?{}', json_dumps({"id": 801, "original_language": "fr"}),
             utcnow_iso(), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )
    provider = RomanianCinemaProvider(db)
    provider._store({"tt0000801", "tt0000802"}, days=-1)

    def offline():
        raise requests.RequestException("offline")

    monkeypatch.setattr(provider, "_fetch_wikidata_ids", offline)
    monkeypatch.setattr(provider, "_fetch_imdb_ids", offline)
    monkeypatch.setattr(provider, "_curated_imdb_ids", lambda: set())
    monkeypatch.setattr(provider, "_fetch_tmdb_ids", lambda: None)
    assert provider.imdb_ids(refresh=True) == {"tt0000802"}
    assert provider.evidence_for("tt0000801")["decision"] == "review"


def test_romanian_rows_use_verified_language_pool_and_keep_rated_blocked(tmp_path, monkeypatch):
    db = Database(tmp_path / "cinecalendar.db")
    keep_id = _insert_movie(db, "tt0000011", "Nevăzut românesc")
    rated_id = _insert_movie(db, "tt0000012", "Văzut românesc")
    _insert_movie(db, "tt0000013", "Alt film")
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
            (rated_id, 8, "2026-09-15", "test", now, now),
        )

    engine = FastRecommendationEngineV12(db)
    monkeypatch.setattr(engine.romanian_cinema, "imdb_ids", lambda refresh=False: {"tt0000011", "tt0000012"})
    rows = list(engine._romanian_rows())
    assert [int(row["id"]) for row in rows] == [keep_id]


def test_ui_patch_adds_dedicated_romanian_page_after_recommendations():
    class DummyWindow:
        NAV = [("today", "Azi"), ("recommendations", "Recomandări"), ("profile", "Profil")]

    install_romanian_cinema_ui_patch(DummyWindow)
    keys = [key for key, _label in DummyWindow.NAV]
    assert keys == ["today", "recommendations", "romanian", "profile"]
    assert hasattr(DummyWindow, "page_romanian")


def test_premium_startup_installs_romanian_cinema_patch():
    app_source = inspect.getsource(app_module.main)
    assert "compose_premium_window(CalendarPremiumWindow)" in app_source
    composition_source = inspect.getsource(ui_composition_module.compose_premium_window)
    assert "install_romanian_cinema_ui_patch(window_cls)" in composition_source



def test_romanian_ui_recalculate_invalidates_trial_round_and_shows_sources():
    source = inspect.getsource(install_romanian_cinema_ui_patch)
    assert 'invalidate("romanian")' in source
    assert 'status.get("source_counts")' in source
    assert "IMDb" in source
    assert "Wikidata" in source
    assert "TMDb" in source
