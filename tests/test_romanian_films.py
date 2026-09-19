from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.romanian_films import (
    backfill_romanian_posters,
    prepare_romanian_library,
    resolve_romanian_catalog_links,
    romanian_chapters,
    romanian_films,
)


def test_curated_romanian_model_has_all_233_rows():
    chapters = romanian_chapters()
    films = romanian_films()
    assert len(chapters) == 12
    assert len(films) == 233
    assert sum(int(ch["count"]) for ch in chapters) == 233
    assert films[0].film == "The Sins of Adam"
    assert films[-1].film == "Amar"


def test_imdb_rating_marks_exact_title_and_year_watched(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    add_manual_rating(db, "Mircea", 1989, 8, imdb_id="tt0097889")

    item = next(x for x in romanian_films(db) if x.film == "Mircea (1989)")
    assert item.watched is True
    assert item.user_rating == 8
    assert item.imdb_id == "tt0097889"


def test_bilingual_alias_matches_romanian_imdb_title(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    add_manual_rating(db, "Cu mâinile curate", 1972, 9, imdb_id="tt0068434")

    item = next(x for x in romanian_films(db) if x.film.startswith("With Clean Hands"))
    assert item.watched is True
    assert item.user_rating == 9


def test_same_title_wrong_year_does_not_remove_from_to_watch(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    add_manual_rating(db, "Mircea", 2024, 7, imdb_id="tt9999991")

    item = next(x for x in romanian_films(db) if x.film == "Mircea (1989)")
    assert item.watched is False


def test_production_sidebar_exposes_curated_romanian_list():
    from cinecalendar.qt_ui_v2 import DecisionWindow

    keys = [key for key, _label in DecisionWindow.NAV]
    assert "romanian_list" in keys
    assert callable(getattr(DecisionWindow, "page_romanian_list", None))


def test_premium_composition_installs_cinematic_romanian_list():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    composition = (root / "cinecalendar" / "ui_composition.py").read_text(encoding="utf-8")
    patch = (root / "cinecalendar" / "romanian_list_ui_patch.py").read_text(encoding="utf-8")

    assert "install_romanian_list_ui_patch(window_cls)" in composition
    assert "QScrollArea" in patch
    assert "poster_label" in patch
    assert "QTableWidget" not in patch
    assert "FILME ROMÂNEȘTI • CRONOLOGIA ACȚIUNII" in patch


def test_unwatched_local_match_exposes_poster_metadata(tmp_path):
    from cinecalendar.util import identity_key, normalize_text, utcnow_iso

    db = Database(tmp_path / "cinecalendar.db")
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,year,title_type,
                genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                imdb_rating,num_votes,poster_url,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt0097889", identity_key("Mircea", "Mircea", 1989, "movie"),
                "Mircea", "Mircea", normalize_text("Mircea"), normalize_text("Mircea"),
                1989, "movie", "[]", "[]", "[]", "", "[]", "{}",
                7.4, 1000, "https://example.invalid/mircea.jpg", "test", now, now,
            ),
        )

    item = next(x for x in romanian_films(db) if x.film == "Mircea (1989)")
    assert item.watched is False
    assert item.local_movie_id is not None
    assert item.poster_url == "https://example.invalid/mircea.jpg"
    assert item.imdb_rating == 7.4


def test_sidebar_names_romanian_section_as_chronology():
    from cinecalendar.qt_ui_v2 import DecisionWindow

    labels = dict(DecisionWindow.NAV)
    assert labels["romanian_list"] == "Filme RO • Cronologie"


def test_cinematic_romanian_page_has_search_progress_and_next_up():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    patch = (root / "cinecalendar" / "romanian_list_ui_patch.py").read_text(encoding="utf-8")
    assert "Cronologia filmului românesc" in patch
    assert "QProgressBar" in patch
    assert "romanian_list_search" in patch
    assert "URMĂTORUL CRONOLOGIC" in patch
    assert "Caută titlu, perioadă sau context" in patch


def test_silent_imdb_sync_failure_keeps_local_fallback_visible():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ui = (root / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    assert "sincronizarea IMDb este temporar indisponibilă; folosesc datele locale" in ui
    assert "imdb_public_sync_last_error" in ui
    assert "Detaliu tehnic:" in ui


def test_existing_romanian_page_is_clearly_named_recommendations():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    patch = (root / "cinecalendar" / "romanian_cinema_ui_patch.py").read_text(encoding="utf-8")
    assert '("romanian", "Recomandări românești")' in patch


def test_romanian_posters_are_backfilled_automatically(tmp_path, monkeypatch):
    from cinecalendar.util import identity_key, normalize_text, utcnow_iso

    db = Database(tmp_path / "cinecalendar.db")
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,year,title_type,
                genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                imdb_rating,num_votes,poster_url,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt0097889", identity_key("Mircea", "Mircea", 1989, "movie"),
                "Mircea", "Mircea", normalize_text("Mircea"), normalize_text("Mircea"),
                1989, "movie", "[]", "[]", "[]", "", "[]", "{}",
                7.4, 1000, None, "test", now, now,
            ),
        )

    class ImdbResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "data": {
                    "titles": [{
                        "id": "tt0097889",
                        "primaryImage": {"url": "https://m.media-amazon.com/images/M/mircea.jpg"},
                        "ratingsSummary": {"aggregateRating": 7.8},
                    }]
                }
            }

    monkeypatch.setattr(
        "cinecalendar.romanian_films.requests.Session.post",
        lambda self, *args, **kwargs: ImdbResponse(),
    )

    result = backfill_romanian_posters(db)
    assert result["filled"] == 1
    assert result["imdb"] == 1

    item = next(x for x in romanian_films(db) if x.film == "Mircea (1989)")
    assert item.poster_url == "https://m.media-amazon.com/images/M/mircea.jpg"
    assert item.imdb_rating == 7.8


