@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

echo ============================================================
echo   Argos — Migración desde Liverpool Bot 456
echo   Este script renombra la carpeta del bot y actualiza
echo   la tarea en el Programador de tareas de Windows.
echo ============================================================
echo.

:: ── Buscar carpeta del bot en rutas comunes ────────────────
:: Maneja tanto "liverpool-bot" como "liverpool-automation"
set "CARPETA_ORIGEN="
set "CARPETA_DESTINO="

for %%D in (C D E F) do (
    for %%N in (liverpool-bot liverpool-automation argos-old) do (
        if exist "%%D:\%%N\main.py" (
            if "!CARPETA_ORIGEN!"=="" (
                set "CARPETA_ORIGEN=%%D:\%%N"
                set "CARPETA_DESTINO=%%D:\argos"
            )
        )
        if exist "%%D:\Users\%USERNAME%\%%N\main.py" (
            if "!CARPETA_ORIGEN!"=="" (
                set "CARPETA_ORIGEN=%%D:\Users\%USERNAME%\%%N"
                set "CARPETA_DESTINO=%%D:\Users\%USERNAME%\argos"
            )
        )
        if exist "%%D:\Users\Public\%%N\main.py" (
            if "!CARPETA_ORIGEN!"=="" (
                set "CARPETA_ORIGEN=%%D:\Users\Public\%%N"
                set "CARPETA_DESTINO=%%D:\Users\Public\argos"
            )
        )
    )
)

if "!CARPETA_ORIGEN!"=="" (
    echo  ERROR: No se encontro la carpeta liverpool-bot.
    echo  Busca manualmente la carpeta y renombrala a argos.
    pause
    exit /b 1
)

echo  Carpeta encontrada: !CARPETA_ORIGEN!
echo  Se renombrara a:    !CARPETA_DESTINO!
echo.

if exist "!CARPETA_DESTINO!" (
    echo  ADVERTENCIA: Ya existe la carpeta !CARPETA_DESTINO!
    echo  Esto podria indicar que ya fue migrada antes.
    echo  Presiona una tecla para continuar de todos modos o Ctrl+C para cancelar.
    pause >nul
)

:: ── Renombrar carpeta ────────────────────────────────────────
echo  Renombrando carpeta...
ren "!CARPETA_ORIGEN!" "argos"
if errorlevel 1 (
    echo  ERROR al renombrar. Puede haber archivos en uso.
    echo  Cierra el bot y vuelve a intentar.
    pause
    exit /b 1
)
echo  Carpeta renombrada correctamente.
echo.

:: ── Actualizar Task Scheduler ────────────────────────────────
echo  Actualizando Programador de tareas...

:: Buscar tarea del bot (puede llamarse ArgosBot o LiverpoolBot o similar)
set "TAREA_ENCONTRADA="
for /f "tokens=*" %%T in ('schtasks /query /fo LIST 2^>nul ^| findstr /i "argos\|liverpool"') do (
    set "TAREA_ENCONTRADA=%%T"
)

:: Intentar actualizar la tarea más común (varios nombres posibles)
set "NOMBRE_TAREA="
for %%T in ("ArgosBot" "Argos Bot" "LiverpoolBot" "Liverpool Bot" "Liverpool Bot 456") do (
    if "!NOMBRE_TAREA!"=="" (
        schtasks /query /tn %%T >nul 2>&1
        if not errorlevel 1 set "NOMBRE_TAREA=%%~T"
    )
)

if "!NOMBRE_TAREA!"=="" (
    echo  No se encontro tarea en el Programador. Puede que no haya sido instalada aun.
    echo  Si la tarea se llama diferente, actualizala manualmente apuntando a:
    echo    !CARPETA_DESTINO!\pythonw.exe main.py
    echo  o bien ejecuta instalar.bat de nuevo desde la nueva carpeta.
) else (
    echo  Tarea encontrada: !NOMBRE_TAREA!
    :: Obtener el comando actual y reemplazar la ruta
    for /f "tokens=*" %%P in ('schtasks /query /tn "!NOMBRE_TAREA!" /fo LIST 2^>nul ^| findstr /i "Ejecutar"') do (
        set "CMD_ACTUAL=%%P"
    )
    :: Crear nueva tarea con ruta actualizada (cada 15 min, repetir)
    schtasks /change /tn "!NOMBRE_TAREA!" /tr "\"!CARPETA_DESTINO!\pythonw.exe\" \"!CARPETA_DESTINO!\main.py\"" >nul 2>&1
    if errorlevel 1 (
        echo  No se pudo actualizar la tarea automaticamente.
        echo  Actualizala manualmente en el Programador de tareas apuntando a:
        echo    !CARPETA_DESTINO!\pythonw.exe main.py
    ) else (
        echo  Tarea actualizada correctamente.
    )
)

echo.
:: ── Crear argos_demo.bat si no existe ───────────────────────
if not exist "!CARPETA_DESTINO!\argos_demo.bat" (
    echo  Creando argos_demo.bat...
    (
        echo @echo off
        echo cd /d "!CARPETA_DESTINO!"
        echo echo Iniciando Argos Modo Demo...
        echo pythonw.exe demo.py
        echo pause
    ) > "!CARPETA_DESTINO!\argos_demo.bat"
    echo  argos_demo.bat creado.
)

echo.
echo ============================================================
echo   Migracion completada.
echo   Nueva ruta: !CARPETA_DESTINO!
echo ============================================================
echo.
pause
