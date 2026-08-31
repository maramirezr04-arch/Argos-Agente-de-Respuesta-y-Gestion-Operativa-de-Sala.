# -*- coding: utf-8 -*-
"""reparador.py — Watchdog de Argos.

Lo lanza la tarea programada "Argos Reparador". A diferencia de lanzar main.py
a ciegas (lo que duplicaba TODO), este script primero revisa si el bot corrió
hace poco leyendo health.json:

  • Si el bot está sano (health fresco) → NO hace nada. Esto es lo que evita
    las ejecuciones duplicadas.
  • Si el bot NO se ejecutó dentro de la ventana esperada → avisa al chat de
    reporte y relanza main.py para que se repare/continúe.

Usa el cache local (config_remota_cache.json) para el webhook y el horario, así
que NO consume cuota de Google Sheets. Configurable desde la hoja CONFIG:
  watchdog_activo (si/no) · watchdog_minutos (umbral) · destino_watchdog (canal)
"""
import os, sys, json, logging, subprocess
from datetime import datetime
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

HEALTH_FILE   = "health.json"
CONFIG_CACHE  = "config_remota_cache.json"
ESTADO_FILE   = "reparador_estado.json"
LOG_DIR       = "logs"

DEFAULT_WATCHDOG_MIN = 30
COOLDOWN_AVISO_MIN   = 45   # no repetir el aviso al chat antes de N minutos
DIAS_MAP = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  [REPARADOR] %(message)s",
    handlers=[logging.FileHandler(f"{LOG_DIR}/{datetime.now():%Y-%m-%d}.log", encoding="utf-8")],
)
log = logging.getLogger("reparador")


def _si(v):
    return str(v).strip().lower() in ("si", "sí", "true", "1", "yes")


def _cfg():
    try:
        with open(CONFIG_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _dentro_horario(cfg):
    """Ventana en la que el bot DEBE estar corriendo (misma logica que main)."""
    ahora = datetime.now()
    def _int(k, d):
        try: return int(cfg.get(k, d))
        except Exception: return d
    hi, hf, mf = _int("hora_inicio", 10), _int("hora_fin", 21), _int("minuto_fin", 30)
    if ahora.hour < hi: return False
    if ahora.hour > hf: return False
    if ahora.hour == hf and ahora.minute >= mf + 15: return False
    dias = cfg.get("dias_activos", "lun,mar,mie,jue,vie,sab,dom")
    activos = {DIAS_MAP[d.strip()] for d in dias.split(",") if d.strip() in DIAS_MAP}
    if activos and ahora.weekday() not in activos: return False
    return True


def _health_edad_min():
    """Minutos desde la ultima actualizacion de health.json. None si no existe."""
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            h = json.load(f)
        ts = datetime.fromisoformat(h["ultima_actualizacion"])
        return (datetime.now() - ts).total_seconds() / 60.0
    except Exception:
        return None


def _webhook_destino(cfg):
    destino = (cfg.get("destino_watchdog") or "reporte").strip().lower()
    if destino.startswith("http"):
        return destino
    return {
        "jefes":   cfg.get("webhook_jefes", ""),
        "tiempos": cfg.get("webhook_tiempos", ""),
    }.get(destino, cfg.get("webhook_reporte", "")).strip()


def _cooldown_ok():
    try:
        with open(ESTADO_FILE, encoding="utf-8") as f:
            ultimo = datetime.fromisoformat(json.load(f).get("ultimo_aviso", "2000-01-01"))
        return (datetime.now() - ultimo).total_seconds() / 60.0 >= COOLDOWN_AVISO_MIN
    except Exception:
        return True


def _marca_aviso():
    try:
        with open(ESTADO_FILE, "w", encoding="utf-8") as f:
            json.dump({"ultimo_aviso": datetime.now().isoformat()}, f)
    except Exception:
        pass


def _post(url, texto):
    try:
        import requests
        r = requests.post(url, json={"text": texto}, timeout=10)
        return 200 <= r.status_code < 300
    except Exception as e:
        log.warning(f"No se pudo avisar al chat: {e}")
        return False


def _relanzar():
    try:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        subprocess.Popen([exe, "main.py"], cwd=os.getcwd())
        return True
    except Exception as e:
        log.error(f"No se pudo relanzar main.py: {e}")
        return False


def main():
    cfg = _cfg()
    if not _si(cfg.get("watchdog_activo", "si")):
        return  # watchdog desactivado desde CONFIG
    if _si(cfg.get("pausado", "no")):
        return  # bot pausado a proposito — no relanzar
    if not _dentro_horario(cfg):
        return  # fuera de horario: el bot debe estar quieto, no alertar

    # Gracia al inicio del día: a las hora_inicio el health de anoche está viejo;
    # darle unos minutos al bot para su primera corrida y evitar falsa alarma.
    ahora = datetime.now()
    try: hora_ini = int(cfg.get("hora_inicio", 10))
    except Exception: hora_ini = 10
    if ahora.hour == hora_ini and ahora.minute < 12:
        return

    try: umbral = int(cfg.get("watchdog_minutos", DEFAULT_WATCHDOG_MIN))
    except Exception: umbral = DEFAULT_WATCHDOG_MIN

    edad = _health_edad_min()
    if edad is not None and edad < umbral:
        return  # BOT SANO: no hacer nada (clave para no duplicar ejecuciones)

    # ── Bot caido: relanzar y avisar ──────────────────────────────
    detalle = ("no hay registro de ejecucion (health.json ausente)"
               if edad is None else
               f"sin ejecutarse hace {int(edad)} min (umbral {umbral} min)")
    log.warning(f"Bot caido: {detalle} — relanzando main.py")

    relanzado = _relanzar()

    pc = ""
    try:
        from config import PC_NOMBRE
        pc = PC_NOMBRE
    except Exception:
        pass

    url = _webhook_destino(cfg)
    if url and _cooldown_ok():
        msg = (f"🛠️ *Argos Reparador* — {pc}\n"
               f"El bot {detalle}.\n"
               f"{'Relanzado ✅' if relanzado else 'No se pudo relanzar ❌'} "
               f"— {datetime.now():%d/%m/%Y %H:%M}\n"
               f"_Si persiste, revisar logs y corregir por GitHub._")
        if _post(url, msg):
            _marca_aviso()
            log.info("Aviso de watchdog enviado al chat")


if __name__ == "__main__":
    main()
