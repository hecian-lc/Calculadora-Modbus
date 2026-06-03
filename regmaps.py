import os
import sys
import logging
import xlrd
import openpyxl

log = logging.getLogger(__name__)

# Subcarpeta donde viven los mapas de registros de fabricante (.xls/.xlsx).
_DATA_SUBDIR = 'data'


def _data_search_roots(user_dir: str) -> list[str]:
    """Directorios candidatos donde buscar un archivo de datos, en orden de
    prioridad. Incluye tanto la raíz como su subcarpeta ``data/`` para que el
    lookup funcione tras la reorganización del repo y siga aceptando un
    override del usuario junto al .exe / CSV.
    """
    roots: list[str] = []
    if user_dir:
        roots += [user_dir, os.path.join(user_dir, _DATA_SUBDIR)]
    module_dir = os.path.dirname(os.path.abspath(__file__))
    roots += [module_dir, os.path.join(module_dir, _DATA_SUBDIR)]
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        roots += [sys._MEIPASS, os.path.join(sys._MEIPASS, _DATA_SUBDIR)]
    return roots


def _find_data_file(filename: str, user_dir: str) -> str:
    """Busca un archivo de datos en los directorios candidatos (raíz del
    proyecto/CSV, subcarpeta ``data/`` y bundle de PyInstaller). Devuelve la
    primera ruta existente; si no aparece, devuelve la primera candidata
    (fallará después con un error claro).
    """
    candidates = [os.path.join(root, filename) for root in _data_search_roots(user_dir)]
    for path in candidates:
        if os.path.exists(path):
            return path
    log.warning('Archivo de datos no encontrado: %s (buscado en %s)',
                filename, ', '.join(_data_search_roots(user_dir)))
    return candidates[0]

# {register_num: (name, unit, data_type, category)}
# register_num = PDU_address + 1  (1-indexed, igual que la nomenclatura Schneider/Modicon)
RegMap = dict[int, tuple[str, str, str, str]]

_DTYPE_MAP = {
    'FLOAT32':  'FLOAT32',
    '4Q_FP_PF': 'FLOAT32',   # factor de potencia 4-cuadrantes — mismo layout que FLOAT32
    'INT16':    'INT16',
    'INT16 ':   'INT16',      # PM2220 tiene espacio al final
    'INT16U':   'UINT16',
    'INT32':    'INT32',
    'INT32U':   'UINT32',
    'UINT16':   'UINT16',
    'UINT32':   'UINT32',
    'INT64':    'INT64',
    'DATETIME': 'DATETIME',
}

# filename → (xls_file, column_index_for_device)
# col_index es la columna que indica 'Y' si ese medidor tiene ese registro
_PM_SCHNEIDER = {
    # PM51xx / PM53xx
    'PM5340': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 8),
    'PM5341': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 8),
    'PM5320': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 7),
    'PM5330': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 6),
    'PM5331': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 6),
    'PM5310': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 5),
    'PM5110': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 4),
    'PM5111': ('PM51xx_PM53xx_PMC Register List_v2011_v2021_R01.xls', 4),
    # PM2xxx
    'PM2230': ('Public_PM2xxx_PMC Register List_v1001.xls', 8),
    'PM2220': ('Public_PM2xxx_PMC Register List_v1001.xls', 7),
    'PM2130': ('Public_PM2xxx_PMC Register List_v1001.xls', 6),
    'PM2120': ('Public_PM2xxx_PMC Register List_v1001.xls', 5),
}

_M4M_FILE = '9AKK107492A7627_en_F_M4M Modbus map.xlsx'


# ── Índices de columna (0-based) en la hoja 'Register List' de Schneider ──────
_SCH_COL_CAT1   = 1    # Categoría nivel 1
_SCH_COL_CAT2   = 2    # Categoría nivel 2 (más específica)
_SCH_COL_DESC   = 3    # Descripción / nombre del registro
_SCH_COL_REG    = 10   # Número de registro (1-indexed Modicon)
_SCH_COL_UNIT   = 11   # Unidad
_SCH_COL_DTYPE  = 13   # Tipo de dato


