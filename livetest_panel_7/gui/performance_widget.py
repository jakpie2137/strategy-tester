
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QMenu, QTableView, QTableWidget, QPushButton,
    QSpacerItem, QSizePolicy, QFrame
)
from PyQt5.QtCore import Qt, pyqtSignal, QPoint
from PyQt5.QtGui import QCursor
from PyQt5 import QtWidgets
import logging

from backtester.utils import smart_price_format

# ---------- helpers (shared) ----------
def _safe_float(x, default=0.0):
    try:
        if x is None:
            return float(default)
        if isinstance(x, str):
            x = x.replace("%", "").replace(" ", "").strip()
        return float(x)
    except Exception:
        return float(default)


def _get_first_key(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _infer_notional_usd(tr):
    if not isinstance(tr, dict):
        return 0.0
    direct = _get_first_key(tr, ["notional", "volume_usd", "quote_qty", "quoteQty"], 0.0)
    val = _safe_float(direct, 0.0)
    if val > 0:
        return val
    amount = _get_first_key(tr, ["amount", "qty", "size", "quantity", "contracts"], 0.0)
    entry = _get_first_key(tr, [
        "entry_price", "open_price", "price_in", "price in", "open price",
        "buy price", "in price"
    ], 0.0)
    a = _safe_float(amount, 0.0)
    p = _safe_float(entry, 0.0)
    return abs(a * p)


def _infer_fee_rate_default():
    try:
        from config import FEE as DEFAULT_FEE_RATE
        return float(DEFAULT_FEE_RATE)
    except Exception:
        return 0.0


def _infer_fee_paid(tr, default_fee_rate=None):
    if not isinstance(tr, dict):
        return 0.0
    fee_fields = ["fee", "fees", "commission", "fee_paid", "fee_open", "fee_close"]
    total = 0.0
    found_any = False
    for k in fee_fields:
        if k in tr and tr[k] is not None:
            total += _safe_float(tr[k], 0.0)
            found_any = True
    if found_any:
        return total
    if default_fee_rate is None:
        default_fee_rate = _infer_fee_rate_default()
    fr = _safe_float(_get_first_key(tr, ["fee_rate", "commission_rate", "taker_fee"], default_fee_rate), 0.0)
    if fr <= 0:
        return 0.0
    amount = _get_first_key(tr, ["amount", "qty", "size", "quantity", "contracts"], 0.0)
    entry = _get_first_key(tr, ["entry_price", "open_price", "price_in", "price in", "open price", "buy price", "in price"], 0.0)
    exitp = _get_first_key(tr, ["exit_price", "close_price", "price_out", "price out", "sell price", "close price", "out price"], entry)
    a = _safe_float(amount, 0.0); ep = _safe_float(entry, 0.0); xp = _safe_float(exitp, ep)
    return fr * (abs(a*ep) + abs(a*xp))


def _infer_start_balance_default():
    try:
        import config
        for name in ("INITIAL_BALANCE", "STARTING_BALANCE", "START_BALANCE", "CAPITAL", "BALANCE"):
            if hasattr(config, name):
                return float(getattr(config, name))
    except Exception:
        pass
    return None


def _infer_leverage_default():
    try:
        import config
        for name in ("LEVERAGE", "DEFAULT_LEVERAGE"):
            if hasattr(config, name):
                v = float(getattr(config, name))
                if v > 0:
                    return v
    except Exception:
        pass
    return None


def _infer_initial_margin(tr):
    if not isinstance(tr, dict):
        return 0.0
    direct = _get_first_key(tr, ["margin", "initial_margin", "used_margin", "im"], None)
    if direct is not None:
        return abs(_safe_float(direct, 0.0))
    notional = _infer_notional_usd(tr)
    lev = _get_first_key(tr, ["leverage", "lev"], _infer_leverage_default())
    lev = _safe_float(lev, 0.0)
    return notional/lev if (notional > 0 and lev > 0) else 0.0


def _pnl_pct(tr):
    pnl = _safe_float(tr.get("pnl", 0.0), 0.0)
    base = _infer_notional_usd(tr)
    return (pnl/base*100.0) if base > 0 else 0.0


def compute_extended_metrics(trades):
    trades = [t for t in (trades or []) if isinstance(t, dict)]
    n = len(trades)
    total_pnl = sum(_safe_float(t.get("pnl", 0.0), 0.0) for t in trades)
    wins = [t for t in trades if _safe_float(t.get("pnl", 0.0), 0.0) > 0]
    losses = [t for t in trades if _safe_float(t.get("pnl", 0.0), 0.0) < 0]

    total_volume_usd = sum(_infer_notional_usd(t) for t in trades)
    total_fee_paid = sum(_infer_fee_paid(t) for t in trades)

    avg_win_usd = sum(_safe_float(t.get("pnl", 0.0), 0.0) for t in wins) / len(wins) if wins else 0.0
    avg_loss_usd = sum(_safe_float(t.get("pnl", 0.0), 0.0) for t in losses) / len(losses) if losses else 0.0

    win_pcts = [_pnl_pct(t) for t in wins] if wins else []
    loss_pcts = [_pnl_pct(t) for t in losses] if losses else []

    avg_gain_pct = (sum(win_pcts)/len(win_pcts)) if win_pcts else 0.0
    avg_loss_pct = (sum(loss_pcts)/len(loss_pcts)) if loss_pcts else 0.0
    total_avg_gain_pct = (total_pnl/total_volume_usd*100.0) if total_volume_usd > 0 else 0.0  # VWATR

    start_bal = _infer_start_balance_default()
    equity_return_pct = (total_pnl/start_bal*100.0) if (start_bal and start_bal > 0) else None
    total_initial_margin = sum(_infer_initial_margin(t) for t in trades)
    roi_pct = (total_pnl/total_initial_margin*100.0) if total_initial_margin > 0 else None

    return {
        "n": n,
        "win_rate": (100.0*len(wins)/n) if n else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": (total_pnl/n) if n else 0.0,
        "best": max((_safe_float(t.get("pnl", 0.0), 0.0) for t in trades), default=0.0),
        "worst": min((_safe_float(t.get("pnl", 0.0), 0.0) for t in trades), default=0.0),
        "total_volume_usd": total_volume_usd,
        "total_fee_paid": total_fee_paid,
        "avg_win_usd": avg_win_usd,
        "avg_loss_usd": avg_loss_usd,
        "avg_gain_pct": avg_gain_pct,
        "avg_loss_pct": avg_loss_pct,
        "total_avg_gain_pct": total_avg_gain_pct,  # VWATR
        "equity_return_pct": equity_return_pct,    # ROC
        "roi_pct": roi_pct,                        # ROI
    }


def _hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)
    line.setFixedHeight(1)
    # subtle line that works in dark/light themes
    line.setStyleSheet("background-color: rgba(255,255,255,0.12); border: none; margin: 6px 0;")
    return line


