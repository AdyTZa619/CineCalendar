from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .db import Database
from .util import json_dumps, json_loads, utcnow_iso


WDQS = "https://query.wikidata.org/sparql"
USER_AGENT = "CineCalendar/2.4 Romanian cinema discovery (Romanian-original-language verification)"
CACHE_PROVIDER = "romanian-cinema"
# v3 intentionally invalidates every earlier country-first cache. A title is eligible for the
# dedicated Romanian cinema lane only after the original language is verified as Romanian and
# Romania appears among the countries of origin. Country metadata alone is never enough.
CACHE_KEY = "wikidata-romanian-language-first-film-imdb-v3"


class RomanianCinemaProvider:
    """Discover Romanian cinema using language as the primary mandatory identity signal.

    Eligibility is deliberately fail-closed:
      * original language MUST include Romanian (Wikidata P364 = Q7913); and
      * Romania MUST be a country of origin (Wikidata P495 = Q218).

    This keeps genuine Romanian co-productions while rejecting Romania-only metadata entries
    whose original language is not Romanian. The local IMDb catalog currently has no trustworthy
    language field, so country-only local metadata is never promoted into this lane. If Wikidata
    is temporarily unavailable, only the last already-verified language-first cache is reused.
    """

    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
        self.last_source = "not-verified"
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
            "country_qid": "Q218",
            "original_language": "Romanian",
            "language_qid": "Q7913",
            "policy": "romanian-original-language-and-romania-origin",
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
        # Both P364 and P495 are mandatory. Language is the primary identity gate; country is
        # the national-production confirmation. No OR/UNION country-only escape hatch exists.
        return """SELECT DISTINCT ?imdb WHERE {
          ?item wdt:P345 ?imdb ;
                wdt:P364 wd:Q7913 ;
                wdt:P495 wd:Q218 ;
                wdt:P31/wdt:P279* wd:Q11424 .
          FILTER(STRSTARTS(STR(?imdb), "tt"))
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
        """Never infer Romanian-language identity from country-only local metadata."""
        self.last_local_count = 0
        return set()

    def imdb_ids(self, refresh: bool = False) -> set[str]:
        cached = None if refresh else self._cached()
        if cached is not None:
            self.last_source = "romanian-language-verified-cache"
            self.last_external_count = len(cached)
            self.last_local_count = 0
            self.last_error = ""
            return set(cached)

        stale = self._cached(allow_expired=True)
        try:
            external = self._fetch_wikidata_ids()
            if external:
                self._store(external)
            self.last_source = "romanian-language-wikidata"
            self.last_external_count = len(external)
            self.last_local_count = 0
            self.last_error = ""
            return external
        except requests.RequestException as exc:
            self.last_error = str(exc)
            self.last_local_count = 0
            if stale:
                self.last_source = "romanian-language-stale-cache"
                self.last_external_count = len(stale)
                return set(stale)
            # Fail closed. Showing no Romanian recommendation is better than labelling a film
            # Romanian only because a country field happens to contain Romania.
            self.last_source = "language-unverified-empty"
            self.last_external_count = 0
            return set()

    def status(self) -> dict:
        return {
            "source": self.last_source,
            "external_count": int(self.last_external_count),
            "local_count": int(self.last_local_count),
            "error": self.last_error,
            "policy": "romanian-original-language-and-romania-origin",
            "language_primary": True,
        }
