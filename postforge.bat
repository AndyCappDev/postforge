@echo off
REM PostForge PostScript Interpreter Launcher
REM Activates virtual environment and runs postforge.py with passed arguments

REM Try common virtual environment locations
set VENV_ACTIVATE=
if exist "venv\Scripts\activate.bat" set VENV_ACTIVATE=venv\Scripts\activate.bat
if exist ".venv\Scripts\activate.bat" set VENV_ACTIVATE=.venv\Scripts\activate.bat
if exist "env\Scripts\activate.bat" set VENV_ACTIVATE=env\Scripts\activate.bat

if "%VENV_ACTIVATE%"=="" (
    echo Virtual environment not found. Looked for:
    echo   venv\Scripts\activate.bat
    echo   .venv\Scripts\activate.bat
    echo   env\Scripts\activate.bat
    echo.
    echo Please run install.bat first to set up the environment.
    echo.
    echo Or run directly with: python -m postforge %*
    exit /b 1
)

REM Activate virtual environment
echo Activating virtual environment: %VENV_ACTIVATE%
call %VENV_ACTIVATE%

REM Run postforge.py with all passed arguments
python -m postforge %*

REM Deactivate virtual environment
deactivate
