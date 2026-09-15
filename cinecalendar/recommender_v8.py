from __future__ import annotations

from datetime import date

from .recommendation import row_to_movie
from .recommender_v7 import (
    EVENT_RELEVANCE_RULES,
    SEASONAL_EVENT_KEYS,
    FastRecommendationEngineV7,
)
from .util import normalize_text


ENGINE_VERSION = "8.0.0"


class FastRecommendationEngineV8(FastRecommendationEngineV7):
    """Make Program calendar useful as well as precise.

    v7 fixed false positives, but the ordinary balanced shortlist could still omit the few
    genuinely relevant titles for a specific feast or historical date.  v8 performs a small
    event-aware lookup against the *entire* local IMDb catalog first, validates every hit with
    v7's strict event rule, then injects those rows ahead of the normal taste/quality pool.

    The result is intentionally two-layered:
      1. factual/date-specific recommendations, when real matches exist;
      2. clearly-labelled personal/seasonal alternatives, never presented as factual ties.
    """

    EVENT_SCAN_LIMIT = 700

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self._calendar_candidate_mode = False
        self.last_event_candidate_count = 0

    @staticmethod
    def _rule_sql(rule: dict) -> tuple[str, list[str]]:
        clauses: list[str] = []
        params: list[str] = []

        # Official IMDb data always has titles.  Overview/keywords are often absent, but
        # become useful automatically after metadata enrichment, so search all four fields.
        for term in rule.get("terms", ()):
            norm = normalize_text(str(term))
            if not norm:
                continue
            like = f"%{norm}%"
            clauses.append(
                "(COALESCE(m.title_norm,'') LIKE ? OR COALESCE(m.original_title_norm,'') LIKE ? "
                "OR LOWER(COALESCE(m.overview,'')) LIKE ? OR LOWER(COALESCE(m.keywords_json,'')) LIKE ?)"
            )
            params.extend((like, like, like, like))

        # semantic_json is generated locally from title + genres even for the official IMDb
        # catalog, so distinctive tags such as passion_of_christ / holocaust remain useful
        # without needing an API key or a plot for every one of the ~260k titles.
        for tag in rule.get("any_tags", ()):
            clauses.append("COALESCE(m.semantic_json,'') LIKE ?")
            params.append(f'%"{tag}"%')

        for group in rule.get("all_tag_groups", ()):
            sub: list[str] = []
            for tag in group:
                sub.append("COALESCE(m.semantic_json,'') LIKE ?")
                params.append(f'%"{tag}"%')
            if sub:
                clauses.append("(" + " AND ".join(sub) + ")")

        if not clauses:
            return "", []
        return "(" + " OR ".join(clauses) + ")", params

    def _event_candidate_rows(self, when: date, limit: int | None = None):
        """Return strictly validated candidates for active concrete events from all movies."""
        limit = max(50, min(int(limit or self.EVENT_SCAN_LIMIT), 1200))
        active = [
            (ev, proximity)
            for ev, proximity in self.calendar.relevant_events(when)
            if ev.key not in SEASONAL_EVENT_KEYS and ev.category != "sezon"
            and ev.key in EVENT_RELEVANCE_RULES
        ]
        if not active:
            self.last_event_candidate_count = 0
            return []

        event_clauses: list[str] = []
        params: list[str] = []
        for ev, _proximity in active:
            clause, values = self._rule_sql(EVENT_RELEVANCE_RULES[ev.key])
            if clause:
                event_clauses.append(clause)
                params.extend(values)
        if not event_clauses:
            self.last_event_candidate_count = 0
            return []

        sql = (
            "SELECT m.* FROM movies m "
            "WHERE m.title_type IN ('movie','short','tvMovie','video','Movie','TV Movie','tv movie') "
            "AND (" + " OR ".join(event_clauses) + ") "
            "ORDER BY COALESCE(m.imdb_rating,0) DESC, COALESCE(m.num_votes,0) DESC LIMIT ?"
        )
        params.append(limit)
        blocked = self._blocked_identities()
        with self.db.connect() as con:
            raw = con.execute(sql, tuple(params)).fetchall()

        out = []
        seen: set[int] = set()
        for row in raw:
            mid = int(row["id"])
            if mid in seen or self._is_blocked(row, blocked):
                continue
            movie = row_to_movie(row)
            score, kind, _reason = self._calendar_score_cached(movie, when)
            if score < .12 or kind not in {"directă", "istorică", "spirituală"}:
                continue
            seen.add(mid)
            out.append(row)

        self.last_event_candidate_count = len(out)
        return out

    def _candidate_rows(self, when: date, limit: int = 100000):
        # Normal Home/Browse recommendations remain exactly on the fast balanced engine.
        base = super()._candidate_rows(when, limit)
        if not self._calendar_candidate_mode:
            return base

        event_rows = self._event_candidate_rows(when, self.EVENT_SCAN_LIMIT)
        if not event_rows:
            return base

        max_rows = max(400, min(int(limit or self.CALENDAR_POOL), self.EXPLORE_POOL))
        merged = []
        seen: set[int] = set()
        for row in list(event_rows) + list(base):
            mid = int(row["id"])
            if mid in seen:
                continue
            seen.add(mid)
            merged.append(row)
            if len(merged) >= max_rows:
                break
        self.last_candidate_count = len(merged)
        return merged

    def calendar_day_program(self, when: date | None = None, count_per_section: int = 6) -> dict:
        when = when or date.today()
        self._calendar_candidate_mode = True
        try:
            result = super().calendar_day_program(when, count_per_section)
        finally:
            self._calendar_candidate_mode = False

        result["event_candidate_count"] = int(self.last_event_candidate_count)
        sections = list(result.get("sections") or [])
        factual_keys = {"exact", "direct", "spiritual", "historical"}
        factual_count = sum(
            len(section.get("recommendations") or [])
            for section in sections
            if section.get("key") in factual_keys
        )

        # The season lane is still useful, but its old cards said "Legătura: slabă", which
        # made real personal recommendations look like failed calendar matches.  Re-label it
        # honestly and rank primarily by the final personal score.
        for section in sections:
            if section.get("key") != "season":
                continue
            recs = list(section.get("recommendations") or [])
            recs.sort(
                key=lambda r: (
                    float(r.score.final),
                    float(r.score.predicted_rating),
                    float(r.score.confidence),
                ),
                reverse=True,
            )
            section["recommendations"] = recs
            if factual_count:
                section["title"] = "Alternative pentru tine azi"
                section["subtitle"] = (
                    "Dacă nu vrei neapărat tema reperului zilei: filme alese după gustul tău "
                    "și momentul anului, separate clar de legătura factuală de mai sus."
                )
            else:
                section["title"] = "Recomandări pentru tine azi"
                section["subtitle"] = (
                    "Nu am găsit suficiente legături factuale sigure cu reperul zilei. "
                    "Acestea sunt recomandări personale reale, adaptate perioadei, fără să inventez o legătură."
                )
            for rec in recs:
                rec.score.calendar_kind = "sezonieră/personală"
                rec.score.calendar_reason = (
                    f"Recomandare după profilul tău și {result.get('phase') or 'perioada curentă'}; "
                    "nu este prezentată ca legătură factuală cu reperul zilei."
                )

        # Extremely rare fallback: if neither a factual nor a seasonal lane survives, the
        # calendar page must still fulfil its basic purpose and recommend something useful.
        if not sections:
            fallback = self.recommend(
                when=when,
                count=max(3, min(6, int(count_per_section or 6))),
                record=False,
                slot="calendar-personal-fallback",
                candidate_limit=self.NORMAL_POOL,
                mode="decide",
            )
            for rec in fallback:
                rec.score.calendar_kind = "personală"
                rec.score.calendar_reason = (
                    "Recomandare după ratingurile tale; nu există o legătură calendaristică factuală suficient de sigură."
                )
            if fallback:
                sections.append({
                    "key": "personal",
                    "title": "Recomandări pentru tine azi",
                    "subtitle": (
                        "Ziua nu are suficiente potriviri calendaristice verificabile, așa că îți arăt cele mai bune "
                        "opțiuni după gustul tău, fără etichete calendaristice inventate."
                    ),
                    "recommendations": fallback,
                })

        # Factual lanes always stay before atmospheric/personal alternatives.
        order = {"exact": 0, "direct": 1, "spiritual": 2, "historical": 3, "atmosphere": 4, "season": 5, "personal": 6}
        sections.sort(key=lambda s: order.get(str(s.get("key")), 99))
        result["sections"] = sections
        return result
