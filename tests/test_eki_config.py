"""Tests del parseo centralizado de Ekis.xlsx (eki_config.py)."""
import os

import pytest

from eki_config import (
    EkiRow, read_ekis_rows, build_decode_config, build_devices,
)

EKIS_XLSX = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'Ekis.xlsx')


def test_read_ekis_rows_de_archivo_real():
    if not os.path.exists(EKIS_XLSX):
        pytest.skip('Ekis.xlsx no disponible')
    rows = read_ekis_rows(EKIS_XLSX)
    assert len(rows) > 0
    for r in rows:
        assert isinstance(r, EkiRow)
        assert r.name
        assert isinstance(r.addr, int)
        assert isinstance(r.qty, int)


def test_build_decode_config_guarda_ambas_convenciones():
    rows = [EkiRow(name='EKI-1', meter='PM5340', ip='192.168.1.1',
                   slave=1, addr=3000, qty=60)]
    config, qty_map = build_decode_config(rows)
    assert config == {'EKI-1': 'PM5340'}
    # 1-indexed (PDU 2999) y 0-indexed (PDU 3000) ambos presentes
    assert qty_map[('EKI-1', 2999)] == 60
    assert qty_map[('EKI-1', 3000)] == 60


def test_build_devices_descarta_sin_ip():
    rows = [
        EkiRow(name='conIP', meter='M', ip='10.0.0.5', slave=2, addr=82, qty=60),
        EkiRow(name='sinIP', meter='M', ip='', slave=1, addr=0, qty=56),
    ]
    devs = build_devices(rows)
    assert len(devs) == 1
    d = devs[0]
    assert d['name'] == 'conIP'
    assert d['host'] == '10.0.0.5'
    assert d['port'] == 502
    assert d['slave'] == 2
    assert d['key'] == 'conIP|82'
