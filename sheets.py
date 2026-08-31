"""sheets.py — Lectura/escritura de Google Sheets.

Vuelca el CSV a las hojas, mantiene DIRECTORIO/HISTORIAL/MONITOR/METRICAS,
carga webhooks y descansos, registra la PC. Extraído de main.py; los globales
y funciones de main se acceden vía M, la config/utils por import directo.
"""

import os, csv, json, time, logging, re, hashlib, math
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter

import requests
import gspread
from google.oauth2.service_account import Credentials

class _LazyMain:
    """Acceso diferido a main.py — evita el import circular al arrancar
    (main.py hace 'from sheets import ...' antes de terminar de ejecutarse)."""
    def __getattr__(self, name):
        import main
        return getattr(main, name)

M = _LazyMain()
from config import LIVERPOOL, GOOGLE, CHAT, XD, CARPETA_DESCARGA, PC_NOMBRE
from api_remisiones import COLUMNAS_REMISION, preparar_filas
from utils import (
    parse_fecha_cached, get_fechas, calcular_minutos, es_de_ayer,
    calcular_segundos_entre, calcular_tiempo_espera_str, seg_a_str, _formato_min,
    _emoji_semaforo, medir, convertir_valor, limpiar_datos, orden_piso, get_genero,
    get_mencion, contar_tipos, calcular_espera, actualizar_historial,
    HISTORIAL_MAX, MARGEN, TIPOS_PRIORIDAD, JEFES_MUJERES,
)

log = logging.getLogger("argos.sheets")


def _es_429(e):
    """True si la excepción de gspread es un límite de cuota (429)."""
    return any(k in str(e) for k in ("429", "RESOURCE_EXHAUSTED", "Quota exceeded"))

def cargar_estructuras_sheets(ss):
    """Lee DIRECTORIO, HISTORIAL, DESCANSOS y PRIORIDADES en una sola tanda."""
    dir_dict, hist_dict, descansos, jefes_en_descanso, prioridades = {}, {}, {}, {}, []
    try:
        # batch_get para leer 4 hojas en una sola llamada
        # HISTORIAL se lee hasta columna F para recuperar Ubicación como respaldo
        ranges  = ["DIRECTORIO!A:F", "HISTORIAL!A:F", "DESCANSOS!A:D", "PRIORIDADES!A:D"]
        results = ss.values_batch_get(ranges).get("valueRanges", [])
        if len(results) >= 1 and results[0].get("values"):
            _raw_rows = results[0]["values"][1:]
            if _raw_rows:
                log.debug(f"[DIAG DIRECTORIO] primera fila: {_raw_rows[0]} (len={len(_raw_rows[0])})")
            for row in _raw_rows:
                if row and row[0]:
                    dir_dict[str(row[0]).strip()] = {
                        "nombre_seccion": row[1] if len(row) > 1 else "",
                        "jefe":           row[2] if len(row) > 2 else "",
                        "gerencia":       row[3] if len(row) > 3 else "",
                        "ubicacion":      row[5] if len(row) > 5 else "",
                    }
        if len(results) >= 2 and results[1].get("values"):
            for row in results[1]["values"][1:]:
                if row and row[0]:
                    hist_dict[str(row[0]).strip()] = {
                        "Jefe":      row[2] if len(row) > 2 else "",
                        "Ubicacion": row[5] if len(row) > 5 else "",
                    }
        # Fallback: si DIRECTORIO tiene Ubicación vacía, tomar la de HISTORIAL
        for sec, info in dir_dict.items():
            if not info["ubicacion"]:
                info["ubicacion"] = hist_dict.get(sec, {}).get("Ubicacion", "")
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
        if len(results) >= 4 and results[3].get("values"):
            for row in results[3]["values"][1:]:
                if not row or len(row) < 3:
                    continue
                activo_str = str(row[3]).strip().lower() if len(row) > 3 else "si"
                if activo_str not in ("", "si", "yes", "true", "1"):
                    continue
                prioridades.append({"campo": row[1], "valor": row[2]})
    except Exception as e:
        log.warning(f"Error batch_get sheets: {e}")
    return dir_dict, hist_dict, descansos, jefes_en_descanso, prioridades


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


