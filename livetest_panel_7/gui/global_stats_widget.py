import logging

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QTabWidget, QSplitter, QScrollArea, QFrame, QLabel,
    QSizePolicy, QSpacerItem, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView
)
import math
import numpy as np
import pandas as pd
# sqlite backend removed (PG)
from collections import defaultdict

from backtester.utils import smart_price_format

# ---------------- małe utilsy ----------------
def _safe_float(x, default=0.0):
    try:
        if x is None:
            return float(default)
        if isinstance(x, str):
            x = x.replace("%","").replace(",", "").strip()
        v = float(x)
        return v if math.isfinite(v) else float(default)
    except Exception:
        return float(default)

def _fmt_num(v, digits=6):
    try:
        return smart_price_format(float(v)) if digits is None else f"{float(v):.{digits}f}"
    except Exception:
        return "—"

def _fmt_pct(v, digits=4):
    try:
        return f"{float(v):.{digits}f}%"
    except Exception:
        return "—"

def _fmt_hms(seconds):
    try:
        s = int(max(0, round(float(seconds))))
    except Exception:
        return "—"
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def _get_first(d: dict, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

# ---------- wyliczanie metryk PnL ----------
def _infer_notional_usd(tr):
    direct = _get_first(tr, ["notional", "volume_usd", "quote_qty", "quoteQty"], 0.0)
    v = _safe_float(direct, 0.0)
    if v > 0:
        return v
    amount = _get_first(tr, ["amount","qty","size","quantity","contracts"], 0.0)
    entry  = _get_first(tr, ["entry_price","open_price","price_in","open price","buy price","in price"], 0.0)
    return abs(_safe_float(amount,0.0) * _safe_float(entry,0.0))

def _infer_fee_paid(tr, default_fee_rate=None):
    total = 0.0; any_explicit=False
    for k in ["fee","fees","commission","fee_paid","fee_open","fee_close"]:
        if k in tr and tr[k] is not None:
            total += _safe_float(tr[k],0.0); any_explicit=True
    if any_explicit:
        return total
    if default_fee_rate is None:
        try:
            from config import FEE as DEFAULT_FEE_RATE
            default_fee_rate=float(DEFAULT_FEE_RATE)
        except Exception:
            default_fee_rate=0.0
    fr = _safe_float(_get_first(tr,["fee_rate","commission_rate","taker_fee"], default_fee_rate), 0.0)
    if fr <= 0:
        return 0.0
    a  = _safe_float(_get_first(tr,["amount","qty","size","quantity","contracts"],0.0),0.0)
    ep = _safe_float(_get_first(tr,["entry_price","open_price","price_in","open price"],0.0),0.0)
    xp = _safe_float(_get_first(tr,["exit_price","close_price","price_out","close price","sell price","out price"],ep),ep)
    return fr*(abs(a*ep)+abs(a*xp))

def _infer_initial_margin(tr):
    direct = _get_first(tr,["margin","initial_margin","used_margin","im"],None)
    if direct is not None:
        return abs(_safe_float(direct,0.0))
    notional = _infer_notional_usd(tr)
    lev = _safe_float(_get_first(tr,["leverage","lev"],None), 0.0)
    return notional/lev if (notional>0 and lev>0) else 0.0

def _infer_start_balance():
    try:
        import config
        for name in (
            "INITIAL_BALANCE","STARTING_BALANCE","START_BALANCE",
            "CAPITAL","BALANCE","START_CAPITAL_USD","STARTING_CAPITAL_USD"
        ):
            if hasattr(config,name):
                return float(getattr(config,name))
    except Exception:
        pass
    return None

def _pnl_pct_from_entry(tr):
    pnl = _safe_float(tr.get("pnl"),0.0)
    base = _infer_notional_usd(tr)
    return (pnl/base*100.0) if base>0 else 0.0

def _guess_leverage_from_config():
    try:
        import config
        for name in ("LEVERAGE", "DEFAULT_LEVERAGE", "LEV"):
            if hasattr(config, name):
                v = float(getattr(config, name))
                if v > 0:
                    return v
    except Exception:
        pass
    return 0.0

def _guess_leverage_from_trades(trades):
    for tr in (trades or []):
        lv = _safe_float(_get_first(tr, ["leverage","lev"], None), 0.0)
        if lv and lv > 0:
            return lv
    return 0.0

def _compute_metrics(trades, start_balance=None):
    t = [tr for tr in (trades or []) if isinstance(tr, dict)]
    n = len(t)
    total_pnl = sum(_safe_float(tr.get("pnl"),0.0) for tr in t)
    avg_pnl   = (total_pnl/n) if n else 0.0
    best      = max((_safe_float(tr.get("pnl"),0.0) for tr in t), default=0.0)
    worst     = min((_safe_float(tr.get("pnl"),0.0) for tr in t), default=0.0)
    wins      = [tr for tr in t if _safe_float(tr.get("pnl"),0.0)>0]
    losses    = [tr for tr in t if _safe_float(tr.get("pnl"),0.0)<0]
    win_rate  = 100.0*len(wins)/n if n else 0.0

    total_vol = sum(_infer_notional_usd(tr) for tr in t)
    total_fee = sum(_infer_fee_paid(tr) for tr in t)
    avg_win_usd  = (sum(_safe_float(tr.get("pnl"),0.0) for tr in wins)/len(wins)) if wins else 0.0
    avg_loss_usd = (sum(_safe_float(tr.get("pnl"),0.0) for tr in losses)/len(losses)) if losses else 0.0
    avg_gain_pct = (np.mean([_pnl_pct_from_entry(tr) for tr in wins]) if wins else 0.0)
    avg_loss_pct = (np.mean([_pnl_pct_from_entry(tr) for tr in losses]) if losses else 0.0)
    vwatr        = (total_pnl/total_vol*100.0) if total_vol>0 else 0.0

    sb  = start_balance if start_balance is not None else _infer_start_balance()
    roc = (total_pnl/sb*100.0) if (sb and sb>0) else None

    # ROI with robust margin estimation
    used_margin = sum(_infer_initial_margin(tr) for tr in t)
    if not used_margin or used_margin <= 0:
        leverage_guess = _guess_leverage_from_trades(t)
        if leverage_guess <= 0:
            leverage_guess = _guess_leverage_from_config()
        if leverage_guess and leverage_guess > 0:
            used_margin = sum(_infer_notional_usd(tr) for tr in t) / leverage_guess
        else:
            used_margin = 0.0
    roi = (total_pnl / used_margin * 100.0) if used_margin > 0 else None

    return dict(
        n=n, win_rate=win_rate, total_pnl=total_pnl, avg_pnl=avg_pnl, best=best, worst=worst,
        total_vol=total_vol, total_fee=total_fee, avg_win_usd=avg_win_usd, avg_loss_usd=avg_loss_usd,
        avg_gain_pct=avg_gain_pct, avg_loss_pct=avg_loss_pct, vwatr=vwatr, roc=roc, roi=roi
    )

class _NumericItem(QTableWidgetItem):
    def __init__(self, text, sort_value=None):
        super().__init__(str(text))
        if sort_value is None:
            try:
                sort_value = float(str(text).replace("%","").replace("—","").strip())
            except Exception:
                sort_value = float('nan')
        self._v = sort_value
    def __lt__(self, other):
        try:
            return float(self._v) < float(getattr(other,'_v', float('nan')))
        except Exception:
            return super().__lt__(other)

# ---------------- wspólna baza: lista + tabela + splitter ----------------
class _BaseTab(QWidget):
    LIST_FONT_PX = 17
    def __init__(self, header_html):
        super().__init__()
        lay = QVBoxLayout(self); lay.setContentsMargins(4,4,4,4); lay.setSpacing(6)
        self.splitter = QSplitter(Qt.Vertical); lay.addWidget(self.splitter,1)

        # List (scroll)
        list_frame = QFrame(); list_v = QVBoxLayout(list_frame); list_v.setContentsMargins(0,0,0,0); list_v.setSpacing(0)
        self.title = QLabel(header_html); self.title.setTextFormat(Qt.RichText)
        self.title.setStyleSheet(f"font-size:{self.LIST_FONT_PX}px; font-weight:600; padding:4px;")
        list_v.addWidget(self.title, 0, Qt.AlignLeft)

        self.list_host = QWidget(); self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(8,6,8,6); self.list_layout.setSpacing(10)
        self._list_widgets=[]
        self._filler = QSpacerItem(0,0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        self.list_layout.addItem(self._filler)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.list_host)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        list_v.addWidget(self.scroll,1)
        self.splitter.addWidget(list_frame)

        # Table
        self.table = QTableWidget(0,0)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.splitter.addWidget(self.table)

        vh = self.table.verticalHeader().defaultSectionSize()
        hh = self.table.horizontalHeader().height()
        self.table.setMinimumHeight(int(hh + 5*vh + 8))
        self.splitter.setSizes([500, int(hh + 5*vh + 60)])

    def clear_list(self):
        for w in self._list_widgets:
            try:
                w.setParent(None); w.deleteLater()
            except Exception:
                pass
        self._list_widgets=[]
        try:
            idx = self.list_layout.indexOf(self._filler)
            if idx>=0: self.list_layout.removeItem(self._filler)
        except Exception:
            pass
        self.list_layout.addItem(self._filler)

    def add_row(self, html):
        lbl = QLabel(html); lbl.setTextFormat(Qt.RichText)
        lbl.setStyleSheet(f"font-size:{self.LIST_FONT_PX}px; padding:2px 6px;")
        self.list_layout.insertWidget(self.list_layout.count()-1, lbl, 0, Qt.AlignLeft)
        self._list_widgets.append(lbl)

    def add_divider(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Plain)
        line.setFixedHeight(1)
        line.setStyleSheet('background-color: rgba(255,255,255,0.12); border: none; margin: 6px 0;')
        self.list_layout.insertWidget(self.list_layout.count()-1, line)
        self._list_widgets.append(line)

# ---------------- PnL tab ----------------
class _PNLTab(_BaseTab):
    COLS = ["Symbol","Trades","Win-rate","Suma PnL","Śr. PnL","Best","Worst",
            "Wolumen (USD)","Prowizje (USD)","Śr. zysk $","Śr. strata $",
            "Śr. zysk %","Śr. strata %","VWATR %","ROC %","ROI %"]
    def __init__(self):
        super().__init__("<b>Łączne statystyki (PnL):</b>")
        self.table.setColumnCount(len(self.COLS)); self.table.setHorizontalHeaderLabels(self.COLS)

    def _metrics(self, trades):
        return _compute_metrics(trades)

    def update(self, all_trades, per_symbol_stats=None):
        self.clear_list()
        m = self._metrics(all_trades or [])
        self.add_row(f"Trades: <b>{m['n']}</b>")
        self.add_row(f"Win-rate: <b>{_fmt_pct(m['win_rate'])}</b>")
        self.add_row(f"Suma PnL: <b>{_fmt_num(m['total_pnl'])}</b>")
        self.add_divider()
        self.add_row(f"Śr. PnL: <b>{_fmt_num(m['avg_pnl'])}</b>")
        self.add_row(f"Best trade: <b>{_fmt_num(m['best'])}</b>")
        self.add_row(f"Worst trade: <b>{_fmt_num(m['worst'])}</b>")
        self.add_divider()
        self.add_row(f"Wolumen (USD): <b>{_fmt_num(m['total_vol'],2)}</b>")
        self.add_row(f"Prowizje (USD): <b>{_fmt_num(m['total_fee'],4)}</b>")
        self.add_row(f"Śr. zysk $: <b>{_fmt_num(m['avg_win_usd'],4)}</b>")
        self.add_row(f"Śr. strata $: <b>{_fmt_num(m['avg_loss_usd'],4)}</b>")
        self.add_row(f"Śr. zysk %: <b>{_fmt_pct(m['avg_gain_pct'])}</b>")
        self.add_row(f"Śr. strata %: <b>{_fmt_pct(m['avg_loss_pct'])}</b>")
        self.add_row(f"Śr. zwrot % (VWATR): <b>{_fmt_pct(m['vwatr'])}</b>")
        self.add_row(f"ROC (zwrot kapitału) %: <b>{_fmt_pct(m['roc']) if m['roc'] is not None else '—'}</b>")
        self.add_row(f"ROI %: <b>{_fmt_pct(m['roi']) if m['roi'] is not None else '—'}</b>")
        _tighten_table_columns(self.table)

        # per-symbol
        per = defaultdict(list)
        if isinstance(per_symbol_stats, dict) and per_symbol_stats:
            for sym, entry in per_symbol_stats.items():
                per[sym] = (entry.get('trades') if isinstance(entry, dict) else entry) or []
        else:
            for tr in (all_trades or []):
                per[_get_first(tr,["symbol","pair","ticker","market"],"-")].append(tr)

        rows=[]
        for sym, arr in per.items():
            s = self._metrics(arr)
            rows.append([
                sym, s['n'], f"{s['win_rate']:.2f}%", f"{s['total_pnl']:.6f}", f"{s['avg_pnl']:.6f}",
                f"{s['best']:.6f}", f"{s['worst']:.6f}",
                f"{s['total_vol']:.2f}", f"{s['total_fee']:.4f}",
                f"{s['avg_win_usd']:.4f}", f"{s['avg_loss_usd']:.4f}",
                f"{s['avg_gain_pct']:.2f}%", f"{s['avg_loss_pct']:.2f}%",
                f"{s['vwatr']:.2f}%", (f"{s['roc']:.2f}%" if s['roc'] is not None else "—"),
                (f"{s['roi']:.2f}%" if s['roi'] is not None else "—"),
            ])

        self.table.setRowCount(0)
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            for c,val in enumerate(row):
                align = Qt.AlignCenter if c!=0 else Qt.AlignLeft
                it = _NumericItem(val)
                it.setTextAlignment(align|Qt.AlignVCenter)
                self.table.setItem(r,c,it)
        self.table.resizeColumnsToContents()

# ---------------- Trades info tab ----------------
class _TradesInfoTab(_BaseTab):
    COLS = ["Symbol","Trades",
            "avg_duration","min_duration","max_duration",
            "avg_price_delta%","min_price_delta%","max_price_delta%",
            "avg_SL%","min_SL%","max_SL%",
            "avg_TP%","min_TP%","max_TP%",
            "avg_TSdist%","min_TSdist%","max_TSdist%"]
    def __init__(self):
        super().__init__("<b>Łączne statystyki (trades info):</b>")
        self.table.setColumnCount(len(self.COLS)); self.table.setHorizontalHeaderLabels(self.COLS)
        self._engine = None
        self._db = None
        self._db_path = None
        self._ind_cache = {}

    def set_engine(self, engine):
        self._engine = engine
        self._db = getattr(engine, "db", None)
        self._db_path = getattr(self._db, "db_path", None)
        self._ind_cache.clear()

    def _ind_df_for(self, symbol):
        if symbol in self._ind_cache:
            return self._ind_cache[symbol]
        table = None
        try:
            if self._engine and getattr(self._engine, "ind_tables_hist", None):
                table = self._engine.ind_tables_hist.get(symbol)
        except Exception:
            table = None

        df = None
        if self._db is not None and hasattr(self._db, "get_indicator_table") and table:
            try:
                df = self._db.get_indicator_table(table, symbol, limit=200000)
            except Exception:
                df = None

        if df is None or df.empty:
            return None
        df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
        for c in ("TP","SL","TS","TS_BENCHMARK","close_price"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df.sort_values("close_time", inplace=True)
        self._ind_cache[symbol] = df
        return df

    def _duration_sec(self, tr):
        d = _safe_float(tr.get("duration_secs"), float('nan'))
        if math.isfinite(d):
            return d
        try:
            e = pd.to_datetime(tr.get("entry_timestamp"), utc=True, errors='coerce')
            x = pd.to_datetime(tr.get("exit_timestamp"),  utc=True, errors='coerce')
            if e is not None and x is not None and not (pd.isna(e) or pd.isna(x)):
                return float((x-e).total_seconds())
        except Exception:
            pass
        return float('nan')

    def _price_delta_pct(self, tr):
        ep = _safe_float(_get_first(tr,["entry_price","open_price","price_in","open price","buy price","in price"]), float('nan'))
        xp = _safe_float(_get_first(tr,["exit_price","close_price","price_out","close price","sell price","out price"]), float('nan'))
        if not (math.isfinite(ep) and math.isfinite(xp)) or ep == 0:
            return float('nan')
        return abs(xp-ep)/abs(ep)*100.0

    def _sl_tp_pct_at_entry(self, symbol, entry_ts, entry_price):
        ep = _safe_float(entry_price, float('nan'))
        def pct(level):
            lv = _safe_float(level, float('nan'))
            return (abs(lv-ep)/abs(ep)*100.0) if (math.isfinite(ep) and ep!=0 and math.isfinite(lv)) else None

        df = self._ind_df_for(symbol)
        if df is None or entry_ts is None:
            return None, None

        ts = pd.to_datetime(entry_ts, utc=True, errors="coerce")
        try:
            i = (df["close_time"] - ts).abs().idxmin()
            row = df.loc[i]
            sl = row.get("SL", np.nan)
            tp = row.get("TP", np.nan)
        except Exception:
            sl = np.nan; tp = np.nan

        return pct(sl), pct(tp)

    def _ts_distances_pct(self, symbol, entry_ts, exit_ts):
        df = self._ind_df_for(symbol)
        if df is None or entry_ts is None or exit_ts is None:
            return []
        s = pd.to_datetime(entry_ts, utc=True, errors="coerce")
        e = pd.to_datetime(exit_ts,  utc=True, errors="coerce")
        sub = df[(df["close_time"] >= s) & (df["close_time"] <= e)].copy()
        if sub.empty or not set(["TS","TS_BENCHMARK","close_price"]).issubset(sub.columns):
            return []
        sub = sub.dropna(subset=["TS","TS_BENCHMARK","close_price"])
        if sub.empty:
            return []
        changed = sub["TS_BENCHMARK"].diff().fillna(1.0).abs() > 1e-12
        filt = sub[changed]
        if filt.empty:
            return []
        vals = (np.abs(filt["TS"] - filt["TS_BENCHMARK"]) /
                np.abs(filt["close_price"]).replace(0, np.nan) * 100.0)
        vals = vals.replace([np.inf, -np.inf], np.nan).dropna()
        return list(vals.astype(float))

    def _agg3(self, vec):
        clean = [v for v in vec if v is not None and math.isfinite(v)]
        if not clean: return (float('nan'), float('nan'), float('nan'))
        arr = np.asarray(clean, dtype=float)
        return (float(np.mean(arr)), float(np.min(arr)), float(np.max(arr)))


    def _sl_tp_pct_from_trade(self, tr):
        """Zwraca (SL_pct, TP_pct) na podstawie pól pojedynczego trejdu.

        Odległość absolutna od entry_price do sl_open / tp_open,
        wyrażona jako % ceny wejścia.
        """
        ep = _safe_float(tr.get("entry_price"), float("nan"))
        if not (math.isfinite(ep) and ep != 0.0):
            return None, None

        def pct(level):
            lv = _safe_float(level, float("nan"))
            if not math.isfinite(lv):
                return None
            return abs(lv - ep) / abs(ep) * 100.0

        sl_open = tr.get("sl_open")
        tp_open = tr.get("tp_open")
        return pct(sl_open), pct(tp_open)

    def _tsdist_pct_from_trade(self, tr):
        """Zwraca początkowy dystans TS w % benchmarku TS.

        |initial_ts - initial_benchmark| / |initial_benchmark| * 100.
        """
        bench = _safe_float(tr.get("initial_benchmark"), float("nan"))
        ts = _safe_float(tr.get("initial_ts"), float("nan"))
        if not (math.isfinite(bench) and bench != 0.0 and math.isfinite(ts)):
            return None
        return abs(ts - bench) / abs(bench) * 100.0

    def update(self, all_trades):
        self.clear_list()

        per = defaultdict(list)
        for t in (all_trades or []):
            per[_get_first(t,["symbol","pair","ticker","market"],"-")].append(t)

        d_all=[]; pd_all=[]; sl_all=[]; tp_all=[]; ts_all=[]
        for sym, arr in per.items():
            for t in arr:
                d_all.append(self._duration_sec(t))
                pd_all.append(self._price_delta_pct(t))
                slpct, tppct = self._sl_tp_pct_from_trade(t)
                if slpct is not None: sl_all.append(slpct)
                if tppct is not None: tp_all.append(tppct)
                ts_val = self._tsdist_pct_from_trade(t)
                if ts_val is not None: ts_all.append(ts_val)

        g_d, n_d, x_d   = self._agg3(d_all)
        g_pd, n_pd, x_pd= self._agg3(pd_all)
        g_sl, n_sl, x_sl= self._agg3(sl_all)
        g_tp, n_tp, x_tp= self._agg3(tp_all)
        g_ts, n_ts, x_ts= self._agg3(ts_all)

        self.add_row(f"Długość trejda (czas) — avg/min/max: <b>{_fmt_hms(g_d)}</b> / <b>{_fmt_hms(n_d)}</b> / <b>{_fmt_hms(x_d)}</b>")
        self.add_row(f"Zmiana ceny (abs, % of entry) — avg/min/max: <b>{_fmt_num(g_pd,6)}%</b> / <b>{_fmt_num(n_pd,6)}%</b> / <b>{_fmt_num(x_pd,6)}%</b>")
        self.add_row(f"SL @entry (% ceny wejścia) — avg/min/max: <b>{_fmt_pct(g_sl)}</b> / <b>{_fmt_pct(n_sl)}</b> / <b>{_fmt_pct(x_sl)}</b>")
        self.add_row(f"TP @entry (% ceny wejścia) — avg/min/max: <b>{_fmt_pct(g_tp)}</b> / <b>{_fmt_pct(n_tp)}</b> / <b>{_fmt_pct(x_tp)}</b>")
        self.add_row(f"TS distance od benchmarku (% ceny, przy uzbrojeniu TS) — avg/min/max: <b>{_fmt_pct(g_ts)}</b> / <b>{_fmt_pct(n_ts)}</b> / <b>{_fmt_pct(x_ts)}</b>")

        rows=[]
        for sym, arr in per.items():
            durs=[]; deltas=[]; sls=[]; tps=[]; tsd=[]
            for t in arr:
                durs.append(self._duration_sec(t))
                deltas.append(self._price_delta_pct(t))
                slpct, tppct = self._sl_tp_pct_from_trade(t)
                if slpct is not None: sls.append(slpct)
                if tppct is not None: tps.append(tppct)
                ts_val = self._tsdist_pct_from_trade(t)
                if ts_val is not None: tsd.append(ts_val)
            A_d, I_d, X_d   = self._agg3(durs)
            A_p, I_p, X_p   = self._agg3(deltas)
            A_sl, I_sl, X_sl= self._agg3(sls)
            A_tp, I_tp, X_tp= self._agg3(tps)
            A_ts, I_ts, X_ts= self._agg3(tsd)
            rows.append([
                sym, len(arr),
                _fmt_hms(A_d), _fmt_hms(I_d), _fmt_hms(X_d),
                f"{_fmt_num(A_p,6)}%", f"{_fmt_num(I_p,6)}%", f"{_fmt_num(X_p,6)}%",
                _fmt_pct(A_sl), _fmt_pct(I_sl), _fmt_pct(X_sl),
                _fmt_pct(A_tp), _fmt_pct(I_tp), _fmt_pct(X_tp),
                _fmt_pct(A_ts), _fmt_pct(I_ts), _fmt_pct(X_ts),
            ])

        self.table.setRowCount(0)
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            for c,val in enumerate(row):
                align = Qt.AlignCenter if c!=0 else Qt.AlignLeft
                it = _NumericItem(val)
                it.setTextAlignment(align|Qt.AlignVCenter)
                self.table.setItem(r,c,it)
        self.table.resizeColumnsToContents()
        _tighten_table_columns(self.table)

# ---------------- public wrapper ----------------

def _tighten_table_columns(table):
    """Autosize kolumn do NAJWIEKSZEJ z: szerokości nagłówka lub komórek (+mały padding)."""
    if table is None or table.model() is None:
        return

    header = table.horizontalHeader()
    hfm = header.fontMetrics()       # metryka czcionki nagłówków
    cfm = table.fontMetrics()        # metryka czcionki komórek

    model = table.model()
    cols  = model.columnCount()
    rows  = model.rowCount()

    # (opcjonalnie) pozwól Qt zrobić pierwszy strzał:
    table.resizeColumnsToContents()

    for c in range(cols):
        # Tekst nagłówka: i z QTableWidgetItem, i z modelu (fallback)
        header_item = table.horizontalHeaderItem(c)
        header_text = header_item.text() if header_item else str(model.headerData(c, Qt.Horizontal) or "")

        maxw = hfm.horizontalAdvance(header_text)  # start od szerokości nagłówka

        # Sprawdź wszystkie komórki w kolumnie
        for r in range(rows):
            it = table.item(r, c)
            if not it:
                continue
            w = cfm.horizontalAdvance(it.text())
            if w > maxw:
                maxw = w

        # Ustaw docelową szerokość z lekkim paddingiem
        table.setColumnWidth(c, max(1, maxw + 8))

    # Ręczne przeciąganie + brak rozciągania ostatniej kolumny
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(1)
    table.setWordWrap(False)
    table.setStyleSheet("QTableView::item{padding-left:1px;padding-right:1px;}")

class GlobalStatsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(4,4,4,4); lay.setSpacing(4)
        self.tabs = QTabWidget(); lay.addWidget(self.tabs,1)
        self.pnl_tab    = _PNLTab()
        self.trades_tab = _TradesInfoTab()
        self.tabs.addTab(self.pnl_tab, "PnL")
        self.tabs.addTab(self.trades_tab, "Trades info")

    def set_engine(self, engine):
        self.trades_tab.set_engine(engine)

    def update_stats(self, all_trades, per_symbol_stats=None):
        self.pnl_tab.update(all_trades or [], per_symbol_stats)
        self.trades_tab.update(all_trades or [])


# ==== AUTO-INJECT HEADERS (DB-aligned) ====
PNL_HEADERS = PNL_HEADERS if 'PNL_HEADERS' in globals() else ["Symbol","Trades","Win-rate","Suma PnL","Śr. PnL","Best","Worst","Wolumen (USD)","Prowizje (USD)","Śr. zysk $","Śr. strata $","Śr. zysk %","Śr. strata %","VWATR %","ROC %","ROI %"]
TRADES_HEADERS  = TRADES_HEADERS if 'TRADES_HEADERS' in globals() else ["Symbol","avg_duration","min_duration","max_duration","avg_price_delta%","min_price_delta%","max_price_delta%","avg_SL%","min_SL%","max_SL%","avg_TP%","min_TP%","max_TP%","avg_TSdist%","min_TSdist%","max_TSdist%"]
# ==== /AUTO-INJECT ====

try:
    GlobalStatsWidget.persist_stats_to_db = _GSW_persist_stats_to_db
    logging.info('[STATS][GSW] method bound via monkey-patch')
except Exception as _e:
    logging.error('[STATS][GSW] monkey-patch failed: %s', _e)
