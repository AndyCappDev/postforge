@echo off
setlocal enabledelayedexpansion

REM PostForge Installation Script for Windows
REM Checks prerequisites and sets up the Python environment

echo ==================================
echo   PostForge Installation Script
echo ==================================
echo.

REM Change to the directory where this script lives
cd /d "%~dp0"

REM Check Python version
echo Checking Python version...

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python not found.
    echo.
    echo Please install Python 3.13+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    exit /b 1
)

for /f "tokens=*" %%i in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PYTHON_VERSION=%%i
for /f "tokens=*" %%i in ('python -c "import sys; print(sys.version_info.major)"') do set PYTHON_MAJOR=%%i
for /f "tokens=*" %%i in ('python -c "import sys; print(sys.version_info.minor)"') do set PYTHON_MINOR=%%i

echo Found Python %PYTHON_VERSION%

if %PYTHON_MAJOR% lss 3 (
    echo Error: Python 3.12+ required ^(3.13+ recommended^).
    echo Found Python %PYTHON_VERSION%
    exit /b 1
)
if %PYTHON_MAJOR% equ 3 if %PYTHON_MINOR% lss 12 (
    echo Error: Python 3.12+ required ^(3.13+ recommended^).
    echo Found Python %PYTHON_VERSION%
    exit /b 1
)
if %PYTHON_MAJOR% equ 3 if %PYTHON_MINOR% lss 13 (
    echo Warning: Python 3.13+ recommended for best experience.
)
echo Python version OK
echo.

REM Create virtual environment
echo Setting up Python virtual environment...

if exist "venv\Scripts\python.exe" (
    echo Virtual environment already exists.
) else (
    if exist "venv" rmdir /s /q venv
    python -m venv venv
    if %errorlevel% neq 0 (
        if exist "venv" rmdir /s /q venv
        echo.
        echo Failed to create virtual environment.
        echo.
        echo Reinstall Python from https://www.python.org/downloads/
        echo Make sure to check "Add Python to PATH" during installation.
        echo.
        echo Then run install.bat again.
        exit /b 1
    )
    echo Created virtual environment.
)

REM Install package with dependencies
echo.
echo Installing Python dependencies...
echo.

venv\Scripts\python -m pip install --upgrade pip -q
if %errorlevel% neq 0 (
    echo Error: Failed to upgrade pip.
    exit /b 1
)

venv\Scripts\python -m pip install -e ".[qt,dev,visual-test]"
if %errorlevel% neq 0 (
    echo.
    echo Error: Failed to install dependencies.
    echo.
    echo If pycairo failed, you may need to install the GTK3 runtime:
    echo   https://github.com/nickvdp/gtk3-windows-installer
    echo.
    echo Then run install.bat again.
    exit /b 1
)

REM Build Cython accelerators (optional - PostForge runs without them)
echo.
echo Building Cython accelerators...
venv\Scripts\python setup_cython.py build_ext --inplace >nul 2>&1
if %errorlevel% neq 0 goto cython_failed
echo Cython build OK -- execution loop accelerated (15-40%% speedup)
goto cython_done
:cython_failed
echo Cython build failed -- PostForge will use the pure Python fallback.
echo To enable Cython acceleration, install Microsoft C++ Build Tools:
echo   https://visualstudio.microsoft.com/visual-cpp-build-tools/
echo Select the "Desktop development with C++" workload during installation.
echo Then run install.bat again.
:cython_done

REM Add venv\Scripts to user PATH so pf works from anywhere
set "SCRIPTS_DIR=%~dp0venv\Scripts"

echo.
echo Installing system commands (pf, postforge)...

echo %PATH% | findstr /i /c:"%SCRIPTS_DIR%" >nul 2>&1
if !errorlevel! equ 0 (
    echo Commands already in PATH.
) else (
    set "USER_PATH="
    for /f "usebackq tokens=2,*" %%A in (`reg query HKCU\Environment /v PATH 2^>nul`) do set "USER_PATH=%%B"
    if defined USER_PATH (
        setx PATH "!USER_PATH!;%SCRIPTS_DIR%" >nul 2>&1
    ) else (
        setx PATH "%SCRIPTS_DIR%" >nul 2>&1
    )
    if !errorlevel! equ 0 (
        echo Added to PATH: %SCRIPTS_DIR%
        echo.
        echo NOTE: Open a new terminal window for the 'pf' command to be available.
    ) else (
        echo Could not update PATH automatically.
        echo To use 'pf' from anywhere, add this directory to your PATH:
        echo   %SCRIPTS_DIR%
    )
)

echo.
echo ==================================
echo   Installation Complete!
echo ==================================
echo.
echo Run PostForge with:
echo.
echo   pf                                     # Interactive prompt
echo   pf samples\tiger.ps                    # Render the classic tiger
echo   pf -d png input.ps                     # Save to .\pf_output directory
echo.

endlocal
