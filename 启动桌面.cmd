@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0ExCatalog.exe" (
  start "" "%~dp0ExCatalog.exe"
  exit /b
)
if exist "%~dp0dist\ExCatalog\ExCatalog.exe" (
  start "" "%~dp0dist\ExCatalog\ExCatalog.exe" --data-root "%~dp0."
  exit /b
)
if exist "%~dp0.venv-desktop\Scripts\pythonw.exe" (
  start "" "%~dp0.venv-desktop\Scripts\pythonw.exe" "%~dp0desktop.py"
  exit /b
)
echo 尚未构建桌面版。请先运行 powershell -ExecutionPolicy Bypass -File build_desktop.ps1
pause
