@echo off
cd /d "%~dp0"
python detector.py --ui
if errorlevel 1 pause
