@echo off
cd /d "%~dp0"
py -3.14 launch.py --stop
if errorlevel 1 pause
