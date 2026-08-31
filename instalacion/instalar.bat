@echo off
:: Argos - Instalador v3
set "SELF=%~f0"
set "INSTALLER_DIR=%~dp0"
if "%INSTALLER_DIR:~-1%"=="\" set "INSTALLER_DIR=%INSTALLER_DIR:~0,-1%"
set "TMP_PS=%SystemRoot%\Temp\argos_setup.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=[IO.File]::ReadAllLines('%SELF%'); [IO.File]::WriteAllLines('%TMP_PS%',($a|Select-Object -Skip 11),[Text.Encoding]::UTF8)"
if not exist "%TMP_PS%" echo ERROR: Ejecuta instalar.bat como Administrador.
if not exist "%TMP_PS%" pause & exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMP_PS%" "%INSTALLER_DIR%"
del "%TMP_PS%" 2>nul & exit /b
param([string]$InstallerDir = "")
$ErrorActionPreference = "Continue"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "  ERROR: Ejecuta como Administrador" -ForegroundColor Red
    Write-Host "  (clic derecho en instalar.bat -> Ejecutar como administrador)" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Presiona Enter para salir"
    exit 1
}

$DEST   = "C:\argos"
$GITHUB = "https://raw.githubusercontent.com/maramirezr04-arch/Argos-Agente-de-Respuesta-y-Gestion-Operativa-de-Sala./main"
$utf8   = New-Object System.Text.UTF8Encoding($false)

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Argos  -  Instalador" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/8] Buscando Python..." -ForegroundColor Yellow
$PYTHONEXE = $null
$PYTHONW   = $null
$rutas = @(
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python39\python.exe",
    "C:\Python313\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe",
    "C:\Program Files\Python313\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe"
)
$cmdPy = Get-Command python.exe -ErrorAction SilentlyContinue
if ($cmdPy) { $rutas = @($cmdPy.Source) + $rutas }
foreach ($r in $rutas) {
    if ($r -and (Test-Path $r)) {
        $PYTHONEXE = $r
        $pw = $r -replace "python\.exe$","pythonw.exe"
        if (Test-Path $pw) { $PYTHONW = $pw } else { $PYTHONW = $r }
        break
    }
}
if (-not $PYTHONEXE) {
    Write-Host "  ERROR: Python no encontrado. Instala Python 3.x desde python.org" -ForegroundColor Red
    Read-Host "Presiona Enter para salir"; exit 1
}
Write-Host "  Python:  $PYTHONEXE" -ForegroundColor Green
Write-Host "  Pythonw: $PYTHONW" -ForegroundColor Green

Write-Host "[2/8] Deteniendo procesos anteriores..." -ForegroundColor Yellow
Get-Process -Name "python","pythonw" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
foreach ($tarea in @("Argos Bot","Argos Reparador","Liverpool Bot","Liverpool Reparador","LiverpoolBot","ArgosBot","Bot Liverpool")) {
    if (Get-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $tarea -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  Tarea vieja eliminada: $tarea" -ForegroundColor Gray
    }
}
Write-Host "  Listo" -ForegroundColor Green

Write-Host "[3/8] Creando carpetas..." -ForegroundColor Yellow
foreach ($d in @($DEST, "$DEST\logs", "$DEST\descargas")) {
    New-Item -ItemType Directory -Path $d -Force -ErrorAction SilentlyContinue | Out-Null
}
Write-Host "  Carpetas listas en $DEST" -ForegroundColor Green

Write-Host "[4/8] Buscando credentials.json..." -ForegroundColor Yellow
$CRED = $null
$credBusqueda = @(
    "$DEST\credentials.json",
    "$InstallerDir\credentials.json",
    "$env:USERPROFILE\Desktop\credentials.json",
    "$env:USERPROFILE\Downloads\credentials.json",
    "C:\Users\Public\Desktop\credentials.json"
)
foreach ($cc in $credBusqueda) {
    if ($cc -and (Test-Path $cc)) { $CRED = $cc; break }
}
if (-not $CRED) {
    Write-Host "  AVISO: credentials.json no encontrado" -ForegroundColor Yellow
    Write-Host "  Coloca credentials.json en $DEST o junto a instalar.bat" -ForegroundColor Yellow
} else {
    if ($CRED -ne "$DEST\credentials.json") {
        Copy-Item $CRED "$DEST\credentials.json" -Force
        Write-Host "  credentials.json copiado desde: $CRED" -ForegroundColor Green
    } else {
        Write-Host "  credentials.json ya esta en $DEST" -ForegroundColor Green
    }
}

