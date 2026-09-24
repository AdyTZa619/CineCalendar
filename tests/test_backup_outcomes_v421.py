from __future__ import annotations

import json
import zipfile

from cinecalendar.backup import PROFILE_VERSION, export_profile, import_profile
from cinecalendar.db import Database
from cinecalendar.recommendation_outcomes_v42 import recommendation_performance
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


def _movie(db: Database, idx: int, title: str) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{9800000+idx:07d}",
                identity_key(title, title, 2025, "movie"),
                title,
                title,
                normalize_text(title),
                normalize_text(title),
                2025,
                "movie",
                110,
                json_dumps(["Drama"]),
                json_dumps(["Director"]),
                json_dumps(["Romania"]),
                7.4,
                10000,
                "test",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def _exposure(
    db: Database,
    movie_id: int,
    *,
    day: str,
    predicted: float | None,
    confidence: float | None,
    final_score: float = .82,
) -> int:
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action,
                   exposure_history_id,predicted_rating,confidence
               ) VALUES(?,?,?,?,?,0,NULL,NULL,?,?)""",
            (
                movie_id,
                day + "T18:00:00+00:00",
                day,
                "decision",
                final_score,
                predicted,
                confidence,
            ),
        )
        return int(cur.lastrowid)


def _rating(db: Database, movie_id: int, value: int, day: str) -> int:
    stamp = day + "T23:00:00+00:00"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, value, day, "test", stamp, stamp),
        )
        return int(cur.lastrowid)


def test_profile_v3_roundtrip_preserves_prediction_snapshots_and_outcomes(tmp_path):
    source = Database(tmp_path / "source.db")
    mid = _movie(source, 1, "Outcome Backup")
    exposure = _exposure(
        source,
        mid,
        day="2026-09-20",
        predicted=8.3,
        confidence=.76,
        final_score=.88,
    )
    rating_id = _rating(source, mid, 9, "2026-09-21")
    with source.tx() as con:
        con.execute(
            """INSERT INTO recommendation_outcomes(
                   exposure_history_id,movie_id,rank_position,context_date,slot,
                   chosen_at,playback_at,watched_at,rating_id,actual_rating,rating_date,
                   predicted_rating,confidence,final_score,engine_version,absolute_error,
                   resolved_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                exposure,
                mid,
                1,
                "2026-09-20",
                "decision",
                "2026-09-20T19:00:00+00:00",
                "2026-09-20T19:10:00+00:00",
                "2026-09-20T21:00:00+00:00",
                rating_id,
                9,
                "2026-09-21",
                8.3,
                .76,
                .88,
                "4.2-test-engine",
                .7,
                "2026-09-21T23:01:00+00:00",
                "2026-09-21T23:01:00+00:00",
            ),
        )

    archive = export_profile(source, tmp_path / "profile-v4.zip")
    with zipfile.ZipFile(archive, "r") as fh:
        payload = json.loads(fh.read("profile.json").decode("utf-8"))
        manifest = json.loads(fh.read("manifest.json").decode("utf-8"))
    assert PROFILE_VERSION == 5
    assert payload["version"] == 5
    assert manifest["counts"]["recommendation_outcomes"] == 1
    assert len(payload["tables"]["recommendation_outcomes"]) == 1

    target = Database(tmp_path / "target.db")
    stats = import_profile(target, archive, mode="restore")
    assert stats["outcomes"] == 1

    with target.connect() as con:
        root = con.execute(
            """SELECT id,predicted_rating,confidence,final_score
               FROM recommendation_history
               WHERE action IS NULL AND slot='decision'"""
        ).fetchone()
        outcome = con.execute("SELECT * FROM recommendation_outcomes").fetchone()
        rating = con.execute("SELECT id,rating FROM ratings").fetchone()

    assert root is not None
    assert abs(float(root["predicted_rating"]) - 8.3) < 1e-9
    assert abs(float(root["confidence"]) - .76) < 1e-9
    assert outcome is not None
    assert int(outcome["exposure_history_id"]) == int(root["id"])
    assert int(outcome["rating_id"]) == int(rating["id"])
    assert int(outcome["actual_rating"]) == 9
    assert abs(float(outcome["predicted_rating"]) - 8.3) < 1e-9
    assert abs(float(outcome["confidence"]) - .76) < 1e-9
    assert abs(float(outcome["absolute_error"]) - .7) < 1e-9
    assert outcome["engine_version"] == "4.2-test-engine"


def test_backup_merge_maps_outcome_to_newer_local_rating(tmp_path):
    source = Database(tmp_path / "source-merge.db")
    mid = _movie(source, 2, "Merge Outcome")
    exposure = _exposure(source, mid, day="2026-09-18", predicted=7.5, confidence=.7)
    old_rating_id = _rating(source, mid, 7, "2026-09-19")
    with source.tx() as con:
        con.execute(
            """INSERT INTO recommendation_outcomes(
                   exposure_history_id,movie_id,rank_position,context_date,slot,
                   chosen_at,rating_id,actual_rating,rating_date,predicted_rating,confidence,
                   final_score,engine_version,absolute_error,resolved_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                exposure, mid, 1, "2026-09-18", "decision",
                "2026-09-18T19:00:00+00:00",
                old_rating_id, 7, "2026-09-19", 7.5, .7, .82,
                "backup-engine", .5,
                "2026-09-19T23:00:00+00:00",
                "2026-09-19T23:00:00+00:00",
            ),
        )
    archive = export_profile(source, tmp_path / "merge-profile.zip")

    target = Database(tmp_path / "target-merge.db")
    target_mid = _movie(target, 2, "Merge Outcome")
    new_rating_id = _rating(target, target_mid, 9, "2026-09-22")
    import_profile(target, archive, mode="merge")

    with target.connect() as con:
        rating = con.execute("SELECT id,rating FROM ratings WHERE movie_id=?", (target_mid,)).fetchone()
        outcome = con.execute("SELECT rating_id FROM recommendation_outcomes").fetchone()
    assert int(rating["id"]) == new_rating_id
    assert int(rating["rating"]) == 9
    assert int(outcome["rating_id"]) == new_rating_id


def test_performance_window_excludes_legacy_exposures_without_prediction_snapshot(tmp_path):
    db = Database(tmp_path / "measurement.db")
    legacy_mid = _movie(db, 3, "Legacy")
    measurable_mid = _movie(db, 4, "Measured")

    legacy = _exposure(
        db,
        legacy_mid,
        day="2026-09-18",
        predicted=None,
        confidence=None,
    )
    measured = _exposure(
        db,
        measurable_mid,
        day="2026-09-20",
        predicted=8.0,
        confidence=.8,
    )

    with db.tx() as con:
        for exposure_id, movie_id, predicted in (
            (legacy, legacy_mid, None),
            (measured, measurable_mid, 8.0),
        ):
            con.execute(
                """INSERT INTO recommendation_outcomes(
                       exposure_history_id,movie_id,rank_position,context_date,slot,
                       chosen_at,predicted_rating,confidence,final_score,engine_version,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    exposure_id,
                    movie_id,
                    1,
                    "2026-09-18" if predicted is None else "2026-09-20",
                    "decision",
                    "2026-09-20T19:00:00+00:00",
                    predicted,
                    None if predicted is None else .8,
                    .8,
                    "legacy" if predicted is None else "4.2+",
                    utcnow_iso(),
                ),
            )

    perf = recommendation_performance(db)
    assert perf.decision_exposures == 1
    assert perf.chosen == 1
    assert perf.measurement_start == "2026-09-20"
    assert len(perf.recent) == 1
    assert perf.recent[0]["title"] == "Measured"
