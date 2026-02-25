@echo off
title Kite Trading System v1.0
echo.
echo ================================================
echo     Kite Trading System v1.0 - Starting Server
echo ================================================
echo.
echo Opening browser in 3 seconds...
echo.

cd /d "%~dp0"
python app.py

pause
