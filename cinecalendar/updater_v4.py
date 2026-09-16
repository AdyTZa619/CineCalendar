from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess

from .updater import NativeUpdateRequest, _download_to, _log, _safe_extract_zip, sha256_path
from .updater_v3 import (
    UpdateInfo,
    _powershell_helper as _legacy_powershell_helper,
    check_for_update,
    cleanup_update_residue,
    health_matches,
    is_newer_version,
    parse_manifest,
    parse_special_startup,
    update_supported,
    write_health_marker,
)


def _sqlite_backup(source: Path, destination: Path) -> bool:
    """Create a transactionally consistent DB snapshot while CineCalendar is still running."""
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(source, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.backup(dst)
        dst.commit()
        check = dst.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise RuntimeError("Snapshotul SQLite pentru rollback nu trece quick_check.")
    finally:
        dst.close()
        src.close()
    os.replace(tmp, destination)
    return True


def _powershell_helper() -> str:
    """PowerShell helper with bundle + SQLite rollback and no reserved $PID shadowing."""
    script = _legacy_powershell_helper()
    script = script.replace(
        "function Wait-ParentExit([int]$Pid, [int]$Seconds) {",
        "function Wait-ParentExit([int]$ProcessId, [int]$Seconds) {",
    )
    script = script.replace("Get-Process -Id $Pid", "Get-Process -Id $ProcessId")
    if "function Wait-ParentExit([int]$ProcessId, [int]$Seconds) {" not in script:
        raise RuntimeError("Helperul PowerShell nu a putut fi reparat: parametrul ProcessId lipsește.")
    if "Get-Process -Id $Pid" in script or "[int]$Pid" in script:
        raise RuntimeError("Helperul PowerShell încă încearcă să suprascrie variabila rezervată $PID.")

    marker = 'Write-UpdateLog "Updater folder pornit pentru $($req.expected_version)."'
    restore_fn = r'''
function Restore-DataBackup() {
  $hasFlag = $req.PSObject.Properties.Name -contains 'db_backup_present'
  $hasPath = $req.PSObject.Properties.Name -contains 'db_backup_path'
  $hasDb = $req.PSObject.Properties.Name -contains 'db_path'
  if (-not $hasFlag -or -not $hasPath -or -not $hasDb) { return }
  if (-not [bool]$req.db_backup_present) { return }
  if (-not (Test-Path -LiteralPath $req.db_backup_path)) { throw 'Backupul SQLite pentru rollback lipsește.' }
  try {
    Remove-Item -LiteralPath ($req.db_path + '-wal') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ($req.db_path + '-shm') -Force -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath $req.db_backup_path -Destination $req.db_path -Force -ErrorAction Stop
    Write-UpdateLog 'Baza SQLite a fost restaurată împreună cu bundle-ul anterior.'
  } catch {
    Write-UpdateLog "ROLLBACK DB EȘUAT: $($_.Exception.Message)"
    throw
  }
}

'''
    if marker not in script:
        raise RuntimeError("Șablonul helperului nu conține punctul de inserare pentru rollback DB.")
    script = script.replace(marker, restore_fn + marker, 1)

    # Every bundle rollback restores the SQLite snapshot before the old executable restarts.
    # The inherited helper has rollback blocks at different indentation levels, so match the
    # restore-copy statement itself instead of relying on fragile surrounding whitespace.
    rollback_pattern = re.compile(r"(?m)^(?P<indent>[ \t]*)Copy-Tree \$backup \$appRoot[ \t]*$")

    def _attach_db_restore(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return f"{indent}Copy-Tree $backup $appRoot\n{indent}Restore-DataBackup"

    script, replaced = rollback_pattern.subn(_attach_db_restore, script)
    if replaced < 3:
        raise RuntimeError(
            f"Helperul updaterului are doar {replaced} căi de rollback ale bundle-ului; aștept cel puțin 3."
        )

    # Success means the migrated DB is accepted; remove the rollback snapshot only then.
    success_marker = "Write-UpdateLog 'Health-check reușit; update folder confirmat.'"
    cleanup = r'''
  try {
    if ($req.PSObject.Properties.Name -contains 'db_backup_path') {
      Remove-Item -LiteralPath $req.db_backup_path -Force -ErrorAction SilentlyContinue
      $dbBackupDir = Split-Path -Parent $req.db_backup_path
      if ((Test-Path -LiteralPath $dbBackupDir) -and -not (Get-ChildItem -LiteralPath $dbBackupDir -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $dbBackupDir -Force -ErrorAction SilentlyContinue
      }
    }
  } catch {}
'''
    if success_marker not in script:
        raise RuntimeError("Blocul de succes al updaterului nu a fost găsit.")
    script = script.replace(success_marker, success_marker + "\n" + cleanup, 1)
    return script


def _brokered_popen(args: list[str]) -> None:
    """Create the updater outside the CineCalendar process tree on Windows."""
    if os.name != "nt":
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return

    broker = shutil.which("powershell.exe") or shutil.which("powershell")
    if not broker:
        raise RuntimeError("Windows PowerShell nu a fost găsit pentru brokerul updaterului.")

    env = os.environ.copy()
    env["CINECALENDAR_BROKER_COMMAND"] = subprocess.list2cmdline(args)
    script = (
        "$cmd=$env:CINECALENDAR_BROKER_COMMAND; "
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd}; "
        "if ($null -eq $r) { exit 91 }; "
        "if ([int]$r.ReturnValue -ne 0) { exit [int]$r.ReturnValue }"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    completed = subprocess.run(
        [broker, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=flags,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Brokerul Windows al updaterului a eșuat (cod {completed.returncode}).")


def stage_and_start_update(info: UpdateInfo, data_root: str | Path, progress=None) -> NativeUpdateRequest:
    if not update_supported():
        raise RuntimeError("Updaterul automat funcționează numai în CineCalendar.exe pe Windows.")
    progress = progress or (lambda _m: None)

    import sys

    current = Path(sys.executable).resolve()
    app_root = current.parent
    data_root = Path(data_root).resolve()
    updates = data_root / "updates"

    cleanup_update_residue(updates)
    # v3 cleanup predates the DB snapshot directory.
    shutil.rmtree(updates / "db-backup", ignore_errors=True)
    updates.mkdir(parents=True, exist_ok=True)

    pending_zip = updates / f"CineCalendar-{info.version}.pending.zip"
    staged = updates / f"staged-{info.version}"
    backup = updates / "backup" / "previous"
    health = updates / "health.ok"
    log = data_root / "logs" / "updater.log"
    helper = updates / "apply_update.ps1"
    request_path = updates / "apply_update.json"
    db_path = data_root / "data" / "cinecalendar.db"
    db_backup = updates / "db-backup" / "cinecalendar.db"

    progress("Descarc pachetul Premium…")
    _download_to(info.url, pending_zip, progress)
    got = sha256_path(pending_zip)
    if got.lower() != info.sha256.lower():
        pending_zip.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 diferit. Așteptat {info.sha256}, primit {got}.")

    progress("SHA-256 verificat. Pregătesc fișierele și snapshotul bazei…")
    _safe_extract_zip(pending_zip, staged)
    db_backup_present = _sqlite_backup(db_path, db_backup)
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
    payload = asdict(req)
    payload.update(
        {
            "db_path": str(db_path),
            "db_backup_path": str(db_backup),
            "db_backup_present": bool(db_backup_present),
        }
    )
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(
        log,
        f"Update folder {info.version} staged; DB rollback snapshot={'da' if db_backup_present else 'nu'}; predau helperul brokerului Windows.",
    )

    updater_ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not updater_ps:
        raise RuntimeError("Windows PowerShell nu a fost găsit; update-ul nu a fost aplicat.")
    _brokered_popen([
        updater_ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        str(request_path),
    ])
    progress("Update pregătit. Rollback-ul protejează acum atât bundle-ul, cât și baza SQLite.")
    return req
