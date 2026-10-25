def test_movies_with_empty_or_nonsensical_titles_are_rejected():
    from cinecalendar.models import Movie

    assert Movie(title="").is_minimally_valid() is False
    assert Movie(title="   ").is_minimally_valid() is False
    assert Movie(title="unknown").is_minimally_valid() is False
    assert Movie(title="Untitled").is_minimally_valid() is False
    assert Movie(title="Inception", year=2020).is_minimally_valid() is False
    assert Movie(title="Inception", year=1994).is_minimally_valid() is True
    assert Movie(title="The Matrix", year=1999).is_minimally_valid() is True
