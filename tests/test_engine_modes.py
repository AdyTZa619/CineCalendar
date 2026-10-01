from cinecalendar.db import Database
from cinecalendar.engine_modes import (
    EngineModeRouter,
    MODE_ADAPTIVE,
    MODE_DISCOVERY,
    MODE_STABLE,
)
from cinecalendar.v5_rating_snapshot import rating_history_snapshot


class _Collaborative:
    def __init__(self):
        self.started = 0
    def start_background(self):
        self.started += 1


class _Engine:
    def __init__(self, name):
        self.name = name
        self.collaborative = _Collaborative()


def _eligible_report(db):
    return {
        "generated_at": "2026-10-01T00:00:00+00:00",
        "rating_snapshot": rating_history_snapshot(db),
        "decision": {
            "discovery_candidate_for_stable": True,
            "discovery_rolling_approved": True,
            "discovery_event_guard_passed": True,
            "discovery_visible_decision_guard_passed": True,
            "selected_discovery_variant": "strict",
            "eligible_for_visible_alpha_trial": True,
            "visible_decision_guard_passed": True,
            "selected_variant": "20%",
        },
    }


def test_one_database_router_is_stable_by_default_and_lazy(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    made = {"discovery": 0, "adaptive": 0}
    stable = _Engine("stable")
    router = EngineModeRouter(
        db,
        stable,
        lambda: made.__setitem__("discovery", made["discovery"] + 1) or _Engine("discovery"),
        lambda: made.__setitem__("adaptive", made["adaptive"] + 1) or _Engine("adaptive"),
    )
    assert router.mode == MODE_STABLE
    assert router.active is stable
    assert made == {"discovery": 0, "adaptive": 0}
    assert db.get_setting("recommendation_engine_mode") == MODE_STABLE


def test_router_unlocks_discovery_and_adaptive_only_for_current_report(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    db.set_setting("v5_evaluation_report", _eligible_report(db))
    stable = _Engine("stable")
    router = EngineModeRouter(
        db, stable, lambda: _Engine("discovery"), lambda: _Engine("adaptive")
    )
    assert router.discovery_eligible() is True
    assert router.adaptive_eligible() is True
    assert router.selected_discovery_variant() == "strict"

    status = router.set_mode(MODE_DISCOVERY)
    assert status["mode"] == MODE_DISCOVERY
    assert router.active.name == "discovery"

    status = router.set_mode(MODE_ADAPTIVE)
    assert status["mode"] == MODE_ADAPTIVE
    assert router.active.name == "adaptive"


def test_router_falls_back_to_stable_when_ratings_change(tmp_path):
    from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso

    db = Database(tmp_path / "cinecalendar.db")
    db.set_setting("v5_evaluation_report", _eligible_report(db))
    router = EngineModeRouter(
        db, _Engine("stable"), lambda: _Engine("discovery"), lambda: _Engine("adaptive")
    )
    router.set_mode(MODE_DISCOVERY)

    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,countries_json,overview,
                keywords_json,semantic_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt8000001", identity_key("Fresh rating", "Fresh rating", 2020, "movie"),
                "Fresh rating", "Fresh rating", normalize_text("Fresh rating"),
                normalize_text("Fresh rating"), 2020, "movie",
                json_dumps(["Drama"]), json_dumps([]), json_dumps([]), "",
                json_dumps([]), json_dumps({}), "test", now, now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(cur.lastrowid), 8, "2026-10-01", "test", now, now),
        )
    assert router.mode == MODE_STABLE
    assert db.get_setting("recommendation_engine_mode") == MODE_STABLE


def test_router_refreshes_when_new_report_arrives_without_rating_change(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    stable = _Engine("stable")
    router = EngineModeRouter(
        db, stable, lambda: _Engine("discovery"), lambda: _Engine("adaptive")
    )
    assert router.discovery_eligible() is False

    report = _eligible_report(db)
    report["generated_at"] = "2026-10-01T01:00:00+00:00"
    db.set_setting("v5_evaluation_report", report)

    assert router.discovery_eligible() is True
    assert router.adaptive_eligible() is True
