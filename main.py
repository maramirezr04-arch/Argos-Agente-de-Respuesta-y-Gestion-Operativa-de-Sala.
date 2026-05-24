import os, logging, requests, csv, time, json, random
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials
from config import LIVERPOOL, GOOGLE, CHAT, CARPETA_DESCARGA, PC_NOMBRE

VERSION = "1.0.3"

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(f"logs/{datetime.now():%Y-%m-%d}.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── OPTIMIZACIONES — caches y métricas ─────────────────────
import gc as gc_mod
from functools import lru_cache

METRICAS_PASOS = {}  # {paso: [tiempos]}
HEALTH_FILE    = "health.json"
QUEUE_FILE     = "mensajes_pendientes.json"

def medir_paso(nombre, inicio):
    dur = time.time() - inicio
    if nombre not in METRICAS_PASOS:
        METRICAS_PASOS[nombre] = []
    METRICAS_PASOS[nombre].append(round(dur, 2))
    if len(METRICAS_PASOS[nombre]) > 50:
        METRICAS_PASOS[nombre] = METRICAS_PASOS[nombre][-50:]
    return dur

def guardar_health(estado, **kwargs):
    """Health check para que el dashboard pueda leer estado actual."""
    try:
        data = {
            "estado":            estado,
            "ultima_actualizacion": datetime.now().isoformat(),
            "metricas_pasos":    {k: round(sum(v)/len(v),2) for k,v in METRICAS_PASOS.items() if v},
        }
        data.update(kwargs)
        with open(HEALTH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def post_chat_con_reintento(url, payload, max_intentos=3):
    """Manda al Chat con backoff exponencial."""
    espera = 5
    for intento in range(max_intentos):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if 200 <= r.status_code < 300:
                return True
            log.warning(f"Webhook respondio {r.status_code}, intento {intento+1}")
        except Exception as e:
            log.warning(f"Webhook fallo intento {intento+1}: {e}")
        time.sleep(espera)
        espera *= 2
    # Encolar mensaje fallido
    encolar_mensaje(url, payload)
    return False

def encolar_mensaje(url, payload):
    """Si Chat falla, guardar para reintentar despues."""
    try:
        cola = []
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                cola = json.load(f)
        cola.append({"url": url, "payload": payload, "ts": datetime.now().isoformat()})
        cola = cola[-100:]
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(cola, f)
    except Exception:
        pass

def reenviar_cola_mensajes():
    """Reintenta mandar mensajes encolados al inicio."""
    try:
        if not os.path.exists(QUEUE_FILE):
            return
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            cola = json.load(f)
        if not cola:
            return
        log.info(f"Reintentando {len(cola)} mensajes encolados...")
        pendientes = []
        for msg in cola:
            try:
                r = requests.post(msg["url"], json=msg["payload"], timeout=10)
                if not (200 <= r.status_code < 300):
                    pendientes.append(msg)
            except Exception:
                pendientes.append(msg)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(pendientes, f)
    except Exception:
        pass

@lru_cache(maxsize=1000)
def parse_fecha_cached(fecha_str):
    """Cache de parseo de fechas — mismas fechas se parsean miles de veces."""
    s = str(fecha_str).strip().lstrip("'")
    if not s:
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def cargar_estructuras_sheets(ss):
    """Lee DIRECTORIO, HISTORIAL y DESCANSOS en una sola tanda."""
    dir_dict, hist_dict, descansos, jefes_en_descanso = {}, {}, {}, {}
    try:
        # batch_get para leer 3 hojas en una sola llamada
        ranges  = ["DIRECTORIO!A:F", "HISTORIAL!A:C", "DESCANSOS!A:D"]
        results = ss.values_batch_get(ranges).get("valueRanges", [])
        if len(results) >= 1 and results[0].get("values"):
            for row in results[0]["values"][1:]:
                if row and row[0]:
                    dir_dict[str(row[0]).strip()] = {
                        "nombre_seccion": row[1] if len(row) > 1 else "",
                        "jefe":           row[2] if len(row) > 2 else "",
                        "ubicacion":      row[5] if len(row) > 5 else "",
                    }
        if len(results) >= 2 and results[1].get("values"):
            for row in results[1]["values"][1:]:
                if row and row[0]:
                    hist_dict[str(row[0]).strip()] = {"Jefe": row[2] if len(row)>2 else ""}
        if len(results) >= 3 and results[2].get("values"):
            hoy_str = datetime.now().strftime("%d/%m/%Y")
            for row in results[2]["values"][1:]:
                if not row or len(row) < 3 or row[0] != hoy_str:
                    continue
                jefe_descansa = str(row[1]).strip().upper()
                jefe_cubre    = str(row[2]).strip().upper()
                if not jefe_descansa or not jefe_cubre:
                    continue
                # Mapear todas las secciones del jefe que descansa usando DIRECTORIO
                for sec, info in dir_dict.items():
                    if str(info.get("jefe","")).strip().upper() == jefe_descansa:
                        descansos[sec]         = jefe_cubre
                        jefes_en_descanso[sec] = jefe_descansa
                log.info(f"Descanso hoy: {jefe_descansa} → cubierto por {jefe_cubre}")
    except Exception as e:
        log.warning(f"Error batch_get sheets: {e}")
    return dir_dict, hist_dict, descansos, jefes_en_descanso


# ── Webhooks ──────────────────────────────────────────────────
WEBHOOK       = "https://chat.googleapis.com/v1/spaces/AAQAQ6DrmfI/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=VzOYmkn9w65FPf64JLq1ySI0VFyD5E8sdc-KQc29nXw"
WEBHOOK_JEFES  = "https://chat.googleapis.com/v1/spaces/AAAAMBLT-t0/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=W0pDeYnH05xXzjRER8arPy9xm820yM6Fh1iDHOEJftQ"
WEBHOOK_TIEMPOS = "https://chat.googleapis.com/v1/spaces/AAQAY67HLLk/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=RZGLTs-sAZfBsAamgr7x2y8E6yPVDENsNghyKIOkj6A"

# ── Constantes ────────────────────────────────────────────────
# ── UMBRALES CONFIGURABLES ────────────────────────────────────
MINUTOS_VENCIDA       = 20     # minutos para considerar vencida
UMBRAL_ANOMALIA       = 1.5    # 50% mas del promedio
WATCHDOG_MINUTOS      = 30     # alerta si no corre en X min
MAX_EJECUCION_SEG     = 600    # 10 min max por ejecucion
# ── Horario — configurable ───────────────────────────────────
HORA_INICIO     = 10
HORA_FIN        = 21
MINUTO_FIN      = 30
# ── Archivos ─────────────────────────────────────────────────
TIEMPOS_FILE      = "tiempos.json"
CONTADOR_FILE     = "contador_jefes.json"
APERTURA_FILE     = "apertura.json"
CIERRE_FILE       = "cierre.json"
LOCK_FILE         = "bot.lock"
PAUSA_FILE        = "pausa.txt"
DIR_CACHE_FILE    = "directorio_cache.json"
MONITOR_BACKUP    = "monitor_backup.csv"
HISTORIAL_MAX     = 10
MARGEN            = 1.3
DIR_CACHE_MINUTOS = 60  # actualiza cache DIRECTORIO cada hora

TIPOS_PRIORIDAD = [
    "HD0D - Mismo dia",
    "HD1D - Manana",
    "CC0D - Mismo dia",
    "CC1D - Manana",
    "C&C Misma Tienda",
]

ORDEN_PISOS = ["PLANTA BAJA", "1er PISO", "2 PISO", "3er PISO"]
NOMBRES_PISOS = {0: "PLANTA BAJA", 1: "1er PISO", 2: "2\u00b0 PISO", 3: "3er PISO"}

JEFES_MUJERES = [
    "ALMA DELIA", "BRENDA", "DENISSE", "GEOLIBETH",
    "JOANA", "LIZBETH", "MARIA DE LOS ANGELES",
    "NUBIA BERENICE", "ROSALBA"
]

MENSAJES_APERTURA = [
    "Iniciamos el dia de hoy con *{total}* clientes en espera de sus articulos. Jefes, contamos con su apoyo para trabajar lo antes posible las remisiones de ayer.",
    "Arrancamos la jornada con *{total}* clientes esperando sus pedidos. Les pedimos su apoyo para dar prioridad a las remisiones pendientes de ayer.",
    "Hoy comenzamos con *{total}* articulos por atender. Jefes, es importante que trabajemos juntos para liquidar primero las remisiones de ayer.",
    "Tenemos *{total}* clientes en espera de sus articulos. Agradecemos su compromiso para atender las remisiones de ayer a la brevedad.",
    "Iniciamos con *{total}* clientes esperando. Contamos con su apoyo para resolver primero los pendientes de ayer.",
    "Comenzamos la jornada con *{total}* remisiones activas. Jefes, solicitamos su apoyo para priorizar los articulos pendientes de ayer.",
    "Hoy arrancan con *{total}* clientes en espera. Recuerden dar prioridad a las remisiones de ayer para mantener la satisfaccion de nuestros clientes.",
    "Al iniciar el dia contamos con *{total}* articulos pendientes de entrega. Les pedimos su apoyo para atender primero los de ayer.",
]

MENSAJES_CIERRE = [
    "Terminamos el dia de hoy con *{total}* clientes aun en espera de sus articulos.",
    "Cerramos la jornada con *{total}* remisiones activas. Manana seguimos dando lo mejor.",
    "Fin de jornada. Al cierre contamos con *{total}* articulos pendientes de entrega.",
    "Buenas noches equipo. Terminamos con *{total}* clientes en espera. Hasta manana.",
    "Cerramos el dia con *{total}* remisiones activas. Gracias por su esfuerzo de hoy.",
]

MENSAJES_CON_TIPOS = [
    "Contamos con clientes en espera de su mercancia, favor de apoyarnos a atender todos los articulos *dando prioridad* a HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Tenemos clientes esperando su pedido. Por favor demos prioridad a los articulos HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda para brindar una mejor experiencia.",
    "Recordemos que cada cliente cuenta. Apoyanos atendiendo los pedidos pendientes, especialmente los de tipo HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Hay clientes en espera de su mercancia. Favor de revisar y atender los articulos pendientes dando prioridad a HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Se tienen articulos pendientes de entrega. Solicitamos tu apoyo para atenderlos a la brevedad, priorizando HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Estimado equipo, contamos con clientes que aguardan su pedido. Les pedimos apoyo para agilizar la atencion, en especial HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "El tiempo de espera de nuestros clientes es importante. Favor de dar atencion inmediata a los pedidos HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Brindemos una excelente experiencia a nuestros clientes. Por favor atiende los articulos pendientes priorizando HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Se tienen articulos en cola de atencion. Agradecemos tu apoyo para resolverlos a la brevedad, con prioridad en HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
    "Para mantener la satisfaccion de nuestros clientes, favor de atender los pedidos pendientes dando prioridad a HD0D, HD1D, CC0D, CC1D y C&C Misma Tienda.",
]

MENSAJES_SIN_TIPOS = [
    "Contamos con clientes en espera de su mercancia, favor de atender sus articulos vencidos (+20 min).",
    "Se tienen articulos con mas de 20 minutos sin atender. Favor de revisar y dar atencion a la brevedad.",
    "Hay pedidos que superaron el tiempo de atencion. Agradecemos tu apoyo para resolverlos de inmediato.",
    "Se detectaron articulos vencidos en tus secciones. Favor de atenderlos para mejorar el tiempo de respuesta.",
    "Tenemos articulos con tiempo de espera elevado. Solicitamos tu apoyo para atenderlos cuanto antes.",
    "Articulos con tiempo vencido detectados. Favor de priorizar su atencion para no afectar al cliente.",
    "Para garantizar la satisfaccion de nuestros clientes, favor de atender los articulos con mas de 20 minutos.",
    "Se requiere tu apoyo para atender articulos con tiempo de espera superado en tus secciones.",
]

# ── WEBHOOKS JEFES ───────────────────────────────────────────
WEBHOOKS_JEFES_CACHE = {}

def cargar_webhooks_jefes(gc, nombres_jefes=None):
    """Lee la hoja WEBHOOKS_JEFES y retorna {nombre_jefe: webhook_url}.
    Si la hoja esta vacia y se pasan nombres_jefes, los escribe como referencia.
    """
    try:
        ss   = gc.open_by_key(GOOGLE["sheet_id"])
        hoja = ss.worksheet("WEBHOOKS_JEFES")
        rows = hoja.get_all_values()

        # Si solo tiene el header (o esta vacia) y tenemos nombres, poblar columna A
        datos = [r for r in rows[1:] if any(c.strip() for c in r)]
        if not datos and nombres_jefes:
            log.info(f"WEBHOOKS_JEFES vacia — agregando {len(nombres_jefes)} nombres de jefes...")
            nuevas = [[n, ""] for n in sorted(nombres_jefes)]
            hoja.append_rows(nuevas, value_input_option="RAW")
            log.info("WEBHOOKS_JEFES: nombres agregados ✅ — agrega los webhooks en columna B")
            return {}

        resultado = {}
        for row in rows[1:]:
            if row and len(row) >= 2 and row[0] and row[1]:
                nombre  = str(row[0]).strip()
                webhook = str(row[1]).strip()
                # Columna C = activo (True por defecto si está vacía)
                activo  = True
                if len(row) >= 3 and str(row[2]).strip().lower() in ("false", "0", "no"):
                    activo = False
                if nombre and webhook and activo:
                    resultado[nombre] = webhook
        log.info(f"WEBHOOKS_JEFES cargados: {len(resultado)} jefes con webhook activo")
        return resultado
    except gspread.WorksheetNotFound:
        log.info("Hoja WEBHOOKS_JEFES no existe — mensajes individuales desactivados")
        return {}
    except Exception as e:
        log.warning(f"Error cargando WEBHOOKS_JEFES: {e}")
        return {}

# ── CONFIG REMOTA (HOJA CONFIG DEL SHEET 1) ──────────────────

CONFIG_REMOTA = {}
CONFIG_CACHE_FILE = "config_remota_cache.json"

def cargar_config_remota(gc):
    """Lee la hoja CONFIG del Sheet 1 y actualiza valores globales."""
    global HORA_INICIO, HORA_FIN, MINUTO_FIN, MINUTOS_VENCIDA, UMBRAL_ANOMALIA, WATCHDOG_MINUTOS, CONFIG_REMOTA
    try:
        ss = gc.open_by_key(GOOGLE["sheet_id"])
        try:
            hoja = ss.worksheet("CONFIG")
        except gspread.WorksheetNotFound:
            # Crear hoja CONFIG con valores por defecto
            hoja = ss.add_worksheet("CONFIG", rows=30, cols=2)
            defaults = [
                ["Parametro", "Valor"],
                ["hora_inicio", "10"],
                ["hora_fin", "21"],
                ["minuto_fin", "30"],
                ["dias_activos", "lun,mar,mie,jue,vie,sab,dom"],
                ["pausado", "no"],
                ["minutos_vencida", "20"],
                ["umbral_anomalia", "1.5"],
                ["watchdog_minutos", "30"],
                ["webhook_reporte", WEBHOOK],
                ["webhook_jefes", WEBHOOK_JEFES],
                ["webhook_tiempos", WEBHOOK_TIEMPOS],
            ]
            hoja.update(defaults, "A1")
            log.info("Hoja CONFIG creada con valores por defecto")

        rows = hoja.get_all_values()
        cfg = {}
        for row in rows[1:]:
            if row and len(row) >= 2 and row[0]:
                cfg[row[0].strip()] = row[1].strip()

        # Aplicar config si existen
        if cfg.get("hora_inicio", "").isdigit():
            HORA_INICIO = int(cfg["hora_inicio"])
        if cfg.get("hora_fin", "").isdigit():
            HORA_FIN = int(cfg["hora_fin"])
        if cfg.get("minuto_fin", "").isdigit():
            MINUTO_FIN = int(cfg["minuto_fin"])
        if cfg.get("minutos_vencida", "").isdigit():
            MINUTOS_VENCIDA = int(cfg["minutos_vencida"])
        try:
            if cfg.get("umbral_anomalia"):
                UMBRAL_ANOMALIA = float(cfg["umbral_anomalia"])
        except: pass
        if cfg.get("watchdog_minutos", "").isdigit():
            WATCHDOG_MINUTOS = int(cfg["watchdog_minutos"])

        CONFIG_REMOTA = cfg

        # Guardar cache local
        try:
            with open(CONFIG_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except: pass

        log.info(f"CONFIG cargado: horario {HORA_INICIO}:00 - {HORA_FIN}:{MINUTO_FIN}")
        return cfg
    except Exception as e:
        log.warning(f"Error cargando CONFIG remoto: {e}")
        # Usar cache local si existe
        try:
            if os.path.exists(CONFIG_CACHE_FILE):
                with open(CONFIG_CACHE_FILE, "r", encoding="utf-8") as f:
                    CONFIG_REMOTA = json.load(f)
        except: pass
        return CONFIG_REMOTA

def bot_pausado_remoto():
    return CONFIG_REMOTA.get("pausado", "").lower() in ("si", "yes", "true", "1")

def registrar_y_verificar_pc(gc):
    """Registra esta PC en la hoja PCS y verifica si esta pausada.
    Retorna True si el bot debe continuar, False si esta pausada."""
    try:
        ss = gc.open_by_key(GOOGLE["sheet_id"])
        try:
            hoja = ss.worksheet("PCS")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("PCS", rows=50, cols=4)
            hoja.update([["nombre", "estado", "ultima_conexion", "version"]], "A1")
            log.info("Hoja PCS creada")

        rows      = hoja.get_all_values()
        nombres   = [r[0] for r in rows[1:]] if len(rows) > 1 else []
        ahora_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        if PC_NOMBRE in nombres:
            idx    = nombres.index(PC_NOMBRE) + 2   # fila en sheet (1-based + header)
            estado = rows[idx - 1][1] if len(rows[idx - 1]) > 1 else "activo"
            # Actualizar ultima conexion y version
            hoja.update([[ahora_str, VERSION]], f"C{idx}:D{idx}")
            log.info(f"PC '{PC_NOMBRE}' registrada — estado: {estado}")
            if estado.lower() == "pausado":
                log.info(f"Esta PC ({PC_NOMBRE}) esta pausada remotamente. Bot detenido.")
                return False
        else:
            # PC nueva — agregar fila
            hoja.append_row([PC_NOMBRE, "activo", ahora_str, VERSION])
            log.info(f"PC '{PC_NOMBRE}' registrada por primera vez en PCS")

        return True
    except Exception as e:
        log.warning(f"Error en registrar_y_verificar_pc: {e} — continuando sin verificacion")
        return True   # En caso de error, dejar correr el bot

def dia_activo_hoy():
    """Verifica si hoy esta en la lista de dias activos."""
    dias_str = CONFIG_REMOTA.get("dias_activos", "lun,mar,mie,jue,vie,sab,dom").lower()
    dias_lista = [d.strip() for d in dias_str.split(",")]
    nombres = ["lun","mar","mie","jue","vie","sab","dom"]
    hoy = nombres[datetime.now().weekday()]
    return hoy in dias_lista

# ── Tiempos adaptativos ───────────────────────────────────────

def cargar_tiempos():
    try:
        if os.path.exists(TIEMPOS_FILE):
            with open(TIEMPOS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"login": [6], "indicadores": [5], "calendario": [2], "datos": [5], "descarga": [30]}

def guardar_tiempos(tiempos):
    try:
        with open(TIEMPOS_FILE, "w") as f:
            json.dump(tiempos, f, indent=2)
    except Exception as e:
        log.error(f"Error guardando tiempos: {e}")

def calcular_espera(historial):
    if not historial:
        return 5.0
    ultimos  = historial[-HISTORIAL_MAX:]
    promedio = sum(ultimos) / len(ultimos)
    return max(3.0, min(60.0, promedio * MARGEN))

def actualizar_historial(historial, nuevo):
    historial.append(round(nuevo, 2))
    return historial[-HISTORIAL_MAX:]

def medir(inicio):
    return round(time.time() - inicio, 2)

# ── Horario ───────────────────────────────────────────────────

def dentro_de_horario():
    ahora = datetime.now()
    if ahora.hour < HORA_INICIO: return False
    if ahora.hour > HORA_FIN:    return False
    # Permitir hasta 15 min despues de MINUTO_FIN para que el cierre alcance a enviarse
    if ahora.hour == HORA_FIN and ahora.minute >= MINUTO_FIN + 15: return False
    return True

def es_hora_apertura():
    ahora = datetime.now()
    return ahora.hour == HORA_INICIO and ahora.minute < 15

def es_hora_cierre():
    ahora = datetime.now()
    # Disparar EN la hora de fin (no 15 min antes)
    return ahora.hour == HORA_FIN and MINUTO_FIN <= ahora.minute < MINUTO_FIN + 15

def apertura_ya_enviada():
    try:
        if os.path.exists(APERTURA_FILE):
            with open(APERTURA_FILE, "r") as f:
                data = json.load(f)
            return data.get("fecha") == datetime.now().strftime("%d/%m/%Y")
    except Exception:
        pass
    return False

def marcar_apertura_enviada():
    try:
        with open(APERTURA_FILE, "w") as f:
            json.dump({"fecha": datetime.now().strftime("%d/%m/%Y")}, f)
    except Exception:
        pass

def cierre_ya_enviado():
    try:
        if os.path.exists(CIERRE_FILE):
            with open(CIERRE_FILE, "r") as f:
                data = json.load(f)
            return data.get("fecha") == datetime.now().strftime("%d/%m/%Y")
    except Exception:
        pass
    return False

def marcar_cierre_enviado():
    try:
        with open(CIERRE_FILE, "w") as f:
            json.dump({"fecha": datetime.now().strftime("%d/%m/%Y")}, f)
    except Exception:
        pass

# ── Contador jefes ────────────────────────────────────────────

def leer_contador():
    try:
        if os.path.exists(CONTADOR_FILE):
            with open(CONTADOR_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"count": 0}

def guardar_contador(c):
    try:
        with open(CONTADOR_FILE, "w") as f:
            json.dump(c, f)
    except Exception:
        pass

# ── Utilidades ────────────────────────────────────────────────

def get_fechas():
    hoy  = datetime.now()
    ayer = hoy - timedelta(days=1)
    return ayer.strftime("%d/%m/%Y"), hoy.strftime("%d/%m/%Y")

def convertir_valor(v):
    if v is None or v == "":
        return ""
    s = str(v).strip().lstrip("'")
    if s == "":
        return ""
    for fmt in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            datetime.strptime(s, fmt)
            return s
        except ValueError:
            pass
    try:
        return int(s) if "." not in s else float(s)
    except (ValueError, TypeError):
        return s

def limpiar_datos(datos):
    """Quita comillas iniciales de TODOS los valores — para Sheet 2 con USER_ENTERED."""
    resultado = []
    for row in datos:
        fila = []
        for v in row:
            s = str(v).strip().lstrip("'") if v is not None else ""
            if s == "" and v != 0:
                fila.append("")
                continue
            # Intentar numero
            try:
                if "." not in s:
                    fila.append(int(s))
                else:
                    fila.append(float(s))
                continue
            except (ValueError, TypeError):
                pass
            fila.append(s)
        resultado.append(fila)
    return resultado

def orden_piso(ubicacion):
    """Detecta el piso basado en palabras clave especificas en orden estricto."""
    ub = str(ubicacion).upper().strip()
    # Quitar caracteres especiales para mejor matching
    ub_limpio = ub.replace("°", "").replace("º", "").replace("°", "")

    # Detectar en orden de mas especifico a menos especifico
    if "PLANTA BAJA" in ub_limpio or "PB" == ub_limpio.strip():
        return 0
    if "3ER" in ub_limpio or "3RO" in ub_limpio or "TERCER" in ub_limpio or "3 PISO" in ub_limpio:
        return 3
    if "2DO" in ub_limpio or "SEGUNDO" in ub_limpio or "2 PISO" in ub_limpio:
        return 2
    if "1ER" in ub_limpio or "1RO" in ub_limpio or "PRIMER" in ub_limpio or "1 PISO" in ub_limpio:
        return 1
    return 99

def get_genero(nom_jefe):
    nom = nom_jefe.upper()
    for mujer in JEFES_MUJERES:
        if mujer in nom:
            return "jefa"
    return "jefe"

def get_mencion(nom_jefe):
    genero = get_genero(nom_jefe)
    emoji  = "\U0001f469\u200d\U0001f4bc" if genero == "jefa" else "\U0001f468\u200d\U0001f4bc"
    return emoji + " *" + genero.upper() + " " + nom_jefe + "*"

def contar_tipos(remisiones):
    conteo = {}
    for r in remisiones:
        te = str(r.get("tipo_entrega", "")).strip()
        for tp in TIPOS_PRIORIDAD:
            if tp.lower() in te.lower() or te.lower() in tp.lower():
                conteo[tp] = conteo.get(tp, 0) + 1
                break
    return conteo

def calcular_minutos(fecha_str):
    if not fecha_str or str(fecha_str).strip() in ("", "nan", "None"):
        return 0
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]:
            try:
                fecha = datetime.strptime(str(fecha_str).strip(), fmt)
                return int((datetime.now() - fecha).total_seconds() / 60)
            except ValueError:
                continue
        return 0
    except Exception:
        return 0

def es_de_ayer(fecha_str):
    if not fecha_str or str(fecha_str).strip() in ("", "nan", "None"):
        return False
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]:
            try:
                fecha = datetime.strptime(str(fecha_str).strip(), fmt)
                return fecha.date() < datetime.now().date()
            except ValueError:
                continue
        return False
    except Exception:
        return False

def calcular_tiempo_espera_str(minutos):
    minutos = int(minutos or 0)
    if minutos < 60:
        return str(minutos) + " min"
    h = minutos // 60
    m = minutos % 60
    return str(h) + "h" + (" " + str(m) + "min" if m else "")

# ── Descarga CSV con 3 navegadores en paralelo ────────────────

def descargar_csv():
    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)
    fecha_ayer, fecha_hoy = get_fechas()
    log.info(f"Descargando reporte {fecha_ayer} -> {fecha_hoy}")

    tiempos       = cargar_tiempos()
    t_login       = calcular_espera(tiempos["login"])
    t_indicadores = calcular_espera(tiempos["indicadores"])
    t_calendario  = calcular_espera(tiempos["calendario"])
    t_datos       = calcular_espera(tiempos["datos"])
    t_descarga    = calcular_espera(tiempos["descarga"])

    # Coordenadas dinámicas según el día de semana en que cae el día 1 del mes actual
    # El calendario del OMS usa lunes=columna1 (x=524) hasta domingo=columna7 (x=840)
    _cols = [524, 577, 630, 683, 735, 788, 840]
    _rows = [240, 290, 340, 390, 440]
    _offset = datetime.now().replace(day=1).weekday()  # 0=lunes, 6=domingo
    coords = {}
    for _d in range(1, 32):
        _cell = _offset + (_d - 1)
        _r, _c = divmod(_cell, 7)
        if _r < len(_rows):
            coords[_d] = (_cols[_c], _rows[_r])

    hoy_dia  = datetime.now().day
    ayer_dia = (datetime.now() - timedelta(days=1)).day
    t0       = time.time()
    destino  = None

    # ── Helpers Flutter-web ───────────────────────────────────────
    def flutter_listo(page, timeout=30000):
        """Espera a que Flutter haya pintado algo en el canvas (flt-glass-pane visible)."""
        page.wait_for_function(
            "() => document.querySelector('flt-glass-pane') !== null",
            timeout=timeout
        )

    def click_y_escribir(page, x, y, texto, timeout_input=8000):
        """
        Flutter web: click en coordenada → espera a que aparezca flt-text-editing
        (el input oculto que Flutter activa) → escribe el texto.
        Si flt-text-editing no aparece, escribe directamente con keyboard.
        """
        page.mouse.click(x, y)
        try:
            page.wait_for_selector("flt-text-editing-host input, .flt-text-editing",
                                   timeout=timeout_input)
            page.keyboard.type(texto, delay=60)
        except Exception:
            # fallback: escribir directamente
            page.keyboard.type(texto, delay=80)

    def esperar_red_quieta(page, timeout=15000):
        """Espera a que no haya peticiones de red activas (Flutter cargó datos)."""
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass  # timeout aceptable, continuar

    # ── Intentos infinitos — reintenta hasta descargar ───────────
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    exito_evento = threading.Event()
    destino      = None
    errores      = {}
    intento_global = 0

    def _un_intento(n, retraso=0):
        """Flujo completo de descarga con retraso opcional antes de arrancar."""
        nonlocal destino
        if retraso > 0:
            # Esperar retraso, pero salir antes si ya hubo éxito
            exito_evento.wait(timeout=retraso)
        if exito_evento.is_set():
            return None

        log.info(f"[#{n}] Iniciando intento...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-extensions", "--no-sandbox", "--disable-dev-shm-usage"]
                )
                try:
                    context = browser.new_context(viewport={"width": 1366, "height": 768},
                                                  accept_downloads=True)
                    page = context.new_page()

                    if exito_evento.is_set(): return None

                    page.goto(LIVERPOOL["url_login"], timeout=60000)
                    page.wait_for_load_state("domcontentloaded")
                    flutter_listo(page, timeout=30000)
                    time.sleep(8)  # Flutter necesita ~8s para renderizar visualmente

                    if exito_evento.is_set(): return None

                    # Login: retry hasta que Flutter active el campo de texto
                    def _click_y_type(x, y, texto, max_r=15):
                        for _ in range(max_r):
                            page.mouse.click(x, y)
                            try:
                                page.wait_for_selector("flt-text-editing-host input", timeout=2500)
                                page.keyboard.type(texto, delay=60)
                                return
                            except Exception:
                                time.sleep(1.5)
                        raise Exception(f"No se pudo activar input en ({x},{y})")

                    _click_y_type(683, 272, LIVERPOOL["usuario"])
                    time.sleep(0.3)
                    _click_y_type(683, 344, LIVERPOOL["password"])
                    time.sleep(0.3)
                    page.mouse.click(683, 480)
                    try:
                        page.wait_for_function(
                            "() => !window.location.href.includes('login')",
                            timeout=30000
                        )
                    except Exception:
                        pass
                    if "login" in page.url:
                        raise Exception("Login fallido")
                    log.info(f"[#{n}] Login OK ✓")

                    if exito_evento.is_set(): return None

                    # Navegar a Indicadores y esperar URL #indicators
                    time.sleep(5)
                    page.mouse.click(683, 114)
                    try:
                        page.wait_for_function(
                            "() => window.location.href.includes('indicators')",
                            timeout=15000
                        )
                    except Exception:
                        pass
                    time.sleep(5)
                    log.info(f"[#{n}] Indicadores OK ✓  URL: {page.url}")

                    if exito_evento.is_set(): return None

                    # Calendario + Guardar = inicia la descarga
                    log.info(f"[#{n}] Descargando...")
                    timeout_ms = max(120000, int(t_descarga * 1000 * 4))
                    with page.expect_download(timeout=timeout_ms) as dl_info:
                        page.mouse.click(1275, 100); time.sleep(4)
                        ax, ay = coords[ayer_dia]; page.mouse.click(ax, ay); time.sleep(1)
                        hx, hy = coords[hoy_dia];  page.mouse.click(hx, hy); time.sleep(1)
                        page.mouse.click(1321, 24)  # Guardar — inicia descarga
                        time.sleep(10)
                    download = dl_info.value

                    if not exito_evento.is_set():
                        exito_evento.set()
                        nombre  = download.suggested_filename
                        dst     = os.path.join(CARPETA_DESCARGA, nombre)
                        download.save_as(dst)
                        destino = dst
                        log.info(f"[#{n}] ✅ Descarga exitosa!")
                        return dst
                    return None

                finally:
                    try: browser.close()
                    except Exception: pass

        except Exception as e:
            if not exito_evento.is_set():
                errores[n] = str(e)[:150]
                log.warning(f"[#{n}] Falló: {errores[n]}")
            return None

    matar_chromium_zombie()
    while not exito_evento.is_set():
        intento_global += 1
        retraso = 0 if intento_global == 1 else 10
        if retraso:
            log.info(f"Reintentando en {retraso}s (intento #{intento_global})...")
            time.sleep(retraso)
        matar_chromium_zombie()
        fut = ThreadPoolExecutor(max_workers=1).submit(_un_intento, intento_global, 0)
        try:
            res = fut.result(timeout=300)
            if res:
                break
        except Exception:
            pass

    if not destino or not os.path.exists(destino):
        raise Exception("Descarga fallida — loop interrumpido inesperadamente")

    t_total = medir(t0)
    log.info(f"Descarga completada en {t_total}s")

    # Borrar archivos anteriores
    try:
        for f in Path(CARPETA_DESCARGA).glob("*.csv"):
            if str(f) != destino:
                f.unlink()
    except Exception:
        pass

    # Actualizar tiempos históricos
    tiempos["login"]       = actualizar_historial(tiempos["login"],       t_login)
    tiempos["indicadores"] = actualizar_historial(tiempos["indicadores"], t_indicadores)
    tiempos["calendario"]  = actualizar_historial(tiempos["calendario"],  t_calendario)
    tiempos["datos"]       = actualizar_historial(tiempos["datos"],        t_datos)
    tiempos["descarga"]    = actualizar_historial(tiempos["descarga"],     t_total)
    guardar_tiempos(tiempos)

    log.info(f"Archivo guardado: {destino} ✅")
    return destino, 1

