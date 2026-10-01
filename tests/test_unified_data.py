from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cinecalendar.db import Database
from cinecalendar.unified_data import AlphaMergeError, MERGE_SETTING, merge_existing_alpha_data
from cinecalendar.util import utcnow_iso


def _movie(db: Database, imdb_id: str, rating: int, updated_at: str) -> int:
    with db.tx() as con:
        con.execute(
            """INSERT INTO movies(imdb_id,identity_key,title,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (imdb_id, f"movie:{imdb_id}", imdb_id, utcnow_iso(), utcnow_iso()),
        )
        movie_id = int(con.execute("SELECT id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()[0])
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, rating, "2026-09-30", "test", updated_at, updated_at),
        )
    return movie_id


def _bases(tmp_path: Path):
    stable_root = tmp_path / "Stable" / "CineCalendarData"
    alpha_path = tmp_path / "Alpha" / "CineCalendarV5AlphaData" / "data" / "cinecalendar.db"
    stable = Database(stable_root / "data" / "cinecalendar.db")
    alpha = Database(alpha_path)
    return stable_root, stable, alpha


def test_merge_keeps_latest_rating_and_combines_alpha_actions_without_settings(tmp_path):
    root, stable, alpha = _bases(tmp_path)
    _movie(stable, "tt0000001", 9, "2026-10-01T12:00:00+00:00")
    _movie(alpha, "tt0000001", 3, "2026-09-30T12:00:00+00:00")
    alpha_only = _movie(alpha, "tt0000002", 8, "2026-10-01T13:00:00+00:00")
    stable.set_setting("premium_skin", "simple")
    alpha.set_setting("premium_skin", "poster_wall")
    alpha.set_setting("v5_evaluation_report", {"old_trial": True})
    with alpha.tx() as con:
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (alpha_only, "skip", 1.0, "2026-10-01T13:00:00+00:00"),
        )
        con.execute(
            "INSERT INTO watchlist(movie_id,status,added_at,updated_at) VALUES(?,?,?,?)",
            (alpha_only, "want_to_watch", "2026-10-01T13:00:00+00:00", "2026-10-01T13:00:00+00:00"),
        )
        con.execute(
            "INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot) VALUES(?,?,?,?)",
            (alpha_only, "2026-10-01T12:00:00+00:00", "2026-10-01", "decision"),
        )
    before = hashlib.sha256(alpha.path.read_bytes()).hexdigest()

    report = merge_existing_alpha_data(stable, root)
    assert report["state"] == "merged"
    assert report["ratings"] == 1
    assert report["feedback"] == 1
    assert report["history"] == 1
    assert len(report["backups"]) == 1 and Path(report["backups"][0]).is_file()
    assert hashlib.sha256(alpha.path.read_bytes()).hexdigest() == before
    with stable.connect() as con:
        rows = con.execute(
            "SELECT m.imdb_id,r.rating FROM ratings r JOIN movies m ON m.id=r.movie_id ORDER BY m.imdb_id"
        ).fetchall()
        assert [(row["imdb_id"], row["rating"]) for row in rows] == [
            ("tt0000001", 9), ("tt0000002", 8),
        ]
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_history").fetchone()[0] == 1
    assert stable.get_setting("premium_skin") == "simple"
    assert stable.get_setting("v5_evaluation_report", None) is None

    assert merge_existing_alpha_data(stable, root)["state"] == "already_merged"
    with stable.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    assert stable.get_setting(MERGE_SETTING)["sources"]


def test_changed_alpha_database_is_merged_again_without_duplicate_feedback(tmp_path):
    root, stable, alpha = _bases(tmp_path)
    first = _movie(alpha, "tt0000001", 7, "2026-09-30T12:00:00+00:00")
    with alpha.tx() as con:
        con.execute("INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
                    (first, "skip", 1.0, "2026-09-30T12:00:00+00:00"))
    assert merge_existing_alpha_data(stable, root)["state"] == "merged"
    _movie(alpha, "tt0000002", 9, "2026-10-01T13:00:00+00:00")
    assert merge_existing_alpha_data(stable, root)["state"] == "merged"
    with stable.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1


def test_equal_timestamp_preserves_stable_rating(tmp_path):
    root, stable, alpha = _bases(tmp_path)
    timestamp = "2026-10-01T12:00:00+00:00"
    _movie(stable, "tt0000001", 9, timestamp)
    _movie(alpha, "tt0000001", 3, timestamp)
    merge_existing_alpha_data(stable, root)
    with stable.connect() as con:
        assert con.execute("SELECT rating FROM ratings").fetchone()[0] == 9


def test_explicit_alpha_path_outside_neighboring_folders(tmp_path):
    root, stable, _alpha = _bases(tmp_path)
    remote_alpha = Database(tmp_path / "other-drive" / "old" / "cinecalendar.db")
    _movie(remote_alpha, "tt0000003", 8, "2026-10-01T13:00:00+00:00")
    report = merge_existing_alpha_data(stable, root, extra_source=remote_alpha.path)
    assert report["state"] == "merged"
    assert str(remote_alpha.path) in report["sources"]
    with stable.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 1
    assert merge_existing_alpha_data(stable, root)["state"] == "already_merged"
    _movie(remote_alpha, "tt0000004", 7, "2026-10-01T14:00:00+00:00")
    assert merge_existing_alpha_data(stable, root)["state"] == "merged"
    with stable.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 2


def test_corrupt_alpha_does_not_modify_stable_database(tmp_path):
    root, stable, alpha = _bases(tmp_path)
    _movie(stable, "tt0000001", 9, "2026-10-01T12:00:00+00:00")
    alpha.path.unlink()
    alpha.path.write_bytes(b"not a SQLite database")
    with pytest.raises(AlphaMergeError):
        merge_existing_alpha_data(stable, root)
    with stable.connect() as con:
        assert con.execute("SELECT rating FROM ratings").fetchone()[0] == 9
    assert stable.get_setting(MERGE_SETTING, None) is None


def test_failed_import_restores_stable_from_verified_backup(tmp_path, monkeypatch):
    root, stable, alpha = _bases(tmp_path)
    _movie(stable, "tt0000001", 9, "2026-10-01T12:00:00+00:00")
    _movie(alpha, "tt0000002", 8, "2026-10-01T13:00:00+00:00")

    def broken_import(db, _profile, mode):
        with db.tx() as con:
            con.execute("DELETE FROM ratings")
        raise RuntimeError("simulated error after a partial commit")

    monkeypatch.setattr("cinecalendar.unified_data.import_profile", broken_import)
    with pytest.raises(AlphaMergeError):
        merge_existing_alpha_data(stable, root)
    with stable.connect() as con:
        rows = con.execute("SELECT rating FROM ratings").fetchall()
    assert [row[0] for row in rows] == [9]
    assert list((root / "backups").glob("before-alpha-merge-*.db"))
    assert stable.get_setting(MERGE_SETTING, None) is None
