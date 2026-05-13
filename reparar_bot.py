"""
reparar_bot.py — Monitor y reparador automatico del Liverpool Bot
Corre cada 15 minutos junto con el bot principal.
Detecta errores en el log, analiza la causa, reintenta y manda
el analisis al espacio "reporte" de Google Chat.
"""
import os, re, time, json, requests, subprocess, sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────
CARPETA        = "C:\\liverpool-automation"
LOG_DIR        = os.path.join(CARPETA, "logs")
PYTHON_PATH    = sys.executable
MAIN_PY        = os.path.join(CARPETA, "main.py")
ESTADO_FILE    = os.path.join(CARPETA, "reparar_estado.json")
MAX_REINTENTO  = 2   # max reintentos consecutivos antes de rendirse

WEBHOOK = "https://chat.googleapis.com/v1/spaces/AAQAQ6DrmfI/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=VzOYmkn9w65FPf64JLq1ySI0VFyD5E8sdc-KQc29nXw"

# ── Catalogo de errores conocidos ─────────────────────────────
ERRORES_CONOCIDOS = [
    {
        "patron":    r"TimeoutError|180000ms|timeout",
        "causa":     "El servidor de Liverpool tardo demasiado en generar el archivo de descarga",
        "solucion":  "El bot reintentara la descarga con los 3 navegadores en paralelo. Si persiste, el servidor de Liverpool puede estar saturado — espera 15 min.",
        "accion":    "reintentar",
    },
    {
        "patron":    r"net::ERR_|NS_ERROR_|ECONNREFUSED|connection refused",
        "causa":     "No hay conexion a internet o el servidor de Liverpool no responde",
        "solucion":  "Verifica la conexion a internet de la PC. El bot reintentara en 15 minutos automaticamente.",
        "accion":    "esperar",
    },
    {
        "patron":    r"INVALID_ARGUMENT|spreadsheetId|gspread",
        "causa":     "Error al conectar con Google Sheets — puede ser problema de credenciales o permisos",
        "solucion":  "Verifica que el archivo credentials.json este en C:\\liverpool-automation\\ y que la cuenta de servicio tenga acceso a los Sheets.",
        "accion":    "notificar",
    },
    {
        "patron":    r"playwright|chromium|browser",
        "causa":     "Error con el navegador Chromium — puede necesitar reinstalacion",
        "solucion":  "Abre CMD en C:\\liverpool-automation\\ y ejecuta: playwright install chromium",
        "accion":    "notificar",
    },
    {
        "patron":    r"FileNotFoundError|No such file|credentials",
        "causa":     "Archivo faltante — probablemente credentials.json no esta en la carpeta",
        "solucion":  "Copia el archivo credentials.json a C:\\liverpool-automation\\",
        "accion":    "notificar",
    },
    {
        "patron":    r"ModuleNotFoundError|ImportError",
        "causa":     "Falta una libreria de Python instalada",
        "solucion":  "Abre CMD y ejecuta: pip install playwright gspread google-auth requests",
        "accion":    "notificar",
    },
    {
        "patron":    r"quota|RESOURCE_EXHAUSTED|rate limit",
        "causa":     "Se excedio el limite de llamadas a la API de Google Sheets",
        "solucion":  "El bot esperara al siguiente ciclo de 15 minutos automaticamente.",
        "accion":    "esperar",
    },
    {
        "patron":    r"Login|login|password|usuario|credentials",
        "causa":     "Error de autenticacion en Liverpool OMS — puede que hayan cambiado la contrasena",
        "solucion":  "Verifica que el usuario y contrasena en config.py sean correctos.",
        "accion":    "notificar",
    },
]

# ── Funciones ─────────────────────────────────────────────────

def leer_estado():
    try:
        if os.path.exists(ESTADO_FILE):
            with open(ESTADO_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"ultimo_error": None, "reintentos": 0, "ultima_reparacion": None}

def guardar_estado(estado):
    try:
        with open(ESTADO_FILE, "w") as f:
            json.dump(estado, f, indent=2)
    except Exception:
        pass

