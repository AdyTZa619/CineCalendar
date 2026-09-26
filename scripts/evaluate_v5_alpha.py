from __future__ import annotations

import argparse
import json
from pathlib import Path

from cinecalendar.recommendation_backtest import compare_quality_engines
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.v5_lab import V5LabRecommendationEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare V5 Lab with the proven V16 baseline.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--candidate-limit", type=int, default=600)
    parser.add_argument("--final-limit", type=int, default=100)
    parser.add_argument("--als-timeout", type=float, default=180.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    report = compare_quality_engines(
        Path(args.db),
        fractions=(0.20,),
        candidate_limit=max(100, int(args.candidate_limit)),
        final_limit=max(25, int(args.final_limit)),
        als_timeout=max(5.0, float(args.als_timeout)),
        baseline_cls=FastRecommendationEngineV16,
        challenger_cls=V5LabRecommendationEngine,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(target)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