def load_schneider_xls(filepath: str, device_col: int) -> RegMap:
    wb = xlrd.open_workbook(filepath)
    ws = wb.sheet_by_name('Register List')
    reg_map: RegMap = {}
    cat1 = ''
    cat2 = ''

    for i in range(1, ws.nrows):
        row = [ws.cell_value(i, j) for j in range(ws.ncols)]

        if row[_SCH_COL_CAT1]:
            cat1 = str(row[_SCH_COL_CAT1]).strip()
        if row[_SCH_COL_CAT2]:
            cat2 = str(row[_SCH_COL_CAT2]).strip()

        description = str(row[_SCH_COL_DESC]).strip() if row[_SCH_COL_DESC] else ''
        applicable = str(row[device_col]).strip() if len(row) > device_col else ''
        reg_raw = row[_SCH_COL_REG]
        unit = str(row[_SCH_COL_UNIT]).strip() if row[_SCH_COL_UNIT] else ''
        dtype_raw = (str(row[_SCH_COL_DTYPE]).strip()
                     if len(row) > _SCH_COL_DTYPE and row[_SCH_COL_DTYPE] else '')

        if applicable not in ('Y', 'R') or not description or not isinstance(reg_raw, float):
            continue
        if dtype_raw not in _DTYPE_MAP:
            continue

        reg_num = int(reg_raw)
        dtype = _DTYPE_MAP[dtype_raw]
        category = cat2 if cat2 else cat1

        reg_map[reg_num] = (description, unit, dtype, category)

    return reg_map


def load_m4m_xlsx(filepath: str) -> RegMap:
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb['Mapping Table']
    reg_map: RegMap = {}

    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or row[0] is None:
            continue

        parameter = str(row[0]).strip()
        unit = str(row[1]).strip() if row[1] else ''
        dtype_raw = str(row[4]).strip() if row[4] else ''
        reg_dec = row[7]
        nr_regs = row[10]

        if not isinstance(reg_dec, (int, float)) or not parameter or not dtype_raw:
            continue

        reg_num = int(reg_dec)
        nr = int(nr_regs) if isinstance(nr_regs, (int, float)) else 1

        if dtype_raw == 'Float':
            dtype = 'FLOAT32'
        elif dtype_raw == 'Unsigned':
            dtype = 'UINT32' if nr >= 2 else 'UINT16'
        elif dtype_raw == 'Signed':
            dtype = 'INT32' if nr >= 2 else 'INT16'
        else:
            continue

        reg_map[reg_num] = (parameter, unit, dtype, 'Measurement')

    return reg_map


