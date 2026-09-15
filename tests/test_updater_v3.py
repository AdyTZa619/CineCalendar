import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cinecalendar.updater_v3 import cleanup_update_residue
from cinecalendar.updater_v4 import _powershell_helper


def test_cleanup_update_residue_removes_only_transient_payloads(tmp_path):
    updates = tmp_path / "updates"
    updates.mkdir()
    (updates / "staged-2.3.2").mkdir()
    (updates / "staged-2.3.2" / "x.bin").write_bytes(b"x")
    (updates / "CineCalendar-2.3.2.pending.zip").write_bytes(b"zip")
    (updates / "CineCalendar-2.3.2.pending.zip.download").write_bytes(b"part")
    (updates / "apply_update.ps1").write_text("x", encoding="utf-8")
    (updates / "apply_update.json").write_text("{}", encoding="utf-8")
    (updates / "health.ok").write_text("2.3.2", encoding="utf-8")
    (updates / "backup" / "previous").mkdir(parents=True)
    (updates / "backup" / "previous" / "old.exe").write_bytes(b"old")
    keep = updates / "keep-me.txt"
    keep.write_text("user-like sentinel", encoding="utf-8")

    cleanup_update_residue(updates)

    assert keep.is_file()
    assert not (updates / "staged-2.3.2").exists()
    assert not (updates / "CineCalendar-2.3.2.pending.zip").exists()
    assert not (updates / "apply_update.ps1").exists()
    assert not (updates / "apply_update.json").exists()
    assert not (updates / "health.ok").exists()
    assert not (updates / "backup").exists()


def test_clean_helper_forces_stuck_parent_and_cleans_success_residue():
    script = _powershell_helper()
    assert "Stop-Process -Id ([int]$req.parent_pid) -Force" in script
    assert "Procesul părinte este închis; încep înlocuirea bundle-ului." in script
    assert "Remove-Item -LiteralPath $backup -Recurse -Force" in script
    assert "Remove-Item -LiteralPath $req.request_path -Force" in script
    assert "Remove-Item -LiteralPath $req.helper_script -Force" in script


def test_helper_never_shadows_powershell_readonly_pid():
    script = _powershell_helper()
    assert "function Wait-ParentExit([int]$ProcessId, [int]$Seconds)" in script
    assert "Get-Process -Id $ProcessId" in script
    assert "[int]$Pid" not in script
    assert "Get-Process -Id $Pid" not in script


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell semantics only")
def test_wait_parent_exit_executes_without_pid_readonly_error():
    """Execute the generated function; regression for '$PID is read-only or constant'."""
    script = _powershell_helper()
    start = script.index("function Wait-ParentExit")
    end = script.index("function Norm", start)
    fn = script[start:end]
    command = fn + "\nif (-not (Wait-ParentExit -ProcessId 2147483647 -Seconds 0)) { exit 9 }"
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "read-only or constant" not in (completed.stderr + completed.stdout).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process semantics only")
def test_brokered_helper_survives_launcher_process_exit(tmp_path):
    """The real PowerShell helper must outlive the short-lived CineCalendar launcher."""
    marker = tmp_path / "brokered-helper-ok.txt"
    helper = tmp_path / "survival.ps1"
    ps_path = str(marker).replace("'", "''")
    helper.write_text(
        "Start-Sleep -Milliseconds 1200\n"
        f"Set-Content -LiteralPath '{ps_path}' -Value 'ok' -Encoding UTF8\n",
        encoding="utf-8-sig",
    )

    args = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(helper),
    ]
    code = (
        "from cinecalendar.updater_v4 import _brokered_popen; "
        "_brokered_popen(" + repr(args) + ")"
    )

    subprocess.run([sys.executable, "-c", code], check=True, cwd=os.getcwd())

    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.15)
    assert marker.exists(), "Brokered updater helper died with its launcher process"
