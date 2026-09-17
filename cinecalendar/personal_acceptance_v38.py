from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import csv
import hashlib
import json
import sqlite3
import time

from .calendar_engine import orthodox_easter
from .calendar_engine_v3 import ContextCalendarEngineV35
from .db import Database
from .production_engine import build_production_recommender, production_stack_status
from .quality_manager_v37 import RecommendationQualityManagerV37
from .recommendation_backtest import run_local_backtest


PERSONAL_ACCEPTANCE_VERSION = "personal-acceptance-v3.8.0"


def _issue(level: str, code: str, message: str, **details) -> dict:
    payload = {"level": level, "code": code, "message": message}
    if details:
        payload["details"] = details
    return payload


def acceptance_dates(anchor: date) -> list[dict]:
    """Dates that must exercise the contexts CineCalendar promises to understand."""
    year = anchor.year
    easter = orthodox_easter(year)
    cases = [
        {"date": anchor, "label": "data auditului", "expected": set()},
        {"date": date(year, 1, 15), "label": "Ziua Culturii Naționale", "expected": {"romanian_culture"}},
        {"date": date(year, 3, 8), "label": "Ziua Internațională a Femeii", "expected": {"womens_day"}},
        {"date": easter - timedelta(days=28), "label": "Postul Mare", "expected": {"great_lent"}},
        {"date": easter, "label": "Sfintele Paști", "expected": {"easter"}},
        {"date": date(year, 5, 1), "label": "Ziua Muncii", "expected": {"labour_day"}},
        {"date": date(year, 6, 1), "label": "Ziua Copilului", "expected": {"children_day"}},
        {"date": date(year, 8, 7), "label": "Postul Adormirii", "expected": {"dormition_fast"}},
        {"date": date(year, 9, 1), "label": "1 septembrie", "expected": {"ww2_start", "september_transition"}},
        {"date": date(year, 9, 14), "label": "Înălțarea Sfintei Cruci", "expected": {"exaltation_cross"}},
        {"date": date(year, 12, 1), "label": "Ziua Națională a României", "expected": {"romania_national"}},
        {"date": date(year, 12, 10), "label": "Postul Nașterii Domnului", "expected": {"nativity_fast"}},
        {"date": date(year, 12, 25), "label": "Nașterea Domnului", "expected": {"nativity"}},
    ]
    out: list[dict] = []
    seen: set[date] = set()
    for item in cases:
        when = item["date"]
        if when in seen:
            # Keep the named contractual date rather than duplicating the same recommendation run.
            prior = next(x for x in out if x["date"] == when)
            prior["expected"].update(item["expected"])
            if prior["label"] == "data auditului":
                prior["label"] = item["label"]
            continue
        seen.add(when)
        out.append({"date": when, "label": item["label"], "expected": set(item["expected"])})
    return out


def _event_summary(calendar: ContextCalendarEngineV35, when: date) -> dict:
    context = calendar.day_context(when)
    events = []
    for event, proximity in list(context.get("events") or []):
        events.append(
            {
                "key": str(event.key),
                "name": str(event.name),
                "category": str(event.category),
                "importance": round(float(event.importance), 6),
                "proximity": round(float(proximity), 6),
            }
        )
    return {
        "date": when.isoformat(),
        "phase": str(context.get("phase") or ""),
        "rhythm": str(context.get("rhythm") or ""),
        "context_strength": round(float(context.get("context_strength", 0.0) or 0.0), 6),
        "explanation": str(context.get("explanation") or ""),
        "events": events,
    }


def _known_future_release(rec, when: date) -> tuple[bool, str]:
    movie = rec.movie
    raw = str(getattr(movie, "release_date", "") or "").strip()
    if raw:
        try:
            released = date.fromisoformat(raw[:10])
            if released > when:
                return True, f"release_date {released.isoformat()} > {when.isoformat()}"
        except ValueError:
            pass
    year = getattr(movie, "year", None)
    if year is not None:
        try:
            if int(year) > when.year:
                return True, f"year {int(year)} > {when.year}"
        except (TypeError, ValueError):
            pass
    return False, ""


