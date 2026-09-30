import os
import sqlite3

from cinecalendar.updater import update_supported
from cinecalendar.util import AppPaths
from cinecalendar.v5_alpha_runtime import ensure_alpha_database


def _make_db(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE marker(value TEXT)")
        con.execute("INSERT INTO marker(value) VALUES(?)",(value,))
        con.commit()
    finally:
        con.close()


def _read_marker(path):
    con=sqlite3.connect(path)
    try:
        return con.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        con.close()


def test_v5_alpha_clones_stable_database_once_without_touching_source(tmp_path, monkeypatch):
    stable=tmp_path/"Stable"/"CineCalendarData"/"data"/"cinecalendar.db"
    alpha_root=tmp_path/"V5Alpha"/"CineCalendarV5AlphaData"
    _make_db(stable,"stable-v1")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA_SOURCE_DB",str(stable))

    first=ensure_alpha_database(alpha_root)
    target=alpha_root/"data"/"cinecalendar.db"
    assert first["state"] == "cloned"
    assert _read_marker(target) == "stable-v1"

    # Change Stable afterwards: Alpha must keep its own private snapshot.
    con=sqlite3.connect(stable)
    try:
        con.execute("UPDATE marker SET value='stable-v2'")
        con.commit()
    finally:
        con.close()

    second=ensure_alpha_database(alpha_root)
    assert second["state"] == "existing"
    assert _read_marker(target) == "stable-v1"
    assert _read_marker(stable) == "stable-v2"


def test_v5_alpha_refuses_silent_empty_database(tmp_path, monkeypatch):
    monkeypatch.delenv("CINECALENDAR_V5_ALPHA_SOURCE_DB",raising=False)
    monkeypatch.delenv("CINECALENDAR_V5_ALPHA_ALLOW_EMPTY",raising=False)
    alpha_root=tmp_path/"isolated"/"CineCalendarV5AlphaData"
    try:
        ensure_alpha_database(alpha_root)
    except FileNotFoundError as exc:
        assert "V5 Alpha nu a găsit baza" in str(exc)
    else:
        raise AssertionError("Alpha created an empty test DB without explicit permission")


def test_v5_alpha_uses_separate_default_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA","1")
    monkeypatch.delenv("CINECALENDAR_DATA_DIR",raising=False)
    monkeypatch.chdir(tmp_path)
    paths=AppPaths.portable()
    assert paths.root.name == "CineCalendarV5AlphaData"
    assert "CineCalendarData" != paths.root.name


def test_v5_alpha_disables_stable_updater(monkeypatch):
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA","1")
    assert update_supported() is False


def test_stable_launcher_explicitly_clears_alpha_flags():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "launcher.py").read_text(encoding="utf-8")
    assert '"CINECALENDAR_V5_ALPHA"' in source
    assert '"CINECALENDAR_V5_ALPHA_ALLOW_EMPTY"' in source
    assert '"CINECALENDAR_V5_ALPHA_SOURCE_DB"' in source
    assert "os.environ.pop(_key, None)" in source
