@echo off
chcp 65001 >nul

echo.
echo   安装书先生的基础依赖
echo   ═══════════════════
echo.

cd /d "%~dp0"
pip install -r requirements.txt

echo.
echo   基础依赖安装完成！
echo.
echo   （可选）如果你想让他能读动态网页，继续运行：
echo   pip install playwright
echo   playwright install chromium
echo.

pause
