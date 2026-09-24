from __future__ import annotations

from cinecalendar.db import Database, SCHEMA_VERSION
from cinecalendar.backup import PROFILE_VERSION, export_profile, import_profile
from cinecalendar.full_catalog_shadow_v413 import (
    FULL_CATALOG_RETRIEVAL_VERSION,
    FullCatalogCandidateGeneratorV413,
    FullCatalogShadowEvaluatorV413,
)
from cinecalendar.profile import build_profile
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


class _Collaborative:
    def __init__(self, mapped=()):
        self.mapped = set(mapped)

    def is_ready(self):
        return True

    def state_token(self):
        return ("ready", "test-model")

    def has_mapping(self, imdb_id):
        return str(imdb_id or "") in self.mapped


def _movie(
    db: Database,
    idx: int,
    title: str,
    *,
    director="Director Bun",
    genres=("Crime", "Thriller"),
    country="Romania",
    year=2020,
    runtime=105,
    semantic=None,
):
    now = utcnow_iso()
    imdb_id = f"tt{9800000+idx:07d}"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, identity_key(title,title,year,"movie"), title, title,
                normalize_text(title), normalize_text(title), year, "movie", runtime,
                json_dumps(list(genres)), json_dumps([director]), json_dumps([country]),
                "A crime investigation and a dark mystery.", json_dumps(["investigation"]),
                json_dumps(semantic or {"crime": 1.0, "dark": .5}), 7.3, 12000,
                "test", now, now,
            ),
        )
        return int(cur.lastrowid), imdb_id


def _rating(db: Database, movie_id: int, rating: int, day="2026-08-01"):
    now = day + "T20:00:00+00:00"
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id,rating,day,"test",now,now),
        )


def _trained_db(tmp_path):
    db = Database(tmp_path / "catalog.db")
    rated=[]
    for idx, score in enumerate((9,9,8),1):
        mid,_iid=_movie(db,idx,f"Favorite {idx}")
        _rating(db,mid,score)
        rated.append(mid)
    build_profile(db)
    return db


def test_schema_v12_creates_shadow_measurement_tables(tmp_path):
    db=Database(tmp_path / "schema.db")
    with db.connect() as con:
        tables={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version=con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert SCHEMA_VERSION == 12
    assert version == 12
    assert {"retrieval_shadow_runs","retrieval_shadow_items"}.issubset(tables)


def test_full_catalog_retrieval_finds_unmapped_match_and_excludes_mapped_title(tmp_path):
    db=_trained_db(tmp_path)
    unmapped,_=_movie(db,20,"Unmapped match")
    mapped,mapped_iid=_movie(db,21,"Mapped match")
    weak,_=_movie(
        db,22,"Weak mismatch",director="Alt Director",genres=("Comedy",),country="Japan",
        year=1982,runtime=170,semantic={"humor":1.0},
    )
    generator=FullCatalogCandidateGeneratorV413(db,_Collaborative({mapped_iid}))

    results=generator.candidates(20)
    ids=[item.movie_id for item in results]

    assert unmapped in ids
    assert mapped not in ids
    assert weak not in ids
    assert generator.status()["version"] == FULL_CATALOG_RETRIEVAL_VERSION
    assert generator.status()["non_als_candidates"] >= 1


def test_shadow_run_persists_counterfactual_without_mutating_baseline(tmp_path):
    db=_trained_db(tmp_path)
    baseline,_=_movie(db,30,"Baseline",director="Other",genres=("Drama",),country="France")
    challenger,_=_movie(db,31,"Shadow candidate")
    evaluator=FullCatalogShadowEvaluatorV413(db,_Collaborative(),"baseline-engine")
    original=[baseline]

    result=evaluator.run_once("2026-09-20","browse",original)

    assert result["state"] == "recorded"
    assert original == [baseline]
    with db.connect() as con:
        rows=con.execute(
            "SELECT source,movie_id FROM retrieval_shadow_items WHERE run_id=? ORDER BY source",
            (result["run_id"],),
        ).fetchall()
    assert {(row["source"],int(row["movie_id"])) for row in rows} == {
        ("baseline",baseline),("challenger",challenger)
    }


def test_same_shadow_comparison_is_recorded_only_once(tmp_path):
    db=_trained_db(tmp_path)
    baseline,_=_movie(db,40,"Baseline",director="Other",genres=("Drama",),country="France")
    _movie(db,41,"Shadow candidate")
    evaluator=FullCatalogShadowEvaluatorV413(db,_Collaborative(),"baseline-engine")

    first=evaluator.run_once("2026-09-20","browse",[baseline])
    second=evaluator.run_once("2026-09-20","browse",[baseline])

    assert first["state"] == "recorded"
    assert second["state"] == "already_recorded"
    assert evaluator.status()["runs"] == 1


def test_shadow_metrics_use_only_ratings_after_the_comparison_date(tmp_path):
    db=_trained_db(tmp_path)
    baseline,_=_movie(db,50,"Baseline",director="Other",genres=("Drama",),country="France")
    challenger,_=_movie(db,51,"Shadow candidate")
    evaluator=FullCatalogShadowEvaluatorV413(db,_Collaborative(),"baseline-engine")
    evaluator.run_once("2026-09-20","browse",[baseline])
    _rating(db,baseline,4,"2026-09-19")
    _rating(db,challenger,9,"2026-09-21")

    status=evaluator.status()

    assert status["baseline"]["rated"] == 0
    assert status["challenger"]["rated"] == 1
    assert status["challenger"]["average_rating"] == 9
    assert status["production_unchanged"] is True


def test_profile_backup_preserves_shadow_evidence(tmp_path):
    source=_trained_db(tmp_path / "source")
    baseline,_=_movie(source,60,"Baseline",director="Other",genres=("Drama",),country="France")
    _movie(source,61,"Shadow candidate")
    evaluator=FullCatalogShadowEvaluatorV413(source,_Collaborative(),"baseline-engine")
    assert evaluator.run_once("2026-09-20","browse",[baseline])["state"] == "recorded"
    archive=export_profile(source,tmp_path / "profile.zip")
    target=Database(tmp_path / "target.db")

    result=import_profile(target,archive,mode="restore")

    assert PROFILE_VERSION == 5
    assert result["shadow_runs"] == 1
    assert result["shadow_items"] == 2
    with target.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM retrieval_shadow_runs").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM retrieval_shadow_items").fetchone()[0] == 2
