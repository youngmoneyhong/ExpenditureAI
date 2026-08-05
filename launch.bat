@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in %CD%\.venv
    py -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Could not create or find .venv\Scripts\python.exe.
    echo Install Python, then run this launcher again.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import streamlit" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install required packages.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501
