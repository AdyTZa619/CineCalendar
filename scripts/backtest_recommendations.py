from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cinecalendar.recommendation_backtest import report_json, run_local_backtest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temporal backtest pentru candidate recall + ranking pe baza locală CineCalendar."
    )
    parser.add_argument("db", type=Path, help="Calea către CineCalendarData/data/cinecalendar.db")
    parser.add_argument("--holdout", type=float, default=0.20, help="Fracția recentă ascunsă (implicit 0.20)")
    parser.add_argument("--candidates", type=int, default=1800, help="Mărimea pool-ului de candidați")
    parser.add_argument("--final", type=int, default=100, help="Câte rezultate finale se evaluează")
    parser.add_argument("--als-timeout", type=float, default=180.0, help="Timeout încărcare ALS în secunde")
    args = parser.parse_args()

    report = run_local_backtest(
        args.db,
        fraction=args.holdout,
        candidate_limit=args.candidates,
        final_limit=args.final,
        als_timeout=args.als_timeout,
    )
    print(report_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
