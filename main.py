import os
import sys
import logging
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
    QThread, pyqtSignal,
)
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSpinBox, QTableView,
    QFileDialog, QStatusBar, QHeaderView, QSizePolicy, QMessageBox,
    QProgressDialog, QToolBar, QFrame,
)
import openpyxl

from modbus import parse_csv, pair_frames, decode_frame, DecodedValue
from regmaps import get_reg_map, auto_detect_meter
from eki_config import read_ekis_rows, build_decode_config

# ─── Columnas de la tabla (sin timestamp/eki/transid — están en el nav bar) ──

COLS = ['Dirección', 'Nombre Registro', 'Categoría', 'Valor', 'Unidad', 'Raw Hex']

COL_ADDR = 0
COL_NAME = 1
COL_CAT  = 2
COL_VAL  = 3
COL_UNIT = 4
COL_HEX  = 5


def fmt_value(v: Optional[float]) -> str:
    if v is None:
        return 'N/A'
    if abs(v) >= 1e6 or (abs(v) < 1e-3 and v != 0.0):
        return f'{v:.4e}'
    return f'{v:.6g}'


# ─── Frame: una trama decodificada con sus metadatos ─────────────────────────

@dataclass
class Frame:
    rows: list[tuple]        # filas de la tabla (6 columnas)
    timestamp: str
    eki_name: str
    trans_id: str
    start_addr: int          # dirección PDU 0-indexed
    qty_received: int        # registros 16-bit recibidos
    qty_expected: int        # según EKi config (0 = desconocido)


# ─── Modelo de datos ──────────────────────────────────────────────────────────

class ModbusModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[tuple] = []

    def set_rows(self, rows: list[tuple]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLS)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() in (COL_ADDR, COL_VAL):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            if index.column() == COL_VAL and self._rows[index.row()][COL_VAL] == 'N/A':
                return QColor('#999999')
        return None

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLS[section]
        return None

    def get_rows(self) -> list[tuple]:
        return self._rows


# ─── Proxy: ordenamiento multi-columna ───────────────────────────────────────

class SortProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self._sort_keys: list[tuple[int, Qt.SortOrder]] = []

    def set_primary_sort(self, col: int, order: Qt.SortOrder) -> None:
        self._sort_keys = [(col, order)] + [(c, o) for c, o in self._sort_keys if c != col]
        self.invalidate()

    def add_secondary_sort(self, col: int, order: Qt.SortOrder) -> None:
        self._sort_keys = [(c, o) for c, o in self._sort_keys if c != col]
        self._sort_keys.append((col, order))
        self.invalidate()

    def sort_description(self) -> str:
        parts = []
        for i, (col, order) in enumerate(self._sort_keys):
            arrow = '↑' if order == Qt.SortOrder.AscendingOrder else '↓'
            name = COLS[col] if 0 <= col < len(COLS) else f'Col{col}'
            prefix = f'{i + 1}.' if len(self._sort_keys) > 1 else ''
            parts.append(f'{prefix}{name} {arrow}')
        return '   '.join(parts)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if not self._sort_keys:
            return False
        m = self.sourceModel()
        for col, order in self._sort_keys:
            asc = (order == Qt.SortOrder.AscendingOrder)
            lv = m.data(m.index(left.row(), col)) or ''
            rv = m.data(m.index(right.row(), col)) or ''
            if col == COL_ADDR:
                try:
                    ln, rn = int(lv), int(rv)
                    if ln != rn:
                        return ln < rn if asc else ln > rn
                    continue
                except (ValueError, TypeError):
                    pass
            if col == COL_VAL:
                try:
                    ln = float(lv) if lv not in ('N/A', '') else float('inf')
                    rn = float(rv) if rv not in ('N/A', '') else float('inf')
                    if ln != rn:
                        return ln < rn if asc else ln > rn
                    continue
                except (ValueError, TypeError):
                    pass
            if lv.lower() != rv.lower():
                return lv.lower() < rv.lower() if asc else lv.lower() > rv.lower()
        return False


# ─── Worker ───────────────────────────────────────────────────────────────────

