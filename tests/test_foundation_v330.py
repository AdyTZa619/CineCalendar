from __future__ import annotations

from datetime import date, timedelta
import inspect
import json
import os
from pathlib import Path
import sqlite3
import uuid
import zipfile

import pytest

import cinecalendar.backup as backup_mod
from cinecalendar.backup import export_profile, import_profile
from cinecalendar.db import Database, MIGRATIONS
from cinecalendar.decision_action_patch import record_decision_action
from cinecalendar.feedback import apply_feedback
from cinecalendar.trust_audit import (
    record_trust_snapshot,
    resolve_exposure_history_id,
)
from cinecalendar.single_instance import SingleInstanceGuard
from cinecalendar.updater_v4 import _powershell_helper
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_success_audit import build_watch_success_audit
from cinecalendar.watch_success_ui_patch import record_watch_event
from cinecalendar.watch_success_v33 import WatchSuccessIntentLearnerV33


def _movie(db: Database, imdb_id: str = "tt9330001", title: str = "Foundation") -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, imdb_id, title, title, 2025, "movie", 110,
                json.dumps(["Drama"]), json.dumps(["Director"]), json.dumps(["RO"]),
                "A detailed premise for foundation tests.", "[]", "{}", 7.3, 30000,
                "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def _exposure(db: Database, movie_id: int, status: str = "trusted") -> int:
    now = utcnow_iso()
    today = date.today().isoformat()
    with db.tx() as con:
        run = con.execute(
            """INSERT INTO recommendation_runs(
                   context_date,slot,generated_at,candidate_count,result_count,engine_version
               ) VALUES(?,?,?,?,?,?)""",
            (today, "decision", now, 20, 1, "16.0.0-top3-trust-gate"),
        )
        root = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score
               ) VALUES(?,?,?,?,?)""",
            (movie_id, now, today, "decision", .81),
        )
        root_id = int(root.lastrowid)
        record_trust_snapshot(
            con,
            history_id=root_id,
            run_id=int(run.lastrowid),
            movie_id=movie_id,
            context_date=today,
            slot="decision",
            rank_position=1,
            engine_version="16.0.0-top3-trust-gate",
            payload={
                "status": status,
                "trust": .8,
                "gate_score": .82,
                "supports": ["rating personal", "ALS", "calitate publică"],
                "red_flag": False,
                "gap": 0.0,
                "als": .9,
                "public_bayes": 7.1,
            },
            created_at=now,
        )
        return root_id


def test_two_same_day_exposures_never_use_latest_guess(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    first = _exposure(db, movie_id, "trusted")
    second = _exposure(db, movie_id, "backfill")

    with db.connect() as con:
        assert resolve_exposure_history_id(con, movie_id, date.today().isoformat()) is None

    chosen = record_decision_action(db, movie_id, "chosen", first)
    watched = record_watch_event(db, movie_id, "watched", first)
    skipped = record_decision_action(db, movie_id, "skip_today", second)

    with db.connect() as con:
        rows = con.execute(
            """SELECT id,action,exposure_history_id FROM recommendation_history
               WHERE id IN (?,?,?) ORDER BY id""",
            (chosen, watched, skipped),
        ).fetchall()
        roots = con.execute(
            "SELECT id,action,recommended_at FROM recommendation_history WHERE id IN (?,?) ORDER BY id",
            (first, second),
        ).fetchall()
    assert [int(r["exposure_history_id"]) for r in rows] == [first, first, second]
    assert [r["action"] for r in roots] == [None, None]


def test_watch_success_audit_is_exposure_level(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    first = _exposure(db, movie_id, "trusted")
    second = _exposure(db, movie_id, "backfill")
    record_watch_event(db, movie_id, "stremio_opened", first)
    record_watch_event(db, movie_id, "watched", first)
    record_decision_action(db, movie_id, "skip_today", second)

    audit = build_watch_success_audit(db, days=30)
    assert audit["exposures"] == 2
    assert audit["funnels"] == 2
    assert audit["watched"] == 1
    assert audit["skipped"] == 1
    assert audit["exact_linked_actions"] == 3
    assert audit["ambiguous_unlinked_actions"] == 0
    assert audit["watch_rate_per_exposure"] == 0.5


def test_watch_success_v33_collapses_by_exposure_not_movie_day(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    first = _exposure(db, movie_id)
    second = _exposure(db, movie_id)
    record_watch_event(db, movie_id, "watched", first)
    record_decision_action(db, movie_id, "skip_today", second)

    status = WatchSuccessIntentLearnerV33(db).status()
    assert status["raw_action_events"] == 2
    assert status["collapsed_action_events"] == 2
    assert status["positive_evidence"] > 0
    assert status["negative_evidence"] > 0


def test_feedback_never_mutates_exposure(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    exposure = _exposure(db, movie_id)
    with db.connect() as con:
        before = dict(con.execute("SELECT * FROM recommendation_history WHERE id=?", (exposure,)).fetchone())

    apply_feedback(db, movie_id, "seen")
    apply_feedback(db, movie_id, "want_to_watch")

    with db.connect() as con:
        after = dict(con.execute("SELECT * FROM recommendation_history WHERE id=?", (exposure,)).fetchone())
    assert after == before


def test_partial_v5_migration_is_resumed_and_upgraded_to_v6(tmp_path):
    path = tmp_path / "partial.db"
    con = sqlite3.connect(path)
    try:
        for version in range(1, 5):
            con.executescript(MIGRATIONS[version])
            con.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, utcnow_iso()),
            )
        # Simulate an old process dying after ALTER TABLE but before trust table/version marker.
        con.execute(
            """ALTER TABLE recommendation_history
               ADD COLUMN exposure_history_id INTEGER REFERENCES recommendation_history(id) ON DELETE SET NULL"""
        )
        con.commit()
    finally:
        con.close()

    db = Database(path)
    with db.connect() as con:
        assert con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_trust_audit'"
        ).fetchone() is not None
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_provenance'"
        ).fetchone() is not None
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert path.with_name(path.stem + ".pre_migration.bak").is_file()


def test_backup_merge_keeps_newer_local_rating_and_restore_can_replace_it(tmp_path):
    source = Database(tmp_path / "source.db")
    movie_id = _movie(source, "tt9330002", "Merge Test")
    old = (date.today() - timedelta(days=20)).isoformat()
    old_ts = old + "T12:00:00+00:00"
    with source.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, 6, old, "backup-source", old_ts, old_ts),
        )
    archive = export_profile(source, tmp_path / "old-profile.zip")

    target = Database(tmp_path / "target.db")
    target_movie = _movie(target, "tt9330002", "Merge Test")
    new = date.today().isoformat()
    new_ts = new + "T12:00:00+00:00"
    with target.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (target_movie, 9, new, "local-new", new_ts, new_ts),
        )

    stats = import_profile(target, archive, mode="merge")
    with target.connect() as con:
        row = con.execute("SELECT rating,source FROM ratings WHERE movie_id=?", (target_movie,)).fetchone()
    assert row["rating"] == 9
    assert row["source"] == "local-new"
    assert stats["conflicts_skipped"] >= 1

    import_profile(target, archive, mode="restore")
    with target.connect() as con:
        row = con.execute("SELECT rating,source FROM ratings WHERE movie_id=?", (target_movie,)).fetchone()
    assert row["rating"] == 6
    assert row["source"] == "backup-source"


def test_updater_rolls_back_sqlite_together_with_bundle():
    helper = _powershell_helper()
    assert "Restore-DataBackup" in helper
    assert "db_backup_present" in helper
    assert "ROLLBACK DB EȘUAT" in helper
    assert helper.count("Restore-DataBackup") >= 3



def test_profile_export_uses_one_consistent_wal_snapshot(tmp_path, monkeypatch):
    db = Database(tmp_path / "snapshot.db")
    movie_id = _movie(db, "tt9330003", "Snapshot Test")
    original = backup_mod._movies_for_ids
    injected = {"done": False}

    def movies_then_concurrent_write(con, movie_ids):
        rows = original(con, movie_ids)
        if not injected["done"]:
            injected["done"] = True
            with db.tx() as writer:
                writer.execute(
                    "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
                    (movie_id, "more_like_this", 0.1, utcnow_iso()),
                )
        return rows

    monkeypatch.setattr(backup_mod, "_movies_for_ids", movies_then_concurrent_write)
    archive_path = export_profile(db, tmp_path / "snapshot.zip")

    with zipfile.ZipFile(archive_path, "r") as archive:
        payload = json.loads(archive.read("profile.json").decode("utf-8"))
    # The concurrent commit happened after the export transaction pinned its snapshot, therefore
    # it must not leak into only the later feedback SELECT of the same backup.
    assert payload["tables"]["feedback"] == []
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1


def test_startup_recovers_corrupt_db_from_last_good_snapshot(tmp_path):
    path = tmp_path / "recover.db"
    db = Database(path)
    movie_id = _movie(db, "tt9330004", "Recovery Test")
    con = db.connect()
    try:
        db._refresh_recovery_snapshot(con)
    finally:
        con.close()
    assert db.recovery_snapshot_path.is_file()

    # sqlite3.Connection is itself a transaction context manager, not a closing context manager.
    # Close the handle explicitly before replacing the file; Windows correctly refuses to overwrite
    # a live SQLite handle, which is unrelated to the recovery behavior under test.
    con = db.connect()
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    path.write_bytes(b"not-a-sqlite-database")

    recovered = Database(path)
    with recovered.connect() as con:
        row = con.execute("SELECT title FROM movies WHERE id=?", (movie_id,)).fetchone()
        assert row is not None and row["title"] == "Recovery Test"
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert list(tmp_path.glob("recover.corrupt-*.db"))


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_mutex_blocks_second_process_guard():
    name = "Local\\CineCalendar-Test-" + uuid.uuid4().hex
    first = SingleInstanceGuard(name)
    second = SingleInstanceGuard(name)
    third = SingleInstanceGuard(name)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert third.acquire() is True
    third.release()


def test_production_ui_patch_order_is_centralized_and_validated():
    repo = Path(__file__).resolve().parents[1]
    app_source = (repo / "cinecalendar" / "app.py").read_text(encoding="utf-8")
    composition = (repo / "cinecalendar" / "ui_composition.py").read_text(encoding="utf-8")
    assert "compose_premium_window(CalendarPremiumWindow)" in app_source
    for scattered in (
        "install_performance_ui_patch(CalendarPremiumWindow)",
        "install_decision_action_patch(CalendarPremiumWindow)",
        "install_watch_success_ui_patch(CalendarPremiumWindow)",
        "install_foundation_v33(CalendarPremiumWindow)",
    ):
        assert scattered not in app_source
    order = [
        "install_performance_ui_patch(window_cls)",
        "install_decision_action_patch(window_cls)",
        "install_watch_success_ui_patch(window_cls)",
        "install_foundation_v33(window_cls)",
    ]
    positions = [composition.index(token) for token in order]
    assert positions == sorted(positions)
    assert "_cinecalendar_v33_composed" in composition
