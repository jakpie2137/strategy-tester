# gui/ma_overlay_patch.py
"""
Bezpieczny, manualny patch na MA overlay. Wywołaj ma_overlay_patch.apply_patch()
DOPIERO po zaimportowaniu gui.plot_widget (i najlepiej po utworzeniu okna).
Jeśli PlotWidget ma już MA w update_chart -> patch nic nie robi.
"""
import logging

def apply_patch():
    try:
        from gui.plot_widget import PlotWidget
    except Exception as e:
        logging.debug(f"[MA patch] Nie mogę zaimportować PlotWidget: {e}")
        return

    # Jeżeli PlotWidget już ma _draw_ma albo overlay MA jest obsłużony – odpuszczamy.
    if hasattr(PlotWidget, "_draw_ma") or hasattr(PlotWidget, "_draw_ma_overlays"):
        logging.debug("[MA patch] _draw_ma istnieje – patch niepotrzebny.")
        return

    # Jeżeli nie ma update_chart ani set_history – nie patchujemy nic.
    target_name = None
    if hasattr(PlotWidget, "update_chart"):
        target_name = "update_chart"
    elif hasattr(PlotWidget, "set_history"):
        target_name = "set_history"
    else:
        logging.debug("[MA patch] Brak update_chart/set_history – rezygnuję.")
        return

    # Dodajemy minimalny rysownik MA na podstawie kolumn MA_FAST/MA_SLOW (bez zależności zewn.)
    def _clear_ma_items(self):
        for it in list(getattr(self, "_ma_items", [])):
            try:
                self.candles_plot.removeItem(it)
            except Exception:
                pass
        self._ma_items = []

    def _draw_ma_overlays(self, df):
        try:
            import numpy as np, pandas as pd, pyqtgraph as pg
            if df is None or getattr(df, "empty", True):
                return
            if "timestamp" not in df.columns:
                return
            x = pd.to_numeric(df["timestamp"], errors="coerce").values.astype("int64")
            fast = df["MA_FAST"].to_numpy() if "MA_FAST" in df.columns else None
            slow = df["MA_SLOW"].to_numpy() if "MA_SLOW" in df.columns else None
            if fast is None and slow is None:
                return
            self._ma_items = getattr(self, "_ma_items", [])

            if fast is not None:
                c1 = pg.PlotCurveItem(x, fast)
                c1.setPen(pg.mkPen((255, 214, 102), width=1.8))
                c1.setZValue(10); self.candles_plot.addItem(c1); self._ma_items.append(c1)
            if slow is not None:
                c2 = pg.PlotCurveItem(x, slow)
                c2.setPen(pg.mkPen((180, 180, 180), width=1.2))
                c2.setZValue(9); self.candles_plot.addItem(c2); self._ma_items.append(c2)
        except Exception as e:
            logging.debug(f"[MA patch] draw err: {e}")

    setattr(PlotWidget, "_clear_ma_items", _clear_ma_items)
    setattr(PlotWidget, "_draw_ma_overlays", _draw_ma_overlays)

    orig = getattr(PlotWidget, target_name)
    def wrapped(self, *args, **kwargs):
        res = orig(self, *args, **kwargs)
        try:
            self._clear_ma_items()
            df = args[0] if args else None
            self._draw_ma_overlays(df)
        except Exception as e:
            logging.debug(f"[MA patch] wrapped err: {e}")
        return res
    setattr(PlotWidget, target_name, wrapped)
    logging.debug(f"[MA patch] Patched {target_name} z overlay MA.")
