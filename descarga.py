"""descarga.py — Descarga de reportes desde OMS y Sistema XD.

Contiene la automatización con Playwright que baja el CSV de indicadores del
OMS (descargar_csv) y el histórico de remisiones del XD (descargar_historico_xd),
más los helpers de limpieza de Chromium, screenshots y lectura/validación del CSV.

main.py importa estas funciones. Para el estado de tiempos adaptativos
(cargar_tiempos/guardar_tiempos) se usa un acceso diferido a main para evitar
un import circular.
"""

import os
import csv
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

from config import LIVERPOOL, XD, CARPETA_DESCARGA
from utils import (
    get_fechas, calcular_espera, actualizar_historial, medir, convertir_valor,
)

log = logging.getLogger("argos.descarga")


def _main():
    """Acceso diferido a main.py (evita import circular en carga)."""
    import main
    return main


def matar_chromium_zombie():
    """Mata solo procesos Chromium de Playwright que quedaron colgados.
    NO toca chrome.exe para no cerrar el navegador personal del usuario."""
    try:
        import subprocess
        CREATE_NO_WINDOW = 0x08000000
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "chromium.exe", "/T"],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW
        )
        if "CORRECTO" in result.stdout or "SUCCESS" in result.stdout:
            log.info("Procesos Chromium zombie eliminados")
    except Exception as e:
        log.warning(f"No se pudo limpiar procesos Chromium: {e}")



def limpiar_cache_chrome():
    """Limpia cache de Chromium cada 5 intentos fallidos."""
    try:
        import shutil
        cache_dir = Path.home() / "AppData" / "Local" / "ms-playwright"
        if cache_dir.exists():
            for f in cache_dir.glob("**/Cache*"):
                try: shutil.rmtree(f, ignore_errors=True)
                except: pass
        log.info("Cache Chrome limpiado")
    except Exception:
        pass



