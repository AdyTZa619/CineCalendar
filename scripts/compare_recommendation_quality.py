from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cinecalendar.recommendation_backtest import compare_quality_engines, report_json


def _fractions(text: str) -> tuple[float, ...]:
    values = []
    for raw in str(text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        value = float(raw)
        if value <= 0 or value >= 0.5:
            raise argparse.ArgumentTypeError("fractions must be between 0 and 0.5")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one fraction is required")
    return tuple(values)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare V16 vs V17 on hidden recent IMDb ratings without modifying the live DB."
    )
    parser.add_argument("db", type=Path, help="Path to CineCalendarData/data/cinecalendar.db")
    parser.add_argument(
        "--fractions",
        type=_fractions,
        default=(0.10, 0.15, 0.20),
        help="Comma-separated newest-history holdouts (default: 0.10,0.15,0.20)",
    )
    parser.add_argument("--candidates", type=int, default=1800)
    parser.add_argument("--final", type=int, default=100)
    parser.add_argument("--als-timeout", type=float, default=180.0)
    args = parser.parse_args()

    report = compare_quality_engines(
        args.db,
        fractions=tuple(args.fractions),
        candidate_limit=args.candidates,
        final_limit=args.final,
        als_timeout=args.als_timeout,
    )
    print(report_json(report))
    return 0 if report.get("aggregate", {}).get("approved") else 2


if __name__ == "__main__":
    raise SystemExit(main())
