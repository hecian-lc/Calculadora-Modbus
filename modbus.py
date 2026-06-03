import csv
import struct
import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class DecodedValue:
    timestamp: str
    eki_name: str
    trans_id: str
    register: int
    name: str
    category: str
    value: Optional[float]
    unit: str
    data_type: str
    raw_hex: str


_INVALID_F32 = {
    b'\xff\xc0\x00\x00',
    b'\x7f\xc0\x00\x00',
    b'\x80\x00\x80\x00',
}

DECODERS = {
    'FLOAT32': (4, lambda b: None if (b in _INVALID_F32 or math.isnan(v := struct.unpack('>f', b)[0])) else v),
    'INT16':   (2, lambda b: None if (v := struct.unpack('>h', b)[0]) == -32768 else float(v)),
    'UINT16':  (2, lambda b: None if (v := struct.unpack('>H', b)[0]) == 0xFFFF else float(v)),
    'INT32':   (4, lambda b: None if (v := struct.unpack('>i', b)[0]) == -2147483648 else float(v)),
    'UINT32':  (4, lambda b: None if (v := struct.unpack('>I', b)[0]) == 0xFFFFFFFF else float(v)),
    'INT64':   (8, lambda b: None if (v := struct.unpack('>q', b)[0]) == -2**63 else float(v)),
    'DATETIME': (8, lambda b: None),  # mostrar como raw hex
}


def word_swap(data: bytes) -> bytes:
    """Intercambia los dos words de 16 bits de cada bloque de 4 bytes:
    [W0, W1] → [W1, W0]. Necesario para dispositivos con endianness por word
    (p. ej. Carlo Gavazzi WM20-96 con endianswap=Word). Un bloque final de
    longitud distinta de 4 se deja intacto.
    """
    result = bytearray()
    for i in range(0, len(data), 4):
        chunk = data[i:i + 4]
        result.extend(chunk[2:4] + chunk[0:2] if len(chunk) == 4 else chunk)
    return bytes(result)


def parse_csv(filepath: str) -> list[dict]:
    rows = []
    with open(filepath, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def pair_frames(rows: list[dict]) -> list[tuple]:
    """
    Empareja tramas Send+Receive por (Name, Trans ID).
    Retorna lista de (timestamp, eki_name, trans_id, start_addr, data_bytes).
    """
    pending: dict[tuple, tuple] = {}
    paired = []

    for row in rows:
        direction = row.get('Send/Recv', '').strip()
        name = row.get('Name', '').strip()
        trans_id = row.get('Trans ID', '').strip()
        fc = row.get('FC', '').strip()
        data_str = row.get('Data', '').strip()
        timestamp = row.get('Time', '').strip()

        if fc != '3' or not data_str:
            continue

        try:
            data_bytes = bytes([int(b, 16) for b in data_str.split()])
        except ValueError:
            continue

        key = (name, trans_id)

        if direction == 'Send' and len(data_bytes) >= 6:
            pending[key] = (timestamp, name, trans_id, data_bytes)

        elif direction == 'Receive' and key in pending:
            _, send_name, send_tid, send_bytes = pending.pop(key)
            if len(data_bytes) >= 4:
                start_addr = (send_bytes[2] << 8) | send_bytes[3]
                byte_count = data_bytes[2]
                payload = data_bytes[3:3 + byte_count]
                if len(payload) == byte_count:
                    paired.append((timestamp, send_name, send_tid, start_addr, payload))

    return paired


def decode_frame(
    timestamp: str,
    eki_name: str,
    trans_id: str,
    start_addr: int,
    data_bytes: bytes,
    reg_map: dict,
    expected_qty: int = 0,
) -> list[DecodedValue]:
    """
    Decodifica los bytes de una trama Receive usando el mapa de registros.
    reg_map: {register_num: (name, unit, data_type, category)}

    Si expected_qty > 0, el rango esperado es [start_addr+1 .. start_addr+expected_qty]
    y se emiten entradas N/A para registros del mapa que no llegaron (trama truncada).
    Los registros recibidos pero sin entrada en el mapa se etiquetan como '(sin doc.)'.
    """
    # ── Paso 1: decodificar lo que hay en el payload ───────────────────────
    decoded: dict[int, DecodedValue] = {}
    offset = 0

    while offset < len(data_bytes):
        reg_num = start_addr + 1 + offset // 2

        if reg_num in reg_map:
            reg_name, unit, data_type, category = reg_map[reg_num]
        else:
            reg_name = '(sin doc.)'
            unit = ''
            data_type = 'FLOAT32'
            category = ''

        size, decoder = DECODERS.get(data_type, DECODERS['FLOAT32'])

        if offset + size > len(data_bytes):
            break

        chunk = data_bytes[offset:offset + size]
        raw_hex = ' '.join(f'{b:02X}' for b in chunk)

        decoded[reg_num] = DecodedValue(
            timestamp=timestamp,
            eki_name=eki_name,
            trans_id=trans_id,
            register=reg_num,
            name=reg_name,
            category=category,
            value=decoder(chunk),
            unit=unit,
            data_type=data_type,
            raw_hex=raw_hex,
        )
        offset += size

    # ── Paso 2: si no hay qty esperada, devolver lo decodificado tal cual ──
    if expected_qty <= 0:
        return sorted(decoded.values(), key=lambda d: d.register)

    # ── Paso 3: iterar el rango completo y rellenar N/A donde falte ────────
    results: list[DecodedValue] = []
    end_reg = start_addr + 1 + expected_qty     # exclusivo

    reg_num = start_addr + 1
    while reg_num < end_reg:
        if reg_num in decoded:
            dv = decoded[reg_num]
            results.append(dv)
            size = DECODERS.get(dv.data_type, DECODERS['FLOAT32'])[0]
            reg_num += max(1, size // 2)

        elif reg_num in reg_map:
            # Registro del mapa que no llegó (trama truncada)
            reg_name, unit, data_type, category = reg_map[reg_num]
            size = DECODERS.get(data_type, DECODERS['FLOAT32'])[0]
            results.append(DecodedValue(
                timestamp=timestamp, eki_name=eki_name, trans_id=trans_id,
                register=reg_num, name=reg_name, category=category,
                value=None, unit=unit, data_type=data_type, raw_hex='',
            ))
            reg_num += max(1, size // 2)

        else:
            # No está en el mapa ni en el payload: incluir como reservado
            results.append(DecodedValue(
                timestamp=timestamp, eki_name=eki_name, trans_id=trans_id,
                register=reg_num, name='(sin doc.)', category='',
                value=None, unit='', data_type='FLOAT32', raw_hex='',
            ))
            reg_num += 2  # asumir FLOAT32

    return results
