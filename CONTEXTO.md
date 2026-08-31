# 🧭 Contexto — Bot Argos (Liverpool 456)

Documento de traspaso. Explica qué es el bot, qué se hizo en esta versión (1.9.1)
y qué queda pendiente. Léelo antes de subir o de retomar el proyecto.

---

## Qué es
Bot en Python que corre en PCs de tienda (`C:\argos`). Cada 15 min (Programador
de tareas de Windows) descarga un reporte del OMS de Liverpool con Playwright,
lo sube a Google Sheets y manda cards a Google Chat (espacios: **reporte**,
**jefes**, **tiempos**). Se auto-actualiza desde GitHub comparando `version.txt`.

- **Repo:** `maramirezr04-arch/Argos-Agente-de-Respuesta-y-Gestion-Operativa-de-Sala.`
  (privado; el nombre termina en un punto `.`)
- **Rama de trabajo:** `main`
- **Versión de este paquete:** **1.9.1**

## Arquitectura
`main.py` orquesta; el resto son módulos:
- `utils.py` — funciones puras (fechas, pisos, formatos)
- `descarga.py` — Playwright (OMS/XD) + lectura/validación CSV
- `kpi.py` — KPI de tiempos XD
- `sheets.py` — lectura/escritura de Google Sheets
- `mensajes.py` — construcción/envío de cards a Chat
- `reparador.py` — **watchdog** (lanzado por la tarea "Argos Reparador")

**Gotcha clave (arquitectura):** el bot corre como script, así que `main` vive en
`sys.modules` como `__main__`. Los submódulos hacen `import main` (vía un proxy
`_LazyMain`) para leer los globales que `main()` muta en runtime (WEBHOOK, CONFIG,
contadores). En `main.py` hay un alias `sys.modules.setdefault("main", __main__)`
que hace que ambos nombres sean el MISMO objeto. **Sin ese alias**, `import main`
crea una segunda copia con los globales por defecto (`WEBHOOK=""`) y los mensajes
se van a webhooks vacíos. No lo quiten.

## 🔒 Reglas de seguridad (IMPORTANTE)
- Los **webhooks** viven SOLO en la hoja CONFIG del Sheet, nunca en el código.
- **`GITHUB_TOKEN` y `credentials.json`** viven solo en cada PC, nunca en el repo.
- `config.py` real NO se sube (tiene token + passwords). En el repo va
  `config.example.py`. El `.gitignore` bloquea ambos.
- El repo fue público en algún momento → **rotar el token de GitHub y las
  contraseñas de OMS/XD** por las dudas (pendiente del dueño).

## ✅ Qué se corrigió en 1.9.1 (esta sesión)
1. **No arrancaba (ImportError):** los submódulos hacían `import main as M` a nivel
   de módulo → import circular. Ahora usan proxy `_LazyMain` diferido.
2. **Mensajes a webhooks vacíos:** doble módulo `__main__`/`main`. Resuelto con el
   alias de `sys.modules` (ver gotcha arriba).
3. **Doble ejecución cada 15 min:** dos tareas del Programador ("Argos Bot" y
   "Argos Reparador") lanzaban `main.py` a la vez → mensajes duplicados + 429 de
   cuota. Ahora `reparador.py` es un watchdog real (solo relanza si el bot no
   corrió en `watchdog_minutos`) y `verificar_lock` es atómico.
4. **Logs de envío engañosos** ("✅" sin verificar) → ahora reflejan el resultado.
5. **Modo `test` daba falsos negativos** en webhooks (cargaba CONFIG después de
   verificar) → ahora carga primero.
6. **`umbral_anomalia`** era una perilla muerta (1.5 hardcodeado) → ahora sí se usa,
   con validación de rango.
7. **Aviso de jefes sin webhook** (1×/día, al espacio reporte).
8. **429 con Retry-After** y reintento real en METRICAS/MONITOR.
9. **sheets.py — cuota:** DIRECTORIO+HISTORIAL en 1 `batch_get` (antes 2),
   `archivar_monitor` 1×/día, `sheet_id` centralizado.

## 🧪 Cómo verificar
- `python test_smoke.py` → valida imports, alias y que toda referencia `M.x`
  exista en main (sin red, ~1s). **Correr antes de cada release.**
- `python main.py test` → chequea internet + webhooks (carga CONFIG real).
- Ciclo real: la tarea programada corre cada 15 min; revisar `C:\argos\logs\`.

## ⏭️ Pendientes
- **Rotar token de GitHub + passwords** (seguridad; del dueño).
- **PATCH 401 al editar cards de jefes:** los webhooks entrantes de Chat no
  editan mensajes con key+token → cada ciclo crea card nueva (clutter). Falta
  confirmar con el body del 401 y decidir: borrar-y-repostear, o App de Chat con
  service account. NO está arreglado.
- **Cuota de Sheets al escalar:** el límite (60 lecturas/min) es por proyecto,
  compartido entre TODAS las PCs. Al sumar tiendas conviene cachear más lecturas.
- `actualizar_directorio.py` relee DIRECTORIO/HISTORIAL una 3ª vez — dedup pendiente.
