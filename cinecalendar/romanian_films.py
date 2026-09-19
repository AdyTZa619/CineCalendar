from __future__ import annotations

from dataclasses import dataclass, replace
import re

import requests

from .db import Database
from .romanian_films_data import DATA
from .models import Movie
from .open_metadata import OpenMovieMetadataProvider
from .util import normalize_text, utcnow_iso


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
    local_movie_id: int | None = None
    poster_url: str | None = None
    imdb_rating: float | None = None
    release_year: int | None = None


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


def display_title(label: str) -> str:
    """Compact card title: keep the real title, drop production-year notes."""
    base = re.split(r"[\[(]", (label or "").strip(), maxsplit=1)[0].strip()
    return base or (label or "").strip()


def load_romanian_films_catalog() -> dict:
    return DATA


def romanian_chapters() -> list[dict]:
    return list(DATA["chapters"])


def _candidate_movies(db: Database, entries: list[RomanianFilmEntry]) -> dict[str, list[dict]]:
    aliases = sorted({alias for entry in entries for alias in _title_aliases(entry.film)})
    if not aliases:
        return {}

    by_alias: dict[str, list[dict]] = {}
    # Keep comfortably below SQLite's parameter limit: each chunk is bound twice.
    with db.connect() as con:
        for start in range(0, len(aliases), 350):
            chunk = aliases[start:start + 350]
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"""SELECT m.id,m.imdb_id,m.title_norm,m.original_title_norm,m.year,m.title_type,
                           m.poster_url,m.imdb_rating,m.num_votes,r.rating AS user_rating
                    FROM movies m
                    LEFT JOIN ratings r ON r.movie_id=m.id
                    WHERE m.title_norm IN ({marks}) OR m.original_title_norm IN ({marks})""",
                tuple(chunk) + tuple(chunk),
            ).fetchall()
            for row in rows:
                item = {
                    "id": int(row["id"]),
                    "imdb_id": row["imdb_id"],
                    "title_norm": row["title_norm"] or "",
                    "original_title_norm": row["original_title_norm"] or "",
                    "year": int(row["year"]) if row["year"] is not None else None,
                    "title_type": row["title_type"] or "",
                    "poster_url": row["poster_url"],
                    "imdb_rating": float(row["imdb_rating"]) if row["imdb_rating"] is not None else None,
                    "num_votes": int(row["num_votes"] or 0),
                    "user_rating": int(row["user_rating"]) if row["user_rating"] is not None else None,
                }
                for alias in {item["title_norm"], item["original_title_norm"]}:
                    if alias:
                        by_alias.setdefault(alias, []).append(item)
    return by_alias


def _match_movie(entry: RomanianFilmEntry, by_alias: dict[str, list[dict]]) -> dict | None:
    aliases = _title_aliases(entry.film)
    if not aliases:
        return None

    candidates: list[dict] = []
    seen: set[int] = set()
    for alias in aliases:
        for row in by_alias.get(alias, []):
            if row["id"] not in seen:
                seen.add(row["id"])
                candidates.append(row)
    if not candidates:
        return None

    years = _release_years(entry.film)
    if years:
        exact_year = [row for row in candidates if row["year"] in years]
        if not exact_year:
            # A dated list entry must never attach to a remake or a same-name film from another year.
            return None
        candidates = exact_year
    else:
        identities = {(row["imdb_id"], row["year"], row["title_norm"], row["original_title_norm"]) for row in candidates}
        if len(identities) != 1:
            return None

    # Prefer feature films and the best-populated IMDb row if duplicate catalog rows remain.
    candidates.sort(
        key=lambda row: (
            0 if str(row["title_type"]).lower() in {"movie", "tvmovie"} else 1,
            0 if row["imdb_id"] else 1,
            -int(row["num_votes"] or 0),
            row["id"],
        )
    )
    return candidates[0]


def romanian_films(db: Database | None = None) -> list[RomanianFilmEntry]:
    entries = [RomanianFilmEntry(**row) for row in DATA["films"]]
    if db is None:
        return entries

    candidates = _candidate_movies(db, entries)
    out: list[RomanianFilmEntry] = []
    for entry in entries:
        match = _match_movie(entry, candidates)
        if match:
            entry = replace(
                entry,
                watched=match["user_rating"] is not None,
                user_rating=match["user_rating"],
                imdb_id=match["imdb_id"],
                local_movie_id=match["id"],
                poster_url=match["poster_url"],
                imdb_rating=match["imdb_rating"],
                release_year=match["year"],
            )
        out.append(entry)
    return out