def _recommendation_summary(rec, rank: int) -> dict:
    score = rec.score
    audit = dict(getattr(score, "trust_audit", {}) or {})
    return {
        "rank": int(rank),
        "movie_id": int(rec.movie.id or 0),
        "imdb_id": str(rec.movie.imdb_id or ""),
        "title": str(rec.movie.title or ""),
        "year": rec.movie.year,
        "release_date": rec.movie.release_date,
        "final": round(float(score.final or 0.0), 6),
        "predicted_rating": round(float(score.predicted_rating or 0.0), 4),
        "confidence": round(float(score.confidence or 0.0), 6),
        "calendar": round(float(score.calendar or 0.0), 6),
        "season": round(float(score.season or 0.0), 6),
        "calendar_kind": str(score.calendar_kind or ""),
        "calendar_reason": str(score.calendar_reason or ""),
        "personal_reason": str(score.personal_reason or ""),
        "trust_status": str(audit.get("status") or ""),
        "red_flag": bool(audit.get("red_flag")),
        "period_context_slot": bool(audit.get("period_context_slot")),
        "top3_role": str(audit.get("top3_role") or ""),
        "period_context_signal": audit.get("period_context_signal"),
    }


def _blocked_sets(db: Database) -> tuple[set[int], set[int]]:
    with db.connect() as con:
        rated = {int(row[0]) for row in con.execute("SELECT movie_id FROM ratings").fetchall()}
        seen = {
            int(row[0])
            for row in con.execute("SELECT DISTINCT movie_id FROM feedback WHERE kind='seen'").fetchall()
        }
    return rated, seen


def _validate_top3(recs, when: date, engine, rated: set[int], seen: set[int]) -> tuple[list[dict], list[dict]]:
    failures: list[dict] = []
    warnings: list[dict] = []
    ids = [int(rec.movie.id or 0) for rec in recs]
    if len(ids) != len(set(ids)):
        failures.append(_issue("fail", "duplicate_top3", "Top 3 conține același movie_id de mai multe ori."))

    period = []
    for rank, rec in enumerate(recs, start=1):
        mid = int(rec.movie.id or 0)
        if mid in rated:
            failures.append(_issue("fail", "rated_movie_leak", "Un film deja evaluat a intrat în Top 3.", rank=rank, movie_id=mid))
        if mid in seen:
            failures.append(_issue("fail", "seen_movie_leak", "Un film marcat seen a intrat în Top 3.", rank=rank, movie_id=mid))
        future, reason = _known_future_release(rec, when)
        if future:
            failures.append(_issue("fail", "future_release_leak", "Un film cunoscut ca nelansat la data simulată a intrat în Top 3.", rank=rank, reason=reason))
        trust = dict(getattr(rec.score, "trust_audit", {}) or {})
        if bool(trust.get("red_flag")) or str(trust.get("status") or "") == "red_flag":
            failures.append(_issue("fail", "red_flag_visible", "Un red flag a intrat în Top 3.", rank=rank, movie_id=mid))
        if bool(trust.get("period_context_slot")):
            period.append((rank, rec))

    if len(period) > 1:
        failures.append(_issue("fail", "multiple_period_slots", "Contextul perioadei a ocupat mai mult de un slot din Top 3."))
    if period:
        rank, rec = period[0]
        if rank == 1:
            failures.append(_issue("fail", "period_replaced_anchor", "Contextul perioadei a înlocuit alegerea personală principală."))
        anchor = recs[0]
        predicted_floor = float(getattr(engine, "PERIOD_PREDICTED_FLOOR", 6.35))
        final_gap = float(getattr(engine, "PERIOD_FINAL_GAP", 0.085))
        calendar_min = float(getattr(engine, "PERIOD_CALENDAR_MIN", 0.16))
        season_min = float(getattr(engine, "PERIOD_SEASON_MIN", 0.68))
        predicted = float(rec.score.predicted_rating or 0.0)
        gap = float(anchor.score.final or 0.0) - float(rec.score.final or 0.0)
        if predicted < predicted_floor:
            failures.append(_issue("fail", "weak_period_slot", "Slotul contextual a coborât sub pragul personal.", predicted=predicted, floor=predicted_floor))
        if gap > final_gap + 1e-9:
            failures.append(_issue("fail", "period_slot_score_gap", "Slotul contextual este prea departe de alegerea principală.", gap=gap, maximum=final_gap))
        if float(rec.score.calendar or 0.0) < calendar_min and float(rec.score.season or 0.0) < season_min:
            failures.append(_issue("fail", "period_slot_without_context", "Slotul contextual nu are suficient semnal calendaristic/sezonier."))

    if recs and len(recs) < 3:
        warnings.append(_issue("warning", "short_top3", "Catalogul eligibil nu a furnizat trei recomandări.", returned=len(recs)))
    return failures, warnings


