import os, logging, requests, csv, time, json, random, hashlib
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright
import gspread
from google.oauth2.service_account import Credentials
from config import LIVERPOOL, GOOGLE, CHAT, CARPETA_DESCARGA, PC_NOMBRE

# Token opcional para descargar de un repo PRIVADO. Vive solo en el config.py
# local de cada PC (lo escribe el instalador), nunca en el repo. Si está vacío,
# el bot descarga por la URL pública (funciona mientras el repo sea público).
try:
    from config import GITHUB_TOKEN
except Exception:
    GITHUB_TOKEN = ""

# El bot se lanza desde el Programador de tareas (wscript → pythonw), cuyo
# directorio de trabajo por defecto es C:\Windows\System32, no la carpeta del
# bot. Todos los archivos de estado (bot.lock, apertura.json, cierre.json,
# contador_jefes.json, mensajes_pisos.json, logs/…) usan rutas relativas, así
# que si el CWD cambia entre ejecuciones el bot pierde su estado: no encuentra
# el ID del mensaje a editar y crea uno NUEVO (mensajes dobles), o no ve que la
# apertura ya se envió. Fijar el CWD a la carpeta del script lo hace consistente
# sin importar cómo se haya lanzado.
os.chdir(Path(__file__).resolve().parent)

# ── Alias de módulo: __main__ ⇄ main ─────────────────────────
# El bot se ejecuta como script (`python main.py` / pythonw), así que este
# módulo vive en sys.modules como "__main__". Los submódulos (kpi, sheets,
# mensajes, descarga) hacen `import main` para leer los globales que main()
# muta en tiempo de ejecución (WEBHOOK, CONFIG_REMOTA, contadores, etc.).
# Sin este alias, ese `import main` cargaría una SEGUNDA copia del módulo con
# los valores por defecto (WEBHOOK="") — distinta del __main__ que corre—, y
# los mensajes se enviarían a webhooks vacíos. Aliasar "main" → __main__ hace
# que ambos nombres apunten al mismo objeto y compartan estado.
import sys as _sys_alias
_sys_alias.modules.setdefault("main", _sys_alias.modules[__name__])

VERSION = "1.9.2"

# El auto-update reemplaza main.py (donde vive VERSION) pero NO baja version.txt,
# así que el archivo quedaba desfasado y confundía. Sincronizarlo con VERSION en
# cada arranque lo mantiene fiel (ya se hizo os.chdir, la ruta es la correcta).
try:
    _vf = Path("version.txt")
    if not _vf.exists() or _vf.read_text(encoding="utf-8").strip() != VERSION:
        _vf.write_text(VERSION, encoding="utf-8")
except Exception:
    pass

# ── Auto-update desde GitHub ─────────────────────────────────
_REPO        = "maramirezr04-arch/Argos-Agente-de-Respuesta-y-Gestion-Operativa-de-Sala."
_UPDATE_BASE = "https://raw.githubusercontent.com/" + _REPO + "/main"
# Para repos privados la API necesita el punto codificado como %2E para evitar
# el bug de content-negotiation de Rails que lo striptea y devuelve 404.
_API_BASE    = "https://api.github.com/repos/maramirezr04-arch/Argos-Agente-de-Respuesta-y-Gestion-Operativa-de-Sala%2E/contents"

# Módulos de código que main.py IMPORTA (deben existir en disco antes de
# arrancar). El bootstrap de más abajo los descarga si faltan, y el
# auto-update los baja de forma atómica junto con main.py.
_MODULOS = [
    "utils.py",
    "descarga.py",
    "api_remisiones.py",
    "kpi.py",
    "sheets.py",
    "mensajes.py",
]

# Archivos extra que se actualizan en segundo plano (sin relanzar)
_UPDATE_EXTRA = [
    "demo.py",
    "actualizar_directorio.py",
    "presentacion/demo_live.html",
    "reparador.py",          # watchdog lanzado por la tarea "Argos Reparador"
    "ejecutar_reparar.vbs",  # su lanzador (ahora apunta a reparador.py)
]

