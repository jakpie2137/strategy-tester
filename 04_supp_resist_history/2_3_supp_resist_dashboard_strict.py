# -*- coding: utf-8 -*-
"""
2_3_supp_resist_dashboard_strict.py — HISTORY dashboard for STRICT rolling S/R tracks.
- Always uses rolling tracks from sr_tracks + sr_track_points (no look-ahead).
- Provides controls for lookback, stride, etc., and a Run button to recompute tracks.
"""
import os, sqlite3, importlib.util, sys, logging
import numpy as np, pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

_LOG_LEVEL = os.environ.get("TP_LOG", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("sr-hist-dashboard-strict")

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DB  = os.path.join(HERE, "data", "history_data.db")
OUT_DB = os.path.join(HERE, "data", "history_suppres_detection.db")
TABLE  = "candles"

spec = importlib.util.spec_from_file_location("supp_resist_history_strict", os.path.join(HERE, "1_3_supp_resist_history_strict.py"))
strict_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = strict_mod
spec.loader.exec_module(strict_mod)

try:
    OUT_DB = strict_mod.ensure_output_db(OUT_DB)
except Exception as e:
    log.warning("ensure_output_db on startup failed: %s", e)

def get_symbols(db_path: str, table: str = TABLE):
    try:
        with sqlite3.connect(db_path) as con:
            df = pd.read_sql_query(f"SELECT DISTINCT symbol FROM {table}", con)
        return sorted(df["symbol"].astype(str).tolist()) if not df.empty else []
    except Exception as e:
        log.error("get_symbols: %s", e)
        return []

def read_latest_ohlc(db_path: str, symbol: str, limit: int = 5000, table: str = TABLE):
    try:
        with sqlite3.connect(db_path) as con:
            df = pd.read_sql_query(
                f"SELECT symbol, close_time, open, high, low, close FROM {table} WHERE symbol=? ORDER BY close_time DESC LIMIT ?",
                con, params=[symbol, int(limit)])
        if df.empty: return df
        df = df.dropna().sort_values("close_time").reset_index(drop=True)
        df["idx"] = df.index.values
        return df
    except Exception as e:
        log.error("read_latest_ohlc: %s", e)
        return pd.DataFrame(columns=["symbol","close_time","open","high","low","close","idx"])

def read_tracks(out_db: str, symbol: str, method: str, lookback: int):
    try:
        with sqlite3.connect(out_db) as con:
            q = ( "SELECT t.id as track_id, p.ts, p.center, p.low, p.high "
                  "FROM sr_tracks t JOIN sr_track_points p ON p.track_id=t.id "
                  "WHERE t.symbol=? AND t.method=? AND t.lookback=? ORDER BY t.id, p.ts" )
            df = pd.read_sql_query(q, con, params=[symbol, method, int(lookback)])
        return df
    except Exception as e:
        log.error("read_tracks: %s", e)
        return pd.DataFrame(columns=["track_id","ts","center","low","high"])

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Support/Resistance Dashboard (HISTORY, STRICT)")
        self.geometry("1240x860")

        self.symbols = get_symbols(IN_DB, TABLE) or ["(no data)"]

        top = ttk.Frame(self); top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        ttk.Label(top, text="Symbol").pack(side=tk.LEFT)
        self.cmb_symbol = ttk.Combobox(top, values=self.symbols, width=18, state="readonly")
        self.cmb_symbol.pack(side=tk.LEFT, padx=6); self.cmb_symbol.set(self.symbols[0])
        self.cmb_symbol.bind('<<ComboboxSelected>>', lambda e: self.draw_plot())

        ttk.Label(top, text="Method").pack(side=tk.LEFT, padx=(18,2))
        methods = list(getattr(strict_mod, "METHODS", {"wick_body":"default"}).keys())
        self.cmb_method = ttk.Combobox(top, values=methods, width=12, state="readonly")
        default_method = getattr(strict_mod, "DEFAULT_METHOD", methods[0])
        self.cmb_method.set(default_method); self.cmb_method.pack(side=tk.LEFT, padx=6)
        self.cmb_method.bind('<<ComboboxSelected>>', lambda e: self.draw_plot())

        ttk.Label(top, text="Lookback (I)").pack(side=tk.LEFT, padx=(18,2))
        self.ent_lookback = ttk.Entry(top, width=8); self.ent_lookback.insert(0, "3000"); self.ent_lookback.pack(side=tk.LEFT, padx=6)

        ttk.Label(top, text="N (candles/level)").pack(side=tk.LEFT, padx=(18,2))
        self.ent_N = ttk.Entry(top, width=8); self.ent_N.insert(0, "500"); self.ent_N.pack(side=tk.LEFT, padx=6)

        ttk.Label(top, text="K (levels cap)").pack(side=tk.LEFT, padx=(18,2))
        self.ent_K = ttk.Entry(top, width=6); self.ent_K.insert(0, "10"); self.ent_K.pack(side=tk.LEFT, padx=6)

        ttk.Label(top, text="R (min sep, %)").pack(side=tk.LEFT, padx=(18,2))
        self.ent_R = ttk.Entry(top, width=8); self.ent_R.insert(0, "0.5"); self.ent_R.pack(side=tk.LEFT, padx=6)

        ttk.Label(top, text="Price limit").pack(side=tk.LEFT, padx=(18,2))
        self.ent_limit = ttk.Entry(top, width=8); self.ent_limit.insert(0, "15000"); self.ent_limit.pack(side=tk.LEFT, padx=6)

        ttk.Label(top, text="Stride").pack(side=tk.LEFT, padx=(18,2))
        self.ent_stride = ttk.Entry(top, width=6); self.ent_stride.insert(0, "30"); self.ent_stride.pack(side=tk.LEFT, padx=6)

        self.var_include_curr = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Include current bar", variable=self.var_include_curr).pack(side=tk.LEFT, padx=10)

        ttk.Button(top, text="Refresh chart", command=self.draw_plot).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Run STRICT detection", command=self.run_detection).pack(side=tk.LEFT, padx=10)

        fig = Figure(figsize=(10,5), dpi=100)
        self.ax = fig.add_subplot(111); self.ax.grid(True, alpha=0.3)
        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.canvas_widget = self.canvas.get_tk_widget(); self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, self); toolbar.update()

        try:
            self.out_db = strict_mod.ensure_output_db(OUT_DB)
        except Exception:
            self.out_db = OUT_DB

        self.draw_plot()

    def _get_params(self):
        symbol = self.cmb_symbol.get()
        method = self.cmb_method.get()
        try: lookback = max(100, int(self.ent_lookback.get()))
        except: lookback = 3000
        try: N = max(1, int(self.ent_N.get()))
        except: N = 500
        try: K = max(1, int(self.ent_K.get()))
        except: K = 10
        try: R_pct = float(self.ent_R.get())
        except: R_pct = 0.5
        try: limit = max(100, int(self.ent_limit.get()))
        except: limit = 15000
        try: stride = max(1, int(self.ent_stride.get()))
        except: stride = 1
        include_current = 1 if self.var_include_curr.get() else 0
        return symbol, method, lookback, N, K, R_pct, limit, stride, include_current

    def run_detection(self):
        symbol, method, lookback, N, K, R_pct, limit, stride, include_current = self._get_params()
        strict_mod.run_once(IN_DB, self.out_db, method, lookback, N, K, R_pct,
                            symbols=symbol, limit=limit, stride=stride, include_current=include_current, table=TABLE)
        self.draw_plot()

    def draw_plot(self):
        symbol, method, lookback, N, K, R_pct, limit, stride, include_current = self._get_params()
        self.ax.clear(); self.ax.grid(True, alpha=0.3)
        if symbol == "(no data)":
            self.ax.set_title("No data"); self.canvas.draw(); return

        dfp = read_latest_ohlc(IN_DB, symbol, limit=limit, table=TABLE)
        if dfp.empty or len(dfp) < 2:
            self.ax.set_title(f"{symbol} (no price data)"); self.canvas.draw(); return

        x = dfp["idx"].to_numpy(); y = dfp["close"].to_numpy()
        self.ax.plot(x, y, linewidth=1.2, label="close")
        tmin = str(dfp["close_time"].iloc[0]); tmax = str(dfp["close_time"].iloc[-1])

        dft = read_tracks(self.out_db, symbol, method, lookback)
        if dft is None or dft.empty:
            self.ax.set_title(f"{symbol} | {method} | I={lookback} (no tracks — run STRICT detection)")
            self.canvas.draw(); return

        mapdf = dfp[["close_time","idx"]].copy()
        dft = dft[(dft["ts"] >= tmin) & (dft["ts"] <= tmax)]
        if dft.empty:
            self.ax.set_title(f"{symbol} | {method} | I={lookback} (no track points in window)")
            self.canvas.draw(); return
        dft = dft.merge(mapdf, left_on="ts", right_on="close_time", how="left").dropna(subset=["idx"])

        for tid, grp in dft.groupby("track_id"):
            grp = grp.sort_values("idx")
            self.ax.plot(grp["idx"], grp["center"], linestyle='-', linewidth=1.8, alpha=1.0)
            self.ax.plot(grp["idx"], grp["low"], linestyle='--', linewidth=1.0, alpha=1.0)
            self.ax.plot(grp["idx"], grp["high"], linestyle='--', linewidth=1.0, alpha=1.0)

        self.ax.set_title(f"{symbol} | {method} | I={lookback}  (N={N}, K={K}, R={R_pct:.2f}%, stride={stride})")
        self.canvas.draw()

if __name__ == "__main__":
    App().mainloop()
