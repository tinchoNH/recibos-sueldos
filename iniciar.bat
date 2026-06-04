@echo off
chcp 65001 > nul
echo.
echo ===================================================
echo   Recibos de Sueldo - Distribución por Email
echo ===================================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo ERROR: Python no está instalado o no está en el PATH.
    echo Descargalo desde https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Instalando dependencias...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo ERROR al instalar dependencias. Revisá la conexión a internet.
    pause
    exit /b 1
)

echo.
echo Iniciando servidor...
echo.
echo  ► Abriendo http://localhost:8000 en tu navegador...
echo  ► Para cerrar la app, presioná Ctrl+C en esta ventana.
echo.

start "" "http://localhost:8000"
python -m uvicorn app:app --host 127.0.0.1 --port 8000

pause