class LoadWorker(QThread):
    finished = pyqtSignal(list, list)   # frames: list[Frame], eki_names: list[str]
    progress = pyqtSignal(int)
    error    = pyqtSignal(str)

    def __init__(self, csv_path: str, eki_config: dict,
                 eki_qty: dict, maps_dir: str) -> None:
        super().__init__()
        self.csv_path = csv_path
        self.eki_config = eki_config    # {eki_name: meter_type}
        self.eki_qty    = eki_qty       # {(eki_name, pdu_addr): expected_qty}
        self.maps_dir   = maps_dir
        self._cancelled = False

    def cancel(self) -> None:
        """Cancelación cooperativa: el bucle de run() la consulta y termina
        de forma ordenada (a diferencia de terminate(), que mata el hilo)."""
        self._cancelled = True

    def run(self) -> None:
        try:
            raw_rows = parse_csv(self.csv_path)
            pairs = pair_frames(raw_rows)
            total = len(pairs)
            reg_maps: dict[str, dict] = {}
            frames: list[Frame] = []
            eki_set: set[str] = set()

            for i, (ts, eki_name, trans_id, start_addr, payload) in enumerate(pairs):
                if self._cancelled:
                    return
                meter_type = self.eki_config.get(eki_name, '') or \
                             auto_detect_meter(start_addr, len(payload) // 2)

                if meter_type not in reg_maps:
                    reg_maps[meter_type] = get_reg_map(meter_type, self.maps_dir)

                reg_map = reg_maps[meter_type]

                # Calcular cantidades ANTES de decodificar
                qty_received = len(payload) // 2
                qty_expected = self.eki_qty.get((eki_name, start_addr), 0)

                decoded: list[DecodedValue] = decode_frame(
                    ts, eki_name, trans_id, start_addr, payload, reg_map,
                    expected_qty=qty_expected,
                )

                rows = [
                    (str(dv.register), dv.name, dv.category,
                     fmt_value(dv.value), dv.unit, dv.raw_hex)
                    for dv in decoded
                ]

                frames.append(Frame(
                    rows=rows,
                    timestamp=ts,
                    eki_name=eki_name,
                    trans_id=trans_id,
                    start_addr=start_addr,
                    qty_received=qty_received,
                    qty_expected=qty_expected,
                ))
                eki_set.add(eki_name)

                if total > 0 and i % max(1, total // 100) == 0:
                    self.progress.emit(int(i / total * 100))

            self.finished.emit(frames, sorted(eki_set))

        except Exception as exc:
            self.error.emit(str(exc))


# ─── Ventana principal ────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle('Calculadora Modbus EKi')
        self.resize(1200, 750)

        self._csv_path: str = ''
        self._maps_dir: str = ''
        self._eki_config: dict[str, str] = {}
        self._eki_qty:    dict[tuple, int] = {}   # {(name, pdu_addr): qty}
        self._worker: Optional[LoadWorker] = None

        self._all_frames:     list[Frame] = []
        self._visible_frames: list[Frame] = []
        self._current_idx: int = 0

        self._model = ModbusModel()
        self._proxy = SortProxy()
        self._proxy.setSourceModel(self._model)

        self._build_ui()

    # ── Construcción UI ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = QToolBar('Acciones')
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        btn_csv = QPushButton('Cargar CSV')
        btn_csv.setFixedHeight(30)
        btn_csv.clicked.connect(self._load_csv)
        toolbar.addWidget(btn_csv)
        toolbar.addSeparator()

        btn_eki = QPushButton('Cargar EKi Config')
        btn_eki.setFixedHeight(30)
        btn_eki.clicked.connect(self._load_eki_config)
        toolbar.addWidget(btn_eki)
        toolbar.addSeparator()

        self._btn_refresh = QPushButton('Refrescar')
        self._btn_refresh.setFixedHeight(30)
        self._btn_refresh.setEnabled(False)
        self._btn_refresh.clicked.connect(self._run_decode)
        toolbar.addWidget(self._btn_refresh)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self._btn_export = QPushButton('Exportar Excel')
        self._btn_export.setFixedHeight(30)
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_excel)
        toolbar.addWidget(self._btn_export)

        # ── Barra de navegación ────────────────────────────────────────────
        nav_widget = QWidget()
        nav_widget.setObjectName('navBar')
        nav_widget.setStyleSheet('#navBar { background: #F5F7FA; }')
        nav_layout = QHBoxLayout(nav_widget)
        nav_layout.setContentsMargins(8, 6, 8, 6)

        # Selector EKi
        nav_layout.addWidget(QLabel('EKi:'))
        self._cmb_eki = QComboBox()
        self._cmb_eki.setMinimumWidth(170)
        self._cmb_eki.currentTextChanged.connect(self._on_eki_filter_change)
        nav_layout.addWidget(self._cmb_eki)

        # Separador
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet('color: #CCC;')
        nav_layout.addWidget(sep)

        # Navegación trama
        self._btn_prev = QPushButton('◀')
        self._btn_prev.setFixedWidth(36)
        self._btn_prev.setFixedHeight(28)
        self._btn_prev.setEnabled(False)
        self._btn_prev.clicked.connect(self._go_prev)
        nav_layout.addWidget(self._btn_prev)

        nav_layout.addWidget(QLabel('Trama'))

        self._spn_frame = QSpinBox()
        self._spn_frame.setMinimum(1)
        self._spn_frame.setMaximum(1)
        self._spn_frame.setFixedWidth(60)
        self._spn_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spn_frame.valueChanged.connect(self._on_spinbox_change)
        nav_layout.addWidget(self._spn_frame)

        nav_layout.addWidget(QLabel('de'))

        self._lbl_total = QLabel('0')
        self._lbl_total.setMinimumWidth(35)
        nav_layout.addWidget(self._lbl_total)

        self._btn_next = QPushButton('▶')
        self._btn_next.setFixedWidth(36)
        self._btn_next.setFixedHeight(28)
        self._btn_next.setEnabled(False)
        self._btn_next.clicked.connect(self._go_next)
        nav_layout.addWidget(self._btn_next)

        # Separador
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet('color: #CCC;')
        nav_layout.addWidget(sep2)

        # Info de la trama actual
        self._lbl_frame_info = QLabel('—')
        self._lbl_frame_info.setStyleSheet('color: #444; font-size: 11px;')
        nav_layout.addWidget(self._lbl_frame_info)

        nav_layout.addStretch()

        # Indicador de orden activo
        self._lbl_sort = QLabel()
        self._lbl_sort.setVisible(False)
        self._lbl_sort.setStyleSheet(
            'color: #1565C0; font-size: 10px; padding: 2px 6px;'
            'background: #E3F2FD; border-radius: 3px;'
        )
        nav_layout.addWidget(self._lbl_sort)

        nav_layout.addSpacing(8)
        self._lbl_rows = QLabel('0 registros')
        nav_layout.addWidget(self._lbl_rows)

        # ── Tabla ──────────────────────────────────────────────────────────
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)

        font = QFont('Consolas', 9)
        self._table.setFont(font)

        hdr = self._table.horizontalHeader()
        hdr.setFont(QFont(font.family(), 9, QFont.Weight.Bold))
        hdr.setStretchLastSection(True)
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(True)
        hdr.sectionClicked.connect(self._on_header_click)
        hdr.setToolTip('Click: ordenar  |  Ctrl+Click: columna secundaria')

        hdr.resizeSection(COL_ADDR,  70)
        hdr.resizeSection(COL_NAME, 300)
        hdr.resizeSection(COL_CAT,  200)
        hdr.resizeSection(COL_VAL,   100)
        hdr.resizeSection(COL_UNIT,  60)
        hdr.setSectionResizeMode(COL_HEX, QHeaderView.ResizeMode.Stretch)

        self._proxy.modelReset.connect(self._update_row_count)

        # ── Layout central ─────────────────────────────────────────────────
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(nav_widget)
        layout.addWidget(self._table)
        self.setCentralWidget(central)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage('Listo. Cargue un archivo CSV para comenzar.')

    # ── Ordenamiento ────────────────────────────────────────────────────────

    def _on_header_click(self, col: int) -> None:
        modifiers = QApplication.keyboardModifiers()
        current_order = next(
            (o for c, o in self._proxy._sort_keys if c == col), None
        )
        new_order = (
            Qt.SortOrder.DescendingOrder
            if current_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            self._proxy.add_secondary_sort(col, new_order)
        else:
            self._proxy.set_primary_sort(col, new_order)

        if self._proxy._sort_keys:
            p_col, p_order = self._proxy._sort_keys[0]
            self._table.horizontalHeader().setSortIndicator(p_col, p_order)

        desc = self._proxy.sort_description()
        self._lbl_sort.setText(f'Orden:  {desc}')
        self._lbl_sort.setVisible(bool(desc))

    # ── Carga de archivos ────────────────────────────────────────────────────

    def _load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Seleccionar captura Modbus', self._maps_dir or '',
            'CSV files (*.csv);;All files (*)'
        )
        if not path:
            return
        self._csv_path = path
        self._maps_dir = os.path.dirname(path)
        self._btn_refresh.setEnabled(True)
        eki_default = os.path.join(self._maps_dir, 'Ekis.xlsx')
        if os.path.exists(eki_default) and not self._eki_config:
            self._load_eki_from_path(eki_default)
        self._run_decode()

    def _load_eki_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Seleccionar configuración EKi', self._maps_dir or '',
            'Excel files (*.xlsx *.xls);;All files (*)'
        )
        if path:
            self._load_eki_from_path(path)
            if self._csv_path:
                self._run_decode()

    def _load_eki_from_path(self, path: str) -> None:
        try:
            rows = read_ekis_rows(path)
            config, qty_map = build_decode_config(rows)
            self._eki_config = config
            self._eki_qty    = qty_map
            self._status.showMessage(
                f'EKi config: {len(config)} medidores — {os.path.basename(path)}'
            )
        except Exception as exc:
            QMessageBox.warning(self, 'Error EKi Config', str(exc))

    # ── Decodificación ──────────────────────────────────────────────────────

    def _run_decode(self) -> None:
        if not self._csv_path or (self._worker and self._worker.isRunning()):
            return
        self._btn_refresh.setEnabled(False)
        self._btn_export.setEnabled(False)
        self._model.set_rows([])
        self._cmb_eki.blockSignals(True)
        self._cmb_eki.clear()
        self._cmb_eki.blockSignals(False)
        self._all_frames = []
        self._visible_frames = []
        self._current_idx = 0

        self._progress = QProgressDialog('Decodificando tramas...', 'Cancelar', 0, 100, self)
        self._progress.setWindowTitle('Cargando')
        self._progress.setMinimumDuration(500)
        self._progress.setModal(True)

        self._worker = LoadWorker(
            self._csv_path, self._eki_config, self._eki_qty, self._maps_dir
        )
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_load_done)
        self._worker.error.connect(self._on_load_error)
        self._progress.canceled.connect(self._worker.cancel)
        self._worker.start()

    def _on_load_done(self, frames: list, eki_names: list) -> None:
        self._progress.close()
        self._all_frames = frames
        self._btn_refresh.setEnabled(True)
        self._btn_export.setEnabled(bool(frames))

        self._cmb_eki.blockSignals(True)
        self._cmb_eki.addItem('Todos')
        self._cmb_eki.addItems(eki_names)
        self._cmb_eki.blockSignals(False)

        self._apply_eki_filter()
        csv_name = os.path.basename(self._csv_path)
        self._status.showMessage(
            f'{csv_name}  |  {len(eki_names)} EKi(s)  |  {len(frames)} tramas'
        )

    def _on_load_error(self, msg: str) -> None:
        self._progress.close()
        self._btn_refresh.setEnabled(True)
        QMessageBox.critical(self, 'Error al cargar', msg)

    # ── Navegación ──────────────────────────────────────────────────────────

    def _on_eki_filter_change(self, eki: str) -> None:
        self._apply_eki_filter()

    def _apply_eki_filter(self) -> None:
        selected = self._cmb_eki.currentText()
        if selected in ('Todos', ''):
            self._visible_frames = list(self._all_frames)
        else:
            self._visible_frames = [f for f in self._all_frames if f.eki_name == selected]

        n = len(self._visible_frames)
        self._spn_frame.blockSignals(True)
        self._spn_frame.setMaximum(max(1, n))
        self._spn_frame.setValue(1)
        self._spn_frame.blockSignals(False)
        self._lbl_total.setText(str(n))

        nav_enabled = n > 0
        self._btn_prev.setEnabled(False)
        self._btn_next.setEnabled(n > 1)
        self._spn_frame.setEnabled(nav_enabled)

        self._current_idx = 0
        self._show_frame(0)

    def _on_spinbox_change(self, value: int) -> None:
        idx = value - 1
        if 0 <= idx < len(self._visible_frames) and idx != self._current_idx:
            self._current_idx = idx
            self._show_frame(idx)

    def _go_prev(self) -> None:
        if self._current_idx > 0:
            self._current_idx -= 1
            self._spn_frame.blockSignals(True)
            self._spn_frame.setValue(self._current_idx + 1)
            self._spn_frame.blockSignals(False)
            self._show_frame(self._current_idx)

    def _go_next(self) -> None:
        if self._current_idx < len(self._visible_frames) - 1:
            self._current_idx += 1
            self._spn_frame.blockSignals(True)
            self._spn_frame.setValue(self._current_idx + 1)
            self._spn_frame.blockSignals(False)
            self._show_frame(self._current_idx)

    def _show_frame(self, idx: int) -> None:
        n = len(self._visible_frames)
        if n == 0:
            self._model.set_rows([])
            self._lbl_frame_info.setText('—')
            return

        frame = self._visible_frames[idx]
        self._model.set_rows(frame.rows)

        # Botones prev/next
        self._btn_prev.setEnabled(idx > 0)
        self._btn_next.setEnabled(idx < n - 1)

        # Info de la trama
        reg_start = frame.start_addr + 1
        reg_end   = frame.start_addr + frame.qty_received
        if frame.qty_expected > 0:
            match = frame.qty_received == frame.qty_expected
            qty_str = (
                f'Regs: {frame.qty_received}/{frame.qty_expected} '
                + ('✓' if match else '✗')
            )
            color = '#2E7D32' if match else '#C62828'
        else:
            qty_str = f'Regs: {frame.qty_received}'
            color = '#444'

        self._lbl_frame_info.setText(
            f'{frame.eki_name}  ·  Trans ID {frame.trans_id}  ·  '
            f't = {frame.timestamp}  ·  Dir {reg_start}–{reg_end}  ·  '
            f'<b style="color:{color}">{qty_str}</b>'
        )
        self._lbl_frame_info.setTextFormat(Qt.TextFormat.RichText)

    def _update_row_count(self) -> None:
        self._lbl_rows.setText(f'{self._model.rowCount()} registros')

    # ── Exportar Excel ──────────────────────────────────────────────────────

    def _export_excel(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exportar a Excel', self._maps_dir or '',
            'Excel files (*.xlsx)'
        )
        if not path:
            return
        if not path.endswith('.xlsx'):
            path += '.xlsx'

        from openpyxl.styles import Font, PatternFill, Alignment

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Modbus'

        # Encabezado con metadatos extra
        meta_cols = ['Timestamp', 'EKi', 'Trans ID'] + COLS
        ws.append(meta_cols)
        header_font  = Font(bold=True, color='FFFFFF')
        header_fill  = PatternFill(fill_type='solid', fgColor='2E4057')
        alt_fill     = PatternFill(fill_type='solid', fgColor='F0F4F8')
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')

        r_idx = 2
        for frame in self._visible_frames:
            for row_data in frame.rows:
                val = row_data[COL_VAL]
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    pass
                full_row = [frame.timestamp, frame.eki_name, frame.trans_id,
                            row_data[COL_ADDR], row_data[COL_NAME],
                            row_data[COL_CAT], val,
                            row_data[COL_UNIT], row_data[COL_HEX]]
                ws.append(full_row)
                if r_idx % 2 == 1:
                    for cell in ws[r_idx]:
                        cell.fill = alt_fill
                r_idx += 1

        col_widths = [14, 20, 8, 10, 40, 28, 14, 8, 16]
        for i, w in enumerate(col_widths, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = w

        try:
            wb.save(path)
            self._status.showMessage(f'Exportado: {path}')
        except Exception as exc:
            QMessageBox.critical(self, 'Error al exportar', str(exc))


# ─── Punto de entrada ─────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    from PyQt6.QtGui import QPalette
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor('#f0f4f8'))
    app.setPalette(palette)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
