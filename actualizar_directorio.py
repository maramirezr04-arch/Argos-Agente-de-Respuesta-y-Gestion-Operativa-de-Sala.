#!/usr/bin/env python3
"""Mantiene la hoja DIRECTORIO actualizada con los jefes del CSV más reciente."""

import sys
import os
import csv
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def setup_logging():
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    hoy = datetime.date.today().strftime("%Y-%m-%d")
    log_file = os.path.join(config.LOGS_DIR, f"{hoy}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [DIRECTORIO] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("directorio")


def get_gc():
    creds = Credentials.from_service_account_file(config.CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def normalizar_piso(planta):
    p = str(planta).upper().strip()
    if "BAJA" in p or p == "PB":
        return "PLANTA BAJA"
    if "3" in p:
        return "3er PISO"
    if "2" in p:
        return "2° PISO"
    if "1" in p:
        return "1er PISO"
    return planta


def leer_jefes_del_csv():
    if not os.path.isdir(config.DESCARGAS_DIR):
        return {}

    csvs = sorted(
        [f for f in os.listdir(config.DESCARGAS_DIR) if f.endswith(".csv")],
        reverse=True,
    )
    if not csvs:
        return {}

    path = os.path.join(config.DESCARGAS_DIR, csvs[0])
    jefes: dict = {}

    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) < 20:
                    continue
                planta       = row[0].strip()
                seccion      = row[5].strip()
                jefe_id      = row[16].strip() if len(row) > 16 else ""
                jefe_nombre  = row[17].strip().upper() if len(row) > 17 else ""

                if not jefe_nombre:
                    continue

                piso = normalizar_piso(planta)

                if jefe_nombre not in jefes:
                    jefes[jefe_nombre] = {"piso": piso, "secciones": set(), "id": jefe_id}
                jefes[jefe_nombre]["secciones"].add(seccion)

    except Exception as e:
        print(f"Error leyendo CSV: {e}")
        return {}

    return jefes


def actualizar_directorio():
    log = setup_logging()
    log.info("Actualizando DIRECTORIO...")

    jefes = leer_jefes_del_csv()
    if not jefes:
        log.warning("No se encontraron jefes en el CSV. Verifica que exista un CSV en descargas/")
        return False

    try:
        gc = get_gc()
        sh = gc.open_by_key(config.SHEET_PRINCIPAL_ID)
        ws = sh.worksheet("DIRECTORIO")

        hoy = datetime.date.today().strftime("%d/%m/%Y")
        encabezados = ["Jefe", "Piso", "Secciones", "ID", "Última actualización"]
        filas = [encabezados]

        for nombre, data in sorted(jefes.items()):
            secciones_str = ", ".join(sorted(data["secciones"]))
            filas.append([nombre, data["piso"], secciones_str, data["id"], hoy])

        ws.clear()
        ws.update("A1", filas, value_input_option="RAW")
        log.info(f"DIRECTORIO actualizado: {len(jefes)} jefes")
        return True

    except Exception as e:
        log.error(f"Error actualizando DIRECTORIO: {e}")
        return False


if __name__ == "__main__":
    ok = actualizar_directorio()
    sys.exit(0 if ok else 1)
