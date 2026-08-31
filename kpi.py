"""kpi.py — KPI de tiempos del Sistema XD.

Calcula y envía los indicadores de tiempo (login→entrega) cruzando el CSV del
OMS con el histórico del XD. Extraído de main.py; las funciones y globales de
main se acceden vía M (import diferido) y la config/utils por import directo.
"""

import os, csv, json, time, logging, re, hashlib, math
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter

class _LazyMain:
    """Acceso diferido a main.py — evita el import circular al arrancar
    (main.py hace 'from kpi import ...' antes de terminar de ejecutarse)."""
    def __getattr__(self, name):
        import main
        return getattr(main, name)

M = _LazyMain()
from config import LIVERPOOL, GOOGLE, CHAT, XD, CARPETA_DESCARGA, PC_NOMBRE
from utils import (
    parse_fecha_cached, get_fechas, calcular_minutos, es_de_ayer,
    calcular_segundos_entre, calcular_tiempo_espera_str, seg_a_str, _formato_min,
    _emoji_semaforo, medir, convertir_valor, limpiar_datos, orden_piso, get_genero,
    get_mencion, contar_tipos, calcular_espera, actualizar_historial,
    HISTORIAL_MAX, MARGEN, TIPOS_PRIORIDAD, JEFES_MUJERES,
)

log = logging.getLogger("argos.kpi")

def calcular_kpi_xd(datos_oms, csv_xd, dir_dict=None):
    """
    Calcula KPI de tiempos cruzando las remisiones de OMS (lista de dicts,
    tal cual las manda la API — ver api_remisiones.py) con el Historico XD
    (event log, sigue siendo un CSV — es un sistema/export distinto).

    Campos clave del OMS: Remision, FechaAsignacionTienda, Seccion.
    Columnas clave del CSV XD:
      Remision, Estatus, Fecha Estatus, Nombre Empleado, Nombre Asesor C&C, Seccion

    Lógica:
      T1 = FechaAsignacionTienda del OMS (cuándo llegó a tienda)
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
    if datos_oms:
        try:
            for row in datos_oms:
                rem = limpiar(row.get("Remision", ""))
                dt  = parse_dt(row.get("FechaAsignacionTienda", ""))
                sec = str(row.get("Seccion", "")).strip().replace(".0", "")
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


def _get_datos_oms():
    """Devuelve las últimas remisiones de OMS obtenidas por la API
    (equivalente al viejo _get_csv_oms(), que buscaba un archivo CSV)."""
    from api_remisiones import cargar_cache_datos
    return cargar_cache_datos()


def _get_csv_xd():
    """Devuelve la ruta al CSV de historico XD más reciente."""
    archivos = sorted(Path(CARPETA_DESCARGA).glob("historico_xd_*.csv"), reverse=True)
    return str(archivos[0]) if archivos else None


def enviar_kpi_jefes_tiempos(datos_oms=None, csv_xd=None, dir_dict=None):
    """
    Calcula KPI de tiempos por jefe (del DIRECTORIO de Sheets) y manda al espacio Tiempos.

    Métrica: tiempo desde asignación a tienda (OMS) hasta asignación/reasignación a vendedor (XD)
    Muestra por jefe: más rápido / promedio / más lento
    """
    try:
        # ── Remisiones OMS (API) ───────────────────────────────────
        if not datos_oms:
            datos_oms = _get_datos_oms()
        if not datos_oms:
            log.warning("KPI jefes: no hay remisiones de OMS en cache")
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
                csv_xd = M.descargar_historico_xd(visible=False)

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
                dir_dict, _, _, _ = M.cargar_estructuras_sheets(ss)
                log.info(f"KPI jefes: directorio cargado ({len(dir_dict)} secciones)")
            except Exception as e:
                log.warning(f"KPI jefes: no se pudo cargar directorio: {e}")

        kpi   = calcular_kpi_xd(datos_oms, csv_xd, dir_dict=dir_dict)
        jefes = kpi["kpi_jefes"]

        if not jefes:
            log.info("KPI jefes: sin datos suficientes (verifica DIRECTORIO y XD)")
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
        lineas.append(f"_Argos — {fecha_now}_")
        lineas.append("━━━━━━━━━━━━━━━━━━━━━━━━━")

        M.post_chat_con_reintento(M.WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("KPI jefes tiempos enviado al espacio Tiempos ✅")

        # ── Escribir KPI en hoja KPI_TIEMPOS (histórico por día) ──────────
        try:
            from config import GOOGLE
            creds2 = Credentials.from_service_account_file(
                GOOGLE["credentials"],
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            gc2    = gspread.authorize(creds2)
            ss2    = gc2.open_by_key(GOOGLE["sheet_id"])
            try:
                hoja_t = ss2.worksheet("KPI_TIEMPOS")
            except Exception:
                hoja_t = ss2.add_worksheet("KPI_TIEMPOS", rows=5000, cols=7)

            try:
                a1 = hoja_t.acell("A1").value or ""
            except Exception:
                a1 = ""
            if "dia" not in a1.lower():
                hoja_t.update(
                    [["Dia", "Jefe", "Min (min)", "Promedio (min)", "Max (min)", "Remisiones", "Hora_cierre"]],
                    "A1"
                )

            dia_hoy   = datetime.now().strftime("%d/%m/%Y")
            hora_hoy  = datetime.now().strftime("%d/%m/%Y %H:%M")
            filas_nuevas = []
            for jefe, d in sorted(jefes.items(), key=lambda x: x[1]["prom_mins"]):
                filas_nuevas.append([
                    dia_hoy,
                    jefe.title(),
                    d["min_mins"],
                    d["prom_mins"],
                    d["max_mins"],
                    d["total_rem"],
                    hora_hoy,
                ])
            hoja_t.append_rows(filas_nuevas, value_input_option="RAW")
            log.info(f"KPI tiempos guardado en KPI_TIEMPOS ({len(jefes)} jefes, día {dia_hoy}) ✅")
        except Exception as e2:
            log.warning(f"No se pudo escribir KPI en KPI_TIEMPOS: {e2}")

    except Exception as e:
        log.error(f"Error enviando KPI jefes tiempos: {e}")


def enviar_kpi_vendedores_tiempos(datos_oms=None, csv_xd=None):
    """
    Calcula KPI de vendedores (T2→T3: Asignación→Etiqueta Generada)
    y manda mensaje al espacio Tiempos.
    """
    try:
        if not datos_oms:
            datos_oms = _get_datos_oms()
        if not datos_oms:
            log.warning("KPI vendedores: no hay remisiones de OMS en cache")
            return

        if not csv_xd:
            csv_xd = _get_csv_xd()
        if not csv_xd:
            log.warning("KPI vendedores: no hay CSV XD descargado")
            return

        kpi        = calcular_kpi_xd(datos_oms, csv_xd)
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

        lineas.append(f"\n_Argos — {fecha_now}_")
        lineas.append("〰〰〰〰〰〰〰〰〰〰〰〰〰〰〰")

        M.post_chat_con_reintento(M.WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("KPI vendedores tiempos enviado al espacio Tiempos ✅")

    except Exception as e:
        log.error(f"Error enviando KPI vendedores tiempos: {e}")
