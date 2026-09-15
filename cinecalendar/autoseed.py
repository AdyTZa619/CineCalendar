from __future__ import annotations

from pathlib import Path

from .imdb_import import import_imdb_csv, validate_imdb_csv
from .profile import build_profile


def _candidate_dirs(app_root: Path) -> list[Path]:
    home = Path.home()
    dirs = [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        app_root,
        app_root.parent,
    ]
    out = []
    seen = set()
    for d in dirs:
        try:
            key = str(d.resolve()).lower()
        except Exception:
            key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _recent_csvs(folder: Path, limit: int = 20) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    try:
        files = [p for p in folder.glob("*.csv") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        return files[:limit]
    except Exception:
        return []


def ensure_initial_ratings(db, app_root: str | Path, log=None) -> dict:
    """Load the newest valid IMDb ratings export automatically when the DB has no ratings.

    No filename is assumed. Downloads, Desktop, Documents and the portable app folder are
    checked. Invalid/non-IMDb CSV files are ignored silently.
    """
    with db.connect() as con:
        current = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
    if current > 0:
        return {"status": "already_loaded", "ratings": current, "file": None}

    root = Path(app_root)
    for folder in _candidate_dirs(root):
        for path in _recent_csvs(folder):
            try:
                _headers, count = validate_imdb_csv(path)
            except Exception:
                continue
            try:
                result = import_imdb_csv(db, path)
                profile = build_profile(db)
                rated = int(profile.get("rated_count", 0) or 0)
                if rated:
                    db.set_setting("ratings_folder", str(path.parent))
                    db.set_setting("auto_watch_enabled", True)
                    if log:
                        log.info("IMDb ratings auto-loaded from %s (%s ratings)", path, rated)
                    return {
                        "status": "imported",
                        "ratings": rated,
                        "rows": count,
                        "file": str(path),
                        "skipped_same_file": bool(result.skipped_same_file),
                    }
            except Exception as exc:
                if log:
                    log.warning("IMDb auto-import candidate failed for %s: %s", path, exc)
                continue
    if log:
        log.warning("No valid IMDb ratings export found automatically.")
    return {"status": "not_found", "ratings": 0, "file": None}