def _catalog_counts(db: Database) -> dict:
    with db.connect() as con:
        movies = int(con.execute("SELECT COUNT(*) FROM movies").fetchone()[0])
        ratings = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
        seen = int(con.execute("SELECT COUNT(DISTINCT movie_id) FROM feedback WHERE kind='seen'").fetchone()[0])
        unseen = int(
            con.execute(
                """SELECT COUNT(*) FROM movies m
                   WHERE NOT EXISTS(SELECT 1 FROM ratings r WHERE r.movie_id=m.id)
                     AND NOT EXISTS(SELECT 1 FROM feedback f WHERE f.movie_id=m.id AND f.kind='seen')"""
            ).fetchone()[0]
        )
        quick = str(con.execute("PRAGMA quick_check").fetchone()[0])
    return {"movies": movies, "ratings": ratings, "seen_feedback": seen, "unseen_candidates": unseen, "quick_check": quick}


def _header(fieldnames: list[str] | None, names: tuple[str, ...]) -> str | None:
    if not fieldnames:
        return None
    lowered = {str(x).strip().lower(): str(x) for x in fieldnames}
    for name in names:
        found = lowered.get(name.lower())
        if found:
            return found
    return None


def csv_fingerprint(db: Database, path: str | Path) -> dict:
    """Compare an IMDb export with the DB using aggregates only; never emit personal rating rows."""
    path = Path(path).expanduser().resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pairs: dict[str, int] = {}
    duplicates = 0
    date_min = ""
    date_max = ""
    rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        const_h = _header(reader.fieldnames, ("Const", "IMDb ID", "imdb_id", "tconst"))
        rating_h = _header(reader.fieldnames, ("Your Rating", "YourRating", "rating"))
        date_h = _header(reader.fieldnames, ("Date Rated", "DateRated", "date_rated"))
        if const_h is None or rating_h is None:
            raise ValueError("CSV IMDb fără coloanele Const/Your Rating.")
        for row in reader:
            iid = str(row.get(const_h) or "").strip()
            raw_rating = str(row.get(rating_h) or "").strip()
            if not iid or not raw_rating:
                continue
            rows += 1
            if iid in pairs:
                duplicates += 1
            try:
                pairs[iid] = int(float(raw_rating))
            except ValueError:
                continue
            if date_h:
                value = str(row.get(date_h) or "").strip()[:10]
                if value:
                    date_min = value if not date_min or value < date_min else date_min
                    date_max = value if not date_max or value > date_max else date_max

    with db.connect() as con:
        db_rows = con.execute(
            """SELECT m.imdb_id,r.rating FROM ratings r
               JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id IS NOT NULL AND m.imdb_id<>''"""
        ).fetchall()
    db_pairs = {str(row[0]): int(row[1]) for row in db_rows}
    common = set(pairs).intersection(db_pairs)
    mismatches = sum(1 for iid in common if pairs[iid] != db_pairs[iid])
    return {
        "name": path.name,
        "sha256": digest,
        "rows": rows,
        "unique_imdb_ids": len(pairs),
        "duplicate_imdb_ids": duplicates,
        "date_min": date_min or None,
        "date_max": date_max or None,
        "db_imdb_ratings": len(db_pairs),
        "exact_rating_matches": sum(1 for iid in common if pairs[iid] == db_pairs[iid]),
        "rating_mismatches": mismatches,
        "missing_in_db": len(set(pairs) - set(db_pairs)),
        "extra_in_db": len(set(db_pairs) - set(pairs)),
    }


