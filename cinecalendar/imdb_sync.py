from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any
from urllib.parse import urlparse

import requests

from .db import Database
from .metadata_provenance import record_metadata_sources
from .models import Movie
from .semantic import extract_semantic
from .util import identity_key, json_dumps, json_loads, normalize_text, utcnow_iso


GRAPHQL_URLS = (
    "https://caching.graphql.imdb.com/",
    "https://api.graphql.imdb.com/",
)
GRAPHQL_URL = GRAPHQL_URLS[0]
DEFAULT_PROFILE_URL = "https://www.imdb.com/user/p.666yozwb6likjcvvjlu2hwmtli/ratings/"
_PROFILE_RE = re.compile(r"^(?:p\.[A-Za-z0-9_-]+|ur\d+)$")

_RESOLVE_PROFILE_QUERY = """
query CineCalendarResolveProfile($profileId: ID) {
  userProfile(input: { profileId: $profileId }) {
    userId
  }
}
"""

_RESOLVE_PROFILE_QUERY_LEGACY = """
query CineCalendarResolveProfileLegacy($profileId: ID) {
  userProfile(input: { userId: $profileId }) {
    userId
  }
}
"""

_QUERY = """
query CineCalendarUserRatings($userId: ID!, $first: Int!, $after: String) {
  userRatings(userId: $userId, first: $first, after: $after) {
    edges {
      node {
        userRating { value date }
        title {
          id
          titleText { text }
          originalTitleText { text }
          releaseYear { year }
          titleType { id text }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_TITLE_METADATA_QUERY = """
query CineCalendarTitleMetadata($ids: [ID!]!) {
  titles(ids: $ids) {
    id
    runtime { seconds }
    ratingsSummary { aggregateRating voteCount }
    genres { genres { text } }
    primaryImage { url }
    principalCredits(first: 8) {
      category { id text }
      credits { name { nameText { text } } }
    }
  }
}
"""

_TITLE_METADATA_CORE_QUERY = """
query CineCalendarTitleMetadataCore($ids: [ID!]!) {
  titles(ids: $ids) {
    id
    runtime { seconds }
    ratingsSummary { aggregateRating voteCount }
    genres { genres { text } }
    primaryImage { url }
  }
}
"""

_TITLE_CREDITS_QUERY = """
query CineCalendarTitleCredits($ids: [ID!]!) {
  titles(ids: $ids) {
    id
    principalCredits(first: 8) {
      category { id text }
      credits { name { nameText { text } } }
    }
  }
}
"""


@dataclass(frozen=True)
class RemoteRating:
    imdb_id: str
    title: str
    rating: int
    date_rated: str | None = None
    original_title: str | None = None
    year: int | None = None
    title_type: str = "Movie"


@dataclass
class SyncResult:
    fetched: int = 0
    new_ratings: list[tuple[str, int]] = field(default_factory=list)
    changed_ratings: list[tuple[str, int, int]] = field(default_factory=list)
    removed_ratings: list[tuple[str, int, str]] = field(default_factory=list)
    reconciled_duplicates: list[tuple[str, str, str]] = field(default_factory=list)
    metadata_enriched: int = 0
    unchanged: int = 0
    stopped_at_baseline: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.new_ratings or self.changed_ratings or self.reconciled_duplicates)


def user_id_from_profile_url(url: str) -> str:
    """Return the id embedded in an IMDb profile URL.

    Modern public profile URLs use a p.* profile id. userRatings does not
    accept that id directly: it expects the internal ur... user id.
    fetch_public_ratings resolves the profile id before requesting ratings.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"imdb.com", "www.imdb.com"}:
        raise ValueError("URL IMDb invalid.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "user" or not _PROFILE_RE.fullmatch(parts[1]):
        raise ValueError("URL-ul trebuie să fie pagina publică IMDb /user/p.../ratings/.")
    return parts[1]


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "CineCalendar/3.9 (personal IMDb ratings sync)",
        "Content-Type": "application/json",
        "Accept": "application/graphql+json, application/json",
        "Origin": "https://www.imdb.com",
        "Referer": "https://www.imdb.com/",
        "x-imdb-client-name": "imdb-web-next",
        "x-imdb-user-language": "en-US",
        "x-imdb-user-country": "RO",
    }


