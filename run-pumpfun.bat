@echo off
cd /d "%~dp0"
set PONS_VENUE=pumpfun
python -m ponspaper run --port 8788 %*
pause