def tomar_screenshot(page, nombre):
    """Guarda screenshot en errores."""
    try:
        screenshots = Path("screenshots")
        screenshots.mkdir(exist_ok=True)
        path = screenshots / (datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_" + nombre + ".png")
        page.screenshot(path=str(path))
        log.info(f"Screenshot guardado: {path}")
        # Borrar screenshots viejos (>7 dias)
        ahora = time.time()
        for f in screenshots.glob("*.png"):
            if (ahora - f.stat().st_mtime) / 86400 > 7:
                f.unlink()
    except Exception as e:
        log.warning(f"Error screenshot: {e}")



def validar_csv(ruta):
    """Valida que el CSV tenga datos correctos."""
    try:
        with open(ruta, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return False, "CSV vacio"
        if len(rows[0]) < 20:
            return False, f"Columnas insuficientes: {len(rows[0])}"
        # Verificar que haya fechas de hoy en alguna fila
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        tiene_hoy = any(fecha_hoy in str(r) for r in rows[1:100])
        if not tiene_hoy:
            log.warning("CSV no contiene fecha de hoy — puede estar desactualizado")
        return True, f"{len(rows)-1} filas"
    except Exception as e:
        return False, str(e)



def leer_csv(ruta):
    datos = []
    with open(ruta, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0:
                continue
            datos.append([convertir_valor(v) for v in row])

    col_status  = 8
    espera      = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Mercancia en Espera de Entrega")
    etiquetas   = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Etiqueta Generada")
    sin_asignar = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Sin Asignar")
    rechazados  = sum(1 for r in datos if len(r) > col_status and r[col_status] == "Rechazado")

    resumen = {"total": len(datos), "espera": espera, "etiquetas": etiquetas, "sin_asignar": sin_asignar, "rechazados": rechazados}
    log.info(f"CSV leido: {len(datos)} filas ✅")
    return datos, resumen



def descargar_csv(visible=False):
    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)
    fecha_ayer, fecha_hoy = get_fechas()
    log.info(f"Descargando reporte {fecha_ayer} -> {fecha_hoy}")

    tiempos       = _main().cargar_tiempos()
    t_login       = calcular_espera(tiempos["login"])
    t_indicadores = calcular_espera(tiempos["indicadores"])
    t_calendario  = calcular_espera(tiempos["calendario"])
    t_datos       = calcular_espera(tiempos["datos"])
    t_descarga    = calcular_espera(tiempos["descarga"])

    # Coordenadas dinámicas según el día de semana en que cae el día 1 del mes actual
    # El calendario del OMS usa lunes=columna1 (x=524) hasta domingo=columna7 (x=840)
    _cols = [524, 577, 630, 683, 735, 788, 840]
    _rows = [240, 290, 340, 390, 440]
    _offset = datetime.now().replace(day=1).weekday()  # 0=lunes, 6=domingo
    coords = {}
    for _d in range(1, 32):
        _cell = _offset + (_d - 1)
        _r, _c = divmod(_cell, 7)
        if _r < len(_rows):
            coords[_d] = (_cols[_c], _rows[_r])

    hoy_dia  = datetime.now().day
    ayer_dia = (datetime.now() - timedelta(days=1)).day
    t0       = time.time()
    destino  = None

    # ── Helpers Flutter-web ───────────────────────────────────────
    def flutter_listo(page, timeout=30000):
        """Espera a que Flutter haya pintado algo en el canvas (flt-glass-pane visible)."""
        page.wait_for_function(
            "() => document.querySelector('flt-glass-pane') !== null",
            timeout=timeout
        )

    def click_y_escribir(page, x, y, texto, timeout_input=8000):
        """
        Flutter web: click en coordenada → espera a que aparezca flt-text-editing
        (el input oculto que Flutter activa) → escribe el texto.
        Si flt-text-editing no aparece, escribe directamente con keyboard.
        """
        page.mouse.click(x, y)
        try:
            page.wait_for_selector("flt-text-editing-host input, .flt-text-editing",
                                   timeout=timeout_input)
            page.keyboard.type(texto, delay=60)
        except Exception:
            # fallback: escribir directamente
            page.keyboard.type(texto, delay=80)

    def esperar_red_quieta(page, timeout=15000):
        """Espera a que no haya peticiones de red activas (Flutter cargó datos)."""
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass  # timeout aceptable, continuar

    # ── Intentos infinitos — reintenta hasta descargar ───────────
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    exito_evento = threading.Event()
    destino      = None
    errores      = {}
    intento_global = 0

    def _un_intento(n, retraso=0):
        """Flujo completo de descarga con retraso opcional antes de arrancar."""
        nonlocal destino
        if retraso > 0:
            # Esperar retraso, pero salir antes si ya hubo éxito
            exito_evento.wait(timeout=retraso)
        if exito_evento.is_set():
            return None

        log.info(f"[#{n}] Iniciando intento...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=not visible,
                    args=["--disable-extensions", "--no-sandbox", "--disable-dev-shm-usage"]
                )
                try:
                    context = browser.new_context(viewport={"width": 1366, "height": 768},
                                                  accept_downloads=True)
                    page = context.new_page()

                    if exito_evento.is_set(): return None

                    page.goto(LIVERPOOL["url_login"], timeout=60000)
                    page.wait_for_load_state("domcontentloaded")
                    flutter_listo(page, timeout=30000)
                    time.sleep(8)  # Flutter necesita ~8s para renderizar visualmente

                    if exito_evento.is_set(): return None

                    # Login: retry hasta que Flutter active el campo de texto
                    def _click_y_type(x, y, texto, max_r=15):
                        for _ in range(max_r):
                            page.mouse.click(x, y)
                            try:
                                page.wait_for_selector("flt-text-editing-host input", timeout=2500)
                                page.keyboard.type(texto, delay=60)
                                return
                            except Exception:
                                time.sleep(1.5)
                        raise Exception(f"No se pudo activar input en ({x},{y})")

                    _click_y_type(683, 272, LIVERPOOL["usuario"])
                    time.sleep(0.3)
                    _click_y_type(683, 344, LIVERPOOL["password"])
                    time.sleep(0.3)
                    page.mouse.click(683, 480)
                    try:
                        page.wait_for_function(
                            "() => !window.location.href.includes('login')",
                            timeout=30000
                        )
                    except Exception:
                        pass
                    if "login" in page.url:
                        raise Exception("Login fallido")
                    log.info(f"[#{n}] Login OK ✓")

                    if exito_evento.is_set(): return None

                    # Navegar a Indicadores y esperar URL #indicators
                    time.sleep(5)
                    page.mouse.click(683, 114)
                    try:
                        page.wait_for_function(
                            "() => window.location.href.includes('indicators')",
                            timeout=15000
                        )
                    except Exception:
                        pass
                    time.sleep(5)
                    log.info(f"[#{n}] Indicadores OK ✓  URL: {page.url}")

                    if exito_evento.is_set(): return None

                    # Calendario + Guardar = inicia la descarga
                    log.info(f"[#{n}] Descargando...")
                    timeout_ms = max(120000, int(t_descarga * 1000 * 4))
                    with page.expect_download(timeout=timeout_ms) as dl_info:
                        page.mouse.click(1275, 100); time.sleep(4)
                        ax, ay = coords[ayer_dia]; page.mouse.click(ax, ay); time.sleep(1)
                        hx, hy = coords[hoy_dia];  page.mouse.click(hx, hy); time.sleep(1)
                        page.mouse.click(1321, 24)  # Guardar — inicia descarga
                        time.sleep(10)
                    download = dl_info.value

                    if not exito_evento.is_set():
                        exito_evento.set()
                        nombre  = download.suggested_filename
                        dst     = os.path.join(CARPETA_DESCARGA, nombre)
                        download.save_as(dst)
                        destino = dst
                        log.info(f"[#{n}] ✅ Descarga exitosa!")
                        return dst
                    return None

                finally:
                    try: browser.close()
                    except Exception: pass

        except Exception as e:
            if not exito_evento.is_set():
                errores[n] = str(e)[:150]
                log.warning(f"[#{n}] Falló: {errores[n]}")
            return None

    matar_chromium_zombie()
    while not exito_evento.is_set():
        intento_global += 1
        retraso = 0 if intento_global == 1 else 10
        if retraso:
            log.info(f"Reintentando en {retraso}s (intento #{intento_global})...")
            time.sleep(retraso)
        matar_chromium_zombie()
        fut = ThreadPoolExecutor(max_workers=1).submit(_un_intento, intento_global, 0)
        try:
            res = fut.result(timeout=300)
            if res:
                break
        except Exception:
            pass

    if not destino or not os.path.exists(destino):
        raise Exception("Descarga fallida — loop interrumpido inesperadamente")

    t_total = medir(t0)
    log.info(f"Descarga completada en {t_total}s")

    # Borrar archivos anteriores (conservar XD histórico para KPI al cierre)
    try:
        for f in Path(CARPETA_DESCARGA).glob("*.csv"):
            if str(f) != destino and not f.name.startswith("historico_xd_"):
                f.unlink()
    except Exception:
        pass

    # Actualizar tiempos históricos
    tiempos["login"]       = actualizar_historial(tiempos["login"],       t_login)
    tiempos["indicadores"] = actualizar_historial(tiempos["indicadores"], t_indicadores)
    tiempos["calendario"]  = actualizar_historial(tiempos["calendario"],  t_calendario)
    tiempos["datos"]       = actualizar_historial(tiempos["datos"],        t_datos)
    tiempos["descarga"]    = actualizar_historial(tiempos["descarga"],     t_total)
    _main().guardar_tiempos(tiempos)

    log.info(f"Archivo guardado: {destino} ✅")
    return destino, 1



def descargar_historico_xd(visible=False):
    """Descarga el historico de remisiones de Sistema XD via Playwright.
    visible=True abre el navegador en pantalla (para pruebas).

    Flujo:
      1. Login
      2. Reportes → Historico Remisiones
      3. Llenar Fecha Inicial (ayer) y Fecha Final (hoy) en los inputs
      4. Clic en Buscar → esperar que la tabla cargue
      5. Clic en Exportar / Exportación → capturar descarga
    """
    from config import XD
    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)

    # ── Borrar archivos XD anteriores ──────────────────────────
    for viejo in Path(CARPETA_DESCARGA).glob("historico_xd_*.csv"):
        try:
            viejo.unlink()
            log.info(f"Historico XD anterior eliminado: {viejo.name}")
        except Exception as e:
            log.warning(f"No se pudo borrar {viejo.name}: {e}")

    hoy  = datetime.now()
    ayer = hoy - timedelta(days=1)
    hoy_str  = hoy.strftime("%Y-%m-%d")
    destino  = os.path.join(CARPETA_DESCARGA, f"historico_xd_{hoy_str}.csv")

    # Formato de fecha que usa el XD (se intenta dd/mm/yyyy y yyyy-mm-dd)
    fecha_ini_str = ayer.strftime("%d/%m/%Y")
    fecha_fin_str = hoy.strftime("%d/%m/%Y")
    log.info(f"Descargando historico XD {fecha_ini_str} → {fecha_fin_str}...")

    matar_chromium_zombie()
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not visible,
                slow_mo=400 if visible else 0,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                accept_downloads=True
            )
            page = context.new_page()

            # ── 1. Login ─────────────────────────────────────────
            log.info("XD [1/5]: abriendo login...")
            page.goto(XD["url_login"], timeout=60000)
            page.wait_for_load_state("load", timeout=30000)
            time.sleep(3)

            log.info("XD [2/5]: llenando credenciales...")
            # Ionic Angular bloquea .fill() con autocomplete="off".
            # Usamos el setter nativo + dispatchEvent para forzar change detection.
            page.wait_for_selector("input.native-input", timeout=15000)
            inputs = page.locator("input.native-input")

            def _set_ionic_input(locator, value):
                el = locator.element_handle()
                page.evaluate("""([el, val]) => {
                    var setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", [el, value])

            inputs.nth(0).click()
            time.sleep(0.2)
            _set_ionic_input(inputs.nth(0), XD["usuario"])
            time.sleep(0.3)
            inputs.nth(1).click()
            time.sleep(0.2)
            _set_ionic_input(inputs.nth(1), XD["password"])
            time.sleep(0.5)
            # Botón INICIAR SESIÓN
            page.locator("ion-button, button").filter(has_text="INICIAR").click()
            log.info("XD: credenciales enviadas, esperando login...")
            # SPA Angular — no llega a networkidle; esperamos que desaparezca el login
            try:
                page.wait_for_selector("input.native-input", state="hidden", timeout=15000)
            except Exception:
                pass
            time.sleep(3)

            # ── 2. Navegar a Reportes → Historico Remisiones ─────
            log.info("XD [3/5]: navegando a Historico Remisiones...")
            page.wait_for_selector("text=Reportes", timeout=20000)
            page.get_by_text("Reportes").first.click()
            time.sleep(2)
            page.get_by_text("Historico Remisiones").first.click()
            time.sleep(5)  # dar tiempo al SPA Angular para renderizar

            # ── 3. Cambiar al iframe que contiene el formulario ───
            log.info("XD [4/5]: accediendo al iframe de Historico Remisiones...")
            # El contenido real está en un iframe externo en pro-oms-report-*.run.app
            iframe_loc = page.frame_locator("iframe")
            # Esperar a que el botón Buscar cargue dentro del iframe
            iframe_loc.get_by_text("Buscar", exact=True).wait_for(timeout=20000)
            log.info("XD: iframe cargado ✅")

            # Leer y corregir fechas dentro del iframe
            try:
                inputs_fecha = iframe_loc.locator("input[type='date']")
                n_f = inputs_fecha.count()
                log.info(f"XD: inputs de fecha en iframe: {n_f}")
                if n_f >= 2:
                    fi_val = inputs_fecha.nth(0).input_value() or ""
                    ff_val = inputs_fecha.nth(1).input_value() or ""
                    log.info(f"XD: fechas actuales: '{fi_val}' → '{ff_val}'")
                    fecha_ini_esperada = ayer.strftime("%Y-%m-%d")
                    fecha_fin_esperada = hoy.strftime("%Y-%m-%d")
                    if fi_val != fecha_ini_esperada:
                        inputs_fecha.nth(0).triple_click()
                        inputs_fecha.nth(0).fill(fecha_ini_esperada)
                        log.info(f"XD: fecha inicial → {fecha_ini_esperada}")
                    if ff_val != fecha_fin_esperada:
                        inputs_fecha.nth(1).triple_click()
                        inputs_fecha.nth(1).fill(fecha_fin_esperada)
                        log.info(f"XD: fecha final → {fecha_fin_esperada}")
                else:
                    log.info("XD: usando fechas por defecto del sistema")
            except Exception as e:
                log.warning(f"XD: no se pudieron verificar fechas: {e}")

            time.sleep(0.5)

            # ── 4. Clic en Buscar (dentro del iframe) ─────────────
            log.info("XD: haciendo clic en Buscar...")
            iframe_loc.get_by_text("Buscar", exact=True).click()
            log.info("XD: clic en Buscar ✅")

            # ── 5. Esperar que la búsqueda termine ───────────────
            # El botón cambia a "Cargando..." mientras procesa y vuelve a "Buscar" al terminar.
            log.info("XD: esperando que el botón vuelva a 'Buscar' (señal de carga completa)...")
            try:
                # Primero esperar que aparezca "Cargando..." (confirma que inició la búsqueda)
                iframe_loc.get_by_text("Cargando", exact=False).wait_for(timeout=10000)
                log.info("XD: búsqueda en proceso (Cargando...)...")
            except Exception:
                log.info("XD: no se detectó 'Cargando', continuando...")

            try:
                # Luego esperar que "Cargando" desaparezca y vuelva "Buscar"
                iframe_loc.get_by_text("Buscar", exact=True).wait_for(timeout=120000)
                log.info("XD: carga completada — botón volvió a 'Buscar' ✅")
            except Exception as e:
                log.warning(f"XD: timeout esperando fin de carga: {e}")

            # Leer total de registros para el log
            try:
                txt = iframe_loc.locator("text=/Total Registros/i").first.inner_text(timeout=3000)
                log.info(f"XD: {txt.strip()}")
            except Exception:
                pass
            time.sleep(1)

            # ── 6. Clic en Exportación (dentro del iframe) ────────
            log.info("XD [5/5]: buscando botón Exportación...")
            btn_exportar = None
            for txt_btn in ["Exportación", "EXPORTACIÓN", "Exportar", "EXPORTAR", "Export", "CSV", "Descargar"]:
                try:
                    loc = iframe_loc.get_by_text(txt_btn, exact=True)
                    if loc.count() > 0:
                        btn_exportar = loc.first
                        log.info(f"XD: botón Exportación encontrado: '{txt_btn}'")
                        break
                except Exception:
                    pass

            if not btn_exportar:
                log.error("XD: no se encontró botón de exportación")
                try:
                    shot = os.path.join(CARPETA_DESCARGA, "xd_debug.png")
                    page.screenshot(path=shot)
                    log.info(f"XD: screenshot guardado en {shot}")
                except Exception:
                    pass
                raise Exception("No se encontró botón de exportación en XD")

            log.info("XD: iniciando descarga...")
            with page.expect_download(timeout=180000) as dl_info:
                btn_exportar.click()

            download = dl_info.value
            download.save_as(destino)
            log.info(f"XD: archivo guardado en {destino} ✅")

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    log.info(f"Historico XD descargado correctamente: {destino} ✅")
    return destino

