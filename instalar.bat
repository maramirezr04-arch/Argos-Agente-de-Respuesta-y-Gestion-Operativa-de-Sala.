@echo off
title Instalador Liverpool Bot
setlocal

:: ── Verificar permisos de administrador ──────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  Se necesitan permisos de Administrador.
    echo  Elevando permisos...
    echo.
    powershell -Command "Start-Process cmd -ArgumentList '/c cd /d ""%~dp0"" && instalar.bat' -Verb RunAs"
    exit /b
)

:: ── Verificar Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ============================================================
    echo   ERROR: Python no esta instalado o no esta en el PATH
    echo  ============================================================
    echo.
    echo  Descargalo desde: https://www.python.org/downloads/
    echo  Durante la instalacion marca: "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

:: ── Mostrar encabezado ────────────────────────────────────────────────────────
echo.
echo  ============================================================
echo    INSTALADOR LIVERPOOL BOT
echo  ============================================================
echo.
echo  Python encontrado:
python --version
echo.
echo  Iniciando instalacion...
echo.

:: ── Ir a la carpeta del instalador ────────────────────────────────────────────
cd /d "%~dp0"

:: ── Ejecutar instalador Python ────────────────────────────────────────────────
python instalar.py
set EXIT_CODE=%errorlevel%

if %EXIT_CODE% neq 0 (
    echo.
    echo  Ocurrio un error durante la instalacion.
    echo  Revisa los mensajes arriba.
    echo.
    pause
    exit /b %EXIT_CODE%
)

endlocal
