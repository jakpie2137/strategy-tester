# gui/plot_widget.py
import gc
import logging
import numpy as np
import pandas as pd
import pytz

try:
    import ta  # for optional indicator recomputation
except Exception:
    ta = None

import pandas as pd
import pyqtgraph as pg
from pyqtgraph import DateAxisItem
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QRadioButton, QButtonGroup, QGraphicsView
)

# --- Fast defaults for pyqtgraph (OpenGL, AA off, etc.) ---
try:
    from .utils import configure_pg_fast_defaults, tune_curve_fast
except Exception:
    try:
        from gui.utils import configure_pg_fast_defaults, tune_curve_fast  # type: ignore
    except Exception:
        def configure_pg_fast_defaults():
            try:
                pg.setConfigOptions(antialias=False, useOpenGL=False, enableExperimental=False)
            except Exception:
                pass

        def tune_curve_fast(curve):
            try:
                curve.setDownsampling(auto=True, method='peak')
                curve.setClipToView(True)
                curve.setSkipFiniteCheck(True)
            except Exception:
                pass

configure_pg_fast_defaults()

# Candlesticks
try:
    from .candlestick_item import CandlestickItem
except Exception:
    try:
        from gui.candlestick_item import CandlestickItem  # type: ignore
    except Exception:
        # Minimal fallback: draw OHLC as thin lines if CandlestickItem is unavailable
        class CandlestickItem(pg.GraphicsObject):
            def __init__(self, data):
                super().__init__()
                self.data = np.asarray(data, dtype=float)  # x, o, c, l, h, w
                self.picture = None
                self._generate()

            def _generate(self):
                p = pg.QtGui.QPicture()
                p.begin(pg.QtGui.QPainter())
                pen_up = pg.mkPen((0, 200, 0), width=1)
                pen_dn = pg.mkPen((220, 0, 0), width=1)
                for x, o, c, low, high, w in self.data:
                    col = pen_up if c >= o else pen_dn
                    pg.QtGui.QPainter(p).setPen(col)
                    # wick
                    pg.QtGui.QPainter(p).drawLine(pg.QtCore.QPointF(x, low), pg.QtCore.QPointF(x, high))
                p.end()
                self.picture = p

            def paint(self, p, *args):
                if self.picture is not None:
                    p.drawPicture(0, 0, self.picture)

            def boundingRect(self):
                if self.data.size == 0:
                    return pg.QtCore.QRectF(0, 0, 0, 0)
                xs = self.data[:, 0]
                lows = self.data[:, 3]
                highs = self.data[:, 4]
                return pg.QtCore.QRectF(float(xs.min()), float(lows.min()), float(xs.max()-xs.min()+1), float(highs.max()-lows.min()+1))

from gui.plot_aggregation import build_plot_df
# --- widths (TP/SL vs TS) ---
try:
    from config import ORDER_LINE_WIDTH, TS_LINE_MULTIPLIER
except Exception:
    ORDER_LINE_WIDTH = 1.0
    TS_LINE_MULTIPLIER = 3.0

# --- trade marker styling (from config.py) ---
try:
    from config import (
        TRADE_ENTRY_MARKER_SIZE,
        TRADE_ENTRY_LONG_COLOR,
        TRADE_ENTRY_SHORT_COLOR,
        TRADE_ENTRY_BORDER_COLOR,
        TRADE_ENTRY_BORDER_WIDTH,
        TRADE_EXIT_MARKER_SIZE,
        TRADE_EXIT_BRUSH_COLOR,
        TRADE_EXIT_BORDER_COLOR,
        TRADE_EXIT_BORDER_WIDTH,
    )
except Exception:
    TRADE_ENTRY_MARKER_SIZE = 22
    TRADE_ENTRY_LONG_COLOR = "#00C800"
    TRADE_ENTRY_SHORT_COLOR = "#DC0000"
    TRADE_ENTRY_BORDER_COLOR = "#FFFFFF"
    TRADE_ENTRY_BORDER_WIDTH = 1
    TRADE_EXIT_MARKER_SIZE = 22
    TRADE_EXIT_BRUSH_COLOR = "#000000"
    TRADE_EXIT_BORDER_COLOR = "#FFFFFF"
    TRADE_EXIT_BORDER_WIDTH = 1

try:
    from config import (
        MAX_PLOT_CANDLES,
        STARTING_BALANCE,
        PLOT_DYNAMIC_AGG_ENABLED,
        PLOT_MAX_VISIBLE_CANDLES,
        PLOT_TARGET_MIN_BINS,
        PLOT_TARGET_MAX_BINS,
        PLOT_PYRAMID_MINUTES,
        PLOT_LOD_DEBOUNCE_MS,
        PLOT_LOD_IMPROVEMENT_FACTOR,
        PLOT_X_MARGIN_MIN_BARS,
        PLOT_X_MARGIN_FRAC,
    )
