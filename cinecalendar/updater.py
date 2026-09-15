from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Callable
import zipfile

import requests


# v2 manifest is ZIP/folder aware. The legacy update.json remains a bridge manifest
# so CineCalendar 2.1 can safely migrate from the old single-EXE updater.
MANIFEST_URL = (
    "https://raw.githubusercontent.com/AdyTZa619/"
    "CineCalendar/main/update-v2.json"
)
POST_UPDATE_MODE = "--cinecalendar-post-update"


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    notes: str = ""
    published_at: str = ""
    channel: str = "stable"


@dataclass
class NativeUpdateRequest:
    parent_pid: int
    app_root: str
    data_root: str
    staged_dir: str
    backup_dir: str
    pending_zip: str
    health: str
    log: str
    expected_version: str
    expected_sha256: str
    updates_dir: str
    helper_script: str
    request_path: str


def _version_key(value: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", str(value or ""))]
    return tuple(nums or [0])


def is_newer_version(remote: str, current: str) -> bool:
    a = _version_key(remote)
    b = _version_key(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def parse_manifest(payload: dict) -> UpdateInfo:
    version = str(payload.get("version") or "").strip()
    url = str(payload.get("url") or "").strip()
    sha = str(payload.get("sha256") or "").strip().lower()
    channel = str(payload.get("channel") or "stable").strip().lower()
    if not version:
        raise ValueError("Manifestul de update nu conține versiunea.")
    if not url.startswith("https://"):
        raise ValueError("URL-ul update-ului trebuie să fie HTTPS.")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("SHA-256 invalid în manifestul de update.")
    if channel != "stable":
        raise ValueError("Canalul manifestului nu este stable.")
    return UpdateInfo(
        version=version,
        url=url,
        sha256=sha,
        notes=str(payload.get("notes") or "").strip(),
        published_at=str(payload.get("publishedAt") or payload.get("published_at") or "").strip(),
        channel=channel,
    )


def check_for_update(current_version: str, timeout: int = 12) -> UpdateInfo | None:
    response = requests.get(
        MANIFEST_URL,
        timeout=(5, timeout),
        headers={"User-Agent": f"CineCalendar/{current_version}", "Cache-Control": "no-cache"},
    )
    response.raise_for_status()
    info = parse_manifest(response.json())
    return info if is_newer_version(info.version, current_version) else None


def sha256_path(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def update_supported() -> bool:
    exe = Path(sys.executable)
    return bool(getattr(sys, "frozen", False) and os.name == "nt" and exe.suffix.lower() == ".exe")


def _log(path: str | Path, message: str) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _download_to(url: str, destination: Path, progress: Callable[[str], None] | None = None) -> None:
    progress = progress or (lambda _m: None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    tmp.unlink(missing_ok=True)
    with requests.get(url, stream=True, timeout=(10, 240), headers={"User-Agent": "CineCalendar-Updater/2.2"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > .35:
                    if total:
                        progress(f"Descarc update: {done/1024/1024:.1f}/{total/1024/1024:.1f} MB ({done*100/total:.0f}%)")
                    else:
                        progress(f"Descarc update: {done/1024/1024:.1f} MB")
                    last = now
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp, destination)


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            target = (root / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Update ZIP invalid: cale nesigură {member.filename!r}.")
        zf.extractall(root)
    exe = root / "CineCalendar.exe"
    internal = root / "_internal"
    if not exe.is_file() or not internal.is_dir():
        raise RuntimeError("Pachetul Premium nu conține CineCalendar.exe și folderul _internal.")
    with exe.open("rb") as fh:
        if fh.read(2) != b"MZ":
            raise RuntimeError("CineCalendar.exe din pachet nu este un executabil Windows valid.")


def _popen(args: list[str]) -> subprocess.Popen:
    kwargs: dict = {"close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, **kwargs)


def _powershell_helper() -> str:
    # Runs outside the app process, so the whole onedir payload can be replaced safely.
    return r'''param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
$req = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json

function Write-UpdateLog([string]$Message) {
  try {
    $stamp = [DateTime]::UtcNow.ToString('o')
    Add-Content -LiteralPath $req.log -Value "$stamp $Message" -Encoding UTF8
  } catch {}
}

function Wait-ParentExit([int]$Pid, [int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $Pid -ErrorAction SilentlyContinue)) { return $true }
    Start-Sleep -Milliseconds 250
  }
  return -not (Get-Process -Id $Pid -ErrorAction SilentlyContinue)
}

function Norm([string]$PathValue) {
  return [IO.Path]::GetFullPath($PathValue).TrimEnd('\\')
}

function Is-ProtectedData([string]$Candidate) {
  return (Norm $Candidate) -ieq (Norm $req.data_root)
}

function Remove-AppPayload([string]$Root) {
  Get-ChildItem -LiteralPath $Root -Force | ForEach-Object {
    if (-not (Is-ProtectedData $_.FullName)) {
      Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop
    }
  }
}

function Copy-Tree([string]$From, [string]$To) {
  New-Item -ItemType Directory -Force -Path $To | Out-Null
  Get-ChildItem -LiteralPath $From -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $To -Recurse -Force -ErrorAction Stop
  }
}

Write-UpdateLog "Updater folder pornit pentru $($req.expected_version)."
if (-not (Wait-ParentExit ([int]$req.parent_pid) 45)) {
  Write-UpdateLog 'Procesul principal nu s-a închis; update anulat fără modificări.'
  exit 7
}

$appRoot = Norm $req.app_root
$staged = Norm $req.staged_dir
$backup = Norm $req.backup_dir
$health = $req.health
$current = Join-Path $appRoot 'CineCalendar.exe'

try {
  if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $backup | Out-Null
  Get-ChildItem -LiteralPath $appRoot -Force | ForEach-Object {
    if (-not (Is-ProtectedData $_.FullName)) {
      Copy-Item -LiteralPath $_.FullName -Destination $backup -Recurse -Force -ErrorAction Stop
    }
  }

  Remove-AppPayload $appRoot
  Copy-Tree $staged $appRoot
  if (-not (Test-Path -LiteralPath $current)) { throw 'CineCalendar.exe lipsește după copiere.' }
} catch {
  Write-UpdateLog "Instalare eșuată înainte de pornire: $($_.Exception.Message). Rollback."
  try {
    Remove-AppPayload $appRoot
    Copy-Tree $backup $appRoot
    Start-Process -FilePath $current | Out-Null
  } catch {
    Write-UpdateLog "ROLLBACK EȘUAT: $($_.Exception.Message)"
    exit 6
  }
  exit 3
}

try { Remove-Item -LiteralPath $health -Force -ErrorAction SilentlyContinue } catch {}
$argLine = '--cinecalendar-post-update "{0}" "{1}"' -f $health, $req.expected_version
try {
  $child = Start-Process -FilePath $current -ArgumentList $argLine -PassThru
} catch {
  Write-UpdateLog "Noua versiune nu pornește: $($_.Exception.Message). Rollback."
  Remove-AppPayload $appRoot
  Copy-Tree $backup $appRoot
  Start-Process -FilePath $current | Out-Null
  exit 4
}

$ok = $false
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
  if (Test-Path -LiteralPath $health) {
    try {
      $first = (Get-Content -LiteralPath $health -TotalCount 1).Trim()
      if ($first -eq [string]$req.expected_version) { $ok = $true; break }
    } catch {}
  }
  if ($child.HasExited) { break }
  Start-Sleep -Milliseconds 400
}

if ($ok) {
  Write-UpdateLog 'Health-check reușit; update folder confirmat.'
  try { Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue } catch {}
  try { Remove-Item -LiteralPath $req.pending_zip -Force -ErrorAction SilentlyContinue } catch {}
  try { Remove-Item -LiteralPath $health -Force -ErrorAction SilentlyContinue } catch {}
  exit 0
}

Write-UpdateLog 'Health-check eșuat; execut rollback automat.'
try { if (-not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue } } catch {}
try {
  Remove-AppPayload $appRoot
  Copy-Tree $backup $appRoot
  Start-Process -FilePath $current | Out-Null
  Write-UpdateLog 'Rollback terminat; versiunea anterioară a fost repornită.'
  exit 5
} catch {
  Write-UpdateLog "ROLLBACK EȘUAT: $($_.Exception.Message)"
  exit 6
}
'''


def stage_and_start_update(info: UpdateInfo, data_root: str | Path,
                           progress: Callable[[str], None] | None = None) -> NativeUpdateRequest:
    if not update_supported():
        raise RuntimeError("Updaterul automat funcționează numai în CineCalendar.exe pentru Windows.")
    progress = progress or (lambda _m: None)
    current = Path(sys.executable).resolve()
    app_root = current.parent
    data_root = Path(data_root).resolve()
    updates = data_root / "updates"
    updates.mkdir(parents=True, exist_ok=True)

    pending_zip = updates / f"CineCalendar-{info.version}.pending.zip"
    staged = updates / f"staged-{info.version}"
    backup = updates / "backup" / "previous"
    health = updates / "health.ok"
    log = updates / "updater.log"
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
        parent_pid=os.getpid(), app_root=str(app_root), data_root=str(data_root),
        staged_dir=str(staged), backup_dir=str(backup), pending_zip=str(pending_zip),
        health=str(health), log=str(log), expected_version=info.version,
        expected_sha256=info.sha256, updates_dir=str(updates), helper_script=str(helper),
        request_path=str(request_path),
    )
    request_path.write_text(json.dumps(asdict(req), ensure_ascii=False, indent=2), encoding="utf-8")
    _log(log, f"Update folder {info.version} staged; handoff către PowerShell helper.")

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("Windows PowerShell nu a fost găsit; update-ul nu a fost aplicat.")
    _popen([
        powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(helper), str(request_path),
    ])
    progress("Update pregătit. CineCalendar se închide și se va reporni automat.")
    return req


def write_health_marker(path: str | Path, version: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(f"{version}\n{datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
    os.replace(tmp, p)


def health_matches(path: str | Path, version: str) -> bool:
    try:
        first = Path(path).read_text(encoding="utf-8").splitlines()[0].strip()
        return first == str(version).strip()
    except Exception:
        return False


def parse_special_startup(argv: list[str]) -> tuple[int | None, tuple[str, str] | None]:
    """Return (exit_code, post_update_health). exit_code=None means continue normal UI."""
    if len(argv) >= 4 and argv[1] == POST_UPDATE_MODE:
        return None, (argv[2], argv[3])
    return None, None
