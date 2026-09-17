from __future__ import annotations

import inspect

from cinecalendar import app


def test_normal_startup_does_not_enter_acceptance_mode():
    assert app._personal_acceptance_startup(["CineCalendar.exe"]) is None


def test_acceptance_mode_runs_before_normal_service_and_updater_startup():
    source = inspect.getsource(app.main)
    assert "_personal_acceptance_startup(sys.argv)" in source
    assert source.index("_personal_acceptance_startup(sys.argv)") < source.index("parse_special_startup(sys.argv)")
    assert source.index("_personal_acceptance_startup(sys.argv)") < source.index("SingleInstanceGuard()")


def test_packaged_entrypoint_has_explicit_private_audit_flag():
    assert app.PERSONAL_ACCEPTANCE_FLAG == "--personal-acceptance"
    source = inspect.getsource(app._personal_acceptance_startup)
    assert "run_personal_acceptance" in source
    assert "write_report" in source
    assert "personal-acceptance-" in source
