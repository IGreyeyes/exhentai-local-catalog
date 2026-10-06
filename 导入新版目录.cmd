@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.14 -X utf8 maintenance.py import
pause
