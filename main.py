#!/usr/bin/env python3
"""Liverpool Bot — Bot principal que corre cada 15 minutos."""

import sys
import os
import json
import csv
import time
import logging
import datetime
import re
import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# ── Rutas de archivos de estado ──────────────────────────────────────────────
LOCK_FILE          = os.path.join(config.BASE_DIR, "bot.lock")
HEALTH_FILE        = os.path.join(config.BASE_DIR, "health.json")
PAUSA_FILE         = os.path.join(config.BASE_DIR, "pausa.txt")
APERTURA_FILE      = os.path.join(config.BASE_DIR, "apertura.json")
CONTADOR_JEFES_FILE= os.path.join(config.BASE_DIR, "contador_jefes.json")
ALERTA_FILE        = os.path.join(config.BASE_DIR, "alerta_estado.json")
RECORDATORIO_FILE  = os.path.join(config.BASE_DIR, "recordatorio_estado.json")
CONFIG_CACHE_FILE  = os.path.join(config.BASE_DIR, "config_remota_cache.json")
DIR_CACHE_FILE     = os.path.join(config.BASE_DIR, "directorio_cache.json")
MENSAJES_FILE      = os.path.join(config.BASE_DIR, "mensajes_pendientes.json")
MONITOR_BACKUP     = os.path.join(config.BASE_DIR, "monitor_backup.csv")

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

STATUS_ACTIVOS = {"PENDIENTE", "ASIGNADO", "EN PROCESO", "ACEPTADO", "NUEVO", "ACTIVO", "SURTIDO"}

