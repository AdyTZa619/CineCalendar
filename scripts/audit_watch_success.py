from __future__ import annotations

import argparse
import json
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.watch_success_audit import build_watch_success_audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit CineCalendar's local recommendation-to-watch funnel.")
    parser.add_argument("database", type=Path, help="Path to CineCalendarData/data/cinecalendar.db")
    parser.add_argument("--days", type=int, default=90, help="Window in days (default: 90)")
    args = parser.parse_args()

    db_path = args.database.expanduser().resolve()
    if not db_path.exists():
        parser.error(f"Database not found: {db_path}")

    report = build_watch_success_audit(Database(db_path), days=args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
