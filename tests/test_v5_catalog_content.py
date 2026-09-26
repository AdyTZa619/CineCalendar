from cinecalendar.db import Database
from cinecalendar.full_catalog_shadow_v414 import FullCatalogCandidateGeneratorV414
from cinecalendar.recommendation import row_to_movie
from cinecalendar.semantic import feature_vector
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso
from cinecalendar.v5_catalog_content import V5CatalogContentRetriever


class _MappedCollaborative:
    def has_mapping(self, imdb_id):
        return True


def _row(db: Database):
    now=utcnow_iso()
    with db.tx() as con:
        cur=con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,runtime_min,genres_json,directors_json,countries_json,
                overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt7654321",identity_key("Mapped good film","Mapped good film",2015,"movie"),
                "Mapped good film","Mapped good film",
                normalize_text("Mapped good film"),normalize_text("Mapped good film"),
                2015,"movie",112,json_dumps(["Drama","History"]),
                json_dumps(["Director Test"]),json_dumps(["Romania"]),
                "A historical drama about faith and family.",json_dumps(["faith","history"]),
                json_dumps({}),7.8,15000,"test",now,now,
            ),
        )
        movie_id=int(cur.lastrowid)
    with db.connect() as con:
        return con.execute("SELECT * FROM movies WHERE id=?",(movie_id,)).fetchone()


def test_v5_content_can_rescue_title_even_when_als_mapping_exists(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    row=_row(db)
    movie=row_to_movie(row)
    vector=feature_vector(movie)
    profile={
        "features":{
            feature:{"preference":0.85,"count":8}
            for feature in vector
        }
    }
    collaborative=_MappedCollaborative()

    legacy=FullCatalogCandidateGeneratorV414(db,collaborative)
    v5=V5CatalogContentRetriever(db,collaborative)

    assert legacy._score_rows([row],profile) == []
    result=v5._score_rows([row],profile)
    assert len(result) == 1
    assert result[0].movie_id == int(row["id"])
    assert result[0].personal_score > 0
    assert result[0].matched_features >= 2
