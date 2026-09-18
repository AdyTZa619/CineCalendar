from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.romanian_films import romanian_chapters, romanian_films


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
