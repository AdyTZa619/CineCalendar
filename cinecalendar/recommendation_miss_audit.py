from __future__ import annotations


MISS_AUDIT_VERSION = "recommendation-miss-audit-v1"


def _stage_map(report: dict, stage: str) -> dict[str, dict]:
    trace = dict(report.get("hidden_trace") or {})
    rows = trace.get(stage) or []
    return {
        str(row.get("imdb_id") or ""): dict(row)
        for row in rows
        if str(row.get("imdb_id") or "")
    }


def build_miss_audit(
    baseline_reports: list[dict],
    discovery_reports: list[dict],
    adaptive_reports: list[dict] | None = None,
) -> dict:
    adaptive_reports = adaptive_reports or []
    counts = {
        "positive_targets": 0,
        "stable_visible_hits": 0,
        "recovered_by_discovery_retrieval": 0,
        "recovered_by_discovery_ranking": 0,
        "recovered_by_discovery_visible": 0,
        "recovered_by_adaptive_visible": 0,
        "retrieval_miss": 0,
        "ranking_miss": 0,
        "visible_miss": 0,
    }
    examples: list[dict] = []

    for idx, baseline in enumerate(baseline_reports):
        discovery = discovery_reports[idx] if idx < len(discovery_reports) else {}
        adaptive = adaptive_reports[idx] if idx < len(adaptive_reports) else {}
        b_c = _stage_map(baseline, "candidate")
        b_f = _stage_map(baseline, "final")
        b_t = _stage_map(baseline, "top3")
        d_c = _stage_map(discovery, "candidate")
        d_f = _stage_map(discovery, "final")
        d_t = _stage_map(discovery, "top3")
        a_t = _stage_map(adaptive, "top3")
        cutoff = str(baseline.get("cutoff_date") or "")

        targets = {
            iid: row for iid, row in b_c.items()
            if int(row.get("rating") or 0) >= 8
        }
        for iid, target in targets.items():
            counts["positive_targets"] += 1
            rating = int(target.get("rating") or 0)
            bcr = b_c.get(iid, {}).get("rank")
            bfr = b_f.get(iid, {}).get("rank")
            btr = b_t.get(iid, {}).get("rank")
            dcr = d_c.get(iid, {}).get("rank")
            dfr = d_f.get(iid, {}).get("rank")
            dtr = d_t.get(iid, {}).get("rank")
            atr = a_t.get(iid, {}).get("rank")

            if btr is not None:
                category = "stable_visible_hit"
                counts["stable_visible_hits"] += 1
                reason = "Stabil l-a pus deja în Top 3."
            elif bcr is None:
                if dcr is not None:
                    category = "recovered_by_discovery_retrieval"
                    counts[category] += 1
                    reason = "Stabil nu l-a găsit în candidați; Descoperire l-a recuperat."
                else:
                    category = "retrieval_miss"
                    counts[category] += 1
                    reason = "Nici Stabil, nici Descoperire nu l-au găsit în pool-ul de candidați."
            elif bfr is None:
                if dfr is not None:
                    category = "recovered_by_discovery_ranking"
                    counts[category] += 1
                    reason = "Era candidat în Stabil, dar doar Descoperire l-a adus în lista finală."
                else:
                    category = "ranking_miss"
                    counts[category] += 1
                    reason = "A fost găsit ca candidat, dar rankingul nu l-a ridicat în lista finală."
            else:
                if dtr is not None:
                    category = "recovered_by_discovery_visible"
                    counts[category] += 1
                    reason = "Stabil îl avea în lista finală, iar Descoperire l-a ridicat în Top 3."
                elif atr is not None:
                    category = "recovered_by_adaptive_visible"
                    counts[category] += 1
                    reason = "Adaptiv l-a ridicat în Top 3 după personalizare."
                else:
                    category = "visible_miss"
                    counts[category] += 1
                    reason = "Filmul bun exista în lista finală, dar nu a ajuns în cele trei opțiuni."

            if len(examples) < 80 and category != "stable_visible_hit":
                examples.append({
                    "imdb_id": iid,
                    "rating": rating,
                    "cutoff_date": cutoff,
                    "category": category,
                    "reason": reason,
                    "stable_candidate_rank": bcr,
                    "stable_final_rank": bfr,
                    "stable_top3_rank": btr,
                    "discovery_candidate_rank": dcr,
                    "discovery_final_rank": dfr,
                    "discovery_top3_rank": dtr,
                    "adaptive_top3_rank": atr,
                })

    total = max(1, int(counts["positive_targets"]))
    return {
        "version": MISS_AUDIT_VERSION,
        "counts": counts,
        "stable_visible_hit_rate": round(counts["stable_visible_hits"] / total, 6),
        "retrieval_miss_rate": round(counts["retrieval_miss"] / total, 6),
        "ranking_miss_rate": round(counts["ranking_miss"] / total, 6),
        "visible_miss_rate": round(counts["visible_miss"] / total, 6),
        "examples": examples,
    }
