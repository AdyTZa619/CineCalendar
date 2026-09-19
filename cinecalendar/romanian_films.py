from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import csv
import gzip
import hashlib
import re
from pathlib import Path
from urllib.parse import quote

import requests

from .db import Database
from .models import Movie
from .open_metadata import OpenMovieMetadataProvider
from .romanian_films_data import DATA
from .util import identity_key, json_dumps, normalize_text, utcnow_iso


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
_ALLOWED_TYPES = {
    "movie", "short", "tvmovie", "video", "tvminiseries", "tvseries",
}
_RESOLVED_SETTING = "romanian_resolved_imdb_ids"
_LOCAL_SCAN_SETTING = "romanian_resolver_local_scan_signature"
_SUGGESTION_ATTEMPTS_SETTING = "romanian_resolver_suggestion_attempts"
_BROKEN_POSTERS_SETTING = "romanian_broken_poster_urls"


def _release_years(label: str) -> set[int]:
    years: set[int] = set()
    for group in _GROUP_RE.findall(label or ""):
        for raw in _YEAR_RE.findall(group):
            years.add(int(raw))
    return years


def _raw_title_aliases(label: str) -> list[str]:
    label = (label or "").strip()
    out: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\b(?:premier[ăa]|miniserie|film)\b.*$", "", value, flags=re.I)
        value = _YEAR_RE.sub(" ", value)
        value = re.sub(r"\s*[-–—]\s*$", "", value)
        value = re.sub(r"\s+", " ", value).strip(" -–—,;")
        if len(value) >= 2 and value not in out:
            out.append(value)

    base = _GROUP_RE.sub(" ", label)
    add(base)
    add(re.split(r"[\[(]", label, maxsplit=1)[0])
    for part in re.split(r"\s*/\s*", base):
        add(part)
    for group in _GROUP_RE.findall(label):
        if re.search(r"[A-Za-zĂÂÎȘȚăâîșț]", group):
            add(group)
            for part in re.split(r"\s*/\s*", group):
                add(part)
    return out


def _title_aliases(label: str) -> set[str]:
    return {normalize_text(x) for x in _raw_title_aliases(label) if normalize_text(x)}


def _loose_norm(value: str | None) -> str:
    """Very small typo-tolerant key used only with year/type safeguards."""
    text = normalize_text(value)
    # Romanian list has a few one-letter/repeated-letter transcription variants
    # (e.g. Mirabella/Mirabela). Collapse repeated letters but keep word order.
    return re.sub(r"([a-z0-9])\1+", r"\1", text)


def display_title(label: str) -> str:
    base = re.split(r"[\[(]", (label or "").strip(), maxsplit=1)[0].strip()
    return base or (label or "").strip()


def load_romanian_films_catalog() -> dict:
    return DATA


def romanian_chapters() -> list[dict]:
    return list(DATA["chapters"])


def _broken_poster_urls(db: Database) -> set[str]:
    raw = db.get_setting(_BROKEN_POSTERS_SETTING, [])
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if str(x).startswith(("http://", "https://"))}


