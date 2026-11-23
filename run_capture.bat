@echo off
rem Headless acquisition only (no GUI): writes to SQLite, prints connect/drop logs.
cd /d "%~dp0AAAwindows_code"
"%~dp0shifeiyuantwo\Scripts\python.exe" main.py %*
if errorlevel 1 pause