except Exception:
    MAX_PLOT_CANDLES = 4000
    STARTING_BALANCE = 10_000.0
    PLOT_DYNAMIC_AGG_ENABLED = False
    PLOT_MAX_VISIBLE_CANDLES = 1000
    PLOT_TARGET_MAX_BINS = PLOT_MAX_VISIBLE_CANDLES
    PLOT_TARGET_MIN_BINS = max(100, PLOT_MAX_VISIBLE_CANDLES // 3)
    PLOT_PYRAMID_MINUTES = [1, 3, 5, 15, 60, 240, 1440, 10080]
    PLOT_LOD_DEBOUNCE_MS = 80
    PLOT_LOD_IMPROVEMENT_FACTOR = 0.7
    PLOT_X_MARGIN_MIN_BARS = 1000
    PLOT_X_MARGIN_FRAC = 0.10

# --- Tooltip config (kept for future grouping if needed) ---
TOOLTIP_GROUPS = [
    ("MACD", ("MACD", "MACD_SIGNAL", "MACD_HIST"), ("macd", "signal", "hist")),
]
TOOLTIP_FIELDS_ORDER = [
    ("RSI",      "RSI"),
    ("ATR",      "ATR"),
    ("MA fast",  "MA_FAST"),
    ("MA slow",  "MA_SLOW"),
    ("BB upper", "BB_UPPER"),
    ("BB mid",   "BB_MIDDLE"),
    ("BB lower", "BB_LOWER"),
    ("TP",       "TP"),
    ("SL",       "SL"),
    ("TS",       "TS"),
]

def _fmt_price(v, prec=6):
    try:
        f = float(v)
        if not np.isfinite(f):
            return "-"
        s = f"{f:.{prec}f}"
        # strip trailing zeros
        s = s.rstrip("0").rstrip(".") if "." in s else s
        return s
    except Exception:
        return "-"


WARSZAWA_TZ = pytz.timezone('Europe/Warsaw')


class PlotWidget(QWidget):
    # stały atrybut klasy (poziom klasy, NIE wewnątrz metody)
    MAX_PLOT_CANDLES = MAX_PLOT_CANDLES  # albo np. = 10_000; zależnie skąd to importujesz

    def _purge_legacy_ma_bb_items(self):
        """Remove any non-tagged legacy MA/BB overlays (safety against duplicates)."""
        try:
            plot = self.candles_plot
        except Exception:
            return

        # Remove any previously added untracked MA/BB items (best-effort)
        to_remove = []
        for it in list(getattr(plot, 'items', [])):
            try:
                tag = it.data(0) if hasattr(it, 'data') else None
            except Exception:
                tag = None
            name = getattr(it, 'name', None)

            # zostawiamy tylko NASZE otagowane elementy
            if tag in ("MA_DB_FAST", "MA_DB_SLOW", "BB_DB_UPPER", "BB_DB_MIDDLE", "BB_DB_LOWER"):
                continue

            # legacy overlaye po nazwach – do wycięcia
            if name in ("MA_FAST", "MA_SLOW", "BB_UPPER", "BB_MIDDLE", "BB_LOWER"):
                to_remove.append(it)

        for it in to_remove:
            try:
                plot.removeItem(it)
            except Exception:
                pass

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # --- toolbar
        btns_layout = QHBoxLayout()
        self.zoom_auto_btn = QPushButton("Autoscale")
        self.zoom_x_btn = QPushButton("Zoom X")
        self.zoom_y_btn = QPushButton("Zoom Y")
        self.zoom_rect_btn = QPushButton("Rectangle Zoom")
        self.zoom_rect_btn.setCheckable(True)
        self.normal_zoom_btn = QPushButton("Normal Zoom")
        for b in (self.zoom_auto_btn, self.zoom_x_btn, self.zoom_y_btn, self.zoom_rect_btn, self.normal_zoom_btn):
            btns_layout.addWidget(b)

        # --- Equity mode toggles
        self.equity_mode_group = QButtonGroup(self)
        self.eq_selected_btn = QRadioButton("Equity: Selected")
        self.eq_all_btn = QRadioButton("Equity: All")
        self.eq_selected_btn.setChecked(True)
        self.equity_mode_group.addButton(self.eq_selected_btn)
        self.equity_mode_group.addButton(self.eq_all_btn)
        btns_layout.addWidget(self.eq_selected_btn)
        btns_layout.addWidget(self.eq_all_btn)

        layout.addLayout(btns_layout)

        # --- charts
        date_axis = DateAxisItem(orientation='bottom', tz=WARSZAWA_TZ)
        self.candles_plot = pg.PlotWidget(axisItems={'bottom': date_axis}, title="Candles")
        self.candles_plot.showGrid(x=True, y=True)
        layout.addWidget(self.candles_plot, stretch=3)

        self.equity_plot = pg.PlotWidget(axisItems={'bottom': DateAxisItem(orientation='bottom', tz=WARSZAWA_TZ)},
                                         title="Equity curve")
        self.equity_plot.showGrid(x=True, y=True)
        layout.addWidget(self.equity_plot, stretch=1)

        self.sub_indicator_plots = [
            pg.PlotWidget(axisItems={'bottom': DateAxisItem(orientation='bottom', tz=WARSZAWA_TZ)})
            for _ in range(3)]
        for sp in self.sub_indicator_plots:
            sp.showGrid(x=True, y=True)
            layout.addWidget(sp, stretch=1)

        # x-link
        self.equity_plot.setXLink(self.candles_plot)
        for sp in self.sub_indicator_plots:
            sp.setXLink(self.candles_plot)

        # --- anti-smudge
        for pw in [self.candles_plot, self.equity_plot] + self.sub_indicator_plots:
            pw.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
            pw.setCacheMode(QGraphicsView.CacheNone)
            pw.setRenderHint(QPainter.Antialiasing, False)

        # --- zoom/crosshair flags
        self.zoom_mode = "auto"
        self.rect_zoom_active = False
        self.region = None

        # --- X-range limits + full-range zoom lock state ---
        self._x_min_allowed = None
        self._x_max_allowed = None
        self._x_max_range = None
        self._x_full_range_lock = False
        self._full_range_y_by_plot = {}
        self._lock_y_in_progress = False


        self.zoom_auto_btn.clicked.connect(self.autoscale)
        self.zoom_x_btn.clicked.connect(self.zoom_x)
        self.zoom_y_btn.clicked.connect(self.zoom_y)
        self.normal_zoom_btn.clicked.connect(self.normal_zoom)
        self.zoom_rect_btn.toggled.connect(self.toggle_rect_zoom)

        # --- crosshair & tooltip
        self.info_label = QLabel(self)
        self.info_label.setStyleSheet(
            "background: rgba(0,0,0,0.85); color: #FFFDDE; padding: 4px 8px; border-radius: 6px; font-size: 12px;"
        )
        self.info_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.info_label.hide()

        self._crosshair_plots = [self.candles_plot, self.equity_plot] + self.sub_indicator_plots
        self._vLines, self._hLines = {}, {}

        for p in self._crosshair_plots:
            pen_v = pg.mkPen(220, 220, 220, width=1, cosmetic=True, style=Qt.SolidLine)
            pen_h = pg.mkPen(220, 220, 220, width=1, cosmetic=True, style=Qt.SolidLine)
            v = pg.InfiniteLine(angle=90, movable=False, pen=pen_v)
            h = pg.InfiniteLine(angle=0, movable=False, pen=pen_h)
            v._is_crosshair = True
            h._is_crosshair = True
            v.setZValue(1_000_000)
            h.setZValue(1_000_000)
            p.addItem(v, ignoreBounds=True)
            p.addItem(h, ignoreBounds=True)
            self._vLines[p] = v
            self._hLines[p] = h
            h.hide()

        self._mouse_proxies = []
        for p in self._crosshair_plots:
            self._mouse_proxies.append(
                pg.SignalProxy(p.scene().sigMouseMoved, rateLimit=60,
                               slot=lambda evt, plot=p: self._on_plot_mouse_moved(evt, plot))
            )

        # --- state/cache
        self.last_df = None
        self._candlestick_item = None
        self._trade_marker_items = []
        self._bb_items = []
        self._indicator_visibility = {
            'ma1': True,
            'ma2': True,
            'bb': True,
            'trades': True,
            'equity': True,
        }

        # --- snapshot świec (numpy, do szybkich update'ów) ---
        # wypełniane w _draw_candles; na razie tylko cache, bez zmiany zachowania
        self._candles_xs = None      # timestamps (float64, sekundy UNIX)
        self._candles_open = None    # open[]
        self._candles_high = None    # high[]
        self._candles_low = None     # low[]
        self._candles_close = None   # close[]
        self._candles_widths = None  # widths[]
        # legacy containers (kept for compatibility with older code paths)
        self._ma_items = []
        # persistent overlay items (one pyqtgraph item per logical series)
        self._ma_fast_curve = None
        self._ma_slow_curve = None
        self._bb_upper_curve = None
        self._bb_middle_curve = None
        self._bb_lower_curve = None
        self._risk_lines = {}
        self._risk_labels = {}
        self._equity_x = None
        self._equity_y = None
        self._equity_curve_item = None
        self.display_config = None
        self.engine = None
        self.indicator_names = None

        # --- pełne dane + LOD (dynamiczna agregacja) ---
        self.raw_df = None
        self.raw_df_symbol = None
        self._base_candle_sec = None
        self._lod_current_level_min = None
        self._lod_current_agg_n = 1
        self._lod_last_xrange = None
        self._lod_in_update = False
        self._lod_timer = QTimer(self)
        self._lod_timer.setSingleShot(True)
        self._lod_timer.timeout.connect(self._on_lod_timeout)
        # Auto-follow (podążanie za ostatnią świecą w trybie live)
        self._auto_follow = True

        if PLOT_DYNAMIC_AGG_ENABLED:
            try:
                vb = self.candles_plot.getViewBox()
                vb.sigXRangeChanged.connect(self._on_xrange_changed)
                vb.sigRangeChanged.connect(self._on_range_changed)
            except Exception:
                logging.debug("[PlotWidget] failed to connect sigXRangeChanged", exc_info=True)

        # Subchart switching state
        self._sub_slot_options = {1: [], 2: [], 3: []}
        self._sub_slot_active = {1: None, 2: None, 3: None}
        self._last_crosshair_x = None

        # equity redraw on toggle
        self.eq_selected_btn.toggled.connect(
            lambda _: self.update_chart(self.last_df, getattr(self, '_last_trades', [])))
        self.eq_all_btn.toggled.connect(lambda _: self.update_chart(self.last_df, getattr(self, '_last_trades', [])))

    def leaveEvent(self, event):
        """Hide tooltip and crosshair when leaving the whole widget."""
        try:
            self.info_label.hide()
        except Exception:
            pass
        try:
            for p in getattr(self, "_crosshair_plots", []):
                try:
                    self._vLines[p].hide()
                    self._hLines[p].hide()
                except Exception:
                    pass
        except Exception:
            pass
        super().leaveEvent(event)

    # ========== Public API ==========
    def set_display_config(self, display_config: dict):
        self.display_config = display_config if isinstance(display_config, dict) else {}
        try:
            self._compute_sub_slot_options()
        except Exception:
            pass

    def get_sub_slot_options(self):
        return self._compute_sub_slot_options()

    def set_active_sub_slot(self, slot: int, indicator_name: str):
        if slot not in (1, 2, 3):
            return
        opts = self._sub_slot_options.get(slot) or []
        if indicator_name in opts:
            self._sub_slot_active[slot] = indicator_name
            if getattr(self, "last_df", None) is not None:
                try:
                    self._draw_subcharts(self.last_df)
                    try:
                        self._autoscale_subcharts_for_window(pad=0.15)
                    except Exception:
                        try:
                            self._autoscale_subcharts_in_window(pad=0.15)
                        except Exception:
                            pass
                except Exception:
                    pass

    def cycle_sub_slot(self, slot: int, step: int = 1):
        if slot not in (1, 2, 3):
            return
        opts = self._sub_slot_options.get(slot) or []
        if not opts:
            return
        cur = self._sub_slot_active.get(slot)
        idx = opts.index(cur) if cur in opts else -1
        new = opts[(idx + step) % len(opts)]
        self._sub_slot_active[slot] = new
        if getattr(self, "last_df", None) is not None:
            try:
                self._draw_subcharts(self.last_df)
                try:
                    self._autoscale_subcharts_for_window(pad=0.15)
                except Exception:
                    try:
                        self._autoscale_subcharts_in_window(pad=0.15)
                    except Exception:
                        pass
            except Exception:
                pass

    def _compute_sub_slot_options(self):
        cfg = (self.display_config or {}) if isinstance(self.display_config, dict) else {}
        buckets = {1: [], 2: [], 3: []}
        for name, comp in cfg.items():
            if not isinstance(comp, dict) or not comp.get("enabled", False):
                continue
            disp = str(comp.get("display", "")).lower().strip()
            if disp in ("sub1", "sub2", "sub3"):
                slot_no = {"sub1": 1, "sub2": 2, "sub3": 3}[disp]
                order = comp.get("slot", 0)
                try:
                    order = int(order)
                except Exception:
                    order = 0
                buckets[slot_no].append((order, str(name)))
        opts = {1: [], 2: [], 3: []}
        for s in (1, 2, 3):
            arr = sorted(buckets[s], key=lambda kv: (kv[0], kv[1]))
            opts[s] = [nm for _, nm in arr][:3]
            if self._sub_slot_active.get(s) not in opts[s]:
                self._sub_slot_active[s] = opts[s][0] if opts[s] else None
        self._sub_slot_options = opts
        return opts

    def set_engine(self, engine):
        self.engine = engine

    
    def set_raw_history(self, df, symbol=None):
        """
        Zapisuje pełne dane (raw_df) do dynamicznej agregacji oraz
        aktualizuje limity osi X. Wywoływane z MainWindow po merge
        świec + wskaźników.
        """
        try:
            if df is None or getattr(df, "empty", True):
                self.raw_df = None
                self.raw_df_symbol = symbol
                self._base_candle_sec = None
                return

            import pandas as pd  # lokalnie, żeby unikać konfliktów przy imporcie

            self.raw_df = df.copy()
            self.raw_df_symbol = symbol

            # Wylicz bazowy interwał z timestampów (sekundy)
            try:
                ts = pd.to_numeric(self.raw_df.get("timestamp", None), errors="coerce")
                ts = ts[ts.notna()]
                if ts.size > 3:
                    dt = ts.sort_values().diff()
                    dt = dt[dt > 0]
                    if dt.notna().any():
                        base_sec = float(dt.median())
                        if base_sec < 1.0:
                            base_sec = 60.0
                        self._base_candle_sec = base_sec
            except Exception:
                self._base_candle_sec = None

            if self._base_candle_sec is None:
                # konserwatywny default – 1 minuta
                self._base_candle_sec = 60.0

            # Zaktualizuj limity X (zoom/scroll)
            self._update_x_limits_from_raw()

        except Exception as e:
            logging.warning(f"[PlotWidget.set_raw_history] failed: {e}")

    def _update_x_limits_from_raw(self):
        """
        Wyznacza limity przewijania X:
          - nie dalej niż dane ± margines,
          - margines >= PLOT_X_MARGIN_MIN_BARS świeczek i >= PLOT_X_MARGIN_FRAC długości danych.
        """
        df = self.raw_df
        if df is None or getattr(df, "empty", True):
            return

        try:
            import pandas as pd
            xs = pd.to_numeric(df["timestamp"], errors="coerce")
            xs = xs[xs.notna()]
            if xs.empty:
                return
            x_min = float(xs.min())
            x_max = float(xs.max())
            span = max(1.0, x_max - x_min)

            base_sec = self._base_candle_sec or 60.0
            n_bars = len(xs)

            margin_bars = max(PLOT_X_MARGIN_MIN_BARS, int(n_bars * PLOT_X_MARGIN_FRAC))
            margin_sec_from_bars = margin_bars * base_sec
            margin_sec_from_frac = span * PLOT_X_MARGIN_FRAC
            margin_sec = max(margin_sec_from_bars, margin_sec_from_frac)

            x_min_allowed = x_min - margin_sec
            x_max_allowed = x_max + margin_sec
            max_range = (x_max - x_min) + 2.0 * margin_sec

            # zapamiętaj limity X do blokady zoomu
            self._x_min_allowed = float(x_min_allowed)
            self._x_max_allowed = float(x_max_allowed)
            self._x_max_range = float(max_range)

            plots = [self.candles_plot, self.equity_plot] + list(getattr(self, "sub_indicator_plots", []))
            for plot in plots:
                try:
                    vb = plot.getViewBox()
                    vb.setLimits(
                        xMin=x_min_allowed,
                        xMax=x_max_allowed,
                        maxXRange=max_range,
                    )
                except Exception:
                    continue

        except Exception as e:
            logging.debug(f"[PlotWidget._update_x_limits_from_raw] failed: {e}")



    def set_auto_follow(self, enabled: bool):
        """Ustawia tryb auto-follow (podążanie za ostatnią świecą).

        Jeśli włączamy auto-follow i mamy jakieś dane, od razu skaczemy
        do ostatniej świecy, zachowując bieżącą szerokość zakresu X,
        a w trybie LOD odświeżamy agregację pod nowy zakres.
        """
        try:
            self._auto_follow = bool(enabled)
        except Exception:
            self._auto_follow = bool(enabled)
        if self._auto_follow:
            try:
                self.jump_to_latest()
            except Exception:
                logging.debug("[PlotWidget.set_auto_follow] jump_to_latest failed", exc_info=True)

    def jump_to_latest(self):
        """Przewija widok na ostatnią świecę, zachowując obecny zoom.

        Używane głównie przez auto-follow; bazuje na raw_df (jeśli jest),
        a w razie potrzeby na last_df.
        """
        try:
            df = None
            if self.raw_df is not None and not getattr(self.raw_df, 'empty', True):
                df = self.raw_df
            elif getattr(self, 'last_df', None) is not None and not getattr(self.last_df, 'empty', True):
                df = self.last_df
            if df is None:
                return

            import pandas as pd
            xs = pd.to_numeric(df.get('timestamp', None), errors='coerce')
            xs = xs[xs.notna()]
            if xs.empty:
                return

            last_ts = float(xs.iloc[-1])

            vb = self.candles_plot.getViewBox()
            try:
                (x0, x1), _ = vb.viewRange()
                span = float(x1 - x0)
                if not np.isfinite(span) or span <= 0:
                    raise Exception('bad span')
            except Exception:
                # fallback: użyj maksymalnego zakresu albo całości danych
                span = None
                try:
                    span = float(getattr(self, '_x_max_range', None) or 0)
                except Exception:
                    span = 0.0
                if not np.isfinite(span) or span <= 0:
                    try:
                        span = float(xs.max() - xs.min())
                    except Exception:
                        span = 60.0
                if span <= 0:
                    span = 60.0
            x1_new = last_ts
            x0_new = x1_new - span
            try:
                vb.setXRange(x0_new, x1_new, padding=0.0)
            except Exception:
                logging.debug("[PlotWidget.jump_to_latest] setXRange failed", exc_info=True)

            if PLOT_DYNAMIC_AGG_ENABLED:
                try:
                    self._lod_last_xrange = (float(x0_new), float(x1_new))
                    if not self._lod_timer.isActive():
                        self._lod_timer.start(int(PLOT_LOD_DEBOUNCE_MS))
                except Exception:
                    logging.debug("[PlotWidget.jump_to_latest] LOD schedule failed", exc_info=True)
        except Exception as e:
            logging.debug(f"[PlotWidget.jump_to_latest] {e}")

    def _on_range_changed(self, vb, ranges):
        """
        Globalny handler zmian zakresu (X i Y) – używany tylko do blokady zoomu,
        gdy jesteśmy na maksymalnym oddaleniu po osi X.
        """
        if not PLOT_DYNAMIC_AGG_ENABLED:
            return
        if self._lock_y_in_progress:
            return
        # w trybie Zoom Y only pozwalamy zawsze zoomować po Y
        if getattr(self, "zoom_mode", "auto") == "y":
            return
        if ranges is None or len(ranges) != 2:
            return
        try:
            (x0, x1), (y0, y1) = ranges
            x0 = float(x0); x1 = float(x1)
        except Exception:
            return

        if self._x_max_range is None:
            return

        span_x = x1 - x0
        if span_x <= 0:
            return

        # Czy jesteśmy praktycznie na pełnym zakresie X?
        at_full_x = span_x >= float(self._x_max_range) * 0.999
        if at_full_x:
            plots = [self.candles_plot, self.equity_plot] + list(getattr(self, "sub_indicator_plots", []))
            if not self._x_full_range_lock:
                # Pierwsze wejście w stan pełnego X – zapamiętaj YRange wszystkich wykresów
                self._x_full_range_lock = True
                self._full_range_y_by_plot = {}
                for p in plots:
                    try:
                        vb2 = p.getViewBox()
                        vr = vb2.viewRange()
                        if vr and len(vr) == 2:
                            (_, _), (y0p, y1p) = vr
                            self._full_range_y_by_plot[id(vb2)] = (float(y0p), float(y1p))
                    except Exception:
                        continue
            else:
                # Jesteśmy na pełnym X i użytkownik dalej próbuje oddalać – blokujemy Y
                self._lock_y_in_progress = True
                try:
                    for p in plots:
                        try:
                            vb2 = p.getViewBox()
                            key = id(vb2)
                            if key in self._full_range_y_by_plot:
                                y0p, y1p = self._full_range_y_by_plot[key]
                                vb2.setYRange(y0p, y1p, padding=0.0)
                        except Exception:
                            continue
                finally:
                    self._lock_y_in_progress = False
        else:
            # wyszliśmy z pełnego zakresu X – zdejmujemy blokadę
            self._x_full_range_lock = False
            self._full_range_y_by_plot = {}
    def _on_xrange_changed(self, vb, xrange_):
        """
        Handler zmian zakresu X (zoom/pan). Odpala tylko debounce – właściwa
        logika LOD jest w _on_lod_timeout.
        """
        if not PLOT_DYNAMIC_AGG_ENABLED:
            return
        if self._lod_in_update:
            # własny redraw – nie reagujemy
            return
        if xrange_ is None or len(xrange_) != 2:
            return

        try:
            x0, x1 = float(xrange_[0]), float(xrange_[1])
        except Exception:
            return

        # Jeśli użytkownik przesunął widok wyraźnie w lewo od ostatniej świecy,
        # wyłączamy auto-follow (nie będziemy już auto-scrollować do końca).
        try:
            if self._auto_follow:
                df_edge = None
                if self.raw_df is not None and not getattr(self.raw_df, 'empty', True):
                    df_edge = self.raw_df
                elif getattr(self, 'last_df', None) is not None and not getattr(self.last_df, 'empty', True):
                    df_edge = self.last_df
                if df_edge is not None and 'timestamp' in df_edge.columns:
                    import pandas as pd
                    xs_edge = pd.to_numeric(df_edge['timestamp'], errors='coerce')
                    xs_edge = xs_edge[xs_edge.notna()]
                    if not xs_edge.empty:
                        last_ts = float(xs_edge.iloc[-1])
                        base_sec = float(self._base_candle_sec or 60.0)
                        # jeśli prawa krawędź jest wyraźnie przed ostatnią świecą -> manualny pan
                        if float(x1) < (last_ts - base_sec):
                            self._auto_follow = False
        except Exception:
            pass

        # Zapamiętaj zakres X i odpal debounce dla LOD.
        self._lod_last_xrange = (float(x0), float(x1))
        try:
            self._lod_timer.start(int(PLOT_LOD_DEBOUNCE_MS))
        except Exception:
            pass

    def _on_lod_timeout(self):
        if not PLOT_DYNAMIC_AGG_ENABLED:
            return
        if self.raw_df is None or self._lod_last_xrange is None:
            return
        x_min, x_max = self._lod_last_xrange
        self._rebuild_lod_for_range(x_min, x_max)

    
    def _rebuild_lod_for_range(self, x_min: float, x_max: float):
        """
        Dynamiczna agregacja na podstawie aktualnego zakresu X.

        Na podstawie span_sec wybieramy poziom z piramidy (w minutach),
        wyliczamy agg_n względem bazowego interwału, a następnie
        AGREGUJEMY TYLKO WIDOCZNY FRAGMENT raw_df (po timestampach).

        Bardzo ważne:
        - nie przycinamy tutaj do PLOT_MAX_VISIBLE_CANDLES; limit dotyczy
          już samego zakresu X + wyboru poziomu z piramidy.
        - nawet jeśli poziom (agg_n) się nie zmieni, ale zakres X jest inny,
          przebudowujemy widok (brak early-return po agg_n).
        """
        if not PLOT_DYNAMIC_AGG_ENABLED:
            return

        df = self.raw_df
        if df is None or getattr(df, "empty", True):
            return

        try:
            span_sec = max(1.0, float(x_max) - float(x_min))
            if span_sec <= 0:
                return

            # --- bazowy interwał (w minutach) ---
            try:
                from config import CANDLE_INTERVAL_MINUTES
                base_min = max(1, int(CANDLE_INTERVAL_MINUTES))
            except Exception:
                # jeśli nie ma w configu, próbujemy estymować z _base_candle_sec
                try:
                    base_min = max(1, int(round((self._base_candle_sec or 60.0) / 60.0)))
                except Exception:
                    base_min = 1

            # --- piramida poziomów (w minutach) ---
            try:
                levels = [
                    L for L in PLOT_PYRAMID_MINUTES
                    if L >= base_min and (L % base_min) == 0
                ]
            except Exception:
                levels = [base_min]
            if not levels:
                levels = [base_min]

            # --- target liczby świeczek ---
            try:
                target_min = float(PLOT_TARGET_MIN_BINS)
                target_max = float(PLOT_TARGET_MAX_BINS)
            except Exception:
                target_min, target_max = 300.0, 900.0
            target_mid = 0.5 * (target_min + target_max)

            ideal_minutes = span_sec / (target_mid * 60.0)
            if ideal_minutes <= 0:
                ideal_minutes = base_min

            # najbliższy poziom piramidy do ideal_minutes
            try:
                best_level = min(levels, key=lambda L: abs(L - ideal_minutes))
            except Exception:
                best_level = base_min

            cur_level = self._lod_current_level_min or base_min
            bins_est_cur = span_sec / (cur_level * 60.0)
            bins_est_best = span_sec / (best_level * 60.0)

            err_cur = abs(bins_est_cur - target_mid)
            err_best = abs(bins_est_best - target_mid)

            lo = target_min * 0.7
            hi = target_max * 1.3
            try:
                improv_factor = float(PLOT_LOD_IMPROVEMENT_FACTOR or 0.7)
            except Exception:
                improv_factor = 0.7

            # --- histereza wyboru poziomu ---
            if lo <= bins_est_cur <= hi and err_best >= err_cur * improv_factor:
                chosen_level = cur_level
            else:
                chosen_level = best_level

            # ile bazowych świec sklejamy razem
            agg_n = max(1, int(round(chosen_level / base_min)))

            # UWAGA: brak early-return po agg_n – zmiana zakresu X zawsze może
            # wymagać przebudowy nawet przy tym samym poziomie.

            # --- wycięcie widocznego fragmentu raw_df po timestamp ---
            df_sub = None
            if getattr(self, "_raw_ts", None) is not None and len(self._raw_ts) == len(df):
                ts = self._raw_ts  # posortowane timestampy
                i0 = int(np.searchsorted(ts, x_min, side="left"))
                i1 = int(np.searchsorted(ts, x_max, side="right")) - 1
                if i1 < i0:
                    i1 = i0
                i0 = max(0, min(i0, len(ts) - 1))
                i1 = max(0, min(i1, len(ts) - 1))
                df_sub = df.iloc[i0 : i1 + 1].copy()
            else:
                # fallback: maska po timestamp
                import pandas as pd
                xs = pd.to_numeric(df["timestamp"], errors="coerce").astype(float)
                mask = np.isfinite(xs) & (xs >= x_min) & (xs <= x_max)
                if mask.any():
                    df_sub = df.loc[mask].copy()

            if df_sub is None or getattr(df_sub, "empty", True):
                return

            # --- agregacja tylko tej podsekcji ---
            # tutaj NIE przycinamy do PLOT_MAX_VISIBLE_CANDLES – limit wynika z piramidy
            df_plot = build_plot_df(
                df_sub,
                agg_n=agg_n,
                max_plot=0,  # 0 => brak tail() w build_plot_df
                symbol=self.raw_df_symbol,
                log_prefix="[PlotWidget LOD]",
            )
            if df_plot is None or getattr(df_plot, "empty", True):
                return

            # zaktualizuj stan dopiero po udanym przeliczeniu
            self._lod_current_level_min = chosen_level
            self._lod_current_agg_n = agg_n

            # --- podmiana widoku (z zabezpieczeniem przed rekurencją) ---
            self._lod_in_update = True
            try:
                trades = getattr(self, "_last_trades", None) or []
                self.update_chart(df_plot, trades)
            finally:
                self._lod_in_update = False

        except Exception as e:
            logging.warning(f"[PlotWidget._rebuild_lod_for_range] {e}")

    def set_history(self, df, trades=None):
        try:
            if df is None or getattr(df, "empty", True):
                self.update_chart(None, [])
                return
            self.update_chart(df, trades or [])
        except Exception as e:
            logging.warning(f"[PlotWidget.set_history] failed: {e}")

    def update_live(self, live_row: dict):
        """
        Aktualizacja danych live na wykresie.

        Szybka ścieżka:
        - jeśli mamy snapshot świec (_candles_xs, _candles_open/..)
          ORAZ CandlestickItem,
        - i tick dotyczy TEJ SAMEJ świecy (taki sam timestamp jak ostatni),
          to:
            * aktualizujemy tylko ostatni wiersz w self.last_df (OHLC),
            * aktualizujemy ostatnie wartości w snapshotach numpy,
            * podajemy nową tablicę do self._candlestick_item.update_data(arr),
          BEZ wywoływania update_chart (czyli bez przeliczania MA/BB/subchartów).

        Wolniejsza ścieżka (fallback):
        - jeśli tick tworzy NOWĄ świecę (timestamp > ostatni),
        - albo snapshot nie jest gotowy,
        to:
            * update/append wiersza w self.last_df
            * i pełne update_chart(df, _last_trades).
        """
        try:
            df = self.last_df
            if df is None or getattr(df, "empty", True) or not live_row:
                return

            # --- 0) Strażnik symbolu – nie mieszamy ticków z innego symbolu ---
            live_sym = live_row.get("symbol")
            if live_sym is not None and "symbol" in df.columns:
                try:
                    last_sym = df["symbol"].iloc[-1]
                    if last_sym is not None and str(last_sym) != str(live_sym):
                        # tick nie od aktualnego symbolu -> ignorujemy
                        return
                except Exception:
                    pass

            # --- 1) Timestamp jako float (sekundy, UTC) ---
            ts = float(live_row.get("timestamp", np.nan))
            if not np.isfinite(ts):
                return

            # sprawdzamy, czy mamy snapshot i CandlestickItem -> wtedy można spróbować szybkiej ścieżki
            xs_snap = getattr(self, "_candles_xs", None)
            candles_item = getattr(self, "_candlestick_item", None)
            has_snapshot = (
                xs_snap is not None
                and isinstance(xs_snap, np.ndarray)
                and xs_snap.size > 0
                and candles_item is not None
            )

            # helper: wolniejsza ścieżka (stare zachowanie, ale bez kopii DF)
            def _fallback_full_redraw():
                nonlocal df
                try:
                    xs = pd.to_numeric(df["timestamp"], errors="coerce").values.astype(float)
                except Exception:
                    return

                if xs.size == 0:
                    return

                last_ts = xs[-1]
                if not np.isfinite(last_ts):
                    return

                # jeśli tick jest starszy niż ostatnia świeca na wykresie -> ignorujemy
                if ts < last_ts - 1e-6:
                    return

                same_candle = abs(last_ts - ts) < 1e-6

                if same_candle:
                    # aktualizujemy ostatni wiersz DF
                    last_idx = df.index[-1]
                    for k in ("open", "high", "low", "close"):
                        if k in live_row and k in df.columns:
                            try:
                                df.at[last_idx, k] = float(live_row[k])
                            except Exception:
                                pass
                else:
                    # dopinamy nową świecę
                    symbol = live_row.get("symbol")
                    if symbol is None and "symbol" in df.columns:
                        try:
                            symbol = df["symbol"].iloc[-1]
                        except Exception:
                            symbol = None

                    new_row = {
                        "timestamp": ts,
                        "open": float(live_row.get("open", np.nan)),
                        "high": float(live_row.get("high", np.nan)),
                        "low": float(live_row.get("low", np.nan)),
                        "close": float(live_row.get("close", np.nan)),
                    }
                    if symbol is not None:
                        new_row["symbol"] = symbol

                    # dopasuj strukturę do istniejącego DF
                    for col in df.columns:
                        if col not in new_row:
                            new_row[col] = np.nan

                    try:
                        df.loc[len(df)] = new_row
                    except Exception:
                        try:
                            self.last_df = pd.concat([df, pd.DataFrame([new_row])],
                                                     ignore_index=True)
                            df = self.last_df
                        except Exception:
                            return

                # pełne odrysowanie na bazie zaktualizowanego self.last_df
                self.update_chart(df, getattr(self, "_last_trades", []))

            # jeśli nie mamy snapshotu -> od razu fallback
            if not has_snapshot:
                _fallback_full_redraw()
                return

            # mamy snapshot i CandlestickItem -> próbujemy szybkiej ścieżki
            xs = xs_snap
            if xs.size == 0:
                return

            last_ts = xs[-1]
            if not np.isfinite(last_ts):
                return

            # stary tick -> ignorujemy
            if ts < last_ts - 1e-6:
                return

            same_candle = np.isfinite(last_ts) and abs(last_ts - ts) < 1e-6

            if same_candle:
                # --- szybka ścieżka: update ostatniej świecy in-place ---
                last_idx = df.index[-1]

                new_o = live_row.get("open")
                new_h = live_row.get("high")
                new_l = live_row.get("low")
                new_c = live_row.get("close")

                # 1) aktualizujemy DF
                try:
                    if new_o is not None and "open" in df.columns:
                        df.at[last_idx, "open"] = float(new_o)
                    if new_h is not None and "high" in df.columns:
                        df.at[last_idx, "high"] = float(new_h)
                    if new_l is not None and "low" in df.columns:
                        df.at[last_idx, "low"] = float(new_l)
                    if new_c is not None and "close" in df.columns:
                        df.at[last_idx, "close"] = float(new_c)
                except Exception:
                    pass

                # 2) aktualizujemy snapshot numpy (tylko ostatni element)
                if getattr(self, "_candles_open", None) is not None and self._candles_open.size == xs.size:
                    if new_o is not None:
                        self._candles_open[-1] = float(new_o)
                if getattr(self, "_candles_high", None) is not None and self._candles_high.size == xs.size:
                    if new_h is not None:
                        self._candles_high[-1] = float(new_h)
                if getattr(self, "_candles_low", None) is not None and self._candles_low.size == xs.size:
                    if new_l is not None:
                        self._candles_low[-1] = float(new_l)
                if getattr(self, "_candles_close", None) is not None and self._candles_close.size == xs.size:
                    if new_c is not None:
                        self._candles_close[-1] = float(new_c)

                widths = getattr(self, "_candles_widths", None)
                if widths is None or not isinstance(widths, np.ndarray) or widths.size != xs.size:
                    # w razie problemów – fallback na prostą, stałą szerokość
                    widths = np.full_like(xs, 30.0, dtype=np.float64)
                    self._candles_widths = widths

                # 3) budujemy tablicę [x, o, c, l, h, w] i aktualizujemy CandlestickItem
                arr = np.column_stack([
                    xs,
                    self._candles_open,
                    self._candles_close,
                    self._candles_low,
                    self._candles_high,
                    widths,
                ])

                try:
                    candles_item.update_data(arr)
                except Exception:
                    # jeśli coś pójdzie nie tak – pełny redraw
                    self.update_chart(df, getattr(self, "_last_trades", []))
                return

            # --- nowa świeca albo timestamp się nie zgadza -> fallback ---
            _fallback_full_redraw()

        except Exception as e:
            logging.debug(f"[PlotWidget.update_live] {e}")

    def append_closed(self, closed_row: dict, new_live: dict = None):
        """
        Dopinanie świecy zamkniętej (i opcjonalnie nowej live) BEZ klonowania DF.
        Pracujemy in-place na self.last_df, a potem odświeżamy wykres przez update_chart.
        """
        try:
            df = self.last_df
            if df is None or getattr(df, 'empty', True):
                return

            def _append_row(row: dict):
                nonlocal df
                if not row:
                    return

                ts = float(row.get('timestamp', np.nan))
                if not np.isfinite(ts):
                    return

                symbol = row.get('symbol')
                if symbol is None and 'symbol' in df.columns:
                    symbol = df['symbol'].iloc[-1]
                new_row = dict(row)
                if symbol is not None:
                    new_row['symbol'] = symbol

                # Upewnij się, że timestamp jest floatem
                try:
                    new_row['timestamp'] = float(new_row.get('timestamp', ts))
                except Exception:
                    new_row['timestamp'] = ts

                # Dobuduj brakujące kolumny jako NaN, żeby DF się nie wysypał
                for col in df.columns:
                    if col not in new_row:
                        new_row[col] = np.nan

                try:
                    df.loc[len(df)] = new_row
                except Exception:
                    try:
                        self.last_df = pd.concat([df, pd.DataFrame([new_row])],
                                                 ignore_index=True)
                        df = self.last_df
                    except Exception:
                        return

            # najpierw zamknięta świeca, potem nowa live (jeśli jest)
            _append_row(closed_row)
            _append_row(new_live)

            # twardy limit długości DF – trzymamy maksymalnie MAX_PLOT_CANDLES świec
            try:
                if len(df) > self.MAX_PLOT_CANDLES:
                    df = df.tail(self.MAX_PLOT_CANDLES)
                    self.last_df = df
            except Exception:
                # w razie problemu – po prostu jedziemy dalej na df bez cięcia
                pass

            # Spróbuj zaktualizować również raw_df (pod dynamiczny LOD) – na razie
            # tylko o zamkniętą świecę (bez nowej live, ta będzie doszyta w kolejnym kroku).
            try:
                if self.raw_df is not None and not getattr(self.raw_df, 'empty', True):
                    raw_df = self.raw_df
                    if isinstance(closed_row, dict) and 'timestamp' in closed_row:
                        new_raw = dict(closed_row)
                        # dbaj o spójność kolumn – dopasuj do raw_df.columns
                        for col in raw_df.columns:
                            if col not in new_raw:
                                new_raw[col] = np.nan
                        try:
                            new_raw['timestamp'] = float(new_raw.get('timestamp'))
                        except Exception:
                            pass
                        try:
                            raw_df.loc[len(raw_df)] = new_raw
                        except Exception:
                            try:
                                self.raw_df = pd.concat([raw_df, pd.DataFrame([new_raw])],
                                                        ignore_index=True)
                                raw_df = self.raw_df
                            except Exception:
                                raw_df = None
                    # miękki limit długości raw_df – może być większy niż na wykresie
                    try:
                        max_raw = int(self.MAX_PLOT_CANDLES) * 3
                    except Exception:
                        max_raw = int(self.MAX_PLOT_CANDLES)
                    try:
                        if raw_df is not None and len(raw_df) > max_raw:
                            self.raw_df = raw_df.tail(max_raw)
                            raw_df = self.raw_df
                    except Exception:
                        pass

                    # po zmianie raw_df odśwież limity X (do zoom/scroll)
                    try:
                        self._update_x_limits_from_raw()
                    except Exception:
                        logging.debug('[PlotWidget.append_closed] _update_x_limits_from_raw failed', exc_info=True)
            except Exception:
                logging.debug('[PlotWidget.append_closed] raw_df update failed', exc_info=True)

            # pełny refresh na bazie self.last_df
            self.update_chart(df, getattr(self, '_last_trades', []))

            # Jeśli włączony jest dynamiczny LOD oraz auto-follow, spróbuj przesunąć
            # widok na koniec i przebudować LOD pod nowy zakres.
            try:
                if PLOT_DYNAMIC_AGG_ENABLED and getattr(self, '_auto_follow', False):
                    df_edge = None
                    if self.raw_df is not None and not getattr(self.raw_df, 'empty', True):
                        df_edge = self.raw_df
                    elif getattr(self, 'last_df', None) is not None and not getattr(self.last_df, 'empty', True):
                        df_edge = self.last_df
                    if df_edge is not None and 'timestamp' in df_edge.columns:
                        import pandas as pd
                        xs = pd.to_numeric(df_edge['timestamp'], errors='coerce')
                        xs = xs[xs.notna()]
                        if not xs.empty:
                            last_ts = float(xs.iloc[-1])
                            vb = self.candles_plot.getViewBox()
                            try:
                                (x0, x1), _ = vb.viewRange()
                                span = float(x1 - x0)
                                if not np.isfinite(span) or span <= 0:
                                    raise Exception('bad span')
                            except Exception:
                                span = float(getattr(self, '_x_max_range', None) or 0)
                                if not np.isfinite(span) or span <= 0:
                                    span = 60.0
                            x1_new = last_ts
                            x0_new = x1_new - span
                            try:
                                vb.setXRange(x0_new, x1_new, padding=0.0)
                            except Exception:
                                logging.debug('[PlotWidget.append_closed] setXRange failed', exc_info=True)

                            try:
                                self._lod_last_xrange = (float(x0_new), float(x1_new))
                                if not self._lod_timer.isActive():
                                    self._lod_timer.start(int(PLOT_LOD_DEBOUNCE_MS))
                            except Exception:
                                logging.debug('[PlotWidget.append_closed] LOD schedule failed', exc_info=True)
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"[PlotWidget.append_closed] {e}")


    # ---- Visibility from GUI (checkboxes) ----
    def apply_indicator_visibility(self, state: dict):
        try:
            self._indicator_visibility.update({k: bool(v) for k, v in (state or {}).items()})
        except Exception:
            pass
        try:
            if getattr(self, "last_df", None) is not None:
                self.update_chart(self.last_df, getattr(self, "_last_trades", []))
        except Exception:
            pass

    def set_ma1_visible(self, flag: bool): self.apply_indicator_visibility({"ma1": flag})
    def set_ma2_visible(self, flag: bool): self.apply_indicator_visibility({"ma2": flag})
    def set_bb_visible(self, flag: bool):  self.apply_indicator_visibility({"bb":  flag})
    def set_trades_visible(self, flag: bool): self.apply_indicator_visibility({"trades": flag})

    # ========== Main update ==========
    
    def update_chart(self, df, trades=None):
        """
        Główny refresh wykresu:
          - minimalizuje liczbę operacji na scenie (bez full clear),
          - reużywa istniejących obiektów (świece, MA, BB, equity, markery),
          - zostawia crosshairy nietknięte.
        Publiczne API pozostaje bez zmian: (df, trades) -> odrysowanie wszystkiego.
        """
        trades = trades or []
        self._last_trades = trades

        # pusta ramka -> czyścimy widoki, ale nie dotykamy konfiguracji/layoutu
        if df is None or getattr(df, "empty", True):
            self.last_df = None
            self.info_label.hide()

            # Candles + overlaye główne
            try:
                if getattr(self, "_candlestick_item", None) is not None:
                    self.candles_plot.removeItem(self._candlestick_item)
            except Exception:
                pass
            self._candlestick_item = None

            # MA / BB / risk lines / markery
            try:
                self._clear_bb_items()
            except Exception:
                pass
            try:
                self._clear_risk_lines()
            except Exception:
                pass
            for it in list(getattr(self, "_trade_marker_items", [])):
                try:
                    self.candles_plot.removeItem(it)
                except Exception:
                    pass
            self._trade_marker_items = []

            # Equity + subcharty
            self.equity_plot.clear()
            self._equity_x = None
            self._equity_y = None
            self._equity_curve_item = None
            self._reinstall_crosshair_on_plot(self.equity_plot)

            for i, sp in enumerate(self.sub_indicator_plots, start=1):
                sp.clear()
                self._reinstall_crosshair_on_plot(sp)
                sp.setTitle(f"Sub {i}")

            self._reinstall_crosshair_all()
            return

        # --- Ucinamy DF do limitu i robimy kopię roboczą ---
        df = df.tail(self.MAX_PLOT_CANDLES).copy()

        # DEBUG: info o DF trafiającym na wykres
        try:
            nrows, ncols = len(df), len(df.columns)
            cols_str = ", ".join(map(str, df.columns))
            logging.debug(
                "[PlotWidget.update_chart] incoming DF: rows=%d, cols=%d, columns=%s",
                nrows, ncols, cols_str
            )
            if "timestamp" in df.columns:
                ts = df["timestamp"]
                ts_num = pd.to_numeric(ts, errors="coerce")
                if ts_num.notna().any():
                    ts_min = float(ts_num.min())
                    ts_max = float(ts_num.max())
                    logging.debug("[PlotWidget.update_chart] timestamp range: %s .. %s", ts_min, ts_max)
        except Exception:
            pass

        # --- zapewnij kolumnę timestamp (sekundy UNIX, preferujemy close_time) ---
        try:
            if "timestamp" not in df.columns:
                if "close_time" in df.columns:
                    df["timestamp"] = (
                        pd.to_datetime(df["close_time"], utc=True, errors="coerce")
                        .astype("int64") // 10 ** 9
                    ).astype(float)
                elif isinstance(df.index, pd.DatetimeIndex):
                    df["timestamp"] = (
                        pd.to_datetime(df.index, utc=True)
                        .astype("int64") // 10 ** 9
                    ).astype(float)
            else:
                df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype(float)
        except Exception:
            pass

        self.last_df = df

        # Kanoniczne kolumny do tooltipów
        self._ensure_canonical_indicator_columns(self.last_df)

        # Candles (persistujący CandlestickItem + numpy)
        self._draw_candles(self.last_df)

        # display_config/indicator_names z engine (lazy init)
        if self.display_config is None and getattr(self, "engine", None) is not None:
            strat = getattr(self.engine, "strategy", None)
            if strat and hasattr(strat, "get_display_config"):
                try:
                    self.display_config = strat.get_display_config()
                except Exception:
                    self.display_config = None
        if self.indicator_names is None and getattr(self, "engine", None) is not None:
            try:
                self.indicator_names = list(getattr(self.engine, "indicator_names", []) or [])
            except Exception:
                self.indicator_names = None

        # Overlaye na głównym wykresie (MA/BB/TP/SL/TS)
        try:
            self._draw_ma(self.last_df)
        except Exception as e:
            logging.debug(f"[PlotWidget] MA draw error: {e}")
        try:
            if self._indicator_visibility.get("bb", True):
                self._draw_bollinger(self.last_df)
            else:
                # gdy użytkownik wyłączył BB – schowaj istniejące krzywe
                self._clear_bb_items()
        except Exception as e:
            logging.debug(f"[PlotWidget] BB draw error: {e}")
        try:
            self._draw_risk_levels(self.last_df)
        except Exception as e:
            logging.debug(f"[PlotWidget] risk lines error: {e}")

        # Subcharty (RSI/MACD/ATR etc.)
        try:
            self._draw_subcharts(self.last_df)
            try:
                self._autoscale_subcharts_for_window(pad=0.15)
            except Exception:
                try:
                    self._autoscale_subcharts_in_window(pad=0.15)
                except Exception:
                    pass
        except Exception as e:
            logging.debug(f"[PlotWidget] subcharts error: {e}")

        # Equity
        try:
            self._draw_equity_curve(self.last_df, trades)
        except Exception as e:
            logging.debug(f"[PlotWidget] equity curve error: {e}")

        # Markery transakcji – usuwamy stare, rysujemy nowe (liczność << liczby świec)
        for it in list(getattr(self, "_trade_marker_items", [])):
            try:
                self.candles_plot.removeItem(it)
            except Exception:
                pass
        self._trade_marker_items = []
        if self._indicator_visibility.get("trades", True):
            try:
                self._draw_trade_markers(self.last_df, trades)
            except Exception as e:
                logging.debug(f"[PlotWidget] trade markers error: {e}")

        # Crosshair musi zostać podpięty do aktualnych ViewBoxów
        self._reinstall_crosshair_all()