def _resolved_map(db: Database) -> dict[str, str]:
    raw = db.get_setting(_RESOLVED_SETTING, {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and re.fullmatch(r"tt\d+", str(v or ""))
    }


def _candidate_movies(db: Database, entries: list[RomanianFilmEntry]) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    aliases = sorted({alias for entry in entries for alias in _title_aliases(entry.film)})
    resolved = _resolved_map(db)
    resolved_ids = sorted(set(resolved.values()))

    by_alias: dict[str, list[dict]] = {}
    by_id: dict[str, dict] = {}
    with db.connect() as con:
        for start in range(0, len(aliases), 350):
            chunk = aliases[start:start + 350]
            if not chunk:
                continue
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
                if item["imdb_id"]:
                    by_id[str(item["imdb_id"])] = item
                for alias in {item["title_norm"], item["original_title_norm"]}:
                    if alias:
                        by_alias.setdefault(alias, []).append(item)

        for start in range(0, len(resolved_ids), 500):
            chunk = resolved_ids[start:start + 500]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"""SELECT m.id,m.imdb_id,m.title_norm,m.original_title_norm,m.year,m.title_type,
                           m.poster_url,m.imdb_rating,m.num_votes,r.rating AS user_rating
                    FROM movies m
                    LEFT JOIN ratings r ON r.movie_id=m.id
                    WHERE m.imdb_id IN ({marks})""",
                tuple(chunk),
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
                if item["imdb_id"]:
                    by_id[str(item["imdb_id"])] = item
    return by_alias, by_id


def _match_movie(
    entry: RomanianFilmEntry,
    by_alias: dict[str, list[dict]],
    by_id: dict[str, dict],
    resolved: dict[str, str],
) -> dict | None:
    mapped_id = resolved.get(entry.film)
    if mapped_id and mapped_id in by_id:
        return by_id[mapped_id]

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
            return None
        candidates = exact_year
    else:
        rated = [row for row in candidates if row["user_rating"] is not None]
        rated_ids = {row["imdb_id"] or row["id"] for row in rated}
        if len(rated_ids) == 1 and rated:
            candidates = rated
        else:
            identities = {
                (row["imdb_id"], row["year"], row["title_norm"], row["original_title_norm"])
                for row in candidates
            }
            if len(identities) != 1:
                return None

    candidates.sort(
        key=lambda row: (
            0 if row["user_rating"] is not None else 1,
            0 if str(row["title_type"]).lower() in {"movie", "tvmovie", "short"} else 1,
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

    resolved = _resolved_map(db)
    by_alias, by_id = _candidate_movies(db, entries)
    out: list[RomanianFilmEntry] = []
    for entry in entries:
        match = _match_movie(entry, by_alias, by_id, resolved)
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


def _catalog_basics_path(db: Database) -> Path:
    root = db.path.parent.parent
    return root / "cache" / "imdb_datasets" / "title.basics.tsv.gz"


def _upsert_resolved_movie(
    db: Database,
    *,
    imdb_id: str,
    title: str,
    original_title: str,
    year: int | None,
    title_type: str,
    poster_url: str | None = None,
    source: str = "romanian_resolver",
) -> int:
    now = utcnow_iso()
    title = title or original_title or imdb_id
    original_title = original_title or title
    typ = title_type or "movie"
    ident = identity_key(title, original_title, year, typ)
    tn = normalize_text(title)
    on = normalize_text(original_title)

    with db.tx() as con:
        existing = con.execute("SELECT id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()
        if existing is None and year is not None:
            rows = con.execute(
                """SELECT id FROM movies
                   WHERE year=?
                     AND (imdb_id IS NULL OR imdb_id=?)
                     AND (title_norm IN (?,?) OR original_title_norm IN (?,?))
                   ORDER BY id LIMIT 2""",
                (year, imdb_id, tn, on, tn, on),
            ).fetchall()
            if len(rows) == 1:
                clash = con.execute(
                    "SELECT id FROM movies WHERE imdb_id=? AND id<>?",
                    (imdb_id, int(rows[0]["id"])),
                ).fetchone()
                if clash is None:
                    existing = rows[0]

        if existing is not None:
            movie_id = int(existing["id"])
            con.execute(
                """UPDATE movies
                   SET imdb_id=COALESCE(imdb_id,?),
                       identity_key=?,title=?,original_title=?,
                       title_norm=?,original_title_norm=?,
                       year=COALESCE(?,year),
                       title_type=COALESCE(NULLIF(?,''),title_type),
                       poster_url=COALESCE(NULLIF(?,''),poster_url),
                       source=CASE WHEN source='local' THEN ? ELSE source END,
                       updated_at=?
                   WHERE id=?""",
                (
                    imdb_id, ident, title, original_title, tn, on, year, typ,
                    poster_url or "", source, now, movie_id,
                ),
            )
            return movie_id

        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,genres_json,directors_json,countries_json,overview,
                   keywords_json,semantic_json,poster_url,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, ident, title, original_title, tn, on, year, typ,
                json_dumps([]), json_dumps([]), json_dumps([]), "",
                json_dumps([]), json_dumps({}), poster_url, source, now, now,
            ),
        )
        return int(cur.lastrowid)


def _choose_candidate(entry: RomanianFilmEntry, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    aliases = _title_aliases(entry.film)
    loose_aliases = {_loose_norm(x) for x in _raw_title_aliases(entry.film)}
    years = _release_years(entry.film)

    scored: list[tuple[int, dict]] = []
    for row in candidates:
        typ = str(row.get("title_type") or row.get("qid") or "").lower()
        if typ and typ not in _ALLOWED_TYPES:
            continue
        year = row.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        title = str(row.get("title") or "")
        original = str(row.get("original_title") or title)
        norms = {normalize_text(title), normalize_text(original)}
        loose = {_loose_norm(title), _loose_norm(original)}

        exact = bool(norms & aliases)
        fuzzy = bool(loose & loose_aliases)
        similarity = 0.0
        if years and not exact and not fuzzy:
            for candidate_norm in norms:
                for alias in aliases:
                    if candidate_norm and alias:
                        similarity = max(
                            similarity,
                            SequenceMatcher(None, candidate_norm, alias).ratio(),
                        )
        year_ok = not years or year in years
        if years and not year_ok:
            continue
        if not exact and not (years and (fuzzy or similarity >= 0.92)):
            continue

        score = 0
        if exact:
            score += 100
        elif fuzzy:
            score += 65
        elif similarity >= 0.92:
            score += 55 + int(similarity * 10)
        if years and year_ok:
            score += 80
        if typ in {"movie", "tvmovie", "short"}:
            score += 10
        scored.append((score, row))

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], str(x[1].get("imdb_id") or "")))
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _resolve_from_local_basics(
    db: Database,
    entries: list[RomanianFilmEntry],
    resolved: dict[str, str],
    progress=None,
) -> int:
    path = _catalog_basics_path(db)
    if not path.is_file():
        return 0

    unresolved = [entry for entry in entries if entry.film not in resolved]
    if not unresolved:
        return 0

    exact_index: dict[str, set[int]] = {}
    loose_index: dict[str, set[int]] = {}
    for idx, entry in enumerate(unresolved):
        for alias in _title_aliases(entry.film):
            exact_index.setdefault(alias, set()).add(idx)
        if _release_years(entry.film):
            for alias in _raw_title_aliases(entry.film):
                loose_index.setdefault(_loose_norm(alias), set()).add(idx)

    hits: dict[int, list[dict]] = {i: [] for i in range(len(unresolved))}
    scanned = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                scanned += 1
                typ = str(row.get("titleType") or "").lower()
                if typ not in _ALLOWED_TYPES:
                    continue
                title = row.get("primaryTitle") or ""
                original = row.get("originalTitle") or title
                norms = {normalize_text(title), normalize_text(original)}
                indexes: set[int] = set()
                for norm in norms:
                    indexes.update(exact_index.get(norm, set()))
                if not indexes:
                    for value in {_loose_norm(title), _loose_norm(original)}:
                        indexes.update(loose_index.get(value, set()))
                if not indexes:
                    continue
                raw_year = row.get("startYear")
                try:
                    year = int(raw_year) if raw_year and raw_year != "\\N" else None
                except ValueError:
                    year = None
                candidate = {
                    "imdb_id": row.get("tconst") or "",
                    "title": title,
                    "original_title": original,
                    "year": year,
                    "title_type": row.get("titleType") or "movie",
                    "poster_url": None,
                }
                if not re.fullmatch(r"tt\d+", candidate["imdb_id"]):
                    continue
                for idx in indexes:
                    hits[idx].append(candidate)
                if progress and scanned % 1_000_000 == 0:
                    progress(f"Identific filme românești în catalogul IMDb… {scanned:,} titluri scanate")
    except (OSError, UnicodeError, csv.Error):
        return 0

    added = 0
    for idx, entry in enumerate(unresolved):
        chosen = _choose_candidate(entry, hits.get(idx, []))
        if not chosen:
            continue
        _upsert_resolved_movie(
            db,
            imdb_id=chosen["imdb_id"],
            title=chosen["title"],
            original_title=chosen["original_title"],
            year=chosen["year"],
            title_type=chosen["title_type"],
            source="imdb_dataset_resolver",
        )
        resolved[entry.film] = chosen["imdb_id"]
        added += 1
    return added


