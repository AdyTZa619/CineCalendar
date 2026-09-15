from __future__ import annotations

import inspect

from cinecalendar import app as app_module
from cinecalendar.db import Database
from cinecalendar.recommender_v12 import FastRecommendationEngineV12
from cinecalendar.romanian_cinema import RomanianCinemaProvider
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


def test_wikidata_query_is_precision_first_for_romanian_identity():
    query = RomanianCinemaProvider.wikidata_query()
    assert "wdt:P495 wd:Q218" in query
    assert "wdt:P364 wd:Q7913" in query
    assert "FILTER NOT EXISTS" in query
    assert "?otherCountry != wd:Q218" in query
    assert "wdt:P31/wdt:P279* wd:Q11424" in query


def test_local_romania_country_is_strict_offline_fallback(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    _insert_movie(db, "tt0000001", "Film RO", ["România"])
    _insert_movie(db, "tt0000002", "Film US", ["United States"])
    _insert_movie(db, "tt0000003", "Coproducție", ["Romania", "France"])
    provider = RomanianCinemaProvider(db)
    assert provider.local_imdb_ids() == {"tt0000001"}


def test_romanian_rows_use_explicit_country_pool_and_keep_rated_blocked(tmp_path, monkeypatch):
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
    source = inspect.getsource(app_module.main)
    assert "install_romanian_cinema_ui_patch(CalendarPremiumWindow)" in source
