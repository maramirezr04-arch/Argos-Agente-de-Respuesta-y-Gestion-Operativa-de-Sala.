"""api_remisiones.py — Obtiene las remisiones directo por API (sin descargar CSV).

Reemplaza el flujo anterior (descargar_csv + leer_csv, en descarga.py): en vez
de automatizar clics para descargar un CSV desde la pantalla "Indicadores",
este módulo automatiza clics SOLO para capturar el Accesstoken real de cada
una de las 3 pestañas de "DETALLES REMISIONES" (Pendientes, Etiqueta
Generada, Realizado), y luego llama directo la API apiXDPRgtRemisiones con
`requests`, paginando hasta traer todo. Es la misma técnica que ya se probó
en Power Query para el proyecto de Excel de Liverpool (capturar token →
llamar API → combinar tablas).

Cada remisión queda como un dict con los mismos nombres de campo que manda
la API (Remision, Sku, DescripcionSku, Seccion, NombreJefeDePiso,
NombreVendedor, StatusRemision, TipoEntrega, FechaAsignacionTienda, etc.).
Ya no hay índices de columna (COL_SECCION=5, COL_JEFE=17...) que memorizar —
el resto del bot (sheets.py, mensajes.py, kpi.py) ahora lee estos mismos
nombres directamente.

⚠️ Ya NO hace falta calibrar nada — las coordenadas y selectores de texto de
   este módulo vienen directo de `capturar_token.py` (el script ya probado
   en producción del proyecto de Excel/Power Query), solo se portó a Python
   dentro de Argos.
"""

import json
import logging
import re
import time
from pathlib import Path
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

from config import LIVERPOOL, CARPETA_DESCARGA

log = logging.getLogger("argos.api_remisiones")

API_REMISIONES_URL = "https://backend-oms-xd.liverpool.com.mx/api/apiXDPRgtRemisiones/"
ENDPOINT_OBJETIVO  = "apixdprgtremisiones/"  # lowercase, sin "statistics"

# Coordenadas Flutter-web (canvas) — ya calibradas y probadas en capturar_token.py.
COORD_LOGIN_USER = (683, 272)
COORD_LOGIN_PASS = (683, 344)
COORD_LOGIN_BTN  = (683, 480)
COORD_DETALLES_REMISIONES = (683, 187)  # medido sobre debug_login.png (1366x768)

# "Pendientes" es la pestaña que carga sola al entrar (no hay que darle clic).
# "Realizado" y "Etiqueta Generada" sí hay que clicarlas — primero se intenta
# por texto (más robusto si cambia el layout), y si falla, por coordenada.
COORD_TAB_REALIZADO         = (490, 152)
COORD_TAB_ETIQUETA_GENERADA = (618, 152)

TOKENS_CACHE_FILE = Path(CARPETA_DESCARGA) / "tokens_remisiones.json"
DATOS_CACHE_FILE  = Path(CARPETA_DESCARGA) / "remisiones_actuales.json"
HASH_FILE         = Path(CARPETA_DESCARGA) / "remisiones_hash.json"

NO_ASIGNADO = ("", "nan", "Sin Asignar", "Sin Seccion", "UNASSIGNED")

# Orden de columnas para volcar en "Hoja 1" / "Sheet 2" — ya no es el orden
# del CSV viejo, es el orden en que decidimos mostrar los campos de la API.
# ⚠️ Las fórmulas manuales de Sheet 2 (columnas A, F, I-K, U) se armaron para
# el layout anterior — hay que revisarlas/rehacerlas contra este nuevo orden.
COLUMNAS_REMISION = [
    "Remision", "Sku", "DescripcionSku", "order_quantity", "Seccion",
    "NombreJefeDePiso", "NombreVendedor", "FechaAsignacionTienda",
    "FechaAsignacionVendedor", "StatusRemision", "TipoOrden", "TipoEntrega",
    "Tienda", "Created_At", "Evento", "Color", "LP",
]


def _convertir(v):
    """Igual que utils.convertir_valor pero para un valor ya sacado de un dict."""
    if v is None or v == "":
        return ""
    s = str(v).strip()
    try:
        return int(s) if "." not in s else float(s)
    except (ValueError, TypeError):
        return s


