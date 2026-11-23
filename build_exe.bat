@echo off
rem ============================================================
rem  Build HCPS monitor (onedir, runtime bundled, portable)
rem  Output: dist\HCPS\  -- copy the whole folder to another PC
rem  Note: ASCII only, CRLF line endings. cmd.exe cannot parse LF.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0shifeiyuantwo\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found: %PY%
    exit /b 1
)

rem Keep PyInstaller cache inside the project (default lives in LOCALAPPDATA).
set "PYINSTALLER_CONFIG_DIR=%~dp0build_cache"

if exist "dist\HCPS" (
    echo [ERROR] dist\HCPS already exists. Rename or delete it first, then rerun.
    exit /b 1
)

echo [1/2] running PyInstaller (torch is big, 5-15 min) ...
"%PY%" -m PyInstaller --noconfirm --distpath "%~dp0dist" --workpath "%~dp0build" "%~dp0AAAwindows_code\hcps.spec"
if errorlevel 1 (
    echo [FAILED] build aborted, see messages above
    exit /b 1
)

if exist "dist\HCPS\_internal\ppg_pipeline\__pycache__" rmdir /s /q "dist\HCPS\_internal\ppg_pipeline\__pycache__"

echo [2/2] copying field readme ...
copy /y "%~dp0docs_pack\shiyongshuoming.txt" "dist\HCPS\shiyongshuoming.txt" >nul 2>&1

echo.
echo [DONE] dist\HCPS
echo        exe    : dist\HCPS\HCPS.exe
echo        verify : dist\HCPS\HCPS.exe --selftest
endlocal
