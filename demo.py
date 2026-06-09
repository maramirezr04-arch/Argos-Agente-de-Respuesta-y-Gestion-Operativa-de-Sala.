"""
Argos — Modo Demo
Ejecuta el bot completo con navegador visible y mensajes redirigidos a webhooks de demo.
Levanta una pantalla web (presentacion/demo_live.html) que se ilumina en tiempo real
mostrando cada paso que hace Argos, ideal para una demostración frente a público.
Leer intervalo desde CONFIG remota; iterar indefinidamente.
"""

import subprocess, sys, time, json, os, threading, webbrowser
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

try:
    import gspread
    from google.oauth2.service_account import Credentials
    from config import GOOGLE
    _GSPREAD_OK = True
except Exception:
    _GSPREAD_OK = False

VERSION = "1.1.9"
RAIZ    = os.path.dirname(os.path.abspath(__file__))

def print_banner():
    print("=" * 60)
    print(f"  🎯 Argos — Modo Demo  v{VERSION}")
    print(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

# ── Servidor local para la pantalla de demostración ───────────
class _SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass  # no ensuciar la consola con cada request

def iniciar_pantalla_demo():
    """Levanta un servidor local y abre demo_live.html en el navegador.
    Devuelve la URL, o None si no se pudo."""
    handler = partial(_SilentHandler, directory=RAIZ)
    for puerto in (8765, 8780, 8799, 8808):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", puerto), handler)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{puerto}/presentacion/demo_live.html"
        print(f"  🖥️  Pantalla de demostración: {url}")
        try:
            webbrowser.open(url)
        except Exception:
            print("     (Ábrela manualmente en el navegador)")
        return url
    print("  ⚠️  No se pudo iniciar la pantalla de demostración (puertos ocupados)")
    return None

def leer_intervalo_remoto():
    """Lee intervalo_demo desde la hoja CONFIG del Sheets. Devuelve int (minutos)."""
    if not _GSPREAD_OK:
        return 15
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(GOOGLE["credentials"], scopes=scopes)
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(GOOGLE["sheet_id"])
        hoja = ss.worksheet("CONFIG")
        rows = hoja.get_all_values()
        for row in rows[1:]:
            if row and len(row) >= 2 and row[0].strip() == "intervalo_demo":
                val = row[1].strip()
                if val.isdigit():
                    return max(1, int(val))
    except Exception as e:
        print(f"  ⚠️  No se pudo leer intervalo_demo del Sheets: {e}")
    return 15

def ejecutar_ciclo(ciclo: int) -> bool:
    """Lanza main.py --demo --forzar y muestra output en tiempo real."""
    print(f"\n{'─'*60}")
    print(f"  ▶  Ciclo #{ciclo}  —  {datetime.now():%H:%M:%S}")
    print(f"{'─'*60}")
    print("  🌐 Abriendo navegador visible (OMS)...")
    cmd = [sys.executable, "main.py", "--demo", "--forzar"]
    # Pasar el número de ciclo al bot para que lo muestre la pantalla de demo
    env = dict(os.environ, ARGOS_DEMO_CICLO=str(ciclo))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=RAIZ,
            env=env,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print("  │ " + line)
        proc.wait()
        ok = proc.returncode == 0
        print(f"\n  {'✅ Ciclo completado' if ok else '❌ Ciclo terminó con error'}")
        return ok
    except KeyboardInterrupt:
        proc.terminate()
        raise
    except Exception as e:
        print(f"  ❌ Error ejecutando ciclo: {e}")
        return False

def main():
    print_banner()
    print("\n  Abriendo la pantalla de demostración...")
    iniciar_pantalla_demo()
    print("\n  Leyendo configuración de demo desde Google Sheets...")
    intervalo = leer_intervalo_remoto()
    print(f"  ⏱  Intervalo entre ciclos: {intervalo} minuto(s)\n")
    print("  Presiona Ctrl+C para detener la demo en cualquier momento.\n")

    ciclo = 0
    try:
        while True:
            ciclo += 1
            ejecutar_ciclo(ciclo)
            print(f"\n  ⏳ Esperando {intervalo} min hasta el próximo ciclo...")
            print(f"     Próxima ejecución: {datetime.now():%H:%M:%S} + {intervalo} min")
            # Releer intervalo en cada ciclo por si se cambió desde el dashboard
            intervalo = leer_intervalo_remoto()
            for remaining in range(intervalo * 60, 0, -30):
                time.sleep(min(30, remaining))
    except KeyboardInterrupt:
        print("\n\n  🛑 Demo detenida por el usuario.")
        print("  Hasta luego.\n")

if __name__ == "__main__":
    main()
