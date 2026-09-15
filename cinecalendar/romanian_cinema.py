from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .db import Database
from .util import json_dumps, json_loads, normalize_text, utcnow_iso


WDQS = "https://query.wikidata.org/sparql"
USER_AGENT = "CineCalendar/2.4 Romanian cinema discovery (Wikidata country-of-origin lookup)"
CACHE_PROVIDER = "romanian-cinema"
CACHE_KEY = "wikidata-country-origin-ro-imdb-v1"


class RomanianCinemaProvider:
    """Discover Romanian productions without treating 'about Romania' as Romanian cinema.

    Eligibility is based on country of origin (Wikidata P495 = Romania/Q218), then intersected
    with the local IMDb catalog. Results are cached locally because this list changes slowly.
    Existing local country metadata is always merged in and provides an offline fallback.
    """

    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
        self.last_source = "local"
        self.last_external_count = 0
        self.last_local_count = 0
        self.last_error = ""

    @staticmethod
    def _valid_imdb_id(value: str) -> bool:
        value = str(value or "").strip()
        return value.startswith("tt") and value[2:].isdigit()

    @staticmethod
    def _extract_wikidata_ids(payload: dict) -> set[str]:
        out: set[str] = set()
        for row in (payload.get("results", {}) or {}).get("bindings", []) or []:
            value = str((row.get("imdb") or {}).get("value") or "").strip()
            if RomanianCinemaProvider._valid_imdb_id(value):
                out.add(value)
        return out

    @staticmethod
    def _contains_romania(countries) -> bool:
        return any(normalize_text(str(value)) == "romania" for value in (countries or []))

    def _cached(self, allow_expired: bool = False) -> set[str] | None:
        with self.db.connect() as con:
            row = con.execute(
                "SELECT payload_json,expires_at FROM metadata_cache WHERE provider=? AND cache_key=?",
                (CACHE_PROVIDER, CACHE_KEY),
            ).fetchone()
        if not row:
            return None
        if not allow_expired and row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                    return None
            except ValueError:
                return None
        payload = json_loads(row["payload_json"], {})
        values = payload.get("imdb_ids") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return None
        return {str(x) for x in values if self._valid_imdb_id(str(x))}

    def _store(self, ids: set[str], days: int = 90) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
        payload = {"imdb_ids": sorted(ids), "country": "Romania", "wikidata_qid": "Q218"}
        with self.db.tx() as con:
            con.execute(
                """INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(provider,cache_key) DO UPDATE SET
                     payload_json=excluded.payload_json,fetched_at=excluded.fetched_at,
                     expires_at=excluded.expires_at""",
                (CACHE_PROVIDER, CACHE_KEY, json_dumps(payload), utcnow_iso(), expires),
            )

    def _fetch_wikidata_ids(self) -> set[str]:
        # P495 is the production country. This deliberately avoids title/theme heuristics.
        query = """SELECT DISTINCT ?imdb WHERE {
          ?item wdt:P495 wd:Q218 ;
                wdt:P345 ?imdb .
          FILTER(STRSTARTS(STR(?imdb), "tt"))
        }
        LIMIT 10000"""
        response = self.session.get(
            WDQS,
            params={"query": query, "format": "json"},
            timeout=(10, 35),
        )
        response.raise_for_status()
        return self._extract_wikidata_ids(response.json())

    def local_imdb_ids(self) -> set[str]:
        out: set[str] = set()
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT imdb_id,countries_json FROM movies
                   WHERE imdb_id IS NOT NULL
                     AND countries_json IS NOT NULL
                     AND countries_json <> '[]'
                     AND (countries_json LIKE '%Romania%' OR countries_json LIKE '%România%')"""
            ).fetchall()
        for row in rows:
            countries = json_loads(row["countries_json"], []) or []
            iid = str(row["imdb_id"] or "")
            if self._valid_imdb_id(iid) and self._contains_romania(countries):
                out.add(iid)
        self.last_local_count = len(out)
        return out

    def imdb_ids(self, refresh: bool = False) -> set[str]:
        local = self.local_imdb_ids()
        cached = None if refresh else self._cached()
        if cached is not None:
            self.last_source = "cache+local"
            self.last_external_count = len(cached)
            self.last_error = ""
            return set(cached) | local

        stale = self._cached(allow_expired=True)
        try:
            external = self._fetch_wikidata_ids()
            if external:
                self._store(external)
            self.last_source = "wikidata+local"
            self.last_external_count = len(external)
            self.last_error = ""
            return external | local
        except requests.RequestException as exc:
            self.last_error = str(exc)
            if stale:
                self.last_source = "stale-cache+local"
                self.last_external_count = len(stale)
                return set(stale) | local
            self.last_source = "local-only"
            self.last_external_count = 0
            return local

    def status(self) -> dict:
        return {
            "source": self.last_source,
            "external_count": int(self.last_external_count),
            "local_count": int(self.last_local_count),
            "error": self.last_error,
        }
