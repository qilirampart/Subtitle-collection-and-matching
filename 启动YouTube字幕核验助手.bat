@echo off
setlocal
cd /d "%~dp0"
python "%~dp0main.py"
if errorlevel 1 (
    echo.
    echo YouTube字幕核验助手启动失败，请保留此窗口并把上面的错误发给开发人员。
    pause
)
