@echo off
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo Error al iniciar. Asegurate de tener instaladas las dependencias:
    echo   pip install -r requirements.txt
    echo.
    pause
)
