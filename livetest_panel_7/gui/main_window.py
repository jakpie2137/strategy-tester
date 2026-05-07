
# gui/main_window.py
import os
import sys
import threading
import time
import logging
import queue
import gc
import tracemalloc
import psutil
from datetime import datetime

# --- PG-only: lightweight SQL context using psycopg (no sqlite) ---
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row


@contextmanager
def pg_conn(db):
    """Context manager exposing a minimal .execute/.fetchone/.fetchall API like sqlite3, backed by psycopg (Postgres)."""
    conn = psycopg.connect(db.dsn, row_factory=dict_row) if hasattr(db, "dsn") else psycopg.connect(db, row_factory=dict_row)
    try:
        with conn:
            with conn.cursor() as cur:
                class _C:
                    def __init__(self, conn, cur):
                        self._conn = conn
                        self._cur = cur
                    def execute(self, sql, params=()):
                        # translate sqlite '?' placeholders to psycopg '%s'
                        sql2 = sql.replace('?', '%s')
                        self._cur.execute(sql2, tuple(params or ()))
                        return self
                    def fetchone(self):
                        r = self._cur.fetchone()
                        if r is None: return None
                        # return tuple-like for legacy code
                        if isinstance(r, dict): return tuple(r.values())
                        return r
                    def fetchall(self):
                        rows = self._cur.fetchall()
                        if rows and isinstance(rows[0], dict):
                            return [tuple(r.values()) for r in rows]
                        return rows
                yield _C(conn, cur)
    finally:
        try: conn.close()
        except Exception: pass


import os, sys
# --- cadence/bootstrap import (adds shared queue, CandleBatchWriter, and cadence loop) ---
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
try:
    from mw_cadence_bootstrap import attach_live_cadence
except Exception:
    # fallback path (in case file sits next to main_window)
    try:
        from ..mw_cadence_bootstrap import attach_live_cadence  # type: ignore
    except Exception:
        attach_live_cadence = None  # will be checked at runtime


import pandas as pd
import pytz

def _as_df(obj):
    """
    Coerce list[dict]/dict/DataFrame to DataFrame; None -> None.
    Używane m.in. przez _normalize_candles_df.
    """
    import pandas as pd

    if obj is None:
        return None

    # Już jest DataFrame
    if isinstance(obj, pd.DataFrame):
        return obj

    # Lista czegoś
    if isinstance(obj, list):
        if not obj:
            return pd.DataFrame()
        first = obj[0]
        # lista dictów
        if isinstance(first, dict):
            return pd.DataFrame(obj)
        # lista tuple/list – weź jak leci
        return pd.DataFrame(obj)

    # Pojedynczy dict
    if isinstance(obj, dict):
        return pd.DataFrame([obj])

    # Inne typy nas nie interesują
    return None