def _graphql_post(
    client: requests.Session,
    query: str,
    variables: dict[str, Any],
    *,
    timeout: int,
    endpoints: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for endpoint in (endpoints or GRAPHQL_URLS):
        try:
            response = client.post(
                endpoint,
                json={"query": query, "variables": variables},
                headers=_headers(),
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue
        if payload.get("errors"):
            message = "; ".join(
                str(x.get("message", "IMDb GraphQL error"))
                for x in payload["errors"][:3]
            )
            last_error = RuntimeError(message)
            continue
        if not isinstance(payload.get("data"), dict):
            last_error = RuntimeError("IMDb GraphQL nu a returnat câmpul data.")
            continue
        return payload
    if last_error is not None:
        raise RuntimeError(f"IMDb GraphQL indisponibil: {last_error}") from last_error
    raise RuntimeError("IMDb GraphQL indisponibil.")


def resolve_public_user_id(
    profile_id: str,
    *,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> str:
    """Resolve a modern public p.* profile id to the internal ur... ratings id."""
    if re.fullmatch(r"ur\d+", profile_id or ""):
        return profile_id
    if not re.fullmatch(r"p\.[A-Za-z0-9_-]+", profile_id or ""):
        raise ValueError("ID-ul profilului IMDb este invalid.")
    client = session or requests.Session()
    errors: list[str] = []
    for query in (_RESOLVE_PROFILE_QUERY, _RESOLVE_PROFILE_QUERY_LEGACY):
        try:
            payload = _graphql_post(
                client,
                query,
                {"profileId": profile_id},
                timeout=timeout,
                endpoints=("https://api.graphql.imdb.com/", "https://caching.graphql.imdb.com/"),
            )
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        profile = (payload.get("data") or {}).get("userProfile")
        user_id = str((profile or {}).get("userId") or "").strip()
        if re.fullmatch(r"ur\d+", user_id):
            return user_id
    detail = " | ".join(errors[-2:])[:300]
    raise RuntimeError(
        "IMDb nu a putut transforma ID-ul public al profilului în ID-ul intern de ratinguri."
        + (f" Detaliu: {detail}" if detail else "")
    )


def _as_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text")
    return str(value or "").strip()


def _parse_node(node: dict[str, Any]) -> RemoteRating:
    title = node.get("title") or {}
    imdb_id = str(title.get("id") or "").strip()
    name = _as_text(title.get("titleText"))
    rating_obj = node.get("userRating") or {}
    rating_value = rating_obj.get("value")
    if rating_value is None:
        rating_value = node.get("rating")
    rating = int(rating_value)
    if not re.fullmatch(r"tt\d+", imdb_id) or not name or not 1 <= rating <= 10:
        raise ValueError("IMDb a returnat un rating incomplet sau invalid.")
    original = _as_text(title.get("originalTitleText")) or name
    year_obj = title.get("releaseYear") or {}
    try:
        year = int(year_obj.get("year")) if year_obj.get("year") is not None else None
    except (TypeError, ValueError):
        year = None
    type_obj = title.get("titleType") or {}
    title_type = _as_text(type_obj.get("text")) or _as_text(type_obj.get("id")) or "Movie"
    rated = str(rating_obj.get("date") or node.get("date") or "").strip() or None
    if rated:
        rated = rated[:10]
    return RemoteRating(imdb_id, name, rating, rated, original, year, title_type)


def fetch_public_ratings(
    profile_url: str,
    *,
    timeout: int = 15,
    max_pages: int = 40,
    session: requests.Session | None = None,
) -> list[RemoteRating]:
    profile_id = user_id_from_profile_url(profile_url)
    client = session or requests.Session()
    user_id = resolve_public_user_id(profile_id, timeout=timeout, session=client)

    after = None
    out: list[RemoteRating] = []
    seen: set[str] = set()
    exhausted = False
    malformed_total = 0

    for _ in range(max_pages):
        payload = _graphql_post(
            client,
            _QUERY,
            {"userId": user_id, "first": 250, "after": after},
            timeout=timeout,
        )
        conn = (payload.get("data") or {}).get("userRatings")
        if not isinstance(conn, dict):
            raise RuntimeError("IMDb nu a returnat lista publică de ratinguri.")
        edges = conn.get("edges")
        if not isinstance(edges, list):
            raise RuntimeError("Răspuns IMDb incompatibil: lipsesc ratingurile.")

        parsed_page = 0
        for edge in edges:
            try:
                rr = _parse_node((edge or {}).get("node") or {})
            except (TypeError, ValueError):
                malformed_total += 1
                continue
            parsed_page += 1
            if rr.imdb_id not in seen:
                seen.add(rr.imdb_id)
                out.append(rr)

        if edges and parsed_page == 0:
            raise RuntimeError(
                "IMDb a returnat o pagină de ratinguri, dar niciun element nu are schema așteptată."
            )

        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            exhausted = True
            break
        after = page.get("endCursor")
        if not after:
            raise RuntimeError("IMDb a indicat o pagină următoare fără cursor.")

    if not exhausted and max_pages > 0:
        raise RuntimeError(
            "IMDb are mai multe pagini decât limita de siguranță; sincronizarea a fost anulată "
            "pentru a evita un import parțial."
        )
    if not out and malformed_total:
        raise RuntimeError("IMDb nu a returnat niciun rating valid.")
    return out


def _metadata_payload(
    client: requests.Session,
    ids: list[str],
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    try:
        return _graphql_post(
            client,
            _TITLE_METADATA_QUERY,
            {"ids": ids},
            timeout=timeout,
        )
    except RuntimeError:
        # Credits are less stable than core title fields. Fetch the stable core
        # first, then retry directors separately so one credits-schema failure
        # does not erase genres/runtime/rating metadata for the whole batch.
        core = _graphql_post(
            client,
            _TITLE_METADATA_CORE_QUERY,
            {"ids": ids},
            timeout=timeout,
        )
        try:
            credits = _graphql_post(
                client,
                _TITLE_CREDITS_QUERY,
                {"ids": ids},
                timeout=timeout,
            )
        except RuntimeError:
            return core

        credit_by_id = {
            str(item.get("id") or ""): item.get("principalCredits") or []
            for item in ((credits.get("data") or {}).get("titles") or [])
            if isinstance(item, dict)
        }
        for item in ((core.get("data") or {}).get("titles") or []):
            if isinstance(item, dict):
                item["principalCredits"] = credit_by_id.get(str(item.get("id") or ""), [])
        return core


def backfill_public_rating_metadata(
    db: Database,
    *,
    limit: int = 5000,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> int:
    """Best-effort metadata fill for all rated IMDb-linked titles with missing fields."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.id,m.imdb_id,m.title,m.original_title,m.year,m.title_type,
                      m.runtime_min,m.genres_json,m.directors_json,m.imdb_rating,
                      m.num_votes,m.poster_url
               FROM movies m
               JOIN ratings r ON r.movie_id=m.id
               WHERE m.imdb_id IS NOT NULL
                 AND TRIM(m.imdb_id)!=''
                 AND (
                     m.imdb_rating IS NULL OR m.num_votes IS NULL OR
                     m.runtime_min IS NULL OR
                     TRIM(COALESCE(m.genres_json,'')) IN ('','[]') OR
                     TRIM(COALESCE(m.directors_json,'')) IN ('','[]') OR
                     m.poster_url IS NULL OR TRIM(m.poster_url)=''
                 )
               ORDER BY COALESCE(r.date_rated,'') DESC,r.id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    if not rows:
        return 0

    by_imdb = {str(row["imdb_id"]): row for row in rows}
    client = session or requests.Session()
    enriched = 0
    now = utcnow_iso()

    for start in range(0, len(by_imdb), 60):
        ids = list(by_imdb)[start:start + 60]
        try:
            payload = _metadata_payload(client, ids, timeout=timeout)
        except RuntimeError:
            continue
        items = (payload.get("data") or {}).get("titles") or []
        if not isinstance(items, list):
            continue

        with db.tx() as con:
            for meta in items:
                if not isinstance(meta, dict):
                    continue
                iid = str(meta.get("id") or "")
                row = by_imdb.get(iid)
                if row is None:
                    continue

                rating_summary = meta.get("ratingsSummary") or {}
                try:
                    imdb_rating = (
                        float(rating_summary.get("aggregateRating"))
                        if rating_summary.get("aggregateRating") is not None else None
                    )
                except (TypeError, ValueError):
                    imdb_rating = None
                try:
                    num_votes = (
                        int(rating_summary.get("voteCount"))
                        if rating_summary.get("voteCount") is not None else None
                    )
                except (TypeError, ValueError):
                    num_votes = None

                runtime = meta.get("runtime") or {}
                try:
                    runtime_min = (
                        int(round(int(runtime.get("seconds")) / 60))
                        if runtime.get("seconds") is not None else None
                    )
                except (TypeError, ValueError):
                    runtime_min = None

                genres: list[str] = []
                for genre_row in ((meta.get("genres") or {}).get("genres") or []):
                    if isinstance(genre_row, dict):
                        value = str(genre_row.get("text") or "").strip()
                        if value and value not in genres:
                            genres.append(value)

                directors: list[str] = []
                for group in meta.get("principalCredits") or []:
                    if not isinstance(group, dict):
                        continue
                    category = group.get("category") or {}
                    category_id = str(category.get("id") or "").lower()
                    category_text = str(category.get("text") or "").lower()
                    if category_id != "director" and "director" not in category_text:
                        continue
                    for credit in group.get("credits") or []:
                        name = ((credit or {}).get("name") or {}).get("nameText") or {}
                        value = str(name.get("text") or "").strip()
                        if value and value not in directors:
                            directors.append(value)

                poster_url = str(((meta.get("primaryImage") or {}).get("url") or "")).strip() or None
                use_genres = genres if genres else None
                use_directors = directors if directors else None
                changed_fields: list[str] = []
                if row["runtime_min"] is None and runtime_min is not None:
                    changed_fields.append("runtime_min")
                if not (json_loads(row["genres_json"], []) or []) and use_genres:
                    changed_fields.append("genres")
                if not (json_loads(row["directors_json"], []) or []) and use_directors:
                    changed_fields.append("directors")
                if row["imdb_rating"] is None and imdb_rating is not None:
                    changed_fields.append("imdb_rating")
                if row["num_votes"] is None and num_votes is not None:
                    changed_fields.append("num_votes")
                if not str(row["poster_url"] or "").strip() and poster_url:
                    changed_fields.append("poster_url")

                movie = Movie(
                    id=int(row["id"]),
                    imdb_id=iid,
                    title=str(row["title"] or ""),
                    original_title=str(row["original_title"] or row["title"] or ""),
                    year=int(row["year"]) if row["year"] is not None else None,
                    title_type=str(row["title_type"] or "Movie"),
                    runtime_min=runtime_min or row["runtime_min"],
                    genres=genres,
                    directors=directors,
                    imdb_rating=imdb_rating,
                    num_votes=num_votes,
                    poster_url=poster_url,
                )
                semantic = extract_semantic(movie)

                con.execute(
                    """UPDATE movies SET
                         runtime_min=COALESCE(runtime_min,?),
                         genres_json=CASE WHEN genres_json='[]' AND ?!='[]' THEN ? ELSE genres_json END,
                         directors_json=CASE WHEN directors_json='[]' AND ?!='[]' THEN ? ELSE directors_json END,
                         imdb_rating=COALESCE(imdb_rating,?),
                         num_votes=COALESCE(num_votes,?),
                         poster_url=COALESCE(NULLIF(poster_url,''),?),
                         semantic_json=CASE WHEN semantic_json='{}' THEN ? ELSE semantic_json END,
                         updated_at=?
                       WHERE id=?""",
                    (
                        runtime_min,
                        json_dumps(use_genres or []), json_dumps(use_genres or []),
                        json_dumps(use_directors or []), json_dumps(use_directors or []),
                        imdb_rating, num_votes, poster_url,
                        json_dumps(semantic), now, int(row["id"]),
                    ),
                )
                if changed_fields:
                    record_metadata_sources(
                        con,
                        int(row["id"]),
                        changed_fields,
                        "imdb_graphql",
                        updated_at=now,
                    )
                    enriched += 1
    return enriched


_IMDB_RATING_SOURCES = ("imdb", "imdb_public_sync", "imdb_csv")
_PUBLIC_ALIAS_SETTING = "imdb_public_id_aliases"


def _manual_identity_candidate(con, item: RemoteRating):
    """Reconcile only against an unbound/manual row, never a different IMDb title."""
    original = item.original_title or item.title
    ident = identity_key(item.title, original, item.year, item.title_type)
    row = con.execute(
        """SELECT * FROM movies
           WHERE imdb_id IS NULL AND identity_key=?
           ORDER BY id LIMIT 1""",
        (ident,),
    ).fetchone()
    if row is not None:
        return row
    tn = normalize_text(item.title)
    on = normalize_text(original)
    return con.execute(
        """SELECT * FROM movies
           WHERE imdb_id IS NULL
             AND year IS ?
             AND LOWER(COALESCE(title_type,''))=LOWER(?)
             AND (title_norm IN (?,?) OR original_title_norm IN (?,?))
           ORDER BY id LIMIT 1""",
        (item.year, item.title_type, tn, on, tn, on),
    ).fetchone()


def _aliases_with_con(con) -> dict[str, str]:
    row = con.execute(
        "SELECT value_json FROM settings WHERE key=?",
        (_PUBLIC_ALIAS_SETTING,),
    ).fetchone()
    raw = json_loads(row[0], {}) if row else {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if re.fullmatch(r"tt\d+", str(k or "")) and re.fullmatch(r"tt\d+", str(v or ""))
    }


def _save_aliases_with_con(con, aliases: dict[str, str], now: str) -> None:
    con.execute(
        """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             value_json=excluded.value_json,updated_at=excluded.updated_at""",
        (_PUBLIC_ALIAS_SETTING, json_dumps(aliases), now),
    )


def _movie_richness(row) -> int:
    score = 0
    if row["imdb_rating"] is not None:
        score += 2
    if int(row["num_votes"] or 0) > 0:
        score += 1
    if row["runtime_min"] is not None:
        score += 1
    if str(row["genres_json"] or "[]") != "[]":
        score += 2
    if str(row["directors_json"] or "[]") != "[]":
        score += 2
    if str(row["poster_url"] or "").strip():
        score += 1
    return score


def _same_title_year_candidates(con, item: RemoteRating, *, exclude_movie_id: int | None = None):
    original = item.original_title or item.title
    tn = normalize_text(item.title)
    on = normalize_text(original)
    params: list[Any] = [item.year, tn, on, tn, on]
    sql = """SELECT m.*,r.rating AS user_rating,r.date_rated AS user_date,
                    r.source AS rating_source
             FROM movies m
             LEFT JOIN ratings r ON r.movie_id=m.id
             WHERE m.year IS ?
               AND (m.title_norm IN (?,?) OR m.original_title_norm IN (?,?))"""
    if exclude_movie_id is not None:
        sql += " AND m.id<>?"
        params.append(int(exclude_movie_id))
    sql += " ORDER BY m.id"
    return con.execute(sql, tuple(params)).fetchall()


def _export_candidate(con, item: RemoteRating, *, exclude_movie_id: int | None = None):
    """Find one canonical local/export row for a public-profile identity alias."""
    candidates = _same_title_year_candidates(
        con, item, exclude_movie_id=exclude_movie_id
    )
    scored: list[tuple[int, Any]] = []
    for row in candidates:
        if str(row["imdb_id"] or "") == item.imdb_id:
            continue
        score = 0
        source = str(row["rating_source"] or "")
        same_rating = row["user_rating"] is not None and int(row["user_rating"]) == int(item.rating)
        same_date = (
            not item.date_rated
            or not row["user_date"]
            or str(row["user_date"])[:10] == str(item.date_rated)[:10]
        )
        if source in {"imdb", "imdb_csv"} and same_rating and same_date:
            score += 120
        elif source == "imdb_public_sync" and same_rating and same_date:
            score += 25

        movie_source = str(row["source"] or "")
        if movie_source in {"imdb_csv", "imdb_dataset", "catalog_csv"}:
            score += 15
        if normalize_text(row["original_title"] or row["title"]) == normalize_text(item.original_title or item.title):
            score += 10
        score += min(10, _movie_richness(row))
        scored.append((score, row))

    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], int(pair[1]["id"])))
    if scored[0][0] < 120:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _repair_candidate_for_sparse_public(con, public_row):
    """Repair rows created by 3.9.8/3.9.9 when the exported movie already existed."""
    item = RemoteRating(
        imdb_id=str(public_row["imdb_id"] or ""),
        title=str(public_row["title"] or ""),
        original_title=str(public_row["original_title"] or public_row["title"] or ""),
        year=int(public_row["year"]) if public_row["year"] is not None else None,
        title_type=str(public_row["title_type"] or "Movie"),
        rating=int(public_row["user_rating"]),
        date_rated=str(public_row["user_date"] or "") or None,
    )
    candidates = _same_title_year_candidates(
        con, item, exclude_movie_id=int(public_row["id"])
    )
    scored: list[tuple[int, Any]] = []
    public_richness = _movie_richness(public_row)
    for row in candidates:
        if not row["imdb_id"]:
            continue
        score = 0
        same_rating = row["user_rating"] is not None and int(row["user_rating"]) == int(item.rating)
        same_date = (
            not item.date_rated
            or not row["user_date"]
            or str(row["user_date"])[:10] == str(item.date_rated)[:10]
        )
        source = str(row["rating_source"] or "")
        if source in {"imdb", "imdb_csv"} and same_rating and same_date:
            score += 140
        if row["user_rating"] is None and str(row["source"] or "") != "imdb_public_sync":
            richness_gap = _movie_richness(row) - public_richness
            if richness_gap >= 3:
                score += 105 + min(10, richness_gap)
        if normalize_text(row["original_title"] or row["title"]) == normalize_text(item.original_title or item.title):
            score += 10
        scored.append((score, row))

    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], int(pair[1]["id"])))
    if scored[0][0] < 110:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _merge_public_duplicate_with_con(
    con,
    public_row,
    canonical,
    result: SyncResult,
    aliases: dict[str, str],
    now: str,
) -> None:
    public_id = int(public_row["id"])
    canonical_id = int(canonical["id"])
    public_imdb = str(public_row["imdb_id"] or "")
    canonical_imdb = str(canonical["imdb_id"] or "")
    public_rating = int(public_row["user_rating"])
    public_date = str(public_row["user_date"] or "") or None

    current = con.execute(
        "SELECT rating,date_rated,source FROM ratings WHERE movie_id=?",
        (canonical_id,),
    ).fetchone()
    if current is None:
        con.execute(
            """UPDATE ratings SET movie_id=?,source=?,updated_at=?
               WHERE movie_id=?""",
            (
                canonical_id,
                "imdb" if str(canonical["source"] or "") == "imdb_csv" else "imdb_public_sync",
                now,
                public_id,
            ),
        )
    else:
        current_date = str(current["date_rated"] or "")
        should_update = (
            int(current["rating"]) != public_rating
            and (not current_date or not public_date or public_date >= current_date[:10])
        )
        if should_update:
            con.execute(
                """UPDATE ratings SET rating=?,date_rated=COALESCE(?,date_rated),
                   updated_at=? WHERE movie_id=?""",
                (public_rating, public_date, now, canonical_id),
            )
        con.execute("DELETE FROM ratings WHERE movie_id=?", (public_id,))

    con.execute(
        """INSERT OR IGNORE INTO watchlist(movie_id,status,added_at,updated_at)
           SELECT ?,status,added_at,updated_at FROM watchlist WHERE movie_id=?""",
        (canonical_id, public_id),
    )
    con.execute("DELETE FROM watchlist WHERE movie_id=?", (public_id,))
    con.execute("UPDATE feedback SET movie_id=? WHERE movie_id=?", (canonical_id, public_id))
    con.execute(
        "UPDATE recommendation_history SET movie_id=? WHERE movie_id=?",
        (canonical_id, public_id),
    )

    if public_imdb and canonical_imdb:
        aliases[public_imdb] = canonical_imdb

    refs = int(con.execute(
        """SELECT
             (SELECT COUNT(*) FROM ratings WHERE movie_id=?)
           + (SELECT COUNT(*) FROM feedback WHERE movie_id=?)
           + (SELECT COUNT(*) FROM recommendation_history WHERE movie_id=?)
           + (SELECT COUNT(*) FROM recommendation_trust_audit WHERE movie_id=?)
           + (SELECT COUNT(*) FROM watchlist WHERE movie_id=?)""",
        (public_id, public_id, public_id, public_id, public_id),
    ).fetchone()[0])
    if refs == 0 and str(public_row["source"] or "") == "imdb_public_sync":
        con.execute("DELETE FROM movies WHERE id=?", (public_id,))

    result.reconciled_duplicates.append(
        (str(public_row["title"] or ""), public_imdb, canonical_imdb)
    )


def _repair_public_sync_duplicates_with_con(
    con,
    result: SyncResult,
    aliases: dict[str, str],
    now: str,
) -> None:
    rows = con.execute(
        """SELECT m.*,r.rating AS user_rating,r.date_rated AS user_date,
                  r.source AS rating_source
           FROM ratings r
           JOIN movies m ON m.id=r.movie_id
           WHERE r.source='imdb_public_sync'
             AND m.imdb_id IS NOT NULL
           ORDER BY r.id"""
    ).fetchall()
    for public_row in rows:
        canonical = _repair_candidate_for_sparse_public(con, public_row)
        if canonical is not None:
            _merge_public_duplicate_with_con(
                con, public_row, canonical, result, aliases, now
            )


def _resolve_public_movie(con, item: RemoteRating, aliases: dict[str, str]):
    canonical_id = aliases.get(item.imdb_id)
    if canonical_id:
        row = con.execute(
            "SELECT * FROM movies WHERE imdb_id=?",
            (canonical_id,),
        ).fetchone()
        if row is not None:
            return row, True

    row = con.execute(
        "SELECT * FROM movies WHERE imdb_id=?",
        (item.imdb_id,),
    ).fetchone()
    if row is not None:
        return row, False

    candidate = _export_candidate(con, item)
    if candidate is not None and candidate["imdb_id"]:
        aliases[item.imdb_id] = str(candidate["imdb_id"])
        return candidate, True

    row = _manual_identity_candidate(con, item)
    return row, False


def _upsert_with_con(
    con,
    item: RemoteRating,
    result: SyncResult,
    now: str,
    aliases: dict[str, str] | None = None,
    *,
    allow_create: bool = True,
) -> bool:
    aliases = aliases if aliases is not None else {}
    original = item.original_title or item.title
    ident = identity_key(item.title, original, item.year, item.title_type)
    movie, is_alias = _resolve_public_movie(con, item, aliases)
    if movie is None and not allow_create:
        return False

    if movie is None:
        cur = con.execute(
            """INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
               year,title_type,genres_json,directors_json,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.imdb_id, ident, item.title, original, normalize_text(item.title),
                normalize_text(original), item.year, item.title_type, json_dumps([]), json_dumps([]),
                "imdb_public_sync", now, now,
            ),
        )
        movie_id = int(cur.lastrowid)
        movie_source = "imdb_public_sync"
    else:
        movie_id = int(movie["id"])
        movie_source = str(movie["source"] or "")
        if not is_alias:
            con.execute(
                """UPDATE movies SET imdb_id=COALESCE(imdb_id,?),title=?,original_title=?,
                   title_norm=?,original_title_norm=?,year=COALESCE(?,year),
                   title_type=COALESCE(NULLIF(?,''),title_type),updated_at=? WHERE id=?""",
                (
                    item.imdb_id, item.title, original, normalize_text(item.title), normalize_text(original),
                    item.year, item.title_type, now, movie_id,
                ),
            )

    old = con.execute(
        "SELECT rating,date_rated,source FROM ratings WHERE movie_id=?",
        (movie_id,),
    ).fetchone()
    if old is None:
        source = "imdb_public_sync"
        if is_alias and movie_source == "imdb_csv":
            source = "imdb"
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, item.rating, item.date_rated, source, now, now),
        )
        result.new_ratings.append((item.title, item.rating))
    elif int(old["rating"]) != item.rating:
        previous = int(old["rating"])
        # Keep the exported CSV provenance when a public-profile alias updates it.
        source = str(old["source"] or "imdb_public_sync")
        con.execute(
            """UPDATE ratings SET rating=?,date_rated=COALESCE(?,date_rated),
               source=?,imported_at=?,updated_at=? WHERE movie_id=?""",
            (item.rating, item.date_rated, source, now, now, movie_id),
        )
        result.changed_ratings.append((item.title, previous, item.rating))
    else:
        if item.date_rated and str(old["date_rated"] or "") != item.date_rated:
            con.execute(
                """UPDATE ratings SET date_rated=?,imported_at=?,updated_at=?
                   WHERE movie_id=?""",
                (item.date_rated, now, now, movie_id),
            )
        result.unchanged += 1
    return True


def _upsert(db: Database, item: RemoteRating, result: SyncResult) -> None:
    """Compatibility wrapper used by tests/helpers."""
    now = utcnow_iso()
    with db.tx() as con:
        aliases = _aliases_with_con(con)
        _upsert_with_con(con, item, result, now, aliases)
        _save_aliases_with_con(con, aliases, now)


def sync_public_ratings(db: Database, profile_url: str, *, baseline_date: str | None = "2026-09-05",
                        session: requests.Session | None = None) -> SyncResult:
    items = fetch_public_ratings(profile_url, session=session)
    result = SyncResult(fetched=len(items))
    cutoff = date.fromisoformat(baseline_date) if baseline_date else None
    now = utcnow_iso()

    # The exported ratings CSV is the historical canonical dataset. Public sync
    # is an incremental updater and identity-alias source; it must never delete
    # or replace valid exported ratings merely because IMDb exposes another ID.
    with db.tx() as con:
        aliases = _aliases_with_con(con)

        # First repair duplicates created by older public-sync versions.
        _repair_public_sync_duplicates_with_con(
            con, result, aliases, now
        )

        for item in items:
            allow_create = True
            if cutoff:
                if not item.date_rated:
                    allow_create = False
                else:
                    try:
                        if date.fromisoformat(item.date_rated) <= cutoff:
                            result.stopped_at_baseline = True
                            allow_create = False
                    except ValueError:
                        allow_create = False

            # Pre-baseline rows may still update/confirm an existing exported
            # row, but they are not allowed to create a second historical movie.
            _upsert_with_con(
                con,
                item,
                result,
                now,
                aliases,
                allow_create=allow_create,
            )

        _save_aliases_with_con(con, aliases, now)
        con.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value_json=excluded.value_json,updated_at=excluded.updated_at""",
            ("imdb_public_sync_last_success", json_dumps(now), now),
        )
        con.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value_json=excluded.value_json,updated_at=excluded.updated_at""",
            ("imdb_public_sync_last_count", json_dumps(len(items)), now),
        )
    return result
