
# gui/trades_table.py
from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem, QSizePolicy
from PyQt5.QtCore import Qt, QDateTime
from PyQt5.QtWidgets import QHeaderView

from PyQt5.QtCore import QDateTime

class _SortableItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by Qt.UserRole value when present."""
    def __lt__(self, other):
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole)
        if a is None or b is None:
            try:
                return float(self.text()) < float(other.text())
            except Exception:
                return super().__lt__(other)
        return a < b

import pandas as pd

_DESIRED_HEADERS = [
    "Trade ID", "Symbol", "Side", "Entry Price", "Exit Price",
    "PnL", "Fee", "Amount", "Entry Time", "Exit Time"
]

def _fmt_num(x):
    try:
        if x is None or x == "":
            return ""
        return f"{float(x):.8f}".rstrip('0').rstrip('.')
    except Exception:
        return str(x)

def _fmt_time(x):
    if x is None or x == "":
        return ""
    try:
        return pd.to_datetime(x, utc=True, errors="coerce").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(x)

class TradesTable(QTableWidget):
    """
    QTableWidget that:
      - lets user drag column widths (Interactive),
      - expands to fill the segment,
      - when the container resizes and user hasn't customized, columns expand EVENLY,
      - after user drags, preserves their proportions on next container resize.
    """
    def __init__(self):
        super().__init__(0, len(_DESIRED_HEADERS))
        self.setHorizontalHeaderLabels(_DESIRED_HEADERS)

        self.setAlternatingRowColors(True)
        self.setStyleSheet('QTableWidget {alternate-background-color: #22252a;}')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)  # drag with cursor
        header.setStretchLastSection(False)                   # we handle fill ourselves
        header.setMinimumSectionSize(60)

        # resizing behavior flags
        self._user_resized = False
        self._col_weights = None
        header.sectionResized.connect(self._on_user_resized)

    # ---------- public API ----------
    
    def update_trades(self, trades):
        trades = [t for t in (trades or []) if t is not None]
        self.setSortingEnabled(False)
        self.setRowCount(0)
        for t in trades:
            row = self.rowCount()
            self.insertRow(row)

            # Prepare raw values for sorting
            def ts_val(x):
                try:
                    return pd.to_datetime(x, utc=True, errors="coerce").value  # ns since epoch
                except Exception:
                    return None
            def fval(x):
                try:
                    return float(x)
                except Exception:
                    return None

            cells = [
                ("Trade ID", str(t.get("trade_id", "")), t.get("trade_id")),
                ("Symbol",   str(t.get("symbol", "")),  None),
                ("Side",     str(t.get("side", "")),    None),
                ("Entry Price", _fmt_num(t.get("entry_price")), fval(t.get("entry_price"))),
                ("Exit Price",  _fmt_num(t.get("exit_price")),  fval(t.get("exit_price"))),
                ("PnL",         _fmt_num(t.get("pnl")),         fval(t.get("pnl"))),
                ("Fee",         _fmt_num(t.get("fee")),         fval(t.get("fee"))),
                ("Amount",      _fmt_num(t.get("amount")),      fval(t.get("amount"))),
                ("Entry Time",  _fmt_time(t.get("entry_time") or t.get("entry_timestamp")), ts_val(t.get("entry_time") or t.get("entry_timestamp"))),
                ("Exit Time",   _fmt_time(t.get("exit_time") or t.get("exit_timestamp")),   ts_val(t.get("exit_time") or t.get("exit_timestamp"))),
            ]
            for c, (_h, text, sort_val) in enumerate(cells):
                item = _SortableItem(text)
                if sort_val is not None:
                    item.setData(Qt.UserRole, sort_val)
                self.setItem(row, c, item)

        # initialize equal layout after data change if not customized
        if not self._user_resized:
            self._set_even_columns()

        # Enable sorting and default sort by 'Entry Time' (column 8) DESC
        self.setSortingEnabled(True)
        try:
            self.sortItems(8, Qt.DescendingOrder)
        except Exception:
            pass
    
    # ---------- sizing logic ----------
    def showEvent(self, ev):
        super().showEvent(ev)
        # on first show, if user hasn't resized, enforce even spread
        if not self._user_resized:
            self._set_even_columns()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._user_resized and self._col_weights:
            self._apply_proportional_widths()
        else:
            self._set_even_columns()

    def _on_user_resized(self, *_):
        # mark custom mode and recompute proportional weights
        self._user_resized = True
        cols = self.columnCount()
        total = sum(max(1, self.columnWidth(i)) for i in range(cols)) or 1
        self._col_weights = [max(1, self.columnWidth(i)) / total for i in range(cols)]

    def _set_even_columns(self):
        cols = self.columnCount()
        if cols <= 0:
            return
        avail = self.viewport().width()
        try:
            vsb = self.verticalScrollBar()
            if vsb and vsb.isVisible():
                avail -= vsb.width()
        except Exception:
            pass
        if avail <= 0:
            return
        base = max(60, int(avail // cols))
        rem = max(0, avail - base * cols)
        # apply even widths, distribute remainder to the leftmost columns
        for i in range(cols):
            w = base + (1 if i < rem else 0)
            self.setColumnWidth(i, w)

    def _apply_proportional_widths(self):
        cols = self.columnCount()
        if not self._col_weights or len(self._col_weights) != cols:
            return
        avail = self.viewport().width()
        try:
            vsb = self.verticalScrollBar()
            if vsb and vsb.isVisible():
                avail -= vsb.width()
        except Exception:
            pass
        if avail <= 0:
            return
        widths = [max(60, int(avail * w)) for w in self._col_weights]
        # Normalize rounding error
        diff = avail - sum(widths)
        i = 0
        while diff != 0 and cols > 0:
            widths[i % cols] += 1 if diff > 0 else -1
            diff += -1 if diff > 0 else 1
            i += 1
        for i, w in enumerate(widths):
            self.setColumnWidth(i, w)

    # optional utility
    def reset_even(self):
        """Call to forget custom sizes and go back to even mode."""
        self._user_resized = False
        self._col_weights = None
        self._set_even_columns()
