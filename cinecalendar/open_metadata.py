from __future__ import annotations

"""Key-free movie metadata fallback built on Wikidata + Wikipedia.

IMDb's downloadable datasets intentionally do not include plots or posters. CineCalendar
therefore uses this provider only when richer metadata is missing and no TMDb result is
available. Wikidata metadata is CC0; Wikipedia extracts are displayed with source attribution
in the UI. Results are cached locally so the public services are not queried repeatedly.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

import requests

from .db import Database
from .models import Movie
from .semantic import extract_semantic
from .util import json_dumps, json_loads, utcnow_iso


WDQS = "https://query.wikidata.org/sparql"
USER_AGENT = "CineCalendar/2.1 personal desktop movie recommender (Wikimedia metadata fallback)"


class OpenMovieMetadataProvider:
    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _cached(self, key: str) -> dict | None:
        with self.db.connect() as con:
            row = con.execute(
                "SELECT payload_json,expires_at FROM metadata_cache WHERE provider='wikimedia' AND cache_key=?",
                (key,),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                    return None
            except ValueError:
                return None
        return json_loads(row["payload_json"], {})

    def _store(self, key: str, payload: dict, days: int = 30) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
        with self.db.tx() as con:
            con.execute(
                """INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at)
                   VALUES('wikimedia',?,?,?,?)
                   ON CONFLICT(provider,cache_key) DO UPDATE SET
                     payload_json=excluded.payload_json,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
                (key, json_dumps(payload), utcnow_iso(), expires),
            )

    @staticmethod
    def _article_summary(session: requests.Session, article_url: str) -> dict:
        if not article_url:
            return {}
        parsed = urlparse(article_url)
        host = parsed.netloc
        if not host.endswith("wikipedia.org"):
            return {}
        title = unquote(parsed.path.split("/wiki/", 1)[-1]).replace(" ", "_")
        if not title:
            return {}
        url = f"https://{host}/api/rest_v1/page/summary/{quote(title, safe='')}"
        response = session.get(url, timeout=(8, 15))
        if response.status_code != 200:
            return {}
        data = response.json()
        image = ""
        if isinstance(data.get("originalimage"), dict):
            image = data["originalimage"].get("source") or ""
        if not image and isinstance(data.get("thumbnail"), dict):
            image = data["thumbnail"].get("source") or ""
        return {
            "overview": (data.get("extract") or "").strip(),
            "image": image,
            "article": data.get("content_urls", {}).get("desktop", {}).get("page") or article_url,
        }

    def _fetch(self, imdb_id: str) -> dict:
        if not imdb_id or not imdb_id.startswith("tt") or not imdb_id[2:].isdigit():
            return {}
        query = f'''SELECT ?item ?itemDescription ?image ?duration ?directorLabel ?countryLabel ?roArticle ?enArticle WHERE {{
          ?item wdt:P345 "{imdb_id}" .
          OPTIONAL {{ ?item wdt:P18 ?image . }}
          OPTIONAL {{ ?item wdt:P2047 ?duration . }}
          OPTIONAL {{ ?item wdt:P57 ?director . }}
          OPTIONAL {{ ?item wdt:P495 ?country . }}
          OPTIONAL {{ ?roArticle schema:about ?item ; schema:isPartOf <https://ro.wikipedia.org/> . }}
          OPTIONAL {{ ?enArticle schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ro,en". }}
        }} LIMIT 30'''
        response = self.session.get(WDQS, params={"query": query, "format": "json"}, timeout=(10, 25))
        response.raise_for_status()
        bindings = response.json().get("results", {}).get("bindings", [])
        if not bindings:
            return {}

        first = bindings[0]
        directors: list[str] = []
        countries: list[str] = []
        for b in bindings:
            d = b.get("directorLabel", {}).get("value", "").strip()
            c = b.get("countryLabel", {}).get("value", "").strip()
            if d and d not in directors and not d.startswith("http"):
                directors.append(d)
            if c and c not in countries and not c.startswith("http"):
                countries.append(c)

        duration = None
        raw_duration = first.get("duration", {}).get("value")
        if raw_duration:
            try:
                candidate = int(round(float(raw_duration)))
                if 20 <= candidate <= 400:
                    duration = candidate
            except (TypeError, ValueError):
                pass

        article = first.get("roArticle", {}).get("value") or first.get("enArticle", {}).get("value") or ""
        summary = {}
        try:
            summary = self._article_summary(self.session, article)
        except requests.RequestException:
            summary = {}

        overview = summary.get("overview") or first.get("itemDescription", {}).get("value", "")
        image = summary.get("image") or first.get("image", {}).get("value", "")
        return {
            "overview": (overview or "").strip(),
            "poster_url": (image or "").strip(),
            "runtime_min": duration,
            "directors": directors[:6],
            "countries": countries[:6],
            "article_url": summary.get("article") or article,
            "attribution": "Wikidata / Wikipedia",
        }

    def enrich_by_imdb(self, movie: Movie) -> Movie:
        if not movie.imdb_id:
            return movie
        key = f"movie:{movie.imdb_id}"
        payload = self._cached(key)
        if payload is None:
            try:
                payload = self._fetch(movie.imdb_id)
            except requests.RequestException:
                return movie
            self._store(key, payload, days=30 if payload else 7)
        if not payload:
            return movie

        if not movie.overview:
            movie.overview = payload.get("overview") or ""
        if not movie.poster_url:
            movie.poster_url = payload.get("poster_url") or None
        if not movie.runtime_min and payload.get("runtime_min"):
            movie.runtime_min = int(payload["runtime_min"])
        if not movie.directors and payload.get("directors"):
            movie.directors = list(payload["directors"])
        if not movie.countries and payload.get("countries"):
            movie.countries = list(payload["countries"])
        movie.semantic = extract_semantic(movie)

        if movie.id is not None:
            with self.db.tx() as con:
                con.execute(
                    """UPDATE movies SET overview=?,runtime_min=?,directors_json=?,countries_json=?,
                       poster_url=?,semantic_json=?,updated_at=? WHERE id=?""",
                    (
                        movie.overview or "",
                        movie.runtime_min,
                        json_dumps(movie.directors),
                        json_dumps(movie.countries),
                        movie.poster_url,
                        json_dumps(movie.semantic),
                        utcnow_iso(),
                        movie.id,
                    ),
                )
        return movie
