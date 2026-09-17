from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

from cinecalendar.personal_acceptance_v38 import run_personal_acceptance, write_report


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Data trebuie să fie YYYY-MM-DD") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit privat CineCalendar 3.8: verifică stiva reală de producție, calendarul, Top 3 "
            "și un holdout temporal pe istoricul local. Ratingurile nu sunt încărcate pe GitHub."
        )
    )
    parser.add_argument("--db", required=True, help=r"Calea către CineCalendarData\data\cinecalendar.db")
    parser.add_argument("--ratings-csv", help="Opțional: exportul IMDb ratings.csv pentru verificarea identității datelor")
    parser.add_argument("--date", dest="anchor", type=_date, default=date.today(), help="Data de referință YYYY-MM-DD")
    parser.add_argument("--output", help="Raport JSON; implicit lângă baza de date")
    parser.add_argument("--candidate-limit", type=int, default=2200)
    parser.add_argument("--final-limit", type=int, default=100)
    parser.add_argument("--als-timeout", type=float, default=180.0)
    parser.add_argument("--no-backtest", action="store_true", help="Sari peste holdout-ul temporal (audit incomplet)")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else db_path.parent.parent / "logs" / f"personal-acceptance-{args.anchor.isoformat()}.json"
    )

    print("CineCalendar 3.8 personal acceptance")
    print("Confidențialitate: ratingurile și baza SQLite rămân locale; raportul nu trimite istoricul pe GitHub.")
    report = run_personal_acceptance(
        db_path,
        anchor=args.anchor,
        ratings_csv=args.ratings_csv,
        candidate_limit=args.candidate_limit,
        final_limit=args.final_limit,
        als_timeout=args.als_timeout,
        run_backtest=not args.no_backtest,
    )
    target = write_report(report, output)

    summary = report.get("summary") or {}
    print(f"Status: {str(report.get('status') or '').upper()}")
    print(f"Motor: {(report.get('quality_manager') or {}).get('preferred_engine')}")
    print(f"Cazuri calendar: {summary.get('calendar_cases', 0)}")
    print(f"Top 3 live verificate: {summary.get('live_cases_with_top3', 0)}")
    print(f"Backtest temporal: {'DA' if summary.get('temporal_backtest_completed') else 'NU'}")
    print(f"Erori: {summary.get('failure_count', 0)} | Avertismente: {summary.get('warning_count', 0)}")
    print(f"Raport: {target}")

    for item in report.get("failures") or []:
        print(f"FAIL [{item.get('code')}]: {item.get('message')}")
    for item in report.get("warnings") or []:
        print(f"WARN [{item.get('code')}]: {item.get('message')}")

    return 1 if report.get("status") == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
