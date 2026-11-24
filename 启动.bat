@echo off
rem ============================================================
rem  IMPORTANT: this file must stay GBK / cp936 encoded.
rem  cmd.exe loses byte-sync on UTF-8 .bat files that contain
rem  Chinese text, which splits lines and throws fake errors.
rem ============================================================
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"
title 长江雨课堂 自动刷课

rem ---------- 1. locate python ----------
set "PY="
if not defined PY for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY goto :nopython

rem ---------- 2. dependency self-check ----------
"%PY%" -c "import requests, selenium" >nul 2>nul
if not errorlevel 1 goto :ready
echo.
echo   首次运行，正在安装依赖（走清华镜像，约 1 分钟）...
echo.
"%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 goto :pipfail

:ready
rem ---------- 3. pass-through when args are given ----------
if "%~1"=="" goto :menu
chcp 65001 >nul 2>nul
"%PY%" yuketang.py %* 2>&1 | "%PY%" tee_log.py
chcp 936 >nul 2>nul
echo.
pause
goto :done

rem ---------- 4. interactive menu ----------
:menu
cls
echo.
echo   ==========================================================
echo       长江雨课堂   自动刷课 / 视频 / 作业
echo   ==========================================================
echo.
echo       Python 路径：%PY%
echo.
echo       本工具协议直连 changjiang.yuketang.cn，不开浏览器。
echo       首次使用推荐执行 [1]（自动弹浏览器，登录后自动抓 Cookie）。
echo.
echo       [1]  浏览器登录抓 Cookie（推荐，全自动抓取+验证）
echo       [2]  测试连接（验证 Cookie 是否有效）
echo       [3]  查看课程列表（确认要刷的课程序号）
echo       [4]  开始刷课（视频 + 作业，实时滚动进度）
echo       [5]  图形窗口（推荐：Cookie 抓取按钮 + 进度条 + 日志区）
echo       [6]  打开运行日志文件夹（排障 / 留档）
echo       [7]  手动配置 Cookie（F12 复制粘贴，兜底方案）
echo       [8]  退出
echo.
echo   ==========================================================
echo.
choice /c 12345678 /n /m "  请选择 1 / 2 / 3 / 4 / 5 / 6 / 7 / 8："
if errorlevel 8 goto :done
if errorlevel 7 goto :cookie
if errorlevel 6 goto :logs
if errorlevel 5 goto :gui
if errorlevel 4 goto :run
if errorlevel 3 goto :list
if errorlevel 2 goto :test
if errorlevel 1 goto :browserlogin

:browserlogin
echo.
echo   即将打开浏览器登录长江雨课堂：
echo   - 微信扫码 / 账号密码登录都行；
echo   - 登录成功后 Cookie 自动抓取并保存（10 分钟内有效等待）；
echo   - 首次使用会自动安装 selenium 并下载浏览器驱动，稍等。
echo.
pause
echo.
chcp 65001 >nul 2>nul
"%PY%" browser_login.py 2>&1 | "%PY%" tee_log.py
chcp 936 >nul 2>nul
echo.
pause
goto :menu

:test
echo.
chcp 65001 >nul 2>nul
"%PY%" yuketang.py test 2>&1 | "%PY%" tee_log.py
chcp 936 >nul 2>nul
echo.
pause
goto :menu

:list
echo.
echo   正在获取课程列表...
echo.
chcp 65001 >nul 2>nul
"%PY%" yuketang.py ls 2>&1 | "%PY%" tee_log.py
chcp 936 >nul 2>nul
echo.
echo   课程列表已展示，记住序号就可以刷课了。
pause
goto :menu

:run
echo.
if not exist "cookie.txt" goto :nocookie
set "IDX="
set /p "IDX=  课程序号（all=全部，先看 [3] 列表）："
if not defined IDX goto :menu
set "MODE="
set /p "MODE=  内容（回车=全部，可选：视频 / 作业）："
echo.
echo   即将开始，注意：
echo   - 作业答案由接口直接下发，自动提交；
echo   - 视频倍速默认 10x，限流会自动等待，进度不涨自动降速；
echo   - 期间浏览器里不要退出该账号的登录。
echo.
pause
echo   开始执行...
echo.
chcp 65001 >nul 2>nul
if defined MODE goto :runmode
"%PY%" yuketang.py %IDX% 2>&1 | "%PY%" tee_log.py
goto :runend
:runmode
"%PY%" yuketang.py %IDX% %MODE% 2>&1 | "%PY%" tee_log.py
:runend
chcp 936 >nul 2>nul
echo.
echo   本次运行结束，完整日志已存到 运行日志 文件夹。
pause
goto :menu

:nocookie
echo.
echo   [提示] 还没有配置 Cookie，请先执行 [1] 浏览器登录抓取。
pause
goto :menu

:gui
"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :notk
for %%i in ("%PY%") do set "PYDIR=%%~dpi"
set "PYW=%PY%"
if exist "%PYDIR%pythonw.exe" set "PYW=%PYDIR%pythonw.exe"
start "" "%PYW%" "%~dp0gui.py"
echo   图形窗口已打开（本窗口可以关掉）。
timeout /t 2 >nul
goto :done

:cookie
echo.
echo   请粘贴浏览器 Cookie（登录长江雨课堂后 F12 复制，详见提示）...
echo.
chcp 65001 >nul 2>nul
"%PY%" yuketang.py setcookie 2>&1 | "%PY%" tee_log.py
chcp 936 >nul 2>nul
echo.
pause
goto :menu

:logs
if exist "运行日志" (
    start "" explorer "%~dp0运行日志"
) else (
    echo.
    echo   还没有运行日志，先跑一次 [1] 或 [4]。
    pause
)
goto :menu

rem ---------- error exits ----------
:nopython
echo.
echo   [错误] 没找到可用的 Python 3！
echo   请先安装 Python 3.12 - 3.14（安装时勾选 Add Python to PATH）。
echo.
pause
goto :done

:notk
echo.
echo   [错误] 当前 Python 缺少 Tkinter 图形库。
echo   请到 python.org 重新安装 Python（勾选 tcl/tk and IDLE）。
echo.
pause
goto :done

:pipfail
echo.
echo   [错误] 依赖安装失败，请检查网络后重开本文件。
echo.
pause

:done
endlocal
exit /b 0