def cargar_webhooks_vendedores(gc, nombres_vendedores=None):
    """Lee la hoja WEBHOOKS_VENDEDORES y retorna {NOMBRE_UPPER: {"url", "ciclos"}}.
    Columnas: A=Vendedor, B=Webhook, C=Activo, D=Ciclos (frecuencia personal;
    vacio = usa el default global ciclos_vendedores).
    Agrega automaticamente cualquier vendedor nuevo detectado en el CSV
    (columna A) para que solo haya que pegar el webhook en la columna B.
    """
    try:
        ss = gc.open_by_key(GOOGLE["sheet_id"])
        try:
            hoja = ss.worksheet("WEBHOOKS_VENDEDORES")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("WEBHOOKS_VENDEDORES", rows=300, cols=4)
            hoja.update([["Vendedor", "Webhook", "Activo", "Ciclos"]], "A1")
            log.info("Hoja WEBHOOKS_VENDEDORES creada — agrega los webhooks en columna B")
        rows = hoja.get_all_values()

        # Asegurar encabezado de la columna D (Ciclos) en hojas ya existentes
        if rows and (len(rows[0]) < 4 or not str(rows[0][3]).strip()):
            try:
                hoja.update([["Ciclos"]], "D1")
            except Exception:
                pass

        # Nombres ya presentes en la hoja (columna A, normalizados)
        existentes = {str(r[0]).strip().upper() for r in rows[1:] if r and str(r[0]).strip()}

        # Agregar vendedores nuevos detectados en el CSV
        if nombres_vendedores:
            nuevos = sorted({
                n.strip() for n in nombres_vendedores
                if n and n.strip().upper() not in existentes
            })
            if nuevos:
                hoja.append_rows([[n, "", "", ""] for n in nuevos], value_input_option="RAW")
                log.info(f"WEBHOOKS_VENDEDORES: {len(nuevos)} vendedor(es) nuevo(s) agregado(s)")
                rows = hoja.get_all_values()

        resultado = {}
        for row in rows[1:]:
            if row and len(row) >= 2 and str(row[0]).strip() and str(row[1]).strip():
                nombre  = str(row[0]).strip()
                webhook = str(row[1]).strip()
                activo  = True
                if len(row) >= 3 and str(row[2]).strip().lower() in ("false", "0", "no"):
                    activo = False
                # Columna D = ciclos personales (0 = usar default global)
                ciclos = 0
                if len(row) >= 4 and str(row[3]).strip().isdigit():
                    ciclos = max(1, int(str(row[3]).strip()))
                if nombre and webhook and activo:
                    resultado[nombre.upper()] = {"url": webhook, "ciclos": ciclos}
        log.info(f"WEBHOOKS_VENDEDORES cargados: {len(resultado)} vendedor(es) con webhook activo")
        return resultado
    except Exception as e:
        log.warning(f"Error cargando WEBHOOKS_VENDEDORES: {e}")
        return {}


def _validar_url_webhook(url, nombre):
    """Valida formato de webhook de Google Chat. Retorna True si es válido."""
    if not url.startswith("https://chat.googleapis.com"):
        log.warning(f"⚠️ {nombre}: URL inválida (debe iniciar con https://chat.googleapis.com)")
        return False
    if "key=" not in url:
        log.warning(f"⚠️ {nombre}: falta parámetro key= — verifica la URL en hoja CONFIG")
        return False
    if "token=" not in url:
        log.warning(f"⚠️ {nombre}: falta parámetro token= — verifica la URL en hoja CONFIG")
        return False
    return True


