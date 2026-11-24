@echo off
rem ============================================================
rem  IMPORTANT: this file must stay GBK / cp936 encoded.
rem ============================================================
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"
title 长江雨课堂 图形窗口

set "PY="
if not defined PY for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY goto :nopython

"%PY%" -c "import requests, selenium" >nul 2>nul
if errorlevel 1 (
    echo   首次运行，正在安装依赖...
    "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
)

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :notk

for %%i in ("%PY%") do set "PYDIR=%%~dpi"
set "PYW=%PY%"
if exist "%PYDIR%pythonw.exe" set "PYW=%PYDIR%pythonw.exe"
start "" "%PYW%" "%~dp0gui.py"
exit /b 0

:nopython
echo   [错误] 没找到可用的 Python 3！请安装 Python 3.12-3.14 并勾选 Add to PATH。
pause
exit /b 1

:notk
echo   [错误] 当前 Python 缺少 Tkinter，请到 python.org 重装（勾选 tcl/tk）。
pause
exit /b 1
