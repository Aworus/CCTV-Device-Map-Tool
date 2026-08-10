@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -c "import PIL" >nul 2>&1
if errorlevel 1 (
    echo Brakuje biblioteki Pillow.
    echo Zainstaluj zaleznosci: py -m pip install -r requirements.txt
    pause
    exit /b 1
)

py "%~dp0app\cctv_device_manager.py"
if errorlevel 1 pause
