@echo off
cd /d "%~dp0"
echo ============================================================
echo   Compilando Calculadora Modbus EKi Dashboard...
echo   (los mapas de registros y Ekis.xlsx van dentro del .exe)
echo ============================================================
echo.

pip install pyinstaller -q

echo Cerrando proceso anterior si esta corriendo...
taskkill /f /im "Calculadora Modbus EKi.exe" >nul 2>&1

echo Eliminando version anterior...
if exist "dist\Calculadora Modbus EKi.exe" del /f /q "dist\Calculadora Modbus EKi.exe"

python -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --name "Calculadora Modbus EKi" ^
  --add-data "data\PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls;data" ^
  --add-data "data\Public_PM2xxx_PMC Register List_v1001.xls;data" ^
  --add-data "data\9AKK107492A7627_en_F_M4M Modbus map.xlsx;data" ^
  --add-data "Ekis.xlsx;." ^
  --add-data "EKIconfig;EKIconfig" ^
  --hidden-import flask ^
  --hidden-import flask.json ^
  --hidden-import werkzeug ^
  --hidden-import werkzeug.serving ^
  --hidden-import pymodbus ^
  --hidden-import pymodbus.client ^
  --hidden-import pymodbus.client.tcp ^
  --hidden-import pymodbus.transport ^
  --hidden-import pymodbus.framer ^
  --hidden-import pymodbus.framer.socket ^
  --hidden-import pymodbus.pdu ^
  --hidden-import openpyxl ^
  --hidden-import xlrd ^
  --hidden-import jinja2 ^
  web_server.py

if errorlevel 1 (
    echo.
    echo ERROR: La compilacion fallo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Compilacion exitosa!
echo ============================================================
echo.
echo   Archivo:  dist\Calculadora Modbus EKi.exe
echo.
echo   Solo necesitas copiar ESE ARCHIVO a la otra PC.
echo   No necesita Python ni ningun archivo adicional.
echo.
echo   NOTA: Si quieres usar una configuracion diferente de
echo   dispositivos en la otra PC, coloca un Ekis.xlsx en la
echo   misma carpeta que el .exe y sera usado en el primer
echo   arranque para crear web_config.json.
echo ============================================================
echo.
pause
