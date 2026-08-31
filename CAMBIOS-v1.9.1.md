# v1.9.1 — Fix crítico de la modularización v1.9.0

Corrige que, tras dividir el `main.py` monolítico en módulos, el bot **no
arrancaba** y los **mensajes a Chat se enviaban a webhooks vacíos**.

## Archivos modificados / nuevos
- **main.py** — alias `sys.modules["main"] → __main__`; carga de CONFIG en modo `test`;
  `verificar_lock` ahora atómico (`O_CREAT|O_EXCL`); `reparador.py` y `ejecutar_reparar.vbs`
  agregados a `_UPDATE_EXTRA`
- **kpi.py** / **sheets.py** / **mensajes.py** — acceso diferido a `main` vía proxy `_LazyMain`
- **mensajes.py** — logs de envío ahora reflejan el resultado real (no "✅" a ciegas)
- **reparador.py** *(NUEVO)* — watchdog real (ver abajo)
- **ejecutar_reparar.vbs** — ahora lanza `reparador.py` en vez de `main.py`
- **version.txt** / **VERSION** — 1.9.0 → 1.9.1

## Bugs corregidos

1. **ImportError al arrancar (crítico).** `kpi/sheets/mensajes` hacían
   `import main as M` a nivel de módulo, re-ejecutando `main.py` a mitad de sus
   propios imports → `ImportError: cannot import name 'calcular_kpi_xd'`. El bot
   no levantaba. Ahora usan un proxy `_LazyMain` que difiere el `import main`.

2. **Mensajes a webhooks vacíos (crítico).** Al correr como `python main.py`,
   el módulo vivo es `__main__`; `import main` en los submódulos creaba una
   SEGUNDA copia con los globales por defecto (`WEBHOOK=""`). Los submódulos
   leían esa copia muerta → posts a URL vacía que fallaban y se encolaban.
   Se corrige aliasando `main` ⇄ `__main__` en `sys.modules` para que compartan
   estado (el webhook real que carga `cargar_config_remota`).

3. **Logs engañosos.** `enviar_notificaciones_vencidas` / `enviar_chat`
   registraban "enviado ✅" sin verificar el POST. Ahora distinguen éxito/fallo.

4. **Modo `test` daba falsos negativos.** `verificar_webhooks()` corría antes de
   cargar la hoja CONFIG → siempre reportaba los 3 webhooks como FALLA. Ahora
   carga CONFIG primero.

5. **Doble ejecución cada 15 min (crítico).** Dos tareas del Programador
   ("Argos Bot" y "Argos Reparador") lanzaban `main.py` a la vez → dos bots en
   paralelo → mensajes DUPLICADOS al chat, dobles logins al OMS y error 429 de
   cuota de lectura en Sheets. El `bot.lock` no lo frenaba por una carrera
   (no atómico). Se corrige con:
   - **`reparador.py`** convertido en watchdog real: lee `health.json` y solo
     relanza + avisa al chat de reporte si el bot NO corrió en los últimos
     `watchdog_minutos`; si está sano, no hace nada. Usa el cache local (no gasta
     cuota de Sheets). Config: `watchdog_activo`, `watchdog_minutos`,
     `destino_watchdog` (llaves ya existentes en CONFIG).
   - **`verificar_lock` atómico**: aunque dos instancias arranquen a la vez,
     solo una corre.

## Mejoras adicionales (sugerencias)
- **`umbral_anomalia` ahora sí funciona.** `es_alerta_anomalia` usaba `1.5`
  hardcodeado e ignoraba la config. Ahora usa `UMBRAL_ANOMALIA`, con validación
  a rango [1.0–5.0] al cargar (un `2026` mal tecleado se ignora y cae a 1.5).
- **Aviso de jefes sin webhook.** Una vez al día, si hay jefes con remisiones
  pero sin webhook configurado, se avisa al espacio jefes (antes se perdían en
  silencio).
- **429 con `Retry-After`.** `_sheets_con_reintento` respeta el header
  Retry-After de Google si viene (si no, backoff 10/20s).
- **`test_smoke.py` (NUEVO).** Prueba de humo sin red: valida imports, alias
  y que toda referencia `M.x` de los submódulos exista en main. Correr antes
  de cada release (`python test_smoke.py`).
- **`.gitignore` (NUEVO).** Evita subir por accidente `config.py`,
  `credentials.json` y los archivos de estado.

## Optimización de sheets.py (cuota de lecturas)
- **DIRECTORIO + HISTORIAL en 1 lectura.** `actualizar_sheets` los leía con dos
  `get_all_values` separados; ahora un solo `batch_get` (verificado: produce
  dicts idénticos, 149=149 secciones).
- **`archivar_monitor_si_necesario` corre 1×/día** (marca local) en vez de leer
  todo MONITOR cada 15 min solo para chequear el tamaño.
- **Reintento real de 429.** `guardar_metricas_dia` y `guardar_en_monitor`
  tragaban su propia excepción, así que el reintento externo nunca actuaba.
  Ahora reintentan internamente en 429 (3 intentos, backoff) y siguen siendo
  no-fatales si falla.
- **`sheet_id` centralizado.** Estaba hardcodeado 7× (`"135lsymm…"`); ahora todo
  usa `GOOGLE["sheet_id"]`.
- **Log DIAG de DIRECTORIO** bajado a `debug` (era ruido en cada ciclo).

Verificado en un ciclo real (19:45): Sheet 1, APP 2.0, METRICAS y MONITOR OK,
0 errores, 0 429, 1 sola instancia.

## Nota de despliegue
- `reparador.py` y `ejecutar_reparar.vbs` se propagan solos vía `_UPDATE_EXTRA`
  al actualizar a 1.9.1. La tarea "Argos Reparador" ya existente empezará a
  comportarse como watchdog en cuanto se actualice su `.vbs`.
- (Opcional) Desfasar la tarea "Argos Reparador" ~7 min respecto a "Argos Bot"
  (p.ej. inicio 10:07) es más prolijo, pero no es necesario: el watchdog +
  el lock atómico ya evitan la duplicación.

## Verificación
- 6 ciclos reales de la tarea programada tras el fix: 6 descargas OMS, 6
  reportes, 3 cards de jefes, **0 errores / 0 ImportError / 0 URL vacía**.
- Harness real (sin stubs) ejecutando cada módulo 20×: utils, descarga, kpi,
  sheets, mensajes, identidad → **todos 20/20 PASS**.
