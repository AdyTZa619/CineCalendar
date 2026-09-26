from __future__ import annotations

import argparse
import json
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.rolling_backtest_v37 import run_window_backtest_group
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.v5_event_replay import aggregate_event_reports, event_replay_windows
from cinecalendar.v5_lab import V5LabRecommendationEngine


def main() -> int:
    parser=argparse.ArgumentParser(
        description="Date-aligned V5 replay: evaluate what CineCalendar could have surfaced on actual rating days."
    )
    parser.add_argument("--db",required=True)
    parser.add_argument("--days",type=int,default=12)
    parser.add_argument("--start-date",default="")
    parser.add_argument("--candidate-limit",type=int,default=FastRecommendationEngineV16.NORMAL_POOL)
    parser.add_argument("--final-limit",type=int,default=100)
    parser.add_argument("--als-timeout",type=float,default=180.0)
    parser.add_argument("--output",default="")
    args=parser.parse_args()

    db_path=Path(args.db).expanduser().resolve()
    db=Database(db_path)
    selection=event_replay_windows(
        db,
        desired_days=max(2,int(args.days)),
        minimum_train=80,
        informative_only=True,
        start_date=str(args.start_date or ""),
    )
    baseline=[]
    challenger=[]
    details=[]
    for window in selection.windows:
        reports=run_window_backtest_group(
            db_path,
            window,
            engine_classes=[FastRecommendationEngineV16,V5LabRecommendationEngine],
            candidate_limit=max(100,int(args.candidate_limit)),
            final_limit=max(25,int(args.final_limit)),
            als_timeout=max(5.0,float(args.als_timeout)),
        )
        baseline.append(reports[0])
        challenger.append(reports[1])
        details.append({
            "date":window.cutoff_date,
            "training_count":window.training_count,
            "holdout_count":window.holdout_count,
            "baseline":reports[0],
            "challenger":reports[1],
        })

    report={
        "selection":selection.as_dict(),
        "baseline":aggregate_event_reports(baseline),
        "challenger":aggregate_event_reports(challenger),
        "details":details,
        "production_unchanged":True,
    }
    payload=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        target=Path(args.output).expanduser()
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(payload,encoding="utf-8")
        print(target)
    else:
        print(payload)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