def bot_pausado_remoto():
    return M.CONFIG_REMOTA.get("pausado", "").lower() in ("si", "yes", "true", "1")


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
            hoja.update([[ahora_str, M.VERSION]], f"C{idx}:D{idx}")
            log.info(f"PC '{PC_NOMBRE}' registrada — estado: {estado}")
            if estado.lower() == "pausado":
                log.info(f"Esta PC ({PC_NOMBRE}) esta pausada remotamente. Bot detenido.")
                return False
        else:
            # PC nueva — agregar fila
            hoja.append_row([PC_NOMBRE, "activo", ahora_str, M.VERSION])
            log.info(f"PC '{PC_NOMBRE}' registrada por primera vez en PCS")

        return True
    except Exception as e:
        log.warning(f"Error en registrar_y_verificar_pc: {e} — continuando sin verificacion")
        return True   # En caso de error, dejar correr el bot


def dia_activo_hoy():
    """Verifica si hoy esta en la lista de dias activos."""
    dias_str = M.CONFIG_REMOTA.get("dias_activos", "lun,mar,mie,jue,vie,sab,dom").lower()
    dias_lista = [d.strip() for d in dias_str.split(",")]
    nombres = ["lun","mar","mie","jue","vie","sab","dom"]
    hoy = nombres[datetime.now().weekday()]
    return hoy in dias_lista


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


