from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any
from urllib.parse import urlparse

import requests

from .db import Database
from .util import identity_key, json_dumps, normalize_text, utcnow_iso


GRAPHQL_URLS = (
    "https://caching.graphql.imdb.com/",
    "https://api.graphql.imdb.com/",
)
GRAPHQL_URL = GRAPHQL_URLS[0]
DEFAULT_PROFILE_URL = "https://www.imdb.com/user/p.666yozwb6likjcvvjlu2hwmtli/ratings/"
_PROFILE_RE = re.compile(r"^(?:p\\.[A-Za-z0-9_-]+|ur\\d+)$")

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
    unchanged: int = 0
    stopped_at_baseline: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.new_ratings or self.changed_ratings)


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
    if re.fullmatch(r"ur\\d+", profile_id or ""):
        return profile_id
    if not re.fullmatch(r"p\\.[A-Za-z0-9_-]+", profile_id or ""):
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
        if re.fullmatch(r"ur\\d+", user_id):
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
    if not re.fullmatch(r"tt\\d+", imdb_id) or not name or not 1 <= rating <= 10:
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


def _upsert_with_con(con, item: RemoteRating, result: SyncResult, now: str) -> None:
    original = item.original_title or item.title
    ident = identity_key(item.title, original, item.year, item.title_type)
    movie = con.execute("SELECT * FROM movies WHERE imdb_id=?", (item.imdb_id,)).fetchone()
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
    else:
        movie_id = int(movie["id"])
        con.execute(
            """UPDATE movies SET title=?,original_title=?,title_norm=?,original_title_norm=?,
               year=COALESCE(?,year),title_type=COALESCE(NULLIF(?,''),title_type),updated_at=? WHERE id=?""",
            (
                item.title, original, normalize_text(item.title), normalize_text(original),
                item.year, item.title_type, now, movie_id,
            ),
        )

    old = con.execute("SELECT rating,date_rated FROM ratings WHERE movie_id=?", (movie_id,)).fetchone()
    if old is None:
        con.execute(
            "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
            (movie_id, item.rating, item.date_rated, "imdb_public_sync", now, now),
        )
        result.new_ratings.append((item.title, item.rating))
    elif int(old["rating"]) != item.rating:
        previous = int(old["rating"])
        con.execute(
            "UPDATE ratings SET rating=?,date_rated=COALESCE(?,date_rated),source=?,imported_at=?,updated_at=? WHERE movie_id=?",
            (item.rating, item.date_rated, "imdb_public_sync", now, now, movie_id),
        )
        result.changed_ratings.append((item.title, previous, item.rating))
    else:
        if item.date_rated and str(old["date_rated"] or "") != item.date_rated:
            con.execute(
                "UPDATE ratings SET date_rated=?,source=?,imported_at=?,updated_at=? WHERE movie_id=?",
                (item.date_rated, "imdb_public_sync", now, now, movie_id),
            )
        result.unchanged += 1


def _upsert(db: Database, item: RemoteRating, result: SyncResult) -> None:
    """Compatibility wrapper; full sync uses one transaction for the entire profile."""
    now = utcnow_iso()
    with db.tx() as con:
        _upsert_with_con(con, item, result, now)


def sync_public_ratings(db: Database, profile_url: str, *, baseline_date: str | None = "2026-09-05",
                        session: requests.Session | None = None) -> SyncResult:
    items = fetch_public_ratings(profile_url, session=session)
    result = SyncResult(fetched=len(items))
    cutoff = date.fromisoformat(baseline_date) if baseline_date else None
    now = utcnow_iso()

    # One transaction for the entire profile. A 2,000+ rating account must not
    # perform thousands of BEGIN/COMMIT cycles on a portable HDD, and a failed
    # sync must never leave a half-imported profile.
    with db.tx() as con:
        for item in items:
            if cutoff:
                if not item.date_rated:
                    existing = con.execute(
                        "SELECT 1 FROM movies WHERE imdb_id=?", (item.imdb_id,)
                    ).fetchone()
                    if existing is None:
                        continue
                else:
                    try:
                        if date.fromisoformat(item.date_rated) <= cutoff:
                            result.stopped_at_baseline = True
                            continue
                    except ValueError:
                        existing = con.execute(
                            "SELECT 1 FROM movies WHERE imdb_id=?", (item.imdb_id,)
                        ).fetchone()
                        if existing is None:
                            continue
            _upsert_with_con(con, item, result, now)

        con.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value_json=excluded.value_json,updated_at=excluded.updated_at""",
            ("imdb_public_sync_last_success", json_dumps(now), now),
        )
    return result
