# Plantilla de configuracion — COPIAR a config.py en cada PC y rellenar.
# config.py real NUNCA se sube al repo (contiene credenciales y el token).

LIVERPOOL = {
    "url_login": "https://surtidoapp-oms.liverpool.com.mx/#/login",
    "usuario":   "TU_USUARIO_OMS",
    "password":  "TU_PASSWORD_OMS",
}

GOOGLE = {
    "sheet_id":       "TU_SHEET_ID",
    "nombre_hoja":    "Hoja 1",
    "credentials":    r"C:\argos\credentials.json",
    "sheet2_id":      "TU_SHEET2_ID",
    "sheet2_hoja":    "Hoja 1",
    "sheet2_col":     "V",
    "sheet2_fila":    2,
    "timestamp_col":  "AS",
    "timestamp_fila": 2,
}

CHAT = {
    "webhook_url":    "",   # los webhooks reales viven en la hoja CONFIG del Sheet
    "looker_url":     "",
    "nombre_reporte": "Indicadores Liverpool 456",
}

XD = {
    "url_login":    "https://front-oms-xd.liverpool.com.mx/#/login",
    "usuario":      "TU_USUARIO_XD",
    "password":     "TU_PASSWORD_XD",
}

CARPETA_DESCARGA = r"C:\argos\descargas"

PC_NOMBRE = "NombreDeEstaPC"

# Token para descargar actualizaciones si el repo es privado (lo pone el
# instalador en cada PC). Dejar vacio si el repo es publico.
GITHUB_TOKEN = ""