log = None  # se asigna en main()


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def setup_logging():
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    today = datetime.date.today().strftime("%Y-%m-%d")
    log_file = os.path.join(config.LOGS_DIR, f"{today}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("bot")


# ═══════════════════════════════════════════════════════════════════════════════
#  LOCK FILE
# ═══════════════════════════════════════════════════════════════════════════════

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < 1800:
                log.warning(f"Bot ya corriendo (PID {data.get('pid')}). Saliendo.")
                sys.exit(0)
            log.warning("Lock obsoleto, limpiando.")
        except Exception:
            pass
    with open(LOCK_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILIDADES JSON
# ═══════════════════════════════════════════════════════════════════════════════

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIEMPO Y HORARIO
# ═══════════════════════════════════════════════════════════════════════════════

def hora_mexico():
    try:
        r = requests.get(
            "https://worldtimeapi.org/api/timezone/America/Mexico_City", timeout=5
        )
        dt_str = r.json()["datetime"][:19]
        return datetime.datetime.fromisoformat(dt_str)
    except Exception:
        return datetime.datetime.now()


def check_time():
    try:
        web = hora_mexico()
        diff = abs((datetime.datetime.now() - web).total_seconds())
        if diff > 120:
            log.warning(f"Reloj del sistema desfasado {diff:.0f}s vs worldtimeapi")
    except Exception:
        pass


def en_horario(cfg, now=None):
    if now is None:
        now = datetime.datetime.now()
    hora_ini = int(cfg.get("hora_inicio", 10))
    hora_fin = int(cfg.get("hora_fin", 21))
    min_fin  = int(cfg.get("minuto_fin", 30))

    dias_map = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
    dias_str = str(cfg.get("dias_activos", "lun,mar,mie,jue,vie,sab,dom"))
    dias_act = {dias_map[d.strip()] for d in dias_str.split(",") if d.strip() in dias_map}

    if now.weekday() not in dias_act:
        return False

    ini = now.replace(hour=hora_ini, minute=0, second=0, microsecond=0)
    fin = now.replace(hour=hora_fin, minute=min_fin, second=0, microsecond=0)
    return ini <= now <= fin


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGS Y MENSAJES PENDIENTES
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_old_logs(days=30):
    if not os.path.isdir(config.LOGS_DIR):
        return
    cutoff = time.time() - days * 86400
    for fname in os.listdir(config.LOGS_DIR):
        fpath = os.path.join(config.LOGS_DIR, fname)
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
            try:
                os.remove(fpath)
            except Exception:
                pass


def send_webhook(url, text):
    try:
        r = requests.post(url, json={"text": text}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Webhook falló: {e}")
        return False


def queue_message(url, text):
    pending = load_json(MENSAJES_FILE, [])
    pending.append({"url": url, "text": text, "ts": time.time()})
    save_json(MENSAJES_FILE, pending)


def resend_pending_messages():
    pending = load_json(MENSAJES_FILE, [])
    if not pending:
        return
    log.info(f"Reenviando {len(pending)} mensajes pendientes...")
    remaining = []
    for msg in pending:
        if time.time() - msg.get("ts", 0) > 86400:
            continue  # descartar de más de 24h
        if not send_webhook(msg["url"], msg["text"]):
            remaining.append(msg)
    save_json(MENSAJES_FILE, remaining)


def enviar_o_encolar(url, text):
    if not send_webhook(url, text):
        queue_message(url, text)


# ═══════════════════════════════════════════════════════════════════════════════
#  GOOGLE SHEETS
# ═══════════════════════════════════════════════════════════════════════════════

def get_gc():
    creds = Credentials.from_service_account_file(config.CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def load_remote_config():
    cache = load_json(CONFIG_CACHE_FILE, {})
    if cache and time.time() - cache.get("_ts", 0) < 300:
        return cache

    defaults = {
        "hora_inicio": 10,
        "hora_fin": 21,
        "minuto_fin": 30,
        "dias_activos": "lun,mar,mie,jue,vie,sab,dom",
        "pausado": "no",
        "minutos_vencida": 20,
        "umbral_anomalia": 1.5,
        "watchdog_activo": "no",
        "watchdog_minutos": 30,
        "webhook_reporte": config.WEBHOOK_REPORTE,
        "webhook_jefes": config.WEBHOOK_JEFES,
        "webhook_tiempos": config.WEBHOOK_TIEMPOS,
        "enviar_reporte": "si",
        "enviar_jefes": "si",
        "enviar_tiempos": "si",
        "destino_apertura": "reporte,jefes",
        "destino_cierre": "jefes",
        "destino_recordatorio": "jefes",
        "destino_alerta": "jefes",
    }

    try:
        gc = get_gc()
        sh = gc.open_by_key(config.SHEET_PRINCIPAL_ID)
        ws = sh.worksheet("CONFIG")
        for row in ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0].strip():
                key = row[0].strip().lower()
                val = row[1].strip()
                for conv in (int, float):
                    try:
                        val = conv(val)
                        break
                    except (ValueError, TypeError):
                        pass
                defaults[key] = val
        log.info("Config remota cargada desde Sheet")
    except Exception as e:
        log.warning(f"Config remota no disponible: {e}. Usando defaults.")

    defaults["_ts"] = time.time()
    save_json(CONFIG_CACHE_FILE, defaults)
    return defaults


def check_internet():
    try:
        requests.get("https://www.google.com", timeout=5)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  DESCARGA CSV
# ═══════════════════════════════════════════════════════════════════════════════

def download_csv():
    os.makedirs(config.DESCARGAS_DIR, exist_ok=True)
    os.makedirs(config.SCREENSHOTS_DIR, exist_ok=True)

    intento = 0
    while True:
        intento += 1
        log.info(f"Descarga CSV — intento {intento}...")
        browser = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(accept_downloads=True)
                page = ctx.new_page()

                page.goto(config.LIVERPOOL_URL, timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)

                # Llenar login
                for sel_user in ['input[type="email"]', 'input[name="username"]',
                                  'input[id*="user"]', 'input[placeholder*="user"]',
                                  'input[placeholder*="Usuario"]']:
                    try:
                        page.fill(sel_user, config.LIVERPOOL_USER, timeout=2000)
                        break
                    except Exception:
                        continue

                for sel_pass in ['input[type="password"]', 'input[name="password"]']:
                    try:
                        page.fill(sel_pass, config.LIVERPOOL_PASS, timeout=2000)
                        break
                    except Exception:
                        continue

                page.keyboard.press("Enter")
                page.wait_for_load_state("networkidle", timeout=25000)
                page.wait_for_timeout(3000)

                # Buscar botón de exportar/descargar CSV
                with page.expect_download(timeout=90000) as dl_info:
                    clicked = False
                    for sel in [
                        'button:has-text("CSV")',
                        'button:has-text("Exportar")',
                        'button:has-text("Descargar")',
                        'a:has-text("CSV")',
                        '[title*="CSV"]',
                        '[title*="Export"]',
                        '.export-btn',
                        '[data-testid*="export"]',
                    ]:
                        try:
                            page.click(sel, timeout=3000)
                            clicked = True
                            break
                        except Exception:
                            continue

                    if not clicked:
                        raise RuntimeError("No se encontró botón de descarga")

                dl = dl_info.value
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = os.path.join(config.DESCARGAS_DIR, f"oms_{ts}.csv")
                dl.save_as(dest)
                browser.close()
                log.info(f"CSV descargado: {dest}")
                return dest

        except Exception as e:
            log.error(f"Error descarga intento {intento}: {e}")
            try:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(
                    path=os.path.join(config.SCREENSHOTS_DIR, f"err_{ts}.png"),
                    full_page=True,
                )
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            espera = min(30 * intento, 300)
            log.info(f"Esperando {espera}s antes de reintentar...")
            time.sleep(espera)


# ═══════════════════════════════════════════════════════════════════════════════
#  LECTURA Y LIMPIEZA DE CSV
# ═══════════════════════════════════════════════════════════════════════════════

def limpiar(val):
    if not isinstance(val, str):
        return val
    val = val.strip()
    val = re.sub(r"^'+", "", val)          # comillas simples al inicio
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        val = val[1:-1]                    # comillas dobles envolventes
    return val


def leer_csv(filepath):
    rows = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        headers = next(reader, [])
        for row in reader:
            rows.append([limpiar(c) for c in row])
    return headers, rows


def validar_csv(filepath):
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            headers = next(csv.reader(f), [])
        if len(headers) < 20:
            log.error(f"CSV inválido: {len(headers)} columnas (mínimo 20)")
            return False
        return True
    except Exception as e:
        log.error(f"Error validando CSV: {e}")
        return False


def col(row, idx, default=""):
    try:
        return row[idx] if len(row) > idx else default
    except Exception:
        return default


def parse_dt(row, fecha_idx, hora_idx=None):
    fecha = col(row, fecha_idx)
    hora  = col(row, hora_idx) if hora_idx is not None else ""
    dt_str = f"{fecha} {hora}".strip()
    for fmt in [
        "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",    "%Y-%m-%d %H:%M",
        "%d/%m/%Y",          "%Y-%m-%d",
    ]:
        try:
            return datetime.datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return None


def normalizar_piso(planta):
    p = str(planta).upper().strip()
    if "BAJA" in p or p == "PB":
        return "PLANTA BAJA"
    if "3" in p and ("PISO" in p or "ER" in p or "°" in p):
        return "3er PISO"
    if "2" in p and ("PISO" in p or "°" in p or "DO" in p):
        return "2° PISO"
    if "1" in p and ("PISO" in p or "ER" in p or "°" in p):
        return "1er PISO"
    return planta


def enriquecer(rows):
    ahora = datetime.datetime.now()
    hoy   = ahora.date()
    result = []
    for row in rows:
        planta        = col(row, 0)
        remision      = col(row, 1)
        sku           = col(row, 2)
        desc          = col(row, 3)
        seccion       = col(row, 5)
        dt_emision    = parse_dt(row, 6)
        dt_asignacion = parse_dt(row, 7)          # col H
        status        = col(row, 8).upper()
        jefe          = col(row, 17).upper()
        tipo          = col(row, 19)

        piso = normalizar_piso(planta)

        min_espera = None
        if dt_asignacion:
            min_espera = (ahora - dt_asignacion).total_seconds() / 60

        de_ayer = bool(dt_asignacion and dt_asignacion.date() < hoy)

        result.append({
            "row":          row,
            "planta":       planta,
            "remision":     remision,
            "seccion":      seccion,
            "piso":         piso,
            "dt_asignacion":dt_asignacion,
            "status":       status,
            "jefe":         jefe,
            "tipo":         tipo,
            "min_espera":   min_espera,
            "de_ayer":      de_ayer,
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  DETECCIÓN DE VENCIDAS
# ═══════════════════════════════════════════════════════════════════════════════

def es_activo(status):
    for s in STATUS_ACTIVOS:
        if s in status.upper():
            return True
    return False


def detectar_vencidas(enriched, minutos_vencida=20):
    vencidas, en_tiempo, de_ayer_list = [], [], []
    for it in enriched:
        if not it["dt_asignacion"] or not es_activo(it["status"]):
            continue
        if it["de_ayer"]:
            de_ayer_list.append(it)
        elif it["min_espera"] and it["min_espera"] > minutos_vencida:
            vencidas.append(it)
        else:
            en_tiempo.append(it)
    return vencidas, en_tiempo, de_ayer_list


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTUALIZAR SHEETS
# ═══════════════════════════════════════════════════════════════════════════════

def actualizar_sheets(headers, rows, gc):
    ts = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # Sheet 2 — limpiar V2:AS5000 y pegar con USER_ENTERED
    try:
        sh2 = gc.open_by_key(config.SHEET_2_ID)
        ws2 = sh2.sheet1
        ws2.batch_clear(["V2:AS5000"])
        data = [headers] + rows
        ws2.update("V2", data, value_input_option="USER_ENTERED")
        ws2.update("AS2", [[ts]])
        log.info(f"Sheet 2 actualizado: {len(rows)} filas")
    except Exception as e:
        log.error(f"Error Sheet 2: {e}")

    # Sheet 1 Hoja 1 — RAW
    try:
        sh1 = gc.open_by_key(config.SHEET_PRINCIPAL_ID)
        ws1 = sh1.worksheet("Hoja 1")
        ws1.clear()
        ws1.update("A1", [headers] + rows, value_input_option="RAW")
        log.info(f"Sheet 1 (Hoja 1) actualizado: {len(rows)} filas")
    except Exception as e:
        log.error(f"Error Sheet 1: {e}")


def actualizar_app2(enriched, gc):
    try:
        sh1 = gc.open_by_key(config.SHEET_PRINCIPAL_ID)
        ws  = sh1.worksheet("APP 2.0")

        filas = []
        colores = []
        for it in enriched:
            if not it["dt_asignacion"] or not es_activo(it["status"]):
                continue
            mins = int(it["min_espera"] or 0)
            if it["de_ayer"]:
                estado = "📅 De ayer"
                color  = {"red": 1.0, "green": 0.95, "blue": 0.6}
            elif it["min_espera"] and it["min_espera"] > 20:
                estado = "🔴 Vencida"
                color  = {"red": 0.95, "green": 0.6, "blue": 0.6}
            else:
                estado = "⏰ En tiempo"
                color  = {"red": 0.7, "green": 0.95, "blue": 0.7}

            filas.append([it["piso"], it["remision"], it["seccion"],
                          it["jefe"], it["tipo"], estado, f"{mins} min"])
            colores.append(color)

        encabezados = ["Piso", "Remisión", "Sección", "Jefe", "Tipo", "Estado", "Tiempo"]
        ws.clear()
        ws.update("A1", [encabezados] + filas, value_input_option="RAW")

        fmt_requests = []
        for i, color in enumerate(colores, start=2):
            fmt_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": i - 1,
                        "endRowIndex": i,
                        "startColumnIndex": 0,
                        "endColumnIndex": 7,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
        if fmt_requests:
            sh1.batch_update({"requests": fmt_requests})

        log.info(f"APP 2.0 actualizada: {len(filas)} órdenes activas")
    except Exception as e:
        log.error(f"Error APP 2.0: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  DIRECTORIO, HISTORIAL, DESCANSOS (cache 60 min)
# ═══════════════════════════════════════════════════════════════════════════════

def get_directorio_historial_descansos(gc):
    cache = load_json(DIR_CACHE_FILE, {})
    if cache and time.time() - cache.get("_ts", 0) < 3600:
        return cache.get("directorio", {}), cache.get("historial", []), cache.get("descansos", {})

    directorio, historial, descansos = {}, [], {}

    try:
        sh1 = gc.open_by_key(config.SHEET_PRINCIPAL_ID)

        try:
            for row in sh1.worksheet("DIRECTORIO").get_all_values()[1:]:
                if len(row) >= 2 and row[0].strip():
                    j = row[0].strip().upper()
                    directorio[j] = {
                        "piso":     row[1].strip() if len(row) > 1 else "",
                        "secciones":[s.strip() for s in row[2].split(",")] if len(row) > 2 else [],
                    }
        except Exception as e:
            log.warning(f"DIRECTORIO: {e}")

        try:
            historial = sh1.worksheet("HISTORIAL").get_all_values()[1:]
        except Exception as e:
            log.warning(f"HISTORIAL: {e}")

        try:
            for row in sh1.worksheet("DESCANSOS").get_all_values()[1:]:
                if len(row) >= 2 and row[0].strip():
                    j = row[0].strip().upper()
                    descansos[j] = {
                        "sustituto": row[1].strip().upper() if len(row) > 1 else "",
                        "hasta":     row[2].strip() if len(row) > 2 else "",
                    }
        except Exception as e:
            log.warning(f"DESCANSOS: {e}")

        save_json(DIR_CACHE_FILE, {
            "_ts": time.time(),
            "directorio": directorio,
            "historial":  historial,
            "descansos":  descansos,
        })
        log.info(f"Cache directorio: {len(directorio)} jefes")
    except Exception as e:
        log.error(f"Error cargando directorio: {e}")

    return directorio, historial, descansos


# ═══════════════════════════════════════════════════════════════════════════════
#  ANOMALÍAS
# ═══════════════════════════════════════════════════════════════════════════════

def verificar_anomalia(n_vencidas, cfg, historial):
    umbral = float(cfg.get("umbral_anomalia", 1.5))
    hoy = datetime.date.today().strftime("%d/%m/%Y")
    vals = []
    for row in historial:
        if len(row) >= 3 and row[0] == hoy:
            try:
                vals.append(int(row[2]))
            except Exception:
                pass
    if not vals:
        return False
    promedio = sum(vals) / len(vals)
    return promedio > 0 and n_vencidas >= promedio * umbral


# ═══════════════════════════════════════════════════════════════════════════════
#  TIEMPOS Y MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

def guardar_tiempos(enriched, gc):
    try:
        ahora = datetime.datetime.now()
        with_time = [it for it in enriched if it["min_espera"] is not None and it["dt_asignacion"]]
        avg_seg = (
            sum(it["min_espera"] * 60 for it in with_time) / len(with_time)
            if with_time else 0
        )
        sh1 = gc.open_by_key(config.SHEET_PRINCIPAL_ID)
        sh1.worksheet("TIEMPOS").append_row([
            ahora.strftime("%d/%m/%Y"),
            ahora.strftime("%H:%M"),
            int(avg_seg),
            len(with_time),
        ])
        log.info(f"TIEMPOS guardado: {avg_seg:.0f}s promedio, {len(with_time)} órdenes")
    except Exception as e:
        log.error(f"Error guardando TIEMPOS: {e}")


def guardar_monitor(vencidas, en_tiempo, de_ayer, gc):
    try:
        ahora = datetime.datetime.now()
        total = len(vencidas) + len(en_tiempo) + len(de_ayer)
        fila = [
            ahora.strftime("%d/%m/%Y"),
            ahora.strftime("%H:%M"),
            len(vencidas),
            len(en_tiempo),
            len(de_ayer),
            total,
        ]
        gc.open_by_key(config.SHEET_PRINCIPAL_ID).worksheet("MONITOR").append_row(fila)

        agregar_header = not os.path.exists(MONITOR_BACKUP)
        with open(MONITOR_BACKUP, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if agregar_header:
                w.writerow(["fecha", "hora", "vencidas", "en_tiempo", "de_ayer", "total"])
            w.writerow(fila)

        log.info("MONITOR guardado")
    except Exception as e:
        log.error(f"Error guardando MONITOR: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

def actualizar_health(status, extra=None):
    data = {"status": status, "ts": datetime.datetime.now().isoformat(), "pid": os.getpid()}
    if extra:
        data.update(extra)
    save_json(HEALTH_FILE, data)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTRUCCIÓN DE MENSAJES
# ═══════════════════════════════════════════════════════════════════════════════

def emoji_jefe(nombre):
    return "👩‍💼" if nombre.upper() in config.JEFES_MUJERES else "👨‍💼"


def build_reporte_msg(vencidas, en_tiempo, de_ayer):
    ts = datetime.datetime.now().strftime("%H:%M")
    total = len(vencidas) + len(en_tiempo) + len(de_ayer)
    lines = [
        f"📊 *Reporte {ts}*",
        f"Total activas: *{total}* | ⏰ En tiempo: *{len(en_tiempo)}* | 🔴 Vencidas: *{len(vencidas)}* | 📅 De ayer: *{len(de_ayer)}*",
    ]
    if vencidas:
        lines.append("\n*Vencidas (+20 min):*")
        for v in vencidas[:8]:
            mins = int(v["min_espera"] or 0)
            lines.append(
                f"  • {v['remision']} — {v['seccion']} ({v['piso']}) — *{mins} min* — {emoji_jefe(v['jefe'])} {v['jefe']}"
            )
        if len(vencidas) > 8:
            lines.append(f"  ... y {len(vencidas) - 8} más")
    return "\n".join(lines)


def build_apertura_msg():
    hoy = datetime.date.today().strftime("%A %d/%m/%Y")
    return (
        f"🟢 *Liverpool Bot — Iniciando monitoreo*\n"
        f"Fecha: {hoy}\n"
        f"Horario: 10:00 AM – 9:30 PM\n"
        f"Reporte cada 15 min. Mensajes por piso cada 30 min."
    )


def build_jefes_piso_msg(piso, vencidas, en_tiempo, de_ayer, directorio, descansos):
    def resolver_jefe(j):
        if j in descansos:
            return descansos[j].get("sustituto", j) or j
        return j

    v_p = [x for x in vencidas  if x["piso"] == piso]
    t_p = [x for x in en_tiempo if x["piso"] == piso]
    a_p = [x for x in de_ayer   if x["piso"] == piso]
    if not (v_p or t_p or a_p):
        return None

    ts = datetime.datetime.now().strftime("%H:%M")
    lines = [f"📍 *{piso}* — {ts}"]

    jefes_grupos: dict = {}
    for lista, clave in [(v_p, "vencidas"), (t_p, "en_tiempo"), (a_p, "de_ayer")]:
        for it in lista:
            j = resolver_jefe(it["jefe"] or "SIN JEFE")
            if j not in jefes_grupos:
                jefes_grupos[j] = {"vencidas": [], "en_tiempo": [], "de_ayer": []}
            jefes_grupos[j][clave].append(it)

    for jefe, g in sorted(jefes_grupos.items()):
        total_j = len(g["vencidas"]) + len(g["en_tiempo"]) + len(g["de_ayer"])
        lines.append(f"\n{emoji_jefe(jefe)} *{jefe}* — {total_j} activas")

        if g["en_tiempo"]:
            lines.append(f"  ⏰ En tiempo ({len(g['en_tiempo'])}):")
            for it in g["en_tiempo"][:5]:
                lines.append(f"    • {it['remision']} — {it['seccion']} — {int(it['min_espera'] or 0)} min — {it['tipo']}")
        if g["vencidas"]:
            lines.append(f"  🔴 Vencidas ({len(g['vencidas'])}):")
            for it in g["vencidas"]:
                lines.append(f"    • {it['remision']} — {it['seccion']} — *{int(it['min_espera'] or 0)} min* — {it['tipo']}")
        if g["de_ayer"]:
            lines.append(f"  📅 De ayer ({len(g['de_ayer'])}):")
            for it in g["de_ayer"][:3]:
                lines.append(f"    • {it['remision']} — {it['seccion']} — {it['tipo']}")

    return "\n".join(lines)


def build_recordatorio_msg(vencidas, en_tiempo, de_ayer):
    total = len(vencidas) + len(en_tiempo) + len(de_ayer)
    return (
        f"⏰ *Recordatorio — Faltan 60 min para cierre*\n"
        f"Activas: *{total}* | 🔴 Vencidas: *{len(vencidas)}* | 📅 De ayer: *{len(de_ayer)}*\n"
        f"El bot cierra reporte a las 9:30 PM."
    )


def build_cierre_msg(vencidas, en_tiempo, de_ayer):
    pendientes = len(vencidas) + len(de_ayer)
    return (
        f"🔴 *Cierre de día — 9:30 PM*\n"
        f"Pendientes al cierre: *{pendientes}*\n"
        f"🔴 Vencidas: *{len(vencidas)}* | ⏰ En tiempo: *{len(en_tiempo)}* | 📅 De ayer: *{len(de_ayer)}*\n"
        f"Bot entrando en modo nocturno. Hasta mañana 🌙"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  CONTROL DE ENVÍOS (apertura, recordatorio, contador jefes, alerta)
# ═══════════════════════════════════════════════════════════════════════════════

def _hoy_str():
    return datetime.date.today().isoformat()


def ya_mando_apertura():
    return load_json(APERTURA_FILE, {}).get("fecha") == _hoy_str()


def marcar_apertura():
    save_json(APERTURA_FILE, {"fecha": _hoy_str()})


def ya_mando_recordatorio():
    return load_json(RECORDATORIO_FILE, {}).get("fecha") == _hoy_str()


def marcar_recordatorio():
    save_json(RECORDATORIO_FILE, {"fecha": _hoy_str()})


def get_contador_jefes():
    d = load_json(CONTADOR_JEFES_FILE, {})
    return d.get("contador", 0) if d.get("fecha") == _hoy_str() else 0


def incrementar_contador_jefes():
    d = load_json(CONTADOR_JEFES_FILE, {})
    if d.get("fecha") != _hoy_str():
        d = {"fecha": _hoy_str(), "contador": 0}
    d["contador"] = d.get("contador", 0) + 1
    save_json(CONTADOR_JEFES_FILE, d)


def puede_enviar_alerta():
    return time.time() - load_json(ALERTA_FILE, {}).get("ultimo", 0) > 1800


def marcar_alerta():
    save_json(ALERTA_FILE, {"ultimo": time.time()})


# ═══════════════════════════════════════════════════════════════════════════════
#  MODO TEST
# ═══════════════════════════════════════════════════════════════════════════════

def modo_test(gc):
    log.info("=== MODO TEST ===")
    ok = True

    for nombre, sheet_id in [("Sheet principal", config.SHEET_PRINCIPAL_ID),
                               ("Sheet 2", config.SHEET_2_ID)]:
        try:
            sh = gc.open_by_key(sheet_id)
            log.info(f"✓ {nombre}: {sh.title}")
        except Exception as e:
            log.error(f"✗ {nombre}: {e}")
            ok = False

    for nombre, url in [("Reporte", config.WEBHOOK_REPORTE),
                         ("Jefes",   config.WEBHOOK_JEFES),
                         ("Tiempos", config.WEBHOOK_TIEMPOS)]:
        if send_webhook(url, f"🔧 Test Liverpool Bot — {nombre}"):
            log.info(f"✓ Webhook {nombre}")
        else:
            log.error(f"✗ Webhook {nombre}")
            ok = False

    log.info(f"=== TEST {'OK' if ok else 'CON ERRORES'} ===")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global log
    log = setup_logging()

    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    forzar  = "--forzar"  in args
    test    = "test"      in args

    log.info("=" * 60)
    log.info(f"Liverpool Bot iniciando | args={args}")

    acquire_lock()
    try:
        cleanup_old_logs(30)
        resend_pending_messages()
        check_time()

        try:
            gc = get_gc()
        except Exception as e:
            log.error(f"No se pudo conectar a Google: {e}")
            actualizar_health("error_google", {"error": str(e)})
            return

        if test:
            modo_test(gc)
            return

        cfg = load_remote_config()

        if dry_run:
            log.info("DRY-RUN: solo verificaciones")
            modo_test(gc)
            return

        if not check_internet():
            log.error("Sin internet")
            actualizar_health("sin_internet")
            return

        if os.path.exists(PAUSA_FILE):
            log.info("Pausado (pausa.txt)")
            actualizar_health("pausado_manual")
            return

        if str(cfg.get("pausado", "no")).lower() == "si":
            log.info("Pausado (config remota)")
            actualizar_health("pausado_config")
            return

        now = datetime.datetime.now()
        if not forzar and not en_horario(cfg, now):
            log.info(f"Fuera de horario: {now.strftime('%H:%M')}")
            actualizar_health("fuera_horario")
            return

        log.info(f"Hora activa: {now.strftime('%H:%M')}")

        # ── Descarga CSV ────────────────────────────────────────────────────
        csv_path = download_csv()
        if not validar_csv(csv_path):
            actualizar_health("csv_invalido")
            return

        headers, rows = leer_csv(csv_path)
        log.info(f"CSV: {len(rows)} filas, {len(headers)} columnas")

        enriched = enriquecer(rows)

        # ── Actualizar Sheets ────────────────────────────────────────────────
        actualizar_sheets(headers, rows, gc)

        # ── Directorio / historial / descansos ───────────────────────────────
        directorio, historial, descansos = get_directorio_historial_descansos(gc)

        # ── Detectar vencidas ────────────────────────────────────────────────
        minutos_vencida = int(cfg.get("minutos_vencida", 20))
        vencidas, en_tiempo, de_ayer = detectar_vencidas(enriched, minutos_vencida)
        total = len(vencidas) + len(en_tiempo) + len(de_ayer)
        log.info(f"Órdenes: {total} total | {len(vencidas)} vencidas | {len(en_tiempo)} en tiempo | {len(de_ayer)} de ayer")

        # ── APP 2.0 ──────────────────────────────────────────────────────────
        actualizar_app2(enriched, gc)

        # ── TIEMPOS ──────────────────────────────────────────────────────────
        guardar_tiempos(enriched, gc)

        # ── Webhooks activos ─────────────────────────────────────────────────
        wh_rep    = str(cfg.get("webhook_reporte", config.WEBHOOK_REPORTE))
        wh_jef    = str(cfg.get("webhook_jefes",   config.WEBHOOK_JEFES))
        wh_tie    = str(cfg.get("webhook_tiempos",  config.WEBHOOK_TIEMPOS))
        ok_rep    = str(cfg.get("enviar_reporte", "si")).lower() == "si"
        ok_jef    = str(cfg.get("enviar_jefes",   "si")).lower() == "si"
        ok_tie    = str(cfg.get("enviar_tiempos",  "si")).lower() == "si"

        # ── 10:00 AM — Apertura ─────────────────────────────────────────────
        if now.hour == 10 and not ya_mando_apertura():
            msg_ap = build_apertura_msg()
            destinos = str(cfg.get("destino_apertura", "reporte,jefes")).split(",")
            if "reporte" in destinos and ok_rep:
                enviar_o_encolar(wh_rep, msg_ap)
            if "jefes" in destinos and ok_jef:
                enviar_o_encolar(wh_jef, msg_ap)
            marcar_apertura()

        # ── 8:30 PM — Recordatorio ───────────────────────────────────────────
        if now.hour == 20 and now.minute >= 30 and not ya_mando_recordatorio():
            if ok_jef:
                enviar_o_encolar(wh_jef, build_recordatorio_msg(vencidas, en_tiempo, de_ayer))
            marcar_recordatorio()

        # ── 9:30 PM — Cierre ────────────────────────────────────────────────
        if now.hour == 21 and now.minute >= 30:
            if ok_jef:
                enviar_o_encolar(wh_jef, build_cierre_msg(vencidas, en_tiempo, de_ayer))

        # ── Cada 15 min — Espacio Reporte ────────────────────────────────────
        if ok_rep:
            enviar_o_encolar(wh_rep, build_reporte_msg(vencidas, en_tiempo, de_ayer))

        # ── Cada 30 min — Espacio Jefes (rotando pisos) ──────────────────────
        if ok_jef and now.minute < 15:
            contador = get_contador_jefes()
            piso = config.PISOS[contador % len(config.PISOS)]
            msg_piso = build_jefes_piso_msg(piso, vencidas, en_tiempo, de_ayer, directorio, descansos)
            if msg_piso:
                enviar_o_encolar(wh_jef, msg_piso)
            incrementar_contador_jefes()

        # ── Alerta de anomalía ───────────────────────────────────────────────
        if verificar_anomalia(len(vencidas), cfg, historial) and puede_enviar_alerta():
            alerta = (
                f"⚠️ *ALERTA: Anomalía de vencidas*\n"
                f"Hay *{len(vencidas)}* vencidas — por encima del promedio del día.\n"
                f"Por favor revisar el piso con mayor retraso."
            )
            if ok_jef:
                enviar_o_encolar(wh_jef, alerta)
            marcar_alerta()

        # ── MONITOR ──────────────────────────────────────────────────────────
        guardar_monitor(vencidas, en_tiempo, de_ayer, gc)

        actualizar_health("ok", {
            "vencidas": len(vencidas),
            "en_tiempo": len(en_tiempo),
            "de_ayer": len(de_ayer),
            "total": total,
        })
        log.info("Ejecución completada ✓")

    except Exception as e:
        log.exception(f"Error no manejado: {e}")
        actualizar_health("error", {"error": str(e)})
        try:
            send_webhook(config.WEBHOOK_REPORTE, f"❌ *Error Liverpool Bot*\n```{str(e)[:400]}```")
        except Exception:
            pass
    finally:
        release_lock()


if __name__ == "__main__":
    main()
