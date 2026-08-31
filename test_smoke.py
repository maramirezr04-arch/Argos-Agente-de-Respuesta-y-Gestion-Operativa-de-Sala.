# -*- coding: utf-8 -*-
"""test_smoke.py — Prueba de humo. Correr ANTES de cada release.

Verifica exactamente lo que rompió la modularización 1.9.0:
  1. Todos los módulos importan sin ImportError.
  2. El alias sys.modules['main'] apunta al módulo real (submódulos y main
     comparten estado; sin esto los mensajes van a webhooks vacíos).
  3. TODA referencia `M.<x>` en los submódulos existe en main — así una
     referencia colgada se detecta acá y no en producción.

No manda mensajes, no toca Sheets, no descarga nada. Corre en ~1s.
Necesita el entorno del bot (config.py + dependencias instaladas).
Uso:  python test_smoke.py      (exit 0 = OK, 1 = falló)
"""
import sys, os, re, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fallos = []

# 1) Importar todo el paquete
try:
    import main
    import utils, descarga, kpi, sheets, mensajes            # noqa: F401
except Exception as e:
    print("SMOKE TEST: FALLÓ en el import ->", repr(e))
    sys.exit(1)

# Silenciar el logging que main haya configurado (no ensuciar el log de prod)
logging.getLogger().handlers = []

# 2) Alias de módulo __main__ ⇄ main
if sys.modules.get("main") is not main:
    fallos.append("sys.modules['main'] no es el módulo main (alias roto)")

# 3) Referencias M.<x> de los submódulos resueltas en main
SUBMODULOS = ["kpi.py", "sheets.py", "mensajes.py"]
patron = re.compile(r"\bM\.([A-Za-z_][A-Za-z0-9_]*)")
raiz = os.path.dirname(os.path.abspath(__file__))
for archivo in SUBMODULOS:
    with open(os.path.join(raiz, archivo), encoding="utf-8") as f:
        codigo = f.read()
    faltan = sorted({n for n in patron.findall(codigo) if not hasattr(main, n)})
    if faltan:
        fallos.append(f"{archivo}: referencias M.x que NO existen en main: {faltan}")

if fallos:
    print("SMOKE TEST: FALLÓ")
    for f in fallos:
        print("  -", f)
    sys.exit(1)

print("SMOKE TEST: OK — imports, alias y todas las referencias M.x verificados")
sys.exit(0)
