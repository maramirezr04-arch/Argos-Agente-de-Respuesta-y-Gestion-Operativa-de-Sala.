#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║          INSTALADOR — LIVERPOOL BOT                      ║
║                                                          ║
║  Uso:  instalar.bat  (o  python instalar.py)             ║
║                                                          ║
║  Requiere:  credentials.json  en la misma carpeta       ║
╚══════════════════════════════════════════════════════════╝

Crea TODO en C:\\liverpool-automation:
  • Todos los archivos Python del bot
  • config.py con credenciales
  • Scripts de ejecución (.bat y .vbs invisibles)
  • Instala dependencias pip + Playwright Chromium
  • Crea las tareas programadas de Windows
"""

import os
import sys
import subprocess
import shutil
import json

# ── Configuración ────────────────────────────────────────────────────────────
DESTINO  = r"C:\liverpool-automation"
PYTHON   = sys.executable

# Archivos Python a copiar desde la misma carpeta que instalar.py
ARCHIVOS_BOT = [
    "main.py",
    "actualizar_directorio.py",
    "reparar_bot.py",
]

# config.py con las credenciales del bot (formato que usa main.py)
CONFIG_PY = r'''LIVERPOOL = {
    "url_login": "https://surtidoapp-oms.liverpool.com.mx/#/login",
    "usuario":   "vmrangelj",
    "password":  "Liverpool1",
}

GOOGLE = {
    "sheet_id":       "135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js",
    "nombre_hoja":    "Hoja 1",
    "credentials":    r"C:\liverpool-automation\credentials.json",
    "sheet2_id":      "1baVeWCA7-fhTHVUnPgd1_67kjJQJchwwvT64K5t0OJs",
    "sheet2_hoja":    "Hoja 1",
    "sheet2_col":     "V",
    "sheet2_fila":    2,
    "timestamp_col":  "AS",
    "timestamp_fila": 2,
}

CHAT = {
    "webhook_url":    "https://chat.googleapis.com/v1/spaces/AAQAQ6DrmfI/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=VzOYmkn9w65FPf64JLq1ySI0VFyD5E8sdc-KQc29nXw",
    "looker_url":     "https://lookerstudio.google.com/u/0/reporting/25b92bb8-e5e8-40a6-bc36-e39b62ff98df/page/Kn5mD",
    "nombre_reporte": "Indicadores Liverpool 456",
}

CARPETA_DESCARGA = r"C:\liverpool-automation\descargas"
'''

# ── Helpers ───────────────────────────────────────────────────────────────────

def banner(texto):
    print(f"\n{'─'*60}")
    print(f"  {texto}")
    print(f"{'─'*60}")


def paso(n, total, desc):
    print(f"\n[{n}/{total}] {desc}...")


def ok(msg):
    print(f"  ✔  {msg}")


def err(msg):
    print(f"  ✘  {msg}")


def run(cmd, check=True, silencioso=False):
    result = subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not silencioso and result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            print(f"     {line}")
    if not silencioso and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            print(f"     {line}")
    if check and result.returncode != 0:
        raise RuntimeError(f"Falló: {cmd}\n{result.stderr}")
    return result


def escribir(path, contenido, encoding="utf-8"):
    with open(path, "w", encoding=encoding) as f:
        f.write(contenido)
    ok(os.path.basename(path))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    banner("INSTALADOR LIVERPOOL BOT")
    print(f"  Destino : {DESTINO}")
    print(f"  Python  : {PYTHON}")
    print(f"  Versión : {sys.version.split()[0]}")

    # ── Verificar Python 3.8+ ────────────────────────────────────────────────
    if sys.version_info < (3, 8):
        err("Se requiere Python 3.8 o superior.")
        sys.exit(1)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # ── Verificar credentials.json ───────────────────────────────────────────
    creds_src = os.path.join(script_dir, "credentials.json")
    if not os.path.exists(creds_src):
        err("No se encontró 'credentials.json' en la misma carpeta que instalar.py")
        print("     Coloca credentials.json junto a instalar.py y vuelve a ejecutar.")
        input("\nPresiona Enter para salir...")
        sys.exit(1)

    try:
        with open(creds_src) as f:
            creds_data = json.load(f)
        if "private_key" not in creds_data:
            err("credentials.json no parece ser una cuenta de servicio de Google válida.")
            sys.exit(1)
    except json.JSONDecodeError:
        err("credentials.json no es un JSON válido.")
        sys.exit(1)

    TOTAL = 9

    # ── PASO 1: Carpetas ──────────────────────────────────────────────────────
    paso(1, TOTAL, "Creando estructura de carpetas")
    for d in [DESTINO,
              os.path.join(DESTINO, "descargas"),
              os.path.join(DESTINO, "logs"),
              os.path.join(DESTINO, "screenshots")]:
        os.makedirs(d, exist_ok=True)
        ok(d)

    # ── PASO 2: Copiar archivos Python ────────────────────────────────────────
    paso(2, TOTAL, "Copiando archivos del bot")
    faltantes_src = []
    for archivo in ARCHIVOS_BOT:
        src = os.path.join(script_dir, archivo)
        dst = os.path.join(DESTINO, archivo)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            ok(archivo)
        else:
            err(f"{archivo} — no encontrado junto a instalar.py")
            faltantes_src.append(archivo)

    if faltantes_src:
        print(f"\n  ⚠  Archivos faltantes: {', '.join(faltantes_src)}")
        print("     Asegúrate de que todos los archivos .py estén en la misma carpeta que instalar.py")
        input("\nPresiona Enter para salir...")
        sys.exit(1)

    # ── PASO 3: Crear config.py ───────────────────────────────────────────────
    paso(3, TOTAL, "Creando config.py con credenciales")
    escribir(os.path.join(DESTINO, "config.py"), CONFIG_PY)

    # ── PASO 4: Copiar credentials.json ──────────────────────────────────────
    paso(4, TOTAL, "Copiando credentials.json")
    shutil.copy2(creds_src, os.path.join(DESTINO, "credentials.json"))
    ok("credentials.json")

    # ── PASO 5: Instalar dependencias pip ─────────────────────────────────────
    paso(5, TOTAL, "Instalando dependencias Python")
    deps = ["gspread>=6.0", "google-auth>=2.0", "requests>=2.28", "playwright>=1.40"]
    for dep in deps:
        try:
            run(f'"{PYTHON}" -m pip install "{dep}" --quiet --disable-pip-version-check',
                silencioso=True)
            ok(dep.split(">=")[0])
        except Exception as e:
            err(f"{dep}: {e}")

    # ── PASO 6: Instalar Playwright Chromium ──────────────────────────────────
    paso(6, TOTAL, "Instalando Playwright Chromium (puede tardar ~5 min)")
    try:
        run(f'"{PYTHON}" -m playwright install chromium')
        ok("Chromium instalado")
    except Exception as e:
        err(f"Playwright: {e}")
        print("     Intenta manualmente: python -m playwright install chromium")

    # ── PASO 7: Crear scripts de ejecución ────────────────────────────────────
    paso(7, TOTAL, "Creando scripts de ejecución")

    bat_principal   = os.path.join(DESTINO, "ejecutar.bat")
    bat_reparar     = os.path.join(DESTINO, "ejecutar_reparar.bat")
    bat_ahora       = os.path.join(DESTINO, "correr_ahora.bat")
    bat_desinstalar = os.path.join(DESTINO, "desinstalar.bat")
    vbs_principal   = os.path.join(DESTINO, "ejecutar.vbs")
    vbs_reparar     = os.path.join(DESTINO, "ejecutar_reparar.vbs")

    escribir(bat_principal, (
        f'@echo off\r\n'
        f'cd /d "{DESTINO}"\r\n'
        f'"{PYTHON}" main.py\r\n'
    ))

    escribir(bat_reparar, (
        f'@echo off\r\n'
        f'cd /d "{DESTINO}"\r\n'
        f'"{PYTHON}" reparar_bot.py\r\n'
    ))

    escribir(bat_ahora, (
        f'@echo off\r\n'
        f'title Liverpool Bot\r\n'
        f'cd /d "{DESTINO}"\r\n'
        f'"{PYTHON}" main.py --forzar\r\n'
        f'pause\r\n'
    ))

    escribir(bat_desinstalar, (
        f'@echo off\r\n'
        f'echo Eliminando tareas programadas...\r\n'
        f'schtasks /delete /tn "Liverpool Bot" /f\r\n'
        f'schtasks /delete /tn "Liverpool Bot Reparador" /f\r\n'
        f'echo Listo.\r\n'
        f'pause\r\n'
    ))

    escribir(vbs_principal, (
        f'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run Chr(34) & "{bat_principal}" & Chr(34), 0, False\r\n'
        f'Set WshShell = Nothing\r\n'
    ))

    escribir(vbs_reparar, (
        f'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run Chr(34) & "{bat_reparar}" & Chr(34), 0, False\r\n'
        f'Set WshShell = Nothing\r\n'
    ))

    # ── PASO 8: Tareas programadas de Windows ─────────────────────────────────
    paso(8, TOTAL, "Creando tareas programadas de Windows")

    tareas = [
        ("Liverpool Bot",         f'wscript "{vbs_principal}"', "MINUTE", "15", "09:45", "21:45"),
        ("Liverpool Bot Reparador", f'wscript "{vbs_reparar}"', "MINUTE", "30", "10:00", "22:00"),
    ]

    for tn, tr, sc, mo, st, et in tareas:
        cmd = (
            f'schtasks /create'
            f' /tn "{tn}"'
            f' /tr "{tr}"'
            f' /sc {sc} /mo {mo}'
            f' /st {st} /et {et}'
            f' /f'
        )
        try:
            run(cmd, check=True, silencioso=True)
            ok(f'"{tn}" — cada {mo} {sc.lower()} de {st} a {et}')
        except Exception as e:
            err(f'"{tn}": {e}')
            print(f"     Crea la tarea manualmente: Programador de tareas → Programa: {tr}")

    # ── PASO 9: Verificación final ─────────────────────────────────────────────
    paso(9, TOTAL, "Verificación final")
    requeridos = [
        "main.py", "actualizar_directorio.py", "reparar_bot.py",
        "config.py", "credentials.json",
        "ejecutar.bat", "ejecutar_reparar.bat",
        "ejecutar.vbs", "ejecutar_reparar.vbs",
        "correr_ahora.bat",
    ]
    faltantes = [a for a in requeridos if not os.path.exists(os.path.join(DESTINO, a))]
    for a in requeridos:
        if os.path.exists(os.path.join(DESTINO, a)):
            ok(a)
        else:
            err(f"{a} — FALTANTE")

    print()
    if faltantes:
        banner("⚠  INSTALACIÓN INCOMPLETA")
        print(f"  Archivos faltantes: {', '.join(faltantes)}")
        print("  Revisa los errores arriba y vuelve a ejecutar instalar.bat")
    else:
        banner("✅  INSTALACIÓN COMPLETADA")
        print(f"  Bot instalado en: {DESTINO}")
        print()
        print("  Comandos útiles:")
        print(f"    Ejecutar ahora (manual):  correr_ahora.bat")
        print(f"    Verificar conexiones:     python main.py test")
        print(f"    Ver tarea:                schtasks /query /tn \"Liverpool Bot\" /fo LIST")
        print(f"    Detener si cuelga:        taskkill /f /im python.exe")
        print(f"    Desinstalar tareas:       desinstalar.bat")
        print()
        print("  El bot comenzará en el próximo ciclo de 15 minutos.")

    input("\nPresiona Enter para cerrar...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInstalación cancelada.")
    except Exception as e:
        print(f"\n❌ Error inesperado: {e}")
        import traceback
        traceback.print_exc()
        input("\nPresiona Enter para cerrar...")
        sys.exit(1)
