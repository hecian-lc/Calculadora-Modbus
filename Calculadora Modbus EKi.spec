# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['web_server.py'],
    pathex=[],
    binaries=[],
    datas=[('data/regmaps.json', 'data'), ('Ekis.xlsx', '.'), ('EKIconfig', 'EKIconfig')],
    hiddenimports=['flask', 'flask.json', 'werkzeug', 'werkzeug.serving', 'pymodbus', 'pymodbus.client', 'pymodbus.client.tcp', 'pymodbus.transport', 'pymodbus.framer', 'pymodbus.framer.socket', 'pymodbus.pdu', 'openpyxl', 'xlrd', 'jinja2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Calculadora Modbus EKi',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
