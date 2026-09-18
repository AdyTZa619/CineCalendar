from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .db import Database
from .romanian_films_data import DATA
from .util import normalize_text


@dataclass(frozen=True)
class RomanianFilmEntry:
    chapter: int
    chapter_title: str
    film: str
    period: str
    season: str
    context: str
    certainty_source: str
    watched: bool = False
    user_rating: int | None = None
    imdb_id: str | None = None


_GROUP_RE = re.compile(r"[\[(]([^\])]+)[\])]")
_YEAR_RE = re.compile(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2})(?!\d)")


def _release_years(label: str) -> set[int]:
    years: set[int] = set()
    # Production years in the supplied list are consistently carried by (...) / [...] groups.
    # This avoids confusing titles such as "Memento 1918 [2023]" with a 1918 release.
    for group in _GROUP_RE.findall(label or ""):
        for raw in _YEAR_RE.findall(group):
            years.add(int(raw))
    return years


def _title_aliases(label: str) -> set[str]:
    label = (label or "").strip()
    aliases: set[str] = set()

    def add(value: str) -> None:
        value = re.sub(r"\b(?:premier[ăa]|miniserie|film)\b.*$", "", value, flags=re.I)
        value = _YEAR_RE.sub(" ", value)
        value = re.sub(r"\s*[-–—]\s*$", "", value)
        norm = normalize_text(value)
        if len(norm) >= 2:
            aliases.add(norm)

    # Main label without bracketed production notes.
    add(_GROUP_RE.sub(" ", label))
    add(re.split(r"[\[(]", label, maxsplit=1)[0])

    # Bilingual / alternate title inside parentheses, e.g. With Clean Hands (Cu mâinile curate - 1972).
    for group in _GROUP_RE.findall(label):
        if re.search(r"[A-Za-zĂÂÎȘȚăâîșț]", group):
            add(group)

    # Slash-separated aliases, e.g. Momentul adevărului / Dreptatea.
    base = _GROUP_RE.sub(" ", label)
    for part in re.split(r"\s*/\s*", base):
        add(part)
    for group in _GROUP_RE.findall(label):
        for part in re.split(r"\s*/\s*", group):
            add(part)

    return aliases


def load_romanian_films_catalog() -> dict:
    return DATA


def romanian_chapters() -> list[dict]:
    return list(DATA["chapters"])


def _rated_lookup(db: Database) -> list[dict]:
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.imdb_id,m.title_norm,m.original_title_norm,m.year,r.rating
               FROM ratings r
               JOIN movies m ON m.id=r.movie_id"""
        ).fetchall()
    return [
        {
            "imdb_id": row["imdb_id"],
            "title_norm": row["title_norm"] or normalize_text(""),
            "original_title_norm": row["original_title_norm"] or normalize_text(""),
            "year": int(row["year"]) if row["year"] is not None else None,
            "rating": int(row["rating"]),
        }
        for row in rows
    ]


def _match_rating(entry: RomanianFilmEntry, rated: list[dict]) -> dict | None:
    aliases = _title_aliases(entry.film)
    if not aliases:
        return None
    years = _release_years(entry.film)

    candidates = [
        row for row in rated
        if row["title_norm"] in aliases or row["original_title_norm"] in aliases
    ]
    if not candidates:
        return None

    if years:
        exact_year = [row for row in candidates if row["year"] in years]
        if exact_year:
            candidates = exact_year
        else:
            # A dated list entry must not disappear because of a same-name film from another year.
            return None

    # Undated entries are auto-matched only when the normalized title identifies one rated movie.
    identities = {(row["imdb_id"], row["year"], row["title_norm"], row["original_title_norm"]) for row in candidates}
    if not years and len(identities) != 1:
        return None
    return candidates[0]


def romanian_films(db: Database | None = None) -> list[RomanianFilmEntry]:
    entries = [RomanianFilmEntry(**row) for row in DATA["films"]]
    if db is None:
        return entries

    rated = _rated_lookup(db)
    out: list[RomanianFilmEntry] = []
    for entry in entries:
        match = _match_rating(entry, rated)
        if match:
            entry = replace(
                entry,
                watched=True,
                user_rating=match["rating"],
                imdb_id=match["imdb_id"],
            )
        out.append(entry)
    return out
