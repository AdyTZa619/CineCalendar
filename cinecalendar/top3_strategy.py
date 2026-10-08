from __future__ import annotations

from .models import Recommendation
from .semantic import feature_vector
from .util import clamp, cosine_sparse


TOP3_STRATEGY_VERSION = "top3-controlled-diversity-v1"
FINAL_GAP = 0.11
TRUST_FLOOR = 0.56


def _trust(rec: Recommendation) -> float:
    payload = getattr(rec.score, "trust_audit", {}) or {}
    value = payload.get("trust")
    return clamp(float(value)) if value is not None else 0.5


def _safe(rec: Recommendation) -> bool:
    payload = getattr(rec.score, "trust_audit", {}) or {}
    return str(payload.get("status") or "") != "red_flag" and not bool(payload.get("red_flag"))


def _diversity(rec: Recommendation, selected: list[Recommendation]) -> float:
    if not selected:
        return 1.0
    current = feature_vector(rec.movie)
    if not current:
        return 0.5
    similarities = []
    for chosen in selected:
        other = feature_vector(chosen.movie)
        if other:
            similarities.append(cosine_sparse(current, other))
    if not similarities:
        return 0.5
    return clamp(1.0 - max(similarities))


def _annotate(rec: Recommendation, role: str, reason: str) -> None:
    payload = dict(getattr(rec.score, "trust_audit", {}) or {})
    payload["top3_role"] = role
    payload["top3_strategy_version"] = TOP3_STRATEGY_VERSION
    rec.score.trust_audit = payload
    rec.score.contributions = [item for item in rec.score.contributions if item[0] != "Rol Top 3"]
    rec.score.contributions.insert(0, ("Rol Top 3", 0.0, reason))


def select_controlled_top3(ordered: list[Recommendation], requested: int = 3) -> tuple[list[Recommendation], list[str]]:
    requested = max(1, int(requested))
    if not ordered:
        return [], []
    if requested > 3:
        return list(ordered[:requested]), ["standard"] * min(requested, len(ordered))

    safe = [rec for rec in ordered if _safe(rec)]
    source = safe or list(ordered)
    anchor = source[0]
    chosen = [anchor]
    roles = ["principal"]
    _annotate(anchor, "principal", "Alegerea principală: cel mai puternic finalist sigur după gust și încredere.")
    if requested == 1:
        return chosen, roles

    anchor_final = float(anchor.score.final)
    remaining = [rec for rec in safe if rec is not anchor]
    context_pool = [
        rec for rec in remaining
        if anchor_final - float(rec.score.final) <= FINAL_GAP
        and float(rec.score.predicted_rating or 0.0) >= 6.4
    ]
    context_signal = [
        rec for rec in context_pool
        if max(float(rec.score.calendar or 0.0), float(rec.score.season or 0.0)) >= 0.30
    ]
    context_pool = context_signal or context_pool
    if context_pool:
        pick = max(
            context_pool,
            key=lambda rec: (
                0.52 * float(rec.score.final)
                + 0.23 * _trust(rec)
                + 0.17 * clamp(float(rec.score.calendar or 0.0))
                + 0.08 * clamp(float(rec.score.season or 0.0)),
                float(rec.score.predicted_rating or 0.0),
            ),
        )
    else:
        pick = remaining[0] if remaining else None
    if pick is not None:
        chosen.append(pick)
        roles.append("context")
        _annotate(pick, "context", "Alternativă apropiată, favorizată când ziua sau sezonul aduc context real.")
    if len(chosen) >= requested:
        return chosen[:requested], roles[:requested]

    remaining = [rec for rec in safe if rec not in chosen]
    explore_pool = [
        rec for rec in remaining
        if anchor_final - float(rec.score.final) <= FINAL_GAP
        and _trust(rec) >= TRUST_FLOOR
        and float(rec.score.predicted_rating or 0.0) >= 6.5
        and float(rec.score.confidence or 0.0) >= 0.40
    ]
    if explore_pool:
        def value(rec: Recommendation):
            diversity = _diversity(rec, chosen)
            predicted = clamp((float(rec.score.predicted_rating or 0.0) - 5.5) / 3.5)
            return (
                0.48 * float(rec.score.final)
                + 0.22 * _trust(rec)
                + 0.18 * diversity
                + 0.07 * clamp(float(rec.score.novelty or 0.0))
                + 0.05 * predicted,
                diversity,
                float(rec.score.predicted_rating or 0.0),
            )
        pick = max(explore_pool, key=value)
    else:
        pick = remaining[0] if remaining else None
    if pick is not None:
        chosen.append(pick)
        roles.append("explorare")
        _annotate(pick, "explorare", "Explorare controlată: diferit de primele două, dar în același culoar de calitate și siguranță.")

    for rec in ordered:
        if len(chosen) >= requested:
            break
        if rec in chosen:
            continue
        chosen.append(rec)
        roles.append("standard")
        _annotate(rec, "standard", "Rezultat suplimentar păstrat în ordinea standard.")
    return chosen[:requested], roles[:requested]
