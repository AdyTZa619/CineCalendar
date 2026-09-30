from datetime import date, timedelta

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.util import json_dumps, utcnow_iso
from cinecalendar.v5_rating_snapshot import rating_history_snapshot, report_rating_freshness
from cinecalendar.v5_visible_trial import (
    AlphaTrialRecommender,
    MODE_V16,
    MODE_V5_20,
)


class FakeEngine:
    def __init__(self, recs):
        self.recs = list(recs)
        self.recommend_calls = 0
        self.decision_calls = 0
        self.romanian_calls = 0
        self.decision_exclusions = []
        self.decision_kwargs = []

    def recommend(self, **kwargs):
        self.recommend_calls += 1
        count = int(kwargs.get("count", 3))
        return list(self.recs[:count])

    def recommend_romanian(self, when=None, count=9):
        self.romanian_calls += 1
        return list(self.recs[: int(count)])

    def decision_pick(self, when=None, exclude_ids=None, mode="decide", **kwargs):
        self.decision_calls += 1
        excluded = set(exclude_ids or set())
        self.decision_exclusions.append(excluded)
        self.decision_kwargs.append(kwargs)
        recs = [rec for rec in self.recs if rec.movie.id not in excluded][:3]
        return (recs[0] if recs else None, recs[1:3])


def _rec(mid: int, final: float):
    return Recommendation(
        Movie(id=mid, imdb_id=f"tt{mid:07d}", title=f"Movie {mid}"),
        ScoreBreakdown(
            final=final,
            predicted_rating=7.0 + final,
            confidence=0.8,
            score_factors={},
        ),
    )


def _eligible(db):
    db.set_setting(
        "v5_evaluation_report",
        {
            "decision": {
                "eligible_for_visible_alpha_trial": True,
                "visible_decision_guard_passed": True,
                "selected_variant": "20%",
            },
            "rating_snapshot": rating_history_snapshot(db),
        },
    )


