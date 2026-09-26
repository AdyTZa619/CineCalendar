from cinecalendar.db import Database
from cinecalendar.util import identity_key, utcnow_iso
from cinecalendar.v5_event_replay import aggregate_event_reports, event_replay_windows


def _rating(db, idx, day, rating):
    now=utcnow_iso(); title=f"Movie {idx}"
    with db.tx() as con:
        cur=con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,countries_json,overview,
                keywords_json,semantic_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'[]','[]','','[]','{}','test',?,?)""",
            (
                f"tt{8700000+idx:07d}",identity_key(title,title,2020,"movie"),
                title,title,title.lower(),title.lower(),2020,"movie","[\"Drama\"]",now,now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(cur.lastrowid),rating,day,"test",now,now),
        )


def test_event_replay_hides_whole_day_and_future(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    for idx in range(80):
        _rating(db,idx,f"2024-01-{1+(idx%20):02d}",6)
    _rating(db,100,"2025-01-10",8)
    _rating(db,101,"2025-01-10",4)
    _rating(db,102,"2025-01-11",9)

    selection=event_replay_windows(db,desired_days=2,minimum_train=50)
    window=selection.windows[0]
    assert window.cutoff_date == "2025-01-10"
    assert {item.rating for item in window.holdout} == {8,4}
    assert len(window.future_rating_ids) == 3


def test_event_replay_selection_is_deterministic_and_informative(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    for idx in range(100):
        day=f"2024-{1+(idx//28):02d}-{1+(idx%28):02d}"
        _rating(db,idx,day,6)
    for idx,day in enumerate(("2025-01-01","2025-02-01","2025-03-01","2025-04-01"),200):
        _rating(db,idx,day,8 if idx%2==0 else 3)

    a=event_replay_windows(db,desired_days=3,minimum_train=80)
    b=event_replay_windows(db,desired_days=3,minimum_train=80)
    assert a.selected_days == b.selected_days
    assert len(a.windows) == 3


def test_event_report_aggregate_uses_target_counts():
    reports=[
        {
            "candidate_recall":{
                "liked_8_plus":2,"loved_9_plus":1,"disliked_4_minus":1,
                "recall_8_plus_at_500":.5,"recall_9_plus_at_500":1.0,
                "dislike_recall_at_500":0.0,
            },
            "final_ranking":{
                "liked_8_plus":2,"loved_9_plus":1,"disliked_4_minus":1,
                "recall_8_plus_at_10":.5,"recall_9_plus_at_10":0.0,
                "dislike_recall_at_10":0.0,
            },
        },
        {
            "candidate_recall":{
                "liked_8_plus":1,"loved_9_plus":0,"disliked_4_minus":2,
                "recall_8_plus_at_500":1.0,"recall_9_plus_at_500":0.0,
                "dislike_recall_at_500":.5,
            },
            "final_ranking":{
                "liked_8_plus":1,"loved_9_plus":0,"disliked_4_minus":2,
                "recall_8_plus_at_10":0.0,"recall_9_plus_at_10":0.0,
                "dislike_recall_at_10":0.0,
            },
        },
    ]
    out=aggregate_event_reports(reports)
    assert out["liked_8_plus"] == 3
    assert out["candidate_8_plus_hits"] == 2
    assert out["candidate_dislike_hits"] == 1
    assert out["candidate_8_plus_recall"] == 0.666667