def load_wm20() -> RegMap:
    """Carlo Gavazzi WM20/WM30/WM40 — variables instantáneas (sección 2.5 del manual).

    Clave = Modicon - 300000 = PDU_address + 1.
    Todos los registros son FLOAT32 big-endian (IEEE 754), longitud 2 words.

    Rango típico de EKI-09 (Gavazzi WM20-96, addr=82, qty=60):
      PDU 82→141, keys 83→141, registros V L2-N .. Asymmetry L-L %.
    """
    # fmt: off
    return {
        81:  ('V L1-N',           'V',    'FLOAT32', 'Instantaneous'),
        83:  ('V L2-N',           'V',    'FLOAT32', 'Instantaneous'),
        85:  ('V L3-N',           'V',    'FLOAT32', 'Instantaneous'),
        87:  ('V L-N avg',        'V',    'FLOAT32', 'Instantaneous'),
        89:  ('V L1-L2',          'V',    'FLOAT32', 'Instantaneous'),
        91:  ('V L2-L3',          'V',    'FLOAT32', 'Instantaneous'),
        93:  ('V L3-L1',          'V',    'FLOAT32', 'Instantaneous'),
        95:  ('V L-L avg',        'V',    'FLOAT32', 'Instantaneous'),
        97:  ('A L1',             'A',    'FLOAT32', 'Instantaneous'),
        99:  ('A L2',             'A',    'FLOAT32', 'Instantaneous'),
        101: ('A L3',             'A',    'FLOAT32', 'Instantaneous'),
        103: ('A N',              'A',    'FLOAT32', 'Instantaneous'),
        105: ('W L1',             'W',    'FLOAT32', 'Instantaneous'),
        107: ('W L2',             'W',    'FLOAT32', 'Instantaneous'),
        109: ('W L3',             'W',    'FLOAT32', 'Instantaneous'),
        111: ('W total',          'W',    'FLOAT32', 'Instantaneous'),
        113: ('VA L1',            'VA',   'FLOAT32', 'Instantaneous'),
        115: ('VA L2',            'VA',   'FLOAT32', 'Instantaneous'),
        117: ('VA L3',            'VA',   'FLOAT32', 'Instantaneous'),
        119: ('VA total',         'VA',   'FLOAT32', 'Instantaneous'),
        121: ('VAR L1',           'var',  'FLOAT32', 'Instantaneous'),
        123: ('VAR L2',           'var',  'FLOAT32', 'Instantaneous'),
        125: ('VAR L3',           'var',  'FLOAT32', 'Instantaneous'),
        127: ('VAR total',        'var',  'FLOAT32', 'Instantaneous'),
        129: ('PF L1',            '-',    'FLOAT32', 'Instantaneous'),
        131: ('PF L2',            '-',    'FLOAT32', 'Instantaneous'),
        133: ('PF L3',            '-',    'FLOAT32', 'Instantaneous'),
        135: ('PF total',         '-',    'FLOAT32', 'Instantaneous'),
        137: ('Hz',               'Hz',   'FLOAT32', 'Instantaneous'),
        139: ('Asymmetry L-N %',  '%',    'FLOAT32', 'Instantaneous'),
        141: ('Asymmetry L-L %',  '%',    'FLOAT32', 'Instantaneous'),
        143: ('Phase sequence',   '-',    'FLOAT32', 'Instantaneous'),
    }
    # fmt: on


def load_siemens_7kt() -> RegMap:
    """Siemens SMART 7KT0310 Multifunction Meter — parámetros de medición continua.

    Clave = PDU_address + 1  (el manual muestra la dirección hex 0-indexed como PDU).
    Todos los registros son FLOAT32 big-endian, longitud 2 words.

    Rango típico de EKI-01 (Siemens 7KT, addr=0, qty=56):
      PDU 0→55, keys 1→55 (impares), registros V L1-N .. Average PF.
    """
    # fmt: off
    return {
        1:  ('Voltage V1N',          'V',    'FLOAT32', 'Voltage'),
        3:  ('Voltage V2N',          'V',    'FLOAT32', 'Voltage'),
        5:  ('Voltage V3N',          'V',    'FLOAT32', 'Voltage'),
        7:  ('Average Voltage LN',   'V',    'FLOAT32', 'Voltage'),
        9:  ('Voltage V12',          'V',    'FLOAT32', 'Voltage'),
        11: ('Voltage V23',          'V',    'FLOAT32', 'Voltage'),
        13: ('Voltage V31',          'V',    'FLOAT32', 'Voltage'),
        15: ('Average Voltage LL',   'V',    'FLOAT32', 'Voltage'),
        17: ('Current I1',           'A',    'FLOAT32', 'Current'),
        19: ('Current I2',           'A',    'FLOAT32', 'Current'),
        21: ('Current I3',           'A',    'FLOAT32', 'Current'),
        23: ('Average Current',      'A',    'FLOAT32', 'Current'),
        25: ('Active Power L1',      'kW',   'FLOAT32', 'Active Power'),
        27: ('Active Power L2',      'kW',   'FLOAT32', 'Active Power'),
        29: ('Active Power L3',      'kW',   'FLOAT32', 'Active Power'),
        31: ('Apparent Power L1',    'kVA',  'FLOAT32', 'Apparent Power'),
        33: ('Apparent Power L2',    'kVA',  'FLOAT32', 'Apparent Power'),
        35: ('Apparent Power L3',    'kVA',  'FLOAT32', 'Apparent Power'),
        37: ('Reactive Power L1',    'kVAr', 'FLOAT32', 'Reactive Power'),
        39: ('Reactive Power L2',    'kVAr', 'FLOAT32', 'Reactive Power'),
        41: ('Reactive Power L3',    'kVAr', 'FLOAT32', 'Reactive Power'),
        43: ('Total Active Power',   'kW',   'FLOAT32', 'Active Power'),
        45: ('Total Apparent Power', 'kVA',  'FLOAT32', 'Apparent Power'),
        47: ('Total Reactive Power', 'kVAr', 'FLOAT32', 'Reactive Power'),
        49: ('Power Factor L1',      '-',    'FLOAT32', 'Power Factor'),
        51: ('Power Factor L2',      '-',    'FLOAT32', 'Power Factor'),
        53: ('Power Factor L3',      '-',    'FLOAT32', 'Power Factor'),
        55: ('Average Power Factor', '-',    'FLOAT32', 'Power Factor'),
    }
    # fmt: on