def test_alpha_trial_defaults_to_v5_when_latest_report_is_eligible(tmp_path):
    db = Database(tmp_path / "trial.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.70), _rec(2, 0.69)])
    v5 = FakeEngine([_rec(2, 0.78), _rec(1, 0.71)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    assert proxy.trial_status()["mode"] == MODE_V5_20
    recs = proxy.recommend(when=date(2026, 9, 27), count=2, slot="browse")
    assert [r.movie.id for r in recs] == [2, 1]
    assert recs[0].score.score_factors["v5_trial_active"] == 1.0
    assert recs[0].score.score_factors["v5_trial_v16_rank"] == 2.0
    assert recs[0].score.score_factors["v5_trial_v5_rank"] == 1.0

    with db.connect() as con:
        rows = con.execute(
            "SELECT active_mode,movie_id,v16_rank,v5_rank,v16_final,v5_final FROM v5_trial_audit ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["active_mode"] == MODE_V5_20
    assert int(rows[0]["movie_id"]) == 2
    assert int(rows[0]["v16_rank"]) == 2
    assert int(rows[0]["v5_rank"]) == 1


def test_alpha_trial_switches_to_v16_instantly(tmp_path):
    db = Database(tmp_path / "trial-switch.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.75), _rec(2, 0.70)])
    v5 = FakeEngine([_rec(2, 0.80), _rec(1, 0.72)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    status = proxy.set_mode(MODE_V16)
    assert status["mode"] == MODE_V16
    recs = proxy.recommend(when=date(2026, 9, 27), count=2)
    assert [r.movie.id for r in recs] == [1, 2]
    assert recs[0].score.score_factors["v5_trial_active"] == 0.0


def test_alpha_trial_refuses_v5_without_eligible_report(tmp_path):
    db = Database(tmp_path / "trial-block.db")
    v16 = FakeEngine([_rec(1, 0.75)])
    v5 = FakeEngine([_rec(1, 0.80)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    assert proxy.trial_status()["mode"] == MODE_V16
    try:
        proxy.set_mode(MODE_V5_20)
    except RuntimeError:
        pass
    else:
        raise AssertionError("V5 trial should stay blocked without an eligible report")


def test_legacy_or_unverified_report_cannot_activate_v5(tmp_path):
    db = Database(tmp_path / "legacy.db")
    _eligible(db)
    report = db.get_setting("v5_evaluation_report")
    report.pop("rating_snapshot")
    db.set_setting("v5_evaluation_report", report)
    proxy = AlphaTrialRecommender(db, FakeEngine([_rec(1, .7)]), FakeEngine([_rec(2, .8)]))
    assert proxy.mode == MODE_V16
    assert proxy.trial_status()["report_current"] is None
    report["rating_snapshot"] = rating_history_snapshot(db)
    report["decision"].pop("visible_decision_guard_passed")
    db.set_setting("v5_evaluation_report", report)
    assert proxy.trial_status()["eligible"] is False


def test_model_switch_reuses_the_exact_same_frozen_pair(tmp_path):
    db = Database(tmp_path / "trial-frozen.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.75), _rec(2, 0.70), _rec(3, 0.68)])
    v5 = FakeEngine([_rec(2, 0.80), _rec(1, 0.72), _rec(3, 0.69)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    # First exposure computes both sides once.
    first = proxy.recommend(
        when=date(2026, 9, 27), count=3, slot="browse", candidate_limit=45000
    )
    assert [r.movie.id for r in first] == [2, 1, 3]
    assert v16.recommend_calls == 1
    assert v5.recommend_calls == 1

    with db.connect() as con:
        first_round = con.execute(
            "SELECT round_id FROM v5_trial_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()["round_id"]

    # Switching to V16 must not run either engine again.
    proxy.set_mode(MODE_V16)
    second = proxy.recommend(
        when=date(2026, 9, 27), count=3, slot="browse", candidate_limit=45000
    )
    assert [r.movie.id for r in second] == [1, 2, 3]
    assert v16.recommend_calls == 1
    assert v5.recommend_calls == 1

    with db.connect() as con:
        second_round = con.execute(
            "SELECT round_id FROM v5_trial_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()["round_id"]
    assert second_round == first_round


def test_explicit_invalidation_forces_a_fresh_pair(tmp_path):
    db = Database(tmp_path / "trial-recalc.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.75), _rec(2, 0.70)])
    v5 = FakeEngine([_rec(2, 0.80), _rec(1, 0.72)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    proxy.recommend(
        when=date(2026, 9, 27), count=2, slot="browse", candidate_limit=45000
    )
    proxy.set_mode(MODE_V16)
    proxy.invalidate_round("browse")
    proxy.recommend(
        when=date(2026, 9, 27), count=2, slot="browse", candidate_limit=45000
    )

    assert v16.recommend_calls == 2
    assert v5.recommend_calls == 2



def test_romanian_lane_uses_same_frozen_v16_v5_pair(tmp_path):
    db = Database(tmp_path / "trial-romanian.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.76), _rec(2, 0.72), _rec(3, 0.68)])
    v5 = FakeEngine([_rec(2, 0.81), _rec(1, 0.74), _rec(3, 0.69)])
    proxy = AlphaTrialRecommender(db, v16, v5)

    first = proxy.recommend_romanian(when=date(2026, 9, 28), count=3)
    assert [r.movie.id for r in first] == [2, 1, 3]
    assert v16.romanian_calls == 1
    assert v5.romanian_calls == 1
    assert first[0].score.score_factors["v5_trial_v16_rank"] == 2.0
    assert first[0].score.score_factors["v5_trial_v5_rank"] == 1.0

    proxy.set_mode(MODE_V16)
    second = proxy.recommend_romanian(when=date(2026, 9, 28), count=3)
    assert [r.movie.id for r in second] == [1, 2, 3]
    assert v16.romanian_calls == 1
    assert v5.romanian_calls == 1

    with db.connect() as con:
        rounds = con.execute(
            "SELECT DISTINCT round_id FROM v5_trial_audit WHERE slot='romanian'"
        ).fetchall()
    assert len(rounds) == 1


def test_plain_navigation_reuses_rounds_until_state_change_or_recalculate(tmp_path):
    db = Database(tmp_path / "trial-navigation.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.76), _rec(2, 0.72)])
    v5 = FakeEngine([_rec(2, 0.81), _rec(1, 0.74)])
    proxy = AlphaTrialRecommender(db, v16, v5)
    when = date(2026, 9, 28)

    for _ in range(3):
        proxy.recommend(when=when, count=2, slot="browse")
        proxy.recommend_romanian(when=when, count=2)
        proxy.decision_pick(when=when)
    assert (v16.recommend_calls, v5.recommend_calls) == (1, 1)
    assert (v16.romanian_calls, v5.romanian_calls) == (1, 1)
    assert (v16.decision_calls, v5.decision_calls) == (1, 1)

    proxy.invalidate_round("browse")
    proxy.recommend(when=when, count=2, slot="browse")
    assert (v16.recommend_calls, v5.recommend_calls) == (2, 2)
    assert (v16.romanian_calls, v5.romanian_calls) == (1, 1)

    db.set_setting("watchlist_decision_mode", "safe")
    # The trial's user state deliberately watches ratings/feedback/watchlist/profile,
    # not an unrelated preference setting. A rating change must still refresh it.
    with db.tx() as con:
        con.execute("INSERT INTO user_profile(profile_key,value_json,updated_at) VALUES(?,?,?)",
                    ('test', '{}', '2030-01-01T00:00:00+00:00'))
    proxy.recommend_romanian(when=when, count=2)
    assert (v16.romanian_calls, v5.romanian_calls) == (2, 2)


def test_decision_trial_refreshes_both_engines_when_chooser_changes(tmp_path):
    db = Database(tmp_path / "trial-chooser.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.76)])
    v5 = FakeEngine([_rec(2, 0.81)])
    proxy = AlphaTrialRecommender(db, v16, v5)
    when = date(2026, 9, 28)

    proxy.decision_pick(when=when)
    proxy.decision_pick(when=when)
    assert (v16.decision_calls, v5.decision_calls) == (1, 1)

    db.set_setting("daily_genre_filter", {"date": when.isoformat(), "genre": "Horror"})
    proxy.decision_pick(when=when)
    assert (v16.decision_calls, v5.decision_calls) == (2, 2)

    db.set_setting("chooser_mood", "intense")
    proxy.decision_pick(when=when)
    assert (v16.decision_calls, v5.decision_calls) == (3, 3)

    db.set_setting("chooser_runtime_bucket", "90")
    proxy.decision_pick(when=when)
    assert (v16.decision_calls, v5.decision_calls) == (4, 4)


def test_next_day_decision_avoids_recent_exposures_on_both_engines(tmp_path):
    db = Database(tmp_path / "trial-recency.db")
    _eligible(db)
    when = date(2026, 9, 30)
    now = utcnow_iso()
    with db.tx() as con:
        for mid in range(1, 7):
            con.execute(
                """INSERT INTO movies(id,imdb_id,identity_key,title,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (mid, f"tt{mid:07d}", f"test:{mid}", f"Film {mid}", now, now),
            )
        for mid, day in (
            (1, when - timedelta(days=1)),
            (2, when - timedelta(days=7)),
            (3, when - timedelta(days=8)),
            (4, when),
            (5, when - timedelta(days=1)),
        ):
            con.execute(
                """INSERT INTO recommendation_history(
                    movie_id,recommended_at,context_date,slot
                   ) VALUES(?,?,?,'decision')""",
                (mid, now, day.isoformat()),
            )
        con.execute(
            "INSERT INTO watchlist(movie_id,status,added_at,updated_at) VALUES(5,'want_to_watch',?,?)",
            (now, now),
        )

    v16 = FakeEngine([_rec(mid, 0.8) for mid in range(1, 7)])
    v5 = FakeEngine([_rec(mid, 0.8) for mid in range(1, 7)])
    proxy = AlphaTrialRecommender(db, v16, v5)
    first, backups = proxy.decision_pick(when=when, exclude_ids={3})
    assert [r.movie.id for r in [first, *backups]] == [4, 5, 6]
    assert v16.decision_exclusions == [{1, 2, 3}]
    assert v5.decision_exclusions == [{1, 2, 3}]

    # A newly imported prior-day exposure must refresh the trial pair.
    with db.tx() as con:
        con.execute(
            """INSERT INTO recommendation_history(
                movie_id,recommended_at,context_date,slot
               ) VALUES(?,?,?,'decision')""",
            (4, now, (when - timedelta(days=1)).isoformat()),
        )
    first, backups = proxy.decision_pick(when=when, exclude_ids={3})
    assert [r.movie.id for r in [first, *backups]] == [5, 6]
    assert v16.decision_exclusions[-1] == {1, 2, 3, 4}
    assert v5.decision_exclusions[-1] == {1, 2, 3, 4}


def test_skipped_movie_stays_excluded_today_after_restart_even_on_watchlist(tmp_path):
    db = Database(tmp_path / "trial-skipped.db")
    _eligible(db)
    when = date(2026, 9, 30)
    now = utcnow_iso()
    with db.tx() as con:
        for mid in range(1, 5):
            con.execute(
                """INSERT INTO movies(id,imdb_id,identity_key,title,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (mid, f"tt{mid:07d}", f"test:{mid}", f"Film {mid}", now, now),
            )
        con.execute(
            """INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,action)
               VALUES(?,?,?,?,?)""",
            (1, now, when.isoformat(), "decision_action", "skip_today"),
        )
        con.execute(
            "INSERT INTO watchlist(movie_id,status,added_at,updated_at) VALUES(1,'want_to_watch',?,?)",
            (now, now),
        )
    engines = lambda: (
        FakeEngine([_rec(mid, .8) for mid in range(1, 5)]),
        FakeEngine([_rec(mid, .8) for mid in range(1, 5)]),
    )
    v16, v5 = engines()
    first, backups = AlphaTrialRecommender(db, v16, v5).decision_pick(when=when)
    assert [r.movie.id for r in [first, *backups]] == [2, 3, 4]
    assert v16.decision_exclusions == [{1}]
    assert v5.decision_exclusions == [{1}]

    # A new process has an empty session_skips set, but the persisted action still wins.
    v16, v5 = engines()
    first, backups = AlphaTrialRecommender(db, v16, v5).decision_pick(when=when)
    assert [r.movie.id for r in [first, *backups]] == [2, 3, 4]
    assert v16.decision_exclusions == [{1}]

    # The skip is only for its calendar day; watchlist intent resumes tomorrow.
    v16, v5 = engines()
    first, _ = AlphaTrialRecommender(db, v16, v5).decision_pick(when=when + timedelta(days=1))
    assert first.movie.id == 1


def test_decision_forwards_contextual_session_to_both_engines(tmp_path):
    db = Database(tmp_path / "trial-context.db")
    v16, v5 = FakeEngine([_rec(1, .7)]), FakeEngine([_rec(2, .8)])
    AlphaTrialRecommender(db, v16, v5).decision_pick(
        when=date(2026, 9, 30), contextual_feedback=()
    )
    assert v16.decision_kwargs == [{"contextual_feedback": ()}]
    assert v5.decision_kwargs == [{"contextual_feedback": ()}]


def test_browse_trial_recomputes_both_sides_when_daily_genre_changes(tmp_path):
    db = Database(tmp_path / "trial-genre.db")
    _eligible(db)
    v16 = FakeEngine([_rec(1, 0.76)])
    v5 = FakeEngine([_rec(2, 0.81)])
    proxy = AlphaTrialRecommender(db, v16, v5)
    when = date(2026, 9, 28)

    proxy.recommend(when=when, count=1, slot="today-gallery")
    proxy.recommend(when=when, count=1, slot="today-gallery")
    assert (v16.recommend_calls, v5.recommend_calls) == (1, 1)

    db.set_setting("daily_genre_filter", {"date": when.isoformat(), "genre": "Horror"})
    proxy.recommend(when=when, count=1, slot="today-gallery")
    assert (v16.recommend_calls, v5.recommend_calls) == (2, 2)

    # A filter for another date must not invalidate today's frozen comparison.
    db.set_setting("daily_genre_filter", {"date": "2026-09-29", "genre": "Western"})
    proxy.recommend(when=when, count=1, slot="today-gallery")
    assert (v16.recommend_calls, v5.recommend_calls) == (3, 3)
    proxy.recommend(when=when, count=1, slot="today-gallery")
    assert (v16.recommend_calls, v5.recommend_calls) == (3, 3)


def test_trial_detects_changed_ratings_after_a_fingerprinted_report(tmp_path):
    db = Database(tmp_path / "trial-history.db")
    _eligible(db)
    report = db.get_setting("v5_evaluation_report")
    report["rating_snapshot"] = rating_history_snapshot(db)
    db.set_setting("v5_evaluation_report", report)
    v16 = FakeEngine([_rec(1, 0.75)])
    v5 = FakeEngine([_rec(2, 0.82)])
    proxy = AlphaTrialRecommender(db, v16, v5)
    assert proxy.mode == MODE_V5_20
    assert report_rating_freshness(db, report) is True

    now = utcnow_iso()
    with db.tx() as con:
        movie = con.execute(
            """INSERT INTO movies(imdb_id,identity_key,title,original_title,year,title_type,
                 genres_json,directors_json,countries_json,keywords_json,semantic_json,
                 source,created_at,updated_at,title_norm,original_title_norm)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("tt0000902", "test:rating", "Test", "Test", 2020, "movie",
             json_dumps([]), json_dumps([]), json_dumps([]), json_dumps([]),
             json_dumps({}), "test", now, now, "test", "test"),
        ).lastrowid
        con.execute(
            "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
            (movie, 9, "2026-09-28", "test", now, now),
        )

    assert report_rating_freshness(db, report) is False
    assert proxy.mode == MODE_V16
    assert proxy.trial_status()["eligible"] is False
    assert proxy.trial_status()["report_current"] is False
