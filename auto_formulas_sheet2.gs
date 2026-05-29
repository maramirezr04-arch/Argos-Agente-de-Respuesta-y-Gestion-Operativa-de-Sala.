// ══════════════════════════════════════════════════════════════
//  Auto Fórmulas — Sheet 2 (hoja de datos del OMS)
//  Versión: 1.1.2
//
//  CÓMO USAR:
//  1. Abre el Sheet 2 (la hoja donde el bot pega los datos crudos)
//  2. Extensiones → Apps Script
//  3. Pega este archivo, guarda
//  4. Ejecuta crearTrigger() UNA sola vez para activar el trigger
//
//  Después de eso corre solo cada minuto.
//  Para desactivar: Extensiones → Apps Script → Activadores → borrar
// ══════════════════════════════════════════════════════════════

function rellenarFormulas() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

  // Detecta automáticamente hasta dónde llega col V
  const colV = sheet.getRange("V:V").getValues();
  let lastRow = 1;
  for (let i = colV.length - 1; i >= 1; i--) {
    if (colV[i][0] !== "") { lastRow = i + 1; break; }
  }

  if (lastRow <= 1) return;

  // A=1, F=6, I=9, J=10, K=11, U=21
  const formulaCols = [1, 6, 9, 10, 11, 21];

  formulaCols.forEach(col => {
    const templateCell = sheet.getRange(2, col);
    const formula = templateCell.getFormula();
    if (!formula) return;

    const targetRange = sheet.getRange(2, col, lastRow - 1, 1);
    templateCell.copyTo(
      targetRange,
      SpreadsheetApp.CopyPasteType.PASTE_FORMULA,
      false
    );
  });

  Logger.log("Fórmulas aplicadas hasta fila " + lastRow);
}

function crearTrigger() {
  // Borra triggers anteriores para no duplicar
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));

  // Corre rellenarFormulas cada 1 minuto automáticamente
  ScriptApp.newTrigger("rellenarFormulas")
    .timeBased()
    .everyMinutes(1)
    .create();
}