Write-Host "[5/8] Instalando config.py..." -ForegroundColor Yellow
$b64cfg = "TElWRVJQT09MID0gewogICAgInVybF9sb2dpbiI6ICJodHRwczovL3N1cnRpZG9hcHAtb21zLmxpdmVycG9vbC5jb20ubXgvIy9sb2dpbiIsCiAgICAidXN1YXJpbyI6ICAgInZtcmFuZ2VsaiIsCiAgICAicGFzc3dvcmQiOiAgIkxpdmVycG9vbDEiLAp9CgpHT09HTEUgPSB7CiAgICAic2hlZXRfaWQiOiAgICAgICAiMTM1bHN5bW01QTY3X2llWVpMYUtJZnZQcGt5cVJXYlVmOVVWLW12M2I3anMiLAogICAgIm5vbWJyZV9ob2phIjogICAgIkhvamEgMSIsCiAgICAiY3JlZGVudGlhbHMiOiAgICByIkM6XGFyZ29zXGNyZWRlbnRpYWxzLmpzb24iLAogICAgInNoZWV0Ml9pZCI6ICAgICAgIjFiYVZlV0NBNy1maFRIVlVuUGdkMV82N2tqSlFKY2h3d3ZUNjRLNXQwT0pzIiwKICAgICJzaGVldDJfaG9qYSI6ICAgICJIb2phIDEiLAogICAgInNoZWV0Ml9jb2wiOiAgICAgIlYiLAogICAgInNoZWV0Ml9maWxhIjogICAgMiwKICAgICJ0aW1lc3RhbXBfY29sIjogICJBUyIsCiAgICAidGltZXN0YW1wX2ZpbGEiOiAyLAp9CgpDSEFUID0gewogICAgIndlYmhvb2tfdXJsIjogICAgIiIsCiAgICAibG9va2VyX3VybCI6ICAgICAiaHR0cHM6Ly9sb29rZXJzdHVkaW8uZ29vZ2xlLmNvbS91LzAvcmVwb3J0aW5nLzI1YjkyYmI4LWU1ZTgtNDBhNi1iYzM2LWUzOWI2MmZmOThkZi9wYWdlL0tuNW1EIiwKICAgICJub21icmVfcmVwb3J0ZSI6ICJJbmRpY2Fkb3JlcyBMaXZlcnBvb2wgNDU2IiwKfQoKWEQgPSB7CiAgICAidXJsX2xvZ2luIjogICAgImh0dHBzOi8vZnJvbnQtb21zLXhkLmxpdmVycG9vbC5jb20ubXgvIy9sb2dpbiIsCiAgICAidXN1YXJpbyI6ICAgICAgIm1hcmFtaXJlenIwNCIsCiAgICAicGFzc3dvcmQiOiAgICAgIkxpdmVycG9vbDEuIiwKfQoKQ0FSUEVUQV9ERVNDQVJHQSA9IHIiQzpcYXJnb3NcZGVzY2FyZ2FzIgoKUENfTk9NQlJFID0gInByaW5jaXBhbCIKCiMgVG9rZW4gcGFyYSBkZXNjYXJnYXIgYWN0dWFsaXphY2lvbmVzIGRlbCByZXBvIHByaXZhZG8gKGxvIHBvbmUgZWwgaW5zdGFsYWRvcikKR0lUSFVCX1RPS0VOID0gIiIK"
$cfgBytes = [System.Convert]::FromBase64String($b64cfg)
$cfgText  = [System.Text.Encoding]::UTF8.GetString($cfgBytes)

