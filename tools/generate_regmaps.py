"""
Genera el mapa de registros unificado ``data/regmaps.json`` a partir de los
loaders originales (dicts hardcodeados + .xls/.xlsx de fabricante).

El JSON es la fuente primaria en runtime (ver regmaps.get_reg_map). Regenéralo
cuando cambie un loader o un .xls de fabricante:

    python tools/generate_regmaps.py

Los datos salen de los mismos loaders que usaba el código antes, así que el
resultado es idéntico bit a bit a la lógica previa (sin transcripción manual).
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from regmaps import _load_legacy, SUPPORTED_METERS   # noqa: E402

DATA_DIR = os.path.join(ROOT, 'data')
OUT_PATH = os.path.join(DATA_DIR, 'regmaps.json')


def build() -> dict:
    out: dict = {}
    for meter in SUPPORTED_METERS:
        regs = _load_legacy(meter, DATA_DIR)
        if not regs:
            print(f'  ¡AVISO! mapa vacío para {meter!r} (¿falta el .xls?)')
            continue
        out[meter] = {str(reg): list(entry) for reg, entry in sorted(regs.items())}
        print(f'  {meter:<16} {len(regs):>4} registros')
    return out


def main() -> int:
    print(f'Generando {OUT_PATH} ...')
    out = build()
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write('\n')
    total = sum(len(v) for v in out.values())
    print(f'OK: {len(out)} medidores, {total} registros -> {OUT_PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