def cargar_prioridades(ss):
    """
    Lee la hoja PRIORIDADES (Campo, Valor, Activo) — reglas configurables
    desde el dashboard que se SUMAN a la regla fija de _categoria_urgente
    (HD0D/HD1D/C&C). Devuelve solo las reglas activas: [{"campo","valor"}].
    """
    prioridades = []
    try:
        hoja = ss.worksheet("PRIORIDADES")
        for row in hoja.get_all_values()[1:]:
            if not row or len(row) < 3 or not row[1] or not row[2]:
                continue
            activo = str(row[3]).strip().lower() if len(row) > 3 else "si"
            if activo not in ("", "si", "yes", "true", "1"):
                continue
            prioridades.append({"campo": row[1], "valor": row[2]})
    except gspread.WorksheetNotFound:
        hoja = ss.add_worksheet("PRIORIDADES", rows=200, cols=5)
        hoja.update([["ID", "Campo", "Valor", "Activo", "Creado"]], "A1")
    except Exception as e:
        log.error(f"Error leyendo PRIORIDADES: {e}")
    return prioridades


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

    # `datos` ahora es una lista de dicts (remisiones tal cual las manda la
    # API) — se ordenan según COLUMNAS_REMISION en vez de por índice de CSV.
    datos_limpios = preparar_filas(datos, COLUMNAS_REMISION)
    hoja1.clear()
    hoja1.update([COLUMNAS_REMISION] + datos_limpios, "A1", value_input_option="RAW")
    log.info("Sheet 1 actualizado ✅ (columnas API)")

    ss2 = gc.open_by_key(GOOGLE["sheet2_id"])
    try:
        hoja2 = ss2.worksheet(GOOGLE["sheet2_hoja"])
    except gspread.WorksheetNotFound:
        hoja2 = ss2.add_worksheet(GOOGLE["sheet2_hoja"], rows=5000, cols=50)
    # Borrar solo hasta la última fila con datos en columna V (no siempre 5000)
    try:
        col_v = hoja2.col_values(22)  # columna V = índice 22
        ultima_fila = max(len(col_v), int(GOOGLE.get("sheet2_fila", 2)))
        hoja2.batch_clear([f"V2:AS{ultima_fila}"])
        log.info(f"Sheet 2 rango V2:AS{ultima_fila} limpiado ✅")
    except Exception as e:
        log.warning(f"No se pudo limpiar Sheet 2: {e}")

    hoja2.update(datos_limpios, f"{GOOGLE['sheet2_col']}{GOOGLE['sheet2_fila']}", value_input_option="USER_ENTERED")
    hoja2.update([[datetime.now().strftime("%d/%m/%Y %H:%M:%S")]], f"{GOOGLE['timestamp_col']}{GOOGLE['timestamp_fila']}")
    log.info("Sheet 2 actualizado ✅")

    # Copiar fórmulas de columnas específicas (A, F, I, J, K, U) hacia abajo
    # ⚠️ REVISAR: estas posiciones (A, F, I-K, U) correspondían al layout viejo
    # del CSV. Con el nuevo orden (COLUMNAS_REMISION) hay que confirmar en la
    # hoja qué columnas tienen fórmula ahora y actualizar formula_cols abajo.
    try:
        fila_inicio = int(GOOGLE.get("sheet2_fila", 2))
        num_filas   = len(datos_limpios)
        if num_filas > 1:
            # Columnas con fórmula (0-based start, exclusive end): A, F, I-K, U
            formula_cols = [(0,1), (5,6), (8,11), (20,21)]
            dest_end     = fila_inicio - 1 + num_filas
            requests     = []
            for start_col, end_col in formula_cols:
                requests.append({"copyPaste": {
                    "source":      {"sheetId": hoja2.id, "startRowIndex": fila_inicio - 1, "endRowIndex": fila_inicio,   "startColumnIndex": start_col, "endColumnIndex": end_col},
                    "destination": {"sheetId": hoja2.id, "startRowIndex": fila_inicio,     "endRowIndex": dest_end, "startColumnIndex": start_col, "endColumnIndex": end_col},
                    "pasteType": "PASTE_FORMULA", "pasteOrientation": "NORMAL"
                }})
            ss2.batch_update({"requests": requests})
            log.info(f"Fórmulas A,F,I,J,K,U copiadas hasta fila {fila_inicio + num_filas - 1} ✅")
    except Exception as e:
        log.warning(f"No se pudo copiar fórmulas: {e}")

    # DIRECTORIO + HISTORIAL en UNA sola lectura (batch_get) en vez de dos
    # get_all_values separados — reduce llamadas a la API de Sheets.
    dir_dict  = {}
    hist_dict = {}
    try:
        _res = ss1.values_batch_get(["DIRECTORIO!A:F", "HISTORIAL!A:C"]).get("valueRanges", [])
        _dir_rows  = _res[0].get("values", []) if len(_res) > 0 else []
        _hist_rows = _res[1].get("values", []) if len(_res) > 1 else []
        for row in _dir_rows[1:]:
            if row and row[0]:
                sec = str(row[0]).strip()
                dir_dict[sec] = {
                    "jefe":          row[2] if len(row) > 2 else "",
                    "nombre_seccion":row[1] if len(row) > 1 else "",
                    "ubicacion":     row[5] if len(row) > 5 else "",  # columna F
                }
        for row in _hist_rows[1:]:
            if row and row[0]:
                hist_dict[str(row[0]).strip()] = {"Jefe": row[2] if len(row) > 2 else ""}
    except Exception as e:
        log.warning(f"Error leyendo DIRECTORIO/HISTORIAL: {e}")

    descansos, jefes_en_descanso = leer_descansos(ss1, dir_dict)
    prioridades = cargar_prioridades(ss1)

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

    ESTATUS_FILTRO = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]

    rows_app = []
    for row in datos:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status not in ESTATUS_FILTRO:
            continue
        sec       = str(row.get("Seccion", "")).strip().replace(".0","")
        jefe      = str(row.get("NombreJefeDePiso", "")).strip()
        ubicacion = dir_dict.get(sec, {}).get("ubicacion", "")
        if not jefe or jefe in ("","nan","Sin Asignar","UNASSIGNED"):
            jefe = dir_dict.get(sec, {}).get("jefe","") or hist_dict.get(sec, {}).get("Jefe","Sin Asignar")
        if sec in descansos:
            jefe = jefe + " -> " + descansos[sec]
        rows_app.append([
            row.get("Remision", ""),
            row.get("Sku", ""),
            row.get("DescripcionSku", ""),
            row.get("order_quantity", ""),
            row.get("NombreVendedor", ""),
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

    return dir_dict, hist_dict, descansos, jefes_en_descanso, prioridades


def archivar_monitor_si_necesario(gc):
    """Si MONITOR tiene filas de >180 dias las mueve a hoja MONITOR_ARCHIVO.
    Corre como máximo 1 vez al día: leer todo MONITOR cada ciclo (cada 15 min)
    solo para chequear el tamaño desperdiciaba una lectura grande de la API."""
    marca = "monitor_archivo_check.json"
    hoy = datetime.now().strftime("%d/%m/%Y")
    try:
        if os.path.exists(marca):
            with open(marca, encoding="utf-8") as f:
                if json.load(f).get("fecha") == hoy:
                    return  # ya se revisó hoy
    except Exception:
        pass
    try:
        with open(marca, "w", encoding="utf-8") as f:
            json.dump({"fecha": hoy}, f)
    except Exception:
        pass
    try:
        ss   = gc.open_by_key(GOOGLE["sheet_id"])
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
    for _reint in range(3):
        try:
            ss = gc.open_by_key(GOOGLE["sheet_id"])
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
            return
        except Exception as e:
            if _es_429(e) and _reint < 2:
                time.sleep(10 * (2 ** _reint))
                continue
            log.warning("No se pudo guardar en MONITOR: " + str(e))
            return


def guardar_tiempos_asignacion(gc, datos, dir_dict):
    """Guarda en hoja TIEMPOS del Sheet 1 los tiempos de remisiones sin asignar."""
    try:
        ss   = gc.open_by_key(M.TIEMPOS_SHEET_ID)
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
            if not row:
                continue
            status = str(row.get("StatusRemision", "")).strip()
            if status != "Mercancia en Espera de Entrega":
                continue
            nom_vendedor = str(row.get("NombreVendedor", "")).strip()
            if nom_vendedor and nom_vendedor not in ("", "nan", "Sin Asignar", "Sin Seccion", "UNASSIGNED"):
                continue  # ya tiene vendedor, no contar

            fecha_h      = row.get("FechaAsignacionTienda", "")
            nom_jefe     = str(row.get("NombreJefeDePiso", "")).strip()
            sec          = str(row.get("Seccion", "")).strip().replace(".0","")

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
        ss   = gc.open_by_key(M.TIEMPOS_SHEET_ID)
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

        lineas.append("_Argos — " + fecha_now + "_")

        M.post_chat_con_reintento(M.WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("Resumen tiempos enviado al espacio tiempos ✅")

    except gspread.WorksheetNotFound:
        log.info("Hoja TIEMPOS no existe aun")
    except Exception as e:
        log.warning(f"Error enviando resumen tiempos: {e}")


def cargar_dir_cache():
    """Carga DIRECTORIO desde cache si no tiene mas de 60 min."""
    try:
        if os.path.exists(M.DIR_CACHE_FILE):
            mtime = os.path.getmtime(M.DIR_CACHE_FILE)
            if (time.time() - mtime) / 60 < M.DIR_CACHE_MINUTOS:
                with open(M.DIR_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return None


def guardar_dir_cache(dir_dict, hist_dict):
    try:
        with open(M.DIR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"dir": dir_dict, "hist": hist_dict, "ts": datetime.now().isoformat()}, f)
    except Exception:
        pass


def respaldo_monitor_local(resumen, vencidas, intentos, exito, duracion):
    """Guarda respaldo local del MONITOR."""
    try:
        existe = os.path.exists(M.MONITOR_BACKUP)
        with open(M.MONITOR_BACKUP, "a", encoding="utf-8", newline="") as f:
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


def guardar_metricas_dia(gc, resumen, vencidas_count):
    """Guarda resumen diario en hoja METRICAS del Sheet 1."""
    for _reint in range(3):
        try:
            ss   = gc.open_by_key(GOOGLE["sheet_id"])
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
            return
        except Exception as e:
            if _es_429(e) and _reint < 2:
                time.sleep(10 * (2 ** _reint))
                continue
            log.warning(f"Error METRICAS: {e}")
            return
