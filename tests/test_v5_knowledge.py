from cinecalendar.db import Database
from cinecalendar.util import identity_key, json_dumps, utcnow_iso
from cinecalendar.v5_knowledge import V5KnowledgeBase


def _rated(db, idx: int, rating: int, *, overview="", countries=()):
    now = utcnow_iso()
    title = f"Movie {idx}"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                 imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                 year,title_type,genres_json,directors_json,countries_json,overview,
                 keywords_json,semantic_json,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{8800000+idx:07d}", identity_key(title,title,2020,"movie"),
                title,title,title.lower(),title.lower(),2020,"movie",
                json_dumps(["Drama"]),json_dumps(["Director"]),json_dumps(list(countries)),
                overview,json_dumps([]),json_dumps({}),"test",now,now,
            ),
        )
        movie_id = int(cur.lastrowid)
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id,rating,"2026-01-01","test",now,now),
        )
    return movie_id


def test_v5_knowledge_prioritizes_informative_ratings(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    liked=_rated(db,1,9)
    neutral=_rated(db,2,6)
    disliked=_rated(db,3,2)

    knowledge=V5KnowledgeBase(db)
    result=knowledge.seed_profile()
    assert result["informative_only"] is True
    assert result["considered_missing"] == 2

    with db.connect() as con:
        rows=con.execute(
            "SELECT movie_id,priority,reason FROM metadata_jobs ORDER BY priority DESC"
        ).fetchall()
    priority={int(row["movie_id"]):int(row["priority"]) for row in rows}
    assert liked in priority
    assert disliked in priority
    assert neutral not in priority
    assert all(str(row["reason"])=="v5_rated_profile" for row in rows)

    all_result=knowledge.seed_profile(informative_only=False)
    assert all_result["considered_missing"] == 3
    with db.connect() as con:
        neutral_job=con.execute(
            "SELECT priority FROM metadata_jobs WHERE movie_id=?",(neutral,)
        ).fetchone()
    assert int(neutral_job["priority"]) == 1300


def test_v5_knowledge_report_measures_factual_readiness(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    _rated(db,10,9,overview="Rich premise",countries=("Romania",))
    _rated(db,11,2)
    _rated(db,12,6,overview="Neutral premise",countries=("France",))

    status=V5KnowledgeBase(db).status()
    assert status["rated_total"] == 3
    assert status["informative_total"] == 2
    assert status["semantic_total"] == 2
    assert status["country_total"] == 2
    assert status["informative_semantic"] == 1
    assert status["informative_country"] == 1
    assert status["positive_total"] == 1
    assert status["negative_total"] == 1
    assert status["positive_semantic_coverage"] == 1.0
    assert status["negative_semantic_coverage"] == 0.0
    assert status["ready_for_rich_ranker"] is False


def test_v5_frontier_uses_separate_reason(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    movie_id=_rated(db,20,7)
    knowledge=V5KnowledgeBase(db)
    assert knowledge.queue_frontier([movie_id]) == 1
    with db.connect() as con:
        row=con.execute("SELECT reason,priority FROM metadata_jobs WHERE movie_id=?",(movie_id,)).fetchone()
    assert row["reason"] == "v5_candidate_frontier"
    assert int(row["priority"]) == 1450
