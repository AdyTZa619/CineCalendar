from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Callable

from .models import Movie
from .semantic import extract_semantic
from .tmdb import IMG_BASE, TmdbProvider
from .util import identity_key, json_dumps, normalize_text, utcnow_iso


V5_ONLINE_DISCOVERY_VERSION = "v5-online-discovery-alpha1"
_DISCOVERY_IDS_SETTING = "v5_online_discovery_first_seen"
_LAST_RUN_SETTING = "v5_online_discovery_last_run"


class V5OnlineDiscovery:
    """Bounded open-world candidate discovery backed by TMDb.

    Network access happens only in refresh(). Normal recommendation reads the persisted candidate
    cache, so scoring remains fast and deterministic. Historical replay only sees candidates whose
    discovery timestamp existed by the replay date, preventing present-day TMDb relationships from
    leaking into the past.
    """

    def __init__(self, db, provider_factory: Callable | None = None):
        self.db = db
        self.provider_factory = provider_factory or TmdbProvider
        self._status = {
            "version": V5_ONLINE_DISCOVERY_VERSION,
            "state": "idle",
            "anchors": 0,
            "discovered": 0,
            "imported": 0,
            "cached": 0,
        }

    @staticmethod
    def _as_day(value: str) -> str:
        return str(value or "")[:10]

    def _first_seen(self) -> dict[int, str]:
        raw = self.db.get_setting(_DISCOVERY_IDS_SETTING, {}) or {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for key, value in raw.items():
            try:
                movie_id = int(key)
            except (TypeError, ValueError):
                continue
            if movie_id > 0 and str(value or ""):
                out[movie_id] = str(value)
        return out

    def cached_candidate_ids(self, when: date | None = None, limit: int = 300) -> list[int]:
        first_seen = self._first_seen()
        if not first_seen:
            return []
        ids = list(first_seen)
        rows_by_id = {}
        with self.db.connect() as con:
            for start in range(0, len(ids), 700):
                chunk = ids[start:start + 700]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"""SELECT m.id,m.release_date,m.year,
                               CASE WHEN r.movie_id IS NULL THEN 0 ELSE 1 END AS rated,
                               CASE WHEN EXISTS(
                                   SELECT 1 FROM feedback f
                                   WHERE f.movie_id=m.id
                                     AND f.kind IN ('not_interested','seen','never_similar')
                               ) THEN 1 ELSE 0 END AS blocked
                        FROM movies m
                        LEFT JOIN ratings r ON r.movie_id=m.id
                        WHERE m.id IN ({marks})""",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    rows_by_id[int(row["id"])] = row

        cutoff = when.isoformat() if when is not None else ""
        eligible = []
        for movie_id, seen_at in first_seen.items():
            row = rows_by_id.get(movie_id)
            if row is None or int(row["rated"] or 0) or int(row["blocked"] or 0):
                continue
            if when is not None:
                if self._as_day(seen_at) > cutoff:
                    continue
                release = self._as_day(row["release_date"])
                year = int(row["year"]) if row["year"] is not None else None
                if release and release > cutoff:
                    continue
                if year is not None and year > when.year:
                    continue
            eligible.append((str(seen_at), movie_id))
        # Newer discovery evidence first; fusion still decides final cross-lane priority.
        eligible.sort(reverse=True)
        return [movie_id for _seen, movie_id in eligible[: max(0, int(limit))]]

    def _anchors(self, limit: int) -> list[int]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.tmdb_id
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE r.rating>=8 AND m.tmdb_id IS NOT NULL
                   ORDER BY r.rating DESC,
                            COALESCE(r.date_rated,r.updated_at,'') DESC,
                            COALESCE(m.num_votes,0) DESC
                   LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        out = []
        seen = set()
        for row in rows:
            try:
                tmdb_id = int(row["tmdb_id"] or 0)
            except (TypeError, ValueError):
                continue
            if tmdb_id > 0 and tmdb_id not in seen:
                seen.add(tmdb_id)
                out.append(tmdb_id)
        return out

    def _upsert_details(self, details: dict) -> int | None:
        if not details or bool(details.get("adult")):
            return None
        external = details.get("external_ids") or {}
        imdb_id = str(external.get("imdb_id") or "").strip()
        if not imdb_id.startswith("tt"):
            return None
        title = str(details.get("title") or "").strip()
        if not title:
            return None
        original = str(details.get("original_title") or title).strip()
        release_date = str(details.get("release_date") or "").strip() or None
        year = None
        if release_date and len(release_date) >= 4 and release_date[:4].isdigit():
            year = int(release_date[:4])
        try:
            tmdb_id = int(details.get("id") or 0)
        except (TypeError, ValueError):
            tmdb_id = 0

        with self.db.connect() as con:
            existing = con.execute(
                "SELECT id FROM movies WHERE imdb_id=? LIMIT 1", (imdb_id,)
            ).fetchone()
        if existing is not None:
            movie_id = int(existing["id"])
            if tmdb_id > 0:
                with self.db.tx() as con:
                    con.execute(
                        "UPDATE movies SET tmdb_id=COALESCE(tmdb_id,?) WHERE id=?",
                        (tmdb_id, movie_id),
                    )
            return movie_id

        genres = [str(x.get("name") or "").strip() for x in details.get("genres") or []]
        genres = [x for x in genres if x]
        countries = [
            str(x.get("name") or "").strip()
            for x in details.get("production_countries") or []
        ]
        countries = [x for x in countries if x]
        crew = (details.get("credits") or {}).get("crew") or []
        directors = [
            str(x.get("name") or "").strip()
            for x in crew
            if str(x.get("job") or "") == "Director" and str(x.get("name") or "").strip()
        ]
        raw_keywords = (details.get("keywords") or {}).get("keywords") or (
            details.get("keywords") or {}
        ).get("results") or []
        keywords = [str(x.get("name") or "").strip() for x in raw_keywords]
        keywords = [x for x in keywords if x]
        poster_path = str(details.get("poster_path") or "").strip()
        poster_url = IMG_BASE + poster_path if poster_path else None
        runtime = details.get("runtime")
        try:
            runtime = int(runtime) if runtime else None
        except (TypeError, ValueError):
            runtime = None

        movie = Movie(
            imdb_id=imdb_id,
            title=title,
            original_title=original,
            year=year,
            title_type="movie",
            runtime_min=runtime,
            genres=genres,
            directors=directors,
            countries=countries,
            overview=str(details.get("overview") or "").strip(),
            keywords=keywords,
            release_date=release_date,
            poster_url=poster_url,
            source="v5_online_tmdb",
        )
        movie.semantic = extract_semantic(movie)
        now = utcnow_iso()
        with self.db.tx() as con:
            con.execute(
                """INSERT OR IGNORE INTO movies(
                    imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                    year,title_type,runtime_min,genres_json,directors_json,countries_json,
                    overview,keywords_json,semantic_json,imdb_rating,num_votes,release_date,
                    poster_url,source,created_at,updated_at,tmdb_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    imdb_id,
                    identity_key(title, original, year, "movie"),
                    title,
                    original,
                    normalize_text(title),
                    normalize_text(original),
                    year,
                    "movie",
                    runtime,
                    json_dumps(genres),
                    json_dumps(directors),
                    json_dumps(countries),
                    movie.overview,
                    json_dumps(keywords),
                    json_dumps(movie.semantic),
                    None,
                    None,
                    release_date,
                    poster_url,
                    "v5_online_tmdb",
                    now,
                    now,
                    tmdb_id if tmdb_id > 0 else None,
                ),
            )
            row = con.execute("SELECT id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()
        return int(row["id"]) if row is not None else None

    def refresh(
        self,
        *,
        force: bool = False,
        anchor_limit: int = 8,
        related_per_anchor: int = 12,
        import_limit: int = 36,
    ) -> dict:
        token = str(self.db.get_setting("tmdb_token", "") or "").strip()
        if not token:
            self._status = {
                "version": V5_ONLINE_DISCOVERY_VERSION,
                "state": "disabled_no_tmdb_token",
                "anchors": 0,
                "discovered": 0,
                "imported": 0,
                "cached": len(self._first_seen()),
            }
            return dict(self._status)

        last = str(self.db.get_setting(_LAST_RUN_SETTING, "") or "")
        if last and not force:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_hours = (
                    datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)
                ).total_seconds() / 3600.0
                if age_hours < 24.0:
                    self._status = {
                        "version": V5_ONLINE_DISCOVERY_VERSION,
                        "state": "fresh_cache",
                        "anchors": 0,
                        "discovered": 0,
                        "imported": 0,
                        "cached": len(self._first_seen()),
                    }
                    return dict(self._status)
            except ValueError:
                pass

        anchors = self._anchors(max(1, int(anchor_limit)))
        if not anchors:
            self._status = {
                "version": V5_ONLINE_DISCOVERY_VERSION,
                "state": "waiting_for_enriched_anchors",
                "anchors": 0,
                "discovered": 0,
                "imported": 0,
                "cached": len(self._first_seen()),
            }
            return dict(self._status)

        provider = self.provider_factory(self.db, token)
        evidence = defaultdict(float)
        for anchor in anchors:
            try:
                related = provider.related_movie_ids(anchor, limit=max(1, int(related_per_anchor)))
            except Exception:
                continue
            for rank, tmdb_id in enumerate(related, 1):
                evidence[int(tmdb_id)] += 1.0 / (20.0 + rank)

        ranked = sorted(evidence, key=lambda mid: evidence[mid], reverse=True)
        now = utcnow_iso()
        first_seen = self._first_seen()
        imported = 0
        accepted = 0
        for tmdb_id in ranked:
            if accepted >= max(1, int(import_limit)):
                break
            try:
                details = provider.movie_details_by_tmdb(tmdb_id)
                movie_id = self._upsert_details(details)
            except Exception:
                continue
            if not movie_id:
                continue
            accepted += 1
            if movie_id not in first_seen:
                first_seen[movie_id] = now
                imported += 1

        self.db.set_setting(
            _DISCOVERY_IDS_SETTING,
            {str(movie_id): seen for movie_id, seen in first_seen.items()},
        )
        self.db.set_setting(_LAST_RUN_SETTING, now)
        self._status = {
            "version": V5_ONLINE_DISCOVERY_VERSION,
            "state": "ready",
            "anchors": len(anchors),
            "discovered": len(ranked),
            "accepted": accepted,
            "imported": imported,
            "cached": len(first_seen),
        }
        return dict(self._status)

    def status(self) -> dict:
        status = dict(self._status)
        status["cached"] = len(self._first_seen())
        return status
