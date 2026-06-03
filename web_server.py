"""
web_server.py  —  Dashboard + Configuración de dispositivos EKi en tiempo real.
Acceder desde:  http://localhost:8080

Al arrancar:
  - Si existe web_config.json  → carga desde ahí.
  - Si no existe               → importa Ekis.xlsx y crea web_config.json.
"""
import sys, os, json, struct, time, threading, webbrowser, csv as csv_mod
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, request, Response
from pymodbus.client import ModbusTcpClient
from modbus import decode_frame, word_swap
from regmaps import get_reg_map
from eki_config import read_ekis_rows, build_devices, find_ekis_path

# Cuando está compilado con PyInstaller, los archivos de usuario
# (Ekis.xlsx, web_config.json, mapas XLS) están junto al .exe
BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
CONFIG_FILE = BASE / 'web_config.json'
app         = Flask(__name__)

POLL_INTERVAL = 30   # segundos entre auto-polls de fondo

_data:         dict = {}
_data_lock         = threading.Lock()
_devices:      list = []
_devices_lock      = threading.Lock()
_reg_map_cache:dict = {}


# ══════════════════════════════════════════════════════════════════════════════
#  Config: carga / guarda
# ══════════════════════════════════════════════════════════════════════════════
def _import_ekis_xlsx() -> list[dict]:
    """Lee Ekis.xlsx y devuelve lista de dicts de dispositivos.
    El parseo vive en eki_config (compartido con la app de escritorio).
    """
    ekis_path = find_ekis_path(str(BASE))
    return build_devices(read_ekis_rows(ekis_path))


def _save_config(devices: list[dict]) -> None:
    exportable = [{k: d[k] for k in ('name','meter','host','port','slave','addr','qty')} for d in devices]
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(exportable, f, ensure_ascii=False, indent=2)


# ── Importar desde carpeta EKIconfig/ ──────────────────────────────────────

# IP del EKi (gateway serial) por número de EKi
_EKI_SERIAL_IPS: dict[str, str] = {
    '2':  '192.168.1.2',
    '6':  '192.168.1.6',
    '8':  '192.168.1.8',
    '9':  '192.168.1.9',
    '12': '192.168.1.12',
    '13': '192.168.1.13',
}

def _meter_from_name(name: str) -> str:
    """Detecta el tipo de medidor por el prefijo del nombre del comando."""
    n = name.upper()
    if 'EKI-2-TS' in n or 'EKI-2-TN' in n: return 'PM2220'
    if 'EKI-9'       in n:                   return 'Gavazzi WM20-96'
    if 'EKI-8-ISLA'  in n:                   return 'ABB M4M 30'
    if 'EKI-13'      in n:                   return 'NSX LV434000'
    return 'PM5340'   # EKI-3,4,5,6,8-TT,10,11,12 y cualquier otro


def _import_ekiconfig_csv(csv_folder: str) -> list[dict]:
    """Lee todos los modbus_commands_EKI*.csv de una carpeta y devuelve
    lista de dispositivos con los campos correctos (FC, endianswap, IP real).
    """
    folder = Path(csv_folder)
    if not folder.exists():
        return []

    devices = []
    for csv_file in sorted(folder.glob('modbus_commands_EKI*.csv')):
        # Número de EKi desde el nombre del archivo
        eki_num = csv_file.stem.replace('modbus_commands_EKI', '')
        eki_ip  = _EKI_SERIAL_IPS.get(eki_num, f'192.168.1.{eki_num}')

        with open(csv_file, newline='', encoding='utf-8') as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                name     = row.get('name',        '').strip()
                mode     = row.get('mode',         '').strip()
                ip_tcp   = row.get('ip',           '').strip()
                port_s   = row.get('port',       '502').strip()
                unitid   = row.get('unitid',       '1').strip()
                funccode = row.get('funccode',     '3').strip()
                readreg  = row.get('readreg',      '0').strip()
                readqty  = row.get('readqty',      '0').strip()
                endian   = row.get('endianswap', 'None').strip()

                if not name or not readqty or int(readqty) == 0:
                    continue

                # Host: TCP Master usa la IP del CSV; Serial Master usa la IP del EKi
                if mode == 'TCP Master' and ip_tcp:
                    host = ip_tcp
                    port = int(port_s) if port_s else 502
                else:
                    host = eki_ip
                    port = 502

                addr = int(readreg)
                qty  = int(readqty)
                fc   = int(funccode) if funccode else 3

                devices.append({
                    'key':    f"{name}|{addr}",
                    'name':   name,
                    'meter':  _meter_from_name(name),
                    'host':   host,
                    'port':   port,
                    'slave':  int(unitid) if unitid else 1,
                    'addr':   addr,
                    'qty':    qty,
                    'fc':     fc,
                    'endian': endian,   # 'None' o 'Word'
                })

    return devices


