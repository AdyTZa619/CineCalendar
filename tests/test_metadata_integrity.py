def test_recommendation_ignores_movie_records_that_fail_basic_validation():
    from datetime import date

    from cinecalendar.db import Database
    from cinecalendar.models import Movie
    from cinecalendar.recommendation import RecommendationEngine

    db = Database("/tmp/cinecalendar_invalid_rec_test.db")
    with db.tx() as con:
        con.execute(
            """INSERT INTO movies(
                imdb_id, identity_key, title, original_title, title_norm, original_title_norm,
                year, title_type, runtime_min, genres_json, directors_json, countries_json,
                overview, keywords_json, semantic_json, imdb_rating, num_votes, source,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt-invalid-1",
                "invalid|unknown|0|movie",
                "unknown",
                "unknown",
                "unknown",
                "unknown",
                0,
                "movie",
                0,
                "[]",
                "[]",
                "[]",
                "",
                "[]",
                "{}",
                None,
                0,
                "test",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        con.execute(
            """INSERT INTO movies(
                imdb_id, identity_key, title, original_title, title_norm, original_title_norm,
                year, title_type, runtime_min, genres_json, directors_json, countries_json,
                overview, keywords_json, semantic_json, imdb_rating, num_votes, source,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt-valid-1",
                "valid|the matrix|1999|movie",
                "The Matrix",
                "The Matrix",
                "the matrix",
                "the matrix",
                1999,
                "movie",
                136,
                '["Sci-Fi"]',
                '["The Wachowskis"]',
                '["United States"]',
                "A hacker learns reality is a simulation.",
                '["cyberpunk"]',
                '{"genre:sci-fi": 0.9, "director:the wachowskis": 0.8}',
                8.7,
                120000,
                "test",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )

    engine = RecommendationEngine(db)
    recs = engine.recommend(when=date(2026, 1, 2), count=3, candidate_limit=50)
    titles = [r.movie.title for r in recs]

    assert "unknown" not in titles
    assert "The Matrix" in titles
