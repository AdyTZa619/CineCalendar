from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Recommendation


@dataclass(frozen=True)
class StoryPeriod:
    rank: int
    year: int
    label: str
    confidence: float


_PERIODS = (
    (0, -3000, "Antichitate", ("ancient", "roman empire", "romanian empire", "gladiator", "pharaoh", "biblical", "bible", "jesus", "christ", "apostle", "greek empire", "sparta")),
    (1, 1000, "Evul Mediu", ("medieval", "middle ages", "crusade", "crusades", "knight", "vikings", "viking", "ottoman", "byzantine")),
    (2, 1800, "Epoca modernă timpurie", ("renaissance", "victorian", "napoleonic", "19th century", "nineteenth century", "wild west")),
    (3, 1914, "Primul Război Mondial", ("world war i", "world war 1", "wwi", "first world war", "great war")),
    (4, 1920, "Perioada interbelică", ("interwar", "roaring twenties", "great depression", "1920s", "1930s")),
    (5, 1939, "Al Doilea Război Mondial", ("world war ii", "world war 2", "wwii", "second world war", "holocaust", "nazi occupation")),
    (6, 1947, "Perioada comunistă", ("communist", "communism", "cold war", "soviet era", "iron curtain", "ceaușescu", "ceausescu")),
    (7, 1990, "Anii 1990", ("1990s", "nineties")),
    (8, 2000, "Anii 2000", ("2000s", "two thousands")),
    (9, 2010, "Contemporan", ("present day", "present-day", "contemporary", "modern day", "modern-day")),
    (10, 3000, "Viitor", ("future", "futuristic", "dystopian future", "near future", "far future")),
)

_YEAR_RE = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)")


def _rank_for_year(year: int) -> tuple[int, str]:
    if year < 500:
        return 0, "Antichitate"
    if year < 1500:
        return 1, "Evul Mediu"
    if year < 1914:
        return 2, "Epoca modernă timpurie"
    if year <= 1918:
        return 3, "Primul Război Mondial"
    if year <= 1938:
        return 4, "Perioada interbelică"
    if year <= 1945:
        return 5, "Al Doilea Război Mondial"
    if year <= 1989:
        return 6, "Perioada comunistă"
    if year <= 1999:
        return 7, "Anii 1990"
    if year <= 2009:
        return 8, "Anii 2000"
    if year <= 2099:
        return 9, "Contemporan"
    return 10, "Viitor"


def infer_story_period(rec: Recommendation) -> StoryPeriod | None:
    movie = rec.movie
    text = " ".join([
        movie.title or "",
        movie.original_title or "",
        movie.overview or "",
        " ".join(movie.keywords or []),
    ]).lower()

    # Explicit period language is stronger than release year and is checked from the most
    # distinctive eras first. This is deliberately metadata-only: no network call on ranking.
    matches = []
    for rank, default_year, label, terms in _PERIODS:
        hits = sum(1 for term in terms if term in text)
        if hits:
            matches.append((hits, rank, default_year, label))
    if matches:
        hits, rank, default_year, label = max(matches, key=lambda x: (x[0], -x[1]))
        years = [int(x) for x in _YEAR_RE.findall(text)]
        compatible = [y for y in years if _rank_for_year(y)[0] == rank]
        setting_year = min(compatible) if compatible else default_year
        return StoryPeriod(rank, setting_year, label, min(0.95, 0.72 + 0.08 * hits))

    years = [int(x) for x in _YEAR_RE.findall(text)]
    # A year in synopsis/keywords is evidence of setting; never substitute release year.
    if years:
        # Prefer the earliest explicit story year. This makes long historical spans start where
        # their narrative begins; explicit period keywords above can override for dominant era.
        year = min(years)
        rank, label = _rank_for_year(year)
        return StoryPeriod(rank, year, label, 0.64)
    return None


def order_recommendations_by_story_period(recs: list[Recommendation]) -> list[Recommendation]:
    indexed = list(enumerate(recs))

    def key(item):
        index, rec = item
        period = infer_story_period(rec)
        if period is None:
            # Unknown setting stays stable after known periods; release year is intentionally not
            # used as a fake story year.
            return (99, 9999, index)
        return (period.rank, period.year, index)

    return [rec for _idx, rec in sorted(indexed, key=key)]
