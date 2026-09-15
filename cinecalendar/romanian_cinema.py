from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .db import Database
from .util import json_dumps, json_loads, normalize_text, utcnow_iso


WDQS = "https://query.wikidata.org/sparql"
USER_AGENT = "CineCalendar/2.4 Romanian cinema discovery (Wikidata country-of-origin lookup)"
CACHE_PROVIDER = "romanian-cinema"
# v2 deliberately drops the broad "Romania appears anywhere in P495" cache.  The main
# Romanian lane is now precision-first: Romania-only productions, plus co-productions whose
# original language is Romanian.  Broad co-productions no longer leak into the main list.
CACHE_KEY = "wikidata-strong-romanian-film-imdb-v2"


class RomanianCinemaProvider:
    """Discover films with a strong Romanian production identity.

    The old rule accepted every item for which Romania appeared anywhere in Wikidata P495.
    That is technically a Romanian co-production but, in practice, it allowed many films that
    do not read as Romanian cinema to dominate the page.  The primary lane is now intentionally
    stricter:
      * Romania is the only declared country of origin; OR
      * Romania is one of the countries of origin and Romanian is an original language.

    Local/offline fallback is stricter still because the local catalog does not reliably carry
    original-language metadata: only titles whose local country list is Romania-only are used.
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
    def _normalized_countries(countries) -> set[str]:
        return {
            normalize_text(str(value))
            for value in (countries or [])
            if normalize_text(str(value))
        }

    @classmethod
    def _romania_only(cls, countries) -> bool:
        values = cls._normalized_countries(countries)
        return values == {"romania"}

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
        payload = {
            "imdb_ids": sorted(ids),
            "country": "Romania",
            "wikidata_qid": "Q218",
            "policy": "romania-only-or-romanian-original-language",
        }
        with self.db.tx() as con:
            con.execute(
                """INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(provider,cache_key) DO UPDATE SET
                     payload_json=excluded.payload_json,fetched_at=excluded.fetched_at,
                     expires_at=excluded.expires_at""",
                (CACHE_PROVIDER, CACHE_KEY, json_dumps(payload), utcnow_iso(), expires),
            )

    @staticmethod
    def wikidata_query() -> str:
        # Q11424 = film, Q218 = Romania, Q7913 = Romanian language.
        # P31/P279 keeps non-film IMDb title entities out.  For co-productions we require
        # Romanian as an original language; otherwise Romania must be the only P495 country.
        return """SELECT DISTINCT ?imdb WHERE {
          ?item wdt:P345 ?imdb ;
                wdt:P495 wd:Q218 ;
                wdt:P31/wdt:P279* wd:Q11424 .
          FILTER(STRSTARTS(STR(?imdb), "tt"))
          {
            FILTER NOT EXISTS {
              ?item wdt:P495 ?otherCountry .
              FILTER(?otherCountry != wd:Q218)
            }
          }
          UNION
          {
            ?item wdt:P364 wd:Q7913 .
          }
        }
        LIMIT 10000"""

    def _fetch_wikidata_ids(self) -> set[str]:
        response = self.session.get(
            WDQS,
            params={"query": self.wikidata_query(), "format": "json"},
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
            # Offline local data has no trustworthy original-language field, so fail closed:
            # a mixed country list is a co-production and does not enter the main Romanian lane.
            if self._valid_imdb_id(iid) and self._romania_only(countries):
                out.add(iid)
        self.last_local_count = len(out)
        return out

    def imdb_ids(self, refresh: bool = False) -> set[str]:
        local = self.local_imdb_ids()
        cached = None if refresh else self._cached()
        if cached is not None:
            self.last_source = "strict-cache+local"
            self.last_external_count = len(cached)
            self.last_error = ""
            return set(cached) | local

        stale = self._cached(allow_expired=True)
        try:
            external = self._fetch_wikidata_ids()
            if external:
                self._store(external)
            self.last_source = "strict-wikidata+local"
            self.last_external_count = len(external)
            self.last_error = ""
            return external | local
        except requests.RequestException as exc:
            self.last_error = str(exc)
            if stale:
                self.last_source = "strict-stale-cache+local"
                self.last_external_count = len(stale)
                return set(stale) | local
            self.last_source = "romania-only-local"
            self.last_external_count = 0
            return local

    def status(self) -> dict:
        return {
            "source": self.last_source,
            "external_count": int(self.last_external_count),
            "local_count": int(self.last_local_count),
            "error": self.last_error,
            "policy": "romania-only-or-romanian-original-language",
        }
