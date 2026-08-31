@echo off
:: Argos - Desinstalador
set "SELF=%~f0"
set "INSTALLER_DIR=%~dp0"
if "%INSTALLER_DIR:~-1%"=="\" set "INSTALLER_DIR=%INSTALLER_DIR:~0,-1%"
set "TMP_PS=%SystemRoot%\Temp\argos_desinst.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=[IO.File]::ReadAllLines('%SELF%'); [IO.File]::WriteAllLines('%TMP_PS%',($a|Select-Object -Skip 11),[Text.Encoding]::UTF8)"
if not exist "%TMP_PS%" echo ERROR: Ejecuta como Administrador.
if not exist "%TMP_PS%" pause & exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMP_PS%"
del "%TMP_PS%" 2>nul & exit /b
$ErrorActionPreference = "Continue"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "  ERROR: Ejecuta como Administrador" -ForegroundColor Red
    Write-Host "  (clic derecho en desinstalar.bat -> Ejecutar como administrador)" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Presiona Enter para salir"
    exit 1
}

$DEST = "C:\argos"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Argos  -  Desinstalador" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/3] Deteniendo procesos..." -ForegroundColor Yellow
Get-Process -Name "python","pythonw" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Write-Host "  Listo" -ForegroundColor Green

Write-Host "[2/3] Eliminando tareas programadas..." -ForegroundColor Yellow
foreach ($tarea in @("Argos Bot","Argos Reparador")) {
    if (Get-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $tarea -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  Tarea eliminada: $tarea" -ForegroundColor Green
    } else {
        Write-Host "  No encontrada: $tarea" -ForegroundColor Gray
    }
}

Write-Host "[3/3] Carpeta de instalacion..." -ForegroundColor Yellow
Write-Host ""
$resp = Read-Host "  Borrar $DEST y todos sus archivos? (S/N)"
if ($resp -match "^[Ss]") {
    if (Test-Path $DEST) {
        Remove-Item -Path $DEST -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  Carpeta $DEST eliminada" -ForegroundColor Green
    } else {
        Write-Host "  La carpeta $DEST no existe" -ForegroundColor Gray
    }
} else {
    Write-Host "  Carpeta conservada (los archivos siguen en $DEST)" -ForegroundColor Gray
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "   DESINSTALACION COMPLETA" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Read-Host "Presiona Enter para cerrar"
