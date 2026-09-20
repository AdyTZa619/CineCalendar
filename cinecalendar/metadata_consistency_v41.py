from __future__ import annotations

from dataclasses import dataclass

from .util import json_loads, normalize_text


@dataclass(frozen=True)
class MetadataConsistencyReport:
    issue_count: int
    issues: tuple[str, ...]


def audit_metadata_consistency(db, limit: int = 5000) -> MetadataConsistencyReport:
    """Detect suspicious metadata combinations without rewriting valid values blindly."""
    issues: list[str] = []
    with db.connect() as con:
        rows = con.execute(
            """SELECT id,imdb_id,title,original_title,title_norm,original_title_norm,year,title_type,
                      runtime_min,genres_json,directors_json,countries_json
               FROM movies
               WHERE id IN (SELECT movie_id FROM ratings)
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()

        duplicates = con.execute(
            """SELECT COALESCE(NULLIF(original_title_norm,''),title_norm) AS n,year,
                      lower(COALESCE(title_type,'movie')) AS t,COUNT(*) AS c
               FROM movies
               WHERE id IN (SELECT movie_id FROM ratings)
                 AND COALESCE(NULLIF(original_title_norm,''),title_norm) IS NOT NULL
               GROUP BY n,year,t HAVING COUNT(*)>1
               ORDER BY c DESC LIMIT 50"""
        ).fetchall()

    for row in rows:
        title = str(row["original_title"] or row["title"] or "Titlu necunoscut")
        iid = str(row["imdb_id"] or "fără IMDb ID")
        runtime = row["runtime_min"]
        year = row["year"]
        genres = json_loads(row["genres_json"], []) or []
        directors = json_loads(row["directors_json"], []) or []
        countries = json_loads(row["countries_json"], []) or []
        normalized_title = normalize_text(str(row["title"] or ""))
        normalized_original = normalize_text(str(row["original_title"] or ""))

        flags: list[str] = []
        if not str(row["original_title"] or "").strip():
            flags.append("titlu original lipsă")
        if runtime is not None and (int(runtime) <= 0 or int(runtime) > 600):
            flags.append(f"durată suspectă {runtime} min")
        if year is not None and (int(year) < 1888 or int(year) > 2100):
            flags.append(f"an suspect {year}")
        if len(genres) > 12:
            flags.append("prea multe genuri")
        if len(directors) > 12:
            flags.append("prea mulți regizori")
        if len(countries) > 12:
            flags.append("prea multe țări")
        if normalized_title and normalized_original and normalized_title != normalized_original:
            # Different localized/original titles are normal. This branch intentionally does not
            # flag them; the audit only records actually suspicious combinations.
            pass
        if flags:
            issues.append(f"{title} [{iid}] — {', '.join(flags)}")

    for row in duplicates:
        issues.append(
            f"Identitate posibil duplicată: {row['n']} ({row['year'] or 'an ?'}) "
            f"• {row['t']} • {int(row['c'])} înregistrări"
        )

    unique = tuple(dict.fromkeys(issues))
    return MetadataConsistencyReport(len(unique), unique)
