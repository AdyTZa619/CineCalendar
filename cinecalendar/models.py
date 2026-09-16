from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date

@dataclass
class Movie:
    id: int | None = None
    imdb_id: str | None = None
    title: str = ""
    original_title: str = ""
    year: int | None = None
    title_type: str = "Movie"
    runtime_min: int | None = None
    genres: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    overview: str = ""
    keywords: list[str] = field(default_factory=list)
    imdb_rating: float | None = None
    num_votes: int | None = None
    release_date: str | None = None
    poster_url: str | None = None
    source: str = "local"
    semantic: dict[str, float] = field(default_factory=dict)

@dataclass
class CalendarEvent:
    key: str
    name: str
    start: date
    end: date
    category: str
    importance: float
    themes: dict[str, float]
    direct_tags: set[str] = field(default_factory=set)
    historical_tags: set[str] = field(default_factory=set)
    spiritual_tags: set[str] = field(default_factory=set)
    atmosphere_tags: set[str] = field(default_factory=set)
    influence_before: int = 0
    influence_after: int = 0

@dataclass
class ScoreBreakdown:
    taste: float = 0.0
    semantic: float = 0.0
    calendar: float = 0.0
    season: float = 0.0
    director_cinema: float = 0.0
    novelty: float = 0.0
    diversity: float = 0.0
    quality: float = 0.0
    repeat_penalty: float = 0.0
    romance_penalty: float = 0.0
    final: float = 0.0
    calendar_kind: str = "slabă"
    calendar_reason: str = ""
    personal_reason: str = ""
    contributions: list[tuple[str, float, str]] = field(default_factory=list)
    predicted_rating: float = 0.0
    confidence: float = 0.0
    uncertainty: float = 1.0
    decision_mode: str = "decide"
    evidence: float = 0.0
    # Separate from the estimated 1-10 taste rating: how easy this already-good candidate looks
    # to actually start now after runtime, premise, quality and short-horizon intent are considered.
    startability: float = 0.0
    # Final V16 trust-gate snapshot. This is telemetry/explanation metadata; it does not change the
    # estimated personal rating and is persisted only when the recommendation exposure is recorded.
    trust_audit: dict = field(default_factory=dict)

@dataclass
class Recommendation:
    movie: Movie
    score: ScoreBreakdown
