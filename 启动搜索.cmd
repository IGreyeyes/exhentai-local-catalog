@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.14 --version >nul 2>&1
if errorlevel 1 (
  echo 未找到 Python 3.14。请先安装官方 Python 3.14 和 Windows Python 启动器。
  echo 安装后重新双击本文件：https://www.python.org/downloads/
  pause
  exit /b 1
)
py -3.14 -X utf8 launch.py
if errorlevel 1 pause