# ========== Helpers: canonical indicator columns ==========
    def _ensure_canonical_indicator_columns(self, df: pd.DataFrame):
        wanted = [
            "MA_FAST", "MA_SLOW",
            "BB_MIDDLE", "BB_UPPER", "BB_LOWER",
            "ATR", "MACD", "MACD_SIGNAL", "MACD_HIST",
            "TP", "SL", "TS"
        ]
        for name in wanted:
            if name in df.columns:
                continue
            for cand in (f"{name}_ind", f"{name}_x", f"{name}_y", name.upper(), name.lower()):
                if cand in df.columns:
                    try:
                        df[name] = pd.to_numeric(df[cand], errors='coerce')
                    except Exception:
                        df[name] = df[cand]
                    break


    # ========== Candles ==========
    def _draw_candles(self, df):
        """
        Rysowanie świec z re-użyciem jednego CandlestickItem:
          - dane wejściowe: DataFrame z kolumnami open/high/low/close + timestamp,
          - wewnątrz trzymamy numpy array [x, o, c, l, h, w],
          - przy każdym refreshu tylko podmieniamy dane (update_data), bez wymiany obiektu na scenie.
        """
        # 1) X: timestamp (sekundy UNIX)
        if "timestamp" in df.columns:
            xs = pd.to_numeric(df["timestamp"], errors="coerce")
        elif isinstance(df.index, pd.DatetimeIndex):
            xs = (df.index.view("int64") // 10 ** 9).astype("float64")
            xs = pd.Series(xs, index=df.index)
        else:
            raise ValueError("Missing 'timestamp' column or DatetimeIndex!")

        # 2) OHLC jako float (śmieci -> NaN)
        o = pd.to_numeric(df["open"], errors="coerce")
        h = pd.to_numeric(df["high"], errors="coerce")
        l = pd.to_numeric(df["low"], errors="coerce")
        c = pd.to_numeric(df["close"], errors="coerce")

        # 3) maska: tylko wiersze z poprawnymi danymi
        mask = o.notna() & h.notna() & l.notna() & c.notna() & xs.notna()
        if not mask.any():
            return

        xs_clean = xs[mask].to_numpy(dtype=np.float64)
        opens = o[mask].to_numpy(dtype=np.float64)
        highs = h[mask].to_numpy(dtype=np.float64)
        lows = l[mask].to_numpy(dtype=np.float64)
        closes = c[mask].to_numpy(dtype=np.float64)

        # 4) szerokość świec w jednostkach X (heurystyka)
        if len(xs_clean) > 1:
            diffs = np.diff(xs_clean[np.isfinite(xs_clean)])
            candle_width = max(10.0, float(np.median(diffs)) * 0.5) if diffs.size else 30.0
        else:
            candle_width = 30.0

        widths = np.full_like(xs_clean, candle_width, dtype=np.float64)
        arr = np.column_stack([xs_clean, opens, closes, lows, highs, widths])

        # 5) reużycie CandlestickItem
        if getattr(self, "_candlestick_item", None) is None:
            from .candlestick_item import CandlestickItem as _Candles  # lazy import for safety
            try:
                self._candlestick_item = _Candles(arr)
            except Exception:
                # fallback: jeśli konstruktor się wysypie, nie blokuj całego GUI
                return
            self.candles_plot.addItem(self._candlestick_item)
        else:
            try:
                # szybkie podmienienie danych bez ruszania sceny
                self._candlestick_item.update_data(arr)
            except Exception:
                # w razie problemu – spróbuj odtworzyć obiekt
                try:
                    self.candles_plot.removeItem(self._candlestick_item)
                except Exception:
                    pass
                self._candlestick_item = None
                from .candlestick_item import CandlestickItem as _Candles  # lazy import
                try:
                    self._candlestick_item = _Candles(arr)
                    self.candles_plot.addItem(self._candlestick_item)
                except Exception:
                    self._candlestick_item = None

        # --- zapisz snapshot świec w numpy (do późniejszych lekkich update'ów) ---
        self._candles_xs = xs_clean
        self._candles_open = opens
        self._candles_high = highs
        self._candles_low = lows
        self._candles_close = closes
        self._candles_widths = widths

    # ========== MA overlay (main) ==========
    def _draw_ma(self, df):
        """
        Rysowanie MA na głównym wykresie:
          - reużywamy maksymalnie dwóch PlotCurveItem (fast/slow),
          - zero clear()/removeItem przy każdym refreshu,
          - widoczność sterowana przez self._indicator_visibility["ma1"/"ma2"].
        """
        cfg = (self.display_config or {}).get("MA") if self.display_config else {}
        if not cfg or not cfg.get("enabled", False) or str(cfg.get("display", "")).lower() != "main":
            # jeśli MA nie jest włączone w configu – schowaj istniejące krzywe
            for attr in ("_ma_fast_curve", "_ma_slow_curve"):
                it = getattr(self, attr, None)
                if it is not None:
                    try:
                        it.setVisible(False)
                    except Exception:
                        pass
            return

        shown1 = bool(self._indicator_visibility.get("ma1", True))
        shown2 = bool(self._indicator_visibility.get("ma2", True))

        try:
            import numpy as np, pandas as pd, pyqtgraph as pg  # noqa

            # MUST come from DB/backtest (bucketed) columns
            if not all(c in df.columns for c in ("MA_FAST", "MA_SLOW", "timestamp")):
                return

            xs = pd.to_numeric(df["timestamp"], errors="coerce").values.astype(float)
            y_fast = pd.to_numeric(df["MA_FAST"], errors="coerce").values.astype(float)
            y_slow = pd.to_numeric(df["MA_SLOW"], errors="coerce").values.astype(float)

            # helper do tworzenia/aktualizacji pojedynczej krzywej
            def _upsert_curve(attr_name: str, x_vals, y_vals, color_rgb, visible: bool):
                curve = getattr(self, attr_name, None)
                has_data = y_vals is not None and np.isfinite(y_vals).sum() > 0 and x_vals is not None

                if not visible or not has_data:
                    if curve is not None:
                        try:
                            curve.setVisible(False)
                        except Exception:
                            pass
                    return

                if curve is None:
                    pen = pg.mkPen(color_rgb, width=1.0)
                    curve = pg.PlotCurveItem(x_vals, y_vals, pen=pen)
                    # otaguj, żeby łatwiej filtrować przy debugowaniu
                    try:
                        curve.setData(0, attr_name.upper())
                    except Exception:
                        pass
                    try:
                        tune_curve_fast(curve)
                    except Exception:
                        pass
                    self.candles_plot.addItem(curve)
                    setattr(self, attr_name, curve)
                    # legacy: trzymaj też w _ma_items
                    try:
                        self._ma_items.append(curve)
                    except Exception:
                        self._ma_items = [curve]
                else:
                    try:
                        curve.setData(x_vals, y_vals)
                    except Exception:
                        # w razie problemów spróbuj odtworzyć
                        try:
                            self.candles_plot.removeItem(curve)
                        except Exception:
                            pass
                        pen = pg.mkPen(color_rgb, width=1.0)
                        curve = pg.PlotCurveItem(x_vals, y_vals, pen=pen)
                        try:
                            tune_curve_fast(curve)
                        except Exception:
                            pass
                        self.candles_plot.addItem(curve)
                        setattr(self, attr_name, curve)
                try:
                    curve.setVisible(True)
                except Exception:
                    pass

            # FAST / SLOW MA
            _upsert_curve("_ma_fast_curve", xs, y_fast, (255, 0, 255), shown1)   # magenta
            _upsert_curve("_ma_slow_curve", xs, y_slow, (10, 4, 191), shown2)    # navy

        except Exception as e:
            logging.debug(f"[PlotWidget._draw_ma] MA draw error: {e}")

    def _draw_bollinger(self, df):
        """
        Bollinger Bands:
          - trzy stałe krzywe (upper/middle/lower),
          - brak ciągłego removeItem/addItem,
          - respektuje self._indicator_visibility["bb"] + display_config["BB"].
        """
        cfg = (self.display_config or {}).get("BB") if self.display_config else {}
        if not cfg or not cfg.get("enabled", False) or str(cfg.get("display", "")).lower() != "main":
            # jeśli BB wyłączone w configu – schowaj istniejące
            self._clear_bb_items()
            return

        if not self._indicator_visibility.get("bb", True):
            self._clear_bb_items()
            return

        try:
            import numpy as np, pandas as pd, pyqtgraph as pg  # noqa

            if not all(c in df.columns for c in ("BB_UPPER", "BB_MIDDLE", "BB_LOWER", "timestamp")):
                return

            x = pd.to_numeric(df["timestamp"], errors="coerce").values.astype(float)
            upper = pd.to_numeric(df["BB_UPPER"], errors="coerce").values.astype(float)
            middle = pd.to_numeric(df["BB_MIDDLE"], errors="coerce").values.astype(float)
            lower = pd.to_numeric(df["BB_LOWER"], errors="coerce").values.astype(float)

            # dla tooltipów/crosshaira trzymajmy to też w last_df
            try:
                self.last_df["BB_MIDDLE"] = middle
                self.last_df["BB_UPPER"] = upper
                self.last_df["BB_LOWER"] = lower
            except Exception:
                pass

            def _upsert(attr_name: str, y_vals, color_rgb, tag: str):
                curve = getattr(self, attr_name, None)
                has_data = y_vals is not None and np.isfinite(y_vals).sum() > 0

                if not has_data:
                    if curve is not None:
                        try:
                            curve.setVisible(False)
                        except Exception:
                            pass
                    return

                if curve is None:
                    pen = pg.mkPen(color_rgb, width=1.0)
                    curve = pg.PlotCurveItem(x, y_vals, pen=pen)
                    try:
                        curve.setData(0, tag)
                    except Exception:
                        pass
                    try:
                        tune_curve_fast(curve)
                    except Exception:
                        pass
                    self.candles_plot.addItem(curve)
                    setattr(self, attr_name, curve)
                    # legacy: trzymajmy w _bb_items
                    try:
                        self._bb_items.append(curve)
                    except Exception:
                        self._bb_items = [curve]
                else:
                    try:
                        curve.setData(x, y_vals)
                    except Exception:
                        try:
                            self.candles_plot.removeItem(curve)
                        except Exception:
                            pass
                        pen = pg.mkPen(color_rgb, width=1.0)
                        curve = pg.PlotCurveItem(x, y_vals, pen=pen)
                        try:
                            tune_curve_fast(curve)
                        except Exception:
                            pass
                        self.candles_plot.addItem(curve)
                        setattr(self, attr_name, curve)

                try:
                    curve.setVisible(True)
                except Exception:
                    pass

            # upper / middle / lower
            _upsert("_bb_upper_curve", upper, (180, 180, 255), "BB_DB_UPPER")
            _upsert("_bb_middle_curve", middle, (130, 130, 130), "BB_DB_MIDDLE")
            _upsert("_bb_lower_curve", lower, (180, 180, 180), "BB_DB_LOWER")

        except Exception as e:
            logging.debug(f"[PlotWidget._draw_bollinger] BB draw error: {e}")

    def _clear_bb_items(self):
        """
        Schowaj/usuń krzywe BB. Używane gdy:
          - DF pusty,
          - BB wyłączone w configu,
          - user odznaczył checkbox 'BB'.
        """
        for attr in ("_bb_upper_curve", "_bb_middle_curve", "_bb_lower_curve"):
            curve = getattr(self, attr, None)
            if curve is None:
                continue
            try:
                self.candles_plot.removeItem(curve)
            except Exception:
                pass
            setattr(self, attr, None)

        # legacy lista – czyścimy dla porządku
        try:
            for it in list(self._bb_items):
                try:
                    self.candles_plot.removeItem(it)
                except Exception:
                    pass
        except Exception:
            pass
        self._bb_items = []
    # ========== Risk levels (TP/SL/TS) ==========
    def _draw_risk_lines_for_position(self, pos, df):
        rp = {"tp_long": 1.03, "sl_long": 0.98, "tp_short": 0.97, "sl_short": 1.02,
              "trail_long": 0.985, "trail_short": 1.015}
        strat = getattr(self.engine, "strategy", None) if getattr(self, "engine", None) else None
        if strat and hasattr(strat, "get_risk_params"):
            try:
                got = strat.get_risk_params() or {}
                rp.update(got)
            except Exception:
                pass

        try:
            side = str(pos.get("side", "")).lower()
        except Exception:
            side = ""
        try:
            entry_price = float(pos.get("entry_price"))
        except Exception:
            return
        if side not in ("long", "short") or not np.isfinite(entry_price):
            return

        def _f(v):
            try:
                vv = float(v)
                return vv if np.isfinite(vv) else None
            except Exception:
                return None

        tp = _f(pos.get("tp_level"))
        sl = _f(pos.get("sl_level"))
        if tp is None:
            tp = entry_price * (rp["tp_long"] if side == "long" else rp["tp_short"])
        if sl is None:
            sl = entry_price * (rp["sl_long"] if side == "long" else rp["sl_short"])
        ts = _f(pos.get("trailing_stop"))

        self._risk_lines["TP"] = self._add_hline(tp, key="TP", color=(90, 214, 90))
        self._risk_lines["SL"] = self._add_hline(sl, key="SL", color=(255, 92, 92))
        if ts is not None:
            self._risk_lines["TS"] = self._add_hline(ts, key="TS", color=(255, 224, 102))

        self._add_price_label(tp, key="TP", color=(90, 214, 90))
        self._add_price_label(sl, key="SL", color=(255, 92, 92))
        if ts is not None:
            self._add_price_label(ts, key="TS", color=(255, 224, 102))

    
    def _draw_risk_levels(self, df):
        """Rysuje poziomy risk-managementu (TS / TP / SL) na podstawie kolumn w DF.

        Korzystamy wyłącznie z kolumn TS, TP, SL (ew. *_ind jako fallback do mapowania),
        a NaN w kolumnach urywają linię (connect='finite').
        """
        if df is None or getattr(df, "empty", True):
            return

        # usuń stare linie z wykresu
        try:
            for item in getattr(self, "_risk_lines", {}).values():
                try:
                    self.candles_plot.removeItem(item)
                except Exception:
                    pass
        except Exception:
            pass
        self._risk_lines = {}

        # legacy: jeśli mamy tylko *_ind, skopiuj do TS/TP/SL
        try:
            import pandas as pd
            for base in ("TS", "TP", "SL"):
                if base not in df.columns:
                    ind_col = f"{base}_ind"
                    if ind_col in df.columns:
                        try:
                            df[base] = pd.to_numeric(df[ind_col], errors="coerce")
                        except Exception:
                            df[base] = df[ind_col]
        except Exception:
            pass

        # rysowanie TS / TP / SL
        try:
            import pandas as pd
            xs = pd.to_numeric(df.get("timestamp"), errors="coerce").to_numpy(dtype=float)
            if xs.size == 0 or not np.isfinite(xs).any():
                return

            for name, color in (
                ("TS", (255, 224, 102)),
                ("TP", (90, 214, 90)),
                ("SL", (255, 92, 92)),
            ):
                if name not in df.columns:
                    continue

                y = pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=float)
                # jeśli w ogóle nie ma sensownych wartości – pomijamy
                if not np.isfinite(y).any():
                    continue

                is_ts = name.upper().startswith("TS")
                width = ORDER_LINE_WIDTH * (TS_LINE_MULTIPLIER if is_ts else 1.0)

                item = pg.PlotCurveItem(
                    xs,
                    y,  # zawiera NaN tam, gdzie brak poziomu
                    pen=pg.mkPen(color, width=width, style=Qt.DashLine),
                    connect="finite",
                )
                self.candles_plot.addItem(item)
                self._risk_lines[name] = item

        except Exception:
            return

    def _is_risk_key(self, key) -> bool:
        k = str(key).strip().lower() if key is not None else ""
        return k in {
            "tp", "sl", "ts", "ts_benchmark", "benchmark",
            "risk_tp", "risk_sl", "risk_ts", "risk"
        }

    def _add_hline(self, y, key, color):
        if not np.isfinite(y):
            return None
        if self._is_risk_key(key) and not getattr(self, "_show_global_tp_sl", False):
            return None
        line = pg.InfiniteLine(pos=float(y), angle=0,
                               pen=pg.mkPen(color, width=1.2, style=Qt.DashLine))
        try:
            self.candles_plot.addItem(line, ignoreBounds=True)
        except Exception:
            pass
        return line

    def _add_price_label(self, y, key, color):
        try:
            from pyqtgraph import TextItem
            if not np.isfinite(y):
                return
            if self._is_risk_key(key) and not getattr(self, "_show_global_tp_sl", False):
                old = self._risk_labels.get(key)
                if old is not None:
                    try:
                        self.candles_plot.removeItem(old)
                    except Exception:
                        pass
                    self._risk_labels.pop(key, None)
                return

            label = TextItem(anchor=(1, 0.5), color=color, fill=pg.mkBrush(0, 0, 0, 150))
            label.setText(_fmt_price(y))

            if self.last_df is not None and 'timestamp' in self.last_df.columns and len(self.last_df):
                xs = pd.to_numeric(self.last_df['timestamp'], errors='coerce')
                x = float(xs.iloc[-1]) if xs.size else 0.0
            else:
                x = 0.0

            label.setPos(x, float(y))
            self.candles_plot.addItem(label, ignoreBounds=True)

            old = self._risk_labels.get(key)
            if old is not None:
                try:
                    self.candles_plot.removeItem(old)
                except Exception:
                    pass
            self._risk_labels[key] = label
        except Exception:
            pass

    def _clear_risk_lines(self):
        for _, line in list(self._risk_lines.items()):
            try:
                self.candles_plot.removeItem(line)
            except Exception:
                pass
        self._risk_lines.clear()
        for _, lbl in list(self._risk_labels.items()):
            try:
                self.candles_plot.removeItem(lbl)
            except Exception:
                pass
        self._risk_labels.clear()

    # ========== Subcharts from display_config ==========
    def _draw_subcharts(self, df: pd.DataFrame):
        import logging
        import numpy as np
        import pandas as pd
        import pyqtgraph as pg

        # clear subplots
        for sp in self.sub_indicator_plots:
            try:
                sp.clear()
            except Exception:
                pass
            self._reinstall_crosshair_on_plot(sp)

        if df is None or df.empty:
            for i, sp in enumerate(self.sub_indicator_plots, start=1):
                try:
                    sp.setTitle(f"Sub {i}")
                except Exception:
                    pass
            return

        # initial active names based on UI selection
        slot_names = {
            1: self._sub_slot_active.get(1),
            2: self._sub_slot_active.get(2),
            3: self._sub_slot_active.get(3),
        }
        slot_zero = {1: False, 2: False, 3: False}
        slot_colors = {1: None, 2: None, 3: None}

        cfg = (self.display_config or {}) if isinstance(self.display_config, dict) else {}

        # 1) config dla już wybranych slotów
        for s in (1, 2, 3):
            sel = slot_names[s]
            if not sel:
                continue
            comp = cfg.get(sel, {}) if isinstance(cfg, dict) else {}
            slot_zero[s] = bool(comp.get("is_zero_always_visible", False))
            clr = comp.get("color")
            # lekki default dla RSI
            slot_colors[s] = clr if isinstance(clr, (str, tuple)) else ("#7db3ff" if str(sel).upper() == "RSI" else None)

        # 2) fallback – jeśli slot jest pusty, spróbuj z display_config.display == "sub1/2/3"
        for name, comp in cfg.items():
            if not isinstance(comp, dict) or not comp.get("enabled", False):
                continue
            disp = str(comp.get("display", "")).lower().strip()
            if disp in ("sub1", "sub2", "sub3"):
                slot = {"sub1": 1, "sub2": 2, "sub3": 3}[disp]
                if slot_names[slot] is None:
                    slot_names[slot] = name
                    slot_zero[slot] = bool(comp.get("is_zero_always_visible", False))
                    clr = comp.get("color")
                    slot_colors[slot] = clr if isinstance(clr, (str, tuple)) else None

        # 3) fallback z indicator_1..3_name jeśli nadal pusto
        try:
            for i in (1, 2, 3):
                if slot_names[i] is None:
                    ncol = f"indicator_{i}_name"
                    if ncol in df.columns and len(df):
                        cand = str(df[ncol].iloc[0])
                        if cand:
                            slot_names[i] = cand
                            comp = cfg.get(cand, {}) if isinstance(cfg, dict) else {}
                            slot_zero[i] = bool(comp.get("is_zero_always_visible", False))
                            clr = comp.get("color")
                            slot_colors[i] = clr if isinstance(clr, (str, tuple)) else None
        except Exception as e:
            logging.warning(f"[SUBCHART] fallback from indicator_1..3 failed: {e}")

        # log – co finalnie rysujemy
        logging.warning(f"[SUBCHART] slot_names={slot_names}, slot_zero={slot_zero}")

        # tytuły subchartów
        for i, sp in enumerate(self.sub_indicator_plots, start=1):
            title = slot_names[i] or f"Sub {i}"
            try:
                sp.setTitle(title)
            except Exception:
                pass

        # oś X: timestamp lub indeks
        if "timestamp" in df.columns:
            x = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=float)
        else:
            x = np.arange(len(df), dtype=float)

        # helper do oszacowania jakości danych (NaN / ostatnia wartość)
        def _tail_nan(series, tail=256):
            s = pd.to_numeric(series, errors="coerce") if series is not None else pd.Series(dtype=float)
            t = s.tail(min(tail, len(s)))
            last_bad = (not len(t)) or (not np.isfinite(t.iloc[-1]))
            frac = float(t.isna().mean()) if len(t) else 1.0
            return frac, last_bad

        # draw each selected
        for slot in (1, 2, 3):
            name = slot_names[slot]
            sp = self.sub_indicator_plots[slot - 1]
            if not name:
                continue
            upper_name = str(name).strip().upper()

            # --- VOLUME ---
            if upper_name == "VOLUME":
                try:
                    if "timestamp" in df.columns:
                        x_vol = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=float)
                    else:
                        x_vol = np.arange(len(df), dtype=float)
                except Exception:
                    x_vol = np.arange(len(df), dtype=float)
                try:
                    dx = np.diff(x_vol)
                    w = float(np.nanmedian(dx)) * 0.8 if dx.size else 1.0
                except Exception:
                    w = 1.0

                vol = pd.to_numeric(df.get("volume_quote", pd.Series(index=df.index)), errors="coerce").fillna(0).to_numpy(dtype=float)
                tb = pd.to_numeric(df.get("taker_buy_volume_quote", pd.Series(index=df.index)), errors="coerce").to_numpy(dtype=float)
                is_up = (
                    pd.to_numeric(df.get("close", pd.Series(index=df.index)), errors="coerce")
                    >= pd.to_numeric(df.get("open", pd.Series(index=df.index)), errors="coerce")
                ).fillna(False).to_numpy(dtype=bool)

                try:
                    brushes = [pg.mkBrush(0, 180, 0) if up else pg.mkBrush(200, 0, 0) for up in is_up]
                    bars = pg.BarGraphItem(x=x_vol, height=vol, width=w, brushes=brushes, pens=None)
                    sp.addItem(bars)
                except Exception:
                    pass
                try:
                    if np.isfinite(tb).any():
                        sp.addItem(pg.PlotCurveItem(x_vol, tb, pen=pg.mkPen(width=1.25)))
                except Exception:
                    pass
                self._autoscale_subplot_for_arrays(sp, [vol, tb], pad=0.15)
                continue

            # --- MACD ---
            if upper_name == "MACD":
                have_trio = all(c in df.columns for c in ("MACD", "MACD_SIGNAL", "MACD_HIST"))
                need_fallback = True
                if have_trio:
                    frac, last_bad = _tail_nan(df["MACD_HIST"])
                    need_fallback = (frac > 0.05) or last_bad
                if need_fallback and ta is not None:
                    comp = cfg.get("MACD", {}) if isinstance(cfg, dict) else {}
                    p = comp.get("params", {}) if isinstance(comp, dict) else {}
                    f = int(p.get("fast", 12))
                    s = int(p.get("slow", 26))
                    sig = int(p.get("signal", 9))
                    try:
                        macd_ind = ta.trend.MACD(close=df["close"], window_slow=s, window_fast=f, window_sign=sig)
                        df = df.copy()
                        df["MACD"] = macd_ind.macd()
                        df["MACD_SIGNAL"] = macd_ind.macd_signal()
                        df["MACD_HIST"] = macd_ind.macd_diff()
                        self.last_df["MACD"] = df["MACD"]
                        self.last_df["MACD_SIGNAL"] = df["MACD_SIGNAL"]
                        self.last_df["MACD_HIST"] = df["MACD_HIST"]
                    except Exception:
                        pass
                macd_arrays = self._draw_macd_on_slot(sp, x, df)
                self._autoscale_subplot_for_arrays(sp, macd_arrays, pad=0.15)
                if slot_zero[slot]:
                    try:
                        sp.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
                    except Exception:
                        pass
                continue

            # --- STOCH / STOCH_RSI ---
            if upper_name in ("STOCH", "STOCH_RSI"):
                try:
                    if upper_name == "STOCH":
                        k_col, d_col = "STOCH_K", "STOCH_D"
                        comp = cfg.get("STOCH", {}) if isinstance(cfg, dict) else {}
                    else:
                        k_col, d_col = "STOCHRSI_K", "STOCHRSI_D"
                        comp = cfg.get("STOCH_RSI", {}) if isinstance(cfg, dict) else {}

                    yk = pd.to_numeric(df.get(k_col, pd.Series(index=df.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
                    yd = pd.to_numeric(df.get(d_col, pd.Series(index=df.index, dtype=float)), errors="coerce").to_numpy(dtype=float)

                    col = comp.get("color", {}) if isinstance(comp, dict) else {}
                    k_color = None
                    if isinstance(col, dict):
                        k_color = col.get("k") or col.get("K")
                    elif isinstance(col, str):
                        k_color = col
                    k_pen = pg.mkPen(k_color or "#9b59b6", width=1.25)
                    d_pen = pg.mkPen((128, 128, 128), width=1.25)

                    sp.addItem(pg.PlotCurveItem(x, yk, pen=k_pen))
                    sp.addItem(pg.PlotCurveItem(x, yd, pen=d_pen))

                    conf = comp.get("confirm", {}) if isinstance(comp, dict) else {}
                    lm = conf.get("long_max", None)
                    sm = conf.get("short_min", None)
                    if lm is not None:
                        try:
                            sp.addItem(pg.InfiniteLine(pos=float(lm), angle=0, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
                        except Exception:
                            pass
                    if sm is not None:
                        try:
                            sp.addItem(pg.InfiniteLine(pos=float(sm), angle=0, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
                        except Exception:
                            pass

                    self._autoscale_subplot_for_arrays(sp, [yk, yd], pad=0.15)
                    if slot_zero[slot]:
                        try:
                            sp.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
                        except Exception:
                            pass
                    continue
                except Exception as e:
                    logging.debug(f"STOCH draw error: {e}")
                    # lecimy dalej do generyka

            # --- GENERIC CASE (RSI, ATR, ATR_PCT, FEAR_GREED, PCT_CHANGE itd.) ---
            y = self._get_series_for_indicator_name(df, name)

            # diagnostyka – zobaczmy, co faktycznie wychodzi
            try:
                if y is None:
                    logging.warning(f"[SUBCHART] series {name}: y=None")
                else:
                    fin = np.isfinite(y)
                    n_fin = int(fin.sum())
                    if n_fin:
                        ymin = float(y[fin].min())
                        ymax = float(y[fin].max())
                    else:
                        ymin = ymax = None
                    logging.warning(f"[SUBCHART] series {name}: len={len(y)}, finite={n_fin}, min={ymin}, max={ymax}")
            except Exception:
                pass

            if y is None:
                continue
            y = np.asarray(y, dtype=float)

            # dopasuj długości x/y gdyby się nie zgadzały
            if y.size != x.size:
                n = int(min(len(x), len(y)))
                if n <= 0:
                    continue
                x_use = x[-n:]
                y_use = y[-n:]
            else:
                x_use = x
                y_use = y

            finite = np.isfinite(y_use)
            if not finite.any():
                continue

            pen = pg.mkPen(slot_colors[slot], width=1) if slot_colors[slot] else None
            curve = pg.PlotCurveItem(x_use, y_use, pen=pen) if pen else pg.PlotCurveItem(x_use, y_use)
            tune_curve_fast(curve)
            sp.addItem(curve)
            if slot_zero[slot]:
                try:
                    sp.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
                except Exception:
                    pass
            self._autoscale_subplot_for_arrays(sp, [y_use], pad=0.15)

    def _get_series_for_indicator_name(self, df: pd.DataFrame, name: str):
        """
        Mapuje nazwę wskaźnika (taką jak w slot_names / display_config)
        na odpowiednią kolumnę w df.

        Bazujemy TYLKO na prawdziwych kolumnach wskaźnikowych z bazy, np.:
        RSI, ATR, ATR_PCT, FEAR_GREED, PCT_CHANGE,
        STOCH_D, STOCH_K, STOCHRSI_D, STOCHRSI_K,
        VOL_AVG, VOL_SMA, BUY_VOL_AVG,
        SL_MAX, SL_MIN, TP_MAX, TP_MIN, TP, SL, TS itd.
        """
        import pandas as pd
        import numpy as np
        import logging

        if not name or df is None or df.empty:
            return None

        nm = str(name)
        up = nm.upper().strip()

        # --- aliasy / specjalne przypadki ---

        # VOLUME / VOL – rysujemy na podstawie kolumn wolumenowych
        if up in ("VOL", "VOLUME"):
            for cand in ("volume_quote", "VOL_AVG", "VOL_SMA", "VOLUME"):
                if cand in df.columns:
                    return pd.to_numeric(df[cand], errors="coerce").to_numpy(dtype=float)

        # STOCH_RSI – użyjemy STOCHRSI_K (główna linia) albo STOCHRSI_D
        if up == "STOCH_RSI":
            for cand in ("STOCHRSI_K", "STOCHRSI_D"):
                if cand in df.columns:
                    return pd.to_numeric(df[cand], errors="coerce").to_numpy(dtype=float)

        # STOCH – użyjemy STOCH_K albo STOCH_D
        if up == "STOCH":
            for cand in ("STOCH_K", "STOCH_D"):
                if cand in df.columns:
                    return pd.to_numeric(df[cand], errors="coerce").to_numpy(dtype=float)

        # --- bezpośrednie kolumny o tej samej nazwie (tu wpada cała reszta) ---

        direct_candidates = [
            nm,          # np. "RSI"
            up,          # "RSI"
            nm.lower(),  # "rsi"
            up.lower(),  # "rsi"
        ]
        for cand in direct_candidates:
            if cand in df.columns:
                return pd.to_numeric(df[cand], errors="coerce").to_numpy(dtype=float)

        # --- ewentualne sufiksy typu *_IND, *_X, *_Y ---

        suffix_candidates = [
            f"{up}_IND",
            f"{up}_X",
            f"{up}_Y",
            f"{up.lower()}_ind",
        ]
        for cand in suffix_candidates:
            if cand in df.columns:
                return pd.to_numeric(df[cand], errors="coerce").to_numpy(dtype=float)

        logging.debug(f"[SUBCHART] no series found for indicator name={name}")
        return None

    def _draw_macd_on_slot(self, plot, x, df):
        if plot is None:
            return []
        y_macd = pd.to_numeric(df['MACD'], errors='coerce').to_numpy(dtype=float) if 'MACD' in df.columns else None
        y_sig  = pd.to_numeric(df['MACD_SIGNAL'], errors='coerce').to_numpy(dtype=float) if 'MACD_SIGNAL' in df.columns else None
        y_hist = pd.to_numeric(df['MACD_HIST'], errors='coerce').to_numpy(dtype=float) if 'MACD_HIST' in df.columns else None

        added = False
        try:
            if y_macd is not None and np.isfinite(y_macd).sum()>0:
                c1 = pg.PlotCurveItem(x, y_macd, pen=pg.mkPen((255,107,107), width=1.2))
                tune_curve_fast(c1)
                plot.addItem(c1); added = True
            if y_sig is not None and np.isfinite(y_sig).sum()>0:
                c2 = pg.PlotCurveItem(x, y_sig, pen=pg.mkPen((180,180,180), width=1.0, style=Qt.DashLine))
                tune_curve_fast(c2)
                plot.addItem(c2); added = True
            if y_hist is not None and np.isfinite(y_hist).sum()>0:
                from pyqtgraph import BarGraphItem
                diffs = np.diff(x[np.isfinite(x)]) if np.isfinite(x).sum() > 1 else np.array([60])
                width = float(np.median(diffs)) if diffs.size else 60.0
                bars = BarGraphItem(x=x, height=y_hist, width=width*0.8, brush=pg.mkBrush(150,150,150,120))
                plot.addItem(bars); added = True
        except Exception:
            pass

        # Ensure canonical columns exist in last_df
        if 'MACD' not in self.last_df.columns:
            for cand in ('MACD_ind','MACD_x','MACD_y'):
                if cand in df.columns:
                    self.last_df['MACD'] = pd.to_numeric(df[cand], errors='coerce'); break
        if 'MACD_SIGNAL' not in self.last_df.columns:
            for cand in ('MACD_SIGNAL_ind','MACD_SIGNAL_x','MACD_SIGNAL_y'):
                if cand in df.columns:
                    self.last_df['MACD_SIGNAL'] = pd.to_numeric(df[cand], errors='coerce'); break
        if 'MACD_HIST' not in self.last_df.columns:
            for cand in ('MACD_HIST_ind','MACD_HIST_x','MACD_HIST_y'):
                if cand in df.columns:
                    self.last_df['MACD_HIST'] = pd.to_numeric(df[cand], errors='coerce'); break

        return [arr for arr in (y_macd, y_sig, y_hist) if arr is not None]

    def _autoscale_subplot_for_arrays(self, plot, y_arrays, pad: float = 0.15):
        """
        Robust autoscale for subplots:
        - akceptuje listy/serie/ndarray (spłaszcza),
        - wycina NaN/inf,
        - radzi sobie z płaskimi seriami (ylo==yhi),
        - nie rozjeżdża się gdy nic sensownego nie ma do narysowania.
        """
        try:
            if pad is None or not np.isfinite(pad) or pad < 0:
                pad = 0.15

            arrs = []
            for a in (y_arrays or []):
                if a is None:
                    continue
                try:
                    # pandas Series/DataFrame -> ndarray
                    if hasattr(a, "to_numpy"):
                        a = a.to_numpy()
                except Exception:
                    pass

                aa = np.asarray(a, dtype=float)
                if aa.ndim > 1:
                    aa = aa.ravel()

                # wytnij śmieci
                aa = aa[np.isfinite(aa)]
                if aa.size:
                    arrs.append(aa)

            if not arrs:
                return  # nic do skalowania

            ylo = min(a.min() for a in arrs)
            yhi = max(a.max() for a in arrs)

            if not (np.isfinite(ylo) and np.isfinite(yhi)):
                return

            if yhi == ylo:
                # płaska seria: daj sensowny bufor zależny od wartości
                base = abs(yhi) if yhi != 0 else 1.0
                ylo -= 0.5 * base
                yhi += 0.5 * base

            rng = yhi - ylo
            ylo -= pad * rng
            yhi += pad * rng

            vb = plot.getViewBox() if hasattr(plot, "getViewBox") else getattr(plot, "vb", None)
            if vb is None:
                return

            vb.disableAutoRange()
            vb.setYRange(ylo, yhi, padding=0.0)
        except Exception:
            # niczego nie wysypuj na GUI — brak autoscale to mniejsze zło
            return

    # Alias requested by some callers in older code
    def _autoscale_subcharts_for_window(self, pad=0.15):
        return self._autoscale_subcharts_in_window(pad=pad)

    # ========== Autoscale subcharts for current X window ==========
    def _autoscale_subcharts_in_window(self, _x_min: float = None, _x_max: float = None, pad: float = 0.15):
        def _apply(plot_like):
            plot_item = getattr(plot_like, "getPlotItem", None)
            plot_item = plot_item() if callable(plot_item) else plot_like
            if plot_item is None:
                return

            vb = plot_item.getViewBox()
            (x_min_vis, x_max_vis), _ = vb.viewRange()
            x_min_vis = float(x_min_vis)
            x_max_vis = float(x_max_vis)

            ys_chunks = []

            def _temporarily_allow_bounds(it):
                try:
                    if getattr(it, 'opts', None) is not None and 'ignoreBounds' in it.opts:
                        it.opts['ignoreBounds'] = False
                except Exception:
                    pass

            # Lines / curves
            for it in list(plot_item.listDataItems() or []):
                try:
                    _temporarily_allow_bounds(it)
                    x, y = it.getData()
                    if y is None:
                        continue
                    y = np.asarray(y, dtype=float)
                    if x is None:
                        x = np.arange(y.size, dtype=float)
                    else:
                        x = np.asarray(x, dtype=float)
                    if x.size == 0 or y.size == 0:
                        continue
                    m = (x >= x_min_vis) & (x <= x_max_vis)
                    if not np.any(m):
                        continue
                    ym = y[m]
                    ym = ym[np.isfinite(ym)]
                    if ym.size:
                        ys_chunks.append(ym)
                except Exception:
                    continue

            # Bars (MACD hist etc.)
            for it in list(getattr(plot_item, "items", []) or []):
                try:
                    if isinstance(it, pg.BarGraphItem):
                        _temporarily_allow_bounds(it)
                        xo = it.opts.get("x")
                        ho = it.opts.get("height")
                        y0 = float(it.opts.get("y0", 0.0))
                        if xo is None or ho is None:
                            continue
                        x = np.asarray(xo, dtype=float)
                        h = np.asarray(ho, dtype=float)
                        if x.size == 0 or h.size == 0:
                            continue
                        m = (x >= x_min_vis) & (x <= x_max_vis)
                        if not np.any(m):
                            continue
                        upper = y0 + h[m]
                        lower = np.full_like(upper, y0)
                        upper = upper[np.isfinite(upper)]
                        lower = lower[np.isfinite(lower)]
                        if upper.size:
                            ys_chunks.append(upper)
                        if lower.size:
                            ys_chunks.append(lower)
                except Exception:
                    continue

            if not ys_chunks:
                return

            ycat = np.concatenate(ys_chunks)
            if ycat.size == 0:
                return

            ylo = float(np.nanmin(ycat))
            yhi = float(np.nanmax(ycat))
            if not np.isfinite(ylo) or not np.isfinite(yhi):
                return
            if yhi == ylo:
                ylo -= 1.0
                yhi += 1.0

            rng = yhi - ylo
            ylo -= pad * rng
            yhi += pad * rng

            try:
                vb.enableAutoRange(axis=vb.YAxis, enable=False)
            except Exception:
                pass
            vb.setYRange(ylo, yhi, padding=0.0)

        _apply(self.equity_plot)
        for idx in (0, 1, 2):
            _apply(self.sub_indicator_plots[idx])

    # ========== Trade markers ==========
    def _draw_trade_markers(self, df, trades):
        for trade in trades:
            if not trade:
                continue
            try:
                if 'entry_timestamp' in trade and trade.get('side') is not None:
                    tx = pd.to_datetime(trade['entry_timestamp'])
                    if hasattr(tx, "timestamp"):
                        tx = int(pd.to_datetime(tx).timestamp())
                    price = float(trade.get('entry_price', 0))
                    side = str(trade.get('side', '')).lower()
                    symbol_marker = 't1' if side == 'long' else 't'
                    entry_brush_color = TRADE_ENTRY_LONG_COLOR if side == 'long' else TRADE_ENTRY_SHORT_COLOR
                    entry_pen = pg.mkPen(TRADE_ENTRY_BORDER_COLOR, width=float(TRADE_ENTRY_BORDER_WIDTH), cosmetic=True)
                    scatter = pg.ScatterPlotItem([tx], [price], symbol=symbol_marker,
                                                 size=float(TRADE_ENTRY_MARKER_SIZE),
                                                 brush=pg.mkBrush(pg.mkColor(entry_brush_color)), pen=entry_pen)
                    self.candles_plot.addItem(scatter)
                    self._trade_marker_items.append(scatter)

                if 'exit_timestamp' in trade and trade.get('side') is not None:
                    tx = pd.to_datetime(trade['exit_timestamp'])
                    if hasattr(tx, "timestamp"):
                        tx = int(pd.to_datetime(tx).timestamp())
                    price = float(trade.get('exit_price', 0))
                    side = str(trade.get('side', '')).lower()
                    symbol_marker = 't' if side == 'long' else 't1'
                    exit_pen = pg.mkPen(TRADE_EXIT_BORDER_COLOR, width=float(TRADE_EXIT_BORDER_WIDTH), cosmetic=True)
                    scatter = pg.ScatterPlotItem([tx], [price], symbol=symbol_marker,
                                                 size=float(TRADE_EXIT_MARKER_SIZE),
                                                 brush=pg.mkBrush(pg.mkColor(TRADE_EXIT_BRUSH_COLOR)), pen=exit_pen)
                    self.candles_plot.addItem(scatter)
                    self._trade_marker_items.append(scatter)
            except Exception as e:
                logging.deb    # ========== Equity curve ==========
    def _draw_equity_curve(self, df: pd.DataFrame, trades: list):
        """
        Rysowanie krzywej equity:
          - reużywamy jeden PlotCurveItem (self._equity_curve_item),
          - NIE robimy equity_plot.clear() przy każdym odświeżeniu,
          - tytuł i dane są tylko podmieniane (setData).
        """
        self._equity_x = None
        self._equity_y = None

        if df is None or getattr(df, "empty", True):
            # nic do rysowania – ewentualnie ukryj istniejącą krzywą
            curve = getattr(self, "_equity_curve_item", None)
            if curve is not None:
                try:
                    curve.setVisible(False)
                except Exception:
                    pass
            return

        # oś czasu – preferujemy timestamp w sekundach
        if 'timestamp' in df.columns:
            times = pd.to_datetime(df['timestamp'], unit='s', utc=True, errors='coerce')
        elif 'close_time' in df.columns:
            times = pd.to_datetime(df['close_time'], utc=True, errors='coerce')
        else:
            try:
                times = pd.to_datetime(df.index, utc=True, errors='coerce')
            except Exception:
                return

        times = times.dropna()
        if times.empty:
            return

        use_all = self.eq_all_btn.isChecked() if hasattr(self, "eq_all_btn") else False
        src_trades: list = []
        if use_all and getattr(self, "engine", None) is not None and hasattr(self.engine, "get_all_trades"):
            try:
                src_trades = self.engine.get_all_trades() or []
            except Exception:
                src_trades = []
        else:
            src_trades = trades or []

        x = (times.astype('int64') // 10**9).to_numpy()

        # upewnij się, że mamy jeden stały PlotCurveItem na equity
        curve = getattr(self, "_equity_curve_item", None)
        if curve is None:
            try:
                curve = pg.PlotCurveItem()
                tune_curve_fast(curve)
                self.equity_plot.addItem(curve)
                self._equity_curve_item = curve
            except Exception:
                self._equity_curve_item = None
                return

        if not src_trades:
            y = np.full(len(times), float(STARTING_BALANCE), dtype=float)
            try:
                curve.setData(x, y)
                curve.setVisible(True)
                self.equity_plot.setTitle("Equity curve")
            except Exception:
                pass
            self._equity_x = x
            self._equity_y = y
            return

        rows = []
        for t in src_trades:
            et = t.get('exit_timestamp') or t.get('close_time') or t.get('timestamp')
            try:
                et = pd.to_datetime(et, utc=True, errors='coerce')
            except Exception:
                et = None
            if et is None or pd.isna(et):
                continue
            pnl = float(t.get('pnl', 0.0))
            rows.append((et, pnl))

        if not rows:
            y = np.full(len(times), float(STARTING_BALANCE), dtype=float)
            try:
                curve.setData(x, y)
                curve.setVisible(True)
                self.equity_plot.setTitle("Equity curve")
            except Exception:
                pass
            self._equity_x = x
            self._equity_y = y
            return

        pnl_df = pd.DataFrame(rows, columns=['t', 'pnl']).groupby('t').sum().sort_index()
        cum = pnl_df['pnl'].cumsum()
        cum_on_candles = cum.reindex(times, method='ffill').fillna(0.0)
        equity = float(STARTING_BALANCE) + cum_on_candles.to_numpy()

        try:
            curve.setData(x, equity)
            curve.setVisible(True)
            self.equity_plot.setTitle("Equity curve (ALL)" if use_all else "Equity curve (SELECTED)")
        except Exception:
            pass

        self._equity_x = x
        self._equity_y = equity

    def _cleanup_stray_crosshairs(self, plot):
        try:
            v_keep = self._vLines.get(plot)
            h_keep = self._hLines.get(plot)
            for it in list(plot.items()):
                if getattr(it, "_is_crosshair", False) and it not in (v_keep, h_keep):
                    try:
                        plot.removeItem(it)
                    except Exception:
                        pass
        except Exception:
            pass


    def _reinstall_crosshair_on_plot(self, plot):
        if plot is None:
            return
        self._cleanup_stray_crosshairs(plot)

        v = self._vLines.get(plot); h = self._hLines.get(plot)
        try:
            solid_pen = pg.mkPen(220, 220, 220, width=1, cosmetic=True, style=Qt.SolidLine)
            if v is not None: v.setPen(solid_pen); v.setZValue(1_000_000)
            if h is not None: h.setPen(solid_pen); h.setZValue(1_000_000)
        except Exception:
            pass

        if v is not None:
            try:
                if v not in plot.items(): plot.addItem(v, ignoreBounds=True)
                v.show()
            except Exception: pass
        if h is not None:
            try:
                if h not in plot.items(): plot.addItem(h, ignoreBounds=True)
                h.hide()
            except Exception: pass

    def _reinstall_crosshair_all(self):
        for p in ([self.candles_plot, self.equity_plot] + self.sub_indicator_plots):
            self._reinstall_crosshair_on_plot(p)

    def _on_plot_mouse_moved(self, evt, plot):
        if self.last_df is None or getattr(self.last_df, "empty", True):
            self.info_label.hide(); return
        pos = evt[0]
        if not plot.sceneBoundingRect().contains(pos):
            try:
                if not any(p.sceneBoundingRect().contains(pos) for p in self._crosshair_plots):
                    self.info_label.hide()
                    for _p in self._crosshair_plots:
                        try:
                            self._vLines[_p].hide()
                            self._hLines[_p].hide()
                        except Exception:
                            pass
            except Exception:
                pass
            return

        self._cleanup_stray_crosshairs(plot)

        vb = plot.getPlotItem().vb
        mp = vb.mapSceneToView(pos)
        x = mp.x(); y = mp.y()

        if not hasattr(self, "_last_crosshair_x"): self._last_crosshair_x = None
        if self._last_crosshair_x is not None:
            try:
                cur_scene_x = vb.mapViewToScene(pg.Point(x, 0)).x()
                last_scene_x = vb.mapViewToScene(pg.Point(self._last_crosshair_x, 0)).x()
                if abs(cur_scene_x - last_scene_x) < 0.5: return
            except Exception:
                if abs(x - self._last_crosshair_x) < 1e-6: return
        self._last_crosshair_x = x

        for p, v in self._vLines.items():
            try: v.setPos(x); v.show()
            except Exception: pass

        for p, h in self._hLines.items():
            if p is plot:
                try: h.setPos(y); h.show()
                except Exception: pass
            else:
                h.hide()

        try:
            xs = pd.to_numeric(self.last_df['timestamp'], errors='coerce').values.astype(float)
        except Exception:
            self.info_label.hide(); return

        if xs.size == 0 or not np.isfinite(x):
            self.info_label.hide(); return

        idx = int(np.searchsorted(xs, x))
        if idx < 0: idx = 0
        if idx >= len(xs): idx = len(xs) - 1

        row = self.last_df.iloc[idx]

        equity_val = None
        try:
            if self._equity_x is not None and self._equity_y is not None:
                ex = self._equity_x; ey = self._equity_y
                j = int(np.searchsorted(ex, xs[idx]))
                if j <= 0: equity_val = float(ey[0])
                elif j >= len(ex): equity_val = float(ey[-1])
                else: equity_val = float(ey[j] if abs(ex[j] - xs[idx]) < abs(xs[idx] - ex[j - 1]) else ey[j - 1])
        except Exception:
            equity_val = None

        text = self._build_unified_tooltip(row, equity_val=equity_val)

        self.info_label.setText(text)
        self.info_label.adjustSize()
        try:
            scene_pt = vb.mapViewToScene(pg.Point(x, y))
            local_pt = plot.mapTo(self, scene_pt.toPoint())
            self.info_label.move(local_pt.x() + 20, local_pt.y() - self.info_label.height() // 2)
            self.info_label.show()
        except Exception:
            pass

    def _build_unified_tooltip(self, row, equity_val: float = None) -> str:
        try:
            ts = pd.to_datetime(row['timestamp'], unit='s', utc=True)
            time_label = ts.tz_convert(WARSZAWA_TZ).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            time_label = str(row.get('close_time', ''))

        text = (
            f"<b>{time_label}</b>"
            f"<br>O: {_fmt_price(row.get('open', np.nan))}"
            f" H: {_fmt_price(row.get('high', np.nan))}"
            f" L: {_fmt_price(row.get('low', np.nan))}"
            f" C: {_fmt_price(row.get('close', np.nan))}"
        )

        already = set()
        MACD_GROUP = {"MACD", "MACD SIGNAL", "MACD HIST"}

        # RSI (first after OHLC)
        try:
            if "RSI" in self.last_df.columns and pd.notna(row.get("RSI", np.nan)):
                if "RSI" not in already:
                    text += f"<br>RSI: {_fmt_price(row['RSI'])}"
                    already.add("RSI")
        except Exception:
            pass

        # indicator_1..3
        for i in range(1, 4):
            ind_col = f"indicator_{i}"
            ind_name_col = f"{ind_col}_name"
            val = row.get(ind_col, np.nan)
            if ind_col in self.last_df.columns and pd.notna(val):
                if ind_name_col in self.last_df.columns and pd.notna(row.get(ind_name_col, None)):
                    ind_name = str(row[ind_name_col])
                elif self.indicator_names and len(self.indicator_names) >= i:
                    ind_name = self.indicator_names[i - 1]
                else:
                    ind_name = ind_col
                iname_u = ind_name.strip().upper()
                if iname_u in MACD_GROUP:
                    continue
                if iname_u not in already:
                    already.add(iname_u)
                    text += f"<br>{ind_name}: {_fmt_price(val)}"

        # MA
        for col, label in (("MA_FAST", "MA fast"), ("MA_SLOW", "MA slow")):
            v = row.get(col, np.nan)
            if col in self.last_df.columns and pd.notna(v):
                k = label.upper()
                if k not in already:
                    text += f"<br>{label}: {_fmt_price(v)}"
                    already.add(k)

        # BB
        for col, label in (("BB_MIDDLE", "BB mid"), ("BB_UPPER", "BB upper"), ("BB_LOWER", "BB lower")):
            v = row.get(col, np.nan)
            if col in self.last_df.columns and pd.notna(v):
                k = label.upper()
                if k not in already:
                    text += f"<br>{label}: {_fmt_price(v)}"
                    already.add(k)

        # ATR
        if "ATR" in self.last_df.columns and pd.notna(row.get("ATR", np.nan)):
            if "ATR" not in already:
                text += f"<br>ATR: {_fmt_price(row['ATR'])}"
                already.add("ATR")

        # MACD trio in one row
        def _f(x):
            try:
                x = float(x)
                return x if np.isfinite(x) else np.nan
            except Exception:
                return np.nan

        macd = _f(row.get("MACD", np.nan))
        macd_sig = _f(row.get("MACD_SIGNAL", np.nan))
        macd_hist = _f(row.get("MACD_HIST", np.nan))

        if any(np.isfinite(v) for v in (macd, macd_sig, macd_hist)):
            trio = ", ".join((_fmt_price(macd), _fmt_price(macd_sig), _fmt_price(macd_hist)))
            text += f"<br>MACD: [{trio}]"
            already.update(MACD_GROUP)

        # TrailingStop / TP / SL
        ts_val = None
        ts_armed = False
        for c in ("TS_ind", "TS"):
            if c in self.last_df.columns and pd.notna(row.get(c, np.nan)):
                try:
                    tsf = float(row[c])
                    if np.isfinite(tsf):
                        ts_val = tsf
                        break
                except Exception:
                    pass

        if ts_val is None and getattr(self, "engine", None) is not None:
            try:
                sym = row.get('symbol') or (
                    self.last_df['symbol'].iloc[-1] if 'symbol' in self.last_df.columns else None)
                if sym and hasattr(self.engine, "contexts"):
                    ctx = self.engine.contexts.get(sym)
                    pos = getattr(ctx, "current_position", None) if ctx else None
                    if pos:
                        tsv = pos.get("trailing_stop", None)
                        if tsv is not None:
                            tsv = float(tsv)
                            if np.isfinite(tsv):
                                ts_val = tsv
                        ts_armed = bool(pos.get("ts_armed", False) or pos.get("ts_active", False))
            except Exception:
                pass

        tp_val = None
        sl_val = None
        for c in ("TP_ind", "TP"):
            if c in self.last_df.columns and pd.notna(row.get(c, np.nan)):
                try:
                    tp_val = float(row[c])
                    break
                except Exception:
                    pass
        for c in ("SL_ind", "SL"):
            if c in self.last_df.columns and pd.notna(row.get(c, np.nan)):
                try:
                    sl_val = float(row[c])
                    break
                except Exception:
                    pass

        if ts_val is not None:
            sfx = " (armed)" if ts_armed else ""
            text += f"<br>TS{sfx}: {_fmt_price(ts_val)}"
        if tp_val is not None or sl_val is not None:
            if tp_val is not None:
                text += f"<br>TP: {_fmt_price(tp_val)}"
            if sl_val is not None:
                text += f"  SL: {_fmt_price(sl_val)}"

        try:
            if equity_val is not None and np.isfinite(equity_val):
                text += f"<br>Equity: <b>{_fmt_price(float(equity_val))}</b>"
        except Exception:
            pass

        return text

    # ========== Zoom controls ==========
    def get_all_plots(self):
        return [self.candles_plot, self.equity_plot] + self.sub_indicator_plots

    def autoscale(self):
        self.zoom_mode = "auto"
        self._deactivate_rect_zoom()
        self._remove_region()

        # Preferuj pełne dane raw_df (bazowy interwał + wskaźniki); fallback na last_df
        df = None
        try:
            if self.raw_df is not None and not getattr(self.raw_df, "empty", True):
                df = self.raw_df
            elif self.last_df is not None and not getattr(self.last_df, "empty", True):
                df = self.last_df
        except Exception:
            df = self.last_df

        if df is None or getattr(df, "empty", True) or "timestamp" not in df.columns:
            return

        xs = pd.to_numeric(df["timestamp"], errors="coerce").values.astype(float)
        lows = pd.to_numeric(df['low'], errors='coerce').values.astype(float)
        highs = pd.to_numeric(df['high'], errors='coerce').values.astype(float)

        x_min, x_max = np.nanmin(xs), np.nanmax(xs)
        y_min, y_max = np.nanmin(lows), np.nanmax(highs)
        if x_min == x_max: x_min -= 60; x_max += 60
        if y_min == y_max: y_min -= 1; y_max += 1
        y_range = y_max - y_min
        y_min -= 0.05 * y_range; y_max += 0.05 * y_range

        vb = self.candles_plot.getViewBox()
        vb.disableAutoRange()
        vb.setXRange(x_min, x_max, padding=0.01)
        vb.setYRange(y_min, y_max, padding=0)
        vb.setMouseEnabled(x=True, y=True)
        vb.setMouseMode(pg.ViewBox.PanMode)

        for plot in [self.equity_plot] + self.sub_indicator_plots:
            vb = plot.getViewBox()
            vb.disableAutoRange()
            items = plot.listDataItems()
            all_y = []
            for item in items:
                try:
                    xdata, ydata = item.getData()
                except Exception:
                    xdata, ydata = None, getattr(item, "yData", None)
                if ydata is None:
                    continue
                if isinstance(ydata, (list, tuple)):
                    all_y.extend([float(v) for v in ydata if v is not None and np.isfinite(v)])
                else:
                    arr = np.asarray(ydata, dtype=float)
                    arr = arr[np.isfinite(arr)]
                    all_y.extend(arr.tolist())
            if all_y:
                ylo, yhi = float(np.nanmin(all_y)), float(np.nanmax(all_y))
                if ylo == yhi: ylo -= 1; yhi += 1
            else:
                ylo, yhi = 0, 1
            vb.setYRange(ylo, yhi, padding=0.15)
            vb.setMouseEnabled(x=True, y=True)
            vb.setMouseMode(pg.ViewBox.PanMode)

    def normal_zoom(self):
        self.zoom_mode = "auto"
        self._deactivate_rect_zoom()
        self._remove_region()
        for plot in self.get_all_plots():
            vb = plot.getViewBox()
            vb.setMouseEnabled(x=True, y=True)
            vb.setMouseMode(pg.ViewBox.PanMode)

    def zoom_x(self):
        self.zoom_mode = "x"
        self._deactivate_rect_zoom()
        for plot in self.get_all_plots():
            vb = plot.getViewBox()
            vb.setMouseEnabled(x=True, y=False)
            vb.setMouseMode(pg.ViewBox.PanMode)

    def zoom_y(self):
        self.zoom_mode = "y"
        self._deactivate_rect_zoom()
        for plot in self.get_all_plots():
            vb = plot.getViewBox()
            vb.setMouseEnabled(x=False, y=True)
            vb.setMouseMode(pg.ViewBox.PanMode)

    def toggle_rect_zoom(self, checked):
        self.rect_zoom_active = bool(checked)
        if checked:
            self.zoom_mode = "rect"
            for plot in self.get_all_plots():
                vb = plot.getViewBox()
                vb.setMouseEnabled(x=True, y=True)
                vb.setMouseMode(pg.ViewBox.RectMode)
            self._remove_region()
        else:
            self.zoom_mode = "auto"
            for plot in self.get_all_plots():
                vb = plot.getViewBox()
                vb.setMouseEnabled(x=True, y=True)
                vb.setMouseMode(pg.ViewBox.PanMode)
            self._remove_region()
            try:
                self.zoom_rect_btn.setChecked(False)
            except Exception:
                pass

    def _deactivate_rect_zoom(self):
        if self.rect_zoom_active:
            self.rect_zoom_active = False
            try:
                self.zoom_rect_btn.setChecked(False)
            except Exception:
                pass
            for plot in self.get_all_plots():
                vb = plot.getViewBox()
                vb.setMouseEnabled(x=True, y=True)
                vb.setMouseMode(pg.ViewBox.PanMode)
            self._remove_region()

    def _remove_region(self):
        if getattr(self, "region", None) is not None:
            try:
                self.candles_plot.removeItem(self.region)
            except Exception:
                pass
            self.region = None

    # ===================== JUMP TO TRADE =====================

    def jump_to_trade(self, trade: dict, pad_candles: int = 10, pad_y: float = 0.08):
        """
        Ustawia widok wykresu tak, aby:
          - po osi X obejmował zakres od entry_time do exit_time (z marginesem w świecach),
          - po osi Y obejmował min/max ceny z tego okresu (plus mały procentowy margines).

        Bazuje w pierwszej kolejności na raw_df (pełne świece + wskaźniki),
        a dopiero jeśli go brakuje – na last_df (aktualnie narysowany widok).
        Dzięki temu działa poprawnie także przy dynamicznej agregacji (LOD).
        """
        if not trade:
            return

        # preferuj pełne dane (raw_df); fallback na last_df
        df = getattr(self, "raw_df", None)
        if df is None or getattr(df, "empty", True):
            df = getattr(self, "last_df", None)

        if df is None or getattr(df, "empty", True):
            return
        if "timestamp" not in df.columns:
            return

        def _to_epoch(ts):
            if ts is None or ts == "":
                return None
            try:
                return float(pd.to_datetime(ts, utc=True).timestamp())
            except Exception:
                try:
                    return float(ts)
                except Exception:
                    return None

        logging.info(f"[PlotWidget] jump_to_trade in={trade}")

        entry_ts = _to_epoch(trade.get("entry_timestamp") or trade.get("open_time") or trade.get("time"))
        exit_ts = _to_epoch(trade.get("exit_timestamp") or trade.get("close_time"))
        if entry_ts is None:
            return
        if exit_ts is None:
            exit_ts = entry_ts

        # zagwarantuj, że exit >= entry
        if exit_ts < entry_ts:
            entry_ts, exit_ts = exit_ts, entry_ts

        xs = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=float)
        if xs.size == 0:
            return

        # znajdź indeksy świec obejmujących [entry_ts, exit_ts]
        i0 = int(np.searchsorted(xs, entry_ts, side="left"))
        i1 = int(np.searchsorted(xs, exit_ts, side="right")) - 1
        i0 = max(0, min(i0, len(xs) - 1))
        i1 = max(i0, min(i1, len(xs) - 1))

        # dodaj margines w świecach po obu stronach
        i0p = max(0, i0 - int(pad_candles))
        i1p = min(len(xs) - 1, i1 + int(pad_candles))
        x_min = float(xs[i0p]); x_max = float(xs[i1p])
        if x_max <= x_min:
            x_max = x_min + 60.0  # minimalnie jedna minuta

        # Y-range: min/max low/high w tym zakresie
        lows = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)[i0p:i1p + 1]
        highs = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)[i0p:i1p + 1]
        y_min = float(np.nanmin(lows)) if lows.size else np.nan
        y_max = float(np.nanmax(highs)) if highs.size else np.nan

        # uwzględnij entry/exit_price, jeśli są
        for k in ("entry_price", "exit_price"):
            try:
                v = float(trade.get(k, np.nan))
                if np.isfinite(v):
                    y_min = v if not np.isfinite(y_min) else min(y_min, v)
                    y_max = v if not np.isfinite(y_max) else max(y_max, v)
            except Exception:
                pass

        # fallback, gdy z jakiegoś powodu nie ma poprawnych low/high
        if not np.isfinite(y_min) or not np.isfinite(y_max) or y_max == y_min:
            closes = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)[i0p:i1p + 1]
            if closes.size:
                mn, mx = float(np.nanmin(closes)), float(np.nanmax(closes))
                if mn == mx:
                    mn -= 1.0; mx += 1.0
                y_min, y_max = mn, mx
            else:
                y_min, y_max = 0.0, 1.0

        # margines procentowy po Y
        pad_abs = float(pad_y) * (y_max - y_min)
        y_min -= pad_abs; y_max += pad_abs

        # ustaw zakres na głównym wykresie świec
        vb = self.candles_plot.getViewBox()
        vb.disableAutoRange()
        logging.info(f"[PlotWidget] jump_to_trade XR=({x_min},{x_max})  YR=({y_min},{y_max})  i0={i0} i1={i1}  pad={pad_candles}/{pad_y}")
        vb.setXRange(x_min, x_max, padding=0.0)
        vb.setYRange(y_min, y_max, padding=0.0)

        # subchart Y autoscale based on visible X window
        try:
            self._autoscale_subcharts_for_window(pad=pad_y)
        except Exception:
            try:
                self._autoscale_subcharts_in_window(pad=pad_y)
            except Exception:
                pass

        # pionowe linie pomocnicze na entry/exit
        try:
            for it in getattr(self, "_jump_lines", []):
                try:
                    self.candles_plot.removeItem(it)
                except Exception:
                    pass
        except Exception:
            pass
        self._jump_lines = []
        try:
            pen_open = pg.mkPen((255, 255, 255), width=1, style=Qt.DashLine)
            pen_close = pg.mkPen((255, 255, 255), width=1, style=Qt.DashLine)
            ln_o = pg.InfiniteLine(pos=float(entry_ts), angle=90, pen=pen_open)
            ln_c = pg.InfiniteLine(pos=float(exit_ts), angle=90, pen=pen_close)
            self.candles_plot.addItem(ln_o, ignoreBounds=True)
            self.candles_plot.addItem(ln_c, ignoreBounds=True)
            self._jump_lines.extend([ln_o, ln_c])
        except Exception:
            pass

        # po tym jak pyqtgraph/zależne wykresy "przetrawią" zmianę zakresu,
        # jeszcze raz dociągniemy autoscale subchartów
        def _rescale_after_propagation():
            try:
                self._autoscale_subcharts_for_window(pad=pad_y)
            except Exception:
                try:
                    self._autoscale_subcharts_in_window(pad=pad_y)
                except Exception:
                    pass

        try:
            QTimer.singleShot(0, _rescale_after_propagation)
        except Exception:
            pass
    # ========== Cleanup ==========
    def force_df_cleanup(self):
        exclude = {"force_df_cleanup", "last_df"}
        attrs = [a for a in dir(self) if 'df' in a and a not in exclude]
        for a in attrs:
            try:
                setattr(self, a, None)
            except Exception:
                pass
        gc.collect()


def clear_all_series(self):
    try:
        views = [getattr(self, 'main_chart', None), getattr(self, 'subchart1', None), getattr(self, 'subchart2', None), getattr(self, 'subchart3', None)]
        for view in views:
            if not view:
                continue
            try:
                for it in list(view.listDataItems()):
                    try:
                        view.removeItem(it)
                        it.deleteLater()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    for attr in ('_series', '_overlays', 'last_df', '_cached_symbol'):
        if hasattr(self, attr):
            setattr(self, attr, None)


# === Volume layer (bars + line) ===
def ensure_volume_layer(self, subchart):
    if not hasattr(self, '_volume_layers'):
        self._volume_layers = {}
    if subchart in self._volume_layers:
        return self._volume_layers[subchart]
    bars = pg.BarGraphItem(x=[], height=[], width=0.8, brush=None, pen=None)
    subchart.addItem(bars)
    buy_line = subchart.plot([], [], name='taker_buy_volume_quote', pen=pg.mkPen(width=1.5))
    self._volume_layers[subchart] = {'bars': bars, 'line': buy_line}
    return self._volume_layers[subchart]

def update_volume_layer(self, subchart, df):
    if df is None or len(df) == 0 or 'volume_quote' not in df.columns:
        return
    layer = self.ensure_volume_layer(subchart)
    import numpy as np
    x = np.arange(len(df))
    vol = df['volume_quote'].fillna(0).to_numpy()
    up = (df['close'] >= df['open']).fillna(False).to_numpy()
    brushes = [pg.mkBrush(0, 180, 0) if u else pg.mkBrush(200, 0, 0) for u in up]
    layer['bars'].setOpts(x=x, height=vol, brushes=brushes)
    if 'taker_buy_volume_quote' in df.columns:
        layer['line'].setData(x, df['taker_buy_volume_quote'].fillna(0).to_numpy())