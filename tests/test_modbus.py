"""Tests de la lógica pura de decodificación Modbus (modbus.py)."""
import os
import struct

import pytest

from modbus import (
    DECODERS, parse_csv, pair_frames, decode_frame, word_swap,
)

CAPTURE_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'capture.csv')


# ─── Decoders individuales y sus valores centinela ───────────────────────────

def _decode(dtype: str, raw: bytes):
    size, fn = DECODERS[dtype]
    assert len(raw) == size
    return fn(raw)


def test_float32_valido():
    raw = struct.pack('>f', 230.5)
    assert _decode('FLOAT32', raw) == pytest.approx(230.5)


def test_float32_centinelas_invalidos():
    # Centinelas "no disponible" usados por los medidores
    assert _decode('FLOAT32', b'\xff\xc0\x00\x00') is None
    assert _decode('FLOAT32', b'\x7f\xc0\x00\x00') is None
    assert _decode('FLOAT32', b'\x80\x00\x80\x00') is None


def test_float32_nan_es_none():
    nan = struct.pack('>f', float('nan'))
    assert _decode('FLOAT32', nan) is None


def test_int16_valido_y_centinela():
    assert _decode('INT16', struct.pack('>h', 123)) == 123.0
    assert _decode('INT16', struct.pack('>h', -32768)) is None   # centinela


def test_uint16_valido_y_centinela():
    assert _decode('UINT16', struct.pack('>H', 1000)) == 1000.0
    assert _decode('UINT16', b'\xff\xff') is None                # 0xFFFF centinela


def test_int32_uint32_int64_centinelas():
    assert _decode('INT32', struct.pack('>i', 5)) == 5.0
    assert _decode('INT32', struct.pack('>i', -2147483648)) is None
    assert _decode('UINT32', struct.pack('>I', 7)) == 7.0
    assert _decode('UINT32', b'\xff\xff\xff\xff') is None
    assert _decode('INT64', struct.pack('>q', 9)) == 9.0
    assert _decode('INT64', struct.pack('>q', -(2 ** 63))) is None


# ─── word_swap ───────────────────────────────────────────────────────────────

def test_word_swap_intercambia_words():
    assert word_swap(b'\x11\x22\x33\x44') == b'\x33\x44\x11\x22'


def test_word_swap_varios_bloques():
    data = b'\x11\x22\x33\x44\xAA\xBB\xCC\xDD'
    assert word_swap(data) == b'\x33\x44\x11\x22\xCC\xDD\xAA\xBB'


def test_word_swap_bloque_final_incompleto_intacto():
    # 6 bytes: el último bloque de 2 bytes se deja tal cual
    assert word_swap(b'\x11\x22\x33\x44\x55\x66') == b'\x33\x44\x11\x22\x55\x66'


def test_word_swap_revierte_a_float():
    original = struct.pack('>f', 415.0)
    swapped = word_swap(original)
    assert word_swap(swapped) == original   # involutivo sobre 4 bytes


# ─── decode_frame ────────────────────────────────────────────────────────────

def test_decode_frame_usa_reg_map():
    reg_map = {1: ('V L1-N', 'V', 'FLOAT32', 'Voltage')}
    payload = struct.pack('>f', 230.0)
    out = decode_frame('t', 'EKI', '1', 0, payload, reg_map)
    assert len(out) == 1
    dv = out[0]
    assert dv.register == 1
    assert dv.name == 'V L1-N'
    assert dv.unit == 'V'
    assert dv.value == pytest.approx(230.0)


def test_decode_frame_registro_sin_doc():
    payload = struct.pack('>f', 1.0)
    out = decode_frame('t', 'EKI', '1', 0, payload, {})
    assert out[0].name == '(sin doc.)'


def test_decode_frame_rellena_na_en_trama_truncada():
    # Mapa con 3 registros FLOAT32 (1,3,5) pero solo llega el primero.
    reg_map = {
        1: ('A', 'V', 'FLOAT32', 'c'),
        3: ('B', 'V', 'FLOAT32', 'c'),
        5: ('C', 'V', 'FLOAT32', 'c'),
    }
    payload = struct.pack('>f', 100.0)          # un solo float (regs 1)
    out = decode_frame('t', 'EKI', '1', 0, payload, reg_map, expected_qty=6)
    regs = {dv.register: dv for dv in out}
    assert regs[1].value == pytest.approx(100.0)
    assert regs[3].value is None                # truncado → N/A
    assert regs[5].value is None
    assert regs[3].raw_hex == ''


# ─── parse_csv + pair_frames sobre la captura real ───────────────────────────

def test_parse_csv_carga_filas():
    rows = parse_csv(CAPTURE_CSV)
    assert len(rows) > 0
    assert 'Send/Recv' in rows[0]
    assert 'Data' in rows[0]


def test_pair_frames_empareja_send_receive():
    rows = parse_csv(CAPTURE_CSV)
    pairs = pair_frames(rows)
    assert len(pairs) > 0
    ts, name, tid, start_addr, payload = pairs[0]
    # Primera pareja de capture.csv: Send addr 0x0C77 = 3191, byte_count 4
    assert start_addr == 0x0C77
    assert len(payload) == 4
    assert name == 'EKI-06-Torres'


def test_pair_frames_ignora_sin_pareja_y_fc_distinto():
    rows = [
        {'Send/Recv': 'Send', 'Name': 'X', 'Trans ID': '1', 'FC': '6',
         'Data': '01 06 00 01 00 02 AA BB', 'Time': 't'},      # FC != 3 → ignorado
        {'Send/Recv': 'Receive', 'Name': 'Y', 'Trans ID': '99', 'FC': '3',
         'Data': '01 03 02 00 01 AA BB', 'Time': 't'},          # Receive sin Send → ignorado
    ]
    assert pair_frames(rows) == []