def _suggest_candidates(session: requests.Session, query_text: str) -> list[dict]:
    if not query_text:
        return []
    url = "https://v3.sg.media-imdb.com/suggestion/x/" + quote(query_text, safe="") + ".json"
    response = session.get(url, timeout=(6, 15), headers={"User-Agent": "CineCalendar/3.9"})
    response.raise_for_status()
    payload = response.json()
    out: list[dict] = []
    for row in payload.get("d") or []:
        iid = str(row.get("id") or "")
        if not re.fullmatch(r"tt\d+", iid):
            continue
        image = ""
        if isinstance(row.get("i"), dict):
            image = str(row["i"].get("imageUrl") or "")
        out.append({
            "imdb_id": iid,
            "title": str(row.get("l") or ""),
            "original_title": str(row.get("l") or ""),
            "year": row.get("y"),
            "title_type": str(row.get("qid") or ""),
            "poster_url": image or None,
        })
    return out


def _resolve_from_suggestions(
    db: Database,
    entries: list[RomanianFilmEntry],
    resolved: dict[str, str],
    progress=None,
    *,
    force: bool = False,
) -> tuple[int, int]:
    session = requests.Session()
    added = 0
    errors = 0
    today = utcnow_iso()[:10]
    attempts = db.get_setting(_SUGGESTION_ATTEMPTS_SETTING, {})
    if not isinstance(attempts, dict):
        attempts = {}
    unresolved = [entry for entry in entries if entry.film not in resolved]
    consecutive_network_errors = 0
    for idx, entry in enumerate(unresolved, 1):
        if not force and str(attempts.get(entry.film) or "") == today:
            continue
        chosen = None
        request_completed = False
        network_down = False
        for alias in _raw_title_aliases(entry.film)[:3]:
            try:
                candidates = _suggest_candidates(session, alias)
                request_completed = True
                consecutive_network_errors = 0
            except (requests.RequestException, ValueError):
                errors += 1
                consecutive_network_errors += 1
                if consecutive_network_errors >= 3:
                    network_down = True
                    break
                continue
            chosen = _choose_candidate(entry, candidates)
            if chosen:
                break
        if network_down:
            break
        if request_completed:
            attempts[entry.film] = today
        if chosen:
            _upsert_resolved_movie(
                db,
                imdb_id=chosen["imdb_id"],
                title=chosen["title"],
                original_title=chosen["original_title"],
                year=int(chosen["year"]) if chosen.get("year") else None,
                title_type=chosen["title_type"] or "movie",
                poster_url=chosen.get("poster_url"),
                source="imdb_suggestion_resolver",
            )
            resolved[entry.film] = chosen["imdb_id"]
            added += 1
        if progress and (idx == len(unresolved) or idx % 15 == 0):
            progress(f"Identificare IMDb online: {idx}/{len(unresolved)}")
    db.set_setting(_SUGGESTION_ATTEMPTS_SETTING, attempts)
    return added, errors


