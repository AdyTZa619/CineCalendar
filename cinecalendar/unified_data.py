from __future__ import annotations

"""One-time, repeatable reconciliation of older Alpha user data into CineCalendarData."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from .backup import export_profile, import_profile
from .db import Database


MERGE_SETTING = "unified_alpha_profile_merge_v1"


class AlphaMergeError(RuntimeError):
    pass


def _alpha_databases(stable_root: Path, extra_source: Path | None = None) -> list[Path]:
    """Look only beside the portable app, where the older Alpha launcher stored data."""
    bundle = stable_root.resolve().parent
    parents = (bundle, bundle.parent)
    found: dict[str, Path] = {}
    for parent in parents:
        roots = [parent]
        try:
            roots.extend(child for child in parent.iterdir() if child.is_dir())
        except OSError:
            pass
        for root in roots:
            candidate = root / "CineCalendarV5AlphaData" / "data" / "cinecalendar.db"
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    resolved = candidate.resolve()
                    found[str(resolved).casefold()] = resolved
            except OSError:
                continue
    for explicit in (os.environ.get("CINECALENDAR_ALPHA_SOURCE_DB"), extra_source):
        if not explicit:
            continue
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise AlphaMergeError(f"Baza Alpha indicată nu există: {candidate}")
        found[str(candidate).casefold()] = candidate
    return sorted(found.values(), key=lambda path: str(path).casefold())


def _snapshot(source: Path, destination: Path) -> None:
    """SQLite online backup includes committed WAL data without writing to the source."""
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.commit()
        check = dst.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise AlphaMergeError(f"Copia SQLite nu trece quick_check: {source}")
    finally:
        dst.close()
        src.close()


def _user_state_fingerprint(source: Path) -> str:
    con = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        digest = hashlib.sha256()
        for table in (
            "ratings", "feedback", "watchlist", "recommendation_history",
            "recommendation_outcomes",
        ):
            digest.update(table.encode("utf-8"))
            try:
                cursor = con.execute(f"SELECT * FROM {table} ORDER BY rowid")
                digest.update(repr([column[0] for column in cursor.description]).encode("utf-8"))
                for row in cursor:
                    digest.update(repr(tuple(row)).encode("utf-8"))
                    digest.update(b"\x00")
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
        return digest.hexdigest()
    finally:
        con.close()


def _profile_without_runtime_settings(source_zip: Path, target_zip: Path) -> None:
    with zipfile.ZipFile(source_zip) as archive:
        payload = json.loads(archive.read("profile.json").decode("utf-8"))
    # Preserve user actions and their evidence, but never import the old Alpha updater,
    # token, trial mode, evaluation report or cached decision into the shared database.
    payload["tables"]["settings"] = []
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("profile.json", json.dumps(payload, ensure_ascii=False))


def _restore_backup(backup: Path, target: Path) -> None:
    source = sqlite3.connect(backup)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _user_action_counts(db: Database) -> tuple[dict[str, tuple[int, str]], int, int]:
    with db.connect() as con:
        ratings = {
            str(row[0]): (int(row[1]), str(row[2] or ""))
            for row in con.execute(
                """SELECT COALESCE(NULLIF(m.imdb_id,''),m.identity_key),r.rating,r.date_rated
                   FROM ratings r JOIN movies m ON m.id=r.movie_id"""
            )
        }
        feedback = int(con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0])
        history = int(con.execute("SELECT COUNT(*) FROM recommendation_history").fetchone()[0])
    return ratings, feedback, history


def merge_existing_alpha_data(
    db: Database, stable_root: Path, extra_source: str | Path | None = None,
) -> dict:
    """Merge changed Alpha profiles into Stable, leaving both original files intact.

    Each source has a durable pre-merge Stable snapshot. Import uses the existing atomic
    profile merge (newer rating/watchlist wins, identical feedback and history dedupe).
    A changed Alpha database is reconciled again on the next launch.
    """
    stable_root = Path(stable_root).resolve()
    sources = [
        path for path in _alpha_databases(stable_root, Path(extra_source) if extra_source else None)
        if path != db.path.resolve()
    ]
    state = db.get_setting(MERGE_SETTING, {}) or {}
    completed = dict(state.get("sources") or {}) if isinstance(state, dict) else {}
    result = {"state": "none", "sources": [], "ratings": 0, "feedback": 0,
              "history": 0, "backups": [], "known_sources": list(completed)}
    if not sources:
        return result

    backups = stable_root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    for source in sources:
        try:
            fingerprint = _user_state_fingerprint(source)
            key = str(source)
            if completed.get(key) == fingerprint:
                continue
            with tempfile.TemporaryDirectory(prefix="alpha-merge-", dir=backups) as work:
                workdir = Path(work)
                alpha_snapshot = workdir / "alpha.db"
                _snapshot(source, alpha_snapshot)
                alpha_db = Database(alpha_snapshot)  # schema upgrades affect the copy only
                profile = export_profile(alpha_db, workdir / "alpha-profile.zip")
                sanitized = workdir / "user-actions.zip"
                _profile_without_runtime_settings(profile, sanitized)

                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
                backup = backups / f"before-alpha-merge-{stamp}-{suffix}.db"
                _snapshot(db.path, backup)
                try:
                    before_ratings, before_feedback, before_history = _user_action_counts(db)
                    import_profile(db, sanitized, mode="merge")
                    with db.connect() as con:
                        check = con.execute("PRAGMA quick_check").fetchone()
                    if not check or str(check[0]).lower() != "ok":
                        raise AlphaMergeError("Baza comună nu trece quick_check după import.")
                    after_ratings, after_feedback, after_history = _user_action_counts(db)
                    completed[key] = fingerprint
                    db.set_setting(MERGE_SETTING, {"sources": completed, "last_backup": str(backup)})
                except Exception:
                    _restore_backup(backup, db.path)
                    raise
                result["sources"].append(key)
                result["known_sources"] = list(completed)
                result["backups"].append(str(backup))
                result["ratings"] += sum(
                    previous != value for key, value in after_ratings.items()
                    for previous in (before_ratings.get(key),)
                )
                result["feedback"] += max(0, after_feedback - before_feedback)
                result["history"] += max(0, after_history - before_history)
        except Exception as exc:
            raise AlphaMergeError(
                f"Unirea datelor Alpha a fost oprită pentru {source}. "
                f"Baza Alpha nu a fost modificată. Dacă importul a început, "
                f"copia bazei principale este în {backups}. "
                f"Cauza: {exc}"
            ) from exc
    result["state"] = "merged" if result["sources"] else "already_merged"
    return result
