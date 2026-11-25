@echo off
rem Launch the eye analysis GUI (eye_tool) with the project venv.
rem venv python is required: it has mediapipe / onnxruntime / PyQt5 / pyqtgraph.
cd /d "%~dp0"
"%~dp0..\shifeiyuantwo\Scripts\python.exe" run_gui.py %*
if errorlevel 1 pause
