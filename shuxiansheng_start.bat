@echo off
chcp 65001 >nul

REM ═══════════════════════════════════
REM  在下面填入你的 API Key
REM ═══════════════════════════════════
set CLAUDE_API_KEY=改成你的key
set OPENAI_API_KEY=改成你的key

REM （可选）外网语音 — 需要安装 Tailscale 并启用 Funnel
REM start /b tailscale funnel 5210

echo.
echo   书先生
echo   ══════════════
echo   正在启动...
echo   浏览器访问: http://localhost:5210
echo   语音通话: http://localhost:5210/talk
echo.

cd /d "%~dp0"
python shuxiansheng_web.py

pause