def load_nsx_lv434000() -> RegMap:
    """Schneider NSX con módulo de comunicación LV434000 (Micrologic 6.3 E).

    Claves = registro 1-indexed (readreg del EKi config es 0-indexed PDU,
    por lo que key = readreg + 1).
    Todos los valores son FLOAT32 big-endian.

    Rangos capturados por EKI-13:
      I:    readreg 32028, qty 8  → 4 floats
      V+Fr: readreg 32056, qty 14 → 7 floats
      P:    readreg 32072, qty 8  → 4 floats
      Avg:  readreg 32132, qty 6  → 3 floats
      FP:   readreg 32206, qty 8  → 4 floats
    """
    # fmt: off
    return {
        # ── Corrientes (readreg 32028) ──────────────────────────────────────
        32029: ('I L1',          'A',   'FLOAT32', 'Current'),
        32031: ('I L2',          'A',   'FLOAT32', 'Current'),
        32033: ('I L3',          'A',   'FLOAT32', 'Current'),
        32035: ('I avg',         'A',   'FLOAT32', 'Current'),
        # ── Tensiones + Frecuencia (readreg 32056) ─────────────────────────
        32057: ('V L1-N',        'V',   'FLOAT32', 'Voltage'),
        32059: ('V L2-N',        'V',   'FLOAT32', 'Voltage'),
        32061: ('V L3-N',        'V',   'FLOAT32', 'Voltage'),
        32063: ('V L1-L2',       'V',   'FLOAT32', 'Voltage'),
        32065: ('V L2-L3',       'V',   'FLOAT32', 'Voltage'),
        32067: ('V L3-L1',       'V',   'FLOAT32', 'Voltage'),
        32069: ('Hz',            'Hz',  'FLOAT32', 'Frequency'),
        # ── Potencias activas (readreg 32072) ──────────────────────────────
        32073: ('W L1',          'W',   'FLOAT32', 'Active Power'),
        32075: ('W L2',          'W',   'FLOAT32', 'Active Power'),
        32077: ('W L3',          'W',   'FLOAT32', 'Active Power'),
        32079: ('W total',       'W',   'FLOAT32', 'Active Power'),
        # ── Promedios (readreg 32132) ──────────────────────────────────────
        32133: ('I avg',         'A',   'FLOAT32', 'Averages'),
        32135: ('V L-N avg',     'V',   'FLOAT32', 'Averages'),
        32137: ('V L-L avg',     'V',   'FLOAT32', 'Averages'),
        # ── Factores de potencia (readreg 32206) ───────────────────────────
        32207: ('PF L1',         '-',   'FLOAT32', 'Power Factor'),
        32209: ('PF L2',         '-',   'FLOAT32', 'Power Factor'),
        32211: ('PF L3',         '-',   'FLOAT32', 'Power Factor'),
        32213: ('PF total',      '-',   'FLOAT32', 'Power Factor'),
    }
    # fmt: on