def resolve_romanian_catalog_links(db: Database, progress=None, *, force: bool = False) -> dict[str, int]:
    entries = [RomanianFilmEntry(**row) for row in DATA["films"]]
    resolved = _resolved_map(db)
    before = len(resolved)

    basics = _catalog_basics_path(db)
    pending_before = sorted(entry.film for entry in entries if entry.film not in resolved)
    basics_stamp = "missing"
    if basics.is_file():
        try:
            stat = basics.stat()
            basics_stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            basics_stamp = "unreadable"
    pending_hash = hashlib.sha256("\n".join(pending_before).encode("utf-8")).hexdigest()
    scan_signature = f"{basics_stamp}:{pending_hash}"

    local = 0
    if force or db.get_setting(_LOCAL_SCAN_SETTING, "") != scan_signature:
        local = _resolve_from_local_basics(db, entries, resolved, progress)

    online, errors = _resolve_from_suggestions(
        db, entries, resolved, progress, force=force
    )
    if resolved:
        db.set_setting(_RESOLVED_SETTING, resolved)

    pending_after = sorted(entry.film for entry in entries if entry.film not in resolved)
    after_hash = hashlib.sha256("\n".join(pending_after).encode("utf-8")).hexdigest()
    db.set_setting(_LOCAL_SCAN_SETTING, f"{basics_stamp}:{after_hash}")

    return {
        "before": before,
        "local": local,
        "online": online,
        "resolved": sum(1 for entry in entries if entry.film in resolved),
        "unresolved": sum(1 for entry in entries if entry.film not in resolved),
        "errors": errors,
    }


