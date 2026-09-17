from __future__ import annotations
import sys
import threading
from datetime import date
from pathlib import Path

from . import __version__
from .service import CineCalendarService
from .single_instance import SingleInstanceGuard
from .updater import parse_special_startup, write_health_marker
from .updater_v3 import cleanup_update_residue


PERSONAL_ACCEPTANCE_FLAG = "--personal-acceptance"


def _personal_acceptance_startup(argv: list[str]) -> int | None:
    """Run the private 3.8 audit from the packaged EXE without opening the GUI.

    This path intentionally executes before the single-instance guard and before CineCalendarService,
    so it never starts the normal background workers or records recommendation exposures. The audit
    itself uses record=False and the temporal backtest works on a SQLite backup.
    """
    if PERSONAL_ACCEPTANCE_FLAG not in argv:
        return None

    import argparse

    from .personal_acceptance_v38 import run_personal_acceptance, write_report
    from .util import AppPaths

    parser = argparse.ArgumentParser(prog="CineCalendar --personal-acceptance", add_help=True)
    parser.add_argument(PERSONAL_ACCEPTANCE_FLAG, action="store_true")
    parser.add_argument("--ratings-csv")
    parser.add_argument("--audit-date")
    parser.add_argument("--audit-output")
    parser.add_argument("--audit-candidate-limit", type=int, default=2200)
    parser.add_argument("--audit-final-limit", type=int, default=100)
    parser.add_argument("--audit-als-timeout", type=float, default=180.0)
    parser.add_argument("--audit-no-backtest", action="store_true")
    args = parser.parse_args(argv[1:])

    paths = AppPaths.portable()
    db_path = paths.data / "cinecalendar.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"Baza CineCalendar nu există: {db_path}")

    anchor = date.today()
    if args.audit_date:
        anchor = date.fromisoformat(str(args.audit_date))
    output = (
        Path(args.audit_output).expanduser().resolve()
        if args.audit_output
        else paths.logs / f"personal-acceptance-{anchor.isoformat()}.json"
    )

    report = run_personal_acceptance(
        db_path,
        anchor=anchor,
        ratings_csv=args.ratings_csv,
        candidate_limit=max(400, int(args.audit_candidate_limit)),
        final_limit=max(25, int(args.audit_final_limit)),
        als_timeout=max(0.0, float(args.audit_als_timeout)),
        run_backtest=not bool(args.audit_no_backtest),
    )
    write_report(report, output)
    return 1 if report.get("status") == "fail" else 0


def main():
    audit_exit = _personal_acceptance_startup(sys.argv)
    if audit_exit is not None:
        return audit_exit

    exit_code, post_update = parse_special_startup(sys.argv)
    if exit_code is not None:
        return exit_code

    instance = SingleInstanceGuard()
    if not instance.acquire():
        # The existing window is brought forward on a best-effort basis. More importantly, the
        # second process exits before opening SQLite or starting background workers.
        return 0

    try:
        service = CineCalendarService()
        service.log.info("CineCalendar Premium start")

        # Keep one authoritative package version in inherited/base widgets.
        from . import qt_ui as base_ui
        base_ui.APP_VERSION = __version__

        # Patch the inherited update action before loading Premium UI.
        from . import qt_ui_v2 as decision_ui
        from .update_exit_guard import install_update_exit_guard
        install_update_exit_guard(decision_ui.DecisionWindow)

        from .premium_calendar_ui import CalendarPremiumWindow, run_premium_calendar
        from .ui_composition import compose_premium_window
        compose_premium_window(CalendarPremiumWindow)

        on_ready = None
        if post_update:
            health_path, expected_version = post_update

            def on_ready():
                write_health_marker(health_path, expected_version)
                service.log.info("Post-update health marker written for %s", expected_version)

                def delayed_cleanup():
                    try:
                        cleanup_update_residue(service.paths.root / "updates")
                        service.log.info("Updater residue cleanup completed after %s", expected_version)
                    except Exception as exc:
                        service.log.exception("Updater residue cleanup failed: %s", exc)

                timer = threading.Timer(5.0, delayed_cleanup)
                timer.daemon = True
                timer.start()

        return run_premium_calendar(service, on_ready=on_ready)
    finally:
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