def leer_log_hoy():
    """Lee el log del dia de hoy."""
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    log_path  = os.path.join(LOG_DIR, fecha_hoy + ".log")
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def detectar_ultimo_error(contenido_log):
    """Extrae el ultimo error del log."""
    lineas  = contenido_log.splitlines()
    errores = [l for l in lineas if "ERROR" in l or "Error" in l]
    if not errores:
        return None
    ultimo = errores[-1]
    # Extraer hora y mensaje
    partes = ultimo.split("  ", 2)
    hora   = partes[0] if partes else ""
    msg    = partes[-1] if len(partes) >= 2 else ultimo
    return {"hora": hora, "mensaje": msg, "linea_completa": ultimo}

def analizar_error(error_info):
    """Analiza el error y retorna causa, solucion y accion."""
    msg = error_info["mensaje"].lower()
    for catalogo in ERRORES_CONOCIDOS:
        if re.search(catalogo["patron"], msg, re.IGNORECASE):
            return catalogo
    # Error desconocido
    return {
        "causa":    "Error no identificado en el log del bot",
        "solucion": "Revisa el log completo en C:\\liverpool-automation\\logs\\ para mas detalles.",
        "accion":   "notificar",
    }

def enviar_analisis(error_info, analisis, reintento_exitoso):
    """Manda el analisis del error al espacio reporte."""
    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    estado    = "✅ Reparado automaticamente" if reintento_exitoso else "⚠️ Requiere atencion manual"

    lineas = [
        "🔧 *Liverpool Bot — Analisis de error*",
        "📅 " + fecha_now,
        "",
        "🕐 *Hora del error:* " + error_info.get("hora", "Desconocida"),
        "❌ *Error detectado:*",
        "  `" + error_info.get("mensaje", "")[:200] + "`",
        "",
        "🔍 *Causa probable:*",
        "  " + analisis["causa"],
        "",
        "💡 *Como resolverlo:*",
        "  " + analisis["solucion"],
        "",
        "📋 *Estado:* " + estado,
    ]

    if reintento_exitoso:
        lineas.append("")
        lineas.append("El bot fue reiniciado y esta funcionando correctamente.")
    else:
        lineas.append("")
        lineas.append("El bot no pudo repararse automaticamente. Por favor revisa la PC.")

    lineas.append("")
    lineas.append("_Liverpool Bot Reparador 456 — " + fecha_now + "_")

    try:
        requests.post(WEBHOOK, json={"text": "\n".join(lineas)}, timeout=10)
        print(f"Analisis enviado al Chat ✅")
    except Exception as e:
        print(f"Error enviando al Chat: {e}")

def reintentar_bot():
    """Intenta correr el bot principal y retorna True si tuvo exito."""
    print("Reintentando bot principal...")
    try:
        resultado = subprocess.run(
            [PYTHON_PATH, MAIN_PY],
            cwd=CARPETA,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutos max
        )
        if resultado.returncode == 0:
            print("Reintento exitoso ✅")
            return True
        else:
            print(f"Reintento fallido: {resultado.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print("Reintento timeout")
        return False
    except Exception as e:
        print(f"Error en reintento: {e}")
        return False

def hay_error_reciente(contenido_log):
    """Verifica si hay un error en los ultimos 20 minutos."""
    ahora   = datetime.now()
    lineas  = contenido_log.splitlines()
    errores = [l for l in lineas if "ERROR" in l]
    if not errores:
        return False
    ultimo = errores[-1]
    try:
        # Extraer timestamp del log (formato: 2026-04-21 10:08:47,294)
        match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ultimo)
        if match:
            hora_error = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            diff_min   = (ahora - hora_error).total_seconds() / 60
            return diff_min <= 20
    except Exception:
        pass
    return True  # si no se puede parsear, asumir reciente