# ── CSV ───────────────────────────────────────────────────────

def leer_csv(ruta):
    datos = []
    with open(ruta, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0:
                continue
            datos.append([convertir_valor(v) for v in row])

    col_status  = 8
    espera      = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Mercancia en Espera de Entrega")
    etiquetas   = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Etiqueta Generada")
    sin_asignar = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Sin Asignar")
    rechazados  = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Rechazado")

    resumen = {"total": len(datos), "espera": espera, "etiquetas": etiquetas, "sin_asignar": sin_asignar, "rechazados": rechazados}
    log.info(f"CSV leido: {len(datos)} filas ✅")
    return datos, resumen

# ── Descansos ─────────────────────────────────────────────────

def leer_descansos(ss, dir_dict):
    """
    Retorna dos dicts:
      descansos_hoy     = {seccion: nombre_sustituto}   — quién CUBRE
      jefes_en_descanso = {seccion: nombre_jefe_original} — quién DESCANSA
    """
    descansos_hoy     = {}
    jefes_en_descanso = {}
    try:
        hoja    = ss.worksheet("DESCANSOS")
        datos   = hoja.get_all_values()
        hoy_str = datetime.now().strftime("%d/%m/%Y")
        todos_jefes = list(set([info["jefe"] for info in dir_dict.values() if info.get("jefe")]))

        def normalizar_sec(s):
            """Quita .0 y ceros a la izquierda para comparación robusta."""
            s = str(s).strip().replace(".0", "")
            try:
                return str(int(s))
            except Exception:
                return s

        def buscar_nombre_completo(nombre_parcial):
            """Mapea un nombre parcial al nombre completo en DIRECTORIO."""
            n = nombre_parcial.strip().upper()
            # Exacto primero
            for jefe in todos_jefes:
                if n == jefe.strip().upper():
                    return jefe
            # Parcial: el nombre del sheet está contenido en el del directorio
            for jefe in todos_jefes:
                if n in jefe.strip().upper():
                    return jefe
            # Parcial inverso: alguna palabra del directorio en el nombre del sheet
            palabras = set(n.split())
            for jefe in todos_jefes:
                if len(palabras & set(jefe.strip().upper().split())) >= 2:
                    return jefe
            return nombre_parcial  # fallback: devolver como vino

        for row in datos[1:]:
            if not row or len(row) < 4:
                continue
            fecha         = str(row[0]).strip()
            seccion_raw   = str(row[1]).strip()
            jefe_descansa = str(row[2]).strip().upper() if len(row) > 2 else ""
            jefe_cubre    = str(row[3]).strip().upper()

            if fecha != hoy_str or not seccion_raw or not jefe_cubre:
                continue

            seccion = normalizar_sec(seccion_raw)

            sustituto_completo = buscar_nombre_completo(jefe_cubre)
            descansa_completo  = buscar_nombre_completo(jefe_descansa) if jefe_descansa else ""

            descansos_hoy[seccion]     = sustituto_completo
            jefes_en_descanso[seccion] = descansa_completo

            log.info(f"Descanso hoy: sec={seccion} | descansa={descansa_completo} | cubre={sustituto_completo}")

    except gspread.WorksheetNotFound:
        hoja = ss.add_worksheet("DESCANSOS", rows=500, cols=5)
        hoja.update([["Fecha", "Seccion", "Jefe que descansa", "Jefe que cubre"]], "A1")
    except Exception as e:
        log.error(f"Error leyendo DESCANSOS: {e}")

    return descansos_hoy, jefes_en_descanso

# ── Detectar vencidas ─────────────────────────────────────────

def detectar_vencidas(datos, dir_dict, hist_dict, descansos):
    ESTATUS_PENDIENTE = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]
    COL_REMISION=1; COL_DESCRIPCION=3; COL_SECCION=5
    COL_FECHA_ASIG=7; COL_STATUS=8; COL_NOMBRE_VEN=13
    COL_ID_JEFE=15; COL_JEFE=17; COL_TIPO_ENTREGA=19

    vencidas = []
    for row in datos:
        if not row or len(row) <= COL_JEFE:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS_PENDIENTE:
            continue
        fecha_asig = row[COL_FECHA_ASIG] if len(row) > COL_FECHA_ASIG else ""
        minutos    = calcular_minutos(fecha_asig)
        if minutos < MINUTOS_VENCIDA:
            continue

        sec          = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
        remision     = str(row[COL_REMISION]) if len(row) > COL_REMISION else ""
        descripcion  = str(row[COL_DESCRIPCION]) if len(row) > COL_DESCRIPCION else ""
        nom_vendedor = str(row[COL_NOMBRE_VEN]).strip() if len(row) > COL_NOMBRE_VEN else ""
        nom_jefe     = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ""
        nom_seccion  = dir_dict.get(sec, {}).get("nombre_seccion", "")
        tipo_entrega = str(row[COL_TIPO_ENTREGA]).strip() if len(row) > COL_TIPO_ENTREGA else ""
        fuente_jefe  = "CSV"

        if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
            if sec in dir_dict and dir_dict[sec].get("jefe"):
                nom_jefe    = dir_dict[sec]["jefe"]
                fuente_jefe = "DIRECTORIO"
            elif sec in hist_dict and hist_dict[sec].get("Jefe"):
                nom_jefe    = hist_dict[sec]["Jefe"]
                fuente_jefe = "HISTORIAL"

        jefe_sustituto = descansos.get(sec)

        vencidas.append({
            "remision":      remision,
            "descripcion":   descripcion[:35],
            "seccion":       sec,
            "nom_seccion":   nom_seccion,
            "minutos":       minutos,
            "status":        status,
            "nom_vendedor":  nom_vendedor,
            "nom_jefe":      nom_jefe,
            "fuente_jefe":   fuente_jefe,
            "jefe_sustituto":jefe_sustituto,
            "tipo_entrega":  tipo_entrega,
            "de_ayer":       es_de_ayer(fecha_asig),
        })

    log.info(f"Remisiones vencidas (+{MINUTOS_VENCIDA} min): {len(vencidas)}")
    return vencidas

