"""
eki_config.py — Lectura centralizada de la configuración de dispositivos EKi
(``Ekis.xlsx``), compartida por la app de escritorio (``main.py``) y el
servidor web (``web_server.py``).

Antes, cada uno reimplementaba el mismo parseo con los mismos índices de
columna mágicos; un cambio en el formato del Excel obligaba a tocar dos
sitios. Ahora ambos consumen :func:`read_ekis_rows` y construyen lo que
necesitan con los adaptadores de abajo.

Formato de ``Ekis.xlsx`` (fila 1 = cabecera, datos desde la fila 2):

==========  ====================================================
Columna(0)  Contenido
==========  ====================================================
1           IP del gateway (se arrastra hacia abajo si está vacía)
4           Nombre del dispositivo
5           Tipo de medidor
9           Slave / unit id (se arrastra hacia abajo si está vacío)
11          Dirección (1-indexed Modicon en el documento)
12          Cantidad de registros 16-bit a leer
==========  ====================================================
"""
import os
import sys
from dataclasses import dataclass

import openpyxl

# ── Índices de columna (0-based) en Ekis.xlsx ────────────────────────────────
EKI_COL_IP    = 1
EKI_COL_NAME  = 4
EKI_COL_METER = 5
EKI_COL_SLAVE = 9
EKI_COL_ADDR  = 11
EKI_COL_QTY   = 12

DEFAULT_PORT = 502
EKIS_FILENAME = 'Ekis.xlsx'


@dataclass
class EkiRow:
    """Una fila de dispositivo de Ekis.xlsx, ya normalizada."""
    name: str
    meter: str
    ip: str
    slave: int
    addr: int      # dirección tal cual figura en el doc (1-indexed Modicon)
    qty: int


def find_ekis_path(base_dir: str) -> str:
    """Resuelve la ruta de ``Ekis.xlsx``: primero junto al .exe/script
    (``base_dir``), luego dentro del bundle de PyInstaller. Devuelve la mejor
    candidata aunque no exista (el llamador decide cómo reportarlo).
    """
    path = os.path.join(base_dir, EKIS_FILENAME)
    if os.path.exists(path):
        return path
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundled = os.path.join(sys._MEIPASS, EKIS_FILENAME)
        if os.path.exists(bundled):
            return bundled
    return path


def read_ekis_rows(path: str) -> list[EkiRow]:
    """Lee ``Ekis.xlsx`` y devuelve las filas de dispositivo normalizadas.

    IP y slave se arrastran hacia abajo (una IP/slave vacía hereda el valor de
    la fila anterior, como en la planilla original). Las filas sin nombre,
    medidor, dirección o cantidad válidos se descartan.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    rows: list[EkiRow] = []
    last_ip = ''
    last_slave = 1

    for raw in ws.iter_rows(min_row=2, values_only=True):
        if not raw or len(raw) <= EKI_COL_QTY:
            continue
        if not raw[EKI_COL_NAME] or not raw[EKI_COL_METER]:
            continue

        if raw[EKI_COL_IP]:
            last_ip = str(raw[EKI_COL_IP]).strip()
        if isinstance(raw[EKI_COL_SLAVE], (int, float)):
            last_slave = int(raw[EKI_COL_SLAVE])

        name  = str(raw[EKI_COL_NAME]).strip()
        meter = str(raw[EKI_COL_METER]).strip()
        addr  = raw[EKI_COL_ADDR]
        qty   = raw[EKI_COL_QTY]

        if not name or not isinstance(addr, (int, float)) or not isinstance(qty, (int, float)):
            continue

        rows.append(EkiRow(
            name=name,
            meter=meter,
            ip=last_ip,
            slave=last_slave,
            addr=int(addr),
            qty=int(qty),
        ))

    return rows


def build_decode_config(rows: list[EkiRow]) -> tuple[dict[str, str], dict[tuple, int]]:
    """Adaptador para la app de escritorio.

    Devuelve ``(config, qty_map)`` donde:
      - ``config``  = {nombre: tipo_de_medidor}
      - ``qty_map`` = {(nombre, pdu_addr): qty_esperada}

    La dirección se guarda con ambas convenciones de indexado de PDU para
    cubrir Schneider/Modicon (1-indexed: addr-1 = PDU) y Gavazzi/Siemens
    (0-indexed: addr = PDU).
    """
    config: dict[str, str] = {}
    qty_map: dict[tuple, int] = {}
    for r in rows:
        config[r.name] = r.meter
        qty_map[(r.name, r.addr - 1)] = r.qty   # Schneider / 1-indexed
        qty_map[(r.name, r.addr)]     = r.qty   # Gavazzi WM20 / Siemens 7KT / 0-indexed
    return config, qty_map


def build_devices(rows: list[EkiRow]) -> list[dict]:
    """Adaptador para el servidor web: lista de dispositivos lista para poll/JSON.
    Descarta filas sin IP (un dispositivo sin host no es poleable).
    """
    devices: list[dict] = []
    for r in rows:
        if not r.ip:
            continue
        devices.append({
            'key':   f'{r.name}|{r.addr}',
            'name':  r.name,
            'meter': r.meter,
            'host':  r.ip,
            'port':  DEFAULT_PORT,
            'slave': r.slave,
            'addr':  r.addr,
            'qty':   r.qty,
        })
    return devices
