@echo off
title Elder Trading System
echo.
echo ================================================
echo     Elder Trading System - Starting Server
echo ================================================
echo.
echo Opening browser in 3 seconds...
echo.

cd /d "%~dp0"
python app.py

pause
