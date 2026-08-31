"""utils.py — Funciones puras del bot Argos.

Contiene helpers sin efectos secundarios (no tocan red, Sheets, ni el estado
global del bot): parseo de fechas, agrupación por piso, formato de tiempos,
conversión de valores del CSV, etc. Al no depender de nada del runtime, se
pueden probar en aislamiento con test_basico.py.

Este módulo lo importa main.py. El bot lo descarga y mantiene en cada PC vía el
mecanismo de auto-actualización (bootstrap + _MODULOS en main.py).
"""

import time
from datetime import datetime, timedelta
from functools import lru_cache

# ── Constantes propias de estas funciones ────────────────────
HISTORIAL_MAX = 10
MARGEN        = 1.3

TIPOS_PRIORIDAD = [
    "HD0D - Mismo dia",
    "HD1D - Manana",
    "CC0D - Mismo dia",
    "CC1D - Manana",
    "C&C Misma Tienda",
]

JEFES_MUJERES = [
    "ALMA DELIA", "BRENDA", "DENISSE", "GEOLIBETH",
    "JOANA", "LIZBETH", "MARIA DE LOS ANGELES",
    "NUBIA BERENICE", "ROSALBA"
]


# ── Fechas ────────────────────────────────────────────────────
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


def get_fechas():
    hoy  = datetime.now()
    ayer = hoy - timedelta(days=1)
    return ayer.strftime("%d/%m/%Y"), hoy.strftime("%d/%m/%Y")


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


# ── Formato de tiempos ────────────────────────────────────────
def calcular_tiempo_espera_str(minutos):
    minutos = int(minutos or 0)
    if minutos < 60:
        return str(minutos) + " min"
    h = minutos // 60
    m = minutos % 60
    return str(h) + "h" + (" " + str(m) + "min" if m else "")


def seg_a_str(seg):
    seg = int(seg or 0)
    if seg < 60:
        return str(seg) + " seg"
    m = seg // 60
    s = seg % 60
    return str(m) + " min" + (" " + str(s) + " seg" if s else "")


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


def medir(inicio):
    return round(time.time() - inicio, 2)


# ── Valores del CSV ───────────────────────────────────────────
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


# ── Agrupación por piso ───────────────────────────────────────
def orden_piso(ubicacion):
    """Detecta el piso basado en palabras clave especificas en orden estricto.
    Acepta variantes: '1 PISO', 'PISO 1', '1ER PISO', 'PRIMER PISO', 'P1', '1'."""
    ub = str(ubicacion).upper().strip()
    # Quitar caracteres especiales para mejor matching
    ub_limpio = ub.replace("°", "").replace("º", "").replace("°", "").strip()

    # Planta baja
    if "PLANTA BAJA" in ub_limpio or ub_limpio in ("PB", "P0", "0", "PLANTA"):
        return 0
    # Tercer piso (más específico primero)
    if ("3ER" in ub_limpio or "3RO" in ub_limpio or "TERCER" in ub_limpio
            or "3 PISO" in ub_limpio or "PISO 3" in ub_limpio
            or ub_limpio in ("3", "P3")):
        return 3
    # Segundo piso
    if ("2DO" in ub_limpio or "SEGUNDO" in ub_limpio
            or "2 PISO" in ub_limpio or "PISO 2" in ub_limpio
            or ub_limpio in ("2", "P2")):
        return 2
    # Primer piso
    if ("1ER" in ub_limpio or "1RO" in ub_limpio or "PRIMER" in ub_limpio
            or "1 PISO" in ub_limpio or "PISO 1" in ub_limpio
            or ub_limpio in ("1", "P1")):
        return 1
    return 99


# ── Jefes ─────────────────────────────────────────────────────
def get_genero(nom_jefe):
    nom = nom_jefe.upper()
    for mujer in JEFES_MUJERES:
        if mujer in nom:
            return "jefa"
    return "jefe"


def get_mencion(nom_jefe):
    genero = get_genero(nom_jefe)
    emoji  = ("\U0001f469‍\U0001f4bc" if genero == "jefa"
              else "\U0001f468‍\U0001f4bc")
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


# ── Tiempos adaptativos ───────────────────────────────────────
def calcular_espera(historial):
    if not historial:
        return 5.0
    ultimos  = historial[-HISTORIAL_MAX:]
    promedio = sum(ultimos) / len(ultimos)
    return max(3.0, min(60.0, promedio * MARGEN))


def actualizar_historial(historial, nuevo):
    historial.append(round(nuevo, 2))
    return historial[-HISTORIAL_MAX:]
