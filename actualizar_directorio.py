from datetime import datetime
import gspread

def actualizar_directorio_e_historial(gc, sheet_id):
    print("Actualizando DIRECTORIO e HISTORIAL...")
    ss = gc.open_by_key(sheet_id)

    hoja1    = ss.worksheet('Hoja 1')
    datos_h1 = hoja1.get_all_values()

    if len(datos_h1) < 2:
        print("  Hoja 1 vacía, omitiendo")
        return

    COL_SECCION = 5
    COL_JEFE    = 17

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
                info_base = dir_dict.get(sec, {})
                hist_dict[sec] = {
                    'Nombre_Seccion': info_base.get('Nombre_Seccion', ''),
                    'Jefe':           jefe,
                    'Gerencia':       info_base.get('Gerencia', ''),
                    'Direccion':      info_base.get('Direccion', ''),
                    'Ubicacion':      info_base.get('Ubicacion', ''),
                    'No_Caja':        info_base.get('No_Caja', ''),
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

    header_dir = ['Sección','Nombre Sección','Jefe de Departamento','Gerencia','Dirección','Ubicación','No. Caja','Fuente','Última Actualización']
    rows_dir   = [header_dir]
    for sec, info in sorted(dir_dict.items()):
        jefe_actual = hist_dict.get(sec, {}).get('Jefe', '') or info.get('Jefe', '')
        rows_dir.append([
            sec,
            info.get('Nombre_Seccion', ''),
            jefe_actual,
            info.get('Gerencia', ''),
            info.get('Direccion', ''),
            info.get('Ubicacion', ''),
            info.get('No_Caja', ''),
            'AUTO' if sec in hist_dict and hist_dict[sec]['Jefe'] != info.get('Jefe','') else 'DIRECTORIO',
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ])
    hoja_dir.clear()
    hoja_dir.update(rows_dir, "A1")
    print(f"  DIRECTORIO actualizado: {len(rows_dir)-1} secciones")
    print(f"  Cambios: {cambios} | Completadas: {completadas}")
    print("  Directorio e historial listos")