def _normalize_meter_type(meter_type: str) -> str:
    """Normaliza el nombre del medidor para el lookup."""
    mt = meter_type.strip().upper().replace(' ', '')
    # Mapeo de nombres alternativos
    aliases = {
        'SIEMENS7KT': 'Siemens 7KT',
        '7KT': 'Siemens 7KT',
        'M4M': 'M4M',
        'ABB': 'M4M',
    }
    return aliases.get(mt, meter_type.strip())


def get_reg_map(meter_type: str, maps_dir: str) -> RegMap:
    """
    Carga y retorna el mapa de registros para el tipo de medidor indicado.
    Retorna dict vacío si el medidor no está soportado o el archivo no se encuentra.
    """
    mt_norm = _normalize_meter_type(meter_type)
    mt_upper = mt_norm.upper().replace(' ', '')

    # Gavazzi WM20 / WM30 / WM40 (hardcoded — datos del manual PDF)
    if 'WM20' in mt_upper or 'WM30' in mt_upper or 'WM40' in mt_upper or 'GAVAZZI' in mt_upper:
        return load_wm20()

    # Siemens 7KT (hardcoded — datos del manual 7KT0310)
    if 'SIEMENS' in mt_upper or '7KT' in mt_upper:
        return load_siemens_7kt()

    # Schneider NSX / Micrologic LV434000
    if 'NSX' in mt_upper or 'LV434000' in mt_upper or 'MICROLOGIC' in mt_upper:
        return load_nsx_lv434000()

    # Buscar en PM Schneider
    for key, (filename, device_col) in _PM_SCHNEIDER.items():
        if mt_upper == key.upper():
            filepath = _find_data_file(filename, maps_dir)
            if not os.path.exists(filepath):
                log.warning('Mapa de registros ausente para %s: %s', meter_type, filepath)
                return {}
            try:
                return load_schneider_xls(filepath, device_col)
            except Exception:
                log.exception('Error al cargar el mapa Schneider %s para %s',
                              filepath, meter_type)
                return {}

    # M4M / ABB
    if 'M4M' in mt_upper:
        filepath = _find_data_file(_M4M_FILE, maps_dir)
        if not os.path.exists(filepath):
            log.warning('Mapa de registros ausente para %s: %s', meter_type, filepath)
            return {}
        try:
            return load_m4m_xlsx(filepath)
        except Exception:
            log.exception('Error al cargar el mapa M4M %s para %s', filepath, meter_type)
            return {}

    log.warning('Tipo de medidor no soportado: %r', meter_type)
    return {}


def auto_detect_meter(start_addr: int, qty: int) -> str:
    """
    Intenta detectar el tipo de medidor por el rango de registros leídos.
    Retorna el tipo sugerido o cadena vacía.
    """
    if start_addr == 0:
        return 'Siemens 7KT'
    if 80 <= start_addr <= 90:
        return 'Gavazzi WM20-96'
    if 2999 <= start_addr <= 3200:
        # Rango ambiguo: PM5340 y PM2220 lo comparten. Se asume PM5340; defina
        # el medidor explícitamente en Ekis.xlsx para evitar la suposición.
        log.warning('auto_detect: rango %d ambiguo (PM5340/PM2220); se asume PM5340. '
                    'Especifique el medidor en Ekis.xlsx para evitarlo.', start_addr)
        return 'PM5340'
    if start_addr < 100:
        return 'Siemens 7KT'
    if start_addr >= 20000:
        return 'M4M'
    return ''
