from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

import requests

from .db import Database
from .util import json_dumps, json_loads, utcnow_iso


WDQS = "https://query.wikidata.org/sparql"
IMDB_GRAPHQL = "https://caching.graphql.imdb.com/"
TMDB_API = "https://api.themoviedb.org/3"
USER_AGENT = "CineCalendar/5 Romanian cinema multi-source verification"
CACHE_PROVIDER = "romanian-cinema"
# A positive IMDb language-search hit is a discovery signal: its constraint does not
# document original language. Admission needs Wikidata, TMDb original-language data,
# or the curated catalogue. Country-only local metadata remains support-only.
CACHE_KEY = "romanian-multisource-language-country-film-imdb-v5"


class RomanianCinemaProvider:
    """Discover genuinely Romanian-language Romanian productions from independent sources.

    Strong verification sources:
      * Wikidata: P364 Romanian + P495 Romania are both mandatory;
      * IMDb public GraphQL advanced search: language=ro + origin country=RO,
        used for discovery and corroboration, not sufficient alone;
      * TMDb, when the user's token is available: discover filters require both RO origin
        and Romanian original language, then only local TMDb/IMDb mappings are used;
      * the app's manually curated Romanian-film catalogue, once its entries are resolved to
        exact IMDb ids.

    Local countries_json is deliberately support-only because a Romanian co-production can
    have another original language. This provider therefore expands recall without reintroducing
    the old country-only false-positive problem.
    """

    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.last_source = "not-verified"
        self.last_external_count = 0
        self.last_local_count = 0
        self.last_error = ""
        self.last_source_counts: dict[str, int] = {}
        self.last_sources_available: list[str] = []
        self._evidence: dict[str, dict] = {}
        self.last_conflict_count = 0

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
    def _extract_imdb_graphql_ids(payload: dict) -> set[str]:
        out: set[str] = set()
        edges = (((payload.get("data") or {}).get("advancedTitleSearch") or {}).get("edges") or [])
        for edge in edges:
            title = ((edge or {}).get("node") or {}).get("title") or {}
            value = str(title.get("id") or "").strip()
            if RomanianCinemaProvider._valid_imdb_id(value):
                out.add(value)
        return out

    def _cached_payload(self, allow_expired: bool = False) -> dict | None:
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
        if not isinstance(payload, dict):
            return None
        if not allow_expired and payload.get("tmdb_detail_signature") != self._tmdb_detail_signature():
            # A newly enriched title can supply a contradiction before the 90-day
            # source snapshot expires. Re-evaluate it on the next Romanian request.
            return None
        return payload

    def _tmdb_detail_signature(self) -> list:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT COUNT(*),COALESCE(MAX(fetched_at),'') FROM metadata_cache
                   WHERE provider='tmdb' AND cache_key LIKE '/movie/%' AND expires_at > ?""",
                (utcnow_iso(),),
            ).fetchone()
        return [int(row[0]), str(row[1])]

    def _restore_status_from_payload(self, payload: dict) -> set[str] | None:
        values = payload.get("imdb_ids")
        if not isinstance(values, list):
            return None
        ids = {str(x) for x in values if self._valid_imdb_id(str(x))}
        raw_counts = payload.get("source_counts")
        self.last_source_counts = {
            str(key): int(value or 0)
            for key, value in (raw_counts.items() if isinstance(raw_counts, dict) else [])
        }
        available = payload.get("sources_available")
        self.last_sources_available = (
            [str(x) for x in available if str(x)]
            if isinstance(available, list) else sorted(self.last_source_counts)
        )
        self.last_local_count = int(payload.get("local_support_count", 0) or 0)
        raw_evidence = payload.get("evidence") or {}
        self._evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
        self.last_conflict_count = int(payload.get("conflict_count", 0) or 0)
        return ids

    def _cached(self, allow_expired: bool = False) -> set[str] | None:
        payload = self._cached_payload(allow_expired=allow_expired)
        return self._restore_status_from_payload(payload) if payload is not None else None

    def _store(
        self,
        ids: set[str],
        days: int = 90,
        *,
        source_sets: dict[str, set[str]] | None = None,
        local_support: set[str] | None = None,
        evidence: dict[str, dict] | None = None,
        conflict_count: int = 0,
    ) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
        source_sets = source_sets or {"verified": set(ids)}
        local_support = local_support or set()
        payload = {
            "imdb_ids": sorted(ids),
            "source_counts": {key: len(values) for key, values in source_sets.items()},
            "sources_available": sorted(source_sets),
            "sources": {key: sorted(values) for key, values in source_sets.items()},
            "local_support_count": len(local_support),
            "evidence": evidence or {},
            "conflict_count": int(conflict_count),
            "tmdb_detail_signature": self._tmdb_detail_signature(),
            "country": "Romania",
            "original_language": "Romanian",
            "policy": "multi-source-romanian-original-language-and-romania-origin",
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
            headers={"Accept": "application/sparql-results+json"},
            timeout=(10, 35),
        )
        response.raise_for_status()
        return self._extract_wikidata_ids(response.json())

    @staticmethod
    def imdb_query(start_year: int, end_year: int) -> str:
        start = max(1870, int(start_year))
        end = max(start, int(end_year))
        return f"""query RomanianCinema {{
          advancedTitleSearch(
            first: 999
            constraints: {{
              titleTypeConstraint: {{anyTitleTypeIds:["movie","short","tvMovie","video"]}}
              releaseDateConstraint: {{releaseDateRange:{{start:"{start}-01-01" end:"{end}-12-31"}}}}
              originCountryConstraint: {{anyCountries:["RO"]}}
              languageConstraint: {{anyLanguages:["ro"]}}
            }}
          ) {{
            edges {{ node {{ title {{ id }} }} }}
          }}
        }}"""

    def _fetch_imdb_ids(self) -> set[str]:
        current_year = datetime.now(timezone.utc).year
        out: set[str] = set()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/graphql+json, application/json",
            "Origin": "https://www.imdb.com",
            "Referer": "https://www.imdb.com/",
            "x-imdb-client-name": "imdb-web-next",
            "x-imdb-user-language": "ro-RO",
            "x-imdb-user-country": "RO",
            "User-Agent": USER_AGENT,
        }
        for start in range(1880, current_year + 1, 20):
            end = min(current_year, start + 19)
            response = self.session.post(
                IMDB_GRAPHQL,
                json={"query": self.imdb_query(start, end)},
                headers=headers,
                timeout=(10, 35),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise requests.RequestException("IMDb GraphQL returned errors")
            out.update(self._extract_imdb_graphql_ids(payload))
        return out

    def _curated_imdb_ids(self) -> set[str]:
        raw = self.db.get_setting("romanian_resolved_imdb_ids", {})
        if not isinstance(raw, dict):
            return set()
        return {
            str(value)
            for value in raw.values()
            if self._valid_imdb_id(str(value or ""))
        }

    def _local_country_support_ids(self) -> set[str]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT imdb_id FROM movies
                   WHERE imdb_id IS NOT NULL
                     AND (
                       countries_json LIKE '%Romania%'
                       OR countries_json LIKE '%România%'
                     )"""
            ).fetchall()
        out = {
            str(row["imdb_id"])
            for row in rows
            if self._valid_imdb_id(str(row["imdb_id"] or ""))
        }
        self.last_local_count = len(out)
        return out

    def _fetch_tmdb_ids(self) -> set[str] | None:
        token = str(self.db.get_setting("tmdb_token", "") or "").strip()
        if not token:
            return None

        headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
        discovered_tmdb: set[int] = set()
        page = 1
        total_pages = 1
        while page <= total_pages and page <= 500:
            response = self.session.get(
                f"{TMDB_API}/discover/movie",
                params={
                    "include_adult": "false",
                    "include_video": "true",
                    "with_origin_country": "RO",
                    "with_original_language": "ro",
                    "sort_by": "primary_release_date.asc",
                    "page": page,
                },
                headers=headers,
                timeout=(8, 25),
            )
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("results") or []:
                try:
                    tmdb_id = int(item.get("id") or 0)
                except (TypeError, ValueError):
                    tmdb_id = 0
                if tmdb_id > 0:
                    discovered_tmdb.add(tmdb_id)
            try:
                total_pages = max(1, min(500, int(payload.get("total_pages") or 1)))
            except (TypeError, ValueError):
                total_pages = 1
            page += 1

        out: set[str] = set()
        tmdb_values = sorted(discovered_tmdb)
        with self.db.connect() as con:
            for start in range(0, len(tmdb_values), 700):
                chunk = tmdb_values[start:start + 700]
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"""SELECT imdb_id FROM movies
                        WHERE tmdb_id IN ({marks})
                          AND imdb_id IS NOT NULL""",
                    tuple(chunk),
                ).fetchall()
                out.update(
                    str(row["imdb_id"])
                    for row in rows
                    if self._valid_imdb_id(str(row["imdb_id"] or ""))
                )
        return out

    def _cached_tmdb_details(self, candidates: set[str]) -> tuple[set[str], dict[str, list[str]]]:
        """Use only fresh, explicitly linked TMDb detail records; absence proves nothing.

        A discover search is a positive signal, but it cannot report why a title was
        omitted. Detail payloads contain the actual original language and production
        countries and can therefore supply either confirmation or a real conflict.
        """
        confirmed: set[str] = set()
        conflicts: dict[str, list[str]] = {}
        if not candidates:
            return confirmed, conflicts
        by_tmdb: dict[int, set[str]] = {}
        with self.db.connect() as con:
            ids = sorted(candidates)
            for start in range(0, len(ids), 700):
                chunk = ids[start:start + 700]
                marks = ",".join("?" for _ in chunk)
                for movie in con.execute(
                    f"SELECT imdb_id,tmdb_id FROM movies WHERE imdb_id IN ({marks}) AND tmdb_id IS NOT NULL",
                    chunk,
                ):
                    by_tmdb.setdefault(int(movie["tmdb_id"]), set()).add(str(movie["imdb_id"]))
            if not by_tmdb:
                return confirmed, conflicts
            rows = con.execute(
                """SELECT cache_key,payload_json FROM metadata_cache
                   WHERE provider='tmdb' AND cache_key LIKE '/movie/%' AND expires_at > ?""",
                (utcnow_iso(),),
            ).fetchall()
        for row in rows:
            match = re.match(r"^/movie/(\d+)\?", str(row["cache_key"]))
            if not match or int(match.group(1)) not in by_tmdb:
                continue
            tmdb_id = int(match.group(1))
            details = json_loads(row["payload_json"], {})
            if not isinstance(details, dict):
                continue
            try:
                if int(details.get("id") or 0) != tmdb_id:
                    continue
            except (ValueError, TypeError):
                continue
            linked_id = str((details.get("external_ids") or {}).get("imdb_id") or "")
            language = str(details.get("original_language") or "").lower()
            countries = details.get("production_countries")
            country_codes = {
                str(item.get("iso_3166_1") or "").upper()
                for item in countries if isinstance(item, dict) and item.get("iso_3166_1")
            } if isinstance(countries, list) else set()
            reasons = []
            if language and language != "ro":
                reasons.append(f"TMDb: limba originală {language}")
            if country_codes and "RO" not in country_codes:
                reasons.append("TMDb: țara de producție fără RO")
            for imdb_id in by_tmdb[tmdb_id]:
                if linked_id and linked_id != imdb_id:
                    conflicts.setdefault(imdb_id, []).append("TMDb: IMDb ID diferit")
                    continue
                if reasons:
                    for reason in reasons:
                        if reason not in conflicts.setdefault(imdb_id, []):
                            conflicts[imdb_id].append(reason)
                elif language == "ro" and "RO" in country_codes:
                    confirmed.add(imdb_id)
        return confirmed - set(conflicts), conflicts

    def evidence_for(self, imdb_id: str) -> dict:
        """Explain one decision without interpreting a missing search hit as a conflict."""
        if self.last_source == "not-verified":
            self.imdb_ids()
        item = self._evidence.get(str(imdb_id), {})
        return dict(item) if isinstance(item, dict) else {}

    def local_imdb_ids(self) -> set[str]:
        """Country-only local metadata is diagnostic support, never an admission source."""
        self._local_country_support_ids()
        return set()

    def imdb_ids(self, refresh: bool = False) -> set[str]:
        cached = None if refresh else self._cached()
        if cached is not None:
            self.last_source = "romanian-multisource-verified-cache"
            self.last_external_count = len(cached)
            self.last_error = ""
            return set(cached)

        stale = self._cached(allow_expired=True)
        stale_evidence = dict(self._evidence)
        errors: list[str] = []
        source_sets: dict[str, set[str]] = {}

        for name, fetcher in (
            ("wikidata", self._fetch_wikidata_ids),
            ("imdb", self._fetch_imdb_ids),
            ("curated", self._curated_imdb_ids),
            ("tmdb", self._fetch_tmdb_ids),
        ):
            try:
                values = fetcher()
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            if values is None:
                continue
            source_sets[name] = {
                str(value) for value in values if self._valid_imdb_id(str(value))
            }

        local_support = self._local_country_support_ids()
        discovered: set[str] = set()
        for values in source_sets.values():
            discovered.update(values)
        detail_confirmed, conflicts = self._cached_tmdb_details(discovered | (stale or set()))
        if detail_confirmed:
            source_sets.setdefault("tmdb_details", set()).update(detail_confirmed)
        verified = set().union(*(
            values for name, values in source_sets.items() if name != "imdb"
        )) if source_sets else set()
        evidence = {
            imdb_id: {
                "sources": sorted(name for name, values in source_sets.items() if imdb_id in values),
                "source_count": len({
                    "tmdb" if name == "tmdb_details" else name
                    for name, values in source_sets.items() if imdb_id in values
                }),
                "local_country_support": imdb_id in local_support,
                "conflicts": conflicts.get(imdb_id, []),
                "decision": (
                    "review" if imdb_id in conflicts else
                    "verified" if imdb_id in verified else "needs-original-language"
                ),
            }
            for imdb_id in discovered | detail_confirmed
        }
        verified.difference_update(conflicts)

        self.last_source_counts = {key: len(values) for key, values in source_sets.items()}
        self.last_sources_available = sorted(source_sets)
        self.last_external_count = len(verified)
        self._evidence = evidence
        self.last_conflict_count = len(conflicts)
        self.last_error = " | ".join(errors)

        if verified:
            self._store(
                verified,
                source_sets=source_sets,
                local_support=local_support,
                evidence=evidence,
                conflict_count=len(conflicts),
            )
            self.last_source = "romanian-multisource-verified"
            return verified

        stale_safe = (stale or set()) - set(conflicts)
        if stale_safe:
            self._evidence = {**stale_evidence, **evidence}
            for imdb_id, reasons in conflicts.items():
                self._evidence[imdb_id] = {
                    **self._evidence.get(imdb_id, {}),
                    "conflicts": reasons, "decision": "review",
                }
            self.last_source = "romanian-multisource-stale-cache"
            self.last_external_count = len(stale_safe)
            return stale_safe

        if conflicts:
            # A stale positive must not resurrect a title with explicit contradictory
            # details. Persist the review decision even when every candidate is held.
            self._store(set(), source_sets=source_sets, local_support=local_support,
                        evidence=evidence, conflict_count=len(conflicts))
            self.last_source = "romanian-conflicts-held-for-review"
            return set()

        self.last_source = "language-unverified-empty"
        self.last_external_count = 0
        return set()

    def status(self) -> dict:
        return {
            "source": self.last_source,
            "external_count": int(self.last_external_count),
            "local_count": int(self.last_local_count),
            "error": self.last_error,
            "policy": "multi-source-romanian-original-language-and-romania-origin",
            "language_primary": True,
            "source_counts": dict(self.last_source_counts),
            "sources_available": list(self.last_sources_available),
            "country_only_is_support": True,
            "conflict_count": self.last_conflict_count,
        }
