@echo off
cd /d "%~dp0"
echo Instalando dependencias...
pip install -r requirements_web.txt -q

echo.
echo Iniciando dashboard web...
echo Abre tu navegador en: http://localhost:8080
echo.
python web_server.py
if errorlevel 1 (
    echo.
    echo Error al iniciar. Verifica las dependencias con:
    echo   pip install -r requirements_web.txt
    pause
)