def _wait_for_als(engine, timeout: float) -> dict:
    engine.collaborative.start_background()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        status = dict(engine.collaborative.status())
        if engine.collaborative.is_ready() or status.get("state") == "error":
            return status
        time.sleep(0.25)
    return dict(engine.collaborative.status())


def run_personal_acceptance(
    db_path: str | Path,
    *,
    anchor: date | None = None,
    ratings_csv: str | Path | None = None,
    candidate_limit: int = 2200,
    final_limit: int = 100,
    als_timeout: float = 180.0,
    run_backtest: bool = True,
) -> dict:
    """Audit the exact local production stack without writing recommendation history.

    The database stays on the user's machine. Live recommendations use record=False; the temporal
    backtest makes its own SQLite backup before hiding future ratings.
    """
    anchor = anchor or date.today()
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    db = Database(db_path)
    counts = _catalog_counts(db)
    manager = RecommendationQualityManagerV37(db)
    preferred_cls = manager.preferred_engine_class()
    calendar = ContextCalendarEngineV35()
    engine = build_production_recommender(db, preferred_cls, calendar)
    stack = production_stack_status(engine)
    quality = manager.status()
    rated, seen = _blocked_sets(db)

    failures: list[dict] = []
    warnings: list[dict] = []
    required_stack = {
        "availability_guard": True,
        "context_guard": True,
        "calendar_class": "ContextCalendarEngineV35",
        "adaptive_class": "AdaptivePreferenceLearnerV2",
        "watch_intent_class": "WatchSuccessIntentLearnerV33",
    }
    for key, expected in required_stack.items():
        if stack.get(key) != expected:
            failures.append(_issue("fail", "production_stack_mismatch", f"Stiva de producție nu respectă contractul 3.8 pentru {key}.", expected=expected, actual=stack.get(key)))

    als = _wait_for_als(engine, als_timeout)
    if not engine.collaborative.is_ready():
        warnings.append(_issue("warning", "als_not_ready", "ALS nu a devenit disponibil în fereastra auditului; recomandările live sunt verificate pe fallback-ul sigur.", state=als.get("state"), error=als.get("error")))

    calendar_cases = []
    for case in acceptance_dates(anchor):
        when = case["date"]
        context = _event_summary(calendar, when)
        event_keys = {event["key"] for event in context["events"]}
        missing = sorted(set(case["expected"]) - event_keys)
        if missing:
            failures.append(_issue("fail", "calendar_contract_missing", "Calendarul 3.8 nu a recunoscut un reper obligatoriu.", date=when.isoformat(), label=case["label"], missing=missing))

        recs = []
        live_error = ""
        if counts["unseen_candidates"] > 0:
            try:
                recs = list(
                    engine.recommend(
                        when=when,
                        count=3,
                        record=False,
                        candidate_limit=max(400, int(candidate_limit)),
                    )
                )
            except Exception as exc:
                live_error = str(exc)
                failures.append(_issue("fail", "live_recommendation_error", "Motorul real a eșuat în acceptance.", date=when.isoformat(), error=live_error))
        summaries = [_recommendation_summary(rec, rank) for rank, rec in enumerate(recs, start=1)]
        top_fail, top_warn = _validate_top3(recs, when, engine, rated, seen)
        failures.extend(top_fail)
        warnings.extend(top_warn)

        strong_context = float(context.get("context_strength", 0.0) or 0.0) >= 0.58
        if strong_context and recs:
            has_context = any(
                float(rec.score.calendar or 0.0) >= 0.12
                or bool((getattr(rec.score, "trust_audit", {}) or {}).get("period_context_slot"))
                for rec in recs
            )
            if not has_context:
                warnings.append(_issue("warning", "strong_date_without_safe_context_pick", "Data are context puternic, dar niciun finalist sigur din Top 3 nu are legătură suficientă. Nu se forțează un film mai slab.", date=when.isoformat(), label=case["label"]))

        calendar_cases.append(
            {
                "date": when.isoformat(),
                "label": case["label"],
                "expected_event_keys": sorted(case["expected"]),
                "context": context,
                "top3": summaries,
                "live_error": live_error or None,
            }
        )

    if counts["unseen_candidates"] <= 0:
        warnings.append(_issue("warning", "no_live_unseen_catalog", "Baza conține numai titluri deja văzute/evaluate; testul live Top 3 este imposibil, dar holdout-ul temporal poate valida recomandarea personală."))

    backtest = None
    if run_backtest:
        try:
            backtest = run_local_backtest(
                db_path,
                engine_cls=preferred_cls,
                candidate_limit=max(400, int(candidate_limit)),
                final_limit=max(25, int(final_limit)),
                als_timeout=max(5.0, float(als_timeout)),
            )
            outcomes = dict(backtest.get("top3_outcomes") or {})
            if int(outcomes.get("matched_hidden_future", 0) or 0) > 0 and float(outcomes.get("disaster_4_minus_rate", 0.0) or 0.0) > 0:
                failures.append(_issue("fail", "hidden_future_disaster", "În holdout, Top 3 a expus cel puțin un film pe care ulterior l-ai notat 1–4.", outcomes=outcomes))
            mean_actual = outcomes.get("mean_actual_rating")
            if mean_actual is not None and float(mean_actual) < 6.0:
                warnings.append(_issue("warning", "weak_hidden_future_mean", "Filmele Top 3 care au putut fi verificate ulterior au o medie sub 6/10.", mean_actual_rating=mean_actual))
        except Exception as exc:
            warnings.append(_issue("warning", "temporal_backtest_unavailable", "Backtestul temporal nu a putut fi finalizat; nu este tratat ca dovadă de regresie, dar acceptance-ul personal rămâne incomplet.", error=str(exc)))

    csv_report = None
    if ratings_csv is not None:
        try:
            csv_report = csv_fingerprint(db, ratings_csv)
            if int(csv_report.get("duplicate_imdb_ids", 0) or 0) > 0:
                failures.append(_issue("fail", "csv_duplicate_imdb", "Exportul IMDb conține IMDb ID-uri duplicate.", count=csv_report["duplicate_imdb_ids"]))
            if int(csv_report.get("rating_mismatches", 0) or 0) > 0:
                warnings.append(_issue("warning", "csv_db_rating_mismatch", "Unele ratinguri din export diferă de baza locală actuală.", count=csv_report["rating_mismatches"]))
        except Exception as exc:
            failures.append(_issue("fail", "csv_fingerprint_error", "Exportul IMDb nu a putut fi verificat.", error=str(exc)))

    status = "fail" if failures else ("warning" if warnings else "pass")
    return {
        "audit_version": PERSONAL_ACCEPTANCE_VERSION,
        "generated_for_date": anchor.isoformat(),
        "privacy": "Ratingurile și baza SQLite sunt procesate local; raportul nu necesită încărcarea istoricului personal pe GitHub.",
        "status": status,
        "database": counts,
        "ratings_csv": csv_report,
        "quality_manager": {
            "preferred_engine": str(quality.get("preferred_engine") or preferred_cls.__name__),
            "baseline_engine": str(quality.get("baseline_engine") or ""),
            "status": str(quality.get("status") or ""),
            "using_previous_validated_engine": bool(quality.get("using_previous_validated_engine")),
            "selected_share": quality.get("selected_share"),
            "window_count": quality.get("window_count"),
        },
        "production_stack": stack,
        "als": als,
        "calendar_cases": calendar_cases,
        "temporal_backtest": backtest,
        "failures": failures,
        "warnings": warnings,
        "summary": {
            "failure_count": len(failures),
            "warning_count": len(warnings),
            "calendar_cases": len(calendar_cases),
            "live_cases_with_top3": sum(1 for item in calendar_cases if item.get("top3")),
            "temporal_backtest_completed": isinstance(backtest, dict),
        },
    }


def write_report(report: dict, path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
