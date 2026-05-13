@echo off
title Instalador Liverpool Bot
echo.
echo  ============================================================
echo   INSTALADOR LIVERPOOL BOT
echo  ============================================================
echo.

:: Verificar que Python este instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python no esta instalado o no esta en el PATH.
    echo.
    echo  Descargalo desde: https://www.python.org/downloads/
    echo  Durante la instalacion marca: "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo  Python encontrado:
python --version
echo.
echo  Iniciando instalacion...
echo.

python instalar.py

:: Si instalar.py no existe, dar instrucciones
if %errorlevel% neq 0 (
    echo.
    echo  Hubo un error. Revisa los mensajes arriba.
    pause
)
