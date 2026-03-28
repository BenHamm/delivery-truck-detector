@echo off
echo Starting Delivery Truck Detector relay...
echo Press Ctrl+C to stop.
echo.

:: === EDIT THESE VALUES ===
set CAMERA_IP=CAMERA_IP_HERE
set CAMERA_USER=CAMERA_USER_HERE
set CAMERA_PASS=CAMERA_PASS_HERE
:: =========================

python forwarder.py --camera-ip %CAMERA_IP% --camera-user %CAMERA_USER% --camera-pass %CAMERA_PASS%

pause