$pcActual = "principal"
if (Test-Path "$DEST\config.py") {
    $oldCfg = [System.IO.File]::ReadAllText("$DEST\config.py")
    if ($oldCfg -match 'PC_NOMBRE\s*=\s*"([^"]+)"') { $pcActual = $Matches[1] }
}
Write-Host ""
Write-Host "  Nombre de esta computadora (para identificarla en Google Sheets):" -ForegroundColor Cyan
Write-Host "  Ejemplos: principal, tienda456, caja1, bodega" -ForegroundColor Gray
Write-Host "  Valor actual: $pcActual" -ForegroundColor Gray
$pcNombre = Read-Host "  Nombre (Enter para usar '$pcActual')"
if ([string]::IsNullOrWhiteSpace($pcNombre)) { $pcNombre = $pcActual }
$cfgText = $cfgText -replace 'PC_NOMBRE\s*=\s*"[^"]*"', "PC_NOMBRE = `"$pcNombre`""
Write-Host ""

$tokenActual = ""
if (Test-Path "$DEST\config.py") {
    if ($oldCfg -match 'GITHUB_TOKEN\s*=\s*"([^"]*)"') { $tokenActual = $Matches[1] }
}
Write-Host "  Token de GitHub (para descargar actualizaciones del repo privado):" -ForegroundColor Cyan
Write-Host "  Se obtiene en github.com -> Settings -> Developer settings ->" -ForegroundColor Gray
Write-Host "  Personal access tokens. Permiso de solo lectura (Contents: Read)." -ForegroundColor Gray
if ($tokenActual) { Write-Host "  Ya hay un token guardado (Enter para conservarlo)." -ForegroundColor Gray }
$tokenInput = Read-Host "  Token (Enter para conservar el actual)"
if ([string]::IsNullOrWhiteSpace($tokenInput)) { $tokenInput = $tokenActual }
$GHTOKEN = $tokenInput
$cfgText = $cfgText -replace 'GITHUB_TOKEN\s*=\s*"[^"]*"', "GITHUB_TOKEN = `"$tokenInput`""
Write-Host ""

[System.IO.File]::WriteAllText("$DEST\config.py", $cfgText, $utf8)
if ($GHTOKEN) {
    Write-Host "  config.py instalado (PC: $pcNombre, token configurado)" -ForegroundColor Green
} else {
    Write-Host "  config.py instalado (PC: $pcNombre, SIN token - solo repo publico)" -ForegroundColor Yellow
}

Write-Host "[6/8] Descargando archivos de GitHub..." -ForegroundColor Yellow
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
# El punto al final del repo causa 404 en la API por content-negotiation de Rails.
# Solución: codificar el punto como %2E en la URL de la API.
$API = "https://api.github.com/repos/maramirezr04-arch/Argos-Agente-de-Respuesta-y-Gestion-Operativa-de-Sala%2E/contents"
foreach ($arch in @("main.py","utils.py","descarga.py","kpi.py","sheets.py","mensajes.py","reparador.py","requirements.txt","version.txt","demo.py","actualizar_directorio.py")) {
    try {
        if ($GHTOKEN) {
            $hdr = @{ Authorization = "token $GHTOKEN"; Accept = "application/vnd.github.raw"; "User-Agent" = "argos-installer" }
            Invoke-WebRequest -Uri "$API/$arch`?ref=main" -Headers $hdr -OutFile "$DEST\$arch" -UseBasicParsing -ErrorAction Stop
        } else {
            Invoke-WebRequest -Uri "$GITHUB/$arch" -OutFile "$DEST\$arch" -UseBasicParsing -ErrorAction Stop
        }
        Write-Host "  $arch OK" -ForegroundColor Green
    } catch {
        Write-Host "  ERROR: $arch no se pudo descargar - $_" -ForegroundColor Red
    }
}

Write-Host "[7/8] Creando scripts de arranque..." -ForegroundColor Yellow
$vbsBot = 'CreateObject("WScript.Shell").Run Chr(34) & "' + $PYTHONW + '" & Chr(34) & " " & Chr(34) & "' + $DEST + '\main.py" & Chr(34), 0, False'
[System.IO.File]::WriteAllText("$DEST\ejecutar.vbs", $vbsBot, $utf8)
$vbsRep = 'CreateObject("WScript.Shell").Run Chr(34) & "' + $PYTHONW + '" & Chr(34) & " " & Chr(34) & "' + $DEST + '\reparador.py" & Chr(34), 0, False'
[System.IO.File]::WriteAllText("$DEST\ejecutar_reparar.vbs", $vbsRep, $utf8)
Write-Host "  ejecutar.vbs creado" -ForegroundColor Green
Write-Host "  ejecutar_reparar.vbs creado" -ForegroundColor Green

$batAhora = "@echo off`r`necho Iniciando Argos Bot...`r`nwscript.exe `"$DEST\ejecutar.vbs`"`r`necho Listo. Revisa $DEST\logs para el estado.`r`npause"
[System.IO.File]::WriteAllText("$DEST\INICIAR AHORA.bat", $batAhora, $utf8)
Write-Host "  INICIAR AHORA.bat creado" -ForegroundColor Green

$batDemo = "@echo off`r`ncd /d `"$DEST`"`r`n`"$PYTHONEXE`" demo.py`r`npause"
[System.IO.File]::WriteAllText("$DEST\EJECUTAR DEMO.bat", $batDemo, $utf8)
Write-Host "  EJECUTAR DEMO.bat creado" -ForegroundColor Green

