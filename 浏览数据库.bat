@echo off
rem Open the read-only SQLite session browser with the project venv.
cd /d "%~dp0"
"%~dp0shifeiyuantwo\Scripts\python.exe" "%~dp0Êý¾Ý¿âä¯ÀÀ.py" %*
if errorlevel 1 pause
