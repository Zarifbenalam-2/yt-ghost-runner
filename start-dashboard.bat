@echo off
title StreamStress Ghost Watcher Dashboard
echo ========================================================
echo Starting Ghost Watcher Live Command Dashboard...
echo Open http://127.0.0.1:8766 in your browser
echo ========================================================
cd /d "%~dp0"
python server.py
pause
