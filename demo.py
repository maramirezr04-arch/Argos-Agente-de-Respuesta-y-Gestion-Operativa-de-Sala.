"""
Argos — Modo Demo (Presentación)

Al ejecutar:
  1. Levanta la pantalla animada (demo_live.html) en el navegador
  2. Abre Google Sheets para que el público vea los datos actualizándose
  3. Abre el espacio de Jefes en Google Chat para que vean llegar los mensajes
  4. Da 10 segundos para acomodar las ventanas
  5. Corre el bot con navegador visible — el público ve el OMS abrirse,
     el CSV descargarse, los datos volcarse al Sheets y los mensajes llegar al Chat,
     todo al mismo tiempo, en tiempo real
"""

import subprocess, sys, time, json, os, threading, webbrowser, re
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

try:
    import gspread
    from google.oauth2.service_account import Credentials
    from config import GOOGLE
    _GSPREAD_OK = True
except Exception:
    _GSPREAD_OK  = False
    GOOGLE       = {"sheet_id": ""}

VERSION = "1.1.9"
RAIZ    = os.path.dirname(os.path.abspath(__file__))

# ── Extraer space IDs de los webhooks (desde el cache de CONFIG) ──
# Los webhooks viven en la hoja CONFIG del Sheets; el bot guarda una copia
# local en config_remota_cache.json. Formato: …/spaces/<SPACE_ID>/messages…
def _space_id(webhook_url):
    m = re.search(r"/spaces/([^/]+)/", str(webhook_url))
    return m.group(1) if m else None

