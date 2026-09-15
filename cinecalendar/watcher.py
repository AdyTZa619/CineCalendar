from __future__ import annotations

from pathlib import Path
import stat as statmod

from .db import Database
from .imdb_import import ImportResult, import_imdb_csv, validate_imdb_csv
from .profile import build_profile
from .util import sha256_file


class RatingsFolderWatcher:
    """Best-effort detector for new IMDb ratings exports.

    The watched folder is commonly Downloads, so unrelated/busy/deleted CSV files are expected.
    File-system races and invalid CSVs must be skipped rather than turning a background helper
    into a persistent application error.
    """

    MAX_RECENT_FILES = 12

    def __init__(self, db: Database, folder: str | Path):
        self.db = db
        self.folder = Path(folder).expanduser()

    def _recent_csv_files(self) -> list[tuple[Path, object]]:
        try:
            if not self.folder.is_dir():
                return []
            paths = list(self.folder.glob("*.csv"))
        except OSError:
            return []

        rows: list[tuple[Path, object]] = []
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode) or st.st_size <= 0:
                continue
            rows.append((path, st))

        rows.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
        return rows[: self.MAX_RECENT_FILES]

    def scan(self) -> list[ImportResult]:
        results: list[ImportResult] = []
        for path, initial_stat in self._recent_csv_files():
            # Cheap signature prevents re-reading an export we already imported.
            with self.db.connect() as con:
                known = con.execute(
                    "SELECT 1 FROM import_files WHERE path_name=? AND size_bytes=? AND mtime_ns=?",
                    (path.name, initial_stat.st_size, initial_stat.st_mtime_ns),
                ).fetchone()
            if known:
                continue

            try:
                # Invalid/unrelated CSVs in Downloads are normal and are intentionally ignored.
                validate_imdb_csv(path)
                digest = sha256_file(path)
                final_stat = path.stat()
            except (OSError, UnicodeError, ValueError):
                continue

            # Do not import a file while the browser/another process is still writing it.
            if (
                final_stat.st_size != initial_stat.st_size
                or final_stat.st_mtime_ns != initial_stat.st_mtime_ns
            ):
                continue

            with self.db.connect() as con:
                if con.execute("SELECT 1 FROM import_files WHERE sha256=?", (digest,)).fetchone():
                    continue

            try:
                result = import_imdb_csv(self.db, path)
            except (OSError, UnicodeError, ValueError):
                # A file can still be replaced between validation and import. Retry on the next scan.
                continue

            results.append(result)
            if result.changed:
                build_profile(self.db)
            break

        return results
