from datetime import datetime
import gspread
from api_remisiones import COLUMNAS_REMISION

def actualizar_directorio_e_historial(gc, sheet_id):
    print("Actualizando DIRECTORIO e HISTORIAL...")
    ss = gc.open_by_key(sheet_id)

    hoja1    = ss.worksheet('Hoja 1')
    datos_h1 = hoja1.get_all_values()

    if len(datos_h1) < 2:
        print("  Hoja 1 vacía, omitiendo")
        return

    # Hoja 1 ahora se escribe con el header COLUMNAS_REMISION (ver sheets.py) —
    # se calculan los índices contra ese header en vez de tenerlos fijos, así
    # si el orden de columnas cambia esto no se vuelve a romper.
    COL_SECCION = COLUMNAS_REMISION.index("Seccion")
    COL_JEFE    = COLUMNAS_REMISION.index("NombreJefeDePiso")

    hoja_hist  = ss.worksheet('HISTORIAL')
    datos_hist = hoja_hist.get_all_values()
    hist_dict  = {}
    for row in datos_hist[1:]:
        if row and row[0]:
            sec = str(row[0]).strip()
            hist_dict[sec] = {
                'Nombre_Seccion': row[1] if len(row) > 1 else '',
                'Jefe':           row[2] if len(row) > 2 else '',
                'Gerencia':       row[3] if len(row) > 3 else '',
                'Direccion':      row[4] if len(row) > 4 else '',
                'Ubicacion':      row[5] if len(row) > 5 else '',
                'No_Caja':        row[6] if len(row) > 6 else '',
                'Fecha':          row[7] if len(row) > 7 else '',
            }

    hoja_dir  = ss.worksheet('DIRECTORIO')
    datos_dir = hoja_dir.get_all_values()
    dir_dict  = {}
    for row in datos_dir[1:]:
        if row and row[0]:
            sec = str(row[0]).strip()
            dir_dict[sec] = {
                'Nombre_Seccion': row[1] if len(row) > 1 else '',
                'Jefe':           row[2] if len(row) > 2 else '',
                'Gerencia':       row[3] if len(row) > 3 else '',
                'Direccion':      row[4] if len(row) > 4 else '',
                'Ubicacion':      row[5] if len(row) > 5 else '',
                'No_Caja':        row[6] if len(row) > 6 else '',
            }

    cambios       = 0
    updates       = []
    completadas   = 0

    for row in datos_h1[1:]:
        if not row or len(row) <= COL_SECCION:
            continue
        sec  = str(row[COL_SECCION]).strip().replace('.0', '')
        jefe = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ''
        if not sec or sec in ('', 'nan'):
            continue
        jefe_valido = jefe and jefe not in ('', 'nan', 'Sin Asignar', 'UNASSIGNED')
        if jefe_valido:
            if sec not in hist_dict or hist_dict[sec]['Jefe'] != jefe:
                # Preservar todos los campos existentes; solo actualizar Jefe y Fecha.
                # Usar dir_dict como respaldo de campos que hist_dict no tenga,
                # pero NUNCA sobreescribir un valor existente con uno vacío.
                prev     = hist_dict.get(sec, {})
                info_base = dir_dict.get(sec, {})
                def _mejor(campo):
                    return prev.get(campo, '') or info_base.get(campo, '')
                hist_dict[sec] = {
                    'Nombre_Seccion': _mejor('Nombre_Seccion'),
                    'Jefe':           jefe,
                    'Gerencia':       _mejor('Gerencia'),
                    'Direccion':      _mejor('Direccion'),
                    'Ubicacion':      _mejor('Ubicacion'),
                    'No_Caja':        _mejor('No_Caja'),
                    'Fecha':          datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                cambios += 1

    for i, row in enumerate(datos_h1[1:], start=2):
        if not row or len(row) <= COL_SECCION:
            continue
        sec  = str(row[COL_SECCION]).strip().replace('.0', '')
        jefe = str(row[COL_JEFE]).strip() if len(row) > COL_JEFE else ''
        jefe_vacio = not jefe or jefe in ('', 'nan', 'Sin Asignar', 'UNASSIGNED')
        if jefe_vacio and sec in hist_dict and hist_dict[sec]['Jefe']:
            updates.append({'range': f'R{i}', 'values': [[hist_dict[sec]['Jefe']]]})
            completadas += 1

    if updates:
        hoja1.batch_update(updates)
        print(f"  Auto-completadas {completadas} celdas de jefe en Hoja 1")

    if cambios > 0 or completadas > 0:
        header_hist = ['Sección','Nombre Sección','Jefe','Gerencia','Dirección','Ubicación','No. Caja','Fecha Registro']
        rows_hist   = [header_hist]
        for sec, info in sorted(hist_dict.items()):
            rows_hist.append([
                sec,
                info.get('Nombre_Seccion', ''),
                info.get('Jefe', ''),
                info.get('Gerencia', ''),
                info.get('Direccion', ''),
                info.get('Ubicacion', ''),
                info.get('No_Caja', ''),
                info.get('Fecha', datetime.now().strftime("%Y-%m-%d")),
            ])
        hoja_hist.clear()
        hoja_hist.update(rows_hist, "A1")
        print(f"  HISTORIAL actualizado: {len(rows_hist)-1} secciones")

    # Guard: si dir_dict quedó vacío (DIRECTORIO estaba mal o vacío),
    # reconstruirlo desde HISTORIAL para no perder los datos.
    if not dir_dict and hist_dict:
        print("  ADVERTENCIA: DIRECTORIO vacío — reconstruyendo desde HISTORIAL")
        for sec, h in hist_dict.items():
            dir_dict[sec] = {
                'Nombre_Seccion': h.get('Nombre_Seccion', ''),
                'Jefe':           h.get('Jefe', ''),
                'Gerencia':       h.get('Gerencia', ''),
                'Direccion':      h.get('Direccion', ''),
                'Ubicacion':      h.get('Ubicacion', ''),
                'No_Caja':        h.get('No_Caja', ''),
            }

    header_dir = ['Sección','Nombre Sección','Jefe de Departamento','Gerencia','Dirección','Ubicación','No. Caja','Fuente','Última Actualización']
    rows_dir   = [header_dir]
    for sec, info in sorted(dir_dict.items()):
        h = hist_dict.get(sec, {})
        # Para cada campo: preferir DIRECTORIO; si está vacío, usar HISTORIAL.
        # Así nunca se pierde un dato que exista en cualquiera de las dos fuentes.
        def _f(campo):
            return info.get(campo, '') or h.get(campo, '')
        rows_dir.append([
            sec,
            _f('Nombre_Seccion'),
            h.get('Jefe', '') or info.get('Jefe', ''),
            _f('Gerencia'),
            _f('Direccion'),
            _f('Ubicacion'),
            _f('No_Caja'),
            'AUTO' if sec in hist_dict and hist_dict[sec]['Jefe'] != info.get('Jefe','') else 'DIRECTORIO',
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ])

    # Solo escribir si hay datos reales; nunca borrar con dir_dict vacío.
    if len(rows_dir) > 1:
        hoja_dir.clear()
        hoja_dir.update(rows_dir, "A1")
        print(f"  DIRECTORIO actualizado: {len(rows_dir)-1} secciones")
    else:
        print("  ADVERTENCIA: dir_dict vacío, se omite la escritura de DIRECTORIO para no perder datos")
    print(f"  Cambios: {cambios} | Completadas: {completadas}")
    print("  Directorio e historial listos")