def test_romanian_ui_starts_poster_backfill_without_manual_action():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    patch = (root / "cinecalendar" / "romanian_list_ui_patch.py").read_text(encoding="utf-8")
    assert "prepare_romanian_library" in patch
    assert "QTimer.singleShot(0, lambda: _auto_prepare_library(self))" in patch
    assert "IMDb identificate" in patch


def test_resolver_can_link_title_missing_from_vote_filtered_catalog(tmp_path, monkeypatch):
    db = Database(tmp_path / "CineCalendarData" / "data" / "cinecalendar.db")

    class SuggestResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "d": [{
                    "id": "tt0097889",
                    "l": "Mircea",
                    "y": 1989,
                    "qid": "movie",
                    "i": {"imageUrl": "https://m.media-amazon.com/images/M/mircea.jpg"},
                }]
            }

    monkeypatch.setattr(
        "cinecalendar.romanian_films.requests.Session.get",
        lambda self, *args, **kwargs: SuggestResponse(),
    )

    result = resolve_romanian_catalog_links(db)
    assert result["online"] >= 1
    item = next(x for x in romanian_films(db) if x.film == "Mircea (1989)")
    assert item.imdb_id == "tt0097889"
    assert item.poster_url == "https://m.media-amazon.com/images/M/mircea.jpg"


def test_undated_ambiguous_title_prefers_unique_rated_candidate(tmp_path):
    from cinecalendar.util import identity_key, normalize_text, utcnow_iso

    db = Database(tmp_path / "cinecalendar.db")
    target = next(x for x in romanian_films() if not any(ch.isdigit() for ch in x.film))
    title = target.film.split(" (", 1)[0].split(" [", 1)[0]
    now = utcnow_iso()

    with db.tx() as con:
        for imdb_id, year, rating in (("tt9000001", 2000, None), ("tt9000002", 2010, 8)):
            cur = con.execute(
                """INSERT INTO movies(
                    imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                    year,title_type,genres_json,directors_json,countries_json,overview,
                    keywords_json,semantic_json,source,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    imdb_id, identity_key(title, title, year, "movie"),
                    title, title, normalize_text(title), normalize_text(title),
                    year, "movie", "[]", "[]", "[]", "", "[]", "{}", "test", now, now,
                ),
            )
            if rating is not None:
                con.execute(
                    "INSERT INTO ratings(movie_id,rating,source,imported_at,updated_at) VALUES(?,?,?,?,?)",
                    (int(cur.lastrowid), rating, "test", now, now),
                )

    item = next(x for x in romanian_films(db) if x.film == target.film)
    assert item.watched is True
    assert item.user_rating == 8
