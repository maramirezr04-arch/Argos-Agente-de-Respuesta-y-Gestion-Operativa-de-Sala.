// ══════════════════════════════════════════════════════════════
//  Argos — Google Apps Script
//  Versión: 1.2.4
//  Última actualización: 2026-06-09
//
//  CÓMO USAR:
//  1. Abre tu proyecto en script.google.com
//  2. Borra todo el contenido actual
//  3. Pega este archivo completo
//  4. Guarda (Ctrl+S) y vuelve a implementar como aplicación web
//     → Ejecutar como: Yo
//     → Quién puede acceder: Cualquier persona
//  5. Copia la URL nueva y actualízala en dashboard.html (APPS_SCRIPT_URL)
// ══════════════════════════════════════════════════════════════

var SHEET_ID = "135lsymm5A67_ieYZLaKIfvPpkyqRWbUf9UV-mv3b7js";

// ── UTILIDAD ──────────────────────────────────────────────────
function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ═══════════════════════════════════════════════════════════════
//  doGet — peticiones de LECTURA desde el dashboard
// ═══════════════════════════════════════════════════════════════
function doGet(e) {
  var accion = e.parameter.accion || e.parameter.action || "";
  var ss = SpreadsheetApp.openById(SHEET_ID);

  // ── CONFIG ────────────────────────────────────────────────
  if (accion === "config") {
    var hoja = ss.getSheetByName("CONFIG");
    if (!hoja) return json({ error: "Hoja CONFIG no existe" });
    var rows = hoja.getDataRange().getValues();
    var cfg = {};
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) cfg[rows[i][0]] = rows[i][1];
    }
    return json({ ok: true, config: cfg });
  }

  // ── DIRECTORIO ────────────────────────────────────────────
  if (accion === "directorio") {
    var hoja = ss.getSheetByName("DIRECTORIO");
    if (!hoja) return json({ error: "Hoja DIRECTORIO no existe" });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) datos.push({
        seccion:   rows[i][0],
        nombre:    rows[i][1],
        jefe:      rows[i][2],
        ubicacion: rows[i][5] || ""
      });
    }
    return json({ ok: true, directorio: datos });
  }

  // ── DESCANSOS ─────────────────────────────────────────────
  if (accion === "descansos") {
    var hoja = ss.getSheetByName("DESCANSOS");
    if (!hoja) return json({ ok: true, descansos: [] });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) datos.push({
        fecha:         rows[i][0],
        jefe_descansa: rows[i][1],
        jefe_cubre:    rows[i][2]
      });
    }
    return json({ ok: true, descansos: datos });
  }

  // ── WEBHOOKS JEFES ────────────────────────────────────────
  if (accion === "webhooks_jefes") {
    var hoja = ss.getSheetByName("WEBHOOKS_JEFES");
    if (!hoja) return json({ ok: true, jefes: [] });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) datos.push({
        nombre:        rows[i][0],
        webhook:       rows[i][1] || "",
        activo:        rows[i][2] !== false && rows[i][2] !== "FALSE",
        tiene_webhook: !!(rows[i][1])
      });
    }
    return json({ ok: true, jefes: datos });
  }

  // ── FESTIVOS ──────────────────────────────────────────────
  if (accion === "festivos") {
    var hoja = ss.getSheetByName("FESTIVOS");
    if (!hoja) return json({ ok: true, festivos: [] });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) datos.push({ fecha: rows[i][0], descripcion: rows[i][1] || "" });
    }
    return json({ ok: true, festivos: datos });
  }

  // ── LOGS (últimas 30 ejecuciones del bot) ─────────────────
  // Columnas MONITOR: Fecha(0) Hora(1) Duracion(2) Total(3)
  //                   Vencidas(4) Estado(5) Intentos(6) Error(7)
  if (accion === "logs") {
    var hoja = ss.getSheetByName("MONITOR");
    if (!hoja) return json({ ok: true, logs: [] });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    var inicio = Math.max(1, rows.length - 30);
    for (var i = inicio; i < rows.length; i++) {
      if (rows[i][0]) datos.push({
        fecha:    rows[i][0],
        hora:     rows[i][1],
        duracion: rows[i][2],
        total:    rows[i][3],
        vencidas: rows[i][4],
        estado:   rows[i][5],
        intentos: rows[i][6],
        error:    rows[i][7] || ""
      });
    }
    return json({ ok: true, logs: datos });
  }

  // ── HISTORICO ─────────────────────────────────────────────
  if (accion === "historico") {
    var hoja = ss.getSheetByName("HISTORIAL");
    if (!hoja) return json({ ok: true, historial: [] });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) datos.push(rows[i]);
    }
    return json({ ok: true, historial: datos });
  }

  // ── PCS — listar PCs registradas ─────────────────────────
  if (accion === "getPcs") {
    var hoja = ss.getSheetByName("PCS");
    if (!hoja) return json({ pcs: [] });
    var rows = hoja.getDataRange().getValues();
    var pcs = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) pcs.push({
        nombre:          rows[i][0],
        estado:          rows[i][1] || "activo",
        ultima_conexion: rows[i][2] || "",
        version:         rows[i][3] || ""
      });
    }
    return json({ pcs: pcs });
  }

  // ── PCS — cambiar estado (GET con parámetros) ─────────────
  if (accion === "setPcEstado") {
    var nombre2 = e.parameter.nombre;
    var estado2 = e.parameter.estado;
    var hoja = ss.getSheetByName("PCS");
    if (!hoja) return json({ ok: false, error: "Hoja PCS no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0] === nombre2) {
        hoja.getRange(i + 1, 2).setValue(estado2);
        return json({ ok: true });
      }
    }
    return json({ ok: false, error: "PC no encontrada" });
  }

  // ── MENSAJES PROGRAMADOS — listar ────────────────────────
  // Columnas: ID(0) Texto(1) Intervalo_ciclos(2) Destino(3)
  //           Activo(4) Ultimo_envio(5) Creado(6)
  if (accion === "mensajes_programados") {
    var hoja = ss.getSheetByName("MENSAJES_PROGRAMADOS");
    if (!hoja) return json({ ok: true, mensajes: [] });
    var rows = hoja.getDataRange().getValues();
    var datos = [];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) datos.push({
        id:           rows[i][0],
        texto:        rows[i][1],
        intervalo_ciclos:rows[i][2],
        destino:      rows[i][3],
        activo:       rows[i][4],
        ultimo_envio: rows[i][5],
        creado:       rows[i][6]
      });
    }
    return json({ ok: true, mensajes: datos });
  }

  return json({ error: "accion no reconocida: " + accion });
}

