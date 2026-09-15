from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess

from .updater import (
    NativeUpdateRequest,
    UpdateInfo,
    _download_to,
    _log,
    _powershell_helper as _legacy_powershell_helper,
    _safe_extract_zip,
    check_for_update,
    health_matches,
    is_newer_version,
    parse_manifest,
    parse_special_startup,
    sha256_path,
    update_supported,
    write_health_marker,
)


def cleanup_update_residue(updates_dir: str | Path) -> None:
    """Remove stale updater payloads while never touching CineCalendarData itself."""
    updates = Path(updates_dir)
    if not updates.exists():
        return
    patterns = (
        "staged-*",
        "*.pending.zip",
        "*.pending.zip.download",
        "*.bootstrap.zip",
        "bootstrap-*-stage",
        "bootstrap-backup-*",
        "health.ok",
        "health.tmp",
        "apply_update.json",
        "apply_update.ps1",
    )
    for pattern in patterns:
        for p in updates.glob(pattern):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    backup = updates / "backup"
    try:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        pass
    try:
        if updates.exists() and not any(updates.iterdir()):
            updates.rmdir()
    except Exception:
        pass


def _detached_popen(args: list[str]) -> subprocess.Popen:
    """Launch the helper outside the GUI process lifetime on Windows."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = flags
    return subprocess.Popen(args, **kwargs)


def _powershell_helper() -> str:
    script = _legacy_powershell_helper()

    old_wait = """Write-UpdateLog \"Updater folder pornit pentru $($req.expected_version).\"\nif (-not (Wait-ParentExit ([int]$req.parent_pid) 45)) {\n  Write-UpdateLog 'Procesul principal nu s-a închis; update anulat fără modificări.'\n  exit 7\n}\n"""
    new_wait = """Write-UpdateLog \"Updater folder pornit pentru $($req.expected_version).\"\nif (-not (Wait-ParentExit ([int]$req.parent_pid) 6)) {\n  Write-UpdateLog 'Procesul principal încă rulează; îl închid forțat pentru handoff.'\n  try { Stop-Process -Id ([int]$req.parent_pid) -Force -ErrorAction Stop } catch {\n    Write-UpdateLog \"Nu am putut opri procesul părinte: $($_.Exception.Message)\"\n  }\n  if (-not (Wait-ParentExit ([int]$req.parent_pid) 12)) {\n    Write-UpdateLog 'Procesul principal nu s-a închis nici după oprirea forțată; update anulat.'\n    exit 7\n  }\n}\nWrite-UpdateLog 'Procesul părinte este închis; încep înlocuirea bundle-ului.'\n"""
    if old_wait not in script:
        raise RuntimeError("Șablonul helperului legacy s-a schimbat; refuz să generez un updater nesigur.")
    script = script.replace(old_wait, new_wait)

    old_success = """if ($ok) {\n  Write-UpdateLog 'Health-check reușit; update folder confirmat.'\n  try { Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue } catch {}\n  try { Remove-Item -LiteralPath $req.pending_zip -Force -ErrorAction SilentlyContinue } catch {}\n  try { Remove-Item -LiteralPath $health -Force -ErrorAction SilentlyContinue } catch {}\n  exit 0\n}\n"""
    new_success = """if ($ok) {\n  Write-UpdateLog 'Health-check reușit; update folder confirmat.'\n  try { Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue } catch {}\n  try { Remove-Item -LiteralPath $req.pending_zip -Force -ErrorAction SilentlyContinue } catch {}\n  try { Remove-Item -LiteralPath $health -Force -ErrorAction SilentlyContinue } catch {}\n  try { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue } catch {}\n  try {\n    Get-ChildItem -LiteralPath $req.updates_dir -Force -ErrorAction SilentlyContinue | ForEach-Object {\n      if ($_.Name -match '^staged-' -or $_.Name -match '\\.pending\\.zip(\\.download)?$' -or $_.Name -match '^bootstrap-' -or $_.Name -eq 'health.ok') {\n        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue\n      }\n    }\n  } catch {}\n  try { Remove-Item -LiteralPath $req.request_path -Force -ErrorAction SilentlyContinue } catch {}\n  try { Remove-Item -LiteralPath $req.helper_script -Force -ErrorAction SilentlyContinue } catch {}\n  try {\n    $backupRoot = Split-Path -Parent $backup\n    if ((Test-Path -LiteralPath $backupRoot) -and -not (Get-ChildItem -LiteralPath $backupRoot -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $backupRoot -Force -ErrorAction SilentlyContinue }\n  } catch {}\n  try {\n    if ((Test-Path -LiteralPath $req.updates_dir) -and -not (Get-ChildItem -LiteralPath $req.updates_dir -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $req.updates_dir -Force -ErrorAction SilentlyContinue }\n  } catch {}\n  exit 0\n}\n"""
    if old_success not in script:
        raise RuntimeError("Blocul de cleanup legacy s-a schimbat; refuz să generez un updater incomplet.")
    return script.replace(old_success, new_success)


def stage_and_start_update(
    info: UpdateInfo,
    data_root: str | Path,
    progress=None,
) -> NativeUpdateRequest:
    if not update_supported():
        raise RuntimeError("Updaterul automat funcționează numai în CineCalendar.exe pentru Windows.")
    progress = progress or (lambda _m: None)

    import sys

    current = Path(sys.executable).resolve()
    app_root = current.parent
    data_root = Path(data_root).resolve()
    updates = data_root / "updates"

    # Every new attempt starts from a clean updater workspace. User data lives outside
    # these transient paths and is never removed.
    cleanup_update_residue(updates)
    updates.mkdir(parents=True, exist_ok=True)

    pending_zip = updates / f"CineCalendar-{info.version}.pending.zip"
    staged = updates / f"staged-{info.version}"
    backup = updates / "backup" / "previous"
    health = updates / "health.ok"
    # Keep diagnostics in the normal logs directory, not among updater payloads.
    log = data_root / "logs" / "updater.log"
    helper = updates / "apply_update.ps1"
    request_path = updates / "apply_update.json"

    progress("Descarc pachetul Premium…")
    _download_to(info.url, pending_zip, progress)
    got = sha256_path(pending_zip)
    if got.lower() != info.sha256.lower():
        pending_zip.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 diferit. Așteptat {info.sha256}, primit {got}.")

    progress("SHA-256 verificat. Pregătesc fișierele…")
    _safe_extract_zip(pending_zip, staged)
    helper.write_text(_powershell_helper(), encoding="utf-8-sig")

    req = NativeUpdateRequest(
        parent_pid=os.getpid(),
        app_root=str(app_root),
        data_root=str(data_root),
        staged_dir=str(staged),
        backup_dir=str(backup),
        pending_zip=str(pending_zip),
        health=str(health),
        log=str(log),
        expected_version=info.version,
        expected_sha256=info.sha256,
        updates_dir=str(updates),
        helper_script=str(helper),
        request_path=str(request_path),
    )
    request_path.write_text(json.dumps(asdict(req), ensure_ascii=False, indent=2), encoding="utf-8")
    _log(log, f"Update folder {info.version} staged; pornesc helperul detașat.")

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("Windows PowerShell nu a fost găsit; update-ul nu a fost aplicat.")

    _detached_popen([
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        str(request_path),
    ])
    progress("Update pregătit. Helperul independent aplică update-ul și repornește CineCalendar.")
    return req