def preparar_filas(datos, columnas=COLUMNAS_REMISION):
    """Convierte una lista de dicts (remisiones de la API) en una lista de
    listas en el orden de `columnas`, lista para mandar a gspread."""
    return [[_convertir(row.get(c, "")) for c in columnas] for row in datos]


def _flutter_listo(page, timeout=30000):
    page.wait_for_function(
        "() => document.querySelector('flt-glass-pane') !== null", timeout=timeout
    )


def capturar_tokens(visible=False, timeout_ms=60000):
    """
    Abre el OMS, hace login, entra a DETALLES REMISIONES y da clic en cada
    una de las 3 pestañas. Por cada clic intercepta la petición POST real a
    apiXDPRgtRemisiones y guarda sus headers + body tal cual los mandó
    Liverpool (no se arman a mano, así siempre coinciden con lo que la API
    espera realmente).

    Retorna: {accion: {"headers": {...}, "body": {...}}}
    """
    capturados = {}

    def _on_request(request):
        url_lower = request.url.lower()
        if ENDPOINT_OBJETIVO in url_lower and "statistics" not in url_lower:
            try:
                body = json.loads(request.post_data or "{}")
            except Exception:
                body = {}
            accion = body.get("accion", f"req_{len(capturados)}")
            capturados[accion] = {"headers": dict(request.headers), "body": body}
            log.info(f"Token capturado para accion={accion}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not visible, args=["--disable-extensions", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            context = browser.new_context(viewport={"width": 1366, "height": 768})
            page = context.new_page()
            page.on("request", _on_request)

            page.goto(LIVERPOOL["url_login"], timeout=timeout_ms)
            page.wait_for_load_state("domcontentloaded")
            _flutter_listo(page)
            time.sleep(8)  # Flutter necesita ~8s para renderizar visualmente

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

            _click_y_type(*COORD_LOGIN_USER, LIVERPOOL["usuario"])
            time.sleep(0.3)
            _click_y_type(*COORD_LOGIN_PASS, LIVERPOOL["password"])
            time.sleep(0.3)
            page.mouse.click(*COORD_LOGIN_BTN)
            try:
                page.wait_for_function(
                    "() => !window.location.href.includes('login')", timeout=30000
                )
            except Exception:
                pass
            if "login" in page.url:
                raise Exception("Login fallido")
            log.info("Login OK ✓")
            time.sleep(4)

            # Entrar a DETALLES REMISIONES — por texto primero, coordenada de respaldo
            try:
                page.get_by_text("DETALLES REMISIONES", exact=False).click(timeout=5000)
            except Exception:
                page.mouse.click(*COORD_DETALLES_REMISIONES)

            # "Pendientes" es la pestaña que carga sola por default → dispara
            # su propia llamada (accion=PendienteSurtido) sin necesidad de clic.
            page.wait_for_timeout(6000)

            # "Realizado" (accion=Surtida)
            try:
                page.get_by_text(re.compile("REALIZADO", re.IGNORECASE)).click(timeout=5000)
            except Exception:
                page.mouse.click(*COORD_TAB_REALIZADO)
            page.wait_for_timeout(5000)

            # "Etiqueta Generada" (accion=EtiquetaGenerada) — singular, no plural
            try:
                page.get_by_text(re.compile("ETIQUETA GENERADA", re.IGNORECASE)).click(timeout=5000)
            except Exception:
                page.mouse.click(*COORD_TAB_ETIQUETA_GENERADA)
            page.wait_for_timeout(5000)

        finally:
            browser.close()

    if len(capturados) < 3:
        log.warning(f"Solo se capturaron {len(capturados)}/3 tokens: {list(capturados.keys())}")

    try:
        Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)
        with open(TOKENS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(capturados, f)
    except Exception as e:
        log.warning(f"No se pudo guardar cache de tokens: {e}")

    return capturados


def _llamar_api(headers, body, max_paginas=50):
    """Pagina sobre apiXDPRgtRemisiones hasta que ya no haya más resultados."""
    remisiones = []
    body = dict(body)
    tam_pagina = body.get("tamanoPagina", 20)
    for pagina in range(max_paginas):
        body["numeroPagina"] = pagina
        r = requests.post(API_REMISIONES_URL, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        lote = r.json().get("Remisiones", [])
        if not lote:
            break
        remisiones.extend(lote)
        if len(lote) < tam_pagina:
            break  # última página
    return remisiones


def obtener_remisiones(visible=False, reintentos=2):
    """
    Reemplaza a descargar_csv() + leer_csv(). Captura los 3 tokens y trae las
    3 pestañas por API. Retorna (datos, intentos) igual que antes — solo que
    `datos` ahora es una lista de dicts (uno por remisión, con los campos tal
    cual los manda la API) en vez de una lista de listas por índice.
    """
    ultimo_error = None
    for intento in range(1, reintentos + 1):
        try:
            tokens = capturar_tokens(visible=visible)
            if not tokens:
                raise Exception("No se capturó ningún token")

            datos = []
            for accion, info in tokens.items():
                lote = _llamar_api(info["headers"], info["body"])
                log.info(f"API accion={accion}: {len(lote)} remisiones")
                datos.extend(lote)

            _guardar_cache_datos(datos)
            return datos, intento
        except Exception as e:
            ultimo_error = e
            log.warning(f"[intento {intento}] Error obteniendo remisiones por API: {e}")
            time.sleep(10)
    raise Exception(f"No se pudieron obtener remisiones por API tras {reintentos} intentos: {ultimo_error}")


def calcular_resumen(datos):
    """Equivalente al resumen que antes calculaba leer_csv() sobre el CSV."""
    def _cuenta(status):
        return sum(1 for r in datos if str(r.get("StatusRemision", "")).strip() == status)

    return {
        "total":       len(datos),
        "espera":      _cuenta("Mercancia en Espera de Entrega"),
        "etiquetas":   _cuenta("Etiqueta Generada"),
        "sin_asignar": sum(
            1 for r in datos if str(r.get("NombreVendedor", "")).strip() in NO_ASIGNADO
        ),
        "rechazados":  _cuenta("Rechazado"),
    }


def validar_datos(datos):
    """Reemplaza a validar_csv() — chequeo de sanidad básico sobre la respuesta de la API."""
    if not datos:
        return False, "Sin remisiones en la respuesta de la API"
    return True, f"{len(datos)} remisiones"


def verificar_datos_congelados(datos, post_chat_con_reintento, webhook):
    """Equivalente a _verificar_csv_congelado() de main.py, pero sobre el
    contenido de `datos` en vez del hash de un archivo CSV."""
    try:
        h = hash(json.dumps(datos, sort_keys=True, default=str))
        estado = {}
        if HASH_FILE.exists():
            with open(HASH_FILE, encoding="utf-8") as f:
                estado = json.load(f)
        if estado.get("hash") == h:
            estado["ciclos_igual"] = estado.get("ciclos_igual", 1) + 1
        else:
            estado = {"hash": h, "ciclos_igual": 1, "alerta_enviada": False}
        with open(HASH_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f)
        if estado["ciclos_igual"] >= 3 and not estado.get("alerta_enviada"):
            fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
            msg = (
                f"⚠️ *Argos — Datos posiblemente congelados*\n\n"
                f"La API de remisiones lleva *{estado['ciclos_igual']} ciclos* "
                f"consecutivos sin cambios. Es posible que el sistema OMS no "
                f"esté actualizando.\n\n_Verificar OMS manualmente_\n_{fecha_now}_"
            )
            post_chat_con_reintento(webhook, {"text": msg})
            estado["alerta_enviada"] = True
            with open(HASH_FILE, "w", encoding="utf-8") as f:
                json.dump(estado, f)
    except Exception as e:
        log.warning(f"Error verificando datos congelados: {e}")


def _guardar_cache_datos(datos):
    try:
        Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)
        with open(DATOS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"datos": datos, "ts": datetime.now().isoformat()}, f)
    except Exception as e:
        log.warning(f"No se pudo guardar cache de remisiones: {e}")


def cargar_cache_datos():
    """Usado por kpi.py cuando necesita las últimas remisiones de OMS sin
    volver a llamar la API (equivalente al viejo _get_csv_oms())."""
    try:
        if DATOS_CACHE_FILE.exists():
            with open(DATOS_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f).get("datos", [])
    except Exception:
        pass
    return []
