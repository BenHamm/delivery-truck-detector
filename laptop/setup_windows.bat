@echo off
echo ==========================================
echo  Delivery Truck Detector - Laptop Relay
echo ==========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found. Install from https://www.python.org/downloads/
    echo     Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo [OK] Python found

:: Check ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [!] ffmpeg not found. Install from https://www.gyan.dev/ffmpeg/builds/
    echo     Download "ffmpeg-release-essentials.zip", extract, and add bin\ to PATH.
    echo     Or run: winget install ffmpeg
    pause
    exit /b 1
)
echo [OK] ffmpeg found

:: Install Python deps
echo.
echo Installing Python dependencies...
pip install requests >nul 2>&1
echo [OK] Dependencies installed

:: Check Tailscale
tailscale status >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Tailscale not found or not connected.
    echo     Install from https://tailscale.com/download/windows
    echo     Then sign in with Ben's Tailscale account.
    pause
    exit /b 1
)
echo [OK] Tailscale connected

echo.
echo ==========================================
echo  Setup complete! Now run:
echo    python forwarder.py
echo ==========================================
pause
