@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Run setup.bat first.
    pause
    exit /b 1
)
python -X utf8 -m jarvis.provider_setup --interactive
set "JARVIS_SETUP_EXIT=%ERRORLEVEL%"
if not "%JARVIS_SETUP_EXIT%"=="0" (
    pause
    exit /b %JARVIS_SETUP_EXIT%
)
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Pythonw was not found. Run setup.bat first.
    pause
    exit /b 1
)
start "JARVIS Desktop" pythonw -X utf8 -m jarvis.ui
exit /b 0
