#!/usr/bin/env python3
"""Prueba de integración de la modularización.

Verifica que main.py y todos los submódulos (utils, descarga, kpi, sheets,
mensajes) se importen juntos sin error y que TODAS las referencias cruzadas
`M.x` (de un submódulo a main) resuelvan a un atributo real de main. Además
ejerce el pipeline de construcción de cards con datos falsos para cazar
NameErrors en las rutas calientes de mensajes.

No requiere Playwright/gspread/credenciales: los stubea.
Uso:  python test_modulos.py
"""
import sys, types, os, ast


def _stub(n, **a):
    m = types.ModuleType(n)
    for k, v in a.items():
        setattr(m, k, v)
    sys.modules[n] = m


_stub("playwright")
_stub("playwright.sync_api", sync_playwright=lambda *a, **k: None)
_stub("gspread", WorksheetNotFound=type("W", (Exception,), {}), authorize=lambda *a, **k: None)
_stub("google")
_stub("google.oauth2")
_stub("google.oauth2.service_account",
      Credentials=type("C", (), {"from_service_account_file": staticmethod(lambda *a, **k: None)}))
_stub("config",
      LIVERPOOL={"url_login": "", "usuario": "", "password": ""},
      GOOGLE={"sheet_id": "x", "credentials": "x", "nombre_hoja": "H"},
      CHAT={"webhook_url": ""}, XD={"url_login": "", "usuario": "", "password": ""},
      CARPETA_DESCARGA="/tmp/argos_test", PC_NOMBRE="t", GITHUB_TOKEN="")
os.makedirs("/tmp/argos_test", exist_ok=True)

import main  # noqa: E402

fallos = []

# 1) Todas las M.x de cada submódulo resuelven a un atributo de main
for mod in ["descarga.py", "kpi.py", "sheets.py", "mensajes.py"]:
    refs = set()
    for node in ast.walk(ast.parse(open(mod, encoding="utf-8").read())):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "M":
            refs.add(node.attr)
    faltan = [x for x in refs if not hasattr(main, x)]
    if faltan:
        fallos.append(f"{mod}: referencias M.x sin resolver: {faltan}")
    else:
        print(f"  OK   {mod}: {len(refs)} referencias M.x resuelven")

# 2) Pipeline de cards con datos falsos (formato dict, como los manda la API)
def _fila(sec, status, jefe="", ven="", fecha="2026-01-01 10:00:00",
          tipo_entrega="HD0D - Mismo dia", tipo_orden="XD"):
    return {
        "Remision": "1234567890", "Sku": "111", "DescripcionSku": "Producto test",
        "Seccion": sec, "NombreJefeDePiso": jefe, "NombreVendedor": ven,
        "FechaAsignacionTienda": fecha, "StatusRemision": status,
        "TipoEntrega": tipo_entrega, "TipoOrden": tipo_orden,
    }

datos = [_fila("100", "Mercancia en Espera de Entrega", "PEDRO JEFE", "ANA VEN"),
         _fila("200", "Etiqueta Generada", "", "")]
dir_dict = {"100": {"ubicacion": "1 Piso", "jefe": "PEDRO JEFE", "nombre_seccion": "Ropa"},
            "200": {"ubicacion": "2 Piso", "jefe": "", "nombre_seccion": "Zapatos"}}

prioridades_test = [{"campo": "jefe", "valor": "PEDRO", "activo": True}]

try:
    pp = main._construir_por_piso(datos, dir_dict, {}, {}, prioridades=prioridades_test)
    assert set(pp.keys()) == {1, 2}, pp.keys()
    card = main.construir_card_resumen_general(pp, dir_dict, "07/07/2026 15:00")
    assert "cardsV2" in card
    for info_piso in pp.values():
        for jefe, info_j in info_piso["jefes"].items():
            assert "cardsV2" in main.construir_card_jefe_individual(jefe, info_j, info_piso["ubicacion"], "hoy")
    assert "cardsV2" in main.construir_card_pendientes_ayer(
        {"PEDRO": {"Seccion 100": {"count": 3, "max_min": 50, "sin_vendedor": 1}}}, 3, "hoy")
    assert main._url_chart_pisos([("1er PISO", 5, 3, 1)]).startswith("https://quickchart.io")
    card_prio = main.construir_card_prioridades_grupo(pp, dir_dict, "07/07/2026 15:00")
    assert card_prio is not None and "cardsV2" in card_prio, "card de prioridades deberia existir (hay jefe con tipo_counts)"
    print("  OK   pipeline de cards se construye sin error")
except Exception as e:
    fallos.append(f"pipeline de cards: {e!r}")

print()
if fallos:
    for f in fallos:
        print("FALLA:", f)
    sys.exit(1)
print("Integración de módulos OK ✅")
sys.exit(0)
