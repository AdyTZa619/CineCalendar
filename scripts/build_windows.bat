@echo off
setlocal
cd /d "%~dp0\.."
if not exist .venv (
  py -3.12 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pytest -q
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --name CineCalendar --manifest CineCalendar.manifest launcher.py
if errorlevel 1 exit /b 1
if not exist dist\CineCalendar.exe exit /b 1
powershell -NoProfile -Command "$p=Start-Process -FilePath '.\dist\CineCalendar.exe' -PassThru; Start-Sleep -Seconds 4; if($p.HasExited -and $p.ExitCode -ne 0){exit 1}; if(-not $p.HasExited){Stop-Process -Id $p.Id -Force}; exit 0"
if errorlevel 1 exit /b 1
echo Build OK: dist\CineCalendar.exe
endlocal
