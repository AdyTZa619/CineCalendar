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
    assert 1300 <= int(neutral_job["priority"]) < 1600


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


def test_v5_knowledge_prioritizes_undercovered_negative_boundary(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")

    # Positive class already has richer premise/country data.
    _rated(db,101,9,overview="Positive rich premise",countries=("Romania",))
    _rated(db,102,8,overview="Another positive premise",countries=("France",))
    # Negative class is intentionally sparse.
    neg1=_rated(db,103,2)
    neg2=_rated(db,104,4)

    result=V5KnowledgeBase(db).seed_profile()
    assert result["priority_focus"] == "negative"
    assert result["negative_coverage"] < result["positive_coverage"]

    with db.connect() as con:
        priorities={
            int(row["movie_id"]): int(row["priority"])
            for row in con.execute(
                "SELECT movie_id,priority FROM metadata_jobs WHERE movie_id IN (?,?,?,?)",
                (neg1,neg2,1,2),
            ).fetchall()
        }
        rows=con.execute(
            "SELECT m.id,j.priority,r.rating FROM metadata_jobs j "
            "JOIN movies m ON m.id=j.movie_id JOIN ratings r ON r.movie_id=m.id "
            "WHERE r.rating IN (2,4,8,9) ORDER BY j.priority DESC"
        ).fetchall()

    assert rows
    # At least one negative example must outrank every positive example when its class is far less covered.
    neg_priorities=[int(row["priority"]) for row in rows if int(row["rating"]) <= 4]
    pos_priorities=[int(row["priority"]) for row in rows if int(row["rating"]) >= 8]
    assert max(neg_priorities) > max(pos_priorities)


def test_v5_priority_missing_fields_increase_information_gain_priority():
    sparse=V5KnowledgeBase._priority(2,class_coverage=0.2,missing_count=5)
    almost_complete=V5KnowledgeBase._priority(2,class_coverage=0.2,missing_count=1)
    assert sparse > almost_complete


def test_metadata_doctor_reprioritizes_v5_taste_boundary_automatically():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    source=(root/"cinecalendar"/"qt_ui.py").read_text(encoding="utf-8")
    assert "V5KnowledgeBase(self.db)" in source
    assert "seed_profile(informative_only=True)" in source
    assert "batch_limit = 50 if not silent else 24" in source
    assert "batch_limit = 25 if not silent else 8" in source
    assert "V5 — datele profilului personal" in source
    assert "Auto-completarea V5 este activă" in source
    assert "75 * 1000" in source
    assert "5 * 60 * 1000" in source
    assert "is_v5_alpha()" in source
    assert "process_metadata_queue(" in source
