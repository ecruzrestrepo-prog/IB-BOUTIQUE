@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto entorno_listo
where py >nul 2>&1
if errorlevel 1 (
    python -m venv .venv
) else (
    py -3 -m venv .venv
)
if errorlevel 1 goto fallo
:entorno_listo
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 'Se requiere Python 3.12 o posterior. Recrea .venv con esa version.')"
if errorlevel 1 goto fallo
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fallo
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto fallo
exit /b 0
:fallo
echo.
echo No se pudo iniciar. Revisa el mensaje anterior y las instrucciones LEEME_PRIMERO.txt.
pause
exit /b 1
