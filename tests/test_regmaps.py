"""Tests de los mapas de registros y la resolución de archivos (regmaps.py)."""
import os

from regmaps import (
    get_reg_map, auto_detect_meter, load_wm20, load_siemens_7kt,
    _find_data_file, _PM_SCHNEIDER, _M4M_FILE,
)

ROOT = os.path.dirname(os.path.dirname(__file__))


# ─── Mapas hardcodeados ──────────────────────────────────────────────────────

def test_wm20_y_7kt_no_vacios_formato_correcto():
    for loader in (load_wm20, load_siemens_7kt):
        m = loader()
        assert m
        for reg, (name, unit, dtype, cat) in m.items():
            assert isinstance(reg, int)
            assert name and dtype == 'FLOAT32'


def test_get_reg_map_alias_resuelven():
    assert get_reg_map('Gavazzi WM20-96', ROOT)        # contiene 'WM20'
    assert get_reg_map('Siemens 7KT', ROOT)
    assert get_reg_map('7KT', ROOT)
    assert get_reg_map('NSX LV434000', ROOT)


def test_get_reg_map_desconocido_vacio():
    assert get_reg_map('MedidorInexistente', ROOT) == {}


# ─── auto_detect_meter ───────────────────────────────────────────────────────

def test_auto_detect_rangos_conocidos():
    assert auto_detect_meter(0, 56) == 'Siemens 7KT'
    assert auto_detect_meter(82, 60) == 'Gavazzi WM20-96'
    assert auto_detect_meter(2999, 50) == 'PM5340'
    assert auto_detect_meter(20000, 10) == 'M4M'
    assert auto_detect_meter(500, 2) == ''


# ─── Resolución de archivos de datos (movidos a data/) ───────────────────────

def test_archivos_de_datos_se_encuentran_en_data():
    # Tras la reorganización, los mapas .xls/.xlsx viven en data/.
    referenced = {fn for fn, _ in _PM_SCHNEIDER.values()} | {_M4M_FILE}
    for fn in referenced:
        path = _find_data_file(fn, ROOT)
        assert os.path.exists(path), f'No se encontró {fn} en {path}'


def test_get_reg_map_schneider_carga_desde_xls():
    # Si el .xls está presente, debe devolver un mapa no vacío.
    m = get_reg_map('PM5340', ROOT)
    assert m, 'PM5340 debería cargar registros desde el .xls en data/'