def _normalize_candles_df(raw_df):
    """
    Normalize candle DF to:
      - DatetimeIndex named 'close_time' in UTC
      - int 'timestamp' column (epoch seconds)
    Accepts list/dict/DF. Returns DataFrame or None.
    """
    import pandas as pd
    df = _as_df(raw_df)
    if df is None or (hasattr(df, 'empty') and df.empty):
        return None

    # Ensure columns lower/consistent
    cols = list(df.columns)
    # If close_time missing, synthesize from timestamp/close_time_ms/index
    if 'close_time' not in df.columns:
        if 'timestamp' in df.columns:
            try:
                df['close_time'] = pd.to_datetime(pd.to_numeric(df['timestamp'], errors='coerce'), unit='s', utc=True)
            except Exception:
                df['close_time'] = pd.to_datetime(df['timestamp'], utc=True, errors='coerce')
        elif 'close_time_ms' in df.columns:
            df['close_time'] = pd.to_datetime(pd.to_numeric(df['close_time_ms'], errors='coerce'), unit='ms', utc=True)
        elif isinstance(df.index, pd.DatetimeIndex):
            df['close_time'] = df.index.tz_localize('UTC') if df.index.tz is None else df.index.tz_convert('UTC')
        elif 'open_time' in df.columns:
            # Fallback: treat open_time as close_time (better than crash)
            df['close_time'] = pd.to_datetime(df['open_time'], utc=True, errors='coerce')

    # Final guard: if still missing, bail out
    if 'close_time' not in df.columns:
        return None

    # Coerce to datetime UTC
    df['close_time'] = pd.to_datetime(df['close_time'], utc=True, errors='coerce')

    # Drop any pre-existing index to avoid duplicates, then set index
    try:
        if df.index.name == 'close_time':
            df = df.reset_index(drop=True)
    except Exception:
        pass
    try:
        df = df.set_index('close_time')
    except Exception:
        # As a last resort, build index from to_datetime of timestamp
        try:
            df.index = pd.to_datetime(pd.to_numeric(df['timestamp'], errors='coerce'), unit='s', utc=True)
            df.index.name = 'close_time'
        except Exception:
            return None

    # Deduplicate index
    try:
        df = df[~df.index.duplicated(keep='last')]
    except Exception:
        pass

    # Ensure UTC tz on index
    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')
    except Exception:
        pass

    # Add integer epoch seconds column
    try:
        df['timestamp'] = (df.index.view('int64') // 10**9).astype('int64')
    except Exception:
        try:
            df['timestamp'] = (df.index.astype('int64') // 10**9).astype('int64')
        except Exception:
            pass

    return df


    if isinstance(obj, pd.DataFrame):
        return obj
    try:
        if isinstance(obj, list):
            if not obj:
                return pd.DataFrame()
            # list of dicts?
            if isinstance(obj[0], dict):
                return pd.DataFrame(obj)
            # list of tuples? try common candle order
            if isinstance(obj[0], (list, tuple)):
                # try to detect header from a sibling attr if present
                cols = ['symbol','open_time','open','high','low','close','volume','close_time']
                try:
                    return pd.DataFrame(obj, columns=cols[:len(obj[0])])
                except Exception:
                    return pd.DataFrame(obj)
        if isinstance(obj, dict):
            return pd.DataFrame([obj])
    except Exception:
        pass
    return None

from PyQt5.QtCore import QTimer, Qt, QObject, pyqtSignal, QSize
from PyQt5.QtGui import QIcon, QKeySequence, QPainter, QPen, QPixmap, QTextCursor, QTextDocument
from PyQt5.QtWidgets import (QComboBox, QToolBar, QLabel,
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget,
    QPlainTextEdit, QSplitter, QDialog, QTextEdit, QShortcut, QToolButton, QButtonGroup,
    QHeaderView, QSizePolicy, QLineEdit, QCheckBox
)

# ---- globals & GC ----
warsaw = pytz.timezone('Europe/Warsaw')
gc.set_threshold(500, 10, 10)

try:
    import objgraph
except ImportError:
    objgraph = None


def run_forever(target, *args, **kwargs):
    """Run target forever, restarting on exception (simple resiliency for threads)."""
    while True:
        try:
            target(*args, **kwargs)
        except Exception as e:
            logging.error(f"Fatal error in thread {target}: {e}", exc_info=True)
            time.sleep(3)


class DebugRamDialog(QDialog):
    def __init__(self, parent=None, text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("DEBUG RAM / Objects in Memory")
        layout = QVBoxLayout(self)
        self.textedit = QTextEdit(readOnly=True)
        self.textedit.setPlainText(text)
        layout.addWidget(self.textedit)
        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)

    def accept(self):
        super().accept()
        gc.collect()


# === Project imports ===
from gui.plot_widget import PlotWidget
from gui.trades_table import TradesTable
from gui.global_stats_widget import GlobalStatsWidget
from gui.performance_widget import PerformanceWidget
from gui.controls import Controls
from gui.ticks_table import TicksTable
from gui.indicators_table import IndicatorsTable
from gui.test_worker import StrategyTestWorker
from gui.chart_worker import ChartDataWorker

from livefetcher import LiveFetcher
from data.db_pg import Database
from config import (
    AVAILABLE_PAIRS, DEFAULT_FETCH_INTERVAL,
    MAX_PLOT_CANDLES, MAX_GUI_TRADES,
    GUI_SOFT_RESTART_RAM_MB, GUI_KILL_RAM_MB, GUI_MAX_SOFT_RESTARTS,
    LAYOUT_DEFAULT, LAYOUT_LIMITS, MAINWINDOW_MIN_WIDTH, MAINWINDOW_MIN_HEIGHT,
    PLOT_AGGREGATION, PLOT_DYNAMIC_AGG_ENABLED,
)
from backtester.strategies.rsi import RSIStrategy
from backtester.strategies.ma import MAStrategy


STRATEGY_MAP = {"RSI": RSIStrategy, "MA": MAStrategy}
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")


class QtLogHandler(logging.Handler, QObject):
    """Qt-safe logging handler that appends to a QPlainTextEdit on the GUI thread."""
    log_signal = pyqtSignal(str)

    def __init__(self, widget: QPlainTextEdit):
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self.widget = widget
        self.log_signal.connect(self._write_log)

    def emit(self, record):
        try:
            self.log_signal.emit(self.format(record))
        except Exception:
            pass

    def _write_log(self, msg: str):
        try:
            self.widget.appendPlainText(msg)
        except Exception:
            pass


class MainWindow(QMainWindow):

    def _backfill_test_metadata(self, test_id, symbols=None):
        """
        Po teście: zapisz symbols/start_date/end_date/candle_interval wyliczone z realnego zakresu wskaźników.
        """
        import logging
        from db_helpers import infer_candle_interval_seconds, seconds_to_interval_label
        if not test_id or not getattr(self, "db", None):
            return

        syms = list(symbols or getattr(self, "_last_test_symbols", []) or [])
        start_dt = end_dt = None

        # 1) min/max z indicators_historical
        with pg_conn(self.db) as conn:
            if syms:
                placeholders = ",".join(["%s"] * len(syms))
                row = conn.execute(
                    f"SELECT MIN(close_time), MAX(close_time) FROM public.indicators_historical WHERE symbol IN ({placeholders})",
                    tuple(syms),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MIN(close_time), MAX(close_time) FROM public.indicators_historical").fetchone()
            if row and (row[0] or row[1]):
                start_dt, end_dt = row[0], row[1]

        # 2) fallback z candles
        if start_dt is None or end_dt is None:
            with pg_conn(self.db) as conn:
                if syms:
                    placeholders = ",".join(["%s"] * len(syms))
                    row = conn.execute(
                        f"SELECT MIN(close_time), MAX(close_time) FROM public.candles WHERE symbol IN ({placeholders})",
                        tuple(syms)
                    ).fetchone()
                else:
                    row = conn.execute("SELECT MIN(close_time), MAX(close_time) FROM public.candles").fetchone()
                if row and (row[0] or row[1]):
                    start_dt = start_dt or row[0]
                    end_dt = end_dt or row[1]

        # 3) infer interval z candles (ostatnie 1000 zamknięć)
        with pg_conn(self.db) as conn:
            if syms:
                placeholders = ",".join(["%s"] * len(syms))
                res = conn.execute(
                    f"""SELECT close_time FROM public.candles
                        WHERE symbol IN ({placeholders})
                        ORDER BY close_time DESC
                        LIMIT 1000
                    """,
                    tuple(syms),
                )
            else:
                res = conn.execute(
                    """SELECT close_time FROM public.candles
                       ORDER BY close_time DESC
                       LIMIT 1000
                    """
                )
            sample_times = [r[0] for r in res.fetchall()]

        sec = infer_candle_interval_seconds(sample_times, default=60)
        interval_label = seconds_to_interval_label(sec) if sec else None

        # 4) zapis metadanych testu
        try:
            self.db.upsert_test_config_metadata(int(test_id), symbols=syms, start_date=start_dt, end_date=end_dt,
                                                candle_interval=interval_label)
        except Exception:
            from db_helpers import upsert_test_config_metadata as _u
            _u(self.db.dsn, int(test_id), syms, start_dt, end_dt, interval_label)

    def __init__(self, db, db_queue, parent=None):
        super().__init__(parent)
        self.db = db
        self.db_queue = db_queue
        self.live_fetchers = {}
        self.live_fetchers_lock = threading.Lock()

        self.setWindowTitle("LIVE Trading Strategy Tester")
        self.resize(1600, 900)
        self.setMinimumSize(MAINWINDOW_MIN_WIDTH, MAINWINDOW_MIN_HEIGHT)
        self.setStyleSheet("""
            QWidget { background-color: #232629; color: #F0F0F0; font-size: 13px; }
            QHeaderView::section { background-color: #31343b; color: #F0F0F0; }
            QTableWidget, QTableView { background-color: #181a1d; color: #F0F0F0; gridline-color: #2b2e33; }
            QTableWidget { alternate-background-color: #22252a; }
            QTableView   { alternate-background-color: #22252a; }
            QTableWidget::item:selected, QTableView::item:selected { background: #2f3640; color: #ffffff; }
            QPushButton { background: #31343b; border-radius: 6px; color: #fff; padding: 4px 16px; }
            QPushButton:hover { background: #43454e; }
            QTabBar::tab {
                background: #232629; color: #F0F0F0; border: 1px solid #2c2e31;
                min-width: 100px; min-height: 24px; margin: 2px;
            }
            QTabBar::tab:selected, QTabBar::tab:hover { background: #32343a; color: #fffdde; font-weight: bold; }
            QTabWidget::pane { border: 1.5px solid #44464c; background: #232629; }
        """)

        # --- Controls + core widgets
        self.controls = Controls()
        self.engine = None
        # Flags controlling behaviour after a completed test:
        # - post_test_mode: set to True once a strategy test finishes
        # - prefer_db_indicators: set to True on first symbol change after test;
        #                         forces indicators (incl. TP/SL/TS) to be loaded from DB
        self.post_test_mode = False
        self.prefer_db_indicators = False
        self.strategy_class = RSIStrategy  # default

        self.plot_widget = PlotWidget()

        # --- Keyboard shortcuts for subchart switching (application-wide) ---
        try:
            self.sc_sub1 = QShortcut(QKeySequence("Shift+1"), self)
            self.sc_sub1.setContext(Qt.ApplicationShortcut)
            self.sc_sub1.activated.connect(lambda: (self.plot_widget.cycle_sub_slot(1, +1), self._refresh_subchart_switchers()))

            self.sc_sub2 = QShortcut(QKeySequence("Shift+2"), self)
            self.sc_sub2.setContext(Qt.ApplicationShortcut)
            self.sc_sub2.activated.connect(lambda: (self.plot_widget.cycle_sub_slot(2, +1), self._refresh_subchart_switchers()))

            self.sc_sub3 = QShortcut(QKeySequence("Shift+3"), self)
            self.sc_sub3.setContext(Qt.ApplicationShortcut)
            self.sc_sub3.activated.connect(lambda: (self.plot_widget.cycle_sub_slot(3, +1), self._refresh_subchart_switchers()))
        except Exception:
            pass
        self.performance_widget = PerformanceWidget()
        self.trades_table = TradesTable()
        self.ticks_table = TicksTable()
        self.indicators_table = IndicatorsTable()
        self.metrics_label = QLabel("RAM: -   CPU: -")

        self.global_performance_widget = GlobalStatsWidget()
        self.global_trades_table = TradesTable()

        # powiązanie Controls -> PlotWidget
        self.controls.attach_plot_widget(self.plot_widget)
        self.controls.indicatorsVisibilityChanged.connect(self.plot_widget.apply_indicator_visibility)

        # jump-to-trade (hotkey "J")
        try:
            if hasattr(self.performance_widget, "auto_wire_from_parent"):
                self.performance_widget.auto_wire_from_parent(self)
            if hasattr(self.performance_widget, "jump_to_trade"):
                self.performance_widget.jump_to_trade.connect(self.on_jump_to_trade, Qt.QueuedConnection)
            self._jump_sc = QShortcut(QKeySequence("J"), self)
            self._jump_sc.setContext(Qt.ApplicationShortcut)
            if hasattr(self.performance_widget, "_emit_from_focused_table"):
                self._jump_sc.activated.connect(self.performance_widget._emit_from_focused_table)
        except Exception as e:
            logging.debug(f"[MainWindow] jump-to-trade wiring: {e}")

        # --- logging widget
        self.log_widget = QPlainTextEdit(readOnly=True)
        self.log_widget.setMaximumHeight(160)
        try:
            self.log_widget.document().setMaximumBlockCount(20000)
        except Exception:
            pass
        self._qt_log_handler = QtLogHandler(self.log_widget)
        self._qt_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(self._qt_log_handler)

        # --- Top (controls + layout selector)
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.controls)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self._build_layout_selector())
        controls_layout.addSpacing(8)
        main_layout.addLayout(controls_layout, stretch=0)

        # --- Central body (dynamic splitters)
        self._body_area = QWidget()
        self._body_layout = QVBoxLayout(self._body_area)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self._body_area, stretch=1)

        # --- Tabs po prawej (re-używane)
        self._right_tabs = QTabWidget()
        self._right_tabs.setMinimumWidth(250)
        # allow full expansion with splitter
        self._right_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._right_tabs.addTab(self.global_performance_widget, "Global Stats")
        self._right_tabs.addTab(self.global_trades_table, "Global Trades")
        self._right_tabs.addTab(self.performance_widget, "Statystyki")
        self._right_tabs.addTab(self.trades_table, "Transakcje")
        self._right_tabs.addTab(self.ticks_table, "Ticki")
        self._right_tabs.addTab(self.indicators_table, "Wskaźniki")

        # --- Inicjalny preset layoutu
        self.current_layout = LAYOUT_DEFAULT
        self._apply_layout(self.current_layout)

        # --- Central widget
        self.setCentralWidget(main_widget)
        self._build_indicator_toolbar()

        # --- Timery i wątki
        self.refresh_interval = 5
        self.refresh_timer = QTimer()
        self.refresh_timer.setInterval(self.refresh_interval * 1000)

        self.metrics_timer = QTimer()
        self.metrics_timer.setInterval(2000)
        self.metrics_timer.timeout.connect(self.update_metrics)
        self.metrics_timer.start()

        self.controls.debug_ram_btn.clicked.connect(self.on_debug_ram)

        self.current_symbol = self.controls.pair_box.currentText()
        self.current_trades = []

        # DB Writer thread (odciążenie GUI)
        self.db_writer_stop_event = threading.Event()
        self.db_writer_thread = threading.Thread(target=self.db_writer, daemon=True)
        self.db_writer_thread.start()

        # licznik restartów GUI
        self._gui_restart_count = 0
        self._last_soft_restart = 0
        self._gui_soft_restart_paused = False

        # worker wykresu per symbol
        self._chart_worker = None
        self._start_chart_worker_for_symbol()

        # Podpięcia kontrolek
        self.controls.pull_btn.clicked.connect(self.start_pulling_data)
        self.controls.test_btn.clicked.connect(self.test_strategy)
        # Symbol change handler:
        # - before any test or before first symbol change after test, behaviour is identical
        # - after a test finishes, first symbol change will switch indicators loading to DB-only mode
        self.controls.pair_box.currentIndexChanged.connect(self._on_symbol_changed)

        # Uporządkuj kolumny tabel po starcie (kilka prób po załadowaniu danych)
        self._reorder_attempts = 0

        def _attempt_order():
            if self._enforce_trade_table_columns():
                return
            self._reorder_attempts += 1
            if self._reorder_attempts < 10:
                QTimer.singleShot(1000, _attempt_order)

        QTimer.singleShot(1500, _attempt_order)

        # =========================
        # Layout helpers (WSZYSTKO wewnątrz klasy)
        # =========================
        try:
            if attach_live_cadence is not None:
                # Defer until event loop to ensure widgets are created; fetchers mogą pojawić się później (po kliknięciu start)
                try:
                    from PyQt5 import QtCore as _QtCore  # or PySide2
                except Exception:
                    from PySide2 import QtCore as _QtCore  # fallback
                _QtCore.QTimer.singleShot(0, lambda: attach_live_cadence(self))
        except Exception as _e:
            import logging as _logging
            _logging.error("[MainWindow] attach_live_cadence failed: %s", _e, exc_info=True)

    def _build_layout_selector(self) -> QWidget:
        """Trzy mini-ikonki layoutów, jak w makietach."""
        def make_icon(kind: int) -> QIcon:
            pix = QPixmap(42, 28)
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            pen = QPen(Qt.white)
            pen.setWidth(2)
            p.setPen(pen)
            p.drawRoundedRect(1, 1, 40, 26, 6, 6)
            if kind == 1:
                p.drawLine(21, 2, 21, 26)
                p.drawLine(2, 18, 40, 18)
            elif kind == 2:
                p.drawLine(2, 12, 40, 12)
                p.drawLine(21, 12, 21, 26)
            else:
                p.drawLine(21, 2, 21, 26)
                p.drawLine(21, 16, 40, 16)
            p.end()
            return QIcon(pix)

        host = QWidget()
        lay = QHBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._layout_btn_group = QButtonGroup(host)
        self._layout_btn_group.setExclusive(True)

        for i in (1, 2, 3):
            btn = QToolButton(host)
            btn.setCheckable(True)
            btn.setIcon(make_icon(i))
            btn.setIconSize(QSize(42, 28))
            btn.setToolTip(f"Layout #{i}")
            self._layout_btn_group.addButton(btn, i)
            lay.addWidget(btn)

        # zaznacz domyślny
        self._layout_btn_group.button(LAYOUT_DEFAULT).setChecked(True)
        self._layout_btn_group.buttonClicked[int].connect(self._on_layout_changed)
        return host

    def _on_layout_changed(self, idx: int):
        self.current_layout = idx
        self._apply_layout(idx)

    def _is_ancestor(self, possible_ancestor: QWidget, child: QWidget) -> bool:
        """Sprawdza czy `possible_ancestor` jest przodkiem `child`."""
        if possible_ancestor is None or child is None:
            return False
        p = child.parentWidget()
        while p is not None:
            if p is possible_ancestor:
                return True
            p = p.parentWidget()
        return False

    def _detach_persistent_from(self, container: QWidget):
        """Odczep stałe widżety zanim usuniemy kontener/splitter."""
        for persistent in (self.plot_widget, self.metrics_label, self._right_tabs, self.log_widget):
            try:
                if persistent is container:
                    persistent.setParent(None)
                elif self._is_ancestor(container, persistent):
                    persistent.setParent(None)
            except Exception:
                pass

    def _clear_body(self):
        """Czyści _body_layout tak, by nie usuwać stałych widżetów (plot/tabs/logi)."""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            self._detach_persistent_from(w)
            if w in (self.plot_widget, self.metrics_label, self._right_tabs, self.log_widget):
                w.setParent(None)
                continue
            w.setParent(None)
            w.deleteLater()

    def _make_charts_container(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(self.plot_widget, 7)
        l.addWidget(self.metrics_label, 0)
        limits = LAYOUT_LIMITS[self.current_layout]['charts']
        if limits.get('min_w'):
            w.setMinimumWidth(limits['min_w'])
        if limits.get('min_h'):
            w.setMinimumHeight(limits['min_h'])
        return w

    def _make_widgets_container(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(self._right_tabs)
        limits = LAYOUT_LIMITS[self.current_layout]['widgets']
        if limits.get('min_w'):
            w.setMinimumWidth(limits['min_w'])
        if limits.get('min_h'):
            w.setMinimumHeight(limits['min_h'])
        return w

    def _logs_widget(self) -> QWidget:
        """Zwraca *stały* widget logów z paskiem wyszukiwania."""
        # bezpiecznik – jeśli ktoś skasował
        try:
            _ = self.log_widget.metaObject()
        except RuntimeError:
            # odtworzenie
            self.log_widget = QPlainTextEdit(readOnly=True)
            self.log_widget.setMaximumHeight(160)
            try:
                self.log_widget.document().setMaximumBlockCount(20000)
            except Exception:
                pass
            self._qt_log_handler = QtLogHandler(self.log_widget)
            self._qt_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(self._qt_log_handler)

        limits = LAYOUT_LIMITS[self.current_layout]['logs']
        if limits.get('min_w'):
            self.log_widget.setMinimumWidth(limits['min_w'])
        if limits.get('min_h'):
            self.log_widget.setMinimumHeight(limits['min_h'])
        self.log_widget.setMaximumHeight(16777215)

        # pasek wyszukiwania
        bar = QWidget()
        h = QHBoxLayout(bar); h.setContentsMargins(0, 0, 0, 0)
        self._log_search = QLineEdit(placeholderText="Szukaj w logach…")
        self._log_case = QCheckBox("Aa")
        self._log_prev = QPushButton("Prev"); self._log_prev.setAutoDefault(False)
        self._log_next = QPushButton("Next"); self._log_next.setAutoDefault(False)
        h.addWidget(self._log_search, 1); h.addWidget(self._log_case); h.addWidget(self._log_prev); h.addWidget(self._log_next)

        # sygnały -> lambdy
        try: self._log_next.clicked.disconnect()
        except Exception: pass
        try: self._log_prev.clicked.disconnect()
        except Exception: pass
        try: self._log_search.returnPressed.disconnect()
        except Exception: pass

        self._log_next.clicked.connect(lambda: self._log_find(False))
        self._log_prev.clicked.connect(lambda: self._log_find(True))
        self._log_search.returnPressed.connect(lambda: self._log_find(False))

        host = QWidget()
        v = QVBoxLayout(host); v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(bar); v.addWidget(self.log_widget)
        return host

    def _apply_layout(self, mode: int):
        self._clear_body()

        charts = self._make_charts_container()
        widgets = self._make_widgets_container()
        logs = self._logs_widget()

        if mode == 1:
            # charts | widgets  +  logs na dole
            hsplit = QSplitter(Qt.Horizontal)
            hsplit.addWidget(charts)
            hsplit.addWidget(widgets)
            hsplit.setChildrenCollapsible(True)
            ds = LAYOUT_LIMITS[1]['default_sizes'].get('h', [1100, 450])
            hsplit.setSizes(ds)

            vroot = QSplitter(Qt.Vertical)
            vroot.addWidget(hsplit)
            vroot.addWidget(logs)
            vroot.setChildrenCollapsible(True)
            ds_v = LAYOUT_LIMITS.get(1, {}).get('default_sizes', {}).get('v_root', [800, 200])
            vroot.setSizes(ds_v)

            self._body_layout.addWidget(vroot, 1)

            hsplit.splitterMoved.connect(self._enforce_visibility)
            hsplit.splitterMoved.connect(lambda *a: self._refresh_tables_auto_resize())
            vroot.splitterMoved.connect(self._enforce_visibility)
            vroot.splitterMoved.connect(lambda *a: self._refresh_tables_auto_resize())

        elif mode == 2:
            # góra: charts; dół: logs | widgets
            bottom = QSplitter(Qt.Horizontal)
            bottom.addWidget(logs)
            bottom.addWidget(widgets)
            bottom.setChildrenCollapsible(True)
            ds_h = LAYOUT_LIMITS[2]['default_sizes'].get('h_bottom', [800, 500])
            bottom.setSizes(ds_h)

            vsplit = QSplitter(Qt.Vertical)
            vsplit.addWidget(charts)
            vsplit.addWidget(bottom)
            vsplit.setChildrenCollapsible(True)
            ds_v = LAYOUT_LIMITS[2]['default_sizes'].get('v', [800, 300])
            vsplit.setSizes(ds_v)

            self._body_layout.addWidget(vsplit, 1)

            bottom.splitterMoved.connect(self._enforce_visibility)
            bottom.splitterMoved.connect(lambda *a: self._refresh_tables_auto_resize())
            vsplit.splitterMoved.connect(self._enforce_visibility)
            vsplit.splitterMoved.connect(lambda *a: self._refresh_tables_auto_resize())

        else:
            # charts | (widgets nad logs)
            rightcol = QSplitter(Qt.Vertical)
            rightcol.addWidget(widgets)
            rightcol.addWidget(logs)
            rightcol.setChildrenCollapsible(True)
            ds_v = LAYOUT_LIMITS[3]['default_sizes'].get('v_right', [500, 300])
            rightcol.setSizes(ds_v)

            hsplit = QSplitter(Qt.Horizontal)
            hsplit.addWidget(charts)
            hsplit.addWidget(rightcol)
            hsplit.setChildrenCollapsible(True)
            ds_h = LAYOUT_LIMITS[3]['default_sizes'].get('h', [1100, 450])
            hsplit.setSizes(ds_h)

            self._body_layout.addWidget(hsplit, 1)

            rightcol.splitterMoved.connect(self._enforce_visibility)
            rightcol.splitterMoved.connect(lambda *a: self._refresh_tables_auto_resize())
            hsplit.splitterMoved.connect(self._enforce_visibility)
            hsplit.splitterMoved.connect(lambda *a: self._refresh_tables_auto_resize())

        QTimer.singleShot(0, self._enforce_visibility)

    def _enforce_visibility(self, *args):
        """Ukryj panel, gdy zostanie ściśnięty poniżej minimalnych rozmiarów."""
        try:
            limits = LAYOUT_LIMITS[self.current_layout]
            if self.log_widget and self.log_widget.parent():
                lw, lh = self.log_widget.width(), self.log_widget.height()
                self.log_widget.setVisible(lw >= limits['logs'].get('min_w', 0) and
                                           lh >= limits['logs'].get('min_h', 0))
            tabs_parent = self._right_tabs.parentWidget()
            if tabs_parent:
                tw, th = tabs_parent.width(), tabs_parent.height()
                tabs_parent.setVisible(tw >= limits['widgets'].get('min_w', 0) and
                                       th >= limits['widgets'].get('min_h', 0))
        except Exception:
            pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._enforce_visibility()
        self._refresh_tables_auto_resize()

    # =========================
    # POST - TEST DB WRITER (FROM RAM)
    # =========================
    def _posttest_write_indicators_from_ram(self, engine):
        """
        Po zakończeniu testu: jeśli WRITE_INDICATORS_TO_DB=False, a
        WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST=True, to zrzucamy wskaźniki
        z RAM (engine.indicators_by_symbol) do indicators_historical przez db_queue.

        Robimy to w batchach, żeby nie zabić DB jednym ogromnym insertem.
        """
        import logging
        import math
        import pandas as pd

        try:
            from config import (
                WRITE_INDICATORS_TO_DB,
                WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST,
                INDICATOR_FLUSH_ROWS,
            )
        except Exception as e:
            logging.error("[POST_TEST_WRITE] config import failed: %s", e, exc_info=True)
            return

        # Jeśli klasyczny tryb jest włączony, to nic nie robimy – wskaźniki już są w DB.
        if WRITE_INDICATORS_TO_DB or not WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST:
            logging.debug(
                "[POST_TEST_WRITE] skipped (WRITE_INDICATORS_TO_DB=%s, WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST=%s)",
                WRITE_INDICATORS_TO_DB,
                WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST,
            )
            return

        ind_map = getattr(engine, "indicators_by_symbol", None) or {}
        if not isinstance(ind_map, dict) or not ind_map:
            logging.warning("[POST_TEST_WRITE] no indicators_by_symbol on engine – nothing to write")
            return

        # Nazwy kolumn wskaźników – używamy z engine, a jak nie ma, to infer z DF.
        indicator_names = list(getattr(engine, "indicator_names", []) or [])

        # Rozmiar batcha – możemy użyć tego samego co flush podczas testu
        batch_size = int(getattr(__import__("config"), "INDICATOR_FLUSH_ROWS", 10000) or 10000)

        # Kolumny, których nie traktujemy jako wskaźniki
        reserved_lower = {
            "symbol",
            "open_time",
            "close_time",
            "close",
            "close_price",
            "timestamp",
            "inserted_at",
        }

        from backtester.engine import INDICATORS_TABLE  # 'indicators_historical'

        logging.info(
            "[POST_TEST_WRITE] start – symbols=%r, batch_size=%d",
            list(ind_map.keys()),
            batch_size,
        )

        for symbol, df in ind_map.items():
            try:
                if df is None:
                    continue
                # Upewniamy się, że mamy DataFrame
                if not isinstance(df, pd.DataFrame):
                    df_local = pd.DataFrame(df)
                else:
                    df_local = df.copy()
            except Exception as e:
                logging.error("[POST_TEST_WRITE] %s: cannot coerce to DataFrame: %s", symbol, e, exc_info=True)
                continue

            if df_local.empty:
                continue

            # symbol jako kolumna
            if "symbol" not in df_local.columns:
                df_local["symbol"] = symbol

            # close_time jako kolumna – index już powinien nim być
            if "close_time" not in df_local.columns:
                df_local["close_time"] = df_local.index

            # Upewnij się, że index jest DatetimeIndex (potrzebne do timestamp)
            if not isinstance(df_local.index, pd.DatetimeIndex):
                try:
                    df_local.index = pd.to_datetime(df_local["close_time"], errors="coerce")
                except Exception:
                    pass

            # Jeśli indicator_names puste – spróbuj wyciągnąć z DF
            if not indicator_names:
                indicator_names = [
                    c for c in df_local.columns
                    if c.lower() not in reserved_lower
                ]

            n = len(df_local)
            total_batches = int(math.ceil(n / float(batch_size)))

            for batch_idx, start in enumerate(range(0, n, batch_size), start=1):
                end = min(start + batch_size, n)
                chunk = df_local.iloc[start:end]
                rows = []

                for idx_ts, row in chunk.iterrows():
                    row_dict = row.to_dict()

                    # Cena zamknięcia
                    price_raw = (
                        row_dict.get("close")
                        or row_dict.get("CLOSE")
                        or row_dict.get("close_price")
                        or row_dict.get("CLOSE_PRICE")
                    )
                    try:
                        price = float(price_raw)
                    except Exception:
                        continue

                    payload = {
                        "symbol": row_dict.get("symbol", symbol),
                        "close_time": row_dict.get("close_time", idx_ts),
                        "close_price": price,
                    }

                    # timestamp = close_time (UTC) w sekundach
                    try:
                        ct = row_dict.get("close_time", idx_ts)
                        cts = pd.Timestamp(ct)
                        if cts.tzinfo is None:
                            cts = cts.tz_localize("UTC")
                        else:
                            cts = cts.tz_convert("UTC")
                        payload["timestamp"] = int(cts.value // 10 ** 9)
                    except Exception:
                        pass

                    # Wskaźniki numeryczne
                    for name in indicator_names:
                        if not name:
                            continue
                        # nie nadpisujemy bazowych pól
                        if name.lower() in reserved_lower:
                            continue
                        v = row_dict.get(name)
                        if v is None:
                            continue
                        try:
                            fv = float(v)
                        except Exception:
                            continue
                        if math.isnan(fv):
                            continue
                        payload[name] = fv

                    if payload:
                        rows.append(payload)

                if not rows:
                    continue

                try:
                    self.db_queue.put({
                        "type": "insert_indicator_rows",
                        "table_name": INDICATORS_TABLE,
                        "rows": rows,
                        "indicator_names": indicator_names,
                    })
                except Exception as e:
                    logging.error(
                        "[POST_TEST_WRITE] %s: enqueue batch %d/%d failed: %s",
                        symbol, batch_idx, total_batches, e, exc_info=True
                    )
                    continue

                logging.info(
                    "[POST_TEST_WRITE] %s: enqueued batch %d/%d (rows=%d)",
                    symbol, batch_idx, total_batches, len(rows),
                )

        logging.info("[POST_TEST_WRITE] enqueue finished")

    # =========================
    # DB WRITER (FIXED)
    # =========================

    def db_writer(self):
        """DB writer consuming tasks from db_queue.

        Obsługuje m.in.:
          - replace_stats_rows
          - insert_candle
          - upsert_live_candle
          - insert_indicator_rows
          - insert_trade_rows / insert_trade
          - insert_ticks
          - update_test_meta (best-effort: log only)
        """
        while True:
            try:
                while not self.db_writer_stop_event.is_set():
                    try:
                        task = self.db_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    try:
                        t = task.get("type")
                        if not t:
                            logging.debug("[DB_WRITER] Task bez 'type': %r", task)
                            continue

                        # --- statystyki globalne / per-symbol ---
                        if t == "replace_stats_rows":
                            rows = task.get("rows") or []

                            # priorytet:
                            # 1) test_id z taska (jeśli ktoś poda)
                            # 2) self.current_test_id (bieżący test w GUI)
                            # 3) self.db.current_test_id (fallback, jeśli ktoś tam ustawia)
                            test_id = task.get("test_id")
                            if not test_id:
                                test_id = getattr(self, "current_test_id", None) or getattr(
                                    self.db, "current_test_id", None
                                )

                            if not rows:
                                logging.debug("[DB_WRITER] replace_stats_rows skipped (no rows)")
                            elif test_id is None:
                                logging.warning(
                                    "[DB_WRITER] replace_stats_rows skipped (no test_id; rows=%d)",
                                    len(rows),
                                )
                            else:
                                try:
                                    self.db.replace_stats_rows(rows, test_id=int(test_id))
                                except Exception as e:
                                    logging.error(
                                        "[DB_WRITER] replace_stats_rows failed: %s",
                                        e,
                                        exc_info=True,
                                    )


                        # --- zapis świec (głównie live) ---
                        elif t == "insert_candle":
                            try:
                                symbol = task["symbol"]
                                candle = task["closed_candle"]
                                if hasattr(self.db, "insert_candle"):
                                    self.db.insert_candle(symbol, candle)
                                else:
                                    logging.debug(
                                        "[DB_WRITER] insert_candle ignored (no handler on db)"
                                    )
                            except Exception as e:
                                logging.error(
                                    "[DB_WRITER] insert_candle failed: %s", e, exc_info=True
                                )

                        elif t == "upsert_live_candle":
                            try:
                                symbol = task["symbol"]
                                candle = task["candle"]
                                if hasattr(self.db, "upsert_live_candle"):
                                    self.db.upsert_live_candle(symbol, candle)
                                elif hasattr(self.db, "insert_candle"):
                                    self.db.insert_candle(symbol, candle)
                                else:
                                    logging.debug(
                                        "[DB_WRITER] upsert_live_candle ignored (no handler on db)"
                                    )
                            except Exception as e:
                                logging.error(
                                    "[DB_WRITER] upsert_live_candle failed: %s", e, exc_info=True
                                )

                        # --- wskaźniki: batch ---
                        elif t == "insert_indicator_rows":
                            table_name = (
                                task.get("table_name")
                                or task.get("table")
                                or "indicators_historical"
                            )
                            rows = task.get("rows") or []
                            indicator_names = task.get("indicator_names")
                            if not rows:
                                logging.debug(
                                    "[DB_WRITER] insert_indicator_rows skipped (no rows)"
                                )
                            else:
                                try:
                                    self.db.insert_indicator_rows(
                                        table_name,
                                        rows,
                                        indicator_names=indicator_names,
                                    )
                                except Exception as e:
                                    logging.error(
                                        "[DB_WRITER] insert_indicator_rows failed: %s",
                                        e,
                                        exc_info=True,
                                    )

                        # --- trejdy z testów: batch ---
                        elif t == "insert_trade_rows":
                            rows = task.get("rows") or []
                            if not rows:
                                logging.debug(
                                    "[DB_WRITER] insert_trade_rows skipped (no rows)"
                                )
                            else:
                                try:
                                    self.db.insert_trade_rows(rows)
                                except Exception as e:
                                    logging.error(
                                        "[DB_WRITER] insert_trade_rows failed: %s",
                                        e,
                                        exc_info=True,
                                    )

                        # --- pojedynczy trejd (na wszelki wypadek) ---
                        elif t == "insert_trade":
                            trade = task.get("trade")
                            if not trade:
                                logging.debug(
                                    "[DB_WRITER] insert_trade skipped (no trade)"
                                )
                            else:
                                try:
                                    self.db.insert_trade_rows([trade])
                                except Exception as e:
                                    logging.error(
                                        "[DB_WRITER] insert_trade failed: %s",
                                        e,
                                        exc_info=True,
                                    )

                        # --- ticki wokół trejdów (opcjonalne) ---
                        elif t == "insert_ticks":
                            ticks = task.get("ticks_list") or task.get("ticks") or []
                            if not ticks:
                                logging.debug("[DB_WRITER] insert_ticks skipped (no ticks)")
                            elif hasattr(self.db, "insert_ticks"):
                                try:
                                    self.db.insert_ticks(ticks)
                                except Exception as e:
                                    logging.error(
                                        "[DB_WRITER] insert_ticks failed: %s",
                                        e,
                                        exc_info=True,
                                    )
                            else:
                                logging.debug(
                                    "[DB_WRITER] insert_ticks ignored (no handler on db)"
                                )

                        # --- meta testu: na razie tylko log (config zapisujesz gdzie indziej) ---
                        elif t == "update_test_meta":
                            try:
                                meta_preview = {
                                    "symbols": task.get("symbols"),
                                    "start_date": task.get("start_date"),
                                    "end_date": task.get("end_date"),
                                    "candle_interval": task.get("candle_interval"),
                                }
                                logging.debug("[DB_WRITER] update_test_meta: %r", meta_preview)
                            except Exception:
                                # log pomocniczy – nie zabijamy wątku
                                pass

                        else:
                            logging.debug(
                                "[DB_WRITER] Unknown task type: %s payload keys=%s",
                                t,
                                list(task.keys()),
                            )

                    except Exception as e:
                        logging.error(
                            "[DB_WRITER] task handling error: %s", e, exc_info=True
                        )

                # stop flag set → krótka pauza
                time.sleep(0.05)

            except Exception as e:
                logging.error("[DB_WRITER] loop error: %s", e, exc_info=True)
                time.sleep(0.5)


    def closeEvent(self, event):
        try:
            root_logger = logging.getLogger()
            if getattr(self, "_qt_log_handler", None) is not None:
                try:
                    root_logger.removeHandler(self._qt_log_handler)
                except Exception:
                    pass
                try:
                    self._qt_log_handler.log_signal.disconnect(self._qt_log_handler._write_log)
                except Exception:
                    pass
                try:
                    self._qt_log_handler.close()
                except Exception:
                    pass
                self._qt_log_handler = None
        except Exception:
            pass
        self.db_writer_stop_event.set()
        try:
            self.db_writer_thread.join(timeout=2)
        except Exception:
            pass
        super().closeEvent(event)

    # =========================
    # CLEAR / PULL / FETCH
    # =========================
    def clear_data(self):
        self.db.clear()
        self.engine = None
        logging.warning("Database cleared.")
        self.plot_widget.update_chart(None, [])
        self.performance_widget.update_stats([])
        self.trades_table.update_trades([])
        self.ticks_table.update_ticks(None)
        self.indicators_table.update_indicators(None)
        self.global_performance_widget.update_stats([])
        self.global_trades_table.update_trades([])
        gc.collect()
        try:
            if objgraph:
                print(">>> OBJGRAPH CLEAR_DATA <<<")
                objgraph.show_growth(limit=15)
        except Exception:
            pass

    def start_pulling_data(self):
        def log_all_threads():
            logging.warning("Current threads:")
            for t in threading.enumerate():
                logging.warning(f"Thread: {t.name} ({t.ident}) Alive: {t.is_alive()}")

        if not hasattr(self, 'fetch_threads'):
            self.fetch_threads = {}
        if not hasattr(self, 'fetch_stop_events'):
            self.fetch_stop_events = {}
        if not hasattr(self, 'live_fetchers'):
            self.live_fetchers = {}

        logging.warning("Start pulling data for all pairs...")
        for symbol in AVAILABLE_PAIRS:
            t = self.fetch_threads.get(symbol)
            if t is not None and t.is_alive():
                logging.warning(f"Fetcher thread for {symbol} is already running. Skipping.")
                continue

            stop_event = threading.Event()
            self.fetch_stop_events[symbol] = stop_event
            t = threading.Thread(
                target=run_forever,
                args=(self.fetch_and_store, symbol, stop_event),
                daemon=True,
                name=f"FetcherThread-{symbol}"
            )
            t.start()
            self.fetch_threads[symbol] = t
            logging.info(f"Started fetcher thread for {symbol}")
        log_all_threads()

    def stop_all_fetchers(self):
        for e in getattr(self, 'fetch_stop_events', {}).values():
            e.set()
        self.fetch_threads = {}
        self.fetch_stop_events = {}

    def fetch_and_store(self, symbol, stop_event: threading.Event):
        if not hasattr(self, "fetcher_started"):
            self.fetcher_started = {}
        if self.fetcher_started.get(symbol):
            logging.warning(f"Fetcher for {symbol} already running! Exiting duplicate thread.")
            return
        self.fetcher_started[symbol] = True

        while not stop_event.is_set():
            try:
                # tworzymy fetcher dla danego symbolu (TYLKO RAZ na iterację zewnętrznej pętli)
                fetcher = LiveFetcher(symbol, freq_seconds=DEFAULT_FETCH_INTERVAL)
                self.live_fetchers[symbol] = fetcher
                logging.info(f"Fetcher started for {symbol}")

                # wewnętrzna pętla: pobieranie ticków + update świec
                while not stop_event.is_set():
                    try:
                        price, closed_candle = fetcher.tick()
                    except Exception as e:
                        logging.error(f"Error in fetcher.tick() ({symbol}): {e}", exc_info=True)
                        continue

                    # 1) upsert świecy LIVE do DB (niedomkniętej)
                    try:
                        live_candle = fetcher.get_inprogress_candle()
                        if live_candle:
                            live_candle = dict(live_candle)  # kopia, żeby nic nie zmodyfikować po drodze
                            self.db_queue.put(
                                {
                                    "type": "upsert_live_candle",
                                    "symbol": symbol,
                                    "candle": live_candle,
                                }
                            )
                    except Exception as e:
                        logging.error(
                            f"db_queue put error (upsert_live_candle, {symbol}): {e}",
                            exc_info=True,
                        )

                    # 2) zamknięta świeca -> insert_candle (z dociągniętymi wolumenami)
                    if closed_candle:
                        try:
                            self.db_queue.put(
                                {
                                    "type": "insert_candle",
                                    "symbol": symbol,
                                    "closed_candle": closed_candle,
                                }
                            )
                        except Exception as e:
                            logging.error(
                                f"db_queue put error (insert_candle, {symbol}): {e}",
                                exc_info=True,
                            )

                    time.sleep(fetcher.freq_seconds)

            except Exception as e:
                logging.critical(f"Fatal error in fetch_and_store for {symbol}: {e}", exc_info=True)
                time.sleep(2)

    # =========================
    # STRATEGY TEST
    # =========================
    def test_strategy(self):
        logging.info("Testing strategy on selected data (selected symbol only)")
        try:
            if hasattr(self.controls, "test_btn"):
                self.controls.test_btn.setEnabled(False)
        except Exception:
            pass

        settings = self.controls.get_settings() if hasattr(self, "controls") else {}
        strategy_name = settings.get("strategy")

        bias = None
        low = str(settings.get("bias", "None")).strip().lower()
        if low in ("long", "short"):
            bias = low

        try:
            from gui.controls import STRATEGY_CHOICES
            strategy_class = STRATEGY_CHOICES.get(strategy_name)
        except Exception:
            strategy_class = None
        if strategy_class is None:
            strategy_class = RSIStrategy

        symbols = list(AVAILABLE_PAIRS)
        self._last_test_symbols = list(symbols)
        self.worker = StrategyTestWorker(self.db, self.db_queue, symbols, strategy_class, bias=bias)

        if hasattr(self.worker, "progress") and hasattr(self, "on_test_progress"):
            self.worker.progress.connect(self.on_test_progress, Qt.QueuedConnection)
        if hasattr(self.worker, "progress_text"):
            self.worker.progress_text.connect(self._append_log_to_ui, Qt.QueuedConnection)
        if hasattr(self.worker, "log"):
            self.worker.log.connect(self._append_log_to_ui, Qt.QueuedConnection)
        if hasattr(self.worker, "finished_with_engine"):
            self.worker.finished_with_engine.connect(self.on_test_strategy_finished, Qt.QueuedConnection)

        def _finish_fallback():
            try:
                eng = getattr(self.worker, "engine", None)
                if eng is not None:
                    self.on_test_strategy_finished(eng)
                else:
                    self.update_symbol_view()
                    self._append_log_to_ui("Finished (no engine signal). View refreshed.")
            finally:
                self._on_test_finished()
        self.worker.finished.connect(_finish_fallback, Qt.QueuedConnection)


        # === [TEST_ID] assign new ID on click & persist full config row ===
        try:
            
            # Pure-PG branch: allocate ID + persist config/meta
            settings = self.controls.get_settings() if hasattr(self, "controls") else {}
            strategy_name = settings.get("strategy") or "unknown"
            self.current_test_id = int(self.db.next_free_test_id())
            try:
                symbols = self.selected_symbols if hasattr(self, "selected_symbols") else []
            except Exception:
                symbols = []
            start_dt = getattr(self, "selected_start_dt", None)
            end_dt = getattr(self, "selected_end_dt", None)
            candle_interval = getattr(self, "selected_interval", None)

            # persist meta + config
            self.db.upsert_test_config_metadata(
                test_id=self.current_test_id,
                symbols=symbols,
                start_date=start_dt,
                end_date=end_dt,
                candle_interval=candle_interval,
            )
            self.db.save_run_config(self.current_test_id, strategy_name, settings)
            logging.info(f"[TEST_ID] PG test_id={self.current_test_id} persisted")

        except Exception as e:
            logging.exception(f"[TEST_ID] failed: {e}")
        self.worker.start()

    def on_test_strategy_finished(self, engine):
        """Zakończenie testu: podpięcie engine->UI, backfill metadanych, refresh wykresu, zapis statystyk."""
        import logging
        from PyQt5.QtCore import QTimer

        self.engine = engine

        try:
            inds = getattr(engine, "indicators_by_symbol", None)
            if isinstance(inds, dict):
                logging.warning("[POST_TEST][DEBUG] indicators_by_symbol keys: %r", list(inds.keys()))
        except Exception:
            pass

        # 0) Post-testowy zapis wskaźników z RAM do DB (asynchronicznie, jeśli włączony tryb)
        try:
            import threading
            threading.Thread(
                target=self._posttest_write_indicators_from_ram,
                args=(engine,),
                daemon=True,
            ).start()
        except Exception as e:
            logging.error("[POST_TEST_WRITE] failed to start background worker: %s", e, exc_info=True)

        # 1) Propagacja engine do widgetów
        try:
            self.global_performance_widget.set_engine(self.engine)
        except Exception:
            pass
        self.plot_widget.engine = self.engine
        try:
            self.plot_widget.indicator_names = list(getattr(self.engine, "indicator_names", []) or [])
        except Exception:
            self.plot_widget.indicator_names = None

        # 2) display_config ze strategii (jeśli jest)
        try:
            strat = getattr(self.engine, "strategy", None)
            if strat and hasattr(strat, "get_display_config"):
                self.plot_widget.set_display_config(strat.get_display_config())
        except Exception as e:
            logging.warning(f"[UI] display_config apply failed: {e}")

        # 3) Odblokowanie UI + odśwież listę symboli
        self.controls.test_btn.setEnabled(True)
        self.update_symbol_view()
        logging.info("Test strategy finished")

        # 3a) Oznacz tryb post-testowy: kolejne zmiany symbolu mogą przełączyć
        # ładowanie wskaźników w tryb "tylko z bazy" (prefer_db_indicators=True).
        try:
            self.post_test_mode = True
        except Exception:
            # Błąd w ustawieniu flagi nie powinien blokować UI ani dalszych działań.
            pass

        # 4) Backfill metadanych testu (raz, bez dupli)
        try:
            test_id = getattr(self, "current_test_id", None)
            syms = list(getattr(self, "_last_test_symbols", []) or getattr(self.engine, "symbols", []) or [])
            self._backfill_test_metadata(test_id, syms)
        except Exception as e:
            logging.error(f"_backfill_test_metadata failed: {e}", exc_info=True)

        # 5) Twarde odświeżenie wykresu: preferuj worker, fallback: selektor symbolu
        try:
            sym = getattr(self, "current_symbol", None)
            if not sym and syms:
                sym = syms[0]
                self.current_symbol = sym

            # W niektórych buildach worker jest w self._chart_worker
            cw = getattr(self, "chart_worker", None) or getattr(self, "_chart_worker", None)
            if cw and hasattr(cw, "request_full_refresh"):
                cw.request_full_refresh(symbol=sym)
            elif hasattr(self, "on_symbol_changed") and sym:
                self.on_symbol_changed(sym)
        except Exception as e:
            logging.error(f"[Chart] refresh failed: {e}", exc_info=True)

        # 6) Zbierz transakcje i zasil tabelki
        all_trades = []
        try:
            eng_syms = list(getattr(self.engine, "symbols", []) or [])
            for s in eng_syms:
                if hasattr(self.engine, "get_trades"):
                    tr = self.engine.get_trades(s) or []
                    all_trades.extend([t for t in tr if t])
        except Exception:
            pass

        from collections import defaultdict
        per_symbol_stats = defaultdict(list)
        for t in all_trades:
            if t and t.get("symbol"):
                per_symbol_stats[t["symbol"]].append(t)

        try:
            self.global_performance_widget.update_stats(all_trades, per_symbol_stats)
        except Exception:
            pass
        try:
            self.global_trades_table.update_trades(all_trades)
        except Exception:
            pass

        # 7) Jednorazowe odroczenie zapisu statystyk po repaint (psycopg → %s placeholders)
        try:
            def __save_stats_after_render():
                import logging

                def _to_float(x):
                    try:
                        if x is None: return None
                        s = str(x).replace('%', ' ').replace('—', ' ').replace(',', ' ').strip()
                        return float(s) if s else None
                    except Exception:
                        return None

                if getattr(self, "current_test_id", None) is None:
                    logging.error("[StatsSave] current_test_id is None; skip")
                    return

                rows = {}
                # PnL tab
                try:
                    tbl = self.global_performance_widget.pnl_tab.table
                    headers = [tbl.horizontalHeaderItem(c).text() for c in range(tbl.columnCount())]
                    col = {h: i for i, h in enumerate(headers)}

                    def GP(r, n):
                        it = tbl.item(r, col.get(n, -1))
                        return it.text() if it else ""

                    for r in range(tbl.rowCount()):
                        sym = GP(r, "Symbol")
                        if not sym:
                            continue
                        rows[sym] = {
                            "symbol": sym,
                            "trades": GP(r, "Trades"),
                            "win_rate": GP(r, "Win-rate"),
                            "total_pnl": GP(r, "Suma PnL"),
                            "avg_pnl": GP(r, "Śr. PnL"),
                            "best": GP(r, "Best"),
                            "worst": GP(r, "Worst"),
                            "total_vol_usd": GP(r, "Wolumen (USD)"),
                            "total_fee_usd": GP(r, "Prowizje (USD)"),
                            "avg_win_usd": GP(r, "Śr. zysk $"),
                            "avg_loss_usd": GP(r, "Śr. strata $"),
                            "avg_gain_pct": GP(r, "Śr. zysk %"),
                            "avg_loss_pct": GP(r, "Śr. strata %"),
                            "vwatr_pct": GP(r, "VWATR %"),
                            "roc_pct": GP(r, "ROC %"),
                            "roi_pct": GP(r, "ROI %"),
                        }
                except Exception as e:
                    logging.exception(f"[StatsSave] PnL read failed: {e}")

                # Trades info tab
                try:
                    t2 = self.global_performance_widget.trades_tab.table
                    h2 = [t2.horizontalHeaderItem(c).text() for c in range(t2.columnCount())]
                    m2 = {h: i for i, h in enumerate(h2)}

                    def GT(r, n):
                        it = t2.item(r, m2.get(n, -1))
                        return it.text() if it else ""

                    for r in range(t2.rowCount()):
                        sym = GT(r, "Symbol")
                        if not sym:
                            continue
                        d = rows.setdefault(sym, {"symbol": sym})
                        d.update({
                            "avg_duration": GT(r, "avg_duration"),
                            "min_duration": GT(r, "min_duration"),
                            "max_duration": GT(r, "max_duration"),
                            "avg_price_delta_pct": GT(r, "avg_price_delta%"),
                            "min_price_delta_pct": GT(r, "min_price_delta%"),
                            "max_price_delta_pct": GT(r, "max_price_delta%"),
                            "avg_sl_pct": GT(r, "avg_SL%"),
                            "min_sl_pct": GT(r, "min_SL%"),
                            "max_sl_pct": GT(r, "max_SL%"),
                            "avg_tp_pct": GT(r, "avg_TP%"),
                            "min_tp_pct": GT(r, "min_TP%"),
                            "max_tp_pct": GT(r, "max_TP%"),
                            "avg_tsdist_pct": GT(r, "avg_TSdist%"),
                            "min_tsdist_pct": GT(r, "min_TSdist%"),
                            "max_tsdist_pct": GT(r, "max_TSdist%"),
                        })
                except Exception as e:
                    logging.exception(f"[StatsSave] Trades read failed: {e}")

                # zapis do PG (psycopg3: %s)
                try:
                    # local helper pg_conn jest zdefiniowany w tym module – użyj jego
                    with pg_conn(self.db) as conn:
                        conn.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        test_id INTEGER,
        symbol TEXT,
        trades INTEGER,
        win_rate REAL,
        total_pnl REAL,
        avg_pnl REAL,
        best REAL,
        worst REAL,
        total_vol_usd REAL,
        total_fee_usd REAL,
        avg_win_usd REAL,
        avg_loss_usd REAL,
        avg_gain_pct REAL,
        avg_loss_pct REAL,
        vwatr_pct REAL,
        roc_pct REAL,
        roi_pct REAL,
        avg_duration TEXT,
        min_duration TEXT,
        max_duration TEXT,
        avg_price_delta_pct REAL,
        min_price_delta_pct REAL,
        max_price_delta_pct REAL,
        avg_sl_pct REAL,
        min_sl_pct REAL,
        max_sl_pct REAL,
        avg_tp_pct REAL,
        min_tp_pct REAL,
        max_tp_pct REAL,
        avg_tsdist_pct REAL,
        min_tsdist_pct REAL,
        max_tsdist_pct REAL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (test_id, symbol)
    )""")
                        cnt = conn.execute(
                            "SELECT COUNT(*) FROM stats WHERE test_id = %s",
                            (int(self.current_test_id),)
                        ).fetchone()[0]
                        if cnt == 0 and rows:
                            sql = """
    INSERT INTO stats (
        test_id, symbol,
        trades, win_rate, total_pnl, avg_pnl, best, worst,
        total_vol_usd, total_fee_usd, avg_win_usd, avg_loss_usd,
        avg_gain_pct, avg_loss_pct, vwatr_pct, roc_pct, roi_pct,
        avg_duration, min_duration, max_duration,
        avg_price_delta_pct, min_price_delta_pct, max_price_delta_pct,
        avg_sl_pct, min_sl_pct, max_sl_pct,
        avg_tp_pct, min_tp_pct, max_tp_pct,
        avg_tsdist_pct, min_tsdist_pct, max_tsdist_pct
    ) VALUES (
        %s,%s,
        %s,%s,%s,%s,%s,%s,
        %s,%s,%s,%s,
        %s,%s,%s,%s,%s,
        %s,%s,%s,
        %s,%s,%s,
        %s,%s,%s,
        %s,%s,%s,
        %s,%s,%s
    )"""
                            for r in rows.values():
                                conn.execute(sql, [
                                    int(self.current_test_id), str(r.get("symbol", "")),
                                    _to_float(r.get("trades")), _to_float(r.get("win_rate")),
                                    _to_float(r.get("total_pnl")), _to_float(r.get("avg_pnl")),
                                    _to_float(r.get("best")), _to_float(r.get("worst")),
                                    _to_float(r.get("total_vol_usd")), _to_float(r.get("total_fee_usd")),
                                    _to_float(r.get("avg_win_usd")), _to_float(r.get("avg_loss_usd")),
                                    _to_float(r.get("avg_gain_pct")), _to_float(r.get("avg_loss_pct")),
                                    _to_float(r.get("vwatr_pct")), _to_float(r.get("roc_pct")),
                                    _to_float(r.get("roi_pct")),
                                    r.get("avg_duration"), r.get("min_duration"), r.get("max_duration"),
                                    _to_float(r.get("avg_price_delta_pct")), _to_float(r.get("min_price_delta_pct")),
                                    _to_float(r.get("max_price_delta_pct")),
                                    _to_float(r.get("avg_sl_pct")), _to_float(r.get("min_sl_pct")),
                                    _to_float(r.get("max_sl_pct")),
                                    _to_float(r.get("avg_tp_pct")), _to_float(r.get("min_tp_pct")),
                                    _to_float(r.get("max_tp_pct")),
                                    _to_float(r.get("avg_tsdist_pct")), _to_float(r.get("min_tsdist_pct")),
                                    _to_float(r.get("max_tsdist_pct")),
                                ])
                            logging.info(f"[StatsSave] wrote {len(rows)} rows for test_id={self.current_test_id}")
                        else:
                            logging.info(
                                f"[StatsSave] skip for test_id={self.current_test_id} (cnt={cnt}, rows={len(rows)})")
                except Exception as e:
                    logging.exception(f"[StatsSave] DB write failed: {e}")

            QTimer.singleShot(0, __save_stats_after_render)
        except Exception as e:
            logging.exception(f"[StatsSave] schedule failed: {e}")

        # 8) przełącz na kartę globalną
        parent_tabs = self.global_performance_widget.parent()
        while parent_tabs and not isinstance(parent_tabs, QTabWidget):
            parent_tabs = parent_tabs.parent()
        if isinstance(parent_tabs, QTabWidget):
            parent_tabs.setCurrentWidget(self.global_performance_widget)

    def test_strategy_error(self, msg):
        self.controls.test_btn.setEnabled(True)
        logging.error(f"Test strategy error: {msg}")
        self._append_log_to_ui(str(msg))

    # =========================
    # UI helpers
    # =========================
    def _append_log_to_ui(self, msg: str):
        """Bezpiecznie dopisz linijkę do widżetu logów i przewiń na dół."""
        try:
            if not msg:
                return
            s = msg if isinstance(msg, str) else repr(msg)
            s = s.rstrip("\n")

            lw = getattr(self, "log_widget", None)
            if lw is None:
                return

            lw.appendPlainText(s)

            # przewiń na dół
            try:
                cur = lw.textCursor()
                cur.movePosition(QTextCursor.End)
                lw.setTextCursor(cur)
                lw.ensureCursorVisible()
            except Exception:
                pass
        except Exception as e:
            logging.debug(f"[MainWindow] _append_log_to_ui error: {e}")

    def _log_find(self, backwards: bool = False):
        try:
            if not hasattr(self, "log_widget") or self.log_widget is None:
                return
            if not hasattr(self, "_log_search"):
                return
            pat = self._log_search.text()
            if not pat:
                return

            doc = self.log_widget.document()
            flags = QTextDocument.FindBackward if backwards else QTextDocument.FindFlags()
            if getattr(self, "_log_case", None) and self._log_case.isChecked():
                flags = flags | QTextDocument.FindCaseSensitively

            cur = self.log_widget.textCursor()
            res = doc.find(pat, cur, flags)
            if not res or res.isNull():
                wrap_cur = QTextCursor(doc)
                wrap_cur.movePosition(QTextCursor.End if backwards else QTextCursor.Start)
                res = doc.find(pat, wrap_cur, flags)
                if not res or res.isNull():
                    return  # not found
            self.log_widget.setTextCursor(res)
            self.log_widget.ensureCursorVisible()
        except Exception as e:
            logging.debug(f"[MainWindow] log find error: {e}")
        try:
            if self.log_widget is not None:
                self.log_widget.appendPlainText(str(msg))
        except Exception:
            pass

    def _on_test_finished(self):
        try:
            if hasattr(self.controls, "test_btn"):
                self.controls.test_btn.setEnabled(True)
        except Exception:
            pass
        logging.info("Test finished.")

    def on_test_progress(self, value: int):
        try:
            if hasattr(self, "progress_bar") and self.progress_bar is not None:
                self.progress_bar.setValue(int(value))
            elif hasattr(self, "progress_label") and self.progress_label is not None:
                self.progress_label.setText(f"{int(value)}%")
        except Exception:
            pass

    def on_jump_to_trade(self, trade: dict):
        logging.info(f"[MainWindow] on_jump_to_trade -> {trade}")
        try:
            if hasattr(self.plot_widget, "jump_to_trade"):
                self.plot_widget.jump_to_trade(trade)
        except Exception as e:
            logging.exception(f"[MainWindow] jump slot err: {e}")
        try:
            self.clear_all_trade_levels()
            self._draw_trade_levels(trade)
        except Exception as e:
            logging.debug(f"draw trade levels failed: {e}")

    # =========================
    # Indicators mapping/merge
    # =========================
    def _map_indicators_from_merge(self, df: pd.DataFrame) -> pd.DataFrame:
        """Mapuje wskaźniki strategii deterministycznie do indicator_1..3 (prefer *_ind)."""
        import numpy as np

        if df is None or df.empty:
            return df

        names = []
        if getattr(self, "engine", None) is not None:
            names = getattr(self.engine, "indicator_names", None) or []
        names = list(names[:3])

        # wyczyść stare kolumny
        for c in [f"indicator_{i}" for i in range(1, 4)] + [f"indicator_{i}_name" for i in range(1, 4)]:
            if c in df.columns:
                df.drop(columns=[c], inplace=True, errors="ignore")

        source_used = {}
        for idx, name in enumerate(names, start=1):
            col_out = f"indicator_{idx}"
            candidates = [f"{name}_ind", f"{name}_x", f"{name}_y", name]
            found = next((c for c in candidates if c in df.columns), None)
            if found is not None:
                try:
                    df[col_out] = pd.to_numeric(df[found], errors="coerce")
                except Exception:
                    df[col_out] = df[found]
                source_used[idx] = found
            else:
                df[col_out] = np.nan
                source_used[idx] = None
            df[f"{col_out}_name"] = name

        try:
            nonnan = {i: int(df[f"indicator_{i}"].notna().sum()) if f"indicator_{i}" in df.columns else 0 for i in
                      (1, 2, 3)}
            labels = {i: df[f"indicator_{i}_name"].iloc[0] if f"indicator_{i}_name" in df.columns and len(df) else ""
                      for i in (1, 2, 3)}
            logging.info(f"[SUBCHART MAP] nonNaN={nonnan} sources={source_used}")
        except Exception:
            pass

        return df

    # =========================
    # Indicators from RAM merge helper
    # =========================
    def _get_indicators_df_for_symbol(self, symbol: str, start=None, end=None, table_name: str = "indicators_historical"):
        """
        Helper: pobierz wskaźniki dla symbolu w [start, end].

        Priorytety:
        1) jeśli engine.indicators_by_symbol ma DF dla symbolu -> bierzemy z RAM,
           filtrujemy po close_time w [start, end]
        2) w innym przypadku – fallback do self.db.get_indicator_table(...)
        """
        import logging
        import pandas as pd
        import config as _cfg

        # If prefer_db_indicators flag is set (post-test, after first symbol change),
        # always load indicators from DB and ignore any in-RAM caches. This ensures
        # that after a completed test, switching symbols will consistently show
        # indicators and risk limits (TP/SL/TS) sourced from the indicators_historical
        # table for every symbol.
        try:
            if getattr(self, "prefer_db_indicators", False):
                df_db = self.db.get_indicator_table(
                    symbol=symbol,
                    start=start,
                    end=end,
                    table_name=table_name,
                )
                logging.debug(
                    "[MainWindow] _get_indicators_df_for_symbol(%s) from DB-only mode: rows=%d",
                    symbol,
                    0 if df_db is None else len(df_db),
                )
                return df_db
        except Exception as e:
            logging.warning(
                "[MainWindow] DB-only _get_indicators_df_for_symbol(%s) failed: %s",
                symbol,
                e,
                exc_info=True,
            )

        # --- 1) spróbuj z RAM: engine.indicators_by_symbol ---
        try:
            eng = getattr(self, "engine", None)
            ind_map = getattr(eng, "indicators_by_symbol", None) or {}
        except Exception:
            ind_map = {}

        if isinstance(ind_map, dict) and symbol in ind_map:
            try:
                df_ram = ind_map[symbol]
            except Exception:
                df_ram = None

            if df_ram is not None:
                try:
                    df = df_ram.copy()
                except Exception:
                    df = df_ram

                if df is not None and not df.empty:
                    # dopilnuj close_time
                    if "close_time" not in df.columns:
                        if isinstance(df.index, pd.DatetimeIndex):
                            df["close_time"] = df.index
                        else:
                            # spróbuj skonwertować jakąś kolumnę czasową, jeśli jest
                            for candidate in ("timestamp", "TIMESTAMP"):
                                if candidate in df.columns:
                                    df["close_time"] = pd.to_datetime(df[candidate], utc=True, errors="coerce")
                                    break

                    if "close_time" in df.columns:
                        df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
                        if start is not None:
                            try:
                                s = pd.to_datetime(start, utc=True)
                                df = df[df["close_time"] >= s]
                            except Exception:
                                pass
                        if end is not None:
                            try:
                                e = pd.to_datetime(end, utc=True)
                                df = df[df["close_time"] <= e]
                            except Exception:
                                pass

                    logging.debug(
                        "[MainWindow] _get_indicators_df_for_symbol(%s) from RAM: rows=%d",
                        symbol, len(df),
                    )
                    return df

        # --- 2) fallback: DB ---
        try:
            df_db = self.db.get_indicator_table(
                symbol=symbol,
                start=start,
                end=end,
                table_name=table_name,
            )
            return df_db
        except Exception as e:
            logging.warning(
                "[MainWindow] _get_indicators_df_for_symbol: db.get_indicator_table(%s, %s) failed: %s",
                table_name,
                symbol,
                e,
            )
            return None


    def _merge_indicators_by_timestamp(self, candles_df, symbol: str):
        """
        Łączy candles_df z tabelą indicators_historical po:
        [symbol, open_time, close_time] lub [symbol, close_time].

        Zakładamy:
        - open_time / close_time są 1:1 tożsame w candles i indicators_historical,
        - candles_df może mieć close_time jako index (DatetimeIndex) albo jako kolumnę,
          więc na wejściu zawsze normalizujemy to do kolumny.
        """
        import pandas as pd
        import logging

        if candles_df is None or len(candles_df) == 0:
            return candles_df

        # lokalny helper do konwersji różnych formatów czasu -> datetime64[ns, UTC]
        def _to_dt(series):
            s = series

            # jeśli już jest datetime, tylko dopinamy UTC
            if pd.api.types.is_datetime64_any_dtype(s):
                return pd.to_datetime(s, utc=True, errors="ignore")

            # liczby -> epoch sekundy / milisekundy
            if pd.api.types.is_numeric_dtype(s):
                v = pd.to_numeric(s, errors="coerce")
                v_valid = v.dropna()
                if v_valid.empty:
                    return pd.to_datetime(v, utc=True, errors="coerce")

                sample = float(v_valid.iloc[0])
                # typowy epoch (sekundy) ~1e9, (milisekundy) ~1e12
                unit = "s"
                if sample > 1e11:
                    unit = "ms"

                return pd.to_datetime(v, unit=unit, utc=True, errors="coerce")

            # stringi / obiekty
            return pd.to_datetime(s, utc=True, errors="coerce")

        # Pracujemy na kopii, żeby nie psuć oryginału
        candles = candles_df.copy()

        # --- symbol ---
        if "symbol" not in candles.columns:
            candles["symbol"] = symbol

        # --- jeśli index jest close_time, przerzuć go do kolumny ---
        try:
            if getattr(candles.index, "name", None) == "close_time" and "close_time" not in candles.columns:
                candles = candles.reset_index()  # index -> kolumna 'close_time'
        except Exception:
            pass

        # --- open_time: dopilnujmy, żeby była kolumna datetime ---
        if "open_time" in candles.columns:
            candles["open_time"] = _to_dt(candles["open_time"])
        elif "OPEN_TIME" in candles.columns:
            candles["open_time"] = _to_dt(candles["OPEN_TIME"])

        # --- close_time: jeśli brak, spróbuj z kolumny timestamp albo z open_time ---
        if "close_time" in candles.columns:
            candles["close_time"] = _to_dt(candles["close_time"])
        elif "CLOSE_TIME" in candles.columns:
            candles["close_time"] = _to_dt(candles["CLOSE_TIME"])
        else:
            # brak kolumny close_time – spróbuj z timestamp
            if "timestamp" in candles.columns:
                candles["close_time"] = _to_dt(candles["timestamp"])
            elif "open_time" in candles.columns:
                # awaryjnie: close_time == open_time
                candles["close_time"] = candles["open_time"]

        # jeżeli nadal nie mamy sensownego czasu, odpuszczamy merge
        if "close_time" not in candles.columns:
            logging.warning(
                "[MainWindow] _merge_indicators_by_timestamp: candles have no close_time; returning unmerged DF"
            )
            return candles_df

        candles = candles.dropna(subset=["close_time"]).reset_index(drop=True)

        t_min = candles["close_time"].min()
        t_max = candles["close_time"].max()

        logging.warning(
            "[MainWindow] candles[%s] time range: %s .. %s (rows=%d)",
            symbol,
            t_min,
            t_max,
            len(candles),
        )

        # --- pobierz wskaźniki (prefer RAM, fallback DB) ---
        try:
            ind_df = self._get_indicators_df_for_symbol(
                symbol=symbol,
                start=t_min,
                end=t_max,
                table_name="indicators_historical",
            )
        except Exception as e:
            logging.error(
                "[MainWindow] _merge_indicators_by_timestamp: _get_indicators_df_for_symbol failed: %s",
                e,
                exc_info=True,
            )
            ind_df = None

        if ind_df is None or ind_df.empty:
            logging.warning(
                "[MainWindow] _merge_indicators_by_timestamp: no indicators for %s in [%s, %s]",
                symbol,
                t_min,
                t_max,
            )
            return candles_df


        ind = ind_df.copy()

        # --- normalizacja open_time / close_time po stronie wskaźników ---

        # open_time (jeśli jest)
        if "open_time" in ind.columns:
            ind["open_time"] = _to_dt(ind["open_time"])
        elif "OPEN_TIME" in ind.columns:
            ind["open_time"] = _to_dt(ind["OPEN_TIME"])

        # wybieramy kolumnę źródłową czasu dla indicatorów
        time_col_ind = None
        for c in ("close_time", "CLOSE_TIME", "timestamp", "TIMESTAMP"):
            if c in ind.columns:
                time_col_ind = c
                break

        if time_col_ind is None:
            logging.warning("[MainWindow] indicators DF has no time column; returning candles only")
            return candles_df

        ind["close_time"] = _to_dt(ind[time_col_ind])

        if "symbol" not in ind.columns:
            ind["symbol"] = symbol

        # wyrzucamy tylko wiersze, gdzie po konwersji czas jest NaT
        ind = ind.dropna(subset=["close_time"]).reset_index(drop=True)

        logging.warning(
            "[MainWindow] indicators[%s] time range: %s .. %s (rows=%d)",
            symbol,
            ind["close_time"].min(),
            ind["close_time"].max(),
            len(ind),
        )

        # --- wybierz klucze merge'u ---
        key_cols = ["symbol"]
        if "open_time" in candles.columns and "open_time" in ind.columns:
            key_cols.append("open_time")
        if "close_time" in candles.columns and "close_time" in ind.columns:
            key_cols.append("close_time")

        if key_cols == ["symbol"]:
            logging.warning(
                "[MainWindow] _merge_indicators_by_timestamp: no common time key; returning unmerged DF"
            )
            return candles_df

        indicator_cols = [c for c in ind.columns if c not in key_cols]

        logging.warning(
            "[MainWindow] merging indicators via merge() on %s, indicator_cols=%s",
            key_cols,
            ", ".join(indicator_cols),
        )

        try:
            merged = pd.merge(
                candles,
                ind[key_cols + indicator_cols],
                how="left",
                on=key_cols,
                suffixes=("", "_ind"),
            )
        except Exception as e:
            logging.error(
                "[MainWindow] indicator merge failed for %s: %s",
                symbol,
                e,
                exc_info=True,
            )
            return candles_df

        logging.warning(
            "[MainWindow] MERGED DF for %s: rows=%d, cols=%d, columns=%s",
            symbol,
            len(merged),
            len(merged.columns),
            ", ".join(map(str, merged.columns)),
        )

        return merged

    # =========================
    # Aktualizacja widoków
    # =========================
    def _build_plot_df_for_gui(self, df, symbol: str):
        """Zbuduj DataFrame pod wykres (z uwzględnieniem PLOT_AGGREGATION i MAX_PLOT_CANDLES).

        - jeśli PLOT_AGGREGATION <= 1: zachowuje się jak dotychczas
          (sort po close_time + tail(MAX_PLOT_CANDLES))
        - jeśli PLOT_AGGREGATION > 1: agreguje świeczki i wskaźniki w buckety
          po agg_n świec bazowych:
            * open  = open pierwszej świecy w buckecie
            * close = close ostatniej świecy w buckecie
            * high  = max(high)
            * low   = min(low)
            * wskaźniki (kolumny numeryczne poza OHLC/czas/volume): średnia
        """
        import logging
        import numpy as np
        import pandas as pd

        if df is None or df.empty:
            return None

        # podstawowe sortowanie po close_time (jeśli możliwe)
        try:
            if "close_time" in df.columns:
                df = df.sort_values("close_time")
        except Exception:
            pass

        # odczytaj agg_n z configu; defensywnie zabezpiecz
        # odczytaj agg_n z configu; defensywnie zabezpiecz
        # Jeśli włączony jest dynamiczny LOD, ignorujemy statyczną agregację
        # po stronie MainWindow i zachowujemy bazowy interwał (agg_n = 1).
        try:
            if PLOT_DYNAMIC_AGG_ENABLED:
                agg_n = 1
            else:
                agg_n = int(PLOT_AGGREGATION)
        except Exception:
            agg_n = 1
            # tryb jak dotychczas – tylko przycięcie do MAX_PLOT_CANDLES
            try:
                if len(df) > MAX_PLOT_CANDLES:
                    df = df.tail(MAX_PLOT_CANDLES)
            except Exception:
                pass
            return df.reset_index(drop=True)

        # --- tryb z agregacją ---
        try:
            df_agg = df.copy()
        except Exception:
            df_agg = pd.DataFrame(df)

        # normalizacja/pilnowanie close_time
        try:
            if "close_time" in df_agg.columns:
                df_agg["close_time"] = pd.to_datetime(df_agg["close_time"], utc=True, errors="coerce")
                df_agg = df_agg[df_agg["close_time"].notna()].sort_values("close_time").reset_index(drop=True)
            elif isinstance(df_agg.index, pd.DatetimeIndex):
                df_agg = df_agg.sort_index().reset_index(drop=False).rename(columns={"index": "close_time"})
            else:
                # fallback: sortujemy po timestamp jeśli istnieje
                if "timestamp" in df_agg.columns:
                    df_agg = df_agg.sort_values("timestamp").reset_index(drop=True)
                else:
                    df_agg = df_agg.reset_index(drop=True)
        except Exception as e:
            logging.debug("[MainWindow] _build_plot_df_for_gui normalize failed for %s: %s", symbol, e)

        n = len(df_agg)
        if n == 0:
            return None

        try:
            bucket_idx = (np.arange(n, dtype=np.int64) // int(agg_n)).astype(np.int64)
        except Exception:
            bucket_idx = np.arange(n, dtype=np.int64)
        df_agg["__bucket"] = bucket_idx

        ohlc_cols = ["open", "high", "low", "close"]
        time_cols = ["open_time", "close_time"]
        volume_cols = ["volume", "volume_quote", "taker_buy_volume", "taker_buy_volume_quote"]

        numeric_cols = []
        for col in df_agg.columns:
            if col in ohlc_cols or col in time_cols or col in volume_cols:
                continue
            if col in ("symbol", "__bucket", "timestamp"):
                continue
            try:
                if np.issubdtype(df_agg[col].dtype, np.number):
                    numeric_cols.append(col)
            except Exception:
                continue

        agg_dict = {}
        if "open_time" in df_agg.columns:
            agg_dict["open_time"] = "first"
        if "close_time" in df_agg.columns:
            agg_dict["close_time"] = "last"
        if "open" in df_agg.columns:
            agg_dict["open"] = "first"
        if "high" in df_agg.columns:
            agg_dict["high"] = "max"
        if "low" in df_agg.columns:
            agg_dict["low"] = "min"
        if "close" in df_agg.columns:
            agg_dict["close"] = "last"

        for col in volume_cols:
            if col in df_agg.columns:
                agg_dict[col] = "sum"

        for col in numeric_cols:
            if col not in agg_dict:
                agg_dict[col] = "mean"

        try:
            grouped = df_agg.groupby("__bucket", sort=True)
            out = grouped.agg(agg_dict).reset_index(drop=True)
        except Exception as e:
            logging.exception("[MainWindow] _build_plot_df_for_gui aggregation failed for %s: %s", symbol, e)
            # fallback – jak wcześniej, bez agregacji
            try:
                if len(df) > MAX_PLOT_CANDLES:
                    df = df.tail(MAX_PLOT_CANDLES)
            except Exception:
                pass
            return df.reset_index(drop=True)

        # timestamp = close_time (sekundy UNIX)
        try:
            if "close_time" in out.columns:
                out["timestamp"] = (out["close_time"].astype("int64") // 10**9).astype("int64")
        except Exception:
            pass

        # docięcie do MAX_PLOT_CANDLES już po agregacji
        try:
            if len(out) > MAX_PLOT_CANDLES:
                out = out.tail(MAX_PLOT_CANDLES)
        except Exception:
            pass

        return out.reset_index(drop=True)

    def update_symbol_view(self):
        """
        Odśwież widok bieżącego symbolu:
          - pobierz świece z DB
          - dołącz wskaźniki (indicators_historical) po (symbol, close_time)
          - zaktualizuj wykres, performance widget, tabele trejdów i ticków
        """
        import pandas as pd
        import logging
        import gc

        symbol = self.controls.pair_box.currentText()
        self.current_symbol = symbol

        logging.info("[MainWindow] update_symbol_view(%s) – start", symbol)

        # 1) Świece z DB
        try:
            raw = self.db.get_candles(symbol, limit=MAX_PLOT_CANDLES)
        except Exception as e:
            logging.error("[MainWindow] get_candles(%s) failed: %s", symbol, e, exc_info=True)
            raw = None

        df = _normalize_candles_df(raw) if raw is not None else None

        if df is None or df.empty:
            logging.warning("[MainWindow] No candles for %s – plot will be cleared", symbol)

        # 2) Merge wskaźników (po close_time)
        if df is not None and not df.empty:
            try:
                merged = self._merge_indicators_by_timestamp(df, symbol)
                if merged is None or merged.empty:
                    logging.warning(
                        "[MainWindow] merged candles+indicators empty for %s – using candles only",
                        symbol,
                    )
                    df_merged = df
                else:
                    df_merged = merged
                # mapowanie indicator_1/2/3 itd.
                try:
                    df_merged = self._map_indicators_from_merge(df_merged)
                except Exception as e:
                    logging.warning("[MainWindow] _map_indicators_from_merge failed: %s", e)
            except Exception as e:
                logging.warning("[MainWindow] Indicator merge failed: %s", e, exc_info=True)
                df_merged = df
        else:
            df_merged = df

        df = df_merged

        # DEBUG: DF po merge – pełny, jeszcze przed przycięciem MAX_PLOT_CANDLES
        try:
            if df is not None and not df.empty:
                logging.info(
                    "[MainWindow] df_merged for %s: rows=%d, cols=%d, columns=%s",
                    symbol,
                    len(df),
                    len(df.columns),
                    ", ".join(map(str, df.columns)),
                )
        except Exception:
            pass

        # 3) Trades z engine'a (w pamięci)
        trades = []
        if getattr(self, "engine", None) is not None and hasattr(self.engine, "get_trades"):
            try:
                trades = self.engine.get_trades(symbol) or []
            except Exception as e:
                logging.error("[MainWindow] engine.get_trades(%s) failed: %s", symbol, e, exc_info=True)
                trades = []
        self.current_trades = trades

        # 3.5) Przekaż pełne dane (raw_df) do PlotWidget pod dynamiczną agregację
        if df is not None and not df.empty:
            try:
                self.plot_widget.set_raw_history(df, symbol=symbol)
            except Exception as e:
                logging.warning("[MainWindow] set_raw_history failed for %s: %s", symbol, e)

        # 4) DataFrame do wykresu (ostatnie MAX_PLOT_CANDLES, z uwzględnieniem PLOT_AGGREGATION)
        if df is not None and not df.empty:
            plot_df = self._build_plot_df_for_gui(df, symbol)

            # DEBUG: finalny DF, który idzie na wykres
            try:
                if plot_df is not None and not plot_df.empty:
                    logging.info(
                        "[MainWindow] plot_df for %s (to PlotWidget): rows=%d, cols=%d, columns=%s",
                        symbol,
                        len(plot_df),
                        len(plot_df.columns),
                        ", ".join(map(str, plot_df.columns)),
                    )
            except Exception:
                pass
        else:
            plot_df = None

        # 5) Konfiguracja wykresu z engine/strategii
        if getattr(self, "engine", None) is not None:
            try:
                self.plot_widget.engine = self.engine
                self.plot_widget.indicator_names = list(getattr(self.engine, "indicator_names", []) or [])
                if hasattr(self.engine.strategy, "get_display_config"):
                    self.plot_widget.set_display_config(self.engine.strategy.get_display_config())
            except Exception as e:
                logging.debug("[MainWindow] plot_widget engine/display_config setup failed: %s", e)

        # 6) Aktualizacja wykresu
        try:
            if plot_df is not None and not plot_df.empty:
                logging.info(
                    "[MainWindow] update_chart for %s: %d candles, %d trades",
                    symbol,
                    len(plot_df),
                    len(self.current_trades or []),
                )
                self.plot_widget.update_chart(
                    plot_df,
                    self.current_trades[-MAX_GUI_TRADES:] if self.current_trades else [],
                )
            else:
                logging.warning("[MainWindow] plot_df empty for %s – clearing chart", symbol)
                self.plot_widget.update_chart(None, [])
        except Exception as e:
            logging.error("[MainWindow] plot_widget.update_chart failed: %s", e, exc_info=True)

        # 7) Performance widget + tabela trejdów
        try:
            self.performance_widget.update_stats(trades[-MAX_GUI_TRADES:] if trades else [])
        except Exception as e:
            logging.debug("[MainWindow] performance_widget.update_stats failed: %s", e)

        try:
            self.trades_table.update_trades(trades[-MAX_GUI_TRADES:] if trades else [])
        except Exception as e:
            logging.debug("[MainWindow] trades_table.update_trades failed: %s", e)

        # 8) ticks table – ostatni trade
        try:
            filtered_trades = [t for t in (trades or []) if t is not None]
            if filtered_trades:
                last_trade_id = filtered_trades[-1].get("trade_id")
                ticks_raw = self.db.get_last_ticks(trade_id=last_trade_id, limit=100)
                ticks_df = _as_df(ticks_raw)
                self.ticks_table.update_ticks(
                    ticks_df if isinstance(ticks_df, pd.DataFrame) and not ticks_df.empty else None
                )
            else:
                self.ticks_table.update_ticks(None)
        except Exception as e:
            logging.debug("[MainWindow] ticks_table.update_ticks failed: %s", e)

        # 9) Podgląd wskaźników (mała tabelka)
        try:
            if df is not None and not df.empty and hasattr(self.engine, "indicator_names"):
                indicator_cols, col_map = [], {}
                for i, name in enumerate(self.engine.indicator_names[:3], start=1):
                    col = f"indicator_{i}"
                    if col in df.columns:
                        indicator_cols.append(col)
                        col_map[col] = name

                extra_cols = [
                    c
                    for c in [
                        "MA_FAST",
                        "MA_SLOW",
                        "BB_MIDDLE",
                        "BB_UPPER",
                        "BB_LOWER",
                        "ATR",
                        "ATR_PCT",
                        "MACD",
                        "MACD_SIGNAL",
                        "MACD_HIST",
                    ]
                    if c in df.columns
                ]
                display_cols = (
                    ["timestamp"] + indicator_cols + extra_cols
                    if (indicator_cols or extra_cols)
                    else None
                )
                subdf = df[display_cols].tail(10).copy() if display_cols else None
                if subdf is not None and not subdf.empty:
                    rename_map = {
                        **col_map,
                        "MA_FAST": "MA fast",
                        "MA_SLOW": "MA slow",
                        "BB_MIDDLE": "MA mid",
                        "BB_UPPER": "BB upper",
                        "BB_LOWER": "BB lower",
                        "MACD_SIGNAL": "MACD signal",
                        "MACD_HIST": "MACD hist",
                    }
                    subdf = subdf.rename(columns=rename_map)
                    try:
                        subdf["timestamp"] = pd.to_datetime(
                            subdf["timestamp"], unit="s"
                        ).dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                    self.indicators_table.update_indicators(subdf)
                else:
                    self.indicators_table.update_indicators(None)
            else:
                self.indicators_table.update_indicators(None)
        except Exception as e:
            logging.warning(f"[MainWindow] Could not update indicators table: {e}")
            self.indicators_table.update_indicators(None)

        gc.collect()
        logging.info("[MainWindow] update_symbol_view(%s) – done", symbol)

    def update_live_chart(self):
        if getattr(self, '_gui_soft_restart_paused', False):
            return
        import pandas as pd

        symbol = self.controls.pair_box.currentText()
        raw = self.db.get_candles(symbol, limit=MAX_PLOT_CANDLES)
        df = _normalize_candles_df(raw)
        if df is None or df.empty:
            self.indicators_table.update_indicators(None)
            return

        # Shim: timestamp z close_time lub z indexu (sekundy UNIX)
        try:
            if 'timestamp' not in df.columns:
                if 'close_time' in df.columns:
                    ts = pd.to_datetime(df['close_time'], utc=True, errors='coerce').astype('int64') // 10 ** 9
                    df = df.copy();
                    df['timestamp'] = ts
                elif isinstance(df.index, pd.DatetimeIndex):
                    ts = pd.to_datetime(df.index, utc=True).astype('int64') // 10 ** 9
                    df = df.copy();
                    df['timestamp'] = ts
        except Exception:
            pass

        try:
            df = self._merge_indicators_by_timestamp(df, symbol)
            df = self._map_indicators_from_merge(df)
        except Exception as e:
            logging.warning("Indicator merge failed: %s", e)

        plot_df = df.tail(MAX_PLOT_CANDLES).copy().reset_index()
        if plot_df is not None and not plot_df.empty:
            self.plot_widget.update_chart(
                plot_df,
                self.current_trades[-MAX_GUI_TRADES:] if self.current_trades else []
            )
            try:
                if hasattr(self.engine, "indicator_names"):
                    indicator_cols, col_map = [], {}
                    for i, name in enumerate(self.engine.indicator_names[:3], start=1):
                        col = f"indicator_{i}"
                        if col in df.columns:
                            indicator_cols.append(col)
                            col_map[col] = name
                    extra_cols = [c for c in
                                  ['MA_FAST', 'MA_SLOW', 'BB_MIDDLE', 'BB_UPPER', 'BB_LOWER', 'ATR', 'ATR_PCT', 'MACD',
                                   'MACD_SIGNAL', 'MACD_HIST'] if c in df.columns]
                    display_cols = ['timestamp'] + indicator_cols + extra_cols if (
                                indicator_cols or extra_cols) else None
                    subdf = df[display_cols].tail(10).copy() if display_cols else None
                    if subdf is not None and not subdf.empty:
                        rename_map = {**col_map, 'MA_FAST': 'MA fast', 'MA_SLOW': 'MA slow', 'BB_MIDDLE': 'MA mid',
                                      'BB_UPPER': 'BB up',
                                      'BB_LOWER': 'BB lo', 'MACD_SIGNAL': 'MACD signal', 'MACD_HIST': 'MACD hist'}
                        subdf = subdf.rename(columns=rename_map)
                        try:
                            subdf['timestamp'] = pd.to_datetime(subdf['timestamp'], unit='s').dt.strftime(
                                '%Y-%m-%d %H:%M:%S')
                        except Exception:
                            pass
                        self.indicators_table.update_indicators(subdf)
                    else:
                        self.indicators_table.update_indicators(None)
                else:
                    self.indicators_table.update_indicators(None)
            except Exception as e:
                logging.warning(f"Could not update indicators table: {e}")
                self.indicators_table.update_indicators(None)
        gc.collect()


    def _on_symbol_changed(self, idx: int):
        """
        Handler for changes in the symbol (pair) combobox.

        Design:
        - We do NOT touch the very first view right after a test finishes
          (the default symbol may still use RAM-backed indicators).
        - Once a test is finished (post_test_mode == True), the *first* symbol change
          switches the indicators loading to DB-only mode (prefer_db_indicators = True).
        - After that, every symbol switch always reloads the view and restarts the
          chart worker for the newly selected symbol.
        """
        import logging

        try:
            # If a test has finished and this is the first symbol change afterwards,
            # enable DB-only indicators mode.
            if getattr(self, "post_test_mode", False) and not getattr(self, "prefer_db_indicators", False):
                self.prefer_db_indicators = True
                logging.info("[MainWindow] Switching to DB-only indicators mode after symbol change")
        except Exception as e:
            logging.debug("[MainWindow] _on_symbol_changed flag update error: %s", e)

        # Trigger the usual update & chart worker restart for the newly selected symbol.
        # We keep these calls separated and protected so that a failure in one does not
        # prevent the other from running.
        try:
            self.update_symbol_view()
        except Exception as e:
            logging.warning("update_symbol_view failed on symbol change: %s", e, exc_info=True)

        try:
            self._start_chart_worker_for_symbol()
        except Exception as e:
            logging.warning("_start_chart_worker_for_symbol failed on symbol change: %s", e, exc_info=True)


    def _start_chart_worker_for_symbol(self):
        symbol = self.controls.pair_box.currentText()
        try:
            if self._chart_worker is not None:
                self._chart_worker.stop()
                self._chart_worker.wait(500)
                self._chart_worker = None
        except Exception:
            pass

        # fetcher może być niedostępny w momencie startu workera (wątek LiveFetcher dopiero rusza),
        # więc przekazujemy do ChartDataWorker *provider* (lambda), który będzie próbował
        # na bieżąco pobierać fetcher z self.live_fetchers.
        fetcher_provider = None
        try:
            live_fetchers = getattr(self, "live_fetchers", None)
            if isinstance(live_fetchers, dict):
                def _provider(sym=symbol, self_ref=self):
                    try:
                        return self_ref.live_fetchers.get(sym)
                    except Exception:
                        return None

                fetcher_provider = _provider
        except Exception:
            fetcher_provider = None

        self._chart_worker = ChartDataWorker(self.db, symbol, fetcher_provider, MAX_PLOT_CANDLES)
        self._chart_worker.full_refresh.connect(self._on_full_refresh)
        self._chart_worker.live_only.connect(self._on_live_only)
        self._chart_worker.append_closed.connect(self._on_append_closed)
        self._chart_worker.start()

    def _on_full_refresh(self, df_raw: pd.DataFrame, df_plot_from_worker: pd.DataFrame = None):
        """
        Full refresh od ChartDataWorker:
          - df_raw: świeczki bazowe (bez wskaźników) dla aktualnego symbolu
          - df_plot_from_worker: opcjonalny widok pod wykres (np. już zagregowany, przycięty)
        """
        import logging
        import pandas as pd

        try:
            df = df_raw
            if df is None or df.empty:
                return

            df = df.copy()
            # --- normalizacja czasu + timestamp ---
            if 'close_time' in df.columns:
                df['close_time'] = pd.to_datetime(df['close_time'], utc=True, errors='coerce')
                df['timestamp'] = df['close_time'].astype('int64') // 10 ** 9
                df.set_index('close_time', inplace=True)
            elif isinstance(df.index, pd.DatetimeIndex):
                df['timestamp'] = df.index.astype('int64') // 10 ** 9
            else:
                return

            # --- symbol ---
            try:
                symbol = df['symbol'].iloc[0] if 'symbol' in df.columns else self.controls.pair_box.currentText()
            except Exception:
                symbol = self.controls.pair_box.currentText()

            # --- dociągamy wskaźniki (opcjonalnie; prefer RAM, fallback DB) ---
            ind_df = None
            ind_table = None
            try:
                if self.engine is not None and hasattr(self.engine, "ind_tables_hist"):
                    ind_table = self.engine.ind_tables_hist.get(symbol, "indicators_historical")
                else:
                    ind_table = "indicators_historical"
            except Exception:
                ind_table = "indicators_historical"

            if ind_table and symbol:
                start_ts = df.index.min()
                end_ts = df.index.max()
                try:
                    ind_df = self._get_indicators_df_for_symbol(
                        symbol=symbol,
                        start=start_ts,
                        end=end_ts,
                        table_name=ind_table,
                    )
                except Exception as e:
                    logging.warning(
                        "[MainWindow] _on_full_refresh: _get_indicators_df_for_symbol(%s, %s) failed: %s",
                        ind_table,
                        symbol,
                        e,
                    )
                    ind_df = None

            if ind_df is not None and not ind_df.empty:
                try:
                    ind_df = ind_df.copy()
                    if 'close_time' in ind_df.columns:
                        ind_df['close_time'] = pd.to_datetime(ind_df['close_time'], utc=True, errors='coerce')
                    if 'symbol' not in ind_df.columns and 'SYMBOL' in ind_df.columns:
                        ind_df['symbol'] = ind_df['SYMBOL']

                    left_df = df.reset_index()  # close_time jako kolumna
                    merged = pd.merge(
                        left_df,
                        ind_df,
                        how='left',
                        on=['symbol', 'close_time'],
                        suffixes=('', '_ind'),
                    )
                    if 'close_time' in merged.columns:
                        merged = merged.set_index('close_time')
                    df = merged

                    try:
                        df = self._map_indicators_from_merge(df)
                    except Exception as e:
                        logging.debug("[MainWindow] _map_indicators_from_merge failed in _on_full_refresh: %s", e)
                except Exception as e:
                    logging.warning("[MainWindow] _on_full_refresh merge indicators failed: %s", e)

            # w tym miejscu df = raw_df + wskaźniki (pełne dane)
            # zapisujemy to jako raw_df w PlotWidget (do dynamicznej agregacji)
            try:
                symbol_for_raw = symbol
            except Exception:
                symbol_for_raw = None

            # 3.5) Przekaż pełne dane (raw_df) do PlotWidget pod dynamiczną agregację
            try:
                self.plot_widget.set_raw_history(df, symbol=symbol)
            except Exception as e:
                logging.warning("[MainWindow] set_raw_history in _on_full_refresh failed: %s", e)

            # --- DF do wykresu ---
            try:
                plot_df = self._build_plot_df_for_gui(df, symbol)
            except Exception as e:
                logging.warning("[MainWindow] _build_plot_df_for_gui failed in _on_full_refresh: %s", e)
                # fallback: proste tail() jak dotychczas
                try:
                    plot_df = df.tail(MAX_PLOT_CANDLES).reset_index(drop=False)
                except Exception:
                    plot_df = df.reset_index(drop=False)

            # --- konfiguracja wykresu z engine ---
            try:
                self.plot_widget.engine = self.engine
                # start from engine indicator_names, then ensure FEAR_GREED is present if available in data
                ind_names = list(getattr(self.engine, "indicator_names", []) or [])
                if "FEAR_GREED" in df.columns and "FEAR_GREED" not in ind_names:
                    ind_names.append("FEAR_GREED")
                self.plot_widget.indicator_names = ind_names

                if hasattr(self.engine.strategy, "get_display_config"):
                    cfg = self.engine.strategy.get_display_config()
                    # opcjonalnie: jeśli strategia nie przypisała FEAR_GREED do żadnego subchartu,
                    # możesz go dorzucić np. do pierwszego subchartu:
                    try:
                        sub1 = cfg.get("sub1") or []
                        if "FEAR_GREED" in ind_names and "FEAR_GREED" not in sub1:
                            sub1 = list(sub1) + ["FEAR_GREED"]
                            cfg["sub1"] = sub1
                    except Exception:
                        pass
                    self.plot_widget.set_display_config(cfg)
            except Exception:
                pass

            # --- ustawiamy historię na wykresie ---
            self.plot_widget.set_history(
                plot_df,
                getattr(self, 'current_trades', [])[-MAX_GUI_TRADES:]
                if getattr(self, 'current_trades', None) else []
            )

            # --- last_close_time ---
            try:
                if plot_df is not None and not plot_df.empty and 'close_time' in plot_df.columns:
                    self.last_close_time = plot_df['close_time'].iloc[-1]
            except Exception:
                pass

            # --- poziomy trejdów ---
            try:
                self.clear_all_trade_levels()
                for trade in (self.current_trades or []):
                    self._draw_trade_levels(trade)
            except Exception as e:
                logging.debug(f"draw trade levels (history) failed: {e}")
        except Exception as e:
            logging.warning(f"_on_full_refresh error: {e}")

    def _on_live_only(self, live):

        try:
            if not live:
                return
            lr = dict(live)  # copy

            # 1) Timestamp (epoch s, UTC) z close_time -> spójnie z full refresh
            if 'timestamp' not in lr or lr.get('timestamp') is None:
                try:
                    import pandas as pd
                    ct = lr.get('close_time') or lr.get('open_time')
                    if ct is not None:
                        ts = pd.to_datetime(ct, utc=True, errors='coerce').value // 10**9
                        lr['timestamp'] = float(ts)
                except Exception:
                    pass

            # 2) Ujednolicenie typów liczbowych
            for k in ('open', 'high', 'low', 'close', 'volume'):
                if k in lr and lr[k] is not None:
                    try:
                        lr[k] = float(lr[k])
                    except Exception:
                        pass

            # 3) Symbol dla kontekstu
            if 'symbol' not in lr:
                lr['symbol'] = getattr(self, 'current_symbol', None)

            self.plot_widget.update_live(lr)
        except Exception as e:
            logging.debug(f"_on_live_only failed: {e}")
    def _on_append_closed(self, closed, new_live):
        try:
            self.plot_widget.append_closed(closed, new_live)
            try:
                if new_live and 'close_time' in new_live:
                    self.last_close_time = pd.to_datetime(new_live['close_time'], utc=True, errors='coerce')
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"_on_append_closed failed: {e}")

    # =========================
    # Inne drobne utilsy
    # =========================
    def debounce(self, func, wait_ms=100):
        timer = getattr(self, '_debounce_timer', None)
        if timer:
            timer.stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(func)
        self._debounce_timer = timer
        timer.start(wait_ms)

    def update_metrics(self):
        if getattr(self, '_gui_soft_restart_paused', False):
            return
        proc = psutil.Process()
        ram = proc.memory_info().rss / (1024 * 1024)
        cpu = proc.cpu_percent()
        self.metrics_label.setText(f"RAM: {ram:.1f} MB   CPU: {cpu}%")
        self._check_ram_restart(ram)

    def _check_ram_restart(self, ram_usage_mb):
        if not hasattr(self, '_gui_restart_count'):
            self._gui_restart_count = 0

        if ram_usage_mb > GUI_SOFT_RESTART_RAM_MB:
            self._append_log_to_ui(f"RAM przekroczył {GUI_SOFT_RESTART_RAM_MB} MB ({ram_usage_mb:.1f} MB). Restart GUI!")
            self._soft_restart_gui()
            self._gui_restart_count += 1

        if ram_usage_mb > GUI_KILL_RAM_MB or self._gui_restart_count > GUI_MAX_SOFT_RESTARTS:
            self._append_log_to_ui(f"RAM przekroczył {GUI_KILL_RAM_MB} MB lub restartów było za dużo. ZAMYKAM APLIKACJĘ!")
            os._exit(1)

    # === Per-trade TP/SL/TS lines ===
    def _time_to_x(self, ts):
        import pyqtgraph as pg
        if ts is None:
            return None
        try:
            import pandas as pd
            if not isinstance(ts, (int, float)):
                ts = pd.to_datetime(ts, utc=True, errors='coerce')
                if ts is pd.NaT:
                    return None
                return pg.datetime2np(ts.to_pydatetime())
        except Exception:
            pass
        return ts

    def _add_h_segment(self, y, x1, x2, pen=None, name=None):
        import pyqtgraph as pg
        if x1 is None or x2 is None or y is None:
            return None
        if x2 < x1:
            x1, x2 = x2, x1
        if pen is None:
            pen = pg.mkPen(style=pg.QtCore.Qt.DashLine, width=1)
        item = pg.PlotDataItem([x1, x2], [y, y], connect="all", pen=pen, name=name)
        self.plot_widget.plot.addItem(item)
        return item

    def _remove_trade_levels(self, trade_id):
        items = getattr(self, "_trade_level_items", {}).pop(trade_id, {})
        for it in items.values():
            try:
                self.plot_widget.plot.removeItem(it)
            except Exception:
                pass

    def _draw_trade_levels(self, trade):
        import pyqtgraph as pg
        tid = trade.get("trade_id")
        if tid is None:
            return
        self._remove_trade_levels(tid)

        x1 = self._time_to_x(trade.get("entry_timestamp"))
        x2 = self._time_to_x(trade.get("exit_timestamp"))
        if x1 is None:
            return
        if x2 is None:
            x2 = self._time_to_x(getattr(self, "last_close_time", None))
            if x2 is None:
                try:
                    x2 = self.plot_widget.plot.viewRange()[0][1]
                except Exception:
                    x2 = x1

        items = {}
        TP_PEN = pg.mkPen((100, 200, 100), width=1, style=pg.QtCore.Qt.DashLine)
        SL_PEN = pg.mkPen((220, 120, 120), width=1, style=pg.QtCore.Qt.DashLine)
        TS_PEN = pg.mkPen((180, 180, 220), width=1, style=pg.QtCore.Qt.DotLine)

        tp = trade.get("tp_level")
        sl = trade.get("sl_level")
        ts = trade.get("trailing_stop")
        if tp is not None:
            items["tp"] = self._add_h_segment(tp, x1, x2, pen=TP_PEN, name=f"TP#{tid}")
        if sl is not None:
            items["sl"] = self._add_h_segment(sl, x1, x2, pen=SL_PEN, name=f"SL#{tid}")
        if ts is not None:
            items["ts"] = self._add_h_segment(ts, x1, x2, pen=TS_PEN, name=f"TS#{tid}")

        if not hasattr(self, "_trade_level_items"):
            self._trade_level_items = {}
        self._trade_level_items[tid] = items

    def _update_active_trade_levels(self, trade):
        tid = trade.get("trade_id")
        if tid is None:
            return
        self._draw_trade_levels(trade)

    def clear_all_trade_levels(self):
        for tid in list(getattr(self, "_trade_level_items", {}).keys()):
            self._remove_trade_levels(tid)

    def _soft_restart_gui(self):
        self._append_log_to_ui("SOFT RESTART GUI... Clearing charts/tables/logs (DB untouched).")
        ram_before = psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2)

        self.plot_widget.update_chart(None, [])
        self.performance_widget.update_stats([])
        self.trades_table.update_trades([])
        self.ticks_table.update_ticks(None)
        self.indicators_table.update_indicators(None)
        self.global_performance_widget.update_stats([])
        self.global_trades_table.update_trades([])
        self.log_widget.clear()

        self.current_trades = None
        self.current_symbol = None
        if hasattr(self, "_last_plot_df"):
            self._last_plot_df = None
        self.engine = None

        if hasattr(self.plot_widget, "last_df"):
            self.plot_widget.last_df = None
        if hasattr(self.plot_widget, "_candlestick_item"):
            self.plot_widget._candlestick_item = None

        gc.collect()
        ram_after = psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2)
        print(f"SOFT RESTART: RAM before: {ram_before:.1f} MB, after GC: {ram_after:.1f} MB")
        try:
            if objgraph:
                objgraph.show_growth(limit=10)
        except Exception:
            pass

        self._append_log_to_ui("GUI SOFT RESTART finished. RAM freed, DB untouched.")

    def make_debug_ram_report(self) -> str:
        output = []
        output.append("=== OBJGRAPH: Najczęstsze typy obiektów w pamięci ===")
        if objgraph:
            for t, n in objgraph.most_common_types(limit=15):
                output.append(f"{t}: {n}")
        else:
            output.append("objgraph not installed!")

        output.append("\n=== GC: Liczba wybranych typów obiektów ===")
        n_widgets = sum(1 for o in gc.get_objects() if getattr(o, "__class__", None) and o.__class__.__name__.endswith("QWidget"))
        n_ndarray = sum(1 for o in gc.get_objects() if type(o).__name__ == "ndarray")
        n_dict = sum(1 for o in gc.get_objects() if type(o).__name__ == "dict")
        n_list = sum(1 for o in gc.get_objects() if type(o).__name__ == "list")
        n_deque = sum(1 for o in gc.get_objects() if type(o).__name__ == "deque")
        n_df = sum(1 for o in gc.get_objects() if type(o).__name__ == "DataFrame")
        n_ts = sum(1 for o in gc.get_objects() if type(o).__name__ == "Timestamp")
        output.append(
            f"QWidget: {n_widgets}  ndarray: {n_ndarray}  dict: {n_dict}  list: {n_list}  deque: {n_deque}  DataFrame: {n_df}  Timestamp: {n_ts}"
        )

        output.append("\n=== TRACEMALLOC: Największe przyrosty RAM (per linia kodu) ===")
        if tracemalloc.is_tracing():
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics('lineno')
            for stat in top_stats[:10]:
                output.append(str(stat))
        else:
            output.append("tracemalloc is not running! Start it in main.py (tracemalloc.start())")

        return "\n".join(output)

    def on_debug_ram(self):
        report = self.make_debug_ram_report()
        DebugRamDialog(self, text=report).exec_()

    def force_df_cleanup(self):
        exclude = {"force_df_cleanup", "last_df"}
        for a in [a for a in dir(self) if 'df' in a and a not in exclude]:
            try:
                setattr(self, a, None)
            except Exception:
                pass
        gc.collect()

    # =========================
    # Kolumny tabel (Trades / Global Trades)
    # =========================
    def _enforce_trade_table_columns(self) -> bool:
        desired = ["trade_id", "symbol", "side", "entry_price", "exit_price", "pnl",
                   "fee", "amount", "entry_time", "exit_time"]

        def apply(view):
            try:
                model = view.model()
                if model is None:
                    return False
                header = view.horizontalHeader()
                labels = []
                for col in range(model.columnCount()):
                    val = model.headerData(col, Qt.Horizontal, Qt.DisplayRole)
                    labels.append(str(val).strip().lower())

                moved_any = False
                for target_pos, name in enumerate(desired):
                    name = name.lower()
                    if name not in labels:
                        continue
                    current_pos = labels.index(name)
                    if current_pos != target_pos:
                        header.moveSection(current_pos, target_pos)
                        lab = labels.pop(current_pos)
                        labels.insert(target_pos, lab)
                        moved_any = True
                return moved_any
            except Exception:
                return False

        ok1 = apply(self.trades_table)
        ok2 = apply(self.global_trades_table)
        return bool(ok1 or ok2)


    def _refresh_tables_auto_resize(self):
        """Force tables to re-evaluate widths when splitters move."""
        for w in [getattr(self, 'trades_table', None),
                  getattr(self, 'global_trades_table', None),
                  getattr(self, 'global_performance_widget', None),
                  getattr(self, 'indicators_table', None),
                  getattr(self, 'ticks_table', None)]:
            if w is None:
                continue
            try:
                if hasattr(w, "_user_resized") and getattr(w, "_user_resized", False) and hasattr(w, "_apply_proportional_widths"):
                    w._apply_proportional_widths()
                elif hasattr(w, "_set_even_columns"):
                    w._set_even_columns()
            except Exception:
                pass

    def _refresh_subchart_switchers(self):
        """Populate/refresh Sub1/Sub2/Sub3 comboboxes from PlotWidget options, if comboboxes exist.
        Safe no-op if comboboxes or plot_widget are missing."""
        try:
            opts = {}
            if hasattr(self, "plot_widget") and hasattr(self.plot_widget, "get_sub_slot_options"):
                opts = self.plot_widget.get_sub_slot_options() or {}
            # Update comboboxes if they exist
            combos = {}
            if hasattr(self, "sub1_combo"): combos[1] = self.sub1_combo
            if hasattr(self, "sub2_combo"): combos[2] = self.sub2_combo
            if hasattr(self, "sub3_combo"): combos[3] = self.sub3_combo
            for slot, combo in combos.items():
                combo.blockSignals(True)
                combo.clear()
                for name in opts.get(slot, []):
                    combo.addItem(name)
                active = getattr(self.plot_widget, "_sub_slot_active", {}).get(slot) if hasattr(self, "plot_widget") else None
                if active:
                    idx = combo.findText(active)
                    combo.setCurrentIndex(idx if idx >= 0 else 0)
                combo.setEnabled(combo.count() > 0)
                combo.blockSignals(False)
        except Exception:
            pass

    def _build_indicator_toolbar(self):
        """Create a persistent toolbar with Sub1/Sub2/Sub3 comboboxes."""
        tb = QToolBar("Indicators", self)
        tb.setObjectName("IndicatorsToolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(16,16))
        tb.setFloatable(False)

        # Widgets
        self.sub1_combo = QComboBox(self); self.sub1_combo.setMinimumWidth(120)
        self.sub2_combo = QComboBox(self); self.sub2_combo.setMinimumWidth(120)
        self.sub3_combo = QComboBox(self); self.sub3_combo.setMinimumWidth(120)

        tb.addWidget(QLabel("Sub1:"))
        tb.addWidget(self.sub1_combo)
        tb.addSeparator()
        tb.addWidget(QLabel("Sub2:"))
        tb.addWidget(self.sub2_combo)
        tb.addSeparator()
        tb.addWidget(QLabel("Sub3:"))
        tb.addWidget(self.sub3_combo)

        # Wire signals
        self.sub1_combo.currentTextChanged.connect(lambda name: self._on_sub_combo_changed(1, name))
        self.sub2_combo.currentTextChanged.connect(lambda name: self._on_sub_combo_changed(2, name))
        self.sub3_combo.currentTextChanged.connect(lambda name: self._on_sub_combo_changed(3, name))

        self.addToolBar(Qt.TopToolBarArea, tb)
        self._refresh_subchart_switchers()
        return tb

    def _on_sub_combo_changed(self, slot: int, name: str):
        try:
            if hasattr(self, "plot_widget") and hasattr(self.plot_widget, "set_active_sub_slot"):
                self.plot_widget.set_active_sub_slot(slot, name)
        except Exception:
            pass