@echo off
setlocal
cd /d "%~dp0"
if not defined SPECTROMETER_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "SPECTROMETER_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined SPECTROMETER_PYTHON set "SPECTROMETER_PYTHON=%USERPROFILE%\.venvs\codex-science-v1\Scripts\python.exe"
if not exist "%SPECTROMETER_PYTHON%" (
    echo Python was not found at "%SPECTROMETER_PYTHON%".
    echo Set SPECTROMETER_PYTHON or create the project environment described in README.md.
    pause
    exit /b 1
)
"%SPECTROMETER_PYTHON%" -X utf8 -m spectrometer_app %*
if errorlevel 1 (
    pause
    exit /b 1
)
