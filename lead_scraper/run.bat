@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python is not installed.
  echo Download it from https://www.python.org/downloads/
  echo During install, tick "Add Python to PATH", then run this file again.
  pause
  exit /b
)

rem Stops Streamlit from asking for an email address on first launch
if not exist "%USERPROFILE%\.streamlit\credentials.toml" call :makecred

if not exist venv\Scripts\activate.bat (
  echo First-time setup, this takes a few minutes. Please wait...
  python -m venv venv
  call venv\Scripts\activate.bat
  pip install -r requirements.txt
  playwright install chromium
) else (
  call venv\Scripts\activate.bat
)

echo Starting Business Lead Finder. Keep this window open while you use it.
streamlit run app.py --server.address localhost --browser.gatherUsageStats false
pause
exit /b

:makecred
mkdir "%USERPROFILE%\.streamlit" 2>nul
echo [general]> "%USERPROFILE%\.streamlit\credentials.toml"
echo email = "">> "%USERPROFILE%\.streamlit\credentials.toml"
exit /b
