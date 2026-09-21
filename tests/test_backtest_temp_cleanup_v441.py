from __future__ import annotations

import json
import os
from pathlib import Path
import time

from cinecalendar.db import Database
from cinecalendar.quality_manager_v34 import (
    QUALITY_MANAGER_VERSION,
    QUALITY_SETTING,
    RecommendationQualityManager,
)
from cinecalendar.quality_manager_v37 import (
    QUALITY_MANAGER_VERSION as QUALITY_MANAGER_V37_VERSION,
    QUALITY_SETTING as QUALITY_V37_SETTING,
    RecommendationQualityManagerV37,
)
from cinecalendar import temp_workspaces
from cinecalendar.temp_workspaces import (
    OWNER_FILE,
    cleanup_abandoned_workspaces,
    managed_temp_workspace,
)
from cinecalendar.util import utcnow_iso


def _owner(path: Path, pid: int, created_at: float) -> None:
    (path / OWNER_FILE).write_text(
        json.dumps({"pid": pid, "created_at": created_at}),
        encoding="utf-8",
    )


def test_managed_workspace_cleans_after_normal_exit(tmp_path):
    with managed_temp_workspace("cinecalendar-backtest-", temp_root=tmp_path) as workspace:
        assert workspace.is_dir()
        assert (workspace / OWNER_FILE).is_file()
        (workspace / "cinecalendar.db").write_bytes(b"test")
        created = workspace

    assert not created.exists()


def test_startup_cleanup_removes_abandoned_but_preserves_active_and_recent_legacy(
    tmp_path,
    monkeypatch,
):
    now = time.time()

    dead = tmp_path / "cinecalendar-backtest-dead"
    dead.mkdir()
    _owner(dead, 222, now)

    live = tmp_path / "cinecalendar-rolling37-live"
    live.mkdir()
    _owner(live, 111, now)

    legacy_old = tmp_path / "cinecalendar-backtest-legacy-old"
    legacy_old.mkdir()
    os.utime(legacy_old, (now - 10_000, now - 10_000))

    legacy_recent = tmp_path / "cinecalendar-backtest-legacy-recent"
    legacy_recent.mkdir()

    unrelated = tmp_path / "other-program-temp"
    unrelated.mkdir()

    monkeypatch.setattr(temp_workspaces, "_pid_is_running", lambda pid: int(pid) == 111)

    result = cleanup_abandoned_workspaces(
        temp_root=tmp_path,
        legacy_grace_seconds=3600,
        max_owned_age_seconds=24 * 60 * 60,
    )

    assert not dead.exists()
    assert not legacy_old.exists()
    assert live.exists()
    assert legacy_recent.exists()
    assert unrelated.exists()
    assert result["removed"] == 2
    assert result["active"] == 1
    assert result["recent_legacy"] == 1
    assert result["failed"] == 0


def test_interrupted_quality_backtest_enters_cooldown_instead_of_restarting(
    tmp_path,
    monkeypatch,
):
    db = Database(tmp_path / "cooldown.db")
    monkeypatch.setattr(RecommendationQualityManager, "MIN_RATINGS", 0)

    manager = RecommendationQualityManager(db)
    token = manager.state_token()
    db.set_setting(
        QUALITY_SETTING,
        {
            "manager_version": QUALITY_MANAGER_VERSION,
            "state_token": token,
            "status": "running",
            "rating_count": 0,
            "worker_pid": 999999,
            "started_at": utcnow_iso(),
            "preferred_engine": "FastRecommendationEngineV16",
        },
    )

    assert manager.start_background(delay_seconds=0.0) is False
    report = manager.cached_report()
    assert report["status"] == "interrupted_cooldown"
    assert int(report["retry_after_seconds"]) > 0
    assert int(report["worker_pid"]) == 0


def test_interrupted_rolling_quality_backtest_enters_cooldown_instead_of_restarting(
    tmp_path,
    monkeypatch,
):
    db = Database(tmp_path / "rolling-cooldown.db")
    monkeypatch.setattr(RecommendationQualityManagerV37, "MIN_RATINGS", 0)

    manager = RecommendationQualityManagerV37(db)
    token = manager.state_token()
    db.set_setting(
        QUALITY_V37_SETTING,
        {
            "manager_version": QUALITY_MANAGER_V37_VERSION,
            "state_token": token,
            "status": "running",
            "rating_count": 0,
            "worker_pid": 999999,
            "started_at": utcnow_iso(),
            "preferred_engine": "FastRecommendationEngineV16",
            "fallback_snapshot": {},
        },
    )

    assert manager.start_background(delay_seconds=0.0) is False
    report = manager.cached_report()
    assert report["status"] == "interrupted_cooldown"
    assert int(report["retry_after_seconds"]) > 0
    assert int(report["worker_pid"]) == 0
