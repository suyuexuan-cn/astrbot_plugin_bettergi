@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title BetterGI 远程辅助程序

if not exist "%~dp0python\python.exe" goto NOPY

"%~dp0python\python.exe" "%~dp0launcher.py" %*
if errorlevel 1 pause
goto :EOF

:NOPY
echo.
echo  [错误] 找不到内置 Python 运行环境：
echo         %~dp0python\python.exe
echo.
echo  请重新解压完整的压缩包（不要只复制单个文件或部分目录），
echo  解压后目录里应当有 python 文件夹。
echo.
pause
exit /b 1
