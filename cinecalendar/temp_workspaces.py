from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Iterator


OWNER_FILE = ".cinecalendar-owner.json"
MANAGED_PREFIXES = (
    "cinecalendar-backtest-",
    "cinecalendar-rolling37-",
    "cinecalendar-v431-bench-",
)
LEGACY_GRACE_SECONDS = 2 * 60 * 60
MAX_OWNED_AGE_SECONDS = 24 * 60 * 60


def _pid_is_running(pid: int) -> bool:
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless existence probe on Windows: Python maps ordinary
        # signals to TerminateProcess. Query the process handle instead.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                wintypes.DWORD(pid),
            )
            if not handle:
                # Access denied means the process exists but cannot be queried by this account.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Failure to prove that another process is dead must never cause its workspace to be
            # deleted. A later startup can retry the cleanup.
            return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_payload(path: Path) -> dict:
    marker = path / OWNER_FILE
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _write_owner(path: Path) -> None:
    payload = {
        "pid": int(os.getpid()),
        "created_at": float(time.time()),
    }
    (path / OWNER_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


@contextmanager
def managed_temp_workspace(
    prefix: str,
    *,
    temp_root: str | Path | None = None,
) -> Iterator[Path]:
    """Create a CineCalendar temp workspace that can be reclaimed after an interrupted process."""
    if prefix not in MANAGED_PREFIXES:
        raise ValueError(f"Unsupported CineCalendar temp prefix: {prefix}")
    root = Path(temp_root).expanduser().resolve() if temp_root is not None else None
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=prefix,
        dir=str(root) if root is not None else None,
        ignore_cleanup_errors=True,
    ) as raw:
        path = Path(raw)
        _write_owner(path)
        yield path


def cleanup_abandoned_workspaces(
    *,
    temp_root: str | Path | None = None,
    legacy_grace_seconds: float = LEGACY_GRACE_SECONDS,
    max_owned_age_seconds: float = MAX_OWNED_AGE_SECONDS,
) -> dict:
    """Remove only abandoned CineCalendar test workspaces.

    New workspaces carry an owner PID. A live owner is preserved. Directories made by older
    releases do not have a marker, so they are removed only after a conservative age grace.
    """
    root = (
        Path(temp_root).expanduser().resolve()
        if temp_root is not None
        else Path(tempfile.gettempdir()).resolve()
    )
    result = {
        "removed": 0,
        "active": 0,
        "recent_legacy": 0,
        "failed": 0,
        "paths": [],
    }
    if not root.is_dir():
        return result

    now = time.time()
    for prefix in MANAGED_PREFIXES:
        for path in root.glob(prefix + "*"):
            try:
                if path.is_symlink() or not path.is_dir() or path.parent.resolve() != root:
                    continue
                stat = path.stat()
            except OSError:
                result["failed"] += 1
                continue

            owner = _owner_payload(path)
            should_remove = False
            if owner:
                try:
                    pid = int(owner.get("pid", 0) or 0)
                except (TypeError, ValueError):
                    pid = 0
                try:
                    created_at = float(owner.get("created_at", stat.st_mtime) or stat.st_mtime)
                except (TypeError, ValueError):
                    created_at = float(stat.st_mtime)
                owned_age = max(0.0, now - created_at)
                if _pid_is_running(pid) and owned_age < max(1.0, float(max_owned_age_seconds)):
                    result["active"] += 1
                    continue
                should_remove = True
            else:
                legacy_age = max(0.0, now - float(stat.st_mtime))
                if legacy_age < max(0.0, float(legacy_grace_seconds)):
                    result["recent_legacy"] += 1
                    continue
                should_remove = True

            if should_remove:
                try:
                    shutil.rmtree(path)
                    result["removed"] += 1
                    result["paths"].append(str(path))
                except OSError:
                    result["failed"] += 1

    return result
