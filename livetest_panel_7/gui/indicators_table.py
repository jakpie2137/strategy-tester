
# gui/indicators_table.py
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QSizePolicy
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHeaderView

class IndicatorsTable(QWidget):
    def __init__(self):
        super().__init__()
        self.table = QTableWidget(0, 1, self)
        self.table.setHorizontalHeaderLabels(["Indicators (preview)"])
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet('QTableWidget {alternate-background-color: #22252a;}')
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(60)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.table)

        self._user_resized = False
        self._col_weights = None
        header.sectionResized.connect(self._on_user_resized)

    def update_indicators(self, df):
        if df is None:
            self.table.setRowCount(0)
            self.table.setColumnCount(1)
            self.table.setHorizontalHeaderLabels(["Indicators (preview)"])
        else:
            self.table.setColumnCount(len(df.columns))
            self.table.setHorizontalHeaderLabels([str(c) for c in df.columns])
            self.table.setRowCount(len(df))
            for r in range(len(df)):
                for c in range(len(df.columns)):
                    self.table.setItem(r, c, QTableWidgetItem(str(df.iloc[r, c])))

        if not self._user_resized:
            self._set_even_columns()

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._user_resized:
            self._set_even_columns()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._user_resized and self._col_weights:
            self._apply_proportional_widths()
        else:
            self._set_even_columns()

    def _on_user_resized(self, *_):
        self._user_resized = True
        cols = self.table.columnCount()
        total = sum(max(1, self.table.columnWidth(i)) for i in range(cols)) or 1
        self._col_weights = [max(1, self.table.columnWidth(i)) / total for i in range(cols)]

    def _set_even_columns(self):
        cols = self.table.columnCount()
        if cols <= 0:
            return
        avail = self.table.viewport().width()
        try:
            vsb = self.table.verticalScrollBar()
            if vsb and vsb.isVisible():
                avail -= vsb.width()
        except Exception:
            pass
        if avail <= 0:
            return
        base = max(60, int(avail // cols))
        rem = max(0, avail - base * cols)
        for i in range(cols):
            w = base + (1 if i < rem else 0)
            self.table.setColumnWidth(i, w)

    def _apply_proportional_widths(self):
        cols = self.table.columnCount()
        if not self._col_weights or len(self._col_weights) != cols:
            return
        avail = self.table.viewport().width()
        try:
            vsb = self.table.verticalScrollBar()
            if vsb and vsb.isVisible():
                avail -= vsb.width()
        except Exception:
            pass
        if avail <= 0:
            return
        widths = [max(60, int(avail * w)) for w in self._col_weights]
        diff = avail - sum(widths)
        i = 0
        while diff != 0 and cols > 0:
            widths[i % cols] += 1 if diff > 0 else -1
            diff += -1 if diff > 0 else 1
            i += 1
        for i, w in enumerate(widths):
            self.table.setColumnWidth(i, w)


# === Added columns for volume ===
def _last_safe(df, col):
    try:
        return df[col].iloc[-1]
    except Exception:
        return ""

def add_volume_columns_to_table(self, row, df):
    if hasattr(self, 'table'):
        from PyQt5.QtWidgets import QTableWidgetItem
        if 'volume_quote' in df.columns:
            self.table.setItem(row, self.table.columnCount()-1, QTableWidgetItem(str(_last_safe(df,'volume_quote'))))
        if 'taker_buy_volume_quote' in df.columns:
            self.table.setItem(row, self.table.columnCount()-1, QTableWidgetItem(str(_last_safe(df,'taker_buy_volume_quote'))))
