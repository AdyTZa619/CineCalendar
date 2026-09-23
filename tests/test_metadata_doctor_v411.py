from __future__ import annotations

from types import SimpleNamespace

from cinecalendar.db import Database, SCHEMA_VERSION
from cinecalendar.metadata_doctor import (
    audit_metadata_doctor,
    process_metadata_queue,
    queue_metadata_movie,
    queue_recommendation_metadata,
    recent_metadata_jobs,
    report_broken_poster,
    refresh_metadata_job,
)
from cinecalendar.models import Movie
from cinecalendar.util import utcnow_iso


def _movie(db: Database, imdb_id: str, title: str, *, poster: str | None = None, complete=False) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                 imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                 year,title_type,runtime_min,genres_json,directors_json,countries_json,
                 overview,keywords_json,semantic_json,poster_url,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, imdb_id, title, title, title.lower(), title.lower(), 2020, "movie",
                100 if complete else None, '["Drama"]' if complete else "[]",
                '["Director"]' if complete else "[]", '["Romania"]' if complete else "[]",
                "Descriere" if complete else "", "[]", "{}", poster, "test", now, now,
            ),
        )
        return int(cur.lastrowid)


def test_schema_v11_creates_persistent_metadata_tables(tmp_path):
    db = Database(tmp_path / "schema.db")
    with db.connect() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        current = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert SCHEMA_VERSION == 11
    assert current == 11
    assert {"metadata_jobs", "metadata_issues"}.issubset(tables)


def test_queue_keeps_visible_recommendations_at_higher_priority(tmp_path):
    db = Database(tmp_path / "priority.db")
    ids = [_movie(db, f"tt910000{i}", f"Film {i}") for i in range(1, 15)]
    recs = [SimpleNamespace(movie=Movie(id=mid, imdb_id=f"tt910000{i}", title=f"Film {i}")) for i, mid in enumerate(ids, 1)]

    assert queue_recommendation_metadata(db, recs) == 14

    jobs = recent_metadata_jobs(db, 20)
    by_id = {int(item["movie_id"]): item for item in jobs}
    assert int(by_id[ids[0]]["priority"]) > int(by_id[ids[12]]["priority"])
    assert by_id[ids[0]]["reason"] == "recommendation:1"


def test_queue_processor_completes_only_after_fields_are_saved(tmp_path):
    db = Database(tmp_path / "process.db")
    movie_id = _movie(db, "tt9200001", "Repair me")
    assert queue_metadata_movie(db, movie_id, priority=900, reason="rated")

    class Provider:
        def __init__(self, provider_db):
            self.db = provider_db

        def enrich_by_imdb(self, movie):
            now = utcnow_iso()
            with self.db.tx() as con:
                con.execute(
                    """UPDATE movies SET runtime_min=105,genres_json='["Drama"]',
                       directors_json='["Director Test"]',countries_json='["Romania"]',
                       overview='Descriere verificată',poster_url='https://image.test/poster.jpg',
                       updated_at=? WHERE id=?""",
                    (now, movie.id),
                )
            movie.runtime_min = 105
            movie.genres = ["Drama"]
            movie.directors = ["Director Test"]
            movie.countries = ["Romania"]
            movie.overview = "Descriere verificată"
            movie.poster_url = "https://image.test/poster.jpg"

    result = process_metadata_queue(db, limit=5, open_factory=Provider)
    assert result.attempted == 1
    assert result.completed == 1
    assert result.improved == 1
    job = recent_metadata_jobs(db, 1)[0]
    assert job["status"] == "complete"
    assert job["missing_json"] == "[]"


def test_foreground_attempt_sets_backoff_and_requeue_does_not_cancel_it(tmp_path):
    db = Database(tmp_path / "backoff.db")
    movie_id = _movie(db, "tt9250001", "Still incomplete")
    assert queue_metadata_movie(db, movie_id, priority=900, reason="recommendation:1")

    assert refresh_metadata_job(db, movie_id, error="surse fără rezultat") is False
    first = recent_metadata_jobs(db, 1)[0]
    assert first["status"] == "retry"
    assert int(first["attempt_count"]) == 1
    assert first["next_check_at"]

    assert queue_metadata_movie(db, movie_id, priority=950, reason="recommendation:1")
    second = recent_metadata_jobs(db, 1)[0]
    assert second["status"] == "retry"
    assert second["next_check_at"] == first["next_check_at"]


def test_broken_poster_is_cleared_reported_and_requeued(tmp_path):
    db = Database(tmp_path / "poster.db")
    url = "https://image.test/broken.jpg"
    movie_id = _movie(db, "tt9300001", "Broken", poster=url, complete=True)

    assert report_broken_poster(db, movie_id, url, "HTTP 404")

    with db.connect() as con:
        movie = con.execute("SELECT poster_url FROM movies WHERE id=?", (movie_id,)).fetchone()
        issue = con.execute(
            "SELECT issue_type,detail FROM metadata_issues WHERE movie_id=? AND resolved_at IS NULL",
            (movie_id,),
        ).fetchone()
        job = con.execute("SELECT priority,reason,status FROM metadata_jobs WHERE movie_id=?", (movie_id,)).fetchone()
    assert movie["poster_url"] is None
    assert issue["issue_type"] == "broken_url"
    assert "HTTP 404" in issue["detail"]
    assert int(job["priority"]) == 1200
    assert job["reason"] == "broken_poster"
    assert job["status"] == "pending"


def test_audit_rejects_non_http_poster_urls(tmp_path):
    db = Database(tmp_path / "audit.db")
    movie_id = _movie(db, "tt9400001", "Unsafe poster", poster="file:///tmp/poster.jpg", complete=True)

    report = audit_metadata_doctor(db)

    assert report.open_issues == 1
    assert report.broken_posters == 1
    with db.connect() as con:
        poster = con.execute("SELECT poster_url FROM movies WHERE id=?", (movie_id,)).fetchone()[0]
        job = con.execute("SELECT reason FROM metadata_jobs WHERE movie_id=?", (movie_id,)).fetchone()
    assert poster is None
    assert job["reason"] == "invalid_poster"


def test_metadata_doctor_is_visible_and_runs_in_background():
    ui = open("cinecalendar/qt_ui.py", encoding="utf-8").read()
    navigation = open("cinecalendar/qt_ui_v2.py", encoding="utf-8").read()
    premium = open("cinecalendar/candidate_metadata_v48.py", encoding="utf-8").read()
    assert '("metadata_doctor", "Metadata Doctor")' in navigation
    assert "def page_metadata_doctor" in ui
    assert "self.metadata_queue_timer.start(15 * 60 * 1000)" in ui
    assert "queue_recommendation_metadata(self.db, recs" in premium