def _leer_space_jefes():
    # 1. Cache local escrito por el bot
    try:
        with open(os.path.join(RAIZ, "config_remota_cache.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        sid = _space_id(cfg.get("webhook_jefes", ""))
        if sid:
            return sid
    except Exception:
        pass
    # 2. Directo del Sheets
    if _GSPREAD_OK:
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_file(GOOGLE["credentials"], scopes=scopes)
            gc    = gspread.authorize(creds)
            hoja  = gc.open_by_key(GOOGLE["sheet_id"]).worksheet("CONFIG")
            for row in hoja.get_all_values()[1:]:
                if row and len(row) >= 2 and row[0].strip() == "webhook_jefes":
                    return _space_id(row[1])
        except Exception:
            pass
    return None

SHEET_ID   = GOOGLE.get("sheet_id", "")
URL_SHEETS = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}" if SHEET_ID else None
_SPACE_JEFES = _leer_space_jefes()
URL_JEFES  = f"https://chat.google.com/room/{_SPACE_JEFES}" if _SPACE_JEFES else None
URL_DIAG   = None   # se asigna tras levantar el servidor

# ── Servidor local para la pantalla animada ───────────────────
class _Silent(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def _iniciar_servidor():
    handler = partial(_Silent, directory=RAIZ)
    for puerto in (8765, 8780, 8799, 8808):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", puerto), handler)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{puerto}/presentacion/demo_live.html"
    return None

# ── Helpers de consola ────────────────────────────────────────
def _sep(char="─"): print("  " + char * 56)
def _ok(msg):       print(f"  ✅ {msg}")
def _info(msg):     print(f"  ℹ️  {msg}")
def _warn(msg):     print(f"  ⚠️  {msg}")

def print_banner():
    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║   🤖  Argos — Presentación en vivo                  ║")
    print(f"  ║   v{VERSION}  ·  {datetime.now():%Y-%m-%d %H:%M:%S}               ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print()

def abrir_ventanas():
    """Abre todas las ventanas de la demostración en el navegador."""
    global URL_DIAG
    _sep()
    print("  Abriendo ventanas para la presentación…\n")

    # 1. Pantalla animada
    URL_DIAG = _iniciar_servidor()
    if URL_DIAG:
        webbrowser.open(URL_DIAG)
        _ok(f"Diagrama en vivo → {URL_DIAG}")
    else:
        _warn("No se pudo iniciar el servidor local para el diagrama")

    time.sleep(0.6)

    # 2. Google Sheets
    if URL_SHEETS:
        webbrowser.open(URL_SHEETS)
        _ok("Google Sheets → pestaña de datos en tiempo real")
    else:
        _warn("Sin sheet_id en config — Sheets no se abre automáticamente")

    time.sleep(0.6)

    # 3. Google Chat — espacio Jefes
    if URL_JEFES:
        webbrowser.open(URL_JEFES)
        _ok("Google Chat (Jefes) → aquí llegarán los mensajes")
    else:
        _warn("No se pudo detectar el space ID de Jefes — ábrelo manualmente")

    print()
    _sep()
    print()
    print("  📋  GUÍA RÁPIDA PARA LA PRESENTACIÓN")
    print()
    print("  Ventana 1 — Diagrama animado")
    print("  └─ Muestra el paso a paso de Argos en tiempo real")
    print("     Tecla [1] = arquitectura  ·  Tecla [2] = demo en vivo")
    print()
    print("  Ventana 2 — Google Sheets")
    print("  └─ Ve a la pestaña MONITOR para ver los datos llegar")
    print()
    print("  Ventana 3 — Google Chat (Jefes)")
    print("  └─ Aquí aparecerán los mensajes cuando el bot termine")
    print()
    print("  Ventana 4 — OMS Liverpool")
    print("  └─ Se abrirá sola al iniciar — el bot entra, navega")
    print("     y descarga el CSV frente al público")
    print()
    _sep()

def cuenta_regresiva(segundos=10):
    """Da tiempo al presentador para acomodar las ventanas."""
    print()
    print(f"  El bot arrancará en {segundos} segundos…")
    print("  (Acomoda las ventanas antes de que empiece)")
    print()
    for i in range(segundos, 0, -1):
        bar  = "█" * (segundos - i + 1) + "░" * (i - 1)
        sys.stdout.write(f"\r  [{bar}] {i:2d}s  — Presiona Ctrl+C para cancelar  ")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write(f"\r  {'█' * segundos}  ¡Arrancando!                              \n\n")
    sys.stdout.flush()

# ── Leer intervalo desde CONFIG ───────────────────────────────
def leer_intervalo_remoto():
    if not _GSPREAD_OK:
        return 15
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(GOOGLE["credentials"], scopes=scopes)
        gc    = gspread.authorize(creds)
        ss    = gc.open_by_key(SHEET_ID)
        hoja  = ss.worksheet("CONFIG")
        for row in hoja.get_all_values()[1:]:
            if row and len(row) >= 2 and row[0].strip() == "intervalo_demo":
                val = row[1].strip()
                if val.isdigit():
                    return max(1, int(val))
    except Exception as e:
        _warn(f"No se pudo leer intervalo_demo: {e}")
    return 15

# ── Ciclo del bot ─────────────────────────────────────────────
def ejecutar_ciclo(ciclo: int) -> bool:
    _sep()
    print(f"  ▶  Ciclo #{ciclo}  —  {datetime.now():%H:%M:%S}")
    print()
    print("  Lo que verás:")
    print("  • Ventana OMS   → navegador entra, descarga el CSV")
    print("  • Ventana Sheets → nuevas filas aparecen en MONITOR")
    print("  • Ventana Chat   → mensajes de jefes y vendedores")
    print("  • Diagrama       → cada nodo se enciende en su turno")
    print()
    _sep()

    cmd = [sys.executable, "main.py", "--demo", "--forzar"]
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
            if not line:
                continue
            # Resaltar las líneas más importantes para el presentador
            if any(k in line for k in ("✅","⚠️","🔄","📨","enviado","completado","Argos")):
                print("  ┃ " + line)
            else:
                print("  │ " + line)
        proc.wait()
        ok = proc.returncode == 0
        print()
        print(f"  {'✅  Ciclo completado — los mensajes ya llegaron al Chat' if ok else '❌  El ciclo terminó con error'}")
        return ok
    except KeyboardInterrupt:
        proc.terminate()
        raise
    except Exception as e:
        print(f"  ❌  Error: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────
def main():
    print_banner()

    # Abrir todas las ventanas
    abrir_ventanas()

    # Leer intervalo
    print("  Leyendo configuración de demo desde Google Sheets…")
    intervalo = leer_intervalo_remoto()
    print(f"  ⏱  Intervalo entre ciclos: {intervalo} minuto(s)")
    print()

    # Cuenta regresiva
    try:
        cuenta_regresiva(10)
    except KeyboardInterrupt:
        print("\n\n  Demo cancelada antes de iniciar.\n")
        return

    ciclo = 0
    try:
        while True:
            ciclo += 1
            ejecutar_ciclo(ciclo)
            print()
            print(f"  ⏳  Siguiente ciclo en {intervalo} min")
            print(f"      ({datetime.now():%H:%M:%S} + {intervalo} min)")
            intervalo = leer_intervalo_remoto()
            for rem in range(intervalo * 60, 0, -30):
                time.sleep(min(30, rem))
    except KeyboardInterrupt:
        print("\n\n  🛑  Demo detenida.\n")

if __name__ == "__main__":
    main()