def backfill_romanian_posters(db: Database, *, progress=None) -> dict[str, int]:
    entries = romanian_films(db)
    targets = [
        item for item in entries
        if item.local_movie_id and item.imdb_id and not item.poster_url
    ]
    if not targets:
        posters = sum(1 for item in entries if item.poster_url)
        return {
            "targets": 0, "filled": 0, "imdb": 0, "suggestion": 0, "wikidata": 0,
            "fallback": 0, "remaining": 0, "posters": posters, "errors": 0,
        }

    ids = list(dict.fromkeys(item.imdb_id for item in targets if item.imdb_id))
    broken_urls = _broken_poster_urls(db)
    session = requests.Session()
    source_errors = 0

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
                    "x-imdb-user-country": "RO",
                },
                timeout=(8, 25),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                source_errors += 1
            else:
                for row in (payload.get("data") or {}).get("titles") or []:
                    if not isinstance(row, dict):
                        continue
                    iid = str(row.get("id") or "").strip()
                    image = str(((row.get("primaryImage") or {}).get("url") or "")).strip()
                    if image in broken_urls:
                        image = ""
                    raw_rating = (row.get("ratingsSummary") or {}).get("aggregateRating")
                    try:
                        aggregate = float(raw_rating) if raw_rating is not None else None
                    except (TypeError, ValueError):
                        aggregate = None
                    if iid:
                        imdb_found[iid] = (image, aggregate)
        except (requests.RequestException, ValueError, TypeError):
            source_errors += 1
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

    refreshed = romanian_films(db)
    still_missing = [
        item for item in refreshed
        if item.local_movie_id and item.imdb_id and not item.poster_url
    ]

    # The suggestion service is often more reliable for poster thumbnails than
    # title GraphQL, especially for obscure/older Romanian titles. Require the
    # candidate IMDb id to equal the already-resolved id, so this cannot attach
    # a same-name remake by accident.
    suggestion_filled = 0
    now = utcnow_iso()
    poster_search_errors = 0
    for idx, item in enumerate(still_missing, 1):
        image = ""
        network_down = False
        for alias in _raw_title_aliases(item.film)[:2]:
            try:
                candidates = _suggest_candidates(session, alias)
            except (requests.RequestException, ValueError, TypeError):
                source_errors += 1
                poster_search_errors += 1
                if poster_search_errors >= 3:
                    network_down = True
                    break
                continue
            poster_search_errors = 0
            chosen = _choose_candidate(item, candidates)
            if chosen and chosen.get("imdb_id") == item.imdb_id and chosen.get("poster_url"):
                candidate_image = str(chosen["poster_url"])
                if candidate_image not in broken_urls:
                    image = candidate_image
                    break
        if network_down:
            break
        if image:
            with db.tx() as con:
                con.execute(
                    """UPDATE movies SET poster_url=CASE
                           WHEN poster_url IS NULL OR TRIM(poster_url)='' THEN ?
                           ELSE poster_url END,
                           updated_at=? WHERE id=?""",
                    (image, now, int(item.local_movie_id)),
                )
            suggestion_filled += 1
        if progress and (idx == len(still_missing) or idx % 20 == 0):
            progress(f"Postere IMDb Search: {idx}/{len(still_missing)}")

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
                headers={"User-Agent": "CineCalendar/3.9 personal movie recommender"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            rows = response.json().get("results", {}).get("bindings", [])
            for row in rows:
                iid = (row.get("imdb") or {}).get("value", "").strip()
                image = (row.get("image") or {}).get("value", "").strip()
                if image and "Special:FilePath/" in image and "?" not in image:
                    image += "?width=342"
                if image in broken_urls:
                    image = ""
                if iid and image and iid not in wikidata_found:
                    wikidata_found[iid] = image
        except (requests.RequestException, ValueError, TypeError):
            source_errors += 1
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

    # Process the entire remainder, not a fixed first page; otherwise the same
    # negative first 48 items starve every later title forever.
    refreshed = romanian_films(db)
    remaining_items = [
        item for item in refreshed
        if item.local_movie_id and item.imdb_id and not item.poster_url
    ]
    fallback = 0
    provider = OpenMovieMetadataProvider(db)
    fallback_errors = 0
    for idx, item in enumerate(remaining_items, 1):
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
            source_errors += 1
            fallback_errors += 1
            if fallback_errors >= 3:
                break
            continue
        fallback_errors = 0
        if movie.poster_url in broken_urls:
            with db.tx() as con:
                con.execute("UPDATE movies SET poster_url=NULL WHERE id=?", (int(item.local_movie_id),))
            movie.poster_url = None
        if movie.poster_url:
            fallback += 1
        if progress and (idx == len(remaining_items) or idx % 12 == 0):
            progress(f"Postere Wikimedia: {idx}/{len(remaining_items)}")

    final_entries = romanian_films(db)
    remaining = sum(
        1 for item in final_entries
        if item.local_movie_id and item.imdb_id and not item.poster_url
    )
    posters = sum(1 for item in final_entries if item.poster_url)
    return {
        "targets": len(targets),
        "filled": imdb_filled + suggestion_filled + wikidata_filled,
        "imdb": imdb_filled,
        "suggestion": suggestion_filled,
        "wikidata": wikidata_filled,
        "fallback": fallback,
        "remaining": remaining,
        "posters": posters,
        "errors": source_errors,
    }


def prepare_romanian_library(db: Database, progress=None, *, force: bool = False) -> dict[str, int]:
    links = resolve_romanian_catalog_links(db, progress, force=force)
    posters = backfill_romanian_posters(db, progress=progress)
    entries = romanian_films(db)
    linked = sum(1 for item in entries if item.imdb_id)
    watched = sum(1 for item in entries if item.watched)
    poster_count = sum(1 for item in entries if item.poster_url)
    result = {
        "total": len(entries),
        "linked": linked,
        "watched": watched,
        "posters": poster_count,
        "unresolved": len(entries) - linked,
        "missing_posters": len(entries) - poster_count,
        "resolver_errors": int(links.get("errors", 0)),
        "poster_errors": int(posters.get("errors", 0)),
        "changed": int(links.get("local", 0)) + int(links.get("online", 0)) +
                   int(posters.get("filled", 0)) + int(posters.get("fallback", 0)),
    }
    db.set_setting("romanian_library_last_diagnostics", result)
    db.set_setting("romanian_library_last_prepare", utcnow_iso())
    return result
