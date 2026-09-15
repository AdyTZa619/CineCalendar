from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
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


def _powershell_helper() -> str:
    """Return a PowerShell helper that never shadows PowerShell's read-only $PID variable."""
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
    return script


def _brokered_popen(args: list[str]) -> None:
    """Create the updater outside the CineCalendar process tree on Windows.

    Direct subprocess children can die with a frozen GUI process or a containing Windows
    job object. Win32_Process.Create is executed by the Windows CIM/WMI service, making
    the real updater independent from the CineCalendar launcher lifetime.
    """
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
    updates.mkdir(parents=True, exist_ok=True)

    pending_zip = updates / f"CineCalendar-{info.version}.pending.zip"
    staged = updates / f"staged-{info.version}"
    backup = updates / "backup" / "previous"
    health = updates / "health.ok"
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
    _log(log, f"Update folder {info.version} staged; predau helperul brokerului Windows.")

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
    progress("Update pregătit. Helperul independent aplică update-ul și repornește CineCalendar.")
    return req
