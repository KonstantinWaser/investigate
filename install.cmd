@echo off
rem Installs investigate for the current Windows user. No administrator rights needed:
rem everything goes under %USERPROFILE%. Run it again at any time to update.
setlocal
title Install investigate
set "BIN=%USERPROFILE%\.local\bin"
set "PATH=%BIN%;%PATH%"
set "TOOLBIN="

echo Installing investigate for this Windows user. This takes a few minutes.
echo.

echo [1/4] uv, the tool that installs investigate
where uv >nul 2>&1
if not errorlevel 1 goto uv_ready
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 goto fail
where uv >nul 2>&1
if errorlevel 1 goto fail
:uv_ready

echo.
echo [2/4] Claude Code, the agent that does the research
where claude >nul 2>&1
if not errorlevel 1 goto claude_ready
echo Claude Code is not installed on this PC.
set "ANSWER="
set /p "ANSWER=Install it now? [Y/N] "
if /i not "%ANSWER%"=="Y" goto claude_skipped
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
if errorlevel 1 goto fail
where claude >nul 2>&1
if errorlevel 1 goto fail
goto claude_ready
:claude_skipped
echo Skipped. investigate cannot research anything until Claude Code is installed:
echo https://code.claude.com/docs/en/setup
:claude_ready

echo.
echo [3/4] investigate
for /f "usebackq delims=" %%i in (`uv tool dir --bin`) do set "TOOLBIN=%%i"
if not defined TOOLBIN goto fail
if not exist "%TOOLBIN%\investigate.exe" goto install_tool
echo Updating. Quitting investigate first if it is running.
"%TOOLBIN%\investigate.exe" stop-tray
if errorlevel 4 goto tray_unknown
if errorlevel 3 goto tray_stays
if errorlevel 1 goto tray_unknown
:install_tool
uv tool install --force --refresh "investigate[tray,transcribe,instagram] @ https://github.com/KonstantinWaser/investigate/archive/refs/heads/main.zip"
if errorlevel 1 goto fail
uv tool update-shell
if errorlevel 1 echo Could not add %TOOLBIN% to your PATH. investigate still works from its shortcuts.

echo.
echo [4/4] Start menu shortcuts, start at login and the setup page
"%TOOLBIN%\investigate.exe" first-run
if errorlevel 1 goto fail

echo.
echo Done. The setup page is open in your browser. You can close this window.
exit /b 0

:tray_unknown
echo.
echo The installed version could not quit investigate by itself.
echo If its tray icon is showing, right-click it and choose Quit, then press a key.
pause
goto install_tool

:tray_stays
echo.
echo investigate is still running and cannot be updated while it runs.
echo Right-click its tray icon, choose Quit, then run this file again.
goto fail

:fail
echo.
echo Something went wrong, see the messages above. Running this file again is safe.
echo If investigate is running, quit it from its tray icon first.
pause
exit /b 1