# ── Mensajes espacio REPORTE ──────────────────────────────────

def enviar_apertura(datos, dir_dict, hist_dict):
    fecha_now  = datetime.now().strftime("%d/%m/%Y %H:%M")
    COL_STATUS = 8; COL_FECHA_ASIG = 7; COL_SECCION = 5; COL_JEFE = 17
    ESTATUS    = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    espera_ayer = 0; espera_hoy = 0; etiq_ayer = 0; etiq_hoy = 0
    jefes_ayer  = {}

    for row in datos:
        if not row or len(row) <= COL_JEFE:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS:
            continue
        fecha_str = str(row[COL_FECHA_ASIG]).strip() if len(row) > COL_FECHA_ASIG else ""
        es_ayer   = es_de_ayer(fecha_str)

        if status == "Mercancia en Espera de Entrega":
            if es_ayer: espera_ayer += 1
            else:       espera_hoy  += 1
        elif status == "Etiqueta Generada":
            if es_ayer: etiq_ayer += 1
            else:       etiq_hoy  += 1

        if es_ayer:
            nom_jefe = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ""
            sec      = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
            if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
                nom_jefe = dir_dict.get(sec, {}).get("jefe", "Sin Asignar")
            if nom_jefe not in jefes_ayer:
                jefes_ayer[nom_jefe] = {"count": 0, "secciones": set()}
            jefes_ayer[nom_jefe]["count"] += 1
            if sec:
                jefes_ayer[nom_jefe]["secciones"].add(sec)

    total = espera_ayer + espera_hoy + etiq_ayer + etiq_hoy

    emoji_apertura = random.choice(["🌅", "🌄", "☀️", "🌞", "🏪", "👋"])
    msg_apertura   = random.choice(MENSAJES_APERTURA).format(total=total)

    lineas = [
        emoji_apertura + " *Buenos dias*",
        "",
        msg_apertura,
        "",
        "📊 *Desglose:*",
        "🔴 Mercancia en Espera: *" + str(espera_ayer + espera_hoy) + "*",
        "  📅 De ayer: *" + str(espera_ayer) + "* | De hoy: *" + str(espera_hoy) + "*",
        "🏷️ Etiquetas Generadas: *" + str(etiq_ayer + etiq_hoy) + "*",
        "  📅 De ayer: *" + str(etiq_ayer) + "* | De hoy: *" + str(etiq_hoy) + "*",
    ]

    if jefes_ayer:
        lineas.append("")
        lineas.append("⚠️ *Jefes con pendientes de ayer:*")
        for jefe, info in sorted(jefes_ayer.items(), key=lambda x: -x[1]["count"]):
            mencion  = get_mencion(jefe)
            nom_secs = []
            for s in sorted(info["secciones"]):
                nom = dir_dict.get(s, {}).get("nombre_seccion", "")
                nom_secs.append("Seccion " + s + (" " + nom if nom else ""))
            lineas.append("  " + mencion + " — *" + str(info["count"]) + "* pendientes")
            lineas.append("    📍 " + " | ".join(nom_secs))

    lineas.append("")
    lineas.append("_Liverpool Bot 456 — " + fecha_now + "_")

    mensaje = "\n".join(lineas)
    post_chat_con_reintento(WEBHOOK, {"text": mensaje})
    post_chat_con_reintento(WEBHOOK_JEFES, {"text": mensaje})
    marcar_apertura_enviada()
    log.info("Mensaje de apertura enviado al espacio reporte y jefes ✅")
    # La apertura también devuelve los datos para que el llamador mande el detallado

def enviar_notificaciones_vencidas(vencidas):
    if not vencidas:
        log.info("Sin remisiones vencidas")
        return

    fecha_now    = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_jefe     = {}
    por_vendedor = {}

    for r in vencidas:
        jefe = r["jefe_sustituto"] or r["nom_jefe"] or "SIN ASIGNAR"
        por_jefe[jefe] = por_jefe.get(jefe, 0) + 1
        ven = r["nom_vendedor"] or "SIN ASIGNAR"
        por_vendedor[ven] = por_vendedor.get(ven, 0) + 1

    sin_vendedor  = sum(1 for r in vencidas if not r["nom_vendedor"])
    con_sustituto = sum(1 for r in vencidas if r["jefe_sustituto"])
    desde_dir     = sum(1 for r in vencidas if r["fuente_jefe"] == "DIRECTORIO")
    desde_hist    = sum(1 for r in vencidas if r["fuente_jefe"] == "HISTORIAL")

    lineas = [
        "📊 *Resumen remisiones vencidas — " + fecha_now + "*\n",
        "🔴 Total vencidas: *" + str(len(vencidas)) + "*",
        "👔 Jefes afectados: *" + str(len(por_jefe)) + "*",
        "👤 Vendedores afectados: *" + str(len(por_vendedor)) + "*",
    ]
    if sin_vendedor  > 0: lineas.append("⚠️ Sin vendedor: *" + str(sin_vendedor) + "*")
    if con_sustituto > 0: lineas.append("🔄 Redirigidas a sustituto: *" + str(con_sustituto) + "*")
    if desde_dir     > 0: lineas.append("📋 Jefe desde DIRECTORIO: *" + str(desde_dir) + "*")
    if desde_hist    > 0: lineas.append("📚 Jefe desde HISTORIAL: *" + str(desde_hist) + "*")

    post_chat_con_reintento(WEBHOOK, {"text": "\n".join(lineas)})
    log.info("Notificacion vencidas enviada al espacio reporte ✅")

def enviar_chat(resumen, exito=True, error=""):
    fecha_ayer, fecha_hoy = get_fechas()
    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    if exito:
        try:
            tiempos      = cargar_tiempos()
            info_tiempos = "\n_Tiempos — login:" + str(round(calcular_espera(tiempos["login"]),1)) + "s datos:" + str(round(calcular_espera(tiempos["datos"]),1)) + "s descarga:" + str(round(calcular_espera(tiempos["descarga"]),1)) + "s_"
        except Exception:
            info_tiempos = ""
        texto = ("📊 *Indicadores Liverpool 456*\n"
                 "Actualizacion: " + fecha_now + "\n"
                 "Periodo: " + fecha_ayer + " -> " + fecha_hoy + "\n\n"
                 "Mercancia en Espera: *" + str(resumen.get("espera",0)) + "*\n"
                 "Etiquetas Generadas: *" + str(resumen.get("etiquetas",0)) + "*\n"
                 "Sin Asignar: *" + str(resumen.get("sin_asignar",0)) + "*\n"
                 "Rechazados: *" + str(resumen.get("rechazados",0)) + "*\n"
                 "Total: *" + str(resumen.get("total",0)) + "*\n\n"
                 "Actualizacion completada" + info_tiempos)
    else:
        texto = "Liverpool Bot - Error (" + fecha_now + ")\n" + error
    post_chat_con_reintento(WEBHOOK, {"text": texto})
    log.info("Mensaje indicadores enviado al espacio reporte ✅")

# ── Mensajes espacio JEFES ────────────────────────────────────

def generar_linea_jefe(jefe, info_j):
    """Genera las 2 lineas del jefe: mencion + colores en una linea."""
    verde_jefe    = sum(ds["count"] for ds in info_j["en_tiempo"].values())
    vencidas_jefe = sum(ds["count"] for ds in info_j["vencidas"].values())
    ayer_jefe     = sum(ds["count"] for ds in info_j["de_ayer"].values())
    sin_asignar   = sum(
        ds["sin_vendedor"]
        for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]]
        for ds in grp.values()
    )
    amarillo_jefe = sum(
        ds["count"] for ds in info_j["en_tiempo"].values()
        if ds["max_min"] >= MINUTOS_VENCIDA * 0.75
    )
    verde_puro = verde_jefe - amarillo_jefe
    rojo_jefe  = vencidas_jefe + ayer_jefe

    partes_color = []
    if verde_puro > 0:    partes_color.append("🟢 *" + str(verde_puro) + "* rem")
    if amarillo_jefe > 0: partes_color.append("🟡 *" + str(amarillo_jefe) + "* rem")
    if rojo_jefe > 0:     partes_color.append("🔴 *" + str(rojo_jefe) + "* rem")
    if sin_asignar > 0:   partes_color.append("⚠️ *" + str(sin_asignar) + "* sin asignar")

    lineas = [get_mencion(jefe)]
    if partes_color:
        lineas.append("  ".join(partes_color))
    return lineas


def _buscar_webhook_jefe(nombre):
    """Busca el webhook de un jefe con coincidencia flexible.
    Primero exacta, luego por palabras clave (apellido + primer nombre).
    Evita mandar dos veces al mismo webhook.
    """
    n = nombre.strip().upper()

    # 1. Exacta
    if n in WEBHOOKS_JEFES_CACHE:
        return WEBHOOKS_JEFES_CACHE[n]

    # 2. Flexible: el nombre del bot contiene alguna clave del sheet, o viceversa
    palabras_bot = set(n.split())
    for clave, url in WEBHOOKS_JEFES_CACHE.items():
        if not url:
            continue
        palabras_hoja = set(clave.strip().upper().split())
        # Si comparten al menos 2 palabras (ej. primer nombre + apellido)
        if len(palabras_bot & palabras_hoja) >= 2:
            log.info(f"Webhook flexible: '{nombre}' → '{clave}'")
            return url

    return ""


_WEBHOOKS_YA_ENVIADOS = set()   # evitar duplicados dentro del mismo ciclo

