@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."
title otae Bot v5.0 Setup

echo ========================================
echo   otae Bot v5.0 Setup
echo ========================================

:: 1. Check Python
echo.
echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found!
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo   %%i

:: 2. Create venv
echo.
echo [2/4] Creating virtual environment...
if exist ".venv\Scripts\python.exe" (
    echo   .venv already exists, skipped
) else (
    python -m venv .venv
    echo   .venv created
)

:: 3. Install dependencies
echo.
echo [3/4] Installing Python packages...

:: First try: Tsinghua mirror (most reliable in China)
echo   Using Tsinghua mirror...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
if not errorlevel 1 goto :install_ok

:: Second try: Aliyun mirror
echo   Tsinghua failed, trying Aliyun mirror...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
if not errorlevel 1 goto :install_ok

:: Third try: default PyPI
echo   Mirror failed, trying default PyPI...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not errorlevel 1 goto :install_ok

echo [ERROR] All pip sources failed. Check your network.
pause
exit /b 1

:install_ok
echo   Packages installed successfully!

:: 4. Done
echo.
echo [4/4] Done! Run scripts\start.bat to launch.
echo ========================================
pause