def verificar_watchdog():
    """Verifica si el bot esta inactivo. Configurable desde la hoja CONFIG."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds  = Credentials.from_service_account_file(os.path.join(CARPETA, "credentials.json"), scopes=scopes)
        gc     = gspread.authorize(creds)
        ss     = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")

        # Leer CONFIG para obtener watchdog_activo y watchdog_minutos
        watchdog_activo  = "si"
        watchdog_minutos = 30
        destinos_str     = "reporte"
        try:
            cfg_hoja = ss.worksheet("CONFIG")
            for row in cfg_hoja.get_all_values()[1:]:
                if row and row[0]:
                    if row[0].strip() == "watchdog_activo":
                        watchdog_activo = row[1].strip().lower() if len(row) > 1 else "si"
                    elif row[0].strip() == "watchdog_minutos":
                        try: watchdog_minutos = int(row[1])
                        except: pass
                    elif row[0].strip() == "destino_watchdog":
                        destinos_str = row[1].strip() if len(row) > 1 else "reporte"
        except Exception:
            pass

        if watchdog_activo != "si":
            print("Watchdog deshabilitado en CONFIG")
            return

        hoja = ss.worksheet("MONITOR")
        rows = hoja.get_all_values()
        if len(rows) < 2:
            return

        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        hoy_rows  = [r for r in rows[1:] if r and r[0] == fecha_hoy and r[5] == "exitosa"]
        if not hoy_rows:
            return

        ultima    = hoy_rows[-1]
        ultima_dt = datetime.strptime(ultima[0] + " " + ultima[1], "%d/%m/%Y %H:%M:%S")
        diff_min  = (datetime.now() - ultima_dt).total_seconds() / 60

        h = datetime.now().hour
        m = datetime.now().minute
        if not (10 <= h < 21 or (h == 21 and m < 30)):
            return

        if diff_min > watchdog_minutos:
            fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
            msg = "🚨 *Watchdog — Bot inactivo*\n\nUltima ejecucion exitosa: " + ultima[1] + " (hace " + str(int(diff_min)) + " min)\n\nLimite configurado: " + str(watchdog_minutos) + " min\n\n_Favor de verificar la PC_\n_Liverpool Bot Reparador — " + fecha_now + "_"

            # Mandar a destinos configurados
            destinos = [d.strip().lower() for d in destinos_str.split(",") if d.strip()]
            webhooks_map = {
                "reporte": "https://chat.googleapis.com/v1/spaces/AAQAQ6DrmfI/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=VzOYmkn9w65FPf64JLq1ySI0VFyD5E8sdc-KQc29nXw",
                "jefes":   "https://chat.googleapis.com/v1/spaces/AAAAMBLT-t0/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=W0pDeYnH05xXzjRER8arPy9xm820yM6Fh1iDHOEJftQ",
                "tiempos": "https://chat.googleapis.com/v1/spaces/AAQAY67HLLk/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=RZGLTs-sAZfBsAamgr7x2y8E6yPVDENsNghyKIOkj6A",
            }
            for d in destinos:
                if d in webhooks_map:
                    try:
                        requests.post(webhooks_map[d], json={"text": msg}, timeout=10)
                    except Exception:
                        pass
            print(f"Watchdog: alerta enviada a {destinos}")
    except Exception as e:
        print(f"Watchdog error: {e}")

def main():
    print(f"[{datetime.now():%H:%M:%S}] Reparador iniciando verificacion...")
    verificar_watchdog()

    estado = leer_estado()
    log    = leer_log_hoy()

    if not log:
        print("Sin log de hoy — bot aun no ha corrido")
        return

    if not hay_error_reciente(log):
        print("Sin errores recientes — bot funcionando correctamente ✅")
        estado["reintentos"] = 0
        guardar_estado(estado)
        return

    error_info = detectar_ultimo_error(log)
    if not error_info:
        print("No se pudo extraer informacion del error")
        return

    # Evitar spam — no reparar si ya se intento recientemente
    ultima_reparacion = estado.get("ultima_reparacion")
    if ultima_reparacion:
        try:
            dt_ultima = datetime.strptime(ultima_reparacion, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - dt_ultima).total_seconds() < 900:  # 15 min
                print("Ya se intento reparar hace menos de 15 min — esperando")
                return
        except Exception:
            pass

    print(f"Error detectado: {error_info['mensaje'][:100]}")
    analisis = analizar_error(error_info)
    print(f"Causa: {analisis['causa']}")
    print(f"Accion: {analisis['accion']}")

    estado["ultimo_error"]       = error_info["mensaje"]
    estado["ultima_reparacion"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    reintento_exitoso = False
    if analisis["accion"] == "reintentar" and estado.get("reintentos", 0) < MAX_REINTENTO:
        estado["reintentos"] = estado.get("reintentos", 0) + 1
        reintento_exitoso    = reintentar_bot()
        if reintento_exitoso:
            estado["reintentos"] = 0
    else:
        print(f"Accion '{analisis['accion']}' — no se reintenta automaticamente")

    enviar_analisis(error_info, analisis, reintento_exitoso)
    guardar_estado(estado)
    print(f"Reparacion completada — exitoso: {reintento_exitoso}")

if __name__ == "__main__":
    main()
