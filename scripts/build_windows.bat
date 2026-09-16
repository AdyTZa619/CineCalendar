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
python scripts\benchmark_engine_v3.py
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean --onedir --windowed --name CineCalendar --manifest CineCalendar.manifest --hidden-import=sqlite3 --collect-all implicit launcher.py
if errorlevel 1 exit /b 1
if not exist dist\CineCalendar\CineCalendar.exe exit /b 1
if not exist dist\CineCalendar\_internal exit /b 1
copy /Y MOVIELENS_MODEL_NOTICE.txt dist\CineCalendar\MOVIELENS_MODEL_NOTICE.txt >nul
set "CINECALENDAR_DATA_DIR=%TEMP%\cinecalendar-local-smoke-data"
if exist "%CINECALENDAR_DATA_DIR%" rmdir /S /Q "%CINECALENDAR_DATA_DIR%"
powershell -NoProfile -Command "$p=Start-Process -FilePath '.\dist\CineCalendar\CineCalendar.exe' -PassThru; Start-Sleep -Seconds 8; if($p.HasExited -and $p.ExitCode -ne 0){exit 1}; if(-not $p.HasExited){Stop-Process -Id $p.Id -Force}; exit 0"
if errorlevel 1 exit /b 1
echo Build OK: dist\CineCalendar\CineCalendar.exe + _internal
endlocal
