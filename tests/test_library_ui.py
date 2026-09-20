from cinecalendar.db import Database
from cinecalendar.library_ui import load_rating_rows, _display_title, _rating_sort_key, _feature_category, _pretty_feature
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


def test_ratings_default_to_latest_first_and_show_original_title(tmp_path):
    db = Database(tmp_path / "recent.db")
    now = utcnow_iso()
    with db.tx() as con:
        rows = [
            ("tt0000001", "localized-1", "The Lives of Others", "Das Leben der Anderen", 2006, "movie", '["Drama"]', '["Florian Henckel von Donnersmarck"]'),
            ("tt0000002", "older-2", "Older Localized", "Older Original", 2001, "movie", '["Drama"]', '["Director Old"]'),
        ]
        for imdb_id, ident, title, original, year, typ, genres, directors in rows:
            cur = con.execute(
                """INSERT INTO movies(
                    imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                    year,title_type,genres_json,directors_json,source,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (imdb_id, ident, title, original, title.lower(), original.lower(),
                 year, typ, genres, directors, "test", now, now),
            )
            rated_date = "2026-09-20" if imdb_id == "tt0000001" else "2026-09-19"
            con.execute(
                "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                (int(cur.lastrowid), 9, rated_date, "imdb", now, now),
            )

    rows = load_rating_rows(db)
    assert rows[0]["date_rated"] == "2026-09-20"
    assert _display_title(rows[0]) == "Das Leben der Anderen"

    sorted_rows = sorted(
        reversed(rows),
        key=lambda row: _rating_sort_key(row, "date_desc"),
        reverse=True,
    )
    assert [row["date_rated"] for row in sorted_rows] == ["2026-09-20", "2026-09-19"]