class PerformanceWidget(QWidget):
    jump_to_trade = pyqtSignal(dict)

    _HEADER_ALIASES = {
        'symbol': {'symbol', 'pair', 'market', 'ticker', 'instrument', 'sym', 'para', 'rynek'},
        'side': {'side', 'direction', 'typ', 'kierunek', 'long/short', 'pos side', 'strona', 'pozycja'},
        'entry_timestamp': {
            'entry_timestamp', 'entry time', 'open_time', 'open time',
            'timestamp_open', 'czas wejścia', 'wejście', 'otwarcie',
            'data wejścia', 'czas otwarcia', 'start', 'opened at'
        },
        'exit_timestamp': {
            'exit_timestamp', 'exit time', 'close_time', 'close time',
            'timestamp_close', 'czas wyjścia', 'wyjście', 'zamknięcie',
            'data wyjścia', 'czas zamknięcia', 'end', 'closed at'
        },
        'entry_price': {
            'entry_price', 'open_price', 'price_in', 'price in',
            'cena wejścia', 'cena otwarcia', 'wejście cena', 'open price', 'buy price', 'in price'
        },
        'exit_price': {
            'exit_price', 'close_price', 'price_out', 'price out',
            'cena wyjścia', 'cena zamknięcia', 'sell price', 'close price', 'out price'
        },
    }

    def __init__(self):
        super().__init__()
        self.setMaximumWidth(940)
        self._wired_tables = []

        # Section 1
        self.n_label = QLabel("Liczba trejdów: -")
        self.winrate_label = QLabel("Win-rate: -")
        self.total_label = QLabel("Suma PnL: -")

        # Section 2
        self.avg_label = QLabel("Śr. PnL: -")
        self.best_label = QLabel("Best trade: -")
        self.worst_label = QLabel("Worst trade: -")

        # Section 3 (extended)
        self.total_volume_label = QLabel("Wolumen (USD): -")
        self.total_fee_label = QLabel("Prowizje (USD): -")
        self.avg_win_usd_label = QLabel("Śr. zysk $: -")
        self.avg_loss_usd_label = QLabel("Śr. strata $: -")
        self.avg_gain_pct_label = QLabel("Śr. zysk %: -")
        self.avg_loss_pct_label = QLabel("Śr. strata %: -")
        self.total_avg_gain_pct_label = QLabel("Śr. zwrot ważony % (VWATR): -")
        self.equity_return_pct_label = QLabel("ROC (zwrot kapitału) %: -")
        self.roi_pct_label = QLabel("ROI %: -")

        for lab in [
            self.n_label, self.winrate_label, self.total_label,
            self.avg_label, self.best_label, self.worst_label,
            self.total_volume_label, self.total_fee_label,
            self.avg_win_usd_label, self.avg_loss_usd_label,
            self.avg_gain_pct_label, self.avg_loss_pct_label,
            self.total_avg_gain_pct_label, self.equity_return_pct_label, self.roi_pct_label
        ]:
            lab.setStyleSheet("font-size: 16px; padding: 2px;")

        self.jump_btn = QPushButton("🔎 Jump to selected trade - ( J ) button")
        self.jump_btn.clicked.connect(self._emit_from_focused_table)

        layout = QVBoxLayout(self)
        # -- section 1 --
        layout.addWidget(self.n_label)
        layout.addWidget(self.winrate_label)
        layout.addWidget(self.total_label)
        layout.addWidget(_hline())  # visual separator

        # -- section 2 --
        layout.addWidget(self.avg_label)
        layout.addWidget(self.best_label)
        layout.addWidget(self.worst_label)
        layout.addWidget(_hline())  # visual separator

        # -- section 3 (requested order) --
        layout.addWidget(self.total_volume_label)
        layout.addWidget(self.total_fee_label)
        layout.addWidget(self.avg_win_usd_label)         # 1
        layout.addWidget(self.avg_loss_usd_label)        # 2
        layout.addWidget(self.avg_gain_pct_label)        # 3
        layout.addWidget(self.avg_loss_pct_label)        # 4
        layout.addWidget(self.total_avg_gain_pct_label)  # 5 (VWATR)
        layout.addWidget(self.equity_return_pct_label)   # 6 (ROC)
        layout.addWidget(self.roi_pct_label)             # 7 (ROI)

        layout.addItem(QSpacerItem(0, 10, QSizePolicy.Minimum, QSizePolicy.Minimum))
        layout.addWidget(self.jump_btn)
        layout.addStretch(1)

    # ---------- public ----------
    def auto_wire_from_parent(self, parent):
        try:
            tables = []
            tables.extend(parent.findChildren(QTableWidget))
            tables.extend(parent.findChildren(QTableView))
            for t in tables:
                self._wire_any_table(t)
        except Exception as e:
            logging.exception(f"[PerformanceWidget] auto_wire_from_parent err: {e}")

    def set_trade_tables(self, *tables):
        for t in tables:
            if t is None:
                continue
            self._wire_any_table(t)

    def update_stats(self, trades):
        trades = [t for t in (trades or []) if t is not None]
        if not trades:
            # reset labels
            self.n_label.setText("Liczba trejdów: 0")
            self.winrate_label.setText("Win-rate: -")
            self.total_label.setText("Suma PnL: -")
            self.avg_label.setText("Śr. PnL: -")
            self.best_label.setText("Best trade: -")
            self.worst_label.setText("Worst trade: -")
            self.total_volume_label.setText("Wolumen (USD): -")
            self.total_fee_label.setText("Prowizje (USD): -")
            self.avg_win_usd_label.setText("Śr. zysk $: -")
            self.avg_loss_usd_label.setText("Śr. strata $: -")
            self.avg_gain_pct_label.setText("Śr. zysk %: -")
            self.avg_loss_pct_label.setText("Śr. strata %: -")
            self.total_avg_gain_pct_label.setText("Śr. zwrot ważony % (VWATR): -")
            self.equity_return_pct_label.setText("ROC (zwrot kapitału) %: -")
            self.roi_pct_label.setText("ROI %: -")
            return

        m = compute_extended_metrics(trades)
        self.n_label.setText(f"Liczba trejdów: <b>{m['n']}</b>")
        self.winrate_label.setText(f"Win-rate: <b>{m['win_rate']:.2f}%</b>")
        self.total_label.setText(f"Suma PnL: <b>{smart_price_format(m['total_pnl'])}</b>")
        self.avg_label.setText(f"Śr. PnL: <b>{smart_price_format(m['avg_pnl'])}</b>")
        self.best_label.setText(f"Best trade: <b>{smart_price_format(m['best'])}</b>")
        self.worst_label.setText(f"Worst trade: <b>{smart_price_format(m['worst'])}</b>")
        self.total_volume_label.setText(f"Wolumen (USD): <b>{smart_price_format(m['total_volume_usd'])}</b>")
        self.total_fee_label.setText(f"Prowizje (USD): <b>{smart_price_format(m['total_fee_paid'])}</b>")
        self.avg_win_usd_label.setText(f"Śr. zysk $: <b>{smart_price_format(m['avg_win_usd'])}</b>")
        self.avg_loss_usd_label.setText(f"Śr. strata $: <b>{smart_price_format(m['avg_loss_usd'])}</b>")
        self.avg_gain_pct_label.setText(f"Śr. zysk %: <b>{m['avg_gain_pct']:.2f}%</b>")
        self.avg_loss_pct_label.setText(f"Śr. strata %: <b>{m['avg_loss_pct']:.2f}%</b>")
        self.total_avg_gain_pct_label.setText(f"Śr. zwrot ważony % (VWATR): <b>{m['total_avg_gain_pct']:.2f}%</b>")
        roc = m.get("equity_return_pct")
        self.equity_return_pct_label.setText(
            f"ROC (zwrot kapitału) %: <b>{roc:.2f}%</b>" if roc is not None else
            "ROC (zwrot kapitału) %: <i>— (brak INITIAL_BALANCE w config)</i>"
        )
        roi = m.get("roi_pct")
        self.roi_pct_label.setText(
            f"ROI %: <b>{roi:.2f}%</b>" if roi is not None else
            "ROI %: <i>— (brak danych o margin/leverage)</i>"
        )

    # ---------- internals ----------
    def _wire_any_table(self, obj):
        try:
            view = None
            if isinstance(obj, QTableWidget):
                view = obj; self._wire_qtablewidget(view)
            elif isinstance(obj, QTableView):
                view = obj; self._wire_qtableview(view)
            else:
                view = getattr(obj, "view", None)
                if isinstance(view, QTableWidget):
                    self._wire_qtablewidget(view)
                elif isinstance(view, QTableView):
                    self._wire_qtableview(view)
                else:
                    return
            self._wired_tables.append(view)
        except Exception as e:
            logging.debug(f"[JumpWire] cannot hook: {e}")

    def _wire_qtableview(self, view: QTableView):
        view.doubleClicked.connect(lambda idx, v=view: self._emit_trade_from_qtableview(v, idx.row(), "doubleClick"))
        view.activated.connect(lambda idx, v=view: self._emit_trade_from_qtableview(v, idx.row(), "activated"))
        self._attach_context_menu(view, lambda: self._emit_trade_from_qtableview(view, view.currentIndex().row(), "contextMenu"))

    def _wire_qtablewidget(self, view: QTableWidget):
        view.cellDoubleClicked.connect(lambda row, _col, v=view: self._emit_trade_from_qtablewidget(v, row, "doubleClick"))
        view.itemActivated.connect(lambda _item, v=view: self._emit_trade_from_qtablewidget(v, v.currentRow(), "activated"))
        self._attach_context_menu(view, lambda: self._emit_trade_from_qtablewidget(view, view.currentRow(), "contextMenu"))

    def _attach_context_menu(self, view, trigger_callable):
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        def _on_menu(pos: QPoint):
            menu = QMenu(view)
            act = menu.addAction("🔎 Jump to trade")
            act.triggered.connect(trigger_callable)
            menu.exec_(view.viewport().mapToGlobal(pos))
        view.customContextMenuRequested.connect(_on_menu)

    def _emit_from_focused_table(self):
        w = QtWidgets.QApplication.focusWidget()
        if not isinstance(w, (QTableWidget, QTableView)):
            obj = QtWidgets.QApplication.widgetAt(QCursor.pos())
            if isinstance(obj, (QTableWidget, QTableView)):
                w = obj
        if isinstance(w, QTableWidget):
            row = w.currentRow()
            self._emit_trade_from_qtablewidget(w, row, "button")
        elif isinstance(w, QTableView):
            row = w.currentIndex().row()
            self._emit_trade_from_qtableview(w, row, "button")

    @classmethod
    def _normalize(cls, s: str) -> str:
        return (s or "").strip().lower()

    @classmethod
    def _map_headers(cls, headers_lower):
        res = {k: None for k in ('symbol', 'side', 'entry_timestamp', 'exit_timestamp', 'entry_price', 'exit_price')}
        for idx, h in enumerate(headers_lower):
            for key, aliases in cls._HEADER_ALIASES.items():
                if res[key] is not None:
                    continue
                for alias in aliases:
                    if h == alias or alias in h:
                        res[key] = idx; break
        return res

    def _emit_trade_from_qtableview(self, view: QTableView, row_idx: int, source: str):
        try:
            if view is None or row_idx is None or row_idx < 0:
                return
            model = view.model()
            if model is None:
                return
            try:
                idx0 = model.index(row_idx, 0)
                data_user = model.data(idx0, role=Qt.UserRole)
                if isinstance(data_user, dict):
                    self.jump_to_trade.emit(data_user); return
            except Exception:
                pass
            cols = getattr(model, "columnCount", lambda *_: 0)()
            headers = []
            for c in range(cols):
                try:
                    h = str(model.headerData(c, Qt.Horizontal))
                except Exception:
                    h = f"col{c}"
                headers.append(self._normalize(h))
            colmap = self._map_headers(headers)

            def _val(c):
                try:
                    idx = model.index(row_idx, c)
                    v = model.data(idx, role=Qt.UserRole)
                    if v in (None, ""):
                        v = model.data(idx, role=Qt.DisplayRole)
                    return v
                except Exception:
                    return None

            trade = {k: _val(v) if v is not None else None for k, v in colmap.items()}
            self.jump_to_trade.emit(trade)
        except Exception as e:
            logging.exception(f"[PerformanceWidget] emit from QTableView err: {e}")

    def _emit_trade_from_qtablewidget(self, view: QTableWidget, row_idx: int, source: str):
        try:
            if view is None or row_idx is None or row_idx < 0:
                return
            cols = view.columnCount()
            headers = []
            for c in range(cols):
                try:
                    item = view.horizontalHeaderItem(c)
                    h = str(item.text()) if item is not None else f"col{c}"
                except Exception:
                    h = f"col{c}"
                headers.append(self._normalize(h))
            colmap = self._map_headers(headers)

            def _val(c):
                try:
                    it = view.item(row_idx, c)
                    if it is None:
                        return None
                    v = it.data(Qt.UserRole)
                    if v in (None, ""):
                        v = it.text()
                    return v
                except Exception:
                    return None

            trade = {k: _val(v) if v is not None else None for k, v in colmap.items()}
            self.jump_to_trade.emit(trade)
        except Exception as e:
            logging.exception(f"[PerformanceWidget] emit from QTableWidget err: {e}")
