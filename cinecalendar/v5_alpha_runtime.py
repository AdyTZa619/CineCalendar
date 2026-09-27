from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys


V5_ALPHA_ENV = "CINECALENDAR_V5_ALPHA"
V5_ALPHA_ALLOW_EMPTY_ENV = "CINECALENDAR_V5_ALPHA_ALLOW_EMPTY"
V5_ALPHA_SOURCE_DB_ENV = "CINECALENDAR_V5_ALPHA_SOURCE_DB"
V5_ALPHA_DATA_DIR = "CineCalendarV5AlphaData"
V5_ALPHA_VERSION = "5.0 Alpha 16"
V5_ALPHA_MUTEX = r"Local\CineCalendar-V5-Alpha-SingleInstance"


def is_v5_alpha() -> bool:
    return str(os.environ.get(V5_ALPHA_ENV, "") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def alpha_data_root() -> Path:
    override = os.environ.get("CINECALENDAR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path.cwd().resolve()
    )
    return base / V5_ALPHA_DATA_DIR


def _candidate_stable_databases(alpha_root: Path) -> list[Path]:
    candidates: list[Path] = []
    explicit = str(os.environ.get(V5_ALPHA_SOURCE_DB_ENV, "") or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser().resolve())

    alpha_root = Path(alpha_root).resolve()
    bundle_root = alpha_root.parent if alpha_root.name == V5_ALPHA_DATA_DIR else alpha_root

    # Alpha extracted inside the Stable bundle.
    candidates.append(bundle_root / "CineCalendarData" / "data" / "cinecalendar.db")

    # Alpha folder nested inside the Stable bundle.
    candidates.append(bundle_root.parent / "CineCalendarData" / "data" / "cinecalendar.db")

    # Alpha bundle placed next to the Stable bundle.
    parent = bundle_root.parent
    try:
        for child in parent.iterdir():
            if not child.is_dir() or child.resolve() == bundle_root.resolve():
                continue
            candidates.append(child / "CineCalendarData" / "data" / "cinecalendar.db")
    except OSError:
        pass

    # Alpha data directory manually placed next to an existing Stable data directory.
    candidates.append(alpha_root.parent / "CineCalendarData" / "data" / "cinecalendar.db")

    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).lower()
        except OSError:
            key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def find_stable_database(alpha_root: Path) -> Path | None:
    target = Path(alpha_root).resolve() / "data" / "cinecalendar.db"
    for candidate in _candidate_stable_databases(alpha_root):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == target:
            continue
        if resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    return None


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".clone")
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(source, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.backup(dst)
        dst.commit()
        check = dst.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise RuntimeError(
                "Copia SQLite pentru V5 Alpha nu trece quick_check."
            )
    finally:
        dst.close()
        src.close()

    os.replace(tmp, destination)


def ensure_alpha_database(alpha_root: Path) -> dict:
    """Create the private V5 Alpha DB exactly once from the Stable DB.

    Stable is never opened for writes. Subsequent Alpha launches reuse only the cloned database.
    """
    alpha_root = Path(alpha_root).resolve()
    target = alpha_root / "data" / "cinecalendar.db"
    if target.is_file() and target.stat().st_size > 0:
        return {
            "state": "existing",
            "target": str(target),
            "source": "",
        }

    source = find_stable_database(alpha_root)
    if source is None:
        if str(os.environ.get(V5_ALPHA_ALLOW_EMPTY_ENV, "") or "").strip() == "1":
            target.parent.mkdir(parents=True, exist_ok=True)
            return {
                "state": "empty_allowed",
                "target": str(target),
                "source": "",
            }
        raise FileNotFoundError(
            "V5 Alpha nu a găsit baza CineCalendar Stable. "
            "Dezarhivează folderul V5 Alpha în folderul CineCalendar Stable "
            "sau lângă folderul lui, apoi pornește din nou. "
            "Baza Stable nu va fi modificată."
        )

    _sqlite_snapshot(source, target)
    return {
        "state": "cloned",
        "target": str(target),
        "source": str(source),
    }


def show_alpha_startup_error(message: str) -> None:
    text = str(message or "V5 Alpha nu a putut porni.")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                text,
                "CineCalendar V5 Alpha",
                0x00000010,
            )
            return
        except Exception:
            pass
    try:
        sys.stderr.write(text + "\n")
    except Exception:
        pass
