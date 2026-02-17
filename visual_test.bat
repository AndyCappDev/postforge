@echo off
setlocal enabledelayedexpansion

REM PostForge Visual Regression Test Launcher
REM Activates virtual environment and runs visual_test.py with passed arguments
REM
REM Usage:
REM   visual_test.bat --baseline                  # Generate baseline
REM   visual_test.bat                             # Compare against baseline
REM   visual_test.bat --baseline -- -d pdf        # Pass -d pdf to postforge
REM   visual_test.bat -- --glyph-cache            # Pass --glyph-cache to postforge

REM Change to the directory where this script lives
cd /d "%~dp0"

REM Check if virtual environment exists
if not exist "venv\Scripts\python.exe" (
    echo Virtual environment not found. Please run install.bat first
    exit /b 1
)

REM Split arguments at "--" separator: args before go to visual_test.py,
REM args after get forwarded to postforge via --flags.
set "VISUAL_ARGS="
set "POSTFORGE_ARGS="
set "FOUND_SEP=0"

for %%a in (%*) do (
    if "%%a"=="--" (
        if !FOUND_SEP!==0 (
            set "FOUND_SEP=1"
        ) else (
            set "POSTFORGE_ARGS=!POSTFORGE_ARGS! %%a"
        )
    ) else (
        if !FOUND_SEP!==1 (
            set "POSTFORGE_ARGS=!POSTFORGE_ARGS! %%a"
        ) else (
            set "VISUAL_ARGS=!VISUAL_ARGS! %%a"
        )
    )
)

if defined POSTFORGE_ARGS (
    venv\Scripts\python visual_test.py %VISUAL_ARGS% --flags %POSTFORGE_ARGS%
) else (
    venv\Scripts\python visual_test.py %VISUAL_ARGS%
)

endlocal
