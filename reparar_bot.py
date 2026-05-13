#!/usr/bin/env python3
"""Bot reparador: detecta si el bot principal dejó de correr y manda diagnóstico."""

import sys
import os
import json
import time
import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

REPARAR_FILE = os.path.join(config.BASE_DIR, "reparar_estado.json")
HEALTH_FILE  = os.path.join(config.BASE_DIR, "health.json")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_webhook(url, text):
    try:
        r = requests.post(url, json={"text": text}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error enviando webhook: {e}")
        return False


def leer_ultimas_lineas_log(n=200):
    hoy = datetime.date.today().strftime("%Y-%m-%d")
    log_file = os.path.join(config.LOGS_DIR, f"{hoy}.log")
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, encoding="utf-8") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def detectar_errores_en_log(lineas):
    errores  = []
    warnings = []
    for linea in lineas:
        if any(k in linea for k in ("[ERROR]", "Traceback", "Exception:", "Error:")):
            errores.append(linea.strip())
        elif "[WARNING]" in linea:
            warnings.append(linea.strip())
    return errores[-10:], warnings[-5:]


def evaluar_salud():
    """Retorna (esta_bien, razon)."""
    health = load_json(HEALTH_FILE, {})

    if not health:
        return False, "No existe health.json — el bot nunca ha corrido"

    ts_str = health.get("ts")
    if not ts_str:
        return False, "health.json no tiene timestamp"

    try:
        ts = datetime.datetime.fromisoformat(ts_str)
    except Exception:
        return False, f"Timestamp inválido: {ts_str}"

    now = datetime.datetime.now()
    mins_ago = (now - ts).total_seconds() / 60

    status = health.get("status", "unknown")
    estados_normales = {"ok", "fuera_horario", "pausado_manual", "pausado_config"}

    # Si está fuera de horario es normal que no haya corrido recientemente
    hora = now.hour
    en_horario_ahora = 10 <= hora < 22
    umbral_mins = 30 if en_horario_ahora else 900  # 15h si es de noche

    if mins_ago > umbral_mins and en_horario_ahora:
        return False, f"Última ejecución hace {mins_ago:.0f} min (umbral {umbral_mins} min)"

    if status not in estados_normales:
        return False, f"Status anormal: {status}"

    return True, f"OK — {status} hace {mins_ago:.0f} min"


def reparar():
    estado = load_json(REPARAR_FILE, {})
    ultimo_diag = estado.get("ultimo_diagnostico", 0)

    # Anti-spam: mínimo 30 min entre diagnósticos
    if time.time() - ultimo_diag < 1800:
        return

    bien, razon = evaluar_salud()

    if bien:
        if estado.get("en_error"):
            # Bot se recuperó — notificar
            send_webhook(config.WEBHOOK_REPORTE, f"✅ *Bot reparador: bot recuperado*\n{razon}")
            save_json(REPARAR_FILE, {"en_error": False, "ultimo_diagnostico": 0})
        return

    # Hay problema — recopilar info y mandar diagnóstico
    print(f"[Reparador] Problema: {razon}")
    lineas = leer_ultimas_lineas_log()
    errores, warnings = detectar_errores_en_log(lineas)

    lines = [
        "⚠️ *Bot Reparador — Diagnóstico automático*",
        f"🕐 Hora: {datetime.datetime.now().strftime('%H:%M')}",
        f"❌ Problema: {razon}",
    ]

    if errores:
        lines.append(f"\n*Últimos errores en el log ({len(errores)}):*")
        for e in errores[-5:]:
            lines.append(f"```{e[:250]}```")

    if warnings:
        lines.append(f"\n⚠️ Warnings recientes: {len(warnings)}")

    lines.append(
        "\n*Acciones sugeridas:*\n"
        "• Abre CMD en `C:\\liverpool-automation`\n"
        "• Ejecuta: `python main.py test` para verificar conexiones\n"
        "• Ejecuta: `python main.py --forzar` para correr manualmente\n"
        "• Revisa el log en `logs/` del día de hoy\n"
        "• Si hay error de Playwright: `python -m playwright install chromium`\n"
        "• Si hay error de credenciales: verifica `credentials.json`"
    )

    send_webhook(config.WEBHOOK_REPORTE, "\n".join(lines))
    save_json(REPARAR_FILE, {
        "en_error": True,
        "ultimo_diagnostico": time.time(),
        "razon": razon,
        "ts": datetime.datetime.now().isoformat(),
    })


if __name__ == "__main__":
    reparar()
