# Calculadora Modbus EKi

Herramienta para **decodificar tramas Modbus** de medidores de energía detrás de
gateways EKi y mostrar los registros con su nombre, unidad y valor de ingeniería.

Tiene dos frontends sobre el mismo motor de decodificación:

- **App de escritorio (PyQt6)** — analiza una **captura CSV** de tráfico Modbus
  ya registrada, empareja peticiones/respuestas y muestra cada trama decodificada.
- **Servidor web (Flask)** — **polea en vivo** los medidores por Modbus TCP y
  expone un dashboard en `http://localhost:8080`.

## Estructura del proyecto

```
.
├── main.py            App de escritorio (PyQt6): decodifica capturas CSV
├── web_server.py      Dashboard web (Flask) con polling Modbus TCP en vivo
├── modbus.py          Motor: parse_csv, pair_frames, decode_frame, word_swap
├── regmaps.py         Resolución de mapas de registros por medidor
├── eki_config.py      Lectura compartida de Ekis.xlsx (escritorio + web)
├── Ekis.xlsx          Configuración de dispositivos (nombre, IP, medidor, addr, qty)
├── EKIconfig/         CSVs de comandos Modbus exportados por EKi
├── capture.csv        Captura de ejemplo para la app de escritorio
├── data/
│   ├── regmaps.json   Mapa de registros unificado (FUENTE en runtime)
│   └── *.xls/.xlsx    Mapas de fabricante (fuente para regenerar regmaps.json)
├── docs/              Manuales PDF de los medidores (referencia, no se usan en runtime)
├── tools/
│   └── generate_regmaps.py  Regenera data/regmaps.json desde los loaders
├── tests/             Tests con pytest
├── requirements.txt       Dependencias de la app de escritorio
├── requirements_web.txt   Dependencias del servidor web
├── requirements_dev.txt   Todo lo anterior + pytest
├── build_exe.bat / *.spec PyInstaller (empaqueta el dashboard web en un .exe)
├── run.bat / run_web.bat  Lanzadores
```

## Requisitos

- Python 3.11+ (probado con 3.13)
- Escritorio: `pip install -r requirements.txt`
- Web: `pip install -r requirements_web.txt`
- Desarrollo/tests: `pip install -r requirements_dev.txt`

## Uso

### App de escritorio (analizar una captura)

```bat
run.bat
```
o `python main.py`. Luego **Cargar CSV** (formato con columnas
`Time, Trans ID, Name, Send/Recv, FC, Data`). Si hay un `Ekis.xlsx` junto al CSV
se carga automáticamente para asignar el tipo de medidor y la cantidad esperada
de registros por trama. Se puede filtrar por EKi, navegar trama a trama, ordenar
por columnas (Ctrl+Click = orden secundario) y exportar a Excel.

### Servidor web (polling en vivo)

```bat
run_web.bat
```
o `python web_server.py`. Abre `http://localhost:8080`. En el primer arranque, si
no existe `web_config.json`, se genera a partir de `Ekis.xlsx`.

### Compilar el .exe del dashboard

```bat
build_exe.bat
```
Genera `dist/Calculadora Modbus EKi.exe` (onefile). Los mapas de `data/`,
`Ekis.xlsx` y `EKIconfig/` quedan embebidos. Para usar otra configuración en la
PC destino, coloca un `Ekis.xlsx` junto al `.exe`.

## Mapas de registros

En runtime, **todos** los medidores se sirven desde un único mapa unificado:
`data/regmaps.json` (`{medidor: {registro: [nombre, unidad, tipo, categoría]}}`).
`regmaps.get_reg_map` resuelve el alias del medidor a su id canónico y busca en
ese JSON; si faltara, recurre al loader original como fallback.

El JSON se **genera** (no se escribe a mano) desde las fuentes originales —
dicts hardcodeados y los `.xls/.xlsx` de fabricante en `data/`:

```bat
python tools/generate_regmaps.py
```

Regenéralo cuando cambie un loader o un `.xls`. Los tests verifican que el JSON
coincide registro a registro con los loaders, así que no hay riesgo de deriva.

| Medidor                              | Fuente del mapa (para regenerar)    |
|--------------------------------------|-------------------------------------|
| Siemens SMART 7KT0310                | dict en `regmaps.py`                |
| Carlo Gavazzi WM20/WM30/WM40         | dict en `regmaps.py`                |
| Schneider NSX / Micrologic LV434000  | dict en `regmaps.py`                |
| Schneider PM51xx / PM53xx            | `data/PM51xx_…xls`                  |
| Schneider PM2xxx                     | `data/Public_PM2xxx_…xls`           |
| ABB M4M 30/32                        | `data/9AKK…M4M Modbus map.xlsx`     |

## Convenciones de direccionamiento

Hay dos convenciones conviviendo, y el código las maneja explícitamente:

- **Schneider / Modicon (1-indexed):** la dirección del documento es
  `PDU_address + 1` (p. ej. registro `3000` ⇒ PDU `2999`).
- **Gavazzi / Siemens (0-indexed):** la dirección del documento es el propio
  `PDU_address` (p. ej. `82` ⇒ PDU `82`).

`Ekis.xlsx` guarda la dirección "de documento"; `eki_config.build_decode_config`
registra la cantidad esperada bajo **ambas** convenciones para que el emparejado
funcione sin saber de antemano cuál usa cada medidor.

## Tests

```bat
python -m pytest
```
Cubren los decoders y sus centinelas "no disponible", `word_swap`, el emparejado
Send/Receive sobre `capture.csv`, el relleno de `N/A` en tramas truncadas, el
parseo de `Ekis.xlsx` y la resolución de los mapas en `data/`.