Write-Host "[8/8] Instalando dependencias y tareas programadas..." -ForegroundColor Yellow
Write-Host "  Instalando paquetes Python (puede tardar)..." -ForegroundColor Gray
try {
    & $PYTHONEXE -m pip install -r "$DEST\requirements.txt" --quiet 2>&1 | Out-Null
    Write-Host "  Paquetes instalados" -ForegroundColor Green
} catch {
    Write-Host "  ADVERTENCIA pip: $_" -ForegroundColor Yellow
}

Write-Host "  Instalando Playwright Chromium (puede tardar)..." -ForegroundColor Gray
try {
    & $PYTHONEXE -m playwright install chromium 2>&1 | Out-Null
    Write-Host "  Playwright instalado" -ForegroundColor Green
} catch {
    Write-Host "  ADVERTENCIA Playwright: $_" -ForegroundColor Yellow
}

$b64bot = "PFRhc2sgdmVyc2lvbj0iMS4yIiB4bWxucz0iaHR0cDovL3NjaGVtYXMubWljcm9zb2Z0LmNvbS93aW5kb3dzLzIwMDQvMDIvbWl0L3Rhc2siPjxUcmlnZ2Vycz48Q2FsZW5kYXJUcmlnZ2VyPjxSZXBldGl0aW9uPjxJbnRlcnZhbD5QVDE1TTwvSW50ZXJ2YWw+PER1cmF0aW9uPlBUMTFIMzBNPC9EdXJhdGlvbj48U3RvcEF0RHVyYXRpb25FbmQ+ZmFsc2U8L1N0b3BBdER1cmF0aW9uRW5kPjwvUmVwZXRpdGlvbj48U3RhcnRCb3VuZGFyeT4yMDI2LTAxLTAxVDEwOjAwOjAwPC9TdGFydEJvdW5kYXJ5PjxFbmFibGVkPnRydWU8L0VuYWJsZWQ+PFNjaGVkdWxlQnlEYXk+PERheXNJbnRlcnZhbD4xPC9EYXlzSW50ZXJ2YWw+PC9TY2hlZHVsZUJ5RGF5PjwvQ2FsZW5kYXJUcmlnZ2VyPjwvVHJpZ2dlcnM+PFByaW5jaXBhbHM+PFByaW5jaXBhbCBpZD0iQXV0aG9yIj48TG9nb25UeXBlPkludGVyYWN0aXZlVG9rZW48L0xvZ29uVHlwZT48UnVuTGV2ZWw+SGlnaGVzdEF2YWlsYWJsZTwvUnVuTGV2ZWw+PC9QcmluY2lwYWw+PC9QcmluY2lwYWxzPjxTZXR0aW5ncz48U3RhcnRXaGVuQXZhaWxhYmxlPnRydWU8L1N0YXJ0V2hlbkF2YWlsYWJsZT48RXhlY3V0aW9uVGltZUxpbWl0PlBUMTRNPC9FeGVjdXRpb25UaW1lTGltaXQ+PE11bHRpcGxlSW5zdGFuY2VzUG9saWN5Pklnbm9yZU5ldzwvTXVsdGlwbGVJbnN0YW5jZXNQb2xpY3k+PERpc2FsbG93U3RhcnRJZk9uQmF0dGVyaWVzPmZhbHNlPC9EaXNhbGxvd1N0YXJ0SWZPbkJhdHRlcmllcz48U3RvcElmR29pbmdPbkJhdHRlcmllcz5mYWxzZTwvU3RvcElmR29pbmdPbkJhdHRlcmllcz48L1NldHRpbmdzPjxBY3Rpb25zIENvbnRleHQ9IkF1dGhvciI+PEV4ZWM+PENvbW1hbmQ+d3NjcmlwdC5leGU8L0NvbW1hbmQ+PEFyZ3VtZW50cz4iQzpcYXJnb3NcZWplY3V0YXIudmJzIjwvQXJndW1lbnRzPjwvRXhlYz48L0FjdGlvbnM+PC9UYXNrPg=="
$botXml = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64bot))
try {
    Register-ScheduledTask -Xml $botXml -TaskName "Argos Bot" -Force | Out-Null
    Write-Host "  Tarea Argos Bot creada (cada 15 min)" -ForegroundColor Green
} catch {
    Write-Host "  ERROR tarea Argos Bot: $_" -ForegroundColor Red
}