def backfill_romanian_posters(db: Database, *, fallback_limit: int = 48, progress=None) -> dict[str, int]:
    """Fill missing posters automatically for the curated Romanian chronology.

    Preferred source is IMDb itself, queried in batches by IMDb id. Titles that
    still have no primary image fall back to Wikidata/Wikipedia. Successful URLs
    are persisted so subsequent launches read them locally.
    """
    entries = romanian_films(db)
    targets = [
        item for item in entries
        if item.local_movie_id and item.imdb_id and not item.poster_url
    ]
    if not targets:
        return {"targets": 0, "filled": 0, "imdb": 0, "wikidata": 0, "fallback": 0, "remaining": 0}

    ids = list(dict.fromkeys(item.imdb_id for item in targets if item.imdb_id))
    session = requests.Session()
    session.headers.update({
        "User-Agent": "CineCalendar/3.9 personal desktop movie recommender",
        "Accept": "application/graphql+json, application/json",
    })

    # 1) IMDb primaryImage is the best match because we already know the exact title id.
    imdb_found: dict[str, tuple[str, float | None]] = {}
    imdb_query = """
    query CineCalendarPosterBatch($ids: [ID!]!) {
      titles(ids: $ids) {
        id
        primaryImage { url }
        ratingsSummary { aggregateRating }
      }
    }
    """
    for start in range(0, len(ids), 80):
        batch = ids[start:start + 80]
        try:
            response = session.post(
                "https://caching.graphql.imdb.com/",
                json={"query": imdb_query, "variables": {"ids": batch}},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/graphql+json, application/json",
                    "Origin": "https://www.imdb.com",
                    "Referer": "https://www.imdb.com/",
                    "x-imdb-client-name": "imdb-web-next",
                    "x-imdb-user-language": "en-US",
                },
                timeout=(8, 25),
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("errors"):
                for row in (payload.get("data") or {}).get("titles") or []:
                    if not isinstance(row, dict):
                        continue
                    iid = str(row.get("id") or "").strip()
                    image = str(((row.get("primaryImage") or {}).get("url") or "")).strip()
                    raw_rating = (row.get("ratingsSummary") or {}).get("aggregateRating")
                    try:
                        aggregate = float(raw_rating) if raw_rating is not None else None
                    except (TypeError, ValueError):
                        aggregate = None
                    if iid:
                        imdb_found[iid] = (image, aggregate)
        except (requests.RequestException, ValueError, TypeError):
            pass
        if progress:
            progress(f"Postere IMDb: {min(start + len(batch), len(ids))}/{len(ids)}")

    imdb_filled = 0
    if imdb_found:
        now = utcnow_iso()
        with db.tx() as con:
            for item in targets:
                image, aggregate = imdb_found.get(item.imdb_id or "", ("", None))
                if not image and aggregate is None:
                    continue
                con.execute(
                    """UPDATE movies
                       SET poster_url=CASE
                           WHEN (poster_url IS NULL OR TRIM(poster_url)='') AND ?<>'' THEN ?
                           ELSE poster_url END,
                           imdb_rating=COALESCE(?, imdb_rating),
                           updated_at=?
                       WHERE id=?""",
                    (image, image, aggregate, now, int(item.local_movie_id)),
                )
                if image:
                    imdb_filled += 1

    # 2) Key-free Wikidata P18 batch fallback for exact IMDb ids still missing posters.
    refreshed = romanian_films(db)
    still_missing = [
        item for item in refreshed
        if item.local_movie_id and item.imdb_id and not item.poster_url
    ]
    missing_ids = list(dict.fromkeys(item.imdb_id for item in still_missing if item.imdb_id))
    wikidata_found: dict[str, str] = {}
    for start in range(0, len(missing_ids), 80):
        batch = missing_ids[start:start + 80]
        values = " ".join(f'"{iid}"' for iid in batch)
        query = f"""SELECT ?imdb ?image WHERE {{
          VALUES ?imdb {{ {values} }}
          ?item wdt:P345 ?imdb .
          OPTIONAL {{ ?item wdt:P18 ?image . }}
        }}"""
        try:
            response = session.get(
                "https://query.wikidata.org/sparql",
                params={"query": query, "format": "json"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            rows = response.json().get("results", {}).get("bindings", [])
            for row in rows:
                iid = (row.get("imdb") or {}).get("value", "").strip()
                image = (row.get("image") or {}).get("value", "").strip()
                if iid and image and iid not in wikidata_found:
                    # Special:FilePath supports width= and avoids downloading giant originals.
                    if "Special:FilePath/" in image and "?" not in image:
                        image += "?width=342"
                    wikidata_found[iid] = image
        except (requests.RequestException, ValueError, TypeError):
            pass
        if progress:
            progress(f"Postere Wikidata: {min(start + len(batch), len(missing_ids))}/{len(missing_ids)}")

    wikidata_filled = 0
    if wikidata_found:
        now = utcnow_iso()
        with db.tx() as con:
            for item in still_missing:
                image = wikidata_found.get(item.imdb_id or "")
                if not image:
                    continue
                con.execute(
                    """UPDATE movies
                       SET poster_url=CASE
                           WHEN poster_url IS NULL OR TRIM(poster_url)='' THEN ?
                           ELSE poster_url END,
                           updated_at=?
                       WHERE id=?""",
                    (image, now, int(item.local_movie_id)),
                )
                wikidata_filled += 1

    # 3) Richer Wikimedia fallback can discover a Wikipedia article image when P18 is absent.
    refreshed = romanian_films(db)
    remaining_items = [
        item for item in refreshed
        if item.local_movie_id and item.imdb_id and not item.poster_url
    ]
    fallback = 0
    provider = OpenMovieMetadataProvider(db)
    limit = max(0, int(fallback_limit))
    for idx, item in enumerate(remaining_items[:limit], 1):
        movie = Movie(
            id=item.local_movie_id,
            imdb_id=item.imdb_id,
            title=display_title(item.film),
            year=item.release_year,
            poster_url=None,
        )
        try:
            provider.enrich_by_imdb(movie)
        except Exception:
            continue
        if movie.poster_url:
            fallback += 1
        if progress:
            progress(f"Postere fallback: {idx}/{min(len(remaining_items), limit)}")

    final_entries = romanian_films(db)
    remaining = sum(
        1 for item in final_entries
        if item.local_movie_id and item.imdb_id and not item.poster_url
    )
    return {
        "targets": len(targets),
        "filled": imdb_filled + wikidata_filled,
        "imdb": imdb_filled,
        "wikidata": wikidata_filled,
        "fallback": fallback,
        "remaining": remaining,
    }
