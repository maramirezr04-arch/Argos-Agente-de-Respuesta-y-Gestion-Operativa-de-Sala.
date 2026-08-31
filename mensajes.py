"""mensajes.py — Construcción y envío de mensajes a Google Chat.

Arma las cards (Resumen General, por piso, por jefe, apertura, cierre,
pendientes de ayer), maneja mensajes reescribibles, alertas de anomalía,
ranking, comparativa semanal y mensajes programados. Extraído de main.py;
globales y funciones de main via M, config/utils por import directo.
"""

import os, csv, json, time, logging, re, hashlib, math, random
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter

import requests
import gspread

class _LazyMain:
    """Acceso diferido a main.py — evita el import circular al arrancar
    (main.py hace 'from mensajes import ...' antes de terminar de ejecutarse)."""
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

log = logging.getLogger("argos.mensajes")

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
        if os.path.exists(M.QUEUE_FILE):
            with open(M.QUEUE_FILE, "r", encoding="utf-8") as f:
                cola = json.load(f)
        cola.append({"url": url, "payload": payload, "ts": datetime.now().isoformat()})
        cola = cola[-100:]
        with open(M.QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(cola, f)
    except Exception:
        pass


def reenviar_cola_mensajes():
    """Reintenta mandar mensajes encolados al inicio."""
    try:
        if not os.path.exists(M.QUEUE_FILE):
            return
        with open(M.QUEUE_FILE, "r", encoding="utf-8") as f:
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
        with open(M.QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(pendientes, f)
    except Exception:
        pass


def _buscar_webhook_vendedor(nombre):
    """Busca el webhook de un vendedor: exacta primero, luego por 2+ palabras.
    Retorna (url, ciclos_personales) — ciclos 0 significa usar el default global."""
    n = nombre.strip().upper()
    if not n:
        return "", 0
    if n in M.WEBHOOKS_VENDEDORES_CACHE:
        d = M.WEBHOOKS_VENDEDORES_CACHE[n]
        return d["url"], d.get("ciclos", 0)
    palabras = set(n.split())
    for clave, d in M.WEBHOOKS_VENDEDORES_CACHE.items():
        if d.get("url") and len(palabras & set(clave.split())) >= 2:
            return d["url"], d.get("ciclos", 0)
    return "", 0


def _coincide_prioridad_manual(row, prioridades):
    """
    Revisa las reglas de prioridad manual que el usuario configura desde el
    dashboard (hoja PRIORIDADES: Campo, Valor, Activo). Se SUMAN a la regla
    fija de _categoria_urgente (HD0D/HD1D/C&C), no la reemplazan.

    Cada regla dice "si el campo X contiene el valor Y, es prioridad" — por
    ejemplo Campo=jefe, Valor=JUANITA, o Campo=tipo_entrega, Valor=Domicilio.
    Devuelve la etiqueta de la primera regla que haga match, o None.
    """
    if not prioridades:
        return None
    CAMPO_API = {
        "seccion":      "Seccion",
        "jefe":         "NombreJefeDePiso",
        "vendedor":     "NombreVendedor",
        "tipo_entrega": "TipoEntrega",
        "tipo_orden":   "TipoOrden",
        "status":       "StatusRemision",
    }
    for regla in prioridades:
        campo_api = CAMPO_API.get(str(regla.get("campo", "")).strip().lower())
        if not campo_api:
            continue
        valor_regla = str(regla.get("valor", "")).strip().lower()
        if not valor_regla:
            continue
        valor_row = str(row.get(campo_api, "")).strip().lower()
        if valor_regla in valor_row:
            return str(regla.get("valor", "")).strip()
    return None


def detectar_vencidas(datos, dir_dict, hist_dict, descansos):
    ESTATUS_PENDIENTE = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]

    vencidas = []
    for row in datos:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status not in ESTATUS_PENDIENTE:
            continue
        fecha_asig = row.get("FechaAsignacionTienda", "")
        minutos    = calcular_minutos(fecha_asig)
        if minutos < M.MINUTOS_VENCIDA:
            continue

        sec          = str(row.get("Seccion", "")).strip().replace(".0","")
        remision     = str(row.get("Remision", ""))
        descripcion  = str(row.get("DescripcionSku", ""))
        nom_vendedor = str(row.get("NombreVendedor", "")).strip()
        nom_jefe     = str(row.get("NombreJefeDePiso", "")).strip()
        nom_seccion  = dir_dict.get(sec, {}).get("nombre_seccion", "")
        tipo_entrega = str(row.get("TipoEntrega", "")).strip()
        fuente_jefe  = "API"

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

    log.info(f"Remisiones vencidas (+{M.MINUTOS_VENCIDA} min): {len(vencidas)}")
    return vencidas


def enviar_apertura(datos, dir_dict, hist_dict, descansos=None):
    fecha_now  = datetime.now().strftime("%d/%m/%Y %H:%M")
    ESTATUS    = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    espera_ayer = 0; espera_hoy = 0; etiq_ayer = 0; etiq_hoy = 0
    jefes_ayer  = {}

    for row in datos:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status not in ESTATUS:
            continue
        fecha_str = str(row.get("FechaAsignacionTienda", "")).strip()
        es_ayer   = es_de_ayer(fecha_str)

        if status == "Mercancia en Espera de Entrega":
            if es_ayer: espera_ayer += 1
            else:       espera_hoy  += 1
        elif status == "Etiqueta Generada":
            if es_ayer: etiq_ayer += 1
            else:       etiq_hoy  += 1

        if es_ayer:
            nom_jefe = str(row.get("NombreJefeDePiso", "")).strip()
            sec      = str(row.get("Seccion", "")).strip().replace(".0","")
            if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
                nom_jefe = dir_dict.get(sec, {}).get("jefe", "Sin Asignar")
            if nom_jefe not in jefes_ayer:
                jefes_ayer[nom_jefe] = {"count": 0, "secciones": set()}
            jefes_ayer[nom_jefe]["count"] += 1
            if sec:
                jefes_ayer[nom_jefe]["secciones"].add(sec)

    total = espera_ayer + espera_hoy + etiq_ayer + etiq_hoy

    emoji_apertura = random.choice(["🌅", "🌄", "☀️", "🌞", "🏪", "👋"])
    msg_apertura   = random.choice(M.MENSAJES_APERTURA).format(total=total)

    lineas = [
        emoji_apertura + " *Buenos dias*",
        "",
        msg_apertura,
        "",
        "📊 *Desglose:*",
        "🔴 Mercancia en Espera: *" + str(espera_ayer + espera_hoy) + "*",
        "  🟣 De ayer: *" + str(espera_ayer) + "* | De hoy: *" + str(espera_hoy) + "*",
        "🏷️ Etiquetas Generadas: *" + str(etiq_ayer + etiq_hoy) + "*",
        "  🟣 De ayer: *" + str(etiq_ayer) + "* | De hoy: *" + str(etiq_hoy) + "*",
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
    lineas.append("_Argos — " + fecha_now + "_")

    mensaje = "\n".join(lineas)
    post_chat_con_reintento(M.WEBHOOK, {"text": mensaje})

    # Espacio jefes → misma card que Resumen General + intro de buenos días
    por_piso_ap = _construir_por_piso(datos, dir_dict, hist_dict, descansos or {})
    if por_piso_ap:
        total_ayer_ap = sum(
            ds["count"]
            for ip in por_piso_ap.values()
            for ij in ip["jefes"].values()
            for ds in ij["de_ayer"].values()
        )
        total_rem_ap = sum(
            ds["count"]
            for ip in por_piso_ap.values()
            for ij in ip["jefes"].values()
            for grp in [ij["en_tiempo"], ij["vencidas"], ij["de_ayer"]]
            for ds in grp.values()
        )
        intro = (f"{emoji_apertura} *Buenos días*\n{msg_apertura}\n"
                 f"🟣 De ayer: *{total_ayer_ap}*  ·  De hoy: *{total_rem_ap - total_ayer_ap}*")
        card_apertura = construir_card_resumen_general(por_piso_ap, dir_dict, fecha_now, intro_texto=intro)
    else:
        card_apertura = construir_card_apertura(
            emoji_apertura, msg_apertura,
            espera_ayer, espera_hoy, etiq_ayer, etiq_hoy,
            jefes_ayer, dir_dict, fecha_now
        )
    post_chat_con_reintento(M.WEBHOOK_JEFES, card_apertura)
    M.marcar_apertura_enviada()
    log.info("Mensaje de apertura enviado al espacio reporte y jefes ✅")


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

    if post_chat_con_reintento(M.WEBHOOK, {"text": "\n".join(lineas)}):
        log.info("Notificacion vencidas enviada al espacio reporte ✅")
    else:
        log.warning("No se pudo enviar notificacion de vencidas al espacio reporte ❌ (encolada)")


def enviar_chat(resumen, exito=True, error=""):
    fecha_ayer, fecha_hoy = get_fechas()
    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    if exito:
        try:
            tiempos      = M.cargar_tiempos()
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
    if post_chat_con_reintento(M.WEBHOOK, {"text": texto}):
        log.info("Mensaje indicadores enviado al espacio reporte ✅")
    else:
        log.warning("No se pudo enviar mensaje de indicadores al espacio reporte ❌ (encolada)")


def _buscar_webhook_jefe(nombre):
    """Busca el webhook de un jefe con coincidencia flexible.
    Primero exacta, luego por palabras clave (apellido + primer nombre).
    Evita mandar dos veces al mismo webhook.
    """
    n = nombre.strip().upper()

    # 1. Exacta
    if n in M.WEBHOOKS_JEFES_CACHE:
        return M.WEBHOOKS_JEFES_CACHE[n]

    # 2. Flexible: el nombre del bot contiene alguna clave del sheet, o viceversa
    palabras_bot = set(n.split())
    for clave, url in M.WEBHOOKS_JEFES_CACHE.items():
        if not url:
            continue
        palabras_hoja = set(clave.strip().upper().split())
        # Si comparten al menos 2 palabras (ej. primer nombre + apellido)
        if len(palabras_bot & palabras_hoja) >= 2:
            log.info(f"Webhook flexible: '{nombre}' → '{clave}'")
            return url

    return ""


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
    if webhook_jefe in M._WEBHOOKS_YA_ENVIADOS:
        log.info(f"Webhook de {jefe} ya usado en este ciclo — omitiendo duplicado")
        return
    M._WEBHOOKS_YA_ENVIADOS.add(webhook_jefe)

    payload = construir_card_jefe_individual(jefe, info_j, ubicacion, fecha_now, es_sustituto, jefe_original)

    # Una card al día por jefe que se reescribe el resto del día
    mensajes_jefes = _leer_mensajes_reescribibles(M.MENSAJES_JEFES_IND_FILE)
    hoy      = datetime.now().strftime("%d/%m/%Y")
    clave    = jefe.strip().upper()
    msg_name = mensajes_jefes.get(clave, {}).get("name", "")
    nuevo    = _enviar_o_reescribir(webhook_jefe, payload, msg_name)
    if nuevo:
        mensajes_jefes[clave] = {"name": nuevo, "fecha": hoy}
        _guardar_mensajes_reescribibles(M.MENSAJES_JEFES_IND_FILE, mensajes_jefes)
    else:
        post_chat_con_reintento(webhook_jefe, payload)
    log.info(f"Mensaje individual enviado a jefe {jefe}")


def construir_card_piso(ubicacion, info_piso, fecha_now):
    """Construye el payload Card v2 para el mensaje de un piso al espacio jefes."""
    sections = []

    sections.append({
        "header": "🟢 < 15 min   🟡 15–20 min   🔴 > 20 min",
        "widgets": []
    })

    for jefe, info_j in sorted(info_piso["jefes"].items()):
        verde = sum(ds["count"] for ds in info_j["en_tiempo"].values())
        amarillo = sum(
            ds["count"] for ds in info_j["en_tiempo"].values()
            if ds["max_min"] >= M.MINUTOS_VENCIDA * 0.75
        )
        verde_puro = verde - amarillo
        rojo = (
            sum(ds["count"] for ds in info_j["vencidas"].values()) +
            sum(ds["count"] for ds in info_j["de_ayer"].values())
        )
        sin_asignar = sum(
            ds["count"]
            for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]]
            for ven, ds in grp.items() if ven == "Sin asignar"
        )

        chips = []
        if verde_puro > 0:   chips.append({"label": f"🟢 {verde_puro} rem"})
        if amarillo > 0:     chips.append({"label": f"🟡 {amarillo} rem"})
        if rojo > 0:         chips.append({"label": f"🔴 {rojo} rem"})
        if sin_asignar > 0:  chips.append({"label": f"⚠️ {sin_asignar} sin asignar"})
        if not chips:        chips.append({"label": "Sin remisiones"})

        # chipList de resumen + detalle por vendedor colapsable
        widgets = [{"chipList": {"chips": chips}}]
        for emoji, grp in [("🟣", info_j["de_ayer"]), ("🔴", info_j["vencidas"]), ("🟢", info_j["en_tiempo"])]:
            for ven, ds in sorted(grp.items(), key=lambda x: -x[1]["max_min"]):
                if ds["count"] == 0:
                    continue
                icono_ven = "⚠️" if ven == "Sin asignar" else emoji
                t_str = calcular_tiempo_espera_str(ds["max_min"]) if ds["max_min"] > 0 else ""
                texto = f"<b>{ds['count']} rem</b>" + (f" · {t_str}" if t_str else "")
                widgets.append({"decoratedText": {
                    "topLabel": f"{icono_ven} {ven}", "text": texto,
                    "startIcon": {"knownIcon": "PERSON"}
                }})
        sec = {"header": f"👤 {jefe}", "widgets": widgets}
        if len(widgets) > 1:
            sec["collapsible"] = True
            sec["uncollapsibleWidgetsCount"] = 1
        sections.append(sec)

    total = sum(
        sum(ds["count"] for ds in grp.values())
        for info_j in info_piso["jefes"].values()
        for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]]
    )
    sections.append({
        "widgets": [
            {"divider": {}},
            {
                "decoratedText": {
                    "topLabel": f"Total {ubicacion}",
                    "text": f"<b>{total} remisiones</b>",
                    "icon": {"knownIcon": "CONFIRMATION_NUMBER_ICON"}
                }
            }
        ]
    })

    card_id = "piso-" + ubicacion.replace(" ", "_")
    return {
        "cardsV2": [{
            "cardId": card_id,
            "card": {
                "header": {
                    "title": f"🏬 {ubicacion}",
                    "subtitle": f"Liverpool Tienda 456 — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/store/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def _url_chart_pisos(datos_piso):
    """Genera URL de quickchart.io con barras apiladas horizontales.
    datos_piso: lista de (nombre, verde, rojo, morado) por piso ordenados por indice.
    """
    import json as _json, urllib.parse as _up
    labels        = [d[0] for d in datos_piso]
    data_verde    = [d[1] for d in datos_piso]
    data_rojo     = [d[2] for d in datos_piso]
    data_morado   = [d[3] for d in datos_piso]
    cfg = {
        "type": "horizontalBar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "En tiempo",
                    "data": data_verde,
                    "backgroundColor": "#34A853"
                },
                {
                    "label": "Vencidas",
                    "data": data_rojo,
                    "backgroundColor": "#EA4335"
                },
                {
                    "label": "De ayer",
                    "data": data_morado,
                    "backgroundColor": "#9C27B0"
                }
            ]
        },
        "options": {
            "legend": {"display": True, "position": "top"},
            "scales": {
                "xAxes": [{"stacked": True, "ticks": {"beginAtZero": True, "precision": 0}}],
                "yAxes": [{"stacked": True}]
            },
            "plugins": {
                "datalabels": {
                    "display": True,
                    "color": "white",
                    "font": {"weight": "bold", "size": 14},
                    "formatter": "function(v){return v>0?v:''}"
                }
            }
        }
    }
    return "https://quickchart.io/chart?c=" + _up.quote(_json.dumps(cfg, separators=(",", ":")))


def construir_card_resumen_general(por_piso, dir_dict, fecha_now, intro_texto=None):
    """Card compacta: gráfica + una sección colapsable por piso (chips + jefes tras 'ver más').
    intro_texto: párrafo opcional al tope (para apertura de Buenos días)."""
    sections = []
    if intro_texto:
        sections.append({"widgets": [{"textParagraph": {"text": intro_texto}}]})

    # ── Gráfica ───────────────────────────────────────────────
    datos_chart = []
    for p_idx in sorted(por_piso.keys()):
        info_piso = por_piso[p_idx]
        verde  = sum(ds["count"] for info_j in info_piso["jefes"].values() for ds in info_j["en_tiempo"].values())
        rojo   = sum(ds["count"] for info_j in info_piso["jefes"].values() for ds in info_j["vencidas"].values())
        morado = sum(ds["count"] for info_j in info_piso["jefes"].values() for ds in info_j["de_ayer"].values())
        datos_chart.append((info_piso["ubicacion"], verde, rojo, morado))

    chart_url = _url_chart_pisos(datos_chart)
    sections.append({"widgets": [{"image": {"imageUrl": chart_url, "altText": "Remisiones por piso"}}]})

    # ── Una sección colapsable por piso ───────────────────────
    # Chips de estatus siempre visibles; jefes detrás del "ver más".
    for p_idx in sorted(por_piso.keys()):
        info_piso = por_piso[p_idx]
        ubicacion = info_piso["ubicacion"]

        sc       = info_piso.get("status_counts", {})
        espera   = sc.get("Mercancia en Espera de Entrega", 0)
        etiqueta = sc.get("Etiqueta Generada", 0)
        ayer_piso = sum(
            ds["count"]
            for info_j in info_piso["jefes"].values()
            for ds in info_j["de_ayer"].values()
        )
        sin_piso = sum(
            info_j[grp].get("Sin asignar", {}).get("count", 0)
            for info_j in info_piso["jefes"].values()
            for grp in ("de_ayer", "vencidas", "en_tiempo")
        )
        piso_chips = []
        if ayer_piso: piso_chips.append({"label": f"🟣 De ayer: {ayer_piso}"})
        if sin_piso:  piso_chips.append({"label": f"⚠️ Sin asignar: {sin_piso}"})
        if espera:    piso_chips.append({"label": f"🔴 Mercancía en Espera: {espera}"})
        if etiqueta:  piso_chips.append({"label": f"🏷️ Etiquetas Generadas: {etiqueta}"})
        if not piso_chips:
            total_piso = sum(
                ds["count"]
                for info_j in info_piso["jefes"].values()
                for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]]
                for ds in grp.values()
            )
            piso_chips.append({"label": f"🏬 {total_piso} rem"})

        piso_widgets = [{"chipList": {"chips": piso_chips}}]

        jefes_ordenados = sorted(
            info_piso["jefes"].items(),
            key=lambda x: -sum(ds["count"] for grp in [x[1]["en_tiempo"], x[1]["vencidas"], x[1]["de_ayer"]] for ds in grp.values())
        )

        for jefe, info_j in jefes_ordenados:
            # Sin asignar — separados para resaltarlos con ⚠️
            sin_ayer  = info_j["de_ayer"].get("Sin asignar", {}).get("count", 0)
            sin_venc  = info_j["vencidas"].get("Sin asignar", {}).get("count", 0)
            sin_verde = info_j["en_tiempo"].get("Sin asignar", {}).get("count", 0)
            total_sin = sin_ayer + sin_venc + sin_verde

            ayer  = sum(ds["count"] for k, ds in info_j["de_ayer"].items()  if k != "Sin asignar")
            venc  = sum(ds["count"] for k, ds in info_j["vencidas"].items() if k != "Sin asignar")
            verde = sum(ds["count"] for k, ds in info_j["en_tiempo"].items() if k != "Sin asignar")
            total_j = ayer + venc + verde + total_sin
            if total_j == 0:
                continue

            partes = []
            if ayer:      partes.append(f"🟣 {ayer}")
            if venc:      partes.append(f"🔴 {venc}")
            if verde:     partes.append(f"🟢 {verde}")
            if total_sin:
                sin_emoji = "🟣" if sin_ayer else ("🔴" if sin_venc else "🟢")
                partes.append(f"⚠️ {sin_emoji} {total_sin} s/a")

            tc = info_j.get("tipo_counts", {})
            for tipo, cnt in sorted(tc.items(), key=lambda x: -x[1]):
                partes.append(f"🚨 {cnt} {tipo}")
            conteos_txt = "  ·  ".join(partes)

            genero  = get_genero(jefe)
            emoji_g = "👩‍💼" if genero == "jefa" else "👨‍💼"
            piso_widgets.append({"decoratedText": {
                "topLabel": f"{emoji_g} {jefe}",
                "text": conteos_txt,
                "startIcon": {"knownIcon": "PERSON"}
            }})

        sections.append({
            "header": f"🏢 {ubicacion}",
            "widgets": piso_widgets,
            "collapsible": True,
            "uncollapsibleWidgetsCount": 1,
        })

    gran_total = sum(v + r + m for _, v, r, m in datos_chart)

    return {
        "cardsV2": [{
            "cardId": "resumen-general",
            "card": {
                "header": {
                    "title": "📊 Resumen General",
                    "subtitle": f"{gran_total} rem pendientes — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/bar_chart/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def construir_card_prioridades_grupo(por_piso, dir_dict, fecha_now):
    """
    Card SEPARADA del Resumen General — solo lista lo que está marcado como
    prioridad (regla fija HD0D/HD1D/C&C/C&C Expreso + reglas manuales del
    dashboard), agrupada por piso y jefe. Se manda aparte al grupo de jefes.
    Devuelve None si no hay nada en prioridad este ciclo (no se manda vacía).
    """
    sections = []
    total = 0
    for p_idx in sorted(por_piso.keys()):
        info_piso = por_piso[p_idx]
        ubicacion = info_piso["ubicacion"]
        jefes_con_prioridad = [
            (jefe, info_j) for jefe, info_j in info_piso["jefes"].items()
            if info_j.get("tipo_counts")
        ]
        if not jefes_con_prioridad:
            continue
        jefes_con_prioridad.sort(key=lambda x: -sum(x[1]["tipo_counts"].values()))

        widgets = []
        for jefe, info_j in jefes_con_prioridad:
            tc = info_j["tipo_counts"]
            detalle = "  ·  ".join(f"{tipo}: {cnt}" for tipo, cnt in sorted(tc.items(), key=lambda x: -x[1]))
            total += sum(tc.values())
            genero  = get_genero(jefe)
            emoji_g = "👩‍💼" if genero == "jefa" else "👨‍💼"
            widgets.append({"decoratedText": {
                "topLabel": f"{emoji_g} {jefe}",
                "text": detalle,
                "startIcon": {"knownIcon": "STAR"}
            }})
        sections.append({"header": f"🏢 {ubicacion}", "widgets": widgets})

    if not sections:
        return None

    return {
        "cardsV2": [{
            "cardId": "prioridades-grupo",
            "card": {
                "header": {
                    "title": "🚨 Prioridades del día",
                    "subtitle": f"{total} remisiones — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/priority_high/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def _card_total_section(label, total):
    return {
        "widgets": [
            {"divider": {}},
            {"decoratedText": {
                "topLabel": label,
                "text": f"<b>{total} remisiones</b>",
                "icon": {"knownIcon": "CONFIRMATION_NUMBER_ICON"}
            }}
        ]
    }


def _card_seccion_grp(titulo, grp, emoji_grp):
    """Sección colapsable con chipList de resumen + fila por vendedor."""
    total = sum(ds["count"] for ds in grp.values())
    if total == 0:
        return None
    sin_asignar = sum(ds["count"] for ven, ds in grp.items() if ven == "Sin asignar")
    con_ven     = total - sin_asignar
    chips = []
    if con_ven > 0:      chips.append({"label": f"{emoji_grp} {con_ven} rem"})
    if sin_asignar > 0:  chips.append({"label": f"⚠️ {sin_asignar} sin asignar"})
    widgets = [{"chipList": {"chips": chips}}]
    for ven, ds in sorted(grp.items(), key=lambda x: -x[1]["max_min"]):
        if ds["count"] == 0:
            continue
        icono = "⚠️" if ven == "Sin asignar" else emoji_grp
        t_str = calcular_tiempo_espera_str(ds["max_min"]) if ds["max_min"] > 0 else ""
        texto = f"<b>{ds['count']} rem</b>" + (f" · {t_str}" if t_str else "")
        widgets.append({"decoratedText": {
            "topLabel": f"{icono} {ven}", "text": texto,
            "startIcon": {"knownIcon": "PERSON"}
        }})
    sec = {"header": titulo, "widgets": widgets}
    if len(widgets) > 1:
        sec["collapsible"] = True
        sec["uncollapsibleWidgetsCount"] = 1
    return sec


def construir_card_jefe_individual(jefe, info_j, ubicacion, fecha_now, es_sustituto=False, jefe_original=""):
    sections = []
    if es_sustituto and jefe_original:
        primer = jefe_original.split()[0].capitalize()
        sections.append({"widgets": [{"textParagraph": {
            "text": f"🔄 <b>Sustituto de {primer}</b> (descansa hoy)"
        }}]})

    tc = info_j.get("tipo_counts", {})
    if tc:
        widgets_prioridad = [
            {"decoratedText": {
                "topLabel": tipo,
                "text": f"<b>{cnt}</b> remisiones",
                "startIcon": {"knownIcon": "STAR"}
            }}
            for tipo, cnt in sorted(tc.items(), key=lambda x: -x[1])
        ]
        sections.append({"header": "🚨 Prioridad", "widgets": widgets_prioridad})

    for titulo, grp, emoji in [
        ("🟣 De ayer sin atender", info_j["de_ayer"],  "🟣"),
        ("🔴 Vencidas (+20 min)",  info_j["vencidas"], "🔴"),
        ("⏰ En tiempo",           info_j["en_tiempo"],"🟢"),
    ]:
        sec = _card_seccion_grp(titulo, grp, emoji)
        if sec:
            sections.append(sec)

    total = sum(ds["count"] for grp in [info_j["en_tiempo"], info_j["vencidas"], info_j["de_ayer"]] for ds in grp.values())
    sections.append(_card_total_section("Total", total))

    genero  = get_genero(jefe)
    emoji_g = "👩‍💼" if genero == "jefa" else "👨‍💼"
    return {
        "cardsV2": [{
            "cardId": "jefe-" + jefe.replace(" ", "_"),
            "card": {
                "header": {
                    "title": f"{emoji_g} {jefe}",
                    "subtitle": f"{ubicacion} — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/person/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def construir_card_apertura(emoji, msg_texto, espera_ayer, espera_hoy, etiq_ayer, etiq_hoy, jefes_ayer, dir_dict, fecha_now):
    sections = [{"widgets": [{"textParagraph": {"text": msg_texto}}]}]

    total_espera = espera_ayer + espera_hoy
    total_etiq   = etiq_ayer   + etiq_hoy
    widgets_desglose = []
    if total_espera > 0:
        widgets_desglose.append({"decoratedText": {
            "topLabel": "🔴 Mercancía en Espera",
            "text": f"<b>{total_espera}</b>  ·  🟣 ayer: {espera_ayer}  |  hoy: {espera_hoy}",
            "startIcon": {"knownIcon": "CLOCK"}
        }})
    if total_etiq > 0:
        widgets_desglose.append({"decoratedText": {
            "topLabel": "🏷️ Etiquetas Generadas",
            "text": f"<b>{total_etiq}</b>  ·  🟣 ayer: {etiq_ayer}  |  hoy: {etiq_hoy}",
            "startIcon": {"knownIcon": "BOOKMARK"}
        }})
    if widgets_desglose:
        sections.append({"header": "📊 Desglose inicial", "widgets": widgets_desglose})

    for jefe, info in sorted(jefes_ayer.items(), key=lambda x: -x[1]["count"]):
        nom_secs = " | ".join(
            "Sec " + s + (" " + dir_dict.get(s, {}).get("nombre_seccion", "") if dir_dict.get(s, {}).get("nombre_seccion") else "")
            for s in sorted(info["secciones"])
        )
        sections.append({"header": f"⚠️ {jefe}", "widgets": [{"decoratedText": {
            "topLabel": f"{info['count']} pendientes de ayer",
            "text": nom_secs or "—",
            "startIcon": {"knownIcon": "CONFIRMATION_NUMBER_ICON"}
        }}]})

    return {
        "cardsV2": [{
            "cardId": "apertura",
            "card": {
                "header": {
                    "title": f"{emoji} Buenos días",
                    "subtitle": f"Liverpool Tienda 456 — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/wb_sunny/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def construir_card_cierre(emoji, msg_texto, espera, etiq, jefes_pendientes, fecha_now):
    sections = [{"widgets": [{"textParagraph": {"text": msg_texto}}]}]
    sections.append({"header": "📊 Resumen de cierre", "widgets": [
        {"decoratedText": {"topLabel": "🔴 Mercancía en Espera", "text": f"<b>{espera}</b>", "startIcon": {"knownIcon": "CLOCK"}}},
        {"decoratedText": {"topLabel": "🏷️ Etiquetas Generadas",  "text": f"<b>{etiq}</b>",   "startIcon": {"knownIcon": "BOOKMARK"}}},
    ]})

    for jefe, secciones in sorted(jefes_pendientes.items()):
        total_j = sum(len(v) for v in secciones.values())
        widgets = [{"chipList": {"chips": [{"label": f"📋 {total_j} rem"}]}}]
        for sec_key, minutos_list in sorted(secciones.items()):
            widgets.append({"decoratedText": {
                "topLabel": f"📍 {sec_key}",
                "text": calcular_tiempo_espera_str(max(minutos_list)),
                "startIcon": {"knownIcon": "PERSON"}
            }})
        sec = {"header": f"⚠️ {jefe}", "widgets": widgets}
        if len(widgets) > 1:
            sec["collapsible"] = True
            sec["uncollapsibleWidgetsCount"] = 1
        sections.append(sec)

    return {
        "cardsV2": [{
            "cardId": "cierre",
            "card": {
                "header": {
                    "title": f"{emoji} Buenas noches",
                    "subtitle": f"Liverpool Tienda 456 — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/nights_stay/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def construir_card_pendientes_ayer(por_jefe, total_ayer, fecha_now):
    sections = []
    for jefe, secciones in sorted(por_jefe.items(), key=lambda x: -sum(d["count"] for d in x[1].values())):
        total_j = sum(d["count"] for d in secciones.values())
        sin_ven = sum(d["sin_vendedor"] for d in secciones.values())
        chips   = [{"label": f"🟣 {total_j} rem"}]
        if sin_ven > 0:
            chips.append({"label": f"⚠️ {sin_ven} sin vendedor"})
        widgets = [{"chipList": {"chips": chips}}]
        for sec_key, ds in sorted(secciones.items()):
            texto = f"<b>{ds['count']} rem</b> · {calcular_tiempo_espera_str(ds['max_min'])}"
            if ds["sin_vendedor"]:
                texto += f" · ⚠️ {ds['sin_vendedor']} sin vendedor"
            widgets.append({"decoratedText": {
                "topLabel": f"📍 {sec_key}", "text": texto,
                "startIcon": {"knownIcon": "BOOKMARK"}
            }})
        sec = {"header": f"⚠️ {jefe}", "widgets": widgets}
        if len(widgets) > 1:
            sec["collapsible"] = True
            sec["uncollapsibleWidgetsCount"] = 1
        sections.append(sec)

    return {
        "cardsV2": [{
            "cardId": "pendientes-ayer",
            "card": {
                "header": {
                    "title": "🟣 Pendientes de ayer",
                    "subtitle": f"{total_ayer} sin resolver — {fecha_now}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/warning/v6/24px.svg",
                    "imageType": "CIRCLE"
                },
                "sections": sections
            }
        }]
    }


def _leer_mensajes_reescribibles(archivo):
    """Lee el registro {clave: {name, fecha}} y descarta entradas de días anteriores."""
    hoy = datetime.now().strftime("%d/%m/%Y")
    try:
        if os.path.exists(archivo):
            with open(archivo) as f:
                data = json.load(f)
            # Limpieza: solo conservar entradas de hoy (formato nuevo)
            return {k: v for k, v in data.items()
                    if isinstance(v, dict) and v.get("fecha") == hoy}
    except Exception:
        pass
    return {}


def _guardar_mensajes_reescribibles(archivo, d):
    try:
        with open(archivo, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def _construir_texto_vendedor(vendedor, v, fecha_now):
    todos = v["de_ayer"] + v["vencidas"] + v["en_tiempo"]
    partes = [fecha_now, "👤 *" + vendedor.title() + "*"]
    for it in sorted(todos, key=lambda x: -x["minutos"]):
        sku_txt  = " · SKU " + it["sku"] if it["sku"] else ""
        tipo_txt = "  " + it["tipo"] + " (prioridad)" if it.get("tipo") else ""
        partes.append("    • *" + it["remision"] + "*" + sku_txt +
                      " — lleva " + calcular_tiempo_espera_str(it["minutos"]) + tipo_txt)
    partes.append("  🟢 *Total: " + str(len(todos)) + " remisiones*")
    return "\n".join(partes)


def _enviar_o_reescribir(webhook_url, payload, msg_name):
    """Reescribe el mensaje existente (PATCH) o crea uno nuevo (POST).
    Funciona para texto y para cardsV2 usando la misma key+token del webhook.
    Devuelve el name del mensaje para guardarlo."""
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(webhook_url)
    params = parse_qs(parsed.query)
    key    = params.get("key",   [""])[0]
    token  = params.get("token", [""])[0]
    update_mask = "cardsV2" if "cardsV2" in payload else "text"

    if msg_name and key and token:
        patch_url = (f"https://chat.googleapis.com/v1/{msg_name}"
                     f"?key={key}&token={token}&updateMask={update_mask}")
        try:
            r = requests.patch(patch_url, json=payload, timeout=15)
            if 200 <= r.status_code < 300:
                return msg_name
            log.info(f"PATCH {r.status_code} — creando nuevo mensaje")
        except Exception as e:
            log.info(f"PATCH error: {e} — creando nuevo mensaje")

    try:
        r = requests.post(webhook_url, json=payload, timeout=15)
        if 200 <= r.status_code < 300:
            return r.json().get("name", "")
    except Exception as e:
        log.warning(f"Error enviando mensaje reescribible: {e}")
    return ""


def _construir_por_piso(todas_remisiones, dir_dict, hist_dict, descansos, prioridades=None):
    """Agrupa remisiones activas en estructura por_piso[p_idx] → jefes → vendedores."""
    ESTATUS_ACTIVOS = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]

    # Diagnóstico: muestra las primeras 5 entradas del dir_dict para verificar ubicacion
    muestra = [(k, v.get("ubicacion","—"), v.get("jefe","—")) for k, v in list(dir_dict.items())[:5]]
    log.info(f"[DIAG dir_dict] primeras 5 secciones: {muestra}")

    por_piso = {}
    for row in todas_remisiones:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status not in ESTATUS_ACTIVOS:
            continue

        sec_raw = str(row.get("Seccion", "")).strip().replace(".0","")
        try:
            sec = str(int(sec_raw))
        except Exception:
            sec = sec_raw
        fecha_asig   = row.get("FechaAsignacionTienda", "")
        minutos      = calcular_minutos(fecha_asig)
        nom_jefe     = str(row.get("NombreJefeDePiso", "")).strip()
        nom_vendedor = str(row.get("NombreVendedor", "")).strip()
        tipo_entrega = str(row.get("TipoEntrega", "")).strip()
        tipo_orden   = str(row.get("TipoOrden", "")).strip()
        es_ayer      = es_de_ayer(fecha_asig)
        vencida      = minutos >= M.MINUTOS_VENCIDA

        ubicacion_raw = dir_dict.get(sec, {}).get("ubicacion", "") or dir_dict.get(sec_raw, {}).get("ubicacion", "") or ""
        p_idx    = orden_piso(ubicacion_raw)
        ubicacion = M.NOMBRES_PISOS.get(p_idx, ubicacion_raw.upper() if ubicacion_raw else "SIN PISO")

        if not nom_jefe or nom_jefe in ("", "nan", "Sin Asignar", "UNASSIGNED"):
            if sec in dir_dict and dir_dict[sec].get("jefe"):
                nom_jefe = dir_dict[sec]["jefe"]
            elif sec in hist_dict and hist_dict[sec].get("Jefe"):
                nom_jefe = hist_dict[sec]["Jefe"]

        sustituto = (descansos or {}).get(sec) or (descansos or {}).get(sec_raw)
        jefe = sustituto or nom_jefe or "SIN ASIGNAR"

        if p_idx not in por_piso:
            por_piso[p_idx] = {"ubicacion": ubicacion, "jefes": {}, "status_counts": {}}
        por_piso[p_idx]["status_counts"][status] = por_piso[p_idx]["status_counts"].get(status, 0) + 1
        if jefe not in por_piso[p_idx]["jefes"]:
            por_piso[p_idx]["jefes"][jefe] = {
                "en_tiempo": {}, "vencidas": {}, "de_ayer": {}, "tipo_counts": {},
                "jefe_original": nom_jefe if sustituto else "",
                "es_sustituto": bool(sustituto),
            }

        info_j = por_piso[p_idx]["jefes"][jefe]
        categoria = M._categoria_urgente(tipo_entrega, tipo_orden) or _coincide_prioridad_manual(row, prioridades)
        if categoria:
            info_j["tipo_counts"][categoria] = info_j["tipo_counts"].get(categoria, 0) + 1

        grp = info_j["de_ayer"] if es_ayer else (info_j["vencidas"] if vencida else info_j["en_tiempo"])
        _ven = nom_vendedor.strip()
        ven_key = "Sin asignar" if _ven.lower() in ("", "nan", "sin asignar", "unassigned") else _ven.title()
        if ven_key not in grp:
            grp[ven_key] = {"count": 0, "max_min": 0}
        grp[ven_key]["count"]  += 1
        grp[ven_key]["max_min"] = max(grp[ven_key]["max_min"], minutos)

    return por_piso


def _avisar_jefes_sin_webhook(sin_webhook):
    """Avisa UNA vez al día (al espacio jefes) qué jefes tienen remisiones pero
    no tienen webhook configurado, para que no se pierdan mensajes en silencio."""
    if not sin_webhook:
        return
    marca = "jefes_sin_webhook_avisado.json"
    hoy = datetime.now().strftime("%d/%m/%Y")
    try:
        if os.path.exists(marca):
            with open(marca, encoding="utf-8") as f:
                if json.load(f).get("fecha") == hoy:
                    return  # ya se avisó hoy
    except Exception:
        pass
    lista = "\n".join(f"• {j.title()}" for j in sorted(sin_webhook))
    texto = ("⚠️ *Jefes con remisiones pero SIN webhook configurado*\n"
             f"No reciben su card individual. Agregar su webhook en la hoja "
             f"WEBHOOKS_JEFES:\n{lista}")
    if M.WEBHOOK and post_chat_con_reintento(M.WEBHOOK, {"text": texto}):
        try:
            with open(marca, "w", encoding="utf-8") as f:
                json.dump({"fecha": hoy}, f)
        except Exception:
            pass
        log.info(f"Aviso de {len(sin_webhook)} jefe(s) sin webhook enviado")


def enviar_mensaje_jefes(todas_remisiones, dir_dict, hist_dict, descansos, jefes_en_descanso=None, prioridades=None):
    """Card resumen general al espacio jefes + cards individuales por jefe."""
    if jefes_en_descanso is None:
        jefes_en_descanso = {}

    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_piso  = _construir_por_piso(todas_remisiones, dir_dict, hist_dict, descansos, prioridades)

    if not por_piso:
        log.info("Sin remisiones activas para mensaje de jefes")
        return

    # ── Card resumen general (gráfica + desglose por piso/gerencia) ──────
    payload_resumen = construir_card_resumen_general(por_piso, dir_dict, fecha_now)
    mensajes_pisos  = _leer_mensajes_reescribibles(M.MENSAJES_PISOS_FILE)
    hoy = datetime.now().strftime("%d/%m/%Y")
    msg_name_res = mensajes_pisos.get("resumen-general", {}).get("name", "")
    nuevo_res = _enviar_o_reescribir(M.WEBHOOK_JEFES, payload_resumen, msg_name_res)
    if nuevo_res:
        mensajes_pisos["resumen-general"] = {"name": nuevo_res, "fecha": hoy}
    else:
        post_chat_con_reintento(M.WEBHOOK_JEFES, payload_resumen)
    log.info("Card resumen general enviada al espacio jefes ✅")

    # Card de prioridades — SEPARADA del Resumen General (mensaje aparte).
    payload_prioridades = construir_card_prioridades_grupo(por_piso, dir_dict, fecha_now)
    if payload_prioridades:
        msg_name_prio = mensajes_pisos.get("prioridades-grupo", {}).get("name", "")
        nuevo_prio = _enviar_o_reescribir(M.WEBHOOK_JEFES, payload_prioridades, msg_name_prio)
        if nuevo_prio:
            mensajes_pisos["prioridades-grupo"] = {"name": nuevo_prio, "fecha": hoy}
        else:
            post_chat_con_reintento(M.WEBHOOK_JEFES, payload_prioridades)
        log.info("Card de prioridades enviada al espacio jefes ✅")

    # Las cards individuales por piso ya no se mandan al espacio jefes —
    # el Resumen General las reemplaza. Solo se mandan las cards individuales
    # a cada jefe por su propio webhook.
    sin_webhook = set()
    for p_idx in sorted(por_piso.keys()):
        info_piso = por_piso[p_idx]
        ubicacion = info_piso["ubicacion"]
        for jefe, info_j in sorted(info_piso["jefes"].items()):
            jefe_original = info_j.get("jefe_original", "")
            j = (jefe or "").strip()
            if j and j.upper() != "SIN ASIGNAR" and not _buscar_webhook_jefe(j):
                sin_webhook.add(j.upper())
            enviar_mensaje_jefe_individual(jefe, info_j, ubicacion, fecha_now, dir_dict, jefe_original=jefe_original)

    _guardar_mensajes_reescribibles(M.MENSAJES_PISOS_FILE, mensajes_pisos)
    log.info("Mensajes individuales de jefes enviados ✅")
    _avisar_jefes_sin_webhook(sin_webhook)


def enviar_mensajes_vendedores(todas_remisiones, dir_dict, descansos=None, prioridades=None):
    """Manda a cada vendedor (con webhook activo en WEBHOOKS_VENDEDORES) un
    mensaje con SUS pendientes. Mismo formato/cadencia que el mensaje individual
    de jefe. Vendedores sin webhook o sin pendientes no reciben nada.
    """
    if not M.WEBHOOKS_VENDEDORES_CACHE:
        log.info("Sin webhooks de vendedores configurados — omitiendo mensajes individuales")
        return
    ESTATUS_ACTIVOS = ["Etiqueta Generada", "Mercancia en Espera de Entrega"]

    fecha_now    = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_vendedor = {}

    for row in todas_remisiones:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status not in ESTATUS_ACTIVOS:
            continue
        nom_vendedor = str(row.get("NombreVendedor", "")).strip()
        if not nom_vendedor or nom_vendedor in ("nan", "Sin Asignar", "Sin Seccion", "UNASSIGNED"):
            continue

        fecha_asig   = row.get("FechaAsignacionTienda", "")
        minutos      = calcular_minutos(fecha_asig)
        remision     = str(row.get("Remision", "")).strip().replace(".0", "")
        sku          = str(row.get("Sku", "")).strip().replace(".0", "")
        tipo_entrega = str(row.get("TipoEntrega", "")).strip()
        tipo_orden   = str(row.get("TipoOrden", "")).strip()
        es_ayer      = es_de_ayer(fecha_asig)
        vencida      = minutos >= M.MINUTOS_VENCIDA

        # Ubicacion / piso (igual que en mensaje de jefes)
        sec_raw = str(row.get("Seccion", "")).strip().replace(".0","")
        try:
            sec = str(int(sec_raw))
        except Exception:
            sec = sec_raw
        ubicacion_raw = dir_dict.get(sec, {}).get("ubicacion", "") or dir_dict.get(sec_raw, {}).get("ubicacion", "") or ""
        p_idx     = orden_piso(ubicacion_raw)
        ubicacion = M.NOMBRES_PISOS.get(p_idx, ubicacion_raw.upper() if ubicacion_raw else "SIN PISO")

        if nom_vendedor not in por_vendedor:
            por_vendedor[nom_vendedor] = {
                "en_tiempo": [], "vencidas": [], "de_ayer": [],
                "ubicacion": ubicacion,
            }
        tipo_norm = M._categoria_urgente(tipo_entrega, tipo_orden) or _coincide_prioridad_manual(row, prioridades) or ""
        item = {
            "remision":  remision,
            "sku":       sku,
            "minutos":   minutos,
            "tipo":      tipo_norm,
        }
        v = por_vendedor[nom_vendedor]
        if es_ayer:
            v["de_ayer"].append(item)
        elif vencida:
            v["vencidas"].append(item)
        else:
            v["en_tiempo"].append(item)

    # Contadores individuales por vendedor (persisten entre ciclos)
    contador   = M.leer_contador()
    ven_counts = contador.get("ven_counts", {})

    enviados    = 0
    ya_enviados = set()
    for vendedor, v in sorted(por_vendedor.items()):
        webhook, ciclos_pers = _buscar_webhook_vendedor(vendedor)
        if not webhook or webhook in ya_enviados:
            continue

        total = len(v["en_tiempo"]) + len(v["vencidas"]) + len(v["de_ayer"])
        if total == 0:
            continue

        # Frecuencia personal (columna D de la hoja) o default global
        ciclos_req = ciclos_pers if ciclos_pers > 0 else M.CICLOS_VENDEDORES
        clave      = vendedor.strip().upper()
        ven_counts[clave] = ven_counts.get(clave, 0) + 1
        if ven_counts[clave] < ciclos_req:
            log.info(f"Vendedor {vendedor}: {ven_counts[clave]}/{ciclos_req} ciclos — aún no toca")
            continue
        ven_counts[clave] = 0
        ya_enviados.add(webhook)

        texto = _construir_texto_vendedor(vendedor, v, fecha_now)
        post_chat_con_reintento(webhook, {"text": texto})
        enviados += 1

    # Persistir contadores individuales
    contador["ven_counts"] = ven_counts
    M.guardar_contador(contador)
    log.info(f"Mensajes individuales a vendedores enviados: {enviados}")


def enviar_cierre(datos, dir_dict, hist_dict=None, descansos=None):
    fecha_now  = datetime.now().strftime("%d/%m/%Y %H:%M")
    ESTATUS    = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    espera = 0; etiq = 0
    for row in datos:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status == "Mercancia en Espera de Entrega": espera += 1
        elif status == "Etiqueta Generada":            etiq   += 1

    total = espera + etiq
    emoji_cierre = random.choice(["🌙", "🌛", "😴", "🏁", "🌜"])
    msg_cierre   = random.choice(M.MENSAJES_CIERRE).format(total=total)

    lineas = [
        emoji_cierre + " *Buenas noches*", "",
        msg_cierre, "",
        "📊 *Resumen de cierre:*",
        "🔴 Mercancia en Espera: *" + str(espera) + "*",
        "🏷️ Etiquetas Generadas: *" + str(etiq) + "*", "",
        "_Argos — " + fecha_now + "_",
    ]
    post_chat_con_reintento(M.WEBHOOK, {"text": "\n".join(lineas)})

    # ── Card jefes → misma que Resumen General + intro de buenas noches ──
    por_piso_c = _construir_por_piso(datos, dir_dict, hist_dict or {}, descansos or {})
    if por_piso_c:
        intro = f"{emoji_cierre} *Buenas noches*\n{msg_cierre}"
        card_cierre = construir_card_resumen_general(por_piso_c, dir_dict, fecha_now, intro_texto=intro)
    else:
        card_cierre = construir_card_cierre(emoji_cierre, msg_cierre, espera, etiq, {}, fecha_now)
    post_chat_con_reintento(M.WEBHOOK_JEFES, card_cierre)
    log.info("Mensaje de cierre enviado al espacio jefes ✅")

    # ── KPI tiempos → espacio Tiempos ─────────────────────────────────
    try:
        log.info("Enviando KPI tiempos al espacio Tiempos en cierre...")
        M.enviar_kpi_jefes_tiempos(dir_dict=dir_dict)
    except Exception as e:
        log.error(f"Error enviando KPI en cierre: {e}")

    M.marcar_cierre_enviado()


def enviar_reporte_salud(resumen, vencidas):
    """Resumen operativo del bot al cierre → espacio Reporte.
    Comunica el valor diario: ciclos de monitoreo, remisiones procesadas e
    incidencias. Pensado para que el negocio vea cuánto trabajó Argos.
    """
    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    c = M.leer_contador()
    ciclos  = c.get("ciclos_dia", 0)
    errores = c.get("errores_dia", 0)

    try:
        horario = f"{int(M.HORA_INICIO):02d}:00 – {int(M.HORA_FIN):02d}:{int(M.MINUTO_FIN):02d}"
    except Exception:
        horario = ""

    estado = "✅ Sin incidencias" if errores == 0 else f"⚠️ {errores} incidencia(s) — recuperado automáticamente"

    lineas = [
        "🤖 *Argos — Reporte del día*",
        "📅 " + fecha_now,
        "",
        "🔁 Ciclos de monitoreo hoy: *" + str(ciclos) + "*",
        "📦 Remisiones en el último corte: *" + str(resumen.get("total", 0)) + "*",
        "🏷️ Etiquetas generadas: *" + str(resumen.get("etiquetas", 0)) + "*",
        "🔴 Vencidas al cierre: *" + str(len(vencidas)) + "*",
        "⚙️ Estado del sistema: " + estado,
    ]
    if horario:
        lineas.append("🕐 Operación: " + horario)
    lineas += ["", "_Argos v" + M.VERSION + " — monitoreo automático_"]

    post_chat_con_reintento(M.WEBHOOK, {"text": "\n".join(lineas)})
    log.info("Reporte de salud diario enviado al espacio reporte ✅")


def enviar_pendientes_ayer(datos, dir_dict, descansos=None):
    """
    Manda al espacio JEFES un recordatorio de remisiones de ayer aún sin atender.
    Se llama cada 30 min DESPUÉS del cierre hasta que no queden pendientes.
    Retorna True si había pendientes, False si ya están todos resueltos.
    """
    if descansos is None:
        descansos = {}

    ESTATUS       = ["Mercancia en Espera de Entrega", "Etiqueta Generada"]

    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_jefe  = {}

    for row in datos:
        if not row:
            continue
        status = str(row.get("StatusRemision", "")).strip()
        if status not in ESTATUS:
            continue
        fecha_str = str(row.get("FechaAsignacionTienda", "")).strip()
        if not es_de_ayer(fecha_str):
            continue

        sec       = str(row.get("Seccion", "")).strip().replace(".0","")
        try: sec = str(int(sec))
        except: pass
        nom_jefe  = str(row.get("NombreJefeDePiso", "")).strip()
        nom_ven   = str(row.get("NombreVendedor", "")).strip()

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

    card_ayer = construir_card_pendientes_ayer(por_jefe, total_ayer, fecha_now)
    post_chat_con_reintento(M.WEBHOOK_JEFES, card_ayer)
    log.info(f"Pendientes de ayer enviados al espacio jefes: {total_ayer} remisiones")
    return True


def enviar_comparativa_semanal(gc):
    """Viernes al cierre manda resumen de la semana."""
    try:
        ss   = gc.open_by_key(GOOGLE["sheet_id"])
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

        lineas.append("\n_Argos — " + fecha_now + "_")

        post_chat_con_reintento(M.WEBHOOK, {"text": "\n".join(lineas)})
        post_chat_con_reintento(M.WEBHOOK_JEFES, {"text": "\n".join(lineas)})
        log.info("Comparativa semanal enviada ✅")
    except Exception as e:
        log.warning(f"Error comparativa semanal: {e}")


def es_alerta_anomalia(vencidas_actual, gc):
    """Detecta si hay mucho mas vencidas que el promedio del dia."""
    try:
        ss   = gc.open_by_key(GOOGLE["sheet_id"])
        hoja = ss.worksheet("MONITOR")
        rows = hoja.get_all_values()
        fecha_hoy = datetime.now().strftime("%d/%m/%Y")
        venc_hoy  = [int(r[4]) for r in rows[1:] if r and r[0] == fecha_hoy and r[4].isdigit()]
        if len(venc_hoy) < 3:
            return False, 0
        promedio = sum(venc_hoy) / len(venc_hoy)
        # Alerta si supera el promedio del día por el factor configurable
        # (umbral_anomalia en CONFIG; default 1.5 = 50% más). Antes estaba
        # hardcodeado a 1.5 e ignoraba la config.
        factor = M.UMBRAL_ANOMALIA if 1.0 <= M.UMBRAL_ANOMALIA <= 5.0 else 1.5
        if vencidas_actual > promedio * factor and vencidas_actual - promedio >= 10:
            return True, promedio
        return False, promedio
    except Exception:
        return False, 0


def mandar_alerta_anomalia(vencidas, promedio):
    """Manda alerta urgente al espacio reporte."""
    # Evitar spam — no mandar si ya se mando hace menos de 30 min
    try:
        if os.path.exists(M.ALERTA_FILE):
            with open(M.ALERTA_FILE, "r") as f:
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
    post_chat_con_reintento(M.WEBHOOK_JEFES, {"text": "\n".join(lineas)})
    try:
        with open(M.ALERTA_FILE, "w") as f:
            json.dump({"ultima": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass
    log.info("⚠️ Alerta de anomalia enviada al espacio jefes")


def enviar_ranking_jefes(gc):
    """Al cierre manda ranking de jefes segun tiempo promedio del dia."""
    try:
        ss   = gc.open_by_key(GOOGLE["sheet_id"])
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

        lineas.append("_Argos — " + fecha_now + "_")

        post_chat_con_reintento(M.WEBHOOK_TIEMPOS, {"text": "\n".join(lineas)})
        log.info("Ranking de jefes enviado ✅")
    except Exception as e:
        log.warning(f"Error enviando ranking: {e}")


def enviar_recordatorio_cierre(datos, dir_dict, hist_dict=None, descansos=None):
    """8:00 PM — aviso a jefes: cierre en 1 hora. Usa la misma card que el Resumen General."""
    fecha_now = datetime.now().strftime("%d/%m/%Y %H:%M")
    por_piso_r = _construir_por_piso(datos, dir_dict, hist_dict or {}, descansos or {})
    if not por_piso_r:
        M.marcar_recordatorio_enviado()
        return

    intro = "⏰ *Faltan 60 minutos para el cierre*\nRevisa tus pendientes antes de las 9:00 PM."
    card = construir_card_resumen_general(por_piso_r, dir_dict, fecha_now, intro_texto=intro)
    post_chat_con_reintento(M.WEBHOOK_JEFES, card)
    M.marcar_recordatorio_enviado()
    log.info("Recordatorio de cierre enviado ✅")


def procesar_mensajes_programados(gc):
    """Envía mensajes programados usando contador de ciclos (igual que contador_jefes)."""
    try:
        ss = gc.open_by_key(GOOGLE["sheet_id"])
        try:
            hoja = ss.worksheet("MENSAJES_PROGRAMADOS")
        except gspread.WorksheetNotFound:
            hoja = ss.add_worksheet("MENSAJES_PROGRAMADOS", rows=200, cols=7)
            hoja.update([["ID", "Texto", "Intervalo_ciclos", "Destino", "Activo", "Ultimo_envio", "Creado"]], "A1")
            log.info("Hoja MENSAJES_PROGRAMADOS creada ✅")
            return

        rows = hoja.get_all_values()
        if len(rows) <= 1:
            return

        ahora = datetime.now()
        WEBHOOKS_DESTINO = {
            "reporte": M.WEBHOOK,
            "jefes":   M.WEBHOOK_JEFES,
            "tiempos": M.WEBHOOK_TIEMPOS,
        }

        contadores = M.leer_contador_msgs()

        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 4:
                continue
            raw_id  = (row[0] if len(row) > 0 else "").strip()
            try:
                msg_id = str(int(float(raw_id))) if raw_id else ""
            except (ValueError, TypeError):
                msg_id = raw_id
            texto   = (row[1] if len(row) > 1 else "").strip()
            try:
                intervalo_ciclos = int(float(row[2])) if len(row) > 2 and row[2] else 0
            except (ValueError, TypeError):
                intervalo_ciclos = 0
            destino = (row[3] if len(row) > 3 else "reporte").strip()
            activo  = (row[4] if len(row) > 4 else "si").strip().lower()

            if activo not in ("si", "yes", "true", "1"):
                continue
            if not texto or intervalo_ciclos <= 0 or not msg_id:
                continue

            contadores[msg_id] = contadores.get(msg_id, 0) + 1
            ciclo_actual = contadores[msg_id]

            if ciclo_actual >= intervalo_ciclos:
                enviado = False
                for dest in [d.strip() for d in destino.split(",")]:
                    url = WEBHOOKS_DESTINO.get(dest)
                    if url and post_chat_con_reintento(url, {"text": texto}):
                        enviado = True
                if enviado:
                    contadores[msg_id] = 0
                    try:
                        hoja.update([[ahora.strftime("%Y-%m-%d %H:%M:%S")]], f"F{i}")
                    except Exception as upd_e:
                        log.warning(f"Error actualizando Ultimo_envio fila {i}: {upd_e}")
                    log.info(f"Mensaje programado enviado (fila {i}): destino={destino}, cada {intervalo_ciclos} ciclo(s)")
            else:
                log.info(f"Msg programado fila {i}: {ciclo_actual}/{intervalo_ciclos} ciclos")

        M.guardar_contador_msgs(contadores)

    except Exception as e:
        log.warning(f"Error procesando mensajes programados: {e}")