// ═══════════════════════════════════════════════════════════════
//  doPost — peticiones de ESCRITURA desde el dashboard
// ═══════════════════════════════════════════════════════════════
function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents); } catch (ex) {}
  var accion = body.accion || body.action || "";
  var ss = SpreadsheetApp.openById(SHEET_ID);

  // ── GUARDAR CONFIG ────────────────────────────────────────
  if (accion === "config") {
    var hoja = ss.getSheetByName("CONFIG");
    if (!hoja) hoja = ss.insertSheet("CONFIG");
    var cfg = body.config || {};
    var rows = hoja.getDataRange().getValues();
    var mapa = {};
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) mapa[rows[i][0]] = i + 1;
    }
    var cambios = 0;
    for (var key in cfg) {
      if (mapa[key]) {
        hoja.getRange(mapa[key], 2).setValue(cfg[key]);
      } else {
        hoja.appendRow([key, cfg[key]]);
      }
      cambios++;
    }
    return json({ ok: true, cambios: cambios });
  }

  // ── MENSAJE DE PRUEBA (lee el webhook desde CONFIG) ───────
  if (accion === "prueba_mensaje") {
    var hojaC = ss.getSheetByName("CONFIG");
    if (!hojaC) return json({ error: "Hoja CONFIG no existe" });
    var clave = "webhook_" + (body.espacio || "");
    var filas = hojaC.getDataRange().getValues();
    var url = "";
    for (var i = 1; i < filas.length; i++) {
      if (String(filas[i][0]).trim() === clave) { url = String(filas[i][1]).trim(); break; }
    }
    if (!url || url.indexOf("https://") !== 0)
      return json({ error: "Webhook '" + clave + "' no configurado en CONFIG" });
    try {
      UrlFetchApp.fetch(url, {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify({ text: body.mensaje || "" })
      });
      return json({ ok: true });
    } catch (ex) {
      return json({ error: "No se pudo enviar: " + ex });
    }
  }

  // ── AGREGAR DESCANSO ──────────────────────────────────────
  if (accion === "agregar_descanso") {
    var hoja = ss.getSheetByName("DESCANSOS");
    if (!hoja) hoja = ss.insertSheet("DESCANSOS");
    hoja.appendRow([body.fecha, body.jefe_descansa, body.jefe_cubre]);
    // Contar secciones del jefe en DIRECTORIO para el toast del dashboard
    var secciones_agregadas = 0;
    var hojaDir = ss.getSheetByName("DIRECTORIO");
    if (hojaDir && body.jefe_descansa) {
      var rowsDir = hojaDir.getDataRange().getValues();
      var jefeNorm = String(body.jefe_descansa).toLowerCase().trim();
      for (var i = 1; i < rowsDir.length; i++) {
        if (rowsDir[i][0] && String(rowsDir[i][2]).toLowerCase().trim() === jefeNorm) {
          secciones_agregadas++;
        }
      }
    }
    return json({ ok: true, secciones_agregadas: secciones_agregadas });
  }

  // ── BORRAR DESCANSO ───────────────────────────────────────
  if (accion === "borrar_descanso") {
    var hoja = ss.getSheetByName("DESCANSOS");
    if (!hoja) return json({ ok: false, error: "Hoja no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = rows.length - 1; i >= 1; i--) {
      if (rows[i][0] == body.fecha && rows[i][1] == body.jefe_descansa) {
        hoja.deleteRow(i + 1);
        break;
      }
    }
    return json({ ok: true });
  }

  // ── TOGGLE WEBHOOK JEFE ───────────────────────────────────
  if (accion === "toggle_webhook_jefe") {
    var hoja = ss.getSheetByName("WEBHOOKS_JEFES");
    if (!hoja) return json({ ok: false, error: "Hoja no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0] === body.nombre) {
        hoja.getRange(i + 1, 3).setValue(body.activo);
        return json({ ok: true });
      }
    }
    return json({ ok: false, error: "Jefe no encontrado" });
  }

  // ── ACTUALIZAR JEFE ───────────────────────────────────────
  if (accion === "actualizar_jefe") {
    var hoja = ss.getSheetByName("DIRECTORIO");
    if (!hoja) return json({ ok: false, error: "Hoja no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) {
      if (String(rows[i][0]) === String(body.seccion)) {
        if (body.jefe !== undefined)      hoja.getRange(i + 1, 3).setValue(body.jefe);
        if (body.ubicacion !== undefined) hoja.getRange(i + 1, 6).setValue(body.ubicacion);
        return json({ ok: true });
      }
    }
    return json({ ok: false, error: "Seccion no encontrada" });
  }

  // ── AGREGAR FESTIVO ───────────────────────────────────────
  if (accion === "agregar_festivo") {
    var hoja = ss.getSheetByName("FESTIVOS");
    if (!hoja) hoja = ss.insertSheet("FESTIVOS");
    hoja.appendRow([body.fecha, body.descripcion || ""]);
    return json({ ok: true });
  }

  // ── BORRAR FESTIVO ────────────────────────────────────────
  if (accion === "borrar_festivo") {
    var hoja = ss.getSheetByName("FESTIVOS");
    if (!hoja) return json({ ok: false, error: "Hoja no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = rows.length - 1; i >= 1; i--) {
      if (rows[i][0] == body.fecha) {
        hoja.deleteRow(i + 1);
        break;
      }
    }
    return json({ ok: true });
  }

  // ── ACCIONES RÁPIDAS (pausar / reanudar / limpiar cola) ───
  if (accion === "accion_rapida") {
    var hoja = ss.getSheetByName("CONFIG");
    if (!hoja) return json({ ok: false, error: "Hoja CONFIG no existe" });
    var rows = hoja.getDataRange().getValues();
    var mapa = {};
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) mapa[rows[i][0]] = i + 1;
    }
    if (body.tipo === "pausar") {
      if (mapa["pausado"]) hoja.getRange(mapa["pausado"], 2).setValue("si");
      else hoja.appendRow(["pausado", "si"]);
      return json({ ok: true, mensaje: "Bot pausado" });
    }
    if (body.tipo === "reanudar") {
      if (mapa["pausado"]) hoja.getRange(mapa["pausado"], 2).setValue("no");
      else hoja.appendRow(["pausado", "no"]);
      return json({ ok: true, mensaje: "Bot reanudado" });
    }
    if (body.tipo === "limpiar_cola") {
      return json({ ok: true, mensaje: "Cola limpiada en la siguiente ejecucion" });
    }
    return json({ ok: false, error: "tipo no reconocido" });
  }

  // ── SUBIR HISTORICO ───────────────────────────────────────
  if (accion === "subir_historico") {
    var hoja = ss.getSheetByName("HISTORIAL");
    if (!hoja) hoja = ss.insertSheet("HISTORIAL");
    var filas = body.filas || [];
    if (filas.length > 0) {
      var lastRow = hoja.getLastRow();
      hoja.getRange(lastRow + 1, 1, filas.length, filas[0].length).setValues(filas);
    }
    return json({ ok: true, filas: filas.length });
  }

  // ── KPI VENDEDORES — señal para el bot ───────────────────
  if (accion === "kpi_vendedores") {
    var hoja = ss.getSheetByName("CONFIG");
    if (!hoja) hoja = ss.insertSheet("CONFIG");
    var rows = hoja.getDataRange().getValues();
    var mapa = {};
    for (var i = 1; i < rows.length; i++) {
      if (rows[i][0]) mapa[rows[i][0]] = i + 1;
    }
    if (mapa["kpi_vendedores_flag"]) {
      hoja.getRange(mapa["kpi_vendedores_flag"], 2).setValue("si");
    } else {
      hoja.appendRow(["kpi_vendedores_flag", "si"]);
    }
    return json({ ok: true, mensaje: "Señal enviada" });
  }

  // ── MENSAJES PROGRAMADOS — agregar ───────────────────────
  if (accion === "agregar_mensaje_programado") {
    var hoja = ss.getSheetByName("MENSAJES_PROGRAMADOS");
    if (!hoja) {
      hoja = ss.insertSheet("MENSAJES_PROGRAMADOS");
      hoja.appendRow(["ID", "Texto", "Intervalo_ciclos", "Destino", "Activo", "Ultimo_envio", "Creado"]);
    }
    hoja.appendRow([body.id, body.texto, body.intervalo_ciclos, body.destino, "si", "", body.creado]);
    return json({ ok: true });
  }

  // ── MENSAJES PROGRAMADOS — borrar ────────────────────────
  if (accion === "borrar_mensaje_programado") {
    var hoja = ss.getSheetByName("MENSAJES_PROGRAMADOS");
    if (!hoja) return json({ ok: false, error: "Hoja no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = rows.length - 1; i >= 1; i--) {
      if (String(rows[i][0]) === String(body.id)) {
        hoja.deleteRow(i + 1);
        break;
      }
    }
    return json({ ok: true });
  }

  // ── MENSAJES PROGRAMADOS — pausar / activar ───────────────
  if (accion === "toggle_mensaje_programado") {
    var hoja = ss.getSheetByName("MENSAJES_PROGRAMADOS");
    if (!hoja) return json({ ok: false, error: "Hoja no existe" });
    var rows = hoja.getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) {
      if (String(rows[i][0]) === String(body.id)) {
        hoja.getRange(i + 1, 5).setValue(body.activo);
        return json({ ok: true });
      }
    }
    return json({ ok: false, error: "Mensaje no encontrado" });
  }

  return json({ error: "accion no reconocida: " + accion });
}
