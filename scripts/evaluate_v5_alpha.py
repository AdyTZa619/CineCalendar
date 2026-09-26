from __future__ import annotations

import argparse
import json
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.rolling_backtest_v37 import (
    comparison_from_reports,
    rolling_windows,
    run_window_backtest_group,
)
from cinecalendar.v5_lab import V5LabRecommendationEngine


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare V5 Lab with V16 on non-overlapping temporal windows."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=600)
    parser.add_argument("--final-limit", type=int, default=100)
    parser.add_argument("--als-timeout", type=float, default=180.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    db = Database(db_path)
    windows = rolling_windows(db, desired_folds=max(2, int(args.folds)))
    if len(windows) < 2:
        raise SystemExit("Nu sunt suficiente ferestre temporale pentru evaluarea V5.")

    baseline_reports = []
    challenger_reports = []
    for window in windows:
        reports = run_window_backtest_group(
            db_path,
            window,
            engine_classes=[FastRecommendationEngineV16, V5LabRecommendationEngine],
            candidate_limit=max(100, int(args.candidate_limit)),
            final_limit=max(25, int(args.final_limit)),
            als_timeout=max(5.0, float(args.als_timeout)),
        )
        baseline_reports.append(reports[0])
        challenger_reports.append(reports[1])

    report = comparison_from_reports(
        windows,
        baseline_cls=FastRecommendationEngineV16,
        challenger_cls=V5LabRecommendationEngine,
        baseline_reports=baseline_reports,
        challenger_reports=challenger_reports,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(target)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
