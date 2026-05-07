# gui/controls.py
from typing import Optional

from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QComboBox, QPushButton, QGroupBox, QCheckBox
)

from config import AVAILABLE_PAIRS, DEFAULT_PAIR, STRATEGY_CHOICES, BIAS_CHOICES, DEFAULT_BIAS


class Controls(QWidget):
    """
    Panel sterujący:
      - wybór pary, strategii, biasu,
      - start pobierania i test,
      - przełączniki widoczności: MA1, MA2, BB, Trades.

    Sygnały:
      - indicatorsVisibilityChanged(dict): {"ma1":bool, "ma2":bool, "bb":bool, "trades":bool}

    Dodatkowo możesz bezpośrednio spiąć z wykresem:
        controls.attach_plot_widget(plot_widget)
    wtedy checkboxy będą natychmiast wywoływać:

        plot_widget.set_ma1_visible(bool)
        plot_widget.set_ma2_visible(bool)
        plot_widget.set_bb_visible(bool)
        plot_widget.set_trades_visible(bool)
    oraz – jak dawniej – sygnał indicatorsVisibilityChanged też będzie emitowany.
    """
    indicatorsVisibilityChanged = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plot_widget = None  # type: Optional[object]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ---------------- Pairs ----------------
        self.pair_box = QComboBox()
        self.pair_box.addItems(AVAILABLE_PAIRS)
        try:
            self.pair_box.setCurrentText(DEFAULT_PAIR)
        except Exception:
            pass

        # ---------------- Strategy ----------------
        self.strategy_box = QComboBox()
        try:
            self.strategy_box.addItems(STRATEGY_CHOICES.keys())
        except Exception:
            # fallback jeśli STRATEGY_CHOICES to np. lista
            try:
                self.strategy_box.addItems(list(STRATEGY_CHOICES))
            except Exception:
                pass

        # ---------------- MA / BB / Trades ----------------
        grp = QGroupBox("MA / BB / Trades")
        g_layout = QHBoxLayout(grp)
        g_layout.setContentsMargins(8, 6, 8, 6)
        g_layout.setSpacing(10)

        self.chk_ma1 = QCheckBox("MA1")
        self.chk_ma1.setChecked(True)
        self.chk_ma2 = QCheckBox("MA2")
        self.chk_ma2.setChecked(True)
        self.chk_bb = QCheckBox("BB")
        self.chk_bb.setChecked(True)
        self.chk_trades = QCheckBox("Trades")
        self.chk_trades.setChecked(True)

        for w in (self.chk_ma1, self.chk_ma2, self.chk_bb, self.chk_trades):
            g_layout.addWidget(w)
            # Jeden wspólny handler – od razu działa i dla plot_widget, i sygnału
            w.stateChanged.connect(self._on_chk_toggled)

        layout.addWidget(grp)

        # ---------------- Bias ----------------
        self.bias_box = QComboBox()
        try:
            self.bias_box.addItems(BIAS_CHOICES)
            self.bias_box.setCurrentText(DEFAULT_BIAS)
        except Exception:
            pass

        # ---------------- Action buttons ----------------
        self.pull_btn = QPushButton("Start Pulling Data")
        self.test_btn = QPushButton("Test Strategy")

        # ---------------- Compose row ----------------
        layout.addWidget(QLabel("Para:"))
        layout.addWidget(self.pair_box)
        layout.addWidget(QLabel("Strategia:"))
        layout.addWidget(self.strategy_box)
        layout.addWidget(self.pull_btn)
        layout.addWidget(self.test_btn)
        layout.addWidget(QLabel("Bias:"))
        layout.addWidget(self.bias_box)

        self.debug_ram_btn = QPushButton("DEBUG RAM")
        layout.addWidget(self.debug_ram_btn)

        layout.addStretch(1)

        # Na starcie wyemituj stan widoczności (żeby UI/plot dostały pierwsze ustawienia)
        self._emit_visibility()

    # ---------------- Public API ----------------
    def attach_plot_widget(self, plot_widget: object):
        """
        Opcjonalne bezpośrednie spięcie z PlotWidgetem.
        Dzięki temu checkboxy od razu włączają/wyłączają warstwy na wykresie,
        a dodatkowo i tak emituje się indicatorsVisibilityChanged(state).
        """
        self._plot_widget = plot_widget
        self._push_state_to_plot()

    def get_settings(self) -> dict:
        return {
            "pair": self.pair_box.currentText() if hasattr(self, "pair_box") else None,
            "strategy": self.strategy_box.currentText() if hasattr(self, "strategy_box") else None,
            "bias": self.bias_box.currentText() if hasattr(self, "bias_box") else "None",
        }

    def get_visibility_state(self) -> dict:
        """Zwraca aktualny stan przełączników widoczności."""
        return {
            'ma1': self.chk_ma1.isChecked() if hasattr(self, 'chk_ma1') else True,
            'ma2': self.chk_ma2.isChecked() if hasattr(self, 'chk_ma2') else True,
            'bb': self.chk_bb.isChecked() if hasattr(self, 'chk_bb') else True,
            'trades': self.chk_trades.isChecked() if hasattr(self, 'chk_trades') else True,
        }

    # ---------------- Internals ----------------
    def _on_chk_toggled(self, _state: int):
        # 1) natychmiast przekaż do wykresu (jeśli podpięty)
        self._push_state_to_plot()
        # 2) wyemituj (dla reszty aplikacji / kompatybilność wstecz)
        self._emit_visibility()

    def _push_state_to_plot(self):
        state = self.get_visibility_state()
        pw = self._plot_widget
        if pw is None:
            return
        # używamy poręcznych aliasów dodanych do PlotWidget
        try:
            if hasattr(pw, "set_ma1_visible"): pw.set_ma1_visible(bool(state['ma1']))
            if hasattr(pw, "set_ma2_visible"): pw.set_ma2_visible(bool(state['ma2']))
            if hasattr(pw, "set_bb_visible"): pw.set_bb_visible(bool(state['bb']))
            if hasattr(pw, "set_trades_visible"): pw.set_trades_visible(bool(state['trades']))
        except Exception:
            # w ostateczności – zbiorczo:
            try:
                if hasattr(pw, "apply_indicator_visibility"):
                    pw.apply_indicator_visibility(state)
            except Exception:
                pass

    def _emit_visibility(self):
        self.indicatorsVisibilityChanged.emit(self.get_visibility_state())
