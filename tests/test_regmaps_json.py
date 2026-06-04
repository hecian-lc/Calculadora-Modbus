"""Verifica que el mapa unificado data/regmaps.json es idéntico, registro a
registro, a lo que devuelven los loaders originales. Si algún .xls o loader
cambia sin regenerar el JSON, estos tests fallan.
"""
import os

import pytest

from regmaps import (
    get_reg_map, _load_legacy, _load_regmaps_json, SUPPORTED_METERS,
)

ROOT = os.path.dirname(os.path.dirname(__file__))
DATA = os.path.join(ROOT, 'data')


def test_json_existe_y_cubre_todos_los_medidores():
    unified = _load_regmaps_json(DATA)
    assert unified, 'data/regmaps.json no se cargó'
    for meter in SUPPORTED_METERS:
        assert meter in unified, f'{meter} falta en regmaps.json (regenerar)'


@pytest.mark.parametrize('meter', SUPPORTED_METERS)
def test_json_coincide_con_loader_original(meter):
    legacy = _load_legacy(meter, DATA)
    from_json = get_reg_map(meter, DATA)   # fuente primaria = JSON
    assert from_json == legacy, f'Mapa de {meter} difiere del original'


def test_get_reg_map_resuelve_alias_via_json():
    # Aliases distintos deben mapear al mismo canónico servido por el JSON.
    assert get_reg_map('7KT', DATA) == get_reg_map('Siemens 7KT', DATA)
    assert get_reg_map('ABB M4M 30', DATA) == get_reg_map('M4M', DATA)
    assert get_reg_map('Gavazzi WM20-96', DATA)   # contiene 'WM20'


def test_get_reg_map_devuelve_copia():
    # Mutar el resultado no debe contaminar la caché del JSON.
    m1 = get_reg_map('Siemens 7KT', DATA)
    m1.clear()
    m2 = get_reg_map('Siemens 7KT', DATA)
    assert m2, 'la caché del JSON quedó contaminada por una mutación externa'
