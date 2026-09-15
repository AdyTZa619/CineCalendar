from __future__ import annotations

from datetime import date

from .recommender_v11 import FastRecommendationEngineV11
from .util import json_loads, normalize_text


ENGINE_VERSION = "12.0.0-als-daily-genre"


class FastRecommendationEngineV12(FastRecommendationEngineV11):
    """ALS recommender with an optional one-day genre choice.

    The user can say what kind of film they feel like watching *today* without permanently
    distorting their taste profile. The selection is a hard eligibility filter for that date
    only; ALS still ranks the surviving films. On the next day the filter is automatically
    inactive unless the user chooses a genre again.
    """

    def _daily_genre_payload(self) -> dict:
        payload = self.db.get_setting("daily_genre_filter", {})
        return payload if isinstance(payload, dict) else {}

    def _active_daily_genre(self, when: date) -> str:
        payload = self._daily_genre_payload()
        if str(payload.get("date") or "") != when.isoformat():
            return ""
        genre = str(payload.get("genre") or "").strip()
        if not genre or normalize_text(genre) in {"orice", "oricare", "any", "all"}:
            return ""
        return genre

    @staticmethod
    def _row_matches_genre(row, genre: str) -> bool:
        target = normalize_text(genre)
        if not target:
            return True
        genres = json_loads(row["genres_json"], []) or []
        return any(normalize_text(str(value)) == target for value in genres)

    def _state_token(self) -> tuple:
        base = super()._state_token()
        payload = self._daily_genre_payload()
        return base + ((str(payload.get("date") or ""), str(payload.get("genre") or "")),)

    def _candidate_rows(self, when: date, limit: int = 100000):
        genre = self._active_daily_genre(when)
        if not genre:
            return super()._candidate_rows(when, limit)

        # Pull the wider established pool first, then apply the explicit user intent. This keeps
        # the query HDD-friendly and avoids a full-table JSON scan of the ~260k-title catalog.
        requested = max(int(limit or self.EXPLORE_POOL), self.EXPLORE_POOL)
        rows = list(super()._candidate_rows(when, requested))
        filtered = [row for row in rows if self._row_matches_genre(row, genre)]
        self.last_candidate_count = len(filtered)
        return filtered