def _load_config() -> list[dict]:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        devices = []
        for d in raw:
            d.setdefault('port',  502)
            d.setdefault('slave', 1)
            d['key'] = f"{d['name']}|{d['addr']}"
            devices.append(d)
        return devices
    else:
        devices = _import_ekis_xlsx()
        _save_config(devices)
        print(f"  web_config.json creado con {len(devices)} dispositivos importados de Ekis.xlsx")
        return devices


def _get_devices() -> list[dict]:
    with _devices_lock:
        return list(_devices)


def _set_devices(devs: list[dict]) -> None:
    global _devices
    with _devices_lock:
        _devices = devs


# ══════════════════════════════════════════════════════════════════════════════
#  Mapas de registros (cacheados)
# ══════════════════════════════════════════════════════════════════════════════
def _get_reg_map(meter: str) -> dict:
    if meter not in _reg_map_cache:
        _reg_map_cache[meter] = get_reg_map(meter, str(BASE))
    return _reg_map_cache[meter]


def _pdu_start(addr_cfg: int, reg_map: dict) -> int:
    if not reg_map:
        return max(0, addr_cfg - 1)
    keys = set(reg_map.keys())
    if (addr_cfg + 1) in keys and addr_cfg not in keys:
        return addr_cfg          # 0-indexed (Gavazzi / Siemens)
    return addr_cfg - 1          # 1-indexed Schneider (3000 → PDU 2999)


# ══════════════════════════════════════════════════════════════════════════════
#  Polling Modbus TCP
# ══════════════════════════════════════════════════════════════════════════════
def _poll_one(dev: dict) -> dict:
    reg_map = _get_reg_map(dev['meter'])
    pdu     = _pdu_start(dev['addr'], reg_map)
    qty     = dev['qty']
    fc      = dev.get('fc', 3)
    endian  = dev.get('endian', 'None')
    ts      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    client  = ModbusTcpClient(host=dev['host'], port=dev['port'], timeout=5)
    try:
        if not client.connect():
            return {'status':'no_conn','error':f"Sin conexión a {dev['host']}:{dev['port']}","ts":ts}
        # FC03 = Read Holding Registers / FC04 = Read Input Registers
        if fc == 4:
            rr = client.read_input_registers(address=pdu, count=qty, slave=dev['slave'])
        else:
            rr = client.read_holding_registers(address=pdu, count=qty, slave=dev['slave'])
        if rr.isError():
            return {'status':'modbus_err','error':str(rr),'ts':ts}
        raw = b''.join(struct.pack('>H', r) for r in rr.registers)
        if endian == 'Word':
            raw = word_swap(raw)
        decoded = decode_frame(ts, dev['name'], 'live', pdu, raw, reg_map, expected_qty=qty)
        regs    = [{
            'reg':   dv.register,
            'name':  dv.name,
            'cat':   dv.category,
            'val':   round(dv.value, 6) if dv.value is not None else None,
            'unit':  dv.unit,
            'hex':   dv.raw_hex,
            'valid': dv.value is not None,
            'undoc': dv.name == '(sin doc.)',
        } for dv in decoded]
        n_doc   = sum(1 for r in regs if not r['undoc'])
        n_valid = sum(1 for r in regs if r['valid'] and not r['undoc'])
        return {'status':'ok','regs':regs,'qty_recv':len(rr.registers),
                'qty_exp':qty,'n_valid':n_valid,'n_doc':n_doc,'ts':ts}
    except Exception as exc:
        return {'status':'error','error':str(exc),'ts':ts}
    finally:
        try: client.close()
        except: pass


