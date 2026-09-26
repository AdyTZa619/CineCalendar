from datetime import date

from cinecalendar.db import Database
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso
from cinecalendar.v5_online_discovery import V5OnlineDiscovery


class _FakeTmdbProvider:
    def __init__(self, db, token):
        self.db=db
        self.token=token

    def related_movie_ids(self, tmdb_id, limit=20):
        assert int(tmdb_id) == 101
        return [202][:limit]

    def movie_details_by_tmdb(self, tmdb_id):
        assert int(tmdb_id) == 202
        return {
            "id":202,
            "adult":False,
            "title":"Online Candidate",
            "original_title":"Online Candidate",
            "release_date":"2024-03-01",
            "runtime":111,
            "overview":"A historical drama about faith, war and family.",
            "genres":[{"name":"Drama"},{"name":"History"}],
            "production_countries":[{"name":"Romania"}],
            "credits":{"crew":[{"job":"Director","name":"Director Online"}]},
            "keywords":{"keywords":[{"name":"faith"},{"name":"history"}]},
            "external_ids":{"imdb_id":"tt9900202"},
            "poster_path":"/poster.jpg",
        }


def _seed_anchor(db: Database):
    now=utcnow_iso()
    with db.tx() as con:
        cur=con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,runtime_min,genres_json,directors_json,countries_json,
                overview,keywords_json,semantic_json,imdb_rating,num_votes,release_date,
                poster_url,source,created_at,updated_at,tmdb_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9900101",identity_key("Anchor","Anchor",2020,"movie"),
                "Anchor","Anchor",normalize_text("Anchor"),normalize_text("Anchor"),
                2020,"movie",100,json_dumps(["Drama"]),json_dumps(["Anchor Director"]),
                json_dumps(["Romania"]),"Anchor overview",json_dumps([]),json_dumps({}),
                8.0,10000,"2020-01-01",None,"test",now,now,101,
            ),
        )
        movie_id=int(cur.lastrowid)
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id,9,"2025-01-01","test",now,now),
        )


def test_v5_online_discovery_imports_new_candidate_and_caches_it(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    db.set_setting("tmdb_token","test-token")
    _seed_anchor(db)

    discovery=V5OnlineDiscovery(db,provider_factory=_FakeTmdbProvider)
    status=discovery.refresh(force=True,anchor_limit=4,related_per_anchor=5,import_limit=10)

    assert status["state"] == "ready"
    assert status["anchors"] == 1
    assert status["imported"] == 1

    with db.connect() as con:
        row=con.execute(
            "SELECT id,source,tmdb_id,overview FROM movies WHERE imdb_id='tt9900202'"
        ).fetchone()
    assert row is not None
    assert row["source"] == "v5_online_tmdb"
    assert int(row["tmdb_id"]) == 202
    assert "historical drama" in row["overview"]

    current=discovery.cached_candidate_ids(limit=20)
    assert int(row["id"]) in current


def test_v5_online_discovery_does_not_leak_current_discovery_into_past_replay(tmp_path):
    db=Database(tmp_path/"cinecalendar.db")
    db.set_setting("tmdb_token","test-token")
    _seed_anchor(db)

    discovery=V5OnlineDiscovery(db,provider_factory=_FakeTmdbProvider)
    discovery.refresh(force=True)

    assert discovery.cached_candidate_ids(when=date(2020,1,1),limit=20) == []
    assert len(discovery.cached_candidate_ids(when=date.today(),limit=20)) == 1
