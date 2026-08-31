#!/usr/bin/env python3
"""Pruebas básicas de las funciones puras de utils.py.

No requiere Playwright, gspread ni credenciales: utils.py solo usa la
librería estándar, así que se importa directo y se prueba la lógica pura
(agrupación por piso, formato de tiempos, conversión de valores, etc.).

Uso:  python test_basico.py
Sale con código 0 si todo pasa, 1 si algo falla.
"""

import sys
import utils

_fallos = []

def check(nombre, obtenido, esperado):
    if obtenido == esperado:
        print(f"  OK   {nombre}")
    else:
        print(f"  FALLA {nombre}: esperaba {esperado!r}, obtuve {obtenido!r}")
        _fallos.append(nombre)

# ── orden_piso (raíz del bug "SIN PISO") ──────────────────────
print("orden_piso:")
check("planta baja",        utils.orden_piso("Planta Baja"), 0)
check("PB exacto",          utils.orden_piso("PB"), 0)
check("3er piso",           utils.orden_piso("3er Piso"), 3)
check("tercer piso",        utils.orden_piso("Tercer Piso"), 3)
check("2do piso",           utils.orden_piso("2do Piso"), 2)
check("segundo piso",       utils.orden_piso("SEGUNDO PISO"), 2)
check("1er piso",           utils.orden_piso("1er Piso"), 1)
check("primer piso",        utils.orden_piso("Primer Piso"), 1)
check("PISO 1 variante",    utils.orden_piso("PISO 1"), 1)
check("P1 variante",        utils.orden_piso("P1"), 1)
check("solo digito 2",      utils.orden_piso("2"), 2)
check("vacio -> sin piso",  utils.orden_piso(""), 99)
check("desconocido",        utils.orden_piso("Bodega Central"), 99)

# ── calcular_tiempo_espera_str ────────────────────────────────
print("calcular_tiempo_espera_str:")
check("0 min",   utils.calcular_tiempo_espera_str(0),   "0 min")
check("30 min",  utils.calcular_tiempo_espera_str(30),  "30 min")
check("59 min",  utils.calcular_tiempo_espera_str(59),  "59 min")
check("60 -> 1h", utils.calcular_tiempo_espera_str(60), "1h")
check("90 -> 1h 30min", utils.calcular_tiempo_espera_str(90), "1h 30min")
check("125 -> 2h 5min", utils.calcular_tiempo_espera_str(125), "2h 5min")

# ── convertir_valor ───────────────────────────────────────────
print("convertir_valor:")
check("vacio",        utils.convertir_valor(""), "")
check("None",         utils.convertir_valor(None), "")
check("entero",       utils.convertir_valor("123"), 123)
check("decimal",      utils.convertir_valor("1.5"), 1.5)
check("texto",        utils.convertir_valor("abc"), "abc")
check("comilla inicial", utils.convertir_valor("'123"), 123)
check("fecha intacta", utils.convertir_valor("2026-06-21 10:00:00"), "2026-06-21 10:00:00")

# ── calcular_espera (clamp 3..60) ─────────────────────────────
print("calcular_espera:")
check("vacio -> 5.0",  utils.calcular_espera([]), 5.0)
check("clamp minimo",  utils.calcular_espera([0.1, 0.1]), 3.0)
check("clamp maximo",  utils.calcular_espera([1000, 1000]), 60.0)

# ── actualizar_historial (mantiene los últimos HISTORIAL_MAX) ──
print("actualizar_historial:")
_h = list(range(utils.HISTORIAL_MAX))
_h2 = utils.actualizar_historial(_h, 99.0)
check("largo acotado", len(_h2), utils.HISTORIAL_MAX)
check("incluye el nuevo", _h2[-1], 99.0)

# ── get_genero / get_mencion ──────────────────────────────────
print("get_genero:")
check("jefa conocida", utils.get_genero("BRENDA LOPEZ"), "jefa")
check("jefe default",  utils.get_genero("CARLOS PEREZ"), "jefe")

# ── Resultado ─────────────────────────────────────────────────
print()
if _fallos:
    print(f"FALLARON {len(_fallos)} prueba(s): {_fallos}")
    sys.exit(1)
print("Todas las pruebas pasaron ✅")
sys.exit(0)