def _poll_all():
    devs = _get_devices()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_poll_one, d): d for d in devs}
        for fut in as_completed(futures):
            dev = futures[fut]
            try:    res = fut.result()
            except Exception as e:
                res = {'status':'error','error':str(e),'ts':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            meta = {k: dev[k] for k in ('name','meter','host','addr','qty')}
            with _data_lock:
                _data[dev['key']] = {**res, 'meta': meta}


def _bg_loop():
    while True:
        _poll_all()
        time.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  API REST
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/data')
def api_data():
    with _data_lock:
        return jsonify(dict(_data))


@app.route('/api/poll')
def api_poll_all():
    _poll_all()
    with _data_lock:
        return jsonify(dict(_data))


@app.route('/api/poll/<path:key>')
def api_poll_one(key):
    dev = next((d for d in _get_devices() if d['key'] == key), None)
    if not dev:
        return jsonify({'error':'Dispositivo no encontrado'}), 404
    res  = _poll_one(dev)
    meta = {k: dev[k] for k in ('name','meter','host','addr','qty')}
    with _data_lock:
        _data[key] = {**res, 'meta': meta}
    return jsonify(res)


# ── Config endpoints ──────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify(_get_devices())


@app.route('/api/config', methods=['POST'])
def api_save_config():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({'error':'Se esperaba una lista de dispositivos'}), 400
    required = {'name','meter','host','addr','qty'}
    for d in data:
        if not required.issubset(d.keys()):
            return jsonify({'error':f"Campos obligatorios: {required}"}), 400
    # Normalizar y asignar clave
    devices = []
    for d in data:
        dev = {
            'key':   f"{d['name']}|{int(d['addr'])}",
            'name':  str(d['name']).strip(),
            'meter': str(d['meter']).strip(),
            'host':  str(d['host']).strip(),
            'port':  int(d.get('port',  502)),
            'slave': int(d.get('slave', 1)),
            'addr':  int(d['addr']),
            'qty':   int(d['qty']),
        }
        devices.append(dev)
    _save_config(devices)
    _set_devices(devices)
    # Limpiar datos de dispositivos eliminados
    valid_keys = {d['key'] for d in devices}
    with _data_lock:
        for k in list(_data.keys()):
            if k not in valid_keys:
                del _data[k]
    return jsonify({'status':'ok','count':len(devices)})


@app.route('/api/config/import', methods=['POST'])
def api_import_xlsx():
    """Re-importa desde Ekis.xlsx y reemplaza la configuración actual."""
    try:
        devices = _import_ekis_xlsx()
        _save_config(devices)
        _set_devices(devices)
        with _data_lock:
            _data.clear()
        return jsonify({'status':'ok','count':len(devices),
                        'devices':[{k:d[k] for k in ('name','meter','host','addr','qty')} for d in devices]})
    except Exception as exc:
        return jsonify({'error':str(exc)}), 500


@app.route('/api/config/import_csv', methods=['POST'])
def api_import_csv():
    """Importa configuración desde la carpeta EKIconfig/ (CSV del EKi Studio).
    Detecta automáticamente FC, endianswap, IPs y tipos de medidor.
    """
    # Buscar carpeta EKIconfig junto al exe/script o en bundle
    candidates = [BASE / 'EKIconfig']
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        candidates.append(Path(sys._MEIPASS) / 'EKIconfig')
    csv_folder = next((p for p in candidates if p.exists()), None)

    if csv_folder is None:
        return jsonify({'error': 'Carpeta EKIconfig/ no encontrada junto al ejecutable'}), 404
    try:
        devices = _import_ekiconfig_csv(str(csv_folder))
        if not devices:
            return jsonify({'error': 'No se encontraron comandos en los CSV'}), 400
        _save_config(devices)
        _set_devices(devices)
        with _data_lock:
            _data.clear()
        return jsonify({'status':'ok','count':len(devices),
                        'csv_folder': str(csv_folder)})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/')
def index():
    devs_json = json.dumps([{k: d[k] for k in ('key','name','meter','host','port','slave','addr','qty')}
                             for d in _get_devices()])
    with _data_lock:
        has_data = len(_data) > 0
    return (_HTML
            .replace('"__DEVICES__"', devs_json)
            .replace('"__HAS_DATA__"', 'true' if has_data else 'false'))


# ══════════════════════════════════════════════════════════════════════════════
#  Dashboard HTML
# ══════════════════════════════════════════════════════════════════════════════
_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calculadora Modbus EKi</title>
<style>
:root{--ok:#2e7d32;--warn:#e65100;--err:#c62828;--na:#9e9e9e;--acc:#1565c0;--bg:#eef1f5;--panel-w:680px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:var(--bg);color:#1a1a2e;font-size:13px}

/* ── Header ── */
header{background:#2E4057;color:#fff;padding:11px 18px;display:flex;align-items:center;gap:10px;
  position:sticky;top:0;z-index:200;box-shadow:0 2px 8px rgba(0,0,0,.35)}
header h1{font-size:1rem;font-weight:700;flex:1}
.hctrl{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.btn{padding:5px 13px;border:none;border-radius:5px;cursor:pointer;font-size:.78rem;font-weight:600;transition:background .15s,transform .1s;white-space:nowrap}
.btn:active{transform:scale(.97)}
.btn-pri{background:#1565c0;color:#fff}.btn-pri:hover{background:#0d47a1}
.btn-cfg{background:#455a75;color:#fff;border:1px solid #5a7a9a}.btn-cfg:hover{background:#374e67}
.btn-sml{padding:3px 9px;font-size:.72rem}
.btn-outline{background:none;border:1px solid rgba(255,255,255,.4);color:#fff}.btn-outline:hover{background:rgba(255,255,255,.1)}
.btn-danger{background:#c62828;color:#fff}.btn-danger:hover{background:#b71c1c}
.btn-success{background:#2e7d32;color:#fff}.btn-success:hover{background:#1b5e20}
select{border:1px solid #ccc;border-radius:4px;padding:4px 8px;font-size:.78rem;background:#fff}
.ts-lbl{font-size:.68rem;color:rgba(255,255,255,.55)}

/* ── Main ── */
main{padding:14px 18px;max-width:1500px;margin:0 auto;transition:margin-right .3s}
main.shifted{margin-right:var(--panel-w)}
.sum-bar{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.sum-chip{background:#fff;border-radius:7px;padding:9px 16px;box-shadow:0 1px 4px rgba(0,0,0,.08);display:flex;flex-direction:column;gap:3px}
.sum-chip b{font-size:1.4rem;line-height:1}
.fbar{display:flex;gap:9px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.fbar label{font-size:.78rem;font-weight:600;color:#555}
.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:13px}

/* ── Cards ── */
.card{background:#fff;border-radius:8px;box-shadow:0 1px 5px rgba(0,0,0,.1);overflow:hidden;border-top:4px solid #ccc}
.card.ok{border-top-color:var(--ok)}.card.warn{border-top-color:var(--warn)}
.card.error,.card.no_conn,.card.modbus_err{border-top-color:var(--err)}.card.loading{opacity:.7}
.chdr{padding:8px 12px;display:flex;align-items:center;gap:7px;background:#f8f9fb;border-bottom:1px solid #eee}
.sdot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.sdot.ok{background:var(--ok)}.sdot.warn{background:var(--warn)}
.sdot.error,.sdot.no_conn,.sdot.modbus_err{background:var(--err)}.sdot.loading{background:#aaa}
.ctitle{font-weight:700;font-size:.86rem}.cmeta{font-size:.7rem;color:#888;margin-top:1px}
.cinfo{padding:4px 12px;font-size:.71rem;color:#666;display:flex;gap:12px;align-items:center;border-bottom:1px solid #f2f2f2;flex-wrap:wrap;min-height:26px}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.69rem;font-weight:700}
.b-ok{background:#e8f5e9;color:#2e7d32}.b-warn{background:#fff3e0;color:#e65100}.b-err{background:#ffebee;color:#c62828}
.tscroll{max-height:340px;overflow-y:auto}
table.rtbl{width:100%;border-collapse:collapse;font-size:.74rem}
table.rtbl thead th{background:#566573;color:#fff;padding:5px 8px;text-align:left;font-weight:600;position:sticky;top:0}
table.rtbl tbody td{padding:4px 8px;border-bottom:1px solid #f2f2f2}
table.rtbl tbody tr:last-child td{border-bottom:none}
table.rtbl tbody tr:hover td{background:#f4f7fb}
tr.na td{color:var(--na)} tr.undoc td{color:#ccc;font-style:italic}
.rnum{color:#aaa;font-size:.69rem}.vval{font-weight:700;text-align:right}.uunit{color:#999}.hhex{font-family:'Consolas',monospace;color:#aaa;font-size:.68rem}
.err-box{padding:12px;color:var(--err);background:#fff5f5;font-size:.8rem;display:flex;gap:7px}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #ddd;border-top-color:var(--acc);border-radius:50%;animation:sp .7s linear infinite;flex-shrink:0}
@keyframes sp{to{transform:rotate(360deg)}}

/* ══════════════════════════════════════════════════════════════
   Config Panel (slide-in desde la derecha)
══════════════════════════════════════════════════════════════ */
#cfg-panel{
  position:fixed;top:0;right:0;width:var(--panel-w);height:100%;
  background:#fff;box-shadow:-4px 0 20px rgba(0,0,0,.18);
  z-index:300;display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .28s ease;
}
#cfg-panel.open{transform:translateX(0)}

.cpanel-hdr{background:#2E4057;color:#fff;padding:13px 16px;display:flex;align-items:center;gap:10px;flex-shrink:0}
.cpanel-hdr h2{font-size:.95rem;font-weight:700;flex:1}
.cpanel-toolbar{padding:10px 14px;display:flex;gap:8px;border-bottom:1px solid #eee;flex-shrink:0;flex-wrap:wrap;align-items:center}
.cpanel-body{flex:1;overflow-y:auto;padding:0}

/* Tabla de config */
.cfg-tbl-wrap{overflow-x:auto}
table.cfg-tbl{width:100%;border-collapse:collapse;font-size:.77rem}
table.cfg-tbl thead th{background:#f0f3f7;color:#444;padding:6px 8px;font-weight:700;text-align:left;border-bottom:2px solid #dde;white-space:nowrap}
table.cfg-tbl tbody tr{border-bottom:1px solid #f0f0f0}
table.cfg-tbl tbody tr:hover{background:#fafbfd}
table.cfg-tbl td{padding:4px 5px;vertical-align:middle}
table.cfg-tbl input,table.cfg-tbl select{
  width:100%;border:1px solid #ddd;border-radius:4px;padding:4px 6px;font-size:.75rem;
  background:#fff;outline:none;transition:border-color .15s
}
table.cfg-tbl input:focus,table.cfg-tbl select:focus{border-color:var(--acc)}
table.cfg-tbl .col-name{min-width:130px}
table.cfg-tbl .col-meter{min-width:130px}
table.cfg-tbl .col-ip{min-width:110px}
table.cfg-tbl .col-port{width:58px}
table.cfg-tbl .col-slave{width:52px}
table.cfg-tbl .col-addr{width:62px}
table.cfg-tbl .col-qty{width:52px}
table.cfg-tbl .col-del{width:34px;text-align:center}
.del-btn{background:none;border:none;cursor:pointer;color:#c62828;font-size:1rem;padding:2px 4px;border-radius:3px;line-height:1}
.del-btn:hover{background:#ffebee}

.cpanel-footer{padding:10px 14px;border-top:1px solid #eee;display:flex;gap:8px;align-items:center;flex-shrink:0}
.save-status{font-size:.75rem;color:#555;flex:1}

/* Toast notificación */
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);
  background:#323232;color:#fff;padding:10px 20px;border-radius:6px;font-size:.82rem;
  transition:transform .3s;z-index:500;pointer-events:none}
#toast.show{transform:translateX(-50%) translateY(0)}
#toast.ok-toast{background:var(--ok)}
#toast.err-toast{background:var(--err)}
</style>
</head>
<body>

<!-- ══ HEADER ══════════════════════════════════════════════════════════════ -->
<header>
  <h1>⚡ Calculadora Modbus EKi — Verificación en tiempo real</h1>
  <div class="hctrl">
    <span id="last-ts" class="ts-lbl">Sin datos</span>
    <select id="sel-interval" title="Auto-refresh">
      <option value="0">Manual</option>
      <option value="10000">10 s</option>
      <option value="30000" selected>30 s</option>
      <option value="60000">1 min</option>
      <option value="300000">5 min</option>
    </select>
    <button class="btn btn-outline btn-sml" onclick="loadCache()">↺ Cache</button>
    <button class="btn btn-pri" id="btn-poll" onclick="pollAll()">⟳ Actualizar todo</button>
    <button class="btn btn-cfg" onclick="togglePanel()" id="btn-cfg">⚙ Dispositivos</button>
  </div>
</header>

<!-- ══ MAIN DASHBOARD ══════════════════════════════════════════════════════ -->
<main id="main-content">
  <div class="sum-bar" id="sum-bar">
    <div class="sum-chip"><b style="color:var(--ok)">—</b><span>OK</span></div>
    <div class="sum-chip"><b style="color:var(--err)">—</b><span>Error</span></div>
    <div class="sum-chip"><b style="color:var(--acc)">—</b><span>Total</span></div>
  </div>
  <div class="fbar">
    <label>Filtrar:</label>
    <select id="flt-meter"  onchange="render()"><option value="">Todos los medidores</option></select>
    <select id="flt-status" onchange="render()">
      <option value="">Todos los estados</option>
      <option value="ok">Conectados OK</option>
      <option value="err">Con error</option>
    </select>
    <select id="flt-eki" onchange="render()"><option value="">Todos los EKis</option></select>
  </div>
  <div class="dgrid" id="dgrid"></div>
</main>

<!-- ══ CONFIG PANEL ════════════════════════════════════════════════════════ -->
<div id="cfg-panel">
  <div class="cpanel-hdr">
    <h2>⚙ Configuración de Dispositivos</h2>
    <button class="btn btn-outline btn-sml" onclick="togglePanel()">✕ Cerrar</button>
  </div>

  <div class="cpanel-toolbar">
    <button class="btn btn-pri btn-sml" onclick="addRow()">＋ Agregar dispositivo</button>
    <button class="btn btn-outline btn-sml" onclick="importCsv()" style="border-color:#1565c0;color:#1565c0;background:#e3f2fd;font-weight:700">
      📂 Importar EKIconfig/
    </button>
    <button class="btn btn-outline btn-sml" onclick="importXlsx()" style="border-color:#ccc;color:#333;background:#f9f9f9">
      📥 Importar Ekis.xlsx
    </button>
    <span style="font-size:.72rem;color:#888;flex:1">Los cambios se guardan en web_config.json</span>
  </div>

  <div class="cpanel-body">
    <div class="cfg-tbl-wrap">
      <table class="cfg-tbl">
        <thead>
          <tr>
            <th class="col-name">Nombre</th>
            <th class="col-meter">Tipo medidor</th>
            <th class="col-ip">IP EKi</th>
            <th class="col-port">Puerto</th>
            <th class="col-slave">Slave</th>
            <th class="col-addr">Dirección</th>
            <th class="col-qty">Qty</th>
            <th class="col-del"></th>
          </tr>
        </thead>
        <tbody id="cfg-tbody"></tbody>
      </table>
    </div>
  </div>

  <div class="cpanel-footer">
    <span class="save-status" id="save-status">Edita la tabla y pulsa Guardar.</span>
    <button class="btn btn-success" onclick="saveConfig()">💾 Guardar</button>
  </div>
</div>

<!-- ══ TOAST ═══════════════════════════════════════════════════════════════ -->
<div id="toast"></div>

<script>
// ── Estado global ─────────────────────────────────────────────────────────
let DEVICES = "__DEVICES__";
const HAS_DATA = "__HAS_DATA__";
let _d = {};
let _tmr = null;
let _panelOpen = false;

const METER_TYPES = [
  'Siemens 7KT',
  'PM5110','PM5111','PM5310','PM5320','PM5330','PM5331','PM5340','PM5341',
  'PM2120','PM2130','PM2220','PM2230',
  'ABB M4M 30',
  'Gavazzi WM20-96',
];

// ── Utilidades ────────────────────────────────────────────────────────────
function fmtVal(v) {
  if (v === null || v === undefined) return '—';
  const a = Math.abs(v);
  if (a >= 1e6 || (a > 0 && a < 1e-3)) return v.toExponential(3);
  return v.toLocaleString('es', {maximumFractionDigits:4, minimumFractionDigits:0});
}
function sc(d) {
  if (!d) return 'loading';
  if (d.status === 'ok') return d.n_valid === d.n_doc ? 'ok' : 'warn';
  return d.status;
}
function cardId(key) { return 'c_' + key.replace(/[^a-z0-9]/gi,'_'); }
function toast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (type==='ok'?'ok-toast':type==='err'?'err-toast':'');
  setTimeout(() => el.className = '', 2800);
}

// ── Config Panel ──────────────────────────────────────────────────────────
function togglePanel() {
  _panelOpen = !_panelOpen;
  document.getElementById('cfg-panel').classList.toggle('open', _panelOpen);
  document.getElementById('main-content').classList.toggle('shifted', _panelOpen);
  document.getElementById('btn-cfg').style.background = _panelOpen ? '#1565c0' : '';
  if (_panelOpen) renderCfgTable();
}

function meterSelect(val) {
  const sel = document.createElement('select');
  METER_TYPES.forEach(m => {
    const o = new Option(m, m);
    if (m === val) o.selected = true;
    sel.add(o);
  });
  // Permitir también valor libre si no está en la lista
  if (val && !METER_TYPES.includes(val)) {
    const o = new Option(val, val, true, true);
    sel.insertBefore(o, sel.firstChild);
  }
  return sel.outerHTML;
}

function renderCfgTable() {
  const tbody = document.getElementById('cfg-tbody');
  tbody.innerHTML = DEVICES.map((d, i) => `
    <tr data-i="${i}">
      <td class="col-name"><input value="${esc(d.name)}"  data-f="name"  oninput="cfgChange(${i},'name',this.value)"></td>
      <td class="col-meter">${meterSelect(d.meter).replace('<select', `<select data-f="meter" onchange="cfgChange(${i},'meter',this.value)"`)}</td>
      <td class="col-ip">  <input value="${esc(d.host)}"  data-f="host"  oninput="cfgChange(${i},'host',this.value)"></td>
      <td class="col-port"><input value="${d.port}"        data-f="port"  type="number" min="1" max="65535" oninput="cfgChange(${i},'port',+this.value)"></td>
      <td class="col-slave"><input value="${d.slave}"      data-f="slave" type="number" min="1" max="255"   oninput="cfgChange(${i},'slave',+this.value)"></td>
      <td class="col-addr"><input value="${d.addr}"        data-f="addr"  type="number" min="0"             oninput="cfgChange(${i},'addr',+this.value)"></td>
      <td class="col-qty"> <input value="${d.qty}"         data-f="qty"   type="number" min="1" max="125"   oninput="cfgChange(${i},'qty',+this.value)"></td>
      <td class="col-del"> <button class="del-btn" onclick="deleteRow(${i})" title="Eliminar">🗑</button></td>
    </tr>`).join('');
  document.getElementById('save-status').textContent =
    `${DEVICES.length} dispositivo(s) — edita y pulsa Guardar.`;
}

function esc(s) { return String(s).replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function cfgChange(i, field, val) {
  DEVICES[i][field] = val;
}

function addRow() {
  DEVICES.push({
    key:'nuevo|0', name:'Nuevo-EKi', meter:'PM5340',
    host:'192.168.1.x', port:502, slave:1, addr:3000, qty:110
  });
  renderCfgTable();
  // Scroll al final
  document.querySelector('.cpanel-body').scrollTop = 99999;
}

function deleteRow(i) {
  if (!confirm(`¿Eliminar "${DEVICES[i].name}"?`)) return;
  DEVICES.splice(i, 1);
  renderCfgTable();
}

async function saveConfig() {
  document.getElementById('save-status').textContent = '⏳ Guardando...';
  try {
    const r = await fetch('/api/config', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(DEVICES)
    });
    const res = await r.json();
    if (res.error) throw new Error(res.error);
    // Actualizar claves
    DEVICES.forEach(d => { d.key = `${d.name}|${d.addr}`; });
    document.getElementById('save-status').textContent =
      `✅ Guardado — ${res.count} dispositivos`;
    toast(`Guardado: ${res.count} dispositivos`, 'ok');
    rebuildFilters();
    render();
  } catch(e) {
    document.getElementById('save-status').textContent = '❌ Error: ' + e.message;
    toast('Error al guardar: ' + e.message, 'err');
  }
}

async function importCsv() {
  if (!confirm('¿Importar configuración desde la carpeta EKIconfig/?\n\nEsto reemplazará la configuración actual con los datos reales de los CSV del EKi Studio (FC correcto, endianswap, IPs exactas).')) return;
  document.getElementById('save-status').textContent = '⏳ Importando EKIconfig/...';
  try {
    const r   = await fetch('/api/config/import_csv', {method:'POST'});
    const res = await r.json();
    if (res.error) throw new Error(res.error);
    const cr   = await fetch('/api/config');
    DEVICES    = await cr.json();
    renderCfgTable();
    document.getElementById('save-status').textContent =
      `✅ Importados ${res.count} comandos desde EKIconfig/`;
    toast(`✅ ${res.count} comandos importados desde EKIconfig/`, 'ok');
    rebuildFilters(); render();
  } catch(e) {
    document.getElementById('save-status').textContent = '❌ ' + e.message;
    toast('Error: ' + e.message, 'err');
  }
}

async function importXlsx() {
  if (!confirm('¿Reemplazar la configuración actual con los datos de Ekis.xlsx?')) return;
  document.getElementById('save-status').textContent = '⏳ Importando...';
  try {
    const r   = await fetch('/api/config/import', {method:'POST'});
    const res = await r.json();
    if (res.error) throw new Error(res.error);
    // Recargar desde el servidor
    const cr   = await fetch('/api/config');
    DEVICES    = await cr.json();
    renderCfgTable();
    document.getElementById('save-status').textContent =
      `✅ Importados ${res.count} dispositivos desde Ekis.xlsx`;
    toast(`Importados ${res.count} dispositivos`, 'ok');
    rebuildFilters();
    render();
  } catch(e) {
    document.getElementById('save-status').textContent = '❌ ' + e.message;
    toast('Error al importar: ' + e.message, 'err');
  }
}

// ── Dashboard render ──────────────────────────────────────────────────────
function renderCard(dev, d) {
  const cls = sc(d);
  let body  = '';
  if (!d) {
    body = `<div class="cinfo"><span class="spin"></span>&nbsp;Esperando poll...</div>`;
  } else if (d.status !== 'ok') {
    body = `<div class="err-box"><span class="spin" style="border-top-color:#c62828"></span>
            <div><b>${d.status}</b>&nbsp;${d.error||''}</div></div>`;
  } else {
    const pct   = d.n_doc>0 ? Math.round(d.n_valid/d.n_doc*100) : 0;
    const pcol  = pct===100?'var(--ok)':pct>50?'var(--warn)':'var(--err)';
    const rows  = d.regs.map(r => {
      const rc  = r.undoc?'undoc':r.valid?'':'na';
      return `<tr class="${rc}">
        <td class="rnum">${r.reg}</td><td>${r.name}</td><td>${r.cat||''}</td>
        <td class="vval">${r.valid?fmtVal(r.val):'<span style="color:#ddd">—</span>'}</td>
        <td class="uunit">${r.unit||''}</td>
        <td class="hhex">${r.hex||''}</td>
      </tr>`;
    }).join('');
    body = `
      <div class="cinfo">
        <span class="badge ${d.qty_recv===d.qty_exp?'b-ok':'b-warn'}">${d.qty_recv===d.qty_exp?'✓':'✗'} ${d.qty_recv}/${d.qty_exp} regs</span>
        <span style="color:${pcol};font-weight:700">${d.n_valid}/${d.n_doc}</span>&nbsp;válidos (${pct}%)
        <span class="ts-lbl" style="margin-left:auto;color:#aaa">${d.ts||''}</span>
      </div>
      <div class="tscroll"><table class="rtbl">
        <thead><tr><th>Reg</th><th>Nombre</th><th>Categoría</th><th>Valor</th><th>Unidad</th><th>Hex</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  }
  const k = dev.key.replace(/'/g,"\\'");
  return `<div class="card ${cls}" id="${cardId(dev.key)}">
    <div class="chdr">
      <div class="sdot ${cls}"></div>
      <div style="flex:1">
        <div class="ctitle">${dev.name}</div>
        <div class="cmeta">${dev.meter} · ${dev.host} · Dir ${dev.addr}+${dev.qty}</div>
      </div>
      <button class="btn btn-outline btn-sml" style="border-color:#ccc;color:#333;background:#f5f5f5" onclick="pollOne('${k}')">⟳</button>
    </div>
    ${body}
  </div>`;
}

function rebuildFilters() {
  const fm = document.getElementById('flt-meter');
  const fe = document.getElementById('flt-eki');
  // Resetear y reconstruir
  while(fm.options.length>1) fm.remove(1);
  while(fe.options.length>1) fe.remove(1);
  [...new Set(DEVICES.map(d=>d.meter))].sort()
    .forEach(m => fm.add(new Option(m,m)));
  DEVICES.forEach(d => fe.add(new Option(d.name,d.name)));
}

function render() {
  const fm = document.getElementById('flt-meter').value;
  const fs = document.getElementById('flt-status').value;
  const fe = document.getElementById('flt-eki').value;

  if (document.getElementById('flt-meter').options.length === 1) rebuildFilters();

  const vis = DEVICES.filter(d => {
    if (fm && d.meter!==fm) return false;
    if (fe && d.name !==fe) return false;
    const dd = _d[d.key];
    if (fs==='ok'  && (!dd||dd.status!=='ok')) return false;
    if (fs==='err' && dd && dd.status==='ok')  return false;
    return true;
  });

  document.getElementById('dgrid').innerHTML =
    vis.map(d => renderCard(d, _d[d.key]||null)).join('');

  const nOk  = Object.values(_d).filter(x=>x.status==='ok').length;
  const nErr = Object.values(_d).filter(x=>x.status!=='ok').length;
  document.getElementById('sum-bar').innerHTML = `
    <div class="sum-chip"><b style="color:var(--ok)">${nOk}</b><span>Conectados OK</span></div>
    <div class="sum-chip"><b style="color:var(--err)">${nErr}</b><span>Con error</span></div>
    <div class="sum-chip"><b style="color:var(--acc)">${DEVICES.length}</b><span>Dispositivos totales</span></div>`;
}

// ── Polling ───────────────────────────────────────────────────────────────
function setBusy(on) {
  const b = document.getElementById('btn-poll');
  b.disabled = on;
  b.textContent = on ? '⏳ Consultando...' : '⟳ Actualizar todo';
}

async function pollAll() {
  setBusy(true);
  document.getElementById('last-ts').textContent = 'Consultando...';
  try {
    const r = await fetch('/api/poll');
    _d = await r.json();
    document.getElementById('last-ts').textContent = 'Actualizado: ' + new Date().toLocaleTimeString('es');
  } catch(e) {
    document.getElementById('last-ts').textContent = 'Error: ' + e.message;
  }
  setBusy(false); render();
}

async function loadCache() {
  try {
    const r = await fetch('/api/data');
    _d = await r.json();
    document.getElementById('last-ts').textContent = '(cache) ' + new Date().toLocaleTimeString('es');
    render();
  } catch(e) { console.error(e); }
}

async function pollOne(key) {
  const el = document.getElementById(cardId(key));
  if (el) el.classList.add('loading');
  try {
    const r = await fetch('/api/poll/' + encodeURIComponent(key));
    _d[key] = await r.json();
    render();
  } catch(e) { console.error(e); }
}

// ── Auto-refresh ──────────────────────────────────────────────────────────
document.getElementById('sel-interval').addEventListener('change', function() {
  if (_tmr) { clearInterval(_tmr); _tmr=null; }
  const ms = parseInt(this.value);
  if (ms>0) _tmr = setInterval(pollAll, ms);
});

// ── Init ──────────────────────────────────────────────────────────────────
(async () => {
  render();
  if (HAS_DATA) await loadCache();
  else          await pollAll();
  const ms = parseInt(document.getElementById('sel-interval').value);
  if (ms>0) _tmr = setInterval(pollAll, ms);
})();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Arranque
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    devs = _load_config()
    _set_devices(devs)

    print("=" * 60)
    print("  Calculadora Modbus EKi — Dashboard Web")
    print(f"  {len(devs)} dispositivos cargados")
    print("=" * 60)
    print("  URL local:  http://localhost:8080")
    print("  Ctrl+C para detener")
    print("=" * 60)

    def _bg_loop():
        while True:
            _poll_all()
            time.sleep(POLL_INTERVAL)

    threading.Thread(target=_bg_loop, daemon=True).start()

    # Abrir navegador automáticamente 1.5 s después de arrancar
    def _open_browser():
        time.sleep(1.5)
        webbrowser.open('http://localhost:8080')
    threading.Thread(target=_open_browser, daemon=True).start()

    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
