# gui/candlestick_item.py  — KOLORY ŚWIEC (drop-in)
import numpy as np
from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QPainter
import pyqtgraph as pg

# >>> KOLORY (konfigurowalne z config.py; jeśli brak – użyjemy domyślnych)
try:
    from config import (
        CANDLE_BULL_COLOR,   # np. "#00E676"
        CANDLE_BEAR_COLOR,   # np. "#FF1744"
        CANDLE_BODY_ALPHA,   # 0..255 (opcjonalnie)
        CANDLE_WICK_ALPHA,   # 0..255 (opcjonalnie)
    )
except Exception:
    CANDLE_BULL_COLOR = "#00E676"
    CANDLE_BEAR_COLOR = "#FF1744"
    CANDLE_BODY_ALPHA = 255
    CANDLE_WICK_ALPHA = 255

def _mk_brush(hex_color: str, alpha: int):
    col = pg.mkColor(hex_color)
    col.setAlpha(max(0, min(255, int(alpha))))
    return pg.mkBrush(col)


class CandlestickItem(pg.GraphicsObject):
    """
    Szybkie świece z LOD:
      - rysuje tylko widoczny zakres X (ViewBox),
      - gdy świec bardzo dużo -> binowanie po ciągłych przedziałach X
        (open=pierwsza, close=ostatnia, high=max(bin), low=min(bin)),
      - knot = DWA prostokąty (górny i dolny), body = prostokąt,
      - bez binowania: body ma szerokość w PIKSELACH jako część lokalnego odstępu,
        z klamrami MIN/MAX w px względem odstępu – brak “regularnych dziur” przy dużym zoomie.
    Wejście: macierz Nx6 -> [x, open, close, low, high, width]
    """

    # ===== USTAWIENIA =====
    MAX_COLUMNS = 3000            # maks. liczba kolumn po zbinowaniu

    # bez binowania (<= MAX_COLUMNS świec na ekranie)
    BODY_FRAC_OF_SPACING = 0.90
    MAX_BODY_FRAC_OF_SPACING = 0.95
    MIN_BODY_PX = 2.0
    MIN_WICK_PX = 1.0
    WICK_FRAC_OF_BODY = 0.25

    # przy binowaniu
    BIN_BODY_FRAC = 0.90
    BIN_WICK_FRAC = 0.25

    # minima w jednostkach danych (awaryjne)
    MIN_W_DATA = 1e-9
    MIN_H_DATA = 0.0

    def __init__(self, data_arr: np.ndarray):
        super().__init__()
        self._set_data(data_arr)

    def update_data(self, data_arr: np.ndarray):
        self._set_data(data_arr)
        self.prepareGeometryChange()
        self.informViewBoundsChanged()

    def _set_data(self, data_arr: np.ndarray):
        if data_arr is None or len(data_arr) == 0:
            self.x = self.o = self.c = self.l = self.h = self.w = np.array([], dtype=float)
            self._bounding = QRectF(0, 0, 0, 0)
            return

        a = np.asarray(data_arr, dtype=float)
        self.x = a[:, 0].astype(float)
        self.o = a[:, 1].astype(float)
        self.c = a[:, 2].astype(float)
        self.l = a[:, 3].astype(float)
        self.h = a[:, 4].astype(float)
        self.w = a[:, 5].astype(float)

        if self.x.size:
            x_lo, x_hi = float(np.nanmin(self.x)), float(np.nanmax(self.x))
            y_lo, y_hi = float(np.nanmin(self.l)), float(np.nanmax(self.h))
            if y_lo == y_hi:
                y_lo -= 1.0; y_hi += 1.0
            ww = float(np.nanmedian(self.w)) if self.w.size else 1.0
            self._bounding = QRectF(x_lo - ww, y_lo, (x_hi - x_lo) + 2 * ww, (y_hi - y_lo))
        else:
            self._bounding = QRectF(0, 0, 0, 0)

    @staticmethod
    def _px_to_data_x(vb, px: float) -> float:
        try:
            dx, _ = vb.viewPixelSize()
            if np.isfinite(dx) and dx > 0:
                return float(px) * float(dx)
        except Exception:
            pass
        return float(px)

    def paint(self, p: QPainter, *args):
        if self.x is None or self.x.size == 0:
            return

        try:
            p.setRenderHint(QPainter.Antialiasing, False)
        except Exception:
            pass

        vb = self.getViewBox()
        if vb is None:
            self._paint_direct(p, 0, len(self.x), None)
            return

        (x_min, x_max), _ = vb.viewRange()
        start = int(np.searchsorted(self.x, x_min, side='left'))
        stop  = int(np.searchsorted(self.x, x_max, side='right'))
        if stop <= start:
            return

        count = stop - start
        if count <= self.MAX_COLUMNS:
            self._paint_direct(p, start, stop, vb)
            return

        # ================= BINOWANIE =================
        edges = np.linspace(self.x[start], self.x[stop - 1], num=self.MAX_COLUMNS + 1, dtype=float)

        xv = self.x[start:stop]
        ov = self.o[start:stop]; cv = self.c[start:stop]
        lv = self.l[start:stop]; hv = self.h[start:stop]

        bin_id = np.searchsorted(edges, xv, side='right') - 1
        bin_id = np.clip(bin_id, 0, self.MAX_COLUMNS - 1)

        order = np.argsort(bin_id, kind='mergesort')
        bins_sorted = bin_id[order]
        idx_sorted  = np.arange(xv.size, dtype=int)[order]

        uniq, first_pos, counts = np.unique(bins_sorted, return_index=True, return_counts=True)
        last_pos = first_pos + counts - 1
        non_empty = counts > 0
        if not np.any(non_empty):
            self._paint_direct(p, start, stop, vb)
            return
        uniq = uniq[non_empty]; first_pos = first_pos[non_empty]; last_pos = last_pos[non_empty]

        first_idx = idx_sorted[first_pos]
        last_idx  = idx_sorted[last_pos]

        open_  = ov[first_idx]
        close  = cv[last_idx]

        hv_sorted = hv[order]; lv_sorted = lv[order]
        high = np.maximum.reduceat(hv_sorted, first_pos)
        low  = np.minimum.reduceat(lv_sorted, first_pos)

        left_edges  = edges[uniq]
        right_edges = edges[uniq + 1]
        centers = 0.5 * (left_edges + right_edges)
        bin_w  = (right_edges - left_edges)

        body_w = np.maximum(self.MIN_W_DATA, bin_w * float(self.BIN_BODY_FRAC))
        wick_w = np.maximum(self.MIN_W_DATA, body_w * float(self.BIN_WICK_FRAC))

        # >>> KOLORY – teraz z konfiguracji:
        bull_body = _mk_brush(CANDLE_BULL_COLOR, CANDLE_BODY_ALPHA)
        bear_body = _mk_brush(CANDLE_BEAR_COLOR, CANDLE_BODY_ALPHA)
        bull_wick = _mk_brush(CANDLE_BULL_COLOR, CANDLE_WICK_ALPHA)
        bear_wick = _mk_brush(CANDLE_BEAR_COLOR, CANDLE_WICK_ALPHA)

        p.setPen(pg.mkPen(None))

        upper_h = np.maximum(0.0, high - np.maximum(open_, close))
        lower_h = np.maximum(0.0, np.minimum(open_, close) - low)

        # górne wicki
        for xi, ww, uh, o_, c_ in zip(centers, wick_w, upper_h, open_, close):
            if uh <= 0:
                continue
            p.setBrush(bull_wick if c_ >= o_ else bear_wick)
            p.drawRect(QRectF(float(xi) - ww/2.0, float(max(o_, c_)), float(ww), float(uh)))

        # dolne wicki
        for xi, ww, lh, o_, c_ in zip(centers, wick_w, lower_h, open_, close):
            if lh <= 0:
                continue
            p.setBrush(bull_wick if c_ >= o_ else bear_wick)
            p.drawRect(QRectF(float(xi) - ww/2.0, float(min(o_, c_) - lh), float(ww), float(lh)))

        # body
        for xi, o_, c_, bw in zip(centers, open_, close, body_w):
            up = c_ >= o_
            y1, y2 = (o_, c_) if up else (c_, o_)
            hgt = float(max(self.MIN_H_DATA, y2 - y1))
            x_left = float(xi) - float(bw) / 2.0
            p.setBrush(bull_body if up else bear_body)
            p.drawRect(QRectF(x_left, float(y1), float(bw), hgt))

    def _paint_direct(self, p: QPainter, start: int, stop: int, vb):
        x = self.x[start:stop]; o = self.o[start:stop]; c = self.c[start:stop]
        l = self.l[start:stop]; h = self.h[start:stop]

        n = x.size
        if n == 0:
            return

        if n == 1:
            spacing = np.array([60.0], dtype=float)
        else:
            left  = np.r_[x[0], x[:-1]]
            right = np.r_[x[1:], x[-1]]
            spacing = np.minimum(x - left, right - x)
            spacing[0]  = max(spacing[0],  x[1]  - x[0])
            spacing[-1] = max(spacing[-1], x[-1] - x[-2])
            bad = ~(np.isfinite(spacing)) | (spacing <= 0)
            if np.any(bad):
                med = np.nanmedian(spacing[~bad]) if np.any(~bad) else 60.0
                spacing = np.where(bad, med, spacing)

        if vb is not None:
            dx, _ = vb.viewPixelSize()
            px_spacing = spacing / max(dx, 1e-12)
            px_body = np.clip(px_spacing * float(self.BODY_FRAC_OF_SPACING),
                              a_min=self.MIN_BODY_PX,
                              a_max=px_spacing * float(self.MAX_BODY_FRAC_OF_SPACING))
            px_wick = np.maximum(self.MIN_WICK_PX, px_body * float(self.WICK_FRAC_OF_BODY))
            body_w = px_body * dx
            wick_w = px_wick * dx
        else:
            body_w = np.maximum(self.MIN_W_DATA, spacing * self.BODY_FRAC_OF_SPACING)
            wick_w = np.maximum(self.MIN_W_DATA, body_w * self.WICK_FRAC_OF_BODY)

        # >>> KOLORY – teraz z konfiguracji:
        bull_body = _mk_brush(CANDLE_BULL_COLOR, CANDLE_BODY_ALPHA)
        bear_body = _mk_brush(CANDLE_BEAR_COLOR, CANDLE_BODY_ALPHA)
        bull_wick = _mk_brush(CANDLE_BULL_COLOR, CANDLE_WICK_ALPHA)
        bear_wick = _mk_brush(CANDLE_BEAR_COLOR, CANDLE_WICK_ALPHA)

        p.setPen(pg.mkPen(None))

        upper_h = np.maximum(0.0, h - np.maximum(o, c))
        lower_h = np.maximum(0.0, np.minimum(o, c) - l)

        # górne
        for xi, ww, uh, o_, c_ in zip(x, wick_w, upper_h, o, c):
            if uh <= 0:
                continue
            p.setBrush(bull_wick if c_ >= o_ else bear_wick)
            p.drawRect(QRectF(float(xi) - ww/2.0, float(max(o_, c_)), float(ww), float(uh)))

        # dolne
        for xi, ww, lh, o_, c_ in zip(x, wick_w, lower_h, o, c):
            if lh <= 0:
                continue
            p.setBrush(bull_wick if c_ >= o_ else bear_wick)
            p.drawRect(QRectF(float(xi) - ww/2.0, float(min(o_, c_) - lh), float(ww), float(lh)))

        # body
        for xi, o_, c_, bw in zip(x, o, c, body_w):
            up = c_ >= o_
            y1, y2 = (o_, c_) if up else (c_, o_)
            hgt = float(max(self.MIN_H_DATA, y2 - y1))
            x_left = float(xi) - float(bw) / 2.0
            p.setBrush(bull_body if up else bear_body)
            p.drawRect(QRectF(x_left, float(y1), float(bw), hgt))

    def boundingRect(self) -> QRectF:
        return self._bounding
