@echo off
rem ChatCut Studio：Windows 启动（双击运行）。用法：
rem   run.bat          正常启动
rem   run.bat demo     演示模式：不调用任何模型，不花钱
rem   run.bat debug    输出详细日志
rem Studio 被意外结束（比如 Agent 运行了 Stop-Process -Name python）时，3 秒后自动重新启动，
rem ChatCut Desktop 里的 Agent 下一次请求就能恢复。要彻底退出：直接关掉这个窗口。
chcp 65001 >nul
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo 还没有安装，请先运行：powershell -ExecutionPolicy Bypass -File chatcut_studio\windows\setup.ps1
  pause & exit /b 1
)
set PYTHONUTF8=1
if /i "%1"=="demo" set CHATCUT_STUDIO_DEMO=1
if /i "%1"=="debug" set LOG_LEVEL=debug
start "" http://127.0.0.1:8765
:loop
".venv\Scripts\python.exe" -m chatcut_studio
echo.
echo [%date% %time%] ChatCut Studio 已停止，3 秒后自动重新启动（要退出请直接关掉这个窗口）……
timeout /t 3 /nobreak >nul
goto loop