def _descargar_repo(rel, timeout=30):
    """Descarga un archivo del repo y devuelve sus bytes (o None si falla).
    Si GITHUB_TOKEN está definido usa la API autenticada (repos privados);
    si no, usa la URL raw pública (funciona mientras el repo sea público)."""
    try:
        if GITHUB_TOKEN:
            headers = {"Authorization": "token " + GITHUB_TOKEN,
                       "Accept": "application/vnd.github.raw",
                       "User-Agent": "argos-bot"}
            r = requests.get(_API_BASE + "/" + rel + "?ref=main", headers=headers, timeout=timeout)
        else:
            r = requests.get(_UPDATE_BASE + "/" + rel, timeout=timeout)
        if r.status_code == 200:
            return r.content
        log.warning(f"Descarga {rel}: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"Descarga {rel} falló: {e}")
    return None

# ── Logging (arriba, para que el bootstrap ya pueda registrar) ──
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

# ── Bootstrap de módulos ──────────────────────────────────────
# main.py importa utils.py (y a futuro otros módulos). Si un módulo no está en
# disco —p.ej. una PC que venía de la versión monolítica y recibió este main.py
# nuevo por el updater viejo— se descarga aquí ANTES de importarlo, evitando que
# el bot quede sin arrancar por un ImportError.
def _bootstrap_modulos():
    for m in _MODULOS:
        if not Path(m).exists():
            log.info(f"Bootstrap: descargando módulo faltante {m}...")
            datos = _descargar_repo(m, timeout=30)
            if datos:
                try:
                    Path(m).write_bytes(datos)
                    log.info(f"Bootstrap: {m} descargado ✅")
                except Exception as e:
                    log.error(f"Bootstrap: no se pudo escribir {m}: {e}")
            else:
                log.error(f"Bootstrap: no se pudo descargar {m} — el bot puede fallar")

_bootstrap_modulos()

# Funciones puras (parseo, pisos, formatos, valores del CSV). Viven en utils.py.
from utils import (
    HISTORIAL_MAX, MARGEN, TIPOS_PRIORIDAD, JEFES_MUJERES,
    parse_fecha_cached, get_fechas, calcular_minutos, es_de_ayer,
    calcular_segundos_entre, calcular_tiempo_espera_str, seg_a_str,
    _formato_min, _emoji_semaforo, medir, convertir_valor, limpiar_datos,
    orden_piso, get_genero, get_mencion, contar_tipos,
    calcular_espera, actualizar_historial,
)

# Descarga del histórico XD (Playwright) — sistema aparte, sigue siendo CSV.
# cargar_tiempos/guardar_tiempos siguen en main y descarga las usa por acceso diferido.
from descarga import (
    tomar_screenshot, matar_chromium_zombie, limpiar_cache_chrome,
    descargar_historico_xd,
)

# Remisiones OMS (Pendientes / Etiqueta Generada / Realizado) por API directa
# en vez de descargar el CSV de Indicadores por navegador. Vive en api_remisiones.py.
from api_remisiones import (
    obtener_remisiones, calcular_resumen, validar_datos,
    verificar_datos_congelados,
)

# KPI de tiempos del Sistema XD. Vive en kpi.py.
from kpi import (
    calcular_kpi_xd, _get_datos_oms, _get_csv_xd,
    enviar_kpi_jefes_tiempos, enviar_kpi_vendedores_tiempos,
)

# Lectura/escritura de Google Sheets. Vive en sheets.py.
from sheets import (
    cargar_estructuras_sheets, leer_descansos, cargar_webhooks_jefes,
    cargar_webhooks_vendedores, registrar_y_verificar_pc, bot_pausado_remoto,
    dia_activo_hoy, aplicar_formato, actualizar_sheets, archivar_monitor_si_necesario,
    guardar_en_monitor, guardar_tiempos_asignacion, enviar_resumen_tiempos,
    guardar_metricas_dia, cargar_dir_cache, guardar_dir_cache, respaldo_monitor_local,
    _validar_url_webhook,
)

# Construcción y envío de mensajes a Google Chat. Vive en mensajes.py.
from mensajes import (
    post_chat_con_reintento, encolar_mensaje, reenviar_cola_mensajes,
    detectar_vencidas, enviar_apertura, enviar_notificaciones_vencidas, enviar_chat,
    _buscar_webhook_jefe, enviar_mensaje_jefe_individual,
    construir_card_piso, _url_chart_pisos, construir_card_resumen_general,
    construir_card_prioridades_grupo,
    _card_total_section, _card_seccion_grp, construir_card_jefe_individual,
    construir_card_apertura, construir_card_cierre, construir_card_pendientes_ayer,
    _leer_mensajes_reescribibles, _guardar_mensajes_reescribibles,
    _construir_texto_vendedor, _enviar_o_reescribir, _construir_por_piso,
    enviar_mensaje_jefes, enviar_mensajes_vendedores, enviar_cierre,
    enviar_reporte_salud, enviar_pendientes_ayer, _buscar_webhook_vendedor,
    es_alerta_anomalia, mandar_alerta_anomalia, enviar_ranking_jefes,
    enviar_comparativa_semanal, enviar_recordatorio_cierre, procesar_mensajes_programados,
)

def _actualizar_extras():
    """Descarga los archivos extra en segundo plano, sin bloquear."""
    raiz = Path(__file__).resolve().parent
    for rel in _UPDATE_EXTRA:
        contenido = _descargar_repo(rel)
        if contenido is None:
            continue
        try:
            destino = raiz / rel
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(contenido)
            log.info(f"🔄 Actualizado: {rel}")
        except Exception as e:
            log.warning(f"No se pudo escribir {rel}: {e}")

def _descargar_extras_faltantes():
    """Descarga solo los extras que aún no existen en disco."""
    raiz      = Path(__file__).resolve().parent
    faltantes = [rel for rel in _UPDATE_EXTRA if not (raiz / rel).exists()]
    if not faltantes:
        return
    log.info(f"Extras faltantes detectados ({len(faltantes)}) — descargando...")
    for rel in faltantes:
        contenido = _descargar_repo(rel)
        if contenido is None:
            continue
        try:
            destino = raiz / rel
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(contenido)
            log.info(f"✅ Descargado: {rel}")
        except Exception as e:
            log.warning(f"No se pudo escribir {rel}: {e}")

def verificar_y_actualizar():
    """
    Compara VERSION local con version.txt del repo.
    Si difiere: descarga main.py, reemplaza el archivo local y relanza el proceso.
    También actualiza los archivos extra en segundo plano.
    Silencioso en caso de error (no interrumpe el bot si hay problema de red).
    """
    import sys as _sys
    # No actualizar en modos de prueba ni demo
    if any(f in _sys.argv for f in ("--dry-run", "test", "--demo", "--no-update")):
        return
    try:
        datos_version = _descargar_repo("version.txt", timeout=8)
        if datos_version is None:
            return
        version_remota = datos_version.decode("utf-8", "ignore").strip()
        if not version_remota or version_remota == VERSION:
            # Sin versión nueva — igual revisa si faltan extras en disco
            _descargar_extras_faltantes()
            return

        log.info(f"🔄 Nueva versión disponible: v{version_remota} (instalada: v{VERSION}). Descargando...")

        # Descarga ATÓMICA: main.py + todos los módulos que main.py importa.
        # Se bajan a memoria, se verifica que TODOS compilen, y solo entonces se
        # escriben en disco. Si algo falla, no se toca nada — la PC nunca queda
        # con un main.py nuevo que importe un módulo viejo o ausente.
        import py_compile, tempfile
        objetivos = ["main.py"] + list(_MODULOS)   # utils.py, etc.
        descargas = {}
        for rel in objetivos:
            contenido = _descargar_repo(rel, timeout=45)
            if contenido is None:
                log.warning(f"No se pudo descargar {rel} — se cancela la actualización")
                return
            descargas[rel] = contenido

        # Verificar que cada archivo .py descargado compila
        for rel, contenido in descargas.items():
            if not rel.endswith(".py"):
                continue
            try:
                with tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False) as _tmp:
                    _tmp.write(contenido)
                    _tmp_path = _tmp.name
                py_compile.compile(_tmp_path, doraise=True)
            except py_compile.PyCompileError as e:
                log.error(f"{rel} descargado NO compila — se cancela la actualización. {e}")
                return
            finally:
                try: os.remove(_tmp_path)
                except Exception: pass

        # Respaldar y escribir TODOS los archivos
        raiz      = Path(__file__).resolve().parent
        respaldos = {}
        for rel, contenido in descargas.items():
            destino = raiz / rel
            if destino.exists():
                respaldos[rel] = destino.read_bytes()
            destino.write_bytes(contenido)

        # Actualizar archivos extra (demo, directorio, etc.) antes de relanzar
        _actualizar_extras()

        # Relanzar la versión nueva y esperar un momento: si arranca y truena de
        # inmediato (import roto, módulo faltante), restaurar TODOS los respaldos
        # para que la PC nunca quede sin un bot funcional.
        log.info(f"✅ Actualizado a v{version_remota} — relanzando...")
        import subprocess
        proc = subprocess.Popen([_sys.executable] + _sys.argv)
        try:
            codigo = proc.wait(timeout=20)
            if codigo and codigo != 0:
                log.error(f"La versión nueva salió con código {codigo} — restaurando respaldos")
                for rel, contenido in respaldos.items():
                    try: (raiz / rel).write_bytes(contenido)
                    except Exception as e: log.error(f"No se pudo restaurar {rel}: {e}")
                return  # seguir corriendo con la versión vieja en este ciclo
        except subprocess.TimeoutExpired:
            pass  # sigue corriendo = arrancó bien, todo OK
        _sys.exit(0)

    except SystemExit:
        raise
    except Exception as e:
        log.warning(f"Auto-update omitido: {e}")

