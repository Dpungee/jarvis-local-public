@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -m jarvis.provider_setup --interactive
set "JARVIS_SETUP_EXIT=%ERRORLEVEL%"
if not "%JARVIS_SETUP_EXIT%"=="0" (
    pause
    exit /b %JARVIS_SETUP_EXIT%
)
python -X utf8 -m jarvis
set "JARVIS_EXIT=%ERRORLEVEL%"
if not "%JARVIS_EXIT%"=="0" pause
exit /b %JARVIS_EXIT%
