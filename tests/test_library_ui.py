from cinecalendar.db import Database
from cinecalendar.library_ui import load_rating_rows, _feature_category, _pretty_feature
from cinecalendar.util import json_dumps, utcnow_iso


def test_rating_library_returns_all_rows_without_500_limit(tmp_path):
    db = Database(tmp_path / "library.db")
    now = utcnow_iso()
    with db.tx() as con:
        movie_rows = []
        for i in range(525):
            movie_rows.append((
                f"tt{i+1:07d}", f"movie-{i}", f"Film {i}", f"Film {i}", 2000 + (i % 25),
                "movie", 100, json_dumps(["Drama"]), json_dumps(["Director Test"]), json_dumps(["US"]),
                "", json_dumps([]), json_dumps({}), 7.0, 1000, None, None, "test", now, now,
                f"film {i}", f"film {i}",
            ))
        con.executemany(
            """
            INSERT INTO movies(
                imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                imdb_rating,num_votes,release_date,poster_url,source,created_at,updated_at,
                title_norm,original_title_norm
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            movie_rows,
        )
        ids = con.execute("SELECT id FROM movies ORDER BY id").fetchall()
        con.executemany(
            "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
            [(row[0], 8, "2026-09-15", "imdb", now, now) for row in ids],
        )

    rows = load_rating_rows(db)
    assert len(rows) == 525
    assert rows[0]["user_rating"] == 8
    assert rows[0]["genres"] == ["Drama"]
    assert rows[0]["directors"] == ["Director Test"]


def test_profile_feature_labels_and_categories_are_human_readable():
    assert _feature_category("genre:western") == "genres"
    assert _feature_category("director:quentin tarantino") == "directors"
    assert _feature_category("theme:cross_veneration") == "themes"
    assert _feature_category("combo:director:quentin tarantino|genre:crime") == "combos"
    assert _pretty_feature("genre:western") == "Western"
    assert _pretty_feature("theme:cross_veneration") == "Cross Veneration"
    assert _pretty_feature("combo:director:quentin tarantino|genre:crime") == "Quentin Tarantino + Crime"