# ── OPTIMIZACIONES — caches y métricas ─────────────────────
import gc as gc_mod
from functools import lru_cache

METRICAS_PASOS = {}  # {paso: [tiempos]}
HEALTH_FILE    = "health.json"
QUEUE_FILE     = "mensajes_pendientes.json"

# ── Estado en vivo para la demostración visual ───────────────
DEMO_ESTADO_FILE = "demo_estado.json"
_DEMO_LIVE       = False   # se activa en main() solo si corre con --demo
_DEMO_CICLO      = 0

def estado_demo(etapa, detalle="", **extra):
    """Escribe la etapa actual del bot para que demo_live.html la ilumine en
    tiempo real durante la demostración. Solo escribe en modo demo; silencioso.
    Etapas: inicio · descarga · sheets · procesa · mensajes · listo."""
    if not _DEMO_LIVE:
        return
    try:
        data = {
            "etapa":   etapa,
            "detalle": detalle,
            "ciclo":   _DEMO_CICLO,
            "ts":      datetime.now().isoformat(),
        }
        data.update(extra)
        with open(DEMO_ESTADO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

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


def _sheets_con_reintento(fn, *args, **kwargs):
    """Ejecuta fn(*args) con hasta 3 reintentos si Google Sheets devuelve 429.
    Respeta el header Retry-After de la respuesta si viene; si no, backoff
    exponencial (10, 20s)."""
    for intento in range(3):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            es_429 = any(k in str(e) for k in ("429", "RESOURCE_EXHAUSTED", "Quota exceeded"))
            if es_429 and intento < 2:
                espera = 10 * (2 ** intento)
                # Preferir Retry-After de Google si está disponible
                try:
                    _resp = getattr(e, "response", None)
                    _ra = _resp.headers.get("Retry-After") if _resp is not None else None
                    if _ra and str(_ra).isdigit():
                        espera = min(int(_ra), 90)
                except Exception:
                    pass
                log.warning(f"Sheets 429 en {fn.__name__} — reintentando en {espera}s")
                time.sleep(espera)
            else:
                raise





# ── Webhooks ──────────────────────────────────────────────────
# Los webhooks de Chat viven en la hoja CONFIG del Sheets (webhook_reporte,
# webhook_jefes, webhook_tiempos) — NO escribirlos aquí: este repo es público.
WEBHOOK         = ""
WEBHOOK_JEFES   = ""
WEBHOOK_TIEMPOS = ""

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
# ── Frecuencia de mensajes — configurable desde dashboard ─────
CICLOS_JEFES       = 2       # ciclos entre mensajes al espacio Jefes
CICLOS_VENDEDORES  = 2       # ciclos entre mensajes individuales a vendedores
CICLOS_REPORTE     = 1       # ciclos entre mensajes al espacio Reporte
HORA_RECORDATORIO  = "20:00" # hora objetivo del recordatorio (HH:MM)
# ── Demo mode ────────────────────────────────────────────────
WEBHOOK_DEMO_1  = ""
WEBHOOK_DEMO_2  = ""
WEBHOOK_DEMO_3  = ""
INTERVALO_DEMO  = 15  # minutos entre ejecuciones demo
# ── Archivos ─────────────────────────────────────────────────
TIEMPOS_FILE      = "tiempos.json"
CONTADOR_FILE     = "contador_jefes.json"
CONTADOR_MSGS_FILE = "contador_mensajes.json"
APERTURA_FILE     = "apertura.json"
CIERRE_FILE       = "cierre.json"
LOCK_FILE         = "bot.lock"
PAUSA_FILE        = "pausa.txt"
CSV_HASH_FILE     = "csv_hash.json"
DIR_CACHE_FILE    = "directorio_cache.json"
MONITOR_BACKUP    = "monitor_backup.csv"
DIR_CACHE_MINUTOS = 60  # actualiza cache DIRECTORIO cada hora

# TIPOS_PRIORIDAD, JEFES_MUJERES, HISTORIAL_MAX y MARGEN viven en utils.py
# (se importan arriba). ORDEN_PISOS/NOMBRES_PISOS se quedan aqu\u00ed porque los usa
# la construcci\u00f3n de mensajes en este archivo.
ORDEN_PISOS = ["PLANTA BAJA", "1er PISO", "2 PISO", "3er PISO"]
NOMBRES_PISOS = {0: "PLANTA BAJA", 1: "1er PISO", 2: "2\u00b0 PISO", 3: "3er PISO"}

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


# ── WEBHOOKS POR VENDEDOR ─────────────────────────────────────
WEBHOOKS_VENDEDORES_CACHE = {}



# ── CONFIG REMOTA (HOJA CONFIG DEL SHEET 1) ──────────────────

CONFIG_REMOTA = {}
CONFIG_CACHE_FILE = "config_remota_cache.json"


def cargar_config_remota(gc):
    """Lee la hoja CONFIG del Sheet 1 y actualiza valores globales."""
    global HORA_INICIO, HORA_FIN, MINUTO_FIN, MINUTOS_VENCIDA, UMBRAL_ANOMALIA, WATCHDOG_MINUTOS, CONFIG_REMOTA, CICLOS_JEFES, CICLOS_VENDEDORES, CICLOS_REPORTE, HORA_RECORDATORIO, WEBHOOK_DEMO_1, WEBHOOK_DEMO_2, WEBHOOK_DEMO_3, INTERVALO_DEMO, WEBHOOK, WEBHOOK_JEFES, WEBHOOK_TIEMPOS
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
                ["ciclos_jefes", "2"],
                ["ciclos_vendedores", "2"],
                ["ciclos_reporte", "1"],
                ["hora_recordatorio", "20:30"],
                ["webhook_demo_1", ""],
                ["webhook_demo_2", ""],
                ["webhook_demo_3", ""],
                ["intervalo_demo", "15"],
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
                _u = float(cfg["umbral_anomalia"])
                # Es un factor multiplicador (1.5 = 50% más). Un valor fuera de
                # [1.0, 5.0] es un error de tipeo (p.ej. "2026"): se ignora.
                if 1.0 <= _u <= 5.0:
                    UMBRAL_ANOMALIA = _u
                else:
                    log.warning(f"umbral_anomalia={_u} fuera de rango [1.0-5.0] — usando {UMBRAL_ANOMALIA}")
        except: pass
        if cfg.get("watchdog_minutos", "").isdigit():
            WATCHDOG_MINUTOS = int(cfg["watchdog_minutos"])
        if cfg.get("ciclos_jefes", "").isdigit():
            CICLOS_JEFES = max(1, int(cfg["ciclos_jefes"]))
        if cfg.get("ciclos_vendedores", "").isdigit():
            CICLOS_VENDEDORES = max(1, int(cfg["ciclos_vendedores"]))
        else:
            # Parametro nuevo: agregarlo a la hoja para que sea visible/editable
            try:
                if "ciclos_vendedores" not in cfg:
                    hoja.append_row(["ciclos_vendedores", "2"], value_input_option="RAW")
            except Exception:
                pass
        if cfg.get("ciclos_reporte", "").isdigit():
            CICLOS_REPORTE = max(1, int(cfg["ciclos_reporte"]))
        if cfg.get("hora_recordatorio"):
            HORA_RECORDATORIO = cfg["hora_recordatorio"].strip()
        # Webhooks de Chat — fuente única: la hoja CONFIG
        for _clave, _var_nombre in [("webhook_reporte", "WEBHOOK"),
                                     ("webhook_jefes",   "WEBHOOK_JEFES"),
                                     ("webhook_tiempos", "WEBHOOK_TIEMPOS")]:
            _url = cfg.get(_clave, "").strip()
            if _url and _validar_url_webhook(_url, _clave):
                if _var_nombre == "WEBHOOK":        WEBHOOK        = _url
                elif _var_nombre == "WEBHOOK_JEFES": WEBHOOK_JEFES  = _url
                else:                                WEBHOOK_TIEMPOS = _url
        if not WEBHOOK or not WEBHOOK_JEFES:
            log.warning("⚠️ Webhooks faltantes en hoja CONFIG (webhook_reporte / webhook_jefes) — los mensajes no se enviarán")
        if cfg.get("webhook_demo_1"):
            WEBHOOK_DEMO_1 = cfg["webhook_demo_1"].strip()
        if cfg.get("webhook_demo_2"):
            WEBHOOK_DEMO_2 = cfg["webhook_demo_2"].strip()
        if cfg.get("webhook_demo_3"):
            WEBHOOK_DEMO_3 = cfg["webhook_demo_3"].strip()
        if cfg.get("intervalo_demo", "").isdigit():
            INTERVALO_DEMO = max(1, int(cfg["intervalo_demo"]))

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
                # Aplicar webhooks desde cache (sin red, sin Sheets)
                if CONFIG_REMOTA.get("webhook_reporte", "").startswith("https://"):
                    WEBHOOK = CONFIG_REMOTA["webhook_reporte"].strip()
                if CONFIG_REMOTA.get("webhook_jefes", "").startswith("https://"):
                    WEBHOOK_JEFES = CONFIG_REMOTA["webhook_jefes"].strip()
                if CONFIG_REMOTA.get("webhook_tiempos", "").startswith("https://"):
                    WEBHOOK_TIEMPOS = CONFIG_REMOTA["webhook_tiempos"].strip()
        except: pass
        return CONFIG_REMOTA




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
    # Ventana amplia: toda la franja de la hora de inicio (p.ej. 10:00–10:59).
    # La descarga del CSV corre ANTES de la verificación de
    # apertura y puede tardar varios minutos, y si la PC arranca tarde el primer
    # ciclo puede caer pasadas las 10:15. La guarda apertura_ya_enviada() evita
    # que se mande dos veces, así que ampliar la ventana es seguro.
    return ahora.hour == HORA_INICIO

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

def leer_contador_msgs():
    try:
        if os.path.exists(CONTADOR_MSGS_FILE):
            with open(CONTADOR_MSGS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def guardar_contador_msgs(c):
    try:
        with open(CONTADOR_MSGS_FILE, "w") as f:
            json.dump(c, f)
    except Exception:
        pass

# ── Utilidades ────────────────────────────────────────────────
# Las funciones puras (get_fechas, convertir_valor, limpiar_datos, orden_piso,
# get_genero, get_mencion, contar_tipos, calcular_minutos, es_de_ayer,
# calcular_tiempo_espera_str) viven ahora en utils.py y se importan arriba.

# ── Descarga CSV (un navegador con reintentos en serie) ───────

# ── CSV ───────────────────────────────────────────────────────

# ── Descansos ─────────────────────────────────────────────────


# ── Detectar vencidas ─────────────────────────────────────────


# ── Mensajes espacio REPORTE ──────────────────────────────────

    # La apertura también devuelve los datos para que el llamador mande el detallado



# ── Mensajes espacio JEFES ────────────────────────────────────

_CC_KEYS = ("CC0D", "CC1D", "C&C")   # palabras clave para detectar C&C


# Tipos que requieren alerta de prioridad (columna W del CSV)
def _categoria_urgente(tipo_entrega, tipo_orden):
    """
    Devuelve la etiqueta de categoría urgente si la remisión aplica, o None
    si no aplica ninguna. Regla confirmada (solo estas 4, nada más):
      - TipoEntrega empieza con "HD0D" (mismo día) o "HD1D" (mañana), O
      - TipoOrden es exactamente "C&C" o "C&C Expreso"
    """
    te = str(tipo_entrega or "").strip().upper()
    to = str(tipo_orden or "").strip().lower()
    if te.startswith("HD0D"):
        return "HD0D - Mismo día"
    if te.startswith("HD1D"):
        return "HD1D - Mañana"
    if to == "c&c expreso":
        return "C&C Expreso"
    if to == "c&c":
        return "C&C"
    return None





_WEBHOOKS_YA_ENVIADOS = set()   # evitar duplicados dentro del mismo ciclo









# ── Helpers compartidos para Cards v2 ────────────────────────












# ── Mensajes que se reescriben en lugar (pisos y jefes) ──────────
MENSAJES_PISOS_FILE      = "mensajes_pisos.json"
MENSAJES_JEFES_IND_FILE  = "mensajes_jefes_ind.json"















# ── Sheets ────────────────────────────────────────────────────





# ── Tiempos de asignacion ────────────────────────────────────

TIEMPOS_SHEET_ID = GOOGLE["sheet_id"]



# ── NUEVAS UTILIDADES ───────────────────────────────────────

def verificar_lock():
    """Previene ejecuciones duplicadas.

    Creación ATÓMICA del lock con O_CREAT|O_EXCL: si dos instancias arrancan en
    el mismo instante (p.ej. dos tareas del Programador disparando a la vez),
    solo UNA logra crear el archivo; la otra recibe FileExistsError y aborta.
    El open() con truncado del código anterior tenía una carrera (ambas pasaban
    el os.path.exists y ambas seguían), causando ejecuciones dobles."""
    try:
        # Lock viejo (>15 min) = instancia muerta: borrarlo y reintentar
        if os.path.exists(LOCK_FILE):
            try:
                if time.time() - os.path.getmtime(LOCK_FILE) < 900:
                    return False
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass  # otro proceso lo quitó justo ahora
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(datetime.now()).encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False  # otra instancia ganó la carrera
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

def _verificar_csv_congelado(ruta):
    """Detecta si el CSV lleva 3+ ciclos sin cambios y alerta al chat de reporte."""
    try:
        with open(ruta, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()
        estado = {}
        if os.path.exists(CSV_HASH_FILE):
            with open(CSV_HASH_FILE) as f:
                estado = json.load(f)
        if estado.get("hash") == h:
            estado["ciclos_igual"] = estado.get("ciclos_igual", 1) + 1
        else:
            estado = {"hash": h, "ciclos_igual": 1, "alerta_enviada": False}
        with open(CSV_HASH_FILE, "w") as f:
            json.dump(estado, f)
        if estado["ciclos_igual"] >= 3 and not estado.get("alerta_enviada"):
            fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
            msg = (f"⚠️ *Argos — Datos posiblemente congelados*\n\n"
                   f"El CSV de OMS lleva *{estado['ciclos_igual']} ciclos* consecutivos "
                   f"sin cambios. Es posible que el sistema OMS no esté actualizando.\n\n"
                   f"_Verificar OMS manualmente_\n_{fecha_now}_")
            post_chat_con_reintento(WEBHOOK, {"text": msg})
            estado["alerta_enviada"] = True
            with open(CSV_HASH_FILE, "w") as f:
                json.dump(estado, f)
            log.warning(f"⚠️ CSV congelado: {estado['ciclos_igual']} ciclos sin cambios — alerta enviada")
    except Exception as e:
        log.warning(f"Error verificando hash CSV: {e}")




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

# ── MEJORA 6: HOJA METRICAS DIARIAS ──────────────────────────


# ── MEJORA 7: COMPARATIVA SEMANAL (VIERNES 9:30 PM) ─────────

def es_viernes_cierre():
    ahora = datetime.now()
    return ahora.weekday() == 4 and ahora.hour == HORA_FIN and MINUTO_FIN - 15 <= ahora.minute < MINUTO_FIN


# ── MEJORA 1: ALERTAS INTELIGENTES ───────────────────────────

ALERTA_FILE = "alerta_estado.json"



# ── MEJORA 2: RANKING AL CIERRE ──────────────────────────────


# ── MEJORA 3: RECORDATORIO 10 MIN ANTES DEL CIERRE ──────────

RECORDATORIO_FILE = "recordatorio_estado.json"

def es_hora_recordatorio():
    """Verifica si el ciclo actual cae dentro de la ventana del recordatorio configurable."""
    ahora = datetime.now()
    try:
        partes   = HORA_RECORDATORIO.split(":")
        objetivo = ahora.replace(hour=int(partes[0]), minute=int(partes[1]), second=0, microsecond=0)
        mins_diff = (ahora - objetivo).total_seconds() / 60
        return 0 <= mins_diff < 20  # ventana de 20 min para no perderse el ciclo
    except Exception:
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


# ── MENSAJES PROGRAMADOS ─────────────────────────────────────



# ── Main ──────────────────────────────────────────────────────

def main():
    import sys
    DRY_RUN   = "--dry-run" in sys.argv or "test" in sys.argv
    TEST_MODE = "test" in sys.argv
    FORZAR    = "--forzar" in sys.argv
    DEMO_MODE = "--demo" in sys.argv

    global _DEMO_LIVE, _DEMO_CICLO
    if DEMO_MODE:
        _DEMO_LIVE = True
        try:
            _DEMO_CICLO = int(os.environ.get("ARGOS_DEMO_CICLO", "0"))
        except Exception:
            _DEMO_CICLO = 0
        estado_demo("inicio", "Argos arrancando...")

    log.info("=" * 50)
    if DRY_RUN: log.info("🧪 MODO DRY-RUN — no se mandaran mensajes ni se actualizaran Sheets")
    log.info("Iniciando Argos")

    # Auto-update — verifica versión y relanza si hay nueva antes de continuar
    verificar_y_actualizar()

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
            # Cargar los webhooks desde la hoja CONFIG antes de verificarlos;
            # de lo contrario WEBHOOK/WEBHOOK_JEFES/WEBHOOK_TIEMPOS están vacíos
            # y el test siempre reportaría los 3 como FALLA.
            try:
                scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                creds  = Credentials.from_service_account_file(GOOGLE["credentials"], scopes=scopes)
                _gc    = gspread.authorize(creds)
                cargar_config_remota(_gc)
                log.info("CONFIG cargada para verificación de webhooks ✅")
            except Exception as e:
                log.warning(f"No se pudo cargar CONFIG (webhooks quedarán vacíos): {e}")
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
        _sheets_con_reintento(cargar_config_remota, gc_global)

        # Modo demo — redirigir webhooks y abrir browser visible
        if DEMO_MODE:
            global WEBHOOK, WEBHOOK_JEFES, WEBHOOK_TIEMPOS
            log.info("🎯 MODO DEMO activado — webhooks redirigidos a canales demo")
            if WEBHOOK_DEMO_1:
                WEBHOOK = WEBHOOK_DEMO_1
                log.info(f"  WEBHOOK → demo_1")
            if WEBHOOK_DEMO_2:
                WEBHOOK_JEFES = WEBHOOK_DEMO_2
                log.info(f"  WEBHOOK_JEFES → demo_2")
            if WEBHOOK_DEMO_3:
                WEBHOOK_TIEMPOS = WEBHOOK_DEMO_3
                log.info(f"  WEBHOOK_TIEMPOS → demo_3")

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

        estado_demo("descarga", "Abriendo OMS y llamando la API de remisiones...")
        datos, intentos_descarga       = obtener_remisiones(visible=DEMO_MODE)

        # Validar respuesta de la API
        es_valido, info_datos = validar_datos(datos)
        if not es_valido:
            raise Exception(f"Datos invalidos: {info_datos}")
        log.info(f"Datos de API validados: {info_datos}")
        verificar_datos_congelados(datos, post_chat_con_reintento, WEBHOOK)

        resumen = calcular_resumen(datos)
        estado_demo("sheets", "Volcando " + str(resumen.get("total", 0)) + " remisiones a Google Sheets...",
                    total=resumen.get("total", 0))

        # DIRECTORIO: actualizar_sheets ya lo lee fresco del Sheet, así que
        # usamos SIEMPRE ese resultado (refleja cambios manuales de inmediato).
        # El cache solo sirve de respaldo si la lectura fresca viene vacía.
        dir_dict, hist_dict, descansos, jefes_en_descanso, prioridades = (
            _sheets_con_reintento(actualizar_sheets, gc_global, datos)
            if not DRY_RUN else ({}, {}, {}, {}, [])
        )
        if not DRY_RUN:
            if dir_dict:
                guardar_dir_cache(dir_dict, hist_dict)
            else:
                # Lectura fresca vacía (error de Sheets): caer al último cache bueno
                cache = cargar_dir_cache()
                if cache:
                    log.warning("DIRECTORIO vacío en lectura fresca — usando cache de respaldo")
                    dir_dict, hist_dict = cache["dir"], cache["hist"]

        # Si WEBHOOKS_JEFES estaba vacia, poblarla ahora que tenemos dir_dict
        if not WEBHOOKS_JEFES_CACHE and dir_dict:
            nombres = {
                str(v.get("jefe", "")).strip().upper()
                for v in dir_dict.values()
                if v.get("jefe")
            } - {""}
            if nombres:
                WEBHOOKS_JEFES_CACHE = cargar_webhooks_jefes(gc_global, nombres_jefes=nombres)

        # Cargar/autollenar webhooks de vendedores con los nombres de la API
        global WEBHOOKS_VENDEDORES_CACHE
        nombres_ven = {
            str(r.get("NombreVendedor", "")).strip() for r in datos
            if str(r.get("NombreVendedor", "")).strip()
            and str(r.get("NombreVendedor", "")).strip() not in ("nan", "Sin Asignar", "Sin Seccion", "UNASSIGNED")
        }
        WEBHOOKS_VENDEDORES_CACHE = cargar_webhooks_vendedores(gc_global, nombres_vendedores=nombres_ven)

        estado_demo("procesa", "Detectando vencidas, calculando tiempos y priorizando C&C / XD Expreso...",
                    total=resumen.get("total", 0))
        vencidas                       = detectar_vencidas(datos, dir_dict, hist_dict, descansos)
        estado_demo("procesa", str(len(vencidas)) + " remisiones vencidas detectadas",
                    total=resumen.get("total", 0), vencidas=len(vencidas))
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
            enviar_apertura(datos, dir_dict, hist_dict, descansos)
            # Reiniciar contadores diarios para el reporte de salud
            _c = leer_contador()
            _c["ciclos_dia"] = 0
            _c["errores_dia"] = 0
            guardar_contador(_c)
            log.info("Primera ejecución: buenos días + pendientes de ayer enviados ✅")

        # ── RECORDATORIO 9:20 PM ──────────────────────────────────────────────
        elif es_hora_recordatorio() and not recordatorio_ya_enviado():
            enviar_recordatorio_cierre(datos, dir_dict, hist_dict, descansos)

        # ── CIERRE 9:30 PM ────────────────────────────────────────────────────
        elif es_hora_cierre() and not cierre_ya_enviado():
            # enviar_cierre manda buenas noches a Jefes y KPI tiempos a Tiempos
            enviar_cierre(datos, dir_dict, hist_dict, descansos)
            try:
                enviar_reporte_salud(resumen, vencidas)
            except Exception as e:
                log.error(f"Error enviando reporte de salud: {e}")
            guardar_health("cierre_enviado")
            liberar_lock()
            return  # no mandar más mensajes este ciclo

        # ── KPI vendedores manual (flag creado por el dashboard) ──────────────
        FLAG_KPI_VEN = Path("kpi_vendedores.flag")
        if FLAG_KPI_VEN.exists():
            try:
                FLAG_KPI_VEN.unlink()
                enviar_kpi_vendedores_tiempos(datos_oms=datos)
            except Exception as e:
                log.error(f"Error KPI vendedores (flag): {e}")

        # ── Verificar anomalia de vencidas ────────────────────────────────────
        es_anom, prom_venc = es_alerta_anomalia(len(vencidas), gc_global)
        if es_anom:
            mandar_alerta_anomalia(len(vencidas), prom_venc)

        # ── Mensajes — espacio REPORTE ────────────────────────────────────────
        estado_demo("mensajes", "Enviando avisos a Google Chat: jefes, vendedores y reporte...",
                    total=resumen.get("total", 0), vencidas=len(vencidas))
        enviar_notificaciones_vencidas(vencidas)
        contador = leer_contador()
        contador["ciclos_dia"] = contador.get("ciclos_dia", 0) + 1
        contador["reporte_count"] = contador.get("reporte_count", 0) + 1
        if contador["reporte_count"] >= CICLOS_REPORTE:
            enviar_chat(resumen, exito=True)
            contador["reporte_count"] = 0
            log.info(f"Reporte enviado, contador reiniciado (cada {CICLOS_REPORTE} ciclo(s))")
        else:
            log.info(f"Contador reporte: {contador['reporte_count']}/{CICLOS_REPORTE}")

        # ── Mensajes por piso — espacio JEFES (desde las 11:00) ─────────────
        contador["count"] = contador.get("count", 0) + 1
        if contador["count"] >= CICLOS_JEFES and datetime.now().hour >= 11:
            _WEBHOOKS_YA_ENVIADOS.clear()
            enviar_mensaje_jefes(datos, dir_dict, hist_dict, descansos, jefes_en_descanso, prioridades)
            contador["count"] = 0
            log.info(f"Mensajes jefes enviados, contador reiniciado (cada {CICLOS_JEFES} ciclo(s))")
        else:
            log.info(f"Contador jefes: {contador['count']}/{CICLOS_JEFES}")

        guardar_contador(contador)

        # ── Mensajes individuales — VENDEDORES ──────────────────────────────
        # Se evalúa cada ciclo; la frecuencia es por vendedor (columna Ciclos
        # de WEBHOOKS_VENDEDORES) con default global ciclos_vendedores.
        enviar_mensajes_vendedores(datos, dir_dict, descansos, prioridades)

        _sheets_con_reintento(guardar_metricas_dia, gc_global, resumen, len(vencidas))
        duracion = time.time() - t_inicio
        _sheets_con_reintento(guardar_en_monitor, gc_global, True, duracion, resumen, len(vencidas), intentos_descarga)
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

        estado_demo("listo", "Ciclo completado en " + str(round(duracion, 1)) + "s — avisos entregados",
                    total=resumen.get("total", 0), vencidas=len(vencidas), duracion=round(duracion, 1))

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
            _c = leer_contador()
            _c["errores_dia"] = _c.get("errores_dia", 0) + 1
            guardar_contador(_c)
        except Exception:
            pass
        try:
            duracion = time.time() - t_inicio
        except Exception:
            duracion = 0
        try:
            if gc_global:
                _sheets_con_reintento(guardar_en_monitor, gc_global, False, duracion, {}, 0)
            respaldo_monitor_local({}, 0, 0, False, duracion)
        except Exception:
            pass
        try:
            enviar_chat({}, exito=False, error=str(e))
        except Exception:
            pass
    finally:
        liberar_lock()
        # Red de seguridad: si el run terminó por una excepción, pudo quedar un
        # Chromium colgado (p.ej. al expirar el timeout de descarga). Limpiarlo
        # evita que se acumulen y consuman la RAM de la PC con el paso de los días.
        try:
            matar_chromium_zombie()
        except Exception:
            pass

# ── KPI TIEMPOS XD ───────────────────────────────────────────











if __name__ == "__main__":
    import sys
    if "--test-xd" in sys.argv:
        log.info("=== MODO PRUEBA XD ===")
        csv_xd    = descargar_historico_xd(visible=True)
        datos_oms = _get_datos_oms()

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
            dir_dict, _, _, _, _ = cargar_estructuras_sheets(ss)
            log.info(f"Directorio cargado: {len(dir_dict)} secciones")
        except Exception as e:
            log.warning(f"No se pudo cargar directorio: {e}")

        kpi   = calcular_kpi_xd(datos_oms, csv_xd, dir_dict=dir_dict)
        jefes = kpi["kpi_jefes"]
        log.info(f"KPI jefes ({len(jefes)}):")
        for j, d in sorted(jefes.items(), key=lambda x: x[1]["prom_mins"]):
            log.info(f"  {j}: min={d['min_mins']}m prom={d['prom_mins']}m max={d['max_mins']}m | {d['total_rem']} rem")

        resp = input("Mandar KPI jefes al espacio Tiempos? (s/n): ").strip().lower()
        if resp == "s":
            enviar_kpi_jefes_tiempos(datos_oms=datos_oms, csv_xd=csv_xd, dir_dict=dir_dict)
    else:
        main()


