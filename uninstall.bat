@echo off
setlocal enabledelayedexpansion

REM PostForge Uninstall Script for Windows
REM Removes virtual environment, PATH entry, and cached data

echo ==================================
echo   PostForge Uninstall
echo ==================================
echo.

REM Change to the directory where this script lives
cd /d "%~dp0"

REM Remove venv\Scripts from user PATH
set "SCRIPTS_DIR=%~dp0venv\Scripts"

echo Removing PostForge from PATH...
set "USER_PATH="
for /f "usebackq tokens=2,*" %%A in (`reg query HKCU\Environment /v PATH 2^>nul`) do set "USER_PATH=%%B"

if defined USER_PATH (
    REM Use PowerShell to cleanly remove the path entry
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$path='!USER_PATH!'; $remove='%SCRIPTS_DIR%'; $parts=$path.Split(';') | Where-Object { $_ -ne $remove -and $_ -ne '' }; $parts -join ';'"`) do set "NEW_PATH=%%P"

    if "!NEW_PATH!" neq "!USER_PATH!" (
        if defined NEW_PATH (
            setx PATH "!NEW_PATH!" >nul 2>&1
        ) else (
            reg delete HKCU\Environment /v PATH /f >nul 2>&1
        )
        echo Removed from PATH: %SCRIPTS_DIR%
    ) else (
        echo PostForge was not in PATH.
    )
) else (
    echo PostForge was not in PATH.
)
echo.

REM Remove virtual environment
if exist "venv" (
    echo Removing virtual environment...
    rmdir /s /q venv
    echo Removed: venv\
) else (
    echo No virtual environment found.
)
echo.

REM Remove font discovery cache
set "CACHE_DIR=%USERPROFILE%\.cache\postforge"
if exist "%CACHE_DIR%" (
    echo Removing font cache...
    rmdir /s /q "%CACHE_DIR%"
    echo Removed: %CACHE_DIR%
) else (
    echo No font cache found.
)
echo.

REM Remove build artifacts
set CLEANED=0
if exist "build" (
    rmdir /s /q build
    echo Removed: build\
    set CLEANED=1
)
if exist "postforge.egg-info" (
    rmdir /s /q postforge.egg-info
    echo Removed: postforge.egg-info\
    set CLEANED=1
)
if !CLEANED! equ 0 (
    echo No build artifacts found.
)
echo.

echo ==================================
echo   Uninstall Complete
echo ==================================
echo.
echo The PostForge source code is still in: %~dp0
echo.
echo NOTE: Open a new terminal window for PATH changes to take effect.
echo.

endlocal
