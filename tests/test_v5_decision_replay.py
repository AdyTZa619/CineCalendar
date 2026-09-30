from cinecalendar.db import Database
from cinecalendar.profile import get_profile
from cinecalendar.rolling_backtest_v37 import TemporalWindowV37, _remove_future
from cinecalendar.recommendation_backtest import HoldoutRating
from cinecalendar.util import json_dumps, utcnow_iso
from cinecalendar.v5_decision_replay import decision_replay_guard


def test_historical_decision_rebuilds_profile_and_hides_same_day_rating(tmp_path):
    db = Database(tmp_path / "replay.db")
    now = utcnow_iso()
    with db.tx() as con:
        for mid, day in ((1, "2026-01-01"), (2, "2026-02-02"), (3, "2026-02-02")):
            con.execute(
                """INSERT INTO movies(id,imdb_id,identity_key,title,original_title,year,title_type,
                genres_json,directors_json,countries_json,keywords_json,semantic_json,
                source,created_at,updated_at,title_norm,original_title_norm)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, f"tt{mid:07d}", f"test:{mid}", f"Film {mid}", f"Film {mid}",
                 2020, "movie", json_dumps([]), json_dumps([]), json_dumps([]),
                 json_dumps([]), json_dumps({}), "test", now, now, "film", "film"),
            )
            con.execute(
                "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                (mid, 9, day, "test", now, now),
            )
    assert get_profile(db)["rated_count"] == 3
    db.set_setting("decision_pool_v16:2026-02-02:decide", {"movie_ids": [2, 3]})
    window = TemporalWindowV37(
        1, 2, (HoldoutRating(3, "tt0000003", 9, "2026-02-02"),),
        (3,), "2026-02-02",
    )
    _remove_future(db, window)
    with db.connect() as con:
        assert [row[0] for row in con.execute("SELECT movie_id FROM ratings")] == [1]
    assert get_profile(db)["rated_count"] == 1
    assert db.get_setting("decision_pool_v16:2026-02-02:decide") is None


def test_visible_decision_needs_evidence_and_a_positive_gain():
    def fold(v16, v5):
        return {
            "v16": {"matched": [{"rating": x} for x in v16]},
            "v5_20": {"matched": [{"rating": x} for x in v5]},
        }

    sparse = decision_replay_guard([fold([9], [9]), fold([], [])])
    assert sparse["informative"] is False
    assert sparse["passed"] is False
    better = decision_replay_guard([fold([6], [9, 8]), fold([8], [9])])
    assert better["informative"] is True
    assert better["passed"] is True
    worse = decision_replay_guard([fold([9], [9, 3]), fold([8], [8])])
    assert worse["passed"] is False
