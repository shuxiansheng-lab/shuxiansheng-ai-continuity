@echo off
chcp 65001 >nul
REM 把下面这行改成你 shuxiansheng_start.bat 里的 CLAUDE_API_KEY
set CLAUDE_API_KEY=改成你的key

echo.
echo  喂材料
echo  ==================
echo.

cd /d "%~dp0"
python feed.py

echo.
pause
