@echo off
setlocal
if not defined SPECTROMETER_LIGHTKEEPER_CHECKOUT set "SPECTROMETER_LIGHTKEEPER_CHECKOUT=%USERPROFILE%\Desktop\Repos\LightKeeper\lightkeeper_repo"
if not defined LIGHTKEEPER_PYTHON set "LIGHTKEEPER_PYTHON=%USERPROFILE%\.venvs\codex-science-v1\Scripts\pythonw.exe"
set "SPECTROMETER_LIVE_SOURCE=%~dp0"
if not exist "%SPECTROMETER_LIGHTKEEPER_CHECKOUT%\Launch_LightKeeper.cmd" (
    echo LightKeeper source checkout was not found at "%SPECTROMETER_LIGHTKEEPER_CHECKOUT%".
    pause
    exit /b 1
)
call "%SPECTROMETER_LIGHTKEEPER_CHECKOUT%\Launch_LightKeeper.cmd" %*