$b64rep = "PFRhc2sgdmVyc2lvbj0iMS4yIiB4bWxucz0iaHR0cDovL3NjaGVtYXMubWljcm9zb2Z0LmNvbS93aW5kb3dzLzIwMDQvMDIvbWl0L3Rhc2siPjxUcmlnZ2Vycz48Q2FsZW5kYXJUcmlnZ2VyPjxSZXBldGl0aW9uPjxJbnRlcnZhbD5QVDE1TTwvSW50ZXJ2YWw+PER1cmF0aW9uPlBUMTFIMzBNPC9EdXJhdGlvbj48U3RvcEF0RHVyYXRpb25FbmQ+ZmFsc2U8L1N0b3BBdER1cmF0aW9uRW5kPjwvUmVwZXRpdGlvbj48U3RhcnRCb3VuZGFyeT4yMDI2LTAxLTAxVDEwOjAwOjAwPC9TdGFydEJvdW5kYXJ5PjxFbmFibGVkPnRydWU8L0VuYWJsZWQ+PFNjaGVkdWxlQnlEYXk+PERheXNJbnRlcnZhbD4xPC9EYXlzSW50ZXJ2YWw+PC9TY2hlZHVsZUJ5RGF5PjwvQ2FsZW5kYXJUcmlnZ2VyPjwvVHJpZ2dlcnM+PFByaW5jaXBhbHM+PFByaW5jaXBhbCBpZD0iQXV0aG9yIj48TG9nb25UeXBlPkludGVyYWN0aXZlVG9rZW48L0xvZ29uVHlwZT48UnVuTGV2ZWw+SGlnaGVzdEF2YWlsYWJsZTwvUnVuTGV2ZWw+PC9QcmluY2lwYWw+PC9QcmluY2lwYWxzPjxTZXR0aW5ncz48U3RhcnRXaGVuQXZhaWxhYmxlPnRydWU8L1N0YXJ0V2hlbkF2YWlsYWJsZT48RXhlY3V0aW9uVGltZUxpbWl0PlBUMTRNPC9FeGVjdXRpb25UaW1lTGltaXQ+PE11bHRpcGxlSW5zdGFuY2VzUG9saWN5Pklnbm9yZU5ldzwvTXVsdGlwbGVJbnN0YW5jZXNQb2xpY3k+PERpc2FsbG93U3RhcnRJZk9uQmF0dGVyaWVzPmZhbHNlPC9EaXNhbGxvd1N0YXJ0SWZPbkJhdHRlcmllcz48U3RvcElmR29pbmdPbkJhdHRlcmllcz5mYWxzZTwvU3RvcElmR29pbmdPbkJhdHRlcmllcz48L1NldHRpbmdzPjxBY3Rpb25zIENvbnRleHQ9IkF1dGhvciI+PEV4ZWM+PENvbW1hbmQ+d3NjcmlwdC5leGU8L0NvbW1hbmQ+PEFyZ3VtZW50cz4iQzpcYXJnb3NcZWplY3V0YXJfcmVwYXJhci52YnMiPC9Bcmd1bWVudHM+PC9FeGVjPjwvQWN0aW9ucz48L1Rhc2s+"
$repXml = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64rep))
try {
    Register-ScheduledTask -Xml $repXml -TaskName "Argos Reparador" -Force | Out-Null
    Write-Host "  Tarea Argos Reparador creada" -ForegroundColor Green
} catch {
    Write-Host "  ERROR tarea Argos Reparador: $_" -ForegroundColor Red
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "   INSTALACION COMPLETA" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Carpeta: $DEST" -ForegroundColor White
Write-Host "  Python:  $PYTHONEXE" -ForegroundColor White
if ($CRED) { Write-Host "  Cred:    $DEST\credentials.json" -ForegroundColor White }
Write-Host ""
Write-Host "  El bot corre automaticamente cada 15 min" -ForegroundColor Cyan
Write-Host "  (Administrador de tareas: Argos Bot / Argos Reparador)" -ForegroundColor Cyan
Write-Host ""
Write-Host "Iniciando el bot ahora..." -ForegroundColor Yellow
try {
    Start-ScheduledTask -TaskName "Argos Bot"
    Write-Host "  Bot iniciado." -ForegroundColor Green
} catch {
    Write-Host "  No se pudo iniciar automaticamente." -ForegroundColor Yellow
}

Write-Host ""
Read-Host "Presiona Enter para cerrar"