def enviar_mensaje_jefe_individual(jefe, info_j, ubicacion, fecha_now, dir_dict, jefe_original=""):
    """
    Manda al webhook personal del jefe su resumen individual detallado.
    Si el jefe es sustituto:
      - Busca webhook del sustituto primero
      - Si no tiene, usa el webhook del jefe que descansa (fallback)
      - Agrega nota en el mensaje indicando que es sustituto
    El jefe que descansa NO recibe mensaje propio.
    """
    es_sustituto  = info_j.get("es_sustituto", False)
    webhook_jefe  = _buscar_webhook_jefe(jefe)

    if not webhook_jefe and es_sustituto and jefe_original:
        # Fallback: mandar al webhook del jefe que descansa
        webhook_jefe = _buscar_webhook_jefe(jefe_original)
        if webhook_jefe:
            log.info(f"Sustituto '{jefe}' sin webhook propio — usando webhook de '{jefe_original}' (descansa)")

    if not webhook_jefe:
        log.info(f"Sin webhook para {jefe} (ni fallback) — omitiendo mensaje individual")
        return

    # Evitar mandar dos veces al mismo webhook en el mismo ciclo
    if webhook_jefe in _WEBHOOKS_YA_ENVIADOS:
        log.info(f"Webhook de {jefe} ya usado en este ciclo — omitiendo duplicado")
        return
    _WEBHOOKS_YA_ENVIADOS.add(webhook_jefe)

    total_jefe = sum(ds["count"] for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]] for ds in grp.values())

    partes = []
    partes.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")
    partes.append("🏬 *" + ubicacion + "* — " + fecha_now)
    partes.append("")
    if es_sustituto and jefe_original:
        partes.append("🔄 *Sustituto de " + jefe_original.split()[0].capitalize() + "* (descansa hoy)")
    partes.append(get_mencion(jefe))
    partes.append("")

    if info_j["en_tiempo"]:
        partes.append("  ⏰ *En tiempo:*")
        for sec_label, ds in sorted(info_j["en_tiempo"].items()):
            linea = "    " + sec_label + " — " + calcular_tiempo_espera_str(ds["max_min"]) + " | " + str(ds["count"]) + " remisiones"
            if ds["sin_vendedor"] > 0:
                linea += " | ⚠️ *" + str(ds["sin_vendedor"]) + "* sin vendedor"
            partes.append(linea)

    if info_j["vencidas"]:
        partes.append("  🔴 *Vencidas (+20 min):*")
        for sec_label, ds in sorted(info_j["vencidas"].items()):
            linea = "    " + sec_label + " — " + calcular_tiempo_espera_str(ds["max_min"]) + " | " + str(ds["count"]) + " remisiones"
            if ds["sin_vendedor"] > 0:
                linea += " | ⚠️ *" + str(ds["sin_vendedor"]) + "* sin vendedor"
            partes.append(linea)

    if info_j["de_ayer"]:
        partes.append("  📅 *De ayer sin atender:*")
        for sec_label, ds in sorted(info_j["de_ayer"].items()):
            linea = "    " + sec_label + " — " + calcular_tiempo_espera_str(ds["max_min"]) + " | " + str(ds["count"]) + " remisiones"
            if ds["sin_vendedor"] > 0:
                linea += " | ⚠️ *" + str(ds["sin_vendedor"]) + "* sin vendedor"
            partes.append(linea)

    partes.append("  🟢 *Total: " + str(total_jefe) + " remisiones*")
    partes.append("_Liverpool Bot 456 — " + fecha_now + "_")
    partes.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")

    post_chat_con_reintento(webhook_jefe, {"text": "\n".join(partes)})
    log.info(f"Mensaje individual enviado a jefe {jefe}")


def enviar_mensaje_jefes(todas_remisiones, dir_dict, hist_dict, descansos, jefes_en_descanso=None):
    """4 mensajes al espacio jefes — uno por piso con TODAS las remisiones activas."""
    if jefes_en_descanso is None:
        jefes_en_descanso = {}
    ESTATUS_ACTIVOS = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]
    COL_STATUS=8; COL_SECCION=5; COL_FECHA_ASIG=7; COL_JEFE=17
    COL_NOMBRE_VEN=13; COL_TIPO_ENTREGA=19

    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_piso  = {}

    for row in todas_remisiones:
        if not row or len(row) <= COL_JEFE:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS_ACTIVOS:
            continue

        sec_raw      = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
        # Normalizar sección para que haga match con descansos (quitar ceros a la izquierda)
        try:
            sec = str(int(sec_raw))
        except Exception:
            sec = sec_raw
        fecha_asig   = row[COL_FECHA_ASIG] if len(row) > COL_FECHA_ASIG else ""
        minutos      = calcular_minutos(fecha_asig)
        nom_jefe     = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ""
        nom_vendedor = str(row[COL_NOMBRE_VEN]).strip() if len(row) > COL_NOMBRE_VEN else ""
        tipo_entrega = str(row[COL_TIPO_ENTREGA]).strip() if len(row) > COL_TIPO_ENTREGA else ""
        nom_sec      = dir_dict.get(sec, {}).get("nombre_seccion", "") or dir_dict.get(sec_raw, {}).get("nombre_seccion", "")
        sec_label    = "Seccion " + sec + (" " + nom_sec if nom_sec else "")
        es_ayer      = es_de_ayer(fecha_asig)
        vencida      = minutos >= MINUTOS_VENCIDA

        # Normalizar piso desde DIRECTORIO columna F
        ubicacion_raw = dir_dict.get(sec, {}).get("ubicacion", "") or dir_dict.get(sec_raw, {}).get("ubicacion", "") or ""
        p_idx = orden_piso(ubicacion_raw)
        ubicacion = NOMBRES_PISOS.get(p_idx, ubicacion_raw.upper() if ubicacion_raw else "SIN PISO")

        # Buscar jefe original
        if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
            if sec in dir_dict and dir_dict[sec].get("jefe"):
                nom_jefe = dir_dict[sec]["jefe"]
            elif sec in hist_dict and hist_dict[sec].get("Jefe"):
                nom_jefe = hist_dict[sec]["Jefe"]

        # Si la sección tiene sustituto hoy, usar sustituto como "jefe" del grupo
        sustituto = descansos.get(sec) or descansos.get(sec_raw)
        jefe = sustituto or nom_jefe or "SIN ASIGNAR"

        if p_idx not in por_piso:
            por_piso[p_idx] = {"ubicacion": ubicacion, "jefes": {}}
        if jefe not in por_piso[p_idx]["jefes"]:
            # jefe_original = quién descansa (para fallback de webhook)
            jefe_original = nom_jefe if sustituto else ""
            por_piso[p_idx]["jefes"][jefe] = {
                "en_tiempo": {}, "vencidas": {}, "de_ayer": {}, "tipos": [],
                "jefe_original": jefe_original,
                "es_sustituto": bool(sustituto),
            }

        info_j = por_piso[p_idx]["jefes"][jefe]
        if tipo_entrega:
            info_j["tipos"].append(tipo_entrega)

        grp = info_j["de_ayer"] if es_ayer else (info_j["vencidas"] if vencida else info_j["en_tiempo"])

        if sec_label not in grp:
            grp[sec_label] = {"count": 0, "max_min": 0, "sin_vendedor": 0}
        grp[sec_label]["count"]   += 1
        grp[sec_label]["max_min"]  = max(grp[sec_label]["max_min"], minutos)
        if not nom_vendedor:
            grp[sec_label]["sin_vendedor"] += 1

    if not por_piso:
        log.info("Sin remisiones activas para mensaje de jefes")
        return

    for p_idx in sorted(por_piso.keys()):
        info_piso = por_piso[p_idx]
        ubicacion = info_piso["ubicacion"]

        total_piso  = 0
        tipos_piso  = {}
        tiene_tipos = False

        for info_j in info_piso["jefes"].values():
            for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]]:
                for ds in grp.values():
                    total_piso += ds["count"]
            for te in info_j["tipos"]:
                for tp in TIPOS_PRIORIDAD:
                    if tp.lower() in te.lower():
                        tipos_piso[tp] = tipos_piso.get(tp, 0) + 1
                        tiene_tipos = True
                        break

        partes = []
        partes.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")
        partes.append("🏬 *" + ubicacion + "* — " + fecha_now)
        partes.append("")
        partes.append("🟢 menos de 15 min  🟡 15-20 min  🔴 más de 20 min")
        partes.append("")

        for jefe, info_j in sorted(info_piso["jefes"].items()):
            for linea in generar_linea_jefe(jefe, info_j):
                partes.append(linea)
            partes.append("")

        partes.append("📋 *Total " + ubicacion + ": " + str(total_piso) + " remisiones*")
        partes.append("_Liverpool Bot 456 — " + fecha_now + "_")
        partes.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")

        post_chat_con_reintento(WEBHOOK_JEFES, {"text": "\n".join(partes)})
        log.info("Mensaje enviado al espacio jefes — piso: " + ubicacion)

        for jefe, info_j in sorted(info_piso["jefes"].items()):
            jefe_original = info_j.get("jefe_original", "")
            enviar_mensaje_jefe_individual(jefe, info_j, ubicacion, fecha_now, dir_dict, jefe_original=jefe_original)

    log.info("4 mensajes por piso enviados al espacio jefes ✅")

def enviar_cierre(datos, dir_dict):
    fecha_now  = datetime.now().strftime("%d/%m/%Y %H:%M")
    COL_STATUS = 8; COL_FECHA_ASIG = 7; COL_SECCION = 5; COL_JEFE = 17
    ESTATUS    = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    espera = 0; etiq = 0
    jefes_pendientes = {}

    for row in datos:
        if not row or len(row) <= COL_JEFE:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS:
            continue
        if status == "Mercancia en Espera de Entrega": espera += 1
        elif status == "Etiqueta Generada":            etiq   += 1

        fecha_str = str(row[COL_FECHA_ASIG]).strip() if len(row) > COL_FECHA_ASIG else ""
        minutos   = calcular_minutos(fecha_str)
        sec       = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
        nom_jefe  = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ""
        if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
            nom_jefe = dir_dict.get(sec, {}).get("jefe", "Sin Asignar")
        nom_sec   = dir_dict.get(sec, {}).get("nombre_seccion", "")
        sec_key   = "Seccion " + sec + (" " + nom_sec if nom_sec else "")
        if nom_jefe not in jefes_pendientes:
            jefes_pendientes[nom_jefe] = {}
        if sec_key not in jefes_pendientes[nom_jefe]:
            jefes_pendientes[nom_jefe][sec_key] = []
        jefes_pendientes[nom_jefe][sec_key].append(minutos)

    total = espera + etiq
    emoji_cierre = random.choice(["🌙", "🌛", "😴", "🏁", "🌜"])
    msg_cierre   = random.choice(MENSAJES_CIERRE).format(total=total)

    lineas = [
        emoji_cierre + " *Buenas noches*",
        "",
        msg_cierre,
        "",
        "📊 *Resumen de cierre:*",
        "🔴 Mercancia en Espera: *" + str(espera) + "*",
        "🏷️ Etiquetas Generadas: *" + str(etiq) + "*",
    ]

    if jefes_pendientes:
        lineas.append("")
        lineas.append("⚠️ *Al cierre contamos con:*")
        for jefe, secciones in sorted(jefes_pendientes.items()):
            total_jefe = sum(len(v) for v in secciones.values())
            lineas.append("  " + get_mencion(jefe) + " — *" + str(total_jefe) + "* pendientes")
            for sec_key, minutos_list in sorted(secciones.items()):
                lineas.append("    📍 " + sec_key + " — " + calcular_tiempo_espera_str(max(minutos_list)))

    lineas.append("")
    lineas.append("_Liverpool Bot 456 — " + fecha_now + "_")

    # ── Mensaje buenas noches → espacio Jefes ────────────────────────
    post_chat_con_reintento(WEBHOOK_JEFES, {"text": "\n".join(lineas)})
    log.info("Mensaje de cierre enviado al espacio jefes ✅")

    # ── KPI tiempos → espacio Tiempos (simultáneo al cierre) ─────────
    try:
        log.info("Enviando KPI tiempos al espacio Tiempos en cierre...")
        enviar_kpi_jefes_tiempos(dir_dict=dir_dict)
    except Exception as e:
        log.error(f"Error enviando KPI en cierre: {e}")

    marcar_cierre_enviado()


def enviar_pendientes_ayer(datos, dir_dict, descansos=None):
    """
    Manda al espacio JEFES un recordatorio de remisiones de ayer aún sin atender.
    Se llama cada 30 min DESPUÉS del cierre hasta que no queden pendientes.
    Retorna True si había pendientes, False si ya están todos resueltos.
    """
    if descansos is None:
        descansos = {}

    COL_STATUS    = 8; COL_FECHA_ASIG = 7; COL_SECCION = 5; COL_JEFE = 17
    COL_NOMBRE_VEN = 13
    ESTATUS       = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_jefe  = {}

    for row in datos:
        if not row or len(row) <= COL_JEFE:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS:
            continue
        fecha_str = str(row[COL_FECHA_ASIG]).strip() if len(row) > COL_FECHA_ASIG else ""
        if not es_de_ayer(fecha_str):
            continue

        sec       = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
        try: sec = str(int(sec))
        except: pass
        nom_jefe  = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ""
        nom_ven   = str(row[COL_NOMBRE_VEN]).strip() if len(row) > COL_NOMBRE_VEN else ""

        if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
            nom_jefe = dir_dict.get(sec, {}).get("jefe", "Sin Asignar")

        # Aplicar sustituto si hay
        jefe = descansos.get(sec) or nom_jefe or "SIN ASIGNAR"

        nom_sec = dir_dict.get(sec, {}).get("nombre_seccion", "")
        sec_key = "Seccion " + sec + (" " + nom_sec if nom_sec else "")
        minutos = calcular_minutos(fecha_str)

        if jefe not in por_jefe:
            por_jefe[jefe] = {}
        if sec_key not in por_jefe[jefe]:
            por_jefe[jefe][sec_key] = {"count": 0, "max_min": 0, "sin_vendedor": 0}
        por_jefe[jefe][sec_key]["count"]   += 1
        por_jefe[jefe][sec_key]["max_min"]  = max(por_jefe[jefe][sec_key]["max_min"], minutos)
        if not nom_ven:
            por_jefe[jefe][sec_key]["sin_vendedor"] += 1

    if not por_jefe:
        log.info("Pendientes de ayer: ninguno — ya resueltos ✅")
        return False

    total_ayer = sum(d["count"] for secs in por_jefe.values() for d in secs.values())

    lineas = [
        "〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰",
        "📅 *Pendientes de ayer — " + fecha_now + "*",
        "_Remisiones del día anterior que aún no han sido atendidas_",
        "",
        "⚠️ *" + str(total_ayer) + " remisiones de ayer sin resolver:*",
        "",
    ]

    for jefe, secciones in sorted(por_jefe.items(), key=lambda x: -sum(d["count"] for d in x[1].values())):
        total_j = sum(d["count"] for d in secciones.values())
        lineas.append(get_mencion(jefe) + " — *" + str(total_j) + "* pendientes")
        for sec_key, ds in sorted(secciones.items()):
            linea = "    📍 " + sec_key + " — " + calcular_tiempo_espera_str(ds["max_min"]) + " | " + str(ds["count"]) + " rem"
            if ds["sin_vendedor"]:
                linea += " | ⚠️ *" + str(ds["sin_vendedor"]) + "* sin vendedor"
            lineas.append(linea)
        lineas.append("")

    lineas.append("_Liverpool Bot 456 — " + fecha_now + "_")
    lineas.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")

    post_chat_con_reintento(WEBHOOK_JEFES, {"text": "\n".join(lineas)})
    log.info(f"Pendientes de ayer enviados al espacio jefes: {total_ayer} remisiones")
    return True

# ── Sheets ────────────────────────────────────────────────────

def aplicar_formato(ss, hoja_app, num_filas):
    sheet_id = hoja_app.id
    try:
        meta    = ss.fetch_sheet_metadata()
        limpiar = []
        for s in meta["sheets"]:
            if s["properties"]["sheetId"] == sheet_id:
                for b in s.get("bandedRanges", []):
                    limpiar.append({"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}})
                for i in range(len(s.get("conditionalFormats", []))):
                    limpiar.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})
        if limpiar:
            ss.batch_update({"requests": limpiar})
    except Exception:
        pass

    ss.batch_update({"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 2}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 9}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 10}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 9}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.914, "green": 0.118, "blue": 0.549}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 10}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        {"addBanding": {"bandedRange": {"range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": num_filas + 3, "startColumnIndex": 0, "endColumnIndex": 9}, "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1}, "secondBandColor": {"red": 0.97, "green": 0.90, "blue": 0.96}}}}},
        {"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": num_filas + 3, "startColumnIndex": 0, "endColumnIndex": 9}], "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Mercancia en Espera"}]}, "format": {"backgroundColor": {"red": 1.0, "green": 0.85, "blue": 0.6}}}}, "index": 0}},
        {"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": num_filas + 3, "startColumnIndex": 0, "endColumnIndex": 9}], "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Etiqueta Generada"}]}, "format": {"backgroundColor": {"red": 0.72, "green": 0.93, "blue": 0.72}}}}, "index": 1}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 9}}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": num_filas + 3}, "properties": {"pixelSize": 22}, "fields": "pixelSize"}},
    ]})
    log.info("Formato APP 2.0 aplicado ✅")

