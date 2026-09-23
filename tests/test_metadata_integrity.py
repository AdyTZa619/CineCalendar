from __future__ import annotations

from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.models import Movie
from cinecalendar.open_metadata import OpenMovieMetadataProvider
from cinecalendar.tmdb import TmdbProvider
from cinecalendar.util import json_loads, utcnow_iso


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _WikidataSession:
    def __init__(self):
        self.params = None
        self.headers = {}

    def get(self, _url, **kwargs):
        self.params = kwargs.get("params") or {}
        return _Response({
            "results": {
                "bindings": [{
                    "item": {"value": "http://www.wikidata.org/entity/Q1"},
                    "itemDescription": {"value": "film test"},
                    "directorLabel": {"value": "Director WD"},
                    "countryLabel": {"value": "Romania"},
                    "genreLabel": {"value": "Drama"},
                    "duration": {"value": "95"},
                }]
            }
        })


def _insert_movie(db: Database) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,runtime_min,genres_json,directors_json,countries_json,
                overview,keywords_json,poster_url,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt7770001","movie|movie|2024|movie","Movie","Movie","movie","movie",
                2024,"movie",100,'["Drama"]','["IMDb Director"]','["Romania"]',
                "",'[]',None,"test",now,now,
            ),
        )
        return int(cur.lastrowid)


def test_wikidata_query_actually_binds_genre(tmp_path):
    db = Database(tmp_path / "wd.db")
    provider = OpenMovieMetadataProvider(db)
    fake = _WikidataSession()
    provider.session = fake

    payload = provider._fetch("tt7770001")

    assert "wdt:P136 ?genre" in str(fake.params.get("query") or "")
    assert payload["genres"] == ["Drama"]
    assert payload["directors"] == ["Director WD"]


def test_tmdb_fallback_does_not_overwrite_existing_imdb_director(tmp_path, monkeypatch):
    db = Database(tmp_path / "tmdb.db")
    mid = _insert_movie(db)
    provider = TmdbProvider(db, "token")

    def fake_get(path, params=None, cache_hours=168):
        if path.startswith("/find/"):
            return {"movie_results": [{"id": 123}]}
        return {
            "original_title": "TMDb Original",
            "overview": "Overview TMDb",
            "runtime": 120,
            "production_countries": [{"name": "France"}],
            "genres": [{"name": "Thriller"}],
            "credits": {"crew": [{"job": "Director", "name": "TMDb Director"}]},
            "keywords": {"keywords": [{"name": "mystery"}]},
            "poster_path": "/poster.jpg",
        }

    monkeypatch.setattr(provider, "_get", fake_get)
    movie = Movie(
        id=mid,
        imdb_id="tt7770001",
        title="Movie",
        original_title="Movie",
        year=2024,
        title_type="movie",
        runtime_min=100,
        genres=["Drama"],
        directors=["IMDb Director"],
        countries=["Romania"],
    )
    provider.enrich_by_imdb(movie)

    assert movie.directors == ["IMDb Director"]
    assert movie.genres == ["Drama"]
    assert movie.runtime_min == 100
    assert movie.countries == ["Romania"]
    assert movie.poster_url and movie.poster_url.endswith("/poster.jpg")

    with db.connect() as con:
        row = con.execute(
            "SELECT directors_json,genres_json,runtime_min,poster_url FROM movies WHERE id=?",
            (mid,),
        ).fetchone()
        provenance = {
            item["field"]: item["provider"]
            for item in con.execute(
                "SELECT field,provider FROM metadata_provenance WHERE movie_id=?",
                (mid,),
            ).fetchall()
        }
    assert json_loads(row["directors_json"], []) == ["IMDb Director"]
    assert json_loads(row["genres_json"], []) == ["Drama"]
    assert int(row["runtime_min"]) == 100
    assert row["poster_url"].endswith("/poster.jpg")
    assert provenance["poster_url"] == "tmdb"
    assert "directors" not in provenance


def test_tmdb_poster_selection_prefers_romanian_then_quality():
    details = {
        "poster_path": "/default.jpg",
        "images": {"posters": [
            {"file_path": "/en.jpg", "iso_639_1": "en", "vote_count": 100, "vote_average": 9, "width": 2000},
            {"file_path": "/ro-low.jpg", "iso_639_1": "ro", "vote_count": 1, "vote_average": 4, "width": 500},
            {"file_path": "/ro-best.jpg", "iso_639_1": "ro", "vote_count": 8, "vote_average": 7, "width": 1000},
        ]},
    }

    assert TmdbProvider._best_poster(details) == "/ro-best.jpg"


def test_tmdb_reports_when_imdb_title_is_not_found(tmp_path, monkeypatch):
    db = Database(tmp_path / "tmdb-not-found.db")
    provider = TmdbProvider(db, "token")
    monkeypatch.setattr(provider, "_get", lambda *_args, **_kwargs: {"movie_results": []})

    movie = Movie(imdb_id="tt9999999", title="Missing", year=2024)
    provider.enrich_by_imdb(movie)

    assert provider.last_status == "not_found"
    assert provider.last_error == ""


def test_schema_v6_has_metadata_provenance(tmp_path):
    db = Database(tmp_path / "schema.db")
    with db.connect() as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = con.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    assert "metadata_provenance" in tables
    assert int(version) >= 6
