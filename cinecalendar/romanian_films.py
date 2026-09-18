from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class RomanianFilmEntry:
    chapter: int
    film: str
    period: str
    season: str
    context: str
    certainty_source: str


def load_romanian_films_catalog() -> dict:
    path = Path(__file__).with_name("data") / "romanian_films.json"
    return json.loads(path.read_text(encoding="utf-8"))


def romanian_films() -> list[RomanianFilmEntry]:
    data = load_romanian_films_catalog()
    return [RomanianFilmEntry(**row) for row in data["films"]]


def romanian_chapters() -> list[dict]:
    return list(load_romanian_films_catalog()["chapters"])