def actualizar_sheets(gc, datos):
    ss1 = gc.open_by_key(GOOGLE["sheet_id"])

    try:
        hoja1 = ss1.worksheet(GOOGLE["nombre_hoja"])
    except gspread.WorksheetNotFound:
        hoja1 = ss1.add_worksheet(GOOGLE["nombre_hoja"], rows=5000, cols=50)

    datos_limpios = limpiar_datos(datos)
    hoja1.clear()
    hoja1.update(datos_limpios, "A1", value_input_option="RAW")
    log.info("Sheet 1 actualizado ✅")

    ss2 = gc.open_by_key(GOOGLE["sheet2_id"])
    try:
        hoja2 = ss2.worksheet(GOOGLE["sheet2_hoja"])
    except gspread.WorksheetNotFound:
        hoja2 = ss2.add_worksheet(GOOGLE["sheet2_hoja"], rows=5000, cols=50)
    # Borrar rango V2:AS hacia abajo antes de pegar datos nuevos
    try:
        hoja2.batch_clear(["V2:AS5000"])
        log.info("Sheet 2 rango V2:AS limpiado ✅")
    except Exception as e:
        log.warning(f"No se pudo limpiar Sheet 2: {e}")

    hoja2.update(datos_limpios, f"{GOOGLE['sheet2_col']}{GOOGLE['sheet2_fila']}", value_input_option="USER_ENTERED")
    hoja2.update([[datetime.now().strftime("%d/%m/%Y %H:%M:%S")]], f"{GOOGLE['timestamp_col']}{GOOGLE['timestamp_fila']}")
    log.info("Sheet 2 actualizado ✅")

    dir_dict  = {}
    hist_dict = {}
    try:
        for row in ss1.worksheet("DIRECTORIO").get_all_values()[1:]:
            if row and row[0]:
                sec = str(row[0]).strip()
                dir_dict[sec] = {
                    "jefe":          row[2] if len(row) > 2 else "",
                    "nombre_seccion":row[1] if len(row) > 1 else "",
                    "ubicacion":     row[5] if len(row) > 5 else "",  # columna F
                }
    except Exception as e:
        log.warning(f"Error leyendo DIRECTORIO: {e}")

    try:
        for row in ss1.worksheet("HISTORIAL").get_all_values()[1:]:
            if row and row[0]:
                hist_dict[str(row[0]).strip()] = {"Jefe": row[2] if len(row) > 2 else ""}
    except Exception as e:
        log.warning(f"Error leyendo HISTORIAL: {e}")

    descansos, jefes_en_descanso = leer_descansos(ss1, dir_dict)

    try:
        hoja_app = ss1.worksheet("APP 2.0")
    except gspread.WorksheetNotFound:
        hoja_app = ss1.add_worksheet("APP 2.0", rows=5000, cols=15)

    hoja_app.clear()
    ubicaciones = sorted(set([v["ubicacion"] for v in dir_dict.values() if v["ubicacion"]]))
    opciones    = ["Todas"] + ubicaciones
    hoja_app.update([["Filtrar por ubicacion", "", "", "Todas", "", "Haz clic en D1 y selecciona"]], "A1")
    ss1.batch_update({"requests": [{"setDataValidation": {"range": {"sheetId": hoja_app.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": 4}, "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": op} for op in opciones]}, "showCustomUi": True, "strict": False}}}]})
    hoja_app.update([["REMISION","SKU","DESCRIPCION","CANTIDAD","COLABORADOR","SECCION","JEFE","UBICACION","ESTATUS"]], "A2")

    COL_REMISION=1; COL_SKU=2; COL_DESCRIPCION=3; COL_CANTIDAD=4
    COL_SECCION=5;  COL_STATUS=8; COL_COLABORADOR=13; COL_JEFE=17
    ESTATUS_FILTRO = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]

    rows_app = []
    for row in datos:
        if not row or len(row) <= COL_COLABORADOR:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS_FILTRO:
            continue
        sec       = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
        jefe      = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE and row[COL_JEFE] else ""
        ubicacion = dir_dict.get(sec, {}).get("ubicacion", "")
        if not jefe or jefe in ("","nan","Sin Asignar","UNASSIGNED"):
            jefe = dir_dict.get(sec, {}).get("jefe","") or hist_dict.get(sec, {}).get("Jefe","Sin Asignar")
        if sec in descansos:
            jefe = jefe + " -> " + descansos[sec]
        rows_app.append([
            row[COL_REMISION]   if len(row)>COL_REMISION   else "",
            row[COL_SKU]        if len(row)>COL_SKU        else "",
            row[COL_DESCRIPCION]if len(row)>COL_DESCRIPCION else "",
            row[COL_CANTIDAD]   if len(row)>COL_CANTIDAD   else "",
            row[COL_COLABORADOR]if len(row)>COL_COLABORADOR else "",
            sec, jefe, ubicacion, status
        ])

    if rows_app:
        hoja_app.update(rows_app, "A3", value_input_option="RAW")
    try:
        aplicar_formato(ss1, hoja_app, len(rows_app))
    except Exception as e:
        log.error(f"Error formato: {e}")

    log.info(f"APP 2.0 actualizada: {len(rows_app)} filas ✅")

    try:
        from actualizar_directorio import actualizar_directorio_e_historial
        actualizar_directorio_e_historial(gc, GOOGLE["sheet_id"])
    except Exception as e:
        log.error(f"Error directorio: {e}")

    return dir_dict, hist_dict, descansos, jefes_en_descanso

def archivar_monitor_si_necesario(gc):
    """Si MONITOR tiene filas de >180 dias las mueve a hoja MONITOR_ARCHIVO."""
    try:
        ss   = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        hoja = ss.worksheet("MONITOR")
        rows = hoja.get_all_values()
        if len(rows) < 1000:  # solo archivar si ya hay muchos datos
            return
        ahora     = datetime.now()
        hdr       = rows[0]
        recientes = [hdr]
        viejas    = []
        for r in rows[1:]:
            try:
                f = datetime.strptime(r[0], "%d/%m/%Y")
                if (ahora - f).days > 180:
                    viejas.append(r)
                else:
                    recientes.append(r)
            except Exception:
                recientes.append(r)
        if not viejas:
            return
        # Crear o usar hoja archivo
        try:
            archivo = ss.worksheet("MONITOR_ARCHIVO")
        except gspread.WorksheetNotFound:
            archivo = ss.add_worksheet("MONITOR_ARCHIVO", rows=20000, cols=10)
            archivo.update([hdr], "A1")
        archivo.append_rows(viejas, value_input_option="RAW")
        # Reescribir MONITOR solo con recientes
        hoja.clear()
        hoja.update(recientes, "A1", value_input_option="RAW")
        log.info(f"MONITOR archivado: {len(viejas)} filas movidas")
    except Exception as e:
        log.warning(f"Error archivando MONITOR: {e}")

def guardar_en_monitor(gc, exito, duracion, resumen, vencidas_count, intentos=1):
    try:
        ss = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        try:
            hoja = ss.worksheet("MONITOR")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("MONITOR", rows=5000, cols=8)
            hoja.update([["Fecha","Hora","Duracion_seg","Total","Vencidas","Estado","Intentos","Error"]], "A1")
        hoja.append_row([
            datetime.now().strftime("%d/%m/%Y"),
            datetime.now().strftime("%H:%M:%S"),
            round(duracion, 0),
            resumen.get("total", 0),
            vencidas_count,
            "exitosa" if exito else "error",
            intentos,
            "",
        ], value_input_option="RAW")
        log.info("Resultado guardado en MONITOR ✅")
    except Exception as e:
        log.warning("No se pudo guardar en MONITOR: " + str(e))

# ── Tiempos de asignacion ────────────────────────────────────

TIEMPOS_SHEET_ID = "135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js"

def seg_a_str(seg):
    seg = int(seg or 0)
    if seg < 60: return str(seg) + " seg"
    m = seg // 60; s = seg % 60
    return str(m) + " min" + (" " + str(s) + " seg" if s else "")

def calcular_segundos_entre(fecha_h, fecha_jk):
    """Calcula segundos entre columna H y columna J+K."""
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"]:
            try:
                dt1 = datetime.strptime(str(fecha_h).strip().lstrip("'"), fmt)
                break
            except ValueError:
                continue
        else:
            return None
        for fmt2 in ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"]:
            try:
                dt2 = datetime.strptime(str(fecha_jk).strip().lstrip("'"), fmt2)
                break
            except ValueError:
                continue
        else:
            return None
        diff = (dt2 - dt1).total_seconds()
        return diff if diff >= 0 else None
    except Exception:
        return None

def guardar_tiempos_asignacion(gc, datos, dir_dict):
    """Guarda en hoja TIEMPOS del Sheet 1 los tiempos de remisiones sin asignar."""
    COL_STATUS=8; COL_FECHA_ASIG=7; COL_FECHA_STATUS=9; COL_HORA_STATUS=10
    COL_JEFE=17; COL_NOMBRE_VEN=13; COL_SECCION=5

    try:
        ss   = gc.open_by_key(TIEMPOS_SHEET_ID)
        try:
            hoja = ss.worksheet("TIEMPOS")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("TIEMPOS", rows=50000, cols=7)
            hoja.update([["Fecha","Hora","Jefe","Seccion","Segundos","Fecha_H","FechaStatus"]], "A1")
            log.info("Hoja TIEMPOS creada ✅")

        fecha_now = datetime.now().strftime("%d/%m/%Y")
        hora_now  = datetime.now().strftime("%H:%M:%S")
        filas     = []

        for row in datos:
            if not row or len(row) <= COL_JEFE:
                continue
            status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
            if status != "Mercancia en Espera de Entrega":
                continue
            nom_vendedor = str(row[COL_NOMBRE_VEN]).strip() if len(row) > COL_NOMBRE_VEN else ""
            if nom_vendedor and nom_vendedor not in ("", "nan", "Sin Asignar", "UNASSIGNED"):
                continue  # ya tiene vendedor, no contar

            fecha_h      = row[COL_FECHA_ASIG]    if len(row) > COL_FECHA_ASIG    else ""
            fecha_status = str(row[COL_FECHA_STATUS]).strip() if len(row) > COL_FECHA_STATUS else ""
            hora_status  = str(row[COL_HORA_STATUS]).strip()  if len(row) > COL_HORA_STATUS  else ""
            nom_jefe     = str(row[COL_JEFE]).strip()         if len(row) > COL_JEFE         else ""
            sec          = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""

            if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
                nom_jefe = dir_dict.get(sec, {}).get("jefe", "Sin Asignar")

            # Para remisiones sin asignar, el tiempo es desde que cayo (H) hasta ahora
            # No usamos J+K porque Liverpool las actualiza al mismo tiempo que H cuando esta sin asignar
            seg = calcular_minutos(fecha_h) * 60
            if seg <= 0:
                continue  # fecha invalida, saltar

            filas.append([fecha_now, hora_now, nom_jefe, sec, round(seg, 0), str(fecha_h), "sin asignar"])

        if filas:
            hoja.append_rows(filas, value_input_option="RAW")
            log.info(f"TIEMPOS: {len(filas)} registros guardados ✅")
        else:
            log.info("TIEMPOS: sin remisiones sin asignar en este ciclo")

    except Exception as e:
        log.warning(f"Error guardando TIEMPOS: {e}")

def enviar_resumen_tiempos(gc):
    """Lee la hoja TIEMPOS del dia y manda resumen al espacio tiempos."""
    try:
        ss   = gc.open_by_key(TIEMPOS_SHEET_ID)
        hoja = ss.worksheet("TIEMPOS")
        rows = hoja.get_all_values()
        if len(rows) <= 1:
            log.info("TIEMPOS: sin datos para resumen")
            return

        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")

        # Filtrar solo registros de hoy
        por_jefe = {}
        for row in rows[1:]:
            if not row or len(row) < 5:
                continue
            if row[0] != fecha_hoy:
                continue
            nom_jefe = row[2]
            try:
                seg = float(row[4])
            except (ValueError, TypeError):
                continue
            if nom_jefe not in por_jefe:
                por_jefe[nom_jefe] = []
            por_jefe[nom_jefe].append(seg)

        if not por_jefe:
            log.info("TIEMPOS: sin datos de hoy para resumen")
            return

        # Calcular totales
        todos_seg   = [s for segs in por_jefe.values() for s in segs]
        prom_gen    = sum(todos_seg) / len(todos_seg) if todos_seg else 0
        total_gen   = len(todos_seg)

        lineas = [
            "📊 *Resumen del dia — tiempos sin asignar*",
            "_" + fecha_hoy + " · Mercancia en Espera sin vendedor_",
            "",
            "⏱️ Promedio general: *" + seg_a_str(prom_gen) + "*",
            "📦 Total remisiones: *" + str(total_gen) + "*",
            "",
        ]

        for jefe, segs in sorted(por_jefe.items(), key=lambda x: -len(x[1])):
            mencion  = get_mencion(jefe)
            promedio = sum(segs) / len(segs)
            minimo   = min(segs)
            maximo   = max(segs)
            lineas.append(mencion)
            lineas.append("  ⏱️ Promedio: *" + seg_a_str(promedio) + "*")
            lineas.append("  ✅ Mas rapido: *" + seg_a_str(minimo) + "*")
            lineas.append("  🔴 Mas lento: *" + seg_a_str(maximo) + "*")
            lineas.append("  📋 Remisiones: *" + str(len(segs)) + "*")
            lineas.append("")

        lineas.append("_Liverpool Bot 456 — " + fecha_now + "_")

        post_chat_con_reintento(WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("Resumen tiempos enviado al espacio tiempos ✅")

    except gspread.WorksheetNotFound:
        log.info("Hoja TIEMPOS no existe aun")
    except Exception as e:
        log.warning(f"Error enviando resumen tiempos: {e}")

# ── NUEVAS UTILIDADES ───────────────────────────────────────

def verificar_lock():
    """Previene ejecuciones duplicadas."""
    try:
        if os.path.exists(LOCK_FILE):
            mtime = os.path.getmtime(LOCK_FILE)
            # Si el lock tiene mas de 15 min, asumirlo muerto
            if time.time() - mtime < 900:
                return False
        with open(LOCK_FILE, "w") as f:
            f.write(str(datetime.now()))
        return True
    except Exception:
        return True

def liberar_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass

def bot_pausado():
    """Si existe pausa.txt, no mandar mensajes."""
    return os.path.exists(PAUSA_FILE)

def verificar_conexion():
    """Ping rapido a Google antes de abrir Chrome."""
    try:
        r = requests.get("https://www.google.com", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def verificar_webhooks():
    """Verifica que los 3 webhooks respondan con ping vacio."""
    webhooks = {"reporte": WEBHOOK, "jefes": WEBHOOK_JEFES, "tiempos": WEBHOOK_TIEMPOS}
    ok = []
    fail = []
    for nombre, url in webhooks.items():
        try:
            r = requests.post(url, json={"text": ""}, timeout=5)
            if r.status_code in (200, 400):  # 400 es esperado por texto vacio
                ok.append(nombre)
            else:
                fail.append(nombre)
        except Exception:
            fail.append(nombre)
    return ok, fail

def limpiar_logs_viejos():
    """Comprime logs de mas de 7 dias y borra los de mas de 90."""
    try:
        import gzip, shutil
        logs_dir = Path("logs")
        if not logs_dir.exists():
            return
        ahora = time.time()
        for f in logs_dir.glob("*.log"):
            edad_dias = (ahora - f.stat().st_mtime) / 86400
            if edad_dias > 7:
                gz_path = f.with_suffix(".log.gz")
                if not gz_path.exists():
                    with open(f, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    f.unlink()
        for f in logs_dir.glob("*.log.gz"):
            edad_dias = (ahora - f.stat().st_mtime) / 86400
            if edad_dias > 90:
                f.unlink()
    except Exception as e:
        log.warning(f"Error limpiando logs: {e}")

def validar_csv(ruta):
    """Valida que el CSV tenga datos correctos."""
    try:
        with open(ruta, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return False, "CSV vacio"
        if len(rows[0]) < 20:
            return False, f"Columnas insuficientes: {len(rows[0])}"
        # Verificar que haya fechas de hoy en alguna fila
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        tiene_hoy = any(fecha_hoy in str(r) for r in rows[1:100])
        if not tiene_hoy:
            log.warning("CSV no contiene fecha de hoy — puede estar desactualizado")
        return True, f"{len(rows)-1} filas"
    except Exception as e:
        return False, str(e)

def tomar_screenshot(page, nombre):
    """Guarda screenshot en errores."""
    try:
        screenshots = Path("screenshots")
        screenshots.mkdir(exist_ok=True)
        path = screenshots / (datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_" + nombre + ".png")
        page.screenshot(path=str(path))
        log.info(f"Screenshot guardado: {path}")
        # Borrar screenshots viejos (>7 dias)
        ahora = time.time()
        for f in screenshots.glob("*.png"):
            if (ahora - f.stat().st_mtime) / 86400 > 7:
                f.unlink()
    except Exception as e:
        log.warning(f"Error screenshot: {e}")

def matar_chromium_zombie():
    """Mata solo procesos Chromium de Playwright que quedaron colgados.
    NO toca chrome.exe para no cerrar el navegador personal del usuario."""
    try:
        import subprocess
        CREATE_NO_WINDOW = 0x08000000
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "chromium.exe", "/T"],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW
        )
        if "CORRECTO" in result.stdout or "SUCCESS" in result.stdout:
            log.info("Procesos Chromium zombie eliminados")
    except Exception as e:
        log.warning(f"No se pudo limpiar procesos Chromium: {e}")

def limpiar_cache_chrome():
    """Limpia cache de Chromium cada 5 intentos fallidos."""
    try:
        import shutil
        cache_dir = Path.home() / "AppData" / "Local" / "ms-playwright"
        if cache_dir.exists():
            for f in cache_dir.glob("**/Cache*"):
                try: shutil.rmtree(f, ignore_errors=True)
                except: pass
        log.info("Cache Chrome limpiado")
    except Exception:
        pass

def cargar_dir_cache():
    """Carga DIRECTORIO desde cache si no tiene mas de 60 min."""
    try:
        if os.path.exists(DIR_CACHE_FILE):
            mtime = os.path.getmtime(DIR_CACHE_FILE)
            if (time.time() - mtime) / 60 < DIR_CACHE_MINUTOS:
                with open(DIR_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return None

def guardar_dir_cache(dir_dict, hist_dict):
    try:
        with open(DIR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"dir": dir_dict, "hist": hist_dict, "ts": datetime.now().isoformat()}, f)
    except Exception:
        pass

def respaldo_monitor_local(resumen, vencidas, intentos, exito, duracion):
    """Guarda respaldo local del MONITOR."""
    try:
        existe = os.path.exists(MONITOR_BACKUP)
        with open(MONITOR_BACKUP, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if not existe:
                w.writerow(["Fecha","Hora","Duracion","Total","Vencidas","Estado","Intentos"])
            w.writerow([
                datetime.now().strftime("%d/%m/%Y"),
                datetime.now().strftime("%H:%M:%S"),
                round(duracion, 0),
                resumen.get("total", 0),
                vencidas,
                "exitosa" if exito else "error",
                intentos,
            ])
    except Exception:
        pass

def verificar_hora_sistema():
    """Verifica que la hora de la PC no este desfasada."""
    try:
        r = requests.get("https://worldtimeapi.org/api/timezone/America/Mexico_City", timeout=5)
        if r.status_code == 200:
            data    = r.json()
            hora_real = datetime.fromisoformat(data["datetime"].split(".")[0])
            diff_seg  = abs((datetime.now() - hora_real.replace(tzinfo=None)).total_seconds())
            if diff_seg > 300:  # mas de 5 min de diferencia
                log.warning(f"Hora del sistema desfasada: {diff_seg}s")
                return False
        return True
    except Exception:
        return True  # no bloquear por esto

# ── MEJORA 5: WATCHDOG — NOTIFICAR SI NO HA CORRIDO ─────────

def verificar_watchdog(gc):
    """Si la ultima ejecucion exitosa fue hace mas de 30 min, alertar."""
    try:
        ss   = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        hoja = ss.worksheet("MONITOR")
        rows = hoja.get_all_values()
        if len(rows) < 2:
            return
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        exitosas_hoy = [r for r in rows[1:] if r and r[0] == fecha_hoy and r[5] == "exitosa"]
        if not exitosas_hoy:
            return
        ultima = exitosas_hoy[-1]
        hora_str = ultima[0] + " " + ultima[1]
        ultima_dt = datetime.strptime(hora_str, "%d/%m/%Y %H:%M:%S")
        diff_min  = (datetime.now() - ultima_dt).total_seconds() / 60
        if diff_min > 30 and dentro_de_horario():
            fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
            msg = "🚨 *Watchdog — Bot inactivo*\n\nUltima ejecucion exitosa: " + ultima[1] + " (hace " + str(int(diff_min)) + " min)\n\n_Favor de verificar la PC_\n_Liverpool Bot 456 — " + fecha_now + "_"
            post_chat_con_reintento(WEBHOOK, {"text": msg})
            log.warning("⚠️ Watchdog: bot inactivo mas de 30 min")
    except Exception as e:
        log.warning(f"Watchdog error: {e}")

# ── MEJORA 6: HOJA METRICAS DIARIAS ──────────────────────────

def guardar_metricas_dia(gc, resumen, vencidas_count):
    """Guarda resumen diario en hoja METRICAS del Sheet 1."""
    try:
        ss   = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        try:
            hoja = ss.worksheet("METRICAS")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("METRICAS", rows=2000, cols=7)
            hoja.update([["Fecha","Total","MercanciaEspera","Etiquetas","SinAsignar","Vencidas","Hora"]], "A1")

        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        rows = hoja.get_all_values()
        fila_hoy = None
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == fecha_hoy:
                fila_hoy = i
                break

        datos_fila = [
            fecha_hoy,
            resumen.get("total", 0),
            resumen.get("espera", 0),
            resumen.get("etiquetas", 0),
            resumen.get("sin_asignar", 0),
            vencidas_count,
            datetime.now().strftime("%H:%M:%S"),
        ]

        if fila_hoy:
            hoja.update([datos_fila], f"A{fila_hoy}", value_input_option="USER_ENTERED")
        else:
            hoja.append_row(datos_fila, value_input_option="USER_ENTERED")
        log.info("METRICAS actualizado ✅")
    except Exception as e:
        log.warning(f"Error METRICAS: {e}")

# ── MEJORA 7: COMPARATIVA SEMANAL (VIERNES 9:30 PM) ─────────

def es_viernes_cierre():
    ahora = datetime.now()
    return ahora.weekday() == 4 and ahora.hour == HORA_FIN and MINUTO_FIN - 15 <= ahora.minute < MINUTO_FIN

def enviar_comparativa_semanal(gc):
    """Viernes al cierre manda resumen de la semana."""
    try:
        ss   = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        hoja = ss.worksheet("METRICAS")
        rows = hoja.get_all_values()
        if len(rows) < 2:
            return

        hoy = datetime.now()
        lunes = hoy - timedelta(days=hoy.weekday())

        dias_semana = []
        for row in rows[1:]:
            if not row or len(row) < 6:
                continue
            try:
                fecha = datetime.strptime(row[0], "%d/%m/%Y")
                if lunes.date() <= fecha.date() <= hoy.date():
                    dias_semana.append({
                        "fecha": row[0],
                        "dia":   ["Lun","Mar","Mie","Jue","Vie","Sab","Dom"][fecha.weekday()],
                        "total": int(row[1]) if row[1].isdigit() else 0,
                        "vencidas": int(row[5]) if len(row) > 5 and row[5].isdigit() else 0,
                    })
            except Exception:
                continue

        if not dias_semana:
            return

        mejor = min(dias_semana, key=lambda x: x["vencidas"])
        peor  = max(dias_semana, key=lambda x: x["vencidas"])
        prom_venc  = sum(d["vencidas"] for d in dias_semana) / len(dias_semana)
        prom_total = sum(d["total"]    for d in dias_semana) / len(dias_semana)

        fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
        lineas = [
            "📊 *Resumen semanal — Liverpool 456*",
            "Semana del " + lunes.strftime("%d/%m") + " al " + hoy.strftime("%d/%m") + "\n",
            "📈 Promedio remisiones por dia: *" + str(int(prom_total)) + "*",
            "🔴 Promedio vencidas por dia: *" + str(int(prom_venc)) + "*",
            "",
            "🏆 Mejor dia: *" + mejor["dia"] + " " + mejor["fecha"] + "* — " + str(mejor["vencidas"]) + " vencidas",
            "⚠️ Dia con mas vencidas: *" + peor["dia"] + " " + peor["fecha"] + "* — " + str(peor["vencidas"]) + " vencidas",
            "",
            "*Detalle por dia:*",
        ]
        for d in dias_semana:
            lineas.append("  " + d["dia"] + " " + d["fecha"] + " — " + str(d["total"]) + " remisiones | " + str(d["vencidas"]) + " vencidas")

        lineas.append("\n_Liverpool Bot 456 — " + fecha_now + "_")

        post_chat_con_reintento(WEBHOOK, {"text": "\n".join(lineas)})
        post_chat_con_reintento(WEBHOOK_JEFES, {"text": "\n".join(lineas)})
        log.info("Comparativa semanal enviada ✅")
    except Exception as e:
        log.warning(f"Error comparativa semanal: {e}")

# ── MEJORA 1: ALERTAS INTELIGENTES ───────────────────────────

ALERTA_FILE = "alerta_estado.json"

def es_alerta_anomalia(vencidas_actual, gc):
    """Detecta si hay mucho mas vencidas que el promedio del dia."""
    try:
        ss   = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        hoja = ss.worksheet("MONITOR")
        rows = hoja.get_all_values()
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        venc_hoy  = [int(r[4]) for r in rows[1:] if r and r[0] == fecha_hoy and r[4].isdigit()]
        if len(venc_hoy) < 3:
            return False, 0
        promedio = sum(venc_hoy) / len(venc_hoy)
        # Alerta si es 50% mas del promedio del dia
        if vencidas_actual > promedio * 1.5 and vencidas_actual - promedio >= 10:
            return True, promedio
        return False, promedio
    except Exception:
        return False, 0

def mandar_alerta_anomalia(vencidas, promedio):
    """Manda alerta urgente al espacio reporte."""
    # Evitar spam — no mandar si ya se mando hace menos de 30 min
    try:
        if os.path.exists(ALERTA_FILE):
            with open(ALERTA_FILE, "r") as f:
                data = json.load(f)
            if data.get("ultima"):
                dt = datetime.strptime(data["ultima"], "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - dt).total_seconds() < 1800:
                    return
    except Exception:
        pass

    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lineas = [
        "🚨 *ALERTA — Vencidas por encima del promedio*",
        "📅 " + fecha_now,
        "",
        "Actualmente: *" + str(vencidas) + "* remisiones vencidas",
        "Promedio del dia: *" + str(int(promedio)) + "*",
        "Incremento: *+" + str(int(vencidas - promedio)) + "*",
        "",
        "_Se recomienda revisar la operacion de inmediato_",
    ]
    post_chat_con_reintento(WEBHOOK_JEFES, {"text": "\n".join(lineas)})
    try:
        with open(ALERTA_FILE, "w") as f:
            json.dump({"ultima": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass
    log.info("⚠️ Alerta de anomalia enviada al espacio jefes")

# ── MEJORA 2: RANKING AL CIERRE ──────────────────────────────

def enviar_ranking_jefes(gc):
    """Al cierre manda ranking de jefes segun tiempo promedio del dia."""
    try:
        ss   = gc.open_by_key("135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js")
        hoja = ss.worksheet("TIEMPOS")
        rows = hoja.get_all_values()
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")

        por_jefe = {}
        for row in rows[1:]:
            if not row or len(row) < 5 or row[0] != fecha_hoy:
                continue
            jefe = row[2]
            try: seg = float(row[4])
            except: continue
            if jefe not in por_jefe:
                por_jefe[jefe] = []
            por_jefe[jefe].append(seg)

        if len(por_jefe) < 2:
            return

        promedios = [(j, sum(s)/len(s), len(s)) for j, s in por_jefe.items()]
        promedios.sort(key=lambda x: x[1])  # del mas rapido al mas lento

        fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
        lineas = [
            "🏆 *Ranking del dia — Tiempo promedio de asignacion*",
            "📅 " + fecha_hoy,
            "",
        ]

        medallas = ["🥇", "🥈", "🥉"]
        for i, (jefe, prom, count) in enumerate(promedios[:3]):
            medalla = medallas[i] if i < 3 else "  "
            lineas.append(medalla + " *" + jefe + "*")
            lineas.append("   Promedio: *" + seg_a_str(prom) + "* | " + str(count) + " remisiones")
            lineas.append("")

        if len(promedios) > 3:
            lineas.append("_Jefes con mayor tiempo promedio:_")
            for jefe, prom, count in promedios[-3:][::-1]:
                lineas.append("  " + jefe + " — " + seg_a_str(prom))
            lineas.append("")

        lineas.append("_Liverpool Bot 456 — " + fecha_now + "_")

        post_chat_con_reintento(WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("Ranking de jefes enviado ✅")
    except Exception as e:
        log.warning(f"Error enviando ranking: {e}")

# ── MEJORA 3: RECORDATORIO 10 MIN ANTES DEL CIERRE ──────────

RECORDATORIO_FILE = "recordatorio_estado.json"

def es_hora_recordatorio():
    """8:30 PM — 1 hora antes del cierre."""
    ahora = datetime.now()
    return ahora.hour == 20 and 25 <= ahora.minute < 45

def recordatorio_ya_enviado():
    try:
        if os.path.exists(RECORDATORIO_FILE):
            with open(RECORDATORIO_FILE, "r") as f:
                data = json.load(f)
            return data.get("fecha") == datetime.now().strftime("%d/%m/%Y")
    except Exception:
        pass
    return False

def marcar_recordatorio_enviado():
    try:
        with open(RECORDATORIO_FILE, "w") as f:
            json.dump({"fecha": datetime.now().strftime("%d/%m/%Y")}, f)
    except Exception:
        pass

def enviar_recordatorio_cierre(datos, dir_dict):
    """9:20 PM — recordatorio a jefes con pendientes."""
    COL_STATUS=8; COL_SECCION=5; COL_JEFE=17
    ESTATUS = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    jefes_pendientes = {}
    for row in datos:
        if not row or len(row) <= COL_JEFE:
            continue
        status = str(row[COL_STATUS]).strip() if len(row) > COL_STATUS else ""
        if status not in ESTATUS:
            continue
        sec      = str(row[COL_SECCION]).strip().replace(".0","") if len(row) > COL_SECCION else ""
        nom_jefe = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ""
        if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
            nom_jefe = dir_dict.get(sec, {}).get("jefe", "Sin Asignar")
        jefes_pendientes[nom_jefe] = jefes_pendientes.get(nom_jefe, 0) + 1

    if not jefes_pendientes:
        return

    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lineas = [
        "⏰ *Recordatorio — cierre en 1 hora*",
        "",
        "Equipo, la jornada termina a las 9:30 PM. Aun tenemos tiempo para cerrar los pendientes del dia:",
        "",
    ]
    for jefe, count in sorted(jefes_pendientes.items(), key=lambda x: -x[1]):
        lineas.append(get_mencion(jefe) + " — *" + str(count) + "* pendientes")

    lineas.append("")
    lineas.append("_Liverpool Bot 456 — " + fecha_now + "_")

    post_chat_con_reintento(WEBHOOK_JEFES, {"text": "\n".join(lineas)})
    marcar_recordatorio_enviado()
    log.info("Recordatorio de cierre enviado ✅")

# ── MENSAJES PROGRAMADOS ─────────────────────────────────────

def procesar_mensajes_programados(gc):
    """Lee hoja MENSAJES_PROGRAMADOS y envía los mensajes cuyo intervalo ya vencio."""
    try:
        ss = gc.open_by_key(GOOGLE["sheet_id"])
        try:
            hoja = ss.worksheet("MENSAJES_PROGRAMADOS")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("MENSAJES_PROGRAMADOS", rows=200, cols=7)
            hoja.update([["ID", "Texto", "Intervalo_min", "Destino", "Activo", "Ultimo_envio", "Creado"]], "A1")
            log.info("Hoja MENSAJES_PROGRAMADOS creada ✅")
            return

        rows = hoja.get_all_values()
        if len(rows) <= 1:
            return

        ahora = datetime.now()
        WEBHOOKS_DESTINO = {
            "reporte": WEBHOOK,
            "jefes":   WEBHOOK_JEFES,
            "tiempos": WEBHOOK_TIEMPOS,
        }
        FORMATOS_DT = ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]

        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 4:
                continue
            texto      = row[1] if len(row) > 1 else ""
            try:
                intervalo = int(float(row[2])) if len(row) > 2 and row[2] else 0
            except (ValueError, TypeError):
                intervalo = 0
            destino    = (row[3] if len(row) > 3 else "reporte").strip()
            activo     = (row[4] if len(row) > 4 else "si").strip().lower()
            ultimo_env = (row[5] if len(row) > 5 else "").strip()

            if activo not in ("si", "yes", "true", "1"):
                continue
            if not texto.strip() or intervalo <= 0:
                continue

            # Determinar si corresponde enviar
            debe_enviar = False
            if not ultimo_env:
                debe_enviar = True
            else:
                for fmt in FORMATOS_DT:
                    try:
                        ultimo_dt   = datetime.strptime(ultimo_env, fmt)
                        mins_pasados = (ahora - ultimo_dt).total_seconds() / 60
                        debe_enviar  = mins_pasados >= intervalo
                        break
                    except ValueError:
                        continue
                else:
                    debe_enviar = True

            if not debe_enviar:
                continue

            enviado = False
            for dest in [d.strip() for d in destino.split(",")]:
                url = WEBHOOKS_DESTINO.get(dest)
                if url and post_chat_con_reintento(url, {"text": texto}):
                    enviado = True

            if enviado:
                try:
                    hoja.update([[ahora.strftime("%Y-%m-%d %H:%M:%S")]], f"F{i}")
                except Exception as upd_e:
                    log.warning(f"Error actualizando Ultimo_envio fila {i}: {upd_e}")
                log.info(f"Mensaje programado enviado (fila {i}): destino={destino}, cada {intervalo} min")

    except Exception as e:
        log.warning(f"Error procesando mensajes programados: {e}")


# ── Main ──────────────────────────────────────────────────────

def main():
    import sys
    DRY_RUN   = "--dry-run" in sys.argv or "test" in sys.argv
    TEST_MODE = "test" in sys.argv
    FORZAR    = "--forzar" in sys.argv

    log.info("=" * 50)
    if DRY_RUN: log.info("🧪 MODO DRY-RUN — no se mandaran mensajes ni se actualizaran Sheets")
    log.info("Iniciando automatizacion Liverpool")

    # Verificar lock
    if not verificar_lock():
        log.warning("⛔ Ya hay una instancia corriendo — abortando")
        return

    try:
        # Limpieza de logs viejos + reenvío de mensajes encolados
        limpiar_logs_viejos()
        reenviar_cola_mensajes()
        guardar_health("iniciando")

        # Verificar hora del sistema
        if not verificar_hora_sistema():
            log.warning("⚠️ Hora de la PC desfasada")

        # Modo test — solo verificar conexiones
        if TEST_MODE:
            log.info("🧪 Modo test — solo verificando...")
            log.info(f"Conexion internet: {'✅' if verificar_conexion() else '❌'}")
            ok, fail = verificar_webhooks()
            log.info(f"Webhooks OK: {ok}")
            if fail:
                log.warning(f"Webhooks FALLA: {fail}")
            return

        if not dentro_de_horario() and not FORZAR:
            log.info(f"Fuera de horario ({HORA_INICIO}:00 - {HORA_FIN}:{MINUTO_FIN}). Bot en pausa. Usa --forzar para ignorar")
            liberar_lock()
            return
        if FORZAR:
            log.info("⚡ MODO FORZAR — ignorando validacion de horario")

        # Verificar conexion antes de abrir Chrome
        if not verificar_conexion():
            log.warning("Sin conexion a internet — esperando 60s")
            time.sleep(60)
            if not verificar_conexion():
                raise Exception("Sin conexion a internet")

        t_inicio  = time.time()
        gc_global = None

        scopes    = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds     = Credentials.from_service_account_file(GOOGLE["credentials"], scopes=scopes)
        gc_global = gspread.authorize(creds)

        # Cargar config remota desde hoja CONFIG
        cargar_config_remota(gc_global)

        # Registrar PC y verificar si esta pausada individualmente
        if not registrar_y_verificar_pc(gc_global):
            liberar_lock()
            return

        # Cargar webhooks personales de jefes (se repobla con nombres tras leer dir_dict)
        global WEBHOOKS_JEFES_CACHE
        WEBHOOKS_JEFES_CACHE = cargar_webhooks_jefes(gc_global)

        # Verificar si hoy es dia activo
        if not dia_activo_hoy():
            log.info("Hoy no es un dia activo segun CONFIG. Bot en pausa.")
            liberar_lock()
            return

        # Verificar pausa remota (todas las PCs)
        if bot_pausado_remoto():
            log.info("Bot pausado remotamente desde CONFIG. No se ejecuta.")
            liberar_lock()
            return

        ruta, intentos_descarga        = descargar_csv()

        # Validar CSV
        es_valido, info_csv = validar_csv(ruta)
        if not es_valido:
            raise Exception(f"CSV invalido: {info_csv}")
        log.info(f"CSV validado: {info_csv}")

        datos, resumen                 = leer_csv(ruta)

        # Cache del DIRECTORIO
        cache = cargar_dir_cache()
        if cache and not DRY_RUN:
            log.info("Usando cache del DIRECTORIO")
            dir_dict, hist_dict = cache["dir"], cache["hist"]
            # Seguir actualizando sheets pero sin releer DIRECTORIO
            _, _, descansos, jefes_en_descanso = actualizar_sheets(gc_global, datos) if not DRY_RUN else ({}, {}, {}, {})
        else:
            dir_dict, hist_dict, descansos, jefes_en_descanso = actualizar_sheets(gc_global, datos) if not DRY_RUN else ({}, {}, {}, {})
            if not DRY_RUN:
                guardar_dir_cache(dir_dict, hist_dict)

        # Si WEBHOOKS_JEFES estaba vacia, poblarla ahora que tenemos dir_dict
        if not WEBHOOKS_JEFES_CACHE and dir_dict:
            nombres = {
                str(v.get("jefe", "")).strip().upper()
                for v in dir_dict.values()
                if v.get("jefe")
            } - {""}
            if nombres:
                WEBHOOKS_JEFES_CACHE = cargar_webhooks_jefes(gc_global, nombres_jefes=nombres)

        vencidas                       = detectar_vencidas(datos, dir_dict, hist_dict, descansos)
        gc_mod.collect()  # liberar memoria

        # Verificar pausa manual
        if bot_pausado():
            log.info("⏸️ Bot pausado (pausa.txt existe) — no se mandaran mensajes")
            liberar_lock()
            return

        if DRY_RUN:
            log.info(f"🧪 Dry-run: {resumen.get('total',0)} remisiones, {len(vencidas)} vencidas — NO se manda nada")
            liberar_lock()
            return

        # Guardar tiempos de asignacion en hoja TIEMPOS
        guardar_tiempos_asignacion(gc_global, datos, dir_dict)

        # Mensajes programados desde el dashboard
        procesar_mensajes_programados(gc_global)

        # ── CIERRE YA ENVIADO: bot no manda más mensajes normales ───────────────
        if cierre_ya_enviado():
            log.info("Buenas noches ya enviado — bot en espera hasta mañana")
            guardar_health("cierre_enviado")
            liberar_lock()
            return

        # ── APERTURA — primera ejecución del día ─────────────────────────────
        if es_hora_apertura() and not apertura_ya_enviada():
            enviar_apertura(datos, dir_dict, hist_dict)
            enviar_pendientes_ayer(datos, dir_dict, descansos)
            log.info("Primera ejecución: buenos días + pendientes de ayer enviados ✅")

        # ── RECORDATORIO 9:20 PM ──────────────────────────────────────────────
        elif es_hora_recordatorio() and not recordatorio_ya_enviado():
            enviar_recordatorio_cierre(datos, dir_dict)

        # ── CIERRE 9:30 PM ────────────────────────────────────────────────────
        elif es_hora_cierre() and not cierre_ya_enviado():
            # enviar_cierre manda buenas noches a Jefes y KPI tiempos a Tiempos
            enviar_cierre(datos, dir_dict)
            guardar_health("cierre_enviado")
            liberar_lock()
            return  # no mandar más mensajes este ciclo

        # ── KPI vendedores manual (flag creado por el dashboard) ──────────────
        FLAG_KPI_VEN = Path("kpi_vendedores.flag")
        if FLAG_KPI_VEN.exists():
            try:
                FLAG_KPI_VEN.unlink()
                enviar_kpi_vendedores_tiempos(csv_bot=ruta)
            except Exception as e:
                log.error(f"Error KPI vendedores (flag): {e}")

        # ── Verificar anomalia de vencidas ────────────────────────────────────
        es_anom, prom_venc = es_alerta_anomalia(len(vencidas), gc_global)
        if es_anom:
            mandar_alerta_anomalia(len(vencidas), prom_venc)

        # ── Mensajes normales — espacio REPORTE ───────────────────────────────
        enviar_notificaciones_vencidas(vencidas)
        enviar_chat(resumen, exito=True)

        # ── Mensajes por piso cada 30 min — espacio JEFES ────────────────────
        # Incluye pendientes de ayer hasta que no quede ninguno
        contador = leer_contador()
        contador["count"] = contador.get("count", 0) + 1
        if contador["count"] >= 2:
            _WEBHOOKS_YA_ENVIADOS.clear()
            enviar_mensaje_jefes(datos, dir_dict, hist_dict, descansos, jefes_en_descanso)
            enviar_pendientes_ayer(datos, dir_dict, descansos)
            contador["count"] = 0
            log.info("Mensajes por piso + pendientes de ayer enviados, contador reiniciado")
        else:
            log.info("Contador jefes: " + str(contador["count"]) + "/2")
        guardar_contador(contador)

        guardar_metricas_dia(gc_global, resumen, len(vencidas))
        duracion = time.time() - t_inicio
        guardar_en_monitor(gc_global, True, duracion, resumen, len(vencidas), intentos_descarga)
        respaldo_monitor_local(resumen, len(vencidas), intentos_descarga, True, duracion)

        # Verificar max ejecucion
        if duracion > MAX_EJECUCION_SEG:
            log.warning(f"⚠️ Ejecucion tardo {int(duracion)}s (max {MAX_EJECUCION_SEG}s)")

        guardar_health("ok",
            ultima_descarga=datetime.now().isoformat(),
            total_remisiones=resumen.get("total",0),
            vencidas=len(vencidas),
            intentos=intentos_descarga,
            duracion_seg=round(duracion,1))

        # Archivar MONITOR cada 6 meses (>180 dias)
        try:
            archivar_monitor_si_necesario(gc_global)
        except Exception:
            pass

        log.info("Proceso completado con exito ✅")

    except Exception as e:
        log.error(f"Error: {e}")
        guardar_health("error", error=str(e)[:200])
        try:
            duracion = time.time() - t_inicio
        except Exception:
            duracion = 0
        try:
            if gc_global:
                guardar_en_monitor(gc_global, False, duracion, {}, 0)
            respaldo_monitor_local({}, 0, 0, False, duracion)
        except Exception:
            pass
        try:
            enviar_chat({}, exito=False, error=str(e))
        except Exception:
            pass
    finally:
        liberar_lock()

# ── KPI TIEMPOS XD ───────────────────────────────────────────

def descargar_historico_xd(visible=False):
    """Descarga el historico de remisiones de Sistema XD via Playwright.
    visible=True abre el navegador en pantalla (para pruebas).

    Flujo:
      1. Login
      2. Reportes → Historico Remisiones
      3. Llenar Fecha Inicial (ayer) y Fecha Final (hoy) en los inputs
      4. Clic en Buscar → esperar que la tabla cargue
      5. Clic en Exportar / Exportación → capturar descarga
    """
    from config import XD
    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)

    # ── Borrar archivos XD anteriores ──────────────────────────
    for viejo in Path(CARPETA_DESCARGA).glob("historico_xd_*.csv"):
        try:
            viejo.unlink()
            log.info(f"Historico XD anterior eliminado: {viejo.name}")
        except Exception as e:
            log.warning(f"No se pudo borrar {viejo.name}: {e}")

    hoy  = datetime.now()
    ayer = hoy - timedelta(days=1)
    hoy_str  = hoy.strftime("%Y-%m-%d")
    destino  = os.path.join(CARPETA_DESCARGA, f"historico_xd_{hoy_str}.csv")

    # Formato de fecha que usa el XD (se intenta dd/mm/yyyy y yyyy-mm-dd)
    fecha_ini_str = ayer.strftime("%d/%m/%Y")
    fecha_fin_str = hoy.strftime("%d/%m/%Y")
    log.info(f"Descargando historico XD {fecha_ini_str} → {fecha_fin_str}...")

    matar_chromium_zombie()
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not visible,
                slow_mo=400 if visible else 0,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                accept_downloads=True
            )
            page = context.new_page()

            # ── 1. Login ─────────────────────────────────────────
            log.info("XD [1/5]: abriendo login...")
            page.goto(XD["url_login"], timeout=60000)
            page.wait_for_load_state("load", timeout=30000)
            time.sleep(3)

            log.info("XD [2/5]: llenando credenciales...")
            # Ionic Angular bloquea .fill() con autocomplete="off".
            # Usamos el setter nativo + dispatchEvent para forzar change detection.
            page.wait_for_selector("input.native-input", timeout=15000)
            inputs = page.locator("input.native-input")

            def _set_ionic_input(locator, value):
                el = locator.element_handle()
                page.evaluate("""([el, val]) => {
                    var setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", [el, value])

            inputs.nth(0).click()
            time.sleep(0.2)
            _set_ionic_input(inputs.nth(0), XD["usuario"])
            time.sleep(0.3)
            inputs.nth(1).click()
            time.sleep(0.2)
            _set_ionic_input(inputs.nth(1), XD["password"])
            time.sleep(0.5)
            # Botón INICIAR SESIÓN
            page.locator("ion-button, button").filter(has_text="INICIAR").click()
            log.info("XD: credenciales enviadas, esperando login...")
            # SPA Angular — no llega a networkidle; esperamos que desaparezca el login
            try:
                page.wait_for_selector("input.native-input", state="hidden", timeout=15000)
            except Exception:
                pass
            time.sleep(3)

            # ── 2. Navegar a Reportes → Historico Remisiones ─────
            log.info("XD [3/5]: navegando a Historico Remisiones...")
            page.wait_for_selector("text=Reportes", timeout=20000)
            page.get_by_text("Reportes").first.click()
            time.sleep(2)
            page.get_by_text("Historico Remisiones").first.click()
            time.sleep(5)  # dar tiempo al SPA Angular para renderizar

            # ── 3. Cambiar al iframe que contiene el formulario ───
            log.info("XD [4/5]: accediendo al iframe de Historico Remisiones...")
            # El contenido real está en un iframe externo en pro-oms-report-*.run.app
            iframe_loc = page.frame_locator("iframe")
            # Esperar a que el botón Buscar cargue dentro del iframe
            iframe_loc.get_by_text("Buscar", exact=True).wait_for(timeout=20000)
            log.info("XD: iframe cargado ✅")

            # Leer y corregir fechas dentro del iframe
            try:
                inputs_fecha = iframe_loc.locator("input[type='date']")
                n_f = inputs_fecha.count()
                log.info(f"XD: inputs de fecha en iframe: {n_f}")
                if n_f >= 2:
                    fi_val = inputs_fecha.nth(0).input_value() or ""
                    ff_val = inputs_fecha.nth(1).input_value() or ""
                    log.info(f"XD: fechas actuales: '{fi_val}' → '{ff_val}'")
                    fecha_ini_esperada = ayer.strftime("%Y-%m-%d")
                    fecha_fin_esperada = hoy.strftime("%Y-%m-%d")
                    if fi_val != fecha_ini_esperada:
                        inputs_fecha.nth(0).triple_click()
                        inputs_fecha.nth(0).fill(fecha_ini_esperada)
                        log.info(f"XD: fecha inicial → {fecha_ini_esperada}")
                    if ff_val != fecha_fin_esperada:
                        inputs_fecha.nth(1).triple_click()
                        inputs_fecha.nth(1).fill(fecha_fin_esperada)
                        log.info(f"XD: fecha final → {fecha_fin_esperada}")
                else:
                    log.info("XD: usando fechas por defecto del sistema")
            except Exception as e:
                log.warning(f"XD: no se pudieron verificar fechas: {e}")

            time.sleep(0.5)

            # ── 4. Clic en Buscar (dentro del iframe) ─────────────
            log.info("XD: haciendo clic en Buscar...")
            iframe_loc.get_by_text("Buscar", exact=True).click()
            log.info("XD: clic en Buscar ✅")

            # ── 5. Esperar que la búsqueda termine ───────────────
            # El botón cambia a "Cargando..." mientras procesa y vuelve a "Buscar" al terminar.
            log.info("XD: esperando que el botón vuelva a 'Buscar' (señal de carga completa)...")
            try:
                # Primero esperar que aparezca "Cargando..." (confirma que inició la búsqueda)
                iframe_loc.get_by_text("Cargando", exact=False).wait_for(timeout=10000)
                log.info("XD: búsqueda en proceso (Cargando...)...")
            except Exception:
                log.info("XD: no se detectó 'Cargando', continuando...")

            try:
                # Luego esperar que "Cargando" desaparezca y vuelva "Buscar"
                iframe_loc.get_by_text("Buscar", exact=True).wait_for(timeout=120000)
                log.info("XD: carga completada — botón volvió a 'Buscar' ✅")
            except Exception as e:
                log.warning(f"XD: timeout esperando fin de carga: {e}")

            # Leer total de registros para el log
            try:
                txt = iframe_loc.locator("text=/Total Registros/i").first.inner_text(timeout=3000)
                log.info(f"XD: {txt.strip()}")
            except Exception:
                pass
            time.sleep(1)

            # ── 6. Clic en Exportación (dentro del iframe) ────────
            log.info("XD [5/5]: buscando botón Exportación...")
            btn_exportar = None
            for txt_btn in ["Exportación", "EXPORTACIÓN", "Exportar", "EXPORTAR", "Export", "CSV", "Descargar"]:
                try:
                    loc = iframe_loc.get_by_text(txt_btn, exact=True)
                    if loc.count() > 0:
                        btn_exportar = loc.first
                        log.info(f"XD: botón Exportación encontrado: '{txt_btn}'")
                        break
                except Exception:
                    pass

            if not btn_exportar:
                log.error("XD: no se encontró botón de exportación")
                try:
                    shot = os.path.join(CARPETA_DESCARGA, "xd_debug.png")
                    page.screenshot(path=shot)
                    log.info(f"XD: screenshot guardado en {shot}")
                except Exception:
                    pass
                raise Exception("No se encontró botón de exportación en XD")

            log.info("XD: iniciando descarga...")
            with page.expect_download(timeout=180000) as dl_info:
                btn_exportar.click()

            download = dl_info.value
            download.save_as(destino)
            log.info(f"XD: archivo guardado en {destino} ✅")

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    log.info(f"Historico XD descargado correctamente: {destino} ✅")
    return destino


def calcular_kpi_xd(csv_oms, csv_xd, dir_dict=None):
    """
    Calcula KPI de tiempos desde el CSV Historico XD (event log).

    Columnas clave:
      Remision, Estatus, Fecha Estatus, Nombre Empleado, Nombre Asesor C&C, Seccion

    Lógica:
      T1 = Fecha_Hora_Assignacion_Tienda del OMS (cuándo llegó a tienda)
           Si no hay OMS, se usa el primer evento del XD para esa remisión.
      T2 = Fecha Estatus del evento "Asignación" → cuándo el jefe asignó
           Jefe = Nombre Empleado de ese evento
      T3 = Fecha Estatus del evento "Etiqueta Generada" → cuándo el vendedor etiquetó
           Vendedor = Nombre Empleado de ese evento

      Tiempo jefe    = T2 − T1  (cuánto tardó en asignar)
      Tiempo vendedor = T3 − T2  (cuánto tardó en etiquetar)

    Retorna dict con kpi_jefes y kpi_vendedores.
    """
    FECHA_FMTS = [
        "%d/%m/%Y, %H:%M:%S",   # formato XD: "17/05/2026, 15:26:53"
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    def parse_dt(s):
        s = str(s).strip().strip('"').strip("'")
        for fmt in FECHA_FMTS:
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
        return None

    def limpiar(s):
        return str(s).strip().strip('="').strip('"').strip()

    # ── 1. Leer OMS: {remision → (T1, seccion)} ──────────────────────
    anclas = {}  # {remision: {"t1": datetime, "sec": str}}
    if csv_oms:
        try:
            with open(csv_oms, encoding="utf-8-sig", errors="replace") as f:
                reader = csv.reader(f)
                hdrs = [h.strip().lower() for h in (next(reader, []) or [])]
                def cidx(names):
                    for n in names:
                        for i, h in enumerate(hdrs):
                            if n in h: return i
                    return -1
                ir  = cidx(["remision", "remisión", "folio"]);  ir  = ir  if ir  >= 0 else 1
                it  = cidx(["assignacion_tienda", "asignacion_tienda", "fecha_hora_assign"]); it = it if it >= 0 else 7
                ise = cidx(["seccion", "sección"]); ise = ise if ise >= 0 else 5
                for row in reader:
                    if len(row) <= max(ir, it, ise):
                        continue
                    rem = limpiar(row[ir])
                    dt  = parse_dt(row[it])
                    sec = str(row[ise]).strip().replace(".0", "")
                    try: sec = str(int(float(sec)))
                    except: pass
                    if rem and dt:
                        anclas[rem] = {"t1": dt, "sec": sec}
        except Exception as e:
            log.warning(f"calcular_kpi_xd: error leyendo OMS: {e}")

    # ── 2. Leer XD: eventos agrupados por remisión ────────────────────
    grupos = {}  # {remision: [{"est": str, "dt": datetime, "emp": str}]}
    total_filas = 0
    try:
        with open(csv_xd, encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_filas += 1
                rem   = limpiar(row.get("Remision") or "")
                est   = (row.get("Estatus") or "").strip()
                fecha = (row.get("Fecha Estatus") or "").strip()
                emp   = (row.get("Nombre Empleado") or "").strip().upper()
                if not rem or not est or not fecha:
                    continue
                dt = parse_dt(fecha)
                if not dt:
                    continue
                grupos.setdefault(rem, []).append({"est": est, "dt": dt, "emp": emp})
    except Exception as e:
        log.error(f"calcular_kpi_xd: error leyendo XD: {e}")
        return {"kpi_jefes": {}, "kpi_vendedores": {}}

    for rem in grupos:
        grupos[rem].sort(key=lambda x: x["dt"])

    ASIG = {"asignación", "asignacion", "reasignación", "reasignacion"}
    SIST = {"movimiento realizado  por el sistema",
            "movimiento realizado por el sistema", "sistema", "system"}

    # kpi_jefes: {nombre_jefe: {"tiempos": [mins], "total_rem": int, "total_ev": int}}
    kpi_jefes = {}

    comunes = set(anclas.keys()) & set(grupos.keys())
    log.info(f"calcular_kpi_xd: {total_filas} filas XD | {len(anclas)} OMS | {len(comunes)} en común")

    for rem in comunes:
        t1  = anclas[rem]["t1"]
        sec = anclas[rem]["sec"]

        # Buscar jefe por sección en DIRECTORIO
        jefe = ""
        if dir_dict:
            info = dir_dict.get(sec) or dir_dict.get(sec.lstrip("0")) or {}
            jefe = info.get("jefe", "").strip().upper()
        if not jefe:
            continue   # sin jefe en directorio → ignorar

        # Recoger todos los eventos de Asignación/Reasignación (no sistema)
        asig_evs = [e for e in grupos[rem]
                    if e["est"].lower() in ASIG
                    and e["emp"]
                    and e["emp"].lower() not in SIST]

        if not asig_evs:
            continue

        if jefe not in kpi_jefes:
            kpi_jefes[jefe] = {"tiempos": [], "total_rem": 0, "total_ev": 0}

        kpi_jefes[jefe]["total_rem"] += 1

        for ev in asig_evs:
            mins = (ev["dt"] - t1).total_seconds() / 60
            if 0 <= mins < 480:   # sanity: 0-8h
                kpi_jefes[jefe]["tiempos"].append(round(mins, 1))
                kpi_jefes[jefe]["total_ev"] += 1

    # Calcular min / prom / max por jefe
    for d in kpi_jefes.values():
        t = d["tiempos"]
        if t:
            d["min_mins"]  = min(t)
            d["prom_mins"] = round(sum(t) / len(t), 1)
            d["max_mins"]  = max(t)
        else:
            d["min_mins"] = d["prom_mins"] = d["max_mins"] = 0

    log.info(f"KPI XD: {len(kpi_jefes)} jefes con datos")
    return {"kpi_jefes": kpi_jefes, "kpi_vendedores": {}}


def _formato_min(minutos):
    """Convierte minutos decimales a string legible en horas y minutos."""
    total_min = int(round(minutos))
    h, m = divmod(total_min, 60)
    if h > 0:
        return f"{h}h {m}min"
    return f"{m}min"


def _emoji_semaforo(promedio):
    if promedio <= 5:   return "🟢"
    if promedio <= 15:  return "🟡"
    return "🔴"


def _get_csv_oms():
    """Devuelve la ruta al CSV de OMS más reciente descargado."""
    archivos = sorted(Path(CARPETA_DESCARGA).glob("indicadores_*.csv"), reverse=True)
    return str(archivos[0]) if archivos else None


def _get_csv_xd():
    """Devuelve la ruta al CSV de historico XD más reciente."""
    archivos = sorted(Path(CARPETA_DESCARGA).glob("historico_xd_*.csv"), reverse=True)
    return str(archivos[0]) if archivos else None


def enviar_kpi_jefes_tiempos(csv_oms=None, csv_xd=None, dir_dict=None):
    """
    Calcula KPI de tiempos por jefe (del DIRECTORIO de Sheets) y manda al espacio Tiempos.

    Métrica: tiempo desde asignación a tienda (OMS) hasta asignación/reasignación a vendedor (XD)
    Muestra por jefe: más rápido / promedio / más lento
    """
    try:
        # ── CSV OMS ───────────────────────────────────────────────
        if not csv_oms:
            csv_oms = _get_csv_oms()
        if not csv_oms:
            log.warning("KPI jefes: no hay CSV OMS")
            return

        # ── CSV XD ────────────────────────────────────────────────
        if not csv_xd:
            hoy_str = datetime.now().strftime("%Y-%m-%d")
            xd_hoy  = os.path.join(CARPETA_DESCARGA, f"historico_xd_{hoy_str}.csv")
            if os.path.exists(xd_hoy):
                csv_xd = xd_hoy
                log.info(f"KPI jefes: usando XD de hoy: {csv_xd}")
            else:
                log.info("KPI jefes: descargando historico XD...")
                csv_xd = descargar_historico_xd()

        # ── DIRECTORIO ────────────────────────────────────────────
        if not dir_dict:
            try:
                from config import GOOGLE
                import gspread
                from google.oauth2.service_account import Credentials
                creds = Credentials.from_service_account_file(
                    GOOGLE["credentials"],
                    scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
                gc  = gspread.authorize(creds)
                ss  = gc.open_by_key(GOOGLE["sheet_id"])
                dir_dict, _, _, _ = cargar_estructuras_sheets(ss)
                log.info(f"KPI jefes: directorio cargado ({len(dir_dict)} secciones)")
            except Exception as e:
                log.warning(f"KPI jefes: no se pudo cargar directorio: {e}")

        kpi   = calcular_kpi_xd(csv_oms, csv_xd, dir_dict=dir_dict)
        jefes = kpi["kpi_jefes"]

        if not jefes:
            log.info("KPI jefes: sin datos suficientes (verifica DIRECTORIO y CSVs)")
            return

        fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")

        # Mapa inverso: jefe → lista de nombres de sección
        secciones_por_jefe = {}
        if dir_dict:
            for sec, info in dir_dict.items():
                j = info.get("jefe", "").strip().upper()
                ns = info.get("nombre_seccion", "").strip()
                if j and ns:
                    secciones_por_jefe.setdefault(j, []).append(ns)

        # Ordenar por promedio (mejor → peor)
        filas = sorted(jefes.items(), key=lambda x: x[1]["prom_mins"])
        todos_prom = [d["prom_mins"] for _, d in filas]

        SEP = "─────────────────────────"
        lineas = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⏱️ *KPI Tiempos de Asignación*",
            f"📅 {datetime.now().strftime('%d/%m/%Y')}  |  Turno cerrado",
            "_Tiempo desde llegada a tienda hasta asignación a vendedor_",
            "",
        ]

        medallas = ["🥇", "🥈", "🥉"]
        for pos, (jefe, d) in enumerate(filas):
            emoji_sem  = _emoji_semaforo(d["prom_mins"])
            medalla    = medallas[pos] if pos < 3 else f"{pos+1}°"
            nombre     = " ".join(w.capitalize() for w in jefe.split())

            # Secciones a cargo (máximo 4 para no saturar)
            secs = secciones_por_jefe.get(jefe, [])
            secs_txt = ", ".join(secs[:4]) + ("…" if len(secs) > 4 else "")

            bloque = (
                f"{medalla} *{nombre}*  {emoji_sem}\n"
            )
            if secs_txt:
                bloque += f"   🏬 _{secs_txt}_\n"
            bloque += (
                f"   🟢 Más rápido: *{_formato_min(d['min_mins'])}*  "
                f"·  ⏱ Promedio: *{_formato_min(d['prom_mins'])}*  "
                f"·  🔴 Más lento: *{_formato_min(d['max_mins'])}*\n"
                f"   📦 {d['total_rem']} remisiones  ·  🔄 {d['total_ev']} atenciones (asig + reasig)"
            )

            lineas.append(bloque)
            if pos < len(filas) - 1:
                lineas.append(SEP)

        if todos_prom:
            p_gral = round(sum(todos_prom) / len(todos_prom), 1)
            lineas.append("")
            lineas.append(f"📊 *Promedio general del turno: {_formato_min(p_gral)}*")

        lineas.append("")
        lineas.append(f"_Liverpool Bot 456 — {fecha_now}_")
        lineas.append("━━━━━━━━━━━━━━━━━━━━━━━━━")

        post_chat_con_reintento(WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("KPI jefes tiempos enviado al espacio Tiempos ✅")

        # ── Escribir KPI en hoja TIEMPOS del Sheet para el dashboard ──
        try:
            from config import GOOGLE
            creds2 = Credentials.from_service_account_file(
                GOOGLE["credentials"],
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            gc2 = gspread.authorize(creds2)
            ss2 = gc2.open_by_key(GOOGLE["sheet_id"])
            try:
                hoja_t = ss2.worksheet("TIEMPOS")
            except Exception:
                hoja_t = ss2.add_worksheet("TIEMPOS", rows=200, cols=10)

            filas_sheet = [["Jefe", "Min (min)", "Promedio (min)", "Max (min)", "Remisiones", "Fecha"]]
            fecha_hoy   = datetime.now().strftime("%d/%m/%Y %H:%M")
            for jefe, d in sorted(jefes.items(), key=lambda x: x[1]["prom_mins"]):
                filas_sheet.append([
                    jefe.title(),
                    d["min_mins"],
                    d["prom_mins"],
                    d["max_mins"],
                    d["total_rem"],
                    fecha_hoy,
                ])
            hoja_t.clear()
            hoja_t.update(filas_sheet, value_input_option="RAW")
            log.info(f"KPI tiempos escrito en hoja TIEMPOS ({len(jefes)} jefes) ✅")
        except Exception as e2:
            log.warning(f"No se pudo escribir KPI en Sheets: {e2}")

    except Exception as e:
        log.error(f"Error enviando KPI jefes tiempos: {e}")


def enviar_kpi_vendedores_tiempos(csv_oms=None, csv_xd=None):
    """
    Calcula KPI de vendedores (T2→T3: Asignación→Etiqueta Generada)
    y manda mensaje al espacio Tiempos.
    """
    try:
        if not csv_oms:
            csv_oms = _get_csv_oms()
        if not csv_oms:
            log.warning("KPI vendedores: no hay CSV OMS")
            return

        if not csv_xd:
            csv_xd = _get_csv_xd()
        if not csv_xd:
            log.warning("KPI vendedores: no hay CSV XD descargado")
            return

        kpi        = calcular_kpi_xd(csv_oms, csv_xd)
        vendedores = kpi["kpi_vendedores"]

        if not vendedores:
            log.info("KPI vendedores: sin datos suficientes")
            return

        fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
        lineas = [
            "〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰",
            f"🏷️ *KPI Etiquetado por Vendedor — {datetime.now().strftime('%d/%m/%Y')}*",
            "_Desde que se asigna la remisión hasta que genera la etiqueta_\n",
        ]

        filas = sorted(vendedores.items(), key=lambda x: x[1]["promedio_mins"])

        for ven, datos in filas[:20]:  # top 20
            p = datos["promedio_mins"]
            emoji = _emoji_semaforo(p)
            nombre = " ".join(w.capitalize() for w in ven.split()[:2])
            lineas.append(f"{emoji} *{nombre}* — {_formato_min(p)} | {datos['total']} rem")

        lineas.append(f"\n_Liverpool Bot 456 — {fecha_now}_")
        lineas.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")

        post_chat_con_reintento(WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("KPI vendedores tiempos enviado al espacio Tiempos ✅")

    except Exception as e:
        log.error(f"Error enviando KPI vendedores tiempos: {e}")


if __name__ == "__main__":
    import sys
    if "--test-xd" in sys.argv:
        log.info("=== MODO PRUEBA XD ===")
        csv_xd  = descargar_historico_xd(visible=True)
        csv_oms = _get_csv_oms()

        # Cargar DIRECTORIO desde Sheets
        dir_dict = {}
        try:
            from config import GOOGLE
            creds = Credentials.from_service_account_file(
                GOOGLE["credentials"],
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            gc  = gspread.authorize(creds)
            ss  = gc.open_by_key(GOOGLE["sheet_id"])
            dir_dict, _, _, _ = cargar_estructuras_sheets(ss)
            log.info(f"Directorio cargado: {len(dir_dict)} secciones")
        except Exception as e:
            log.warning(f"No se pudo cargar directorio: {e}")

        kpi   = calcular_kpi_xd(csv_oms, csv_xd, dir_dict=dir_dict)
        jefes = kpi["kpi_jefes"]
        log.info(f"KPI jefes ({len(jefes)}):")
        for j, d in sorted(jefes.items(), key=lambda x: x[1]["prom_mins"]):
            log.info(f"  {j}: min={d['min_mins']}m prom={d['prom_mins']}m max={d['max_mins']}m | {d['total_rem']} rem")

        resp = input("Mandar KPI jefes al espacio Tiempos? (s/n): ").strip().lower()
        if resp == "s":
            enviar_kpi_jefes_tiempos(csv_oms=csv_oms, csv_xd=csv_xd, dir_dict=dir_dict)
    else:
        main()


