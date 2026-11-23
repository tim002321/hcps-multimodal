@echo off
rem Launch the acquisition GUI (AAAwindows_code) with the project venv.
rem venv python must be used: system python lacks PyQt5/torch/onnxruntime.
cd /d "%~dp0AAAwindows_code"
"%~dp0shifeiyuantwo\Scripts\python.exe" run_monitor.py %*
if errorlevel 1 pause
