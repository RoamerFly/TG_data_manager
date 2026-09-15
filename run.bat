@echo off
chcp 65001 >nul 2>&1
title Telegram 缓存管理器

REM Telegram Desktop 缓存数据管理器 启动脚本
REM 使用 Miniconda Python 环境

set PYTHON=E:\A-Environment\Miniconda3\python.exe

REM 如果 Miniconda 不存在, 尝试系统 Python
if not exist "%PYTHON%" (
    set PYTHON=python
)

echo ========================================
echo   Telegram Desktop 缓存数据管理器
echo ========================================
echo.

REM 检查依赖
"%PYTHON%" -c "import flask, tgcrypto, Crypto, PIL" 2>nul
if errorlevel 1 (
    echo 正在安装依赖...
    "%PYTHON%" -m pip install -r requirements.txt
    echo.
)

REM 启动服务器
echo 启动服务...
echo 浏览器访问: http://127.0.0.1:5000
echo 按 Ctrl+C 停止
echo.
"%PYTHON%" server.py %*

pause
