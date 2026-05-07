# -*- coding: utf-8 -*-
"""
2_2_supp_resist_dashboard_live.py — Tk dashboard for LIVE Support/Resistance.

- Price source: data/realtime_data.db (table 'candles')
- Levels source: data/realtime_suppres_detection.db (tables 'sr_levels', 'sr_points')
- "Run detection" imports and calls 1_2_supp_resist_live.py once.
- Optional auto refresh of the chart (does NOT run detection automatically).
"""
import os, sqlite3, importlib.util, sys, logging, threading, time
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
log = logging.getLogger("sr-dashboard")

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DB  = os.path.join(HERE, "data", "realtime_data.db")
OUT_DB = os.path.join(HERE, "data", "realtime_suppres_detection.db")
TABLE  = "candles"

# Safe import of detector for button-triggered runs
spec = importlib.util.spec_from_file_location("supp_resist_live", os.path.join(HERE, "1_2_supp_resist_live.py"))
supp_resist = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = supp_resist
spec.loader.exec_module(supp_resist)

# Ensure output tables exist
try:
    supp_resist.ensure_output_db(OUT_DB)
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

def read_sr_points(out_db: str, symbol: str, method: str, lookback: int):
    try:
        with sqlite3.connect(out_db) as con:
            q = ( "SELECT l.id as level_id, l.kind, l.price, p.ts, p.price as y, p.side "
                  "FROM sr_levels l JOIN sr_points p ON p.level_id=l.id "
                  "WHERE l.symbol=? AND l.method=? AND l.lookback=? ORDER BY l.price, p.ts" )
            df = pd.read_sql_query(q, con, params=[symbol, method, int(lookback)])
        return df
    except Exception as e:
        msg = str(e).lower()
        if "no such table" in msg:
            try:
                supp_resist.ensure_output_db(out_db)
                log.info("read_sr_points: created missing tables in %s", out_db)
            except Exception as ee:
                log.error("read_sr_points: ensure_output_db failed: %s", ee)
            return pd.DataFrame(columns=["level_id","kind","price","ts","y","side"])
        log.error("read_sr_points: %s", e)
        return pd.DataFrame(columns=["level_id","kind","price","ts","y","side"])

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Support/Resistance Dashboard LIVE")
        self.geometry("1240x860")

        self.symbols = get_symbols(IN_DB, TABLE) or ["(no data)"]

        top = ttk.Frame(self); top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        ttk.Label(top, text="Symbol").pack(side=tk.LEFT)
        self.cmb_symbol = ttk.Combobox(top, values=self.symbols, width=18, state="readonly")
        self.cmb_symbol.pack(side=tk.LEFT, padx=6); self.cmb_symbol.set(self.symbols[0])
        self.cmb_symbol.bind('<<ComboboxSelected>>', lambda e: self.draw_plot())

        ttk.Label(top, text="Method").pack(side=tk.LEFT, padx=(18,2))
        methods = list(getattr(supp_resist, "METHODS", {"touch":"default"}).keys())
        self.cmb_method = ttk.Combobox(top, values=methods, width=12, state="readonly")
        default_method = getattr(supp_resist, "DEFAULT_METHOD", methods[0])
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
        self.ent_limit = ttk.Entry(top, width=8); self.ent_limit.insert(0, "3000"); self.ent_limit.pack(side=tk.LEFT, padx=6)

        ttk.Button(top, text="Refresh chart", command=self.draw_plot).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Run detection", command=self.run_detection).pack(side=tk.LEFT, padx=10)

        # Auto refresh controls (chart only)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Auto refresh chart", variable=self.auto, command=self._toggle_auto).pack(side=tk.LEFT, padx=10)
        ttk.Label(top, text="every (s)").pack(side=tk.LEFT)
        self.ent_autoint = ttk.Entry(top, width=6); self.ent_autoint.insert(0, "10"); self.ent_autoint.pack(side=tk.LEFT, padx=4)

        fig = Figure(figsize=(10,5), dpi=100)
        self.ax = fig.add_subplot(111); self.ax.grid(True, alpha=0.3)
        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.canvas_widget = self.canvas.get_tk_widget(); self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, self); toolbar.update()

        self._auto_thread = None
        self._auto_stop   = threading.Event()

        self.draw_plot()

    def _toggle_auto(self):
        if self.auto.get():
            try:
                interval = max(2, int(self.ent_autoint.get()))
            except:
                interval = 10
            self._start_auto(interval)
        else:
            self._stop_auto()

    def _start_auto(self, interval: int):
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._auto_stop.clear()
        def runner():
            while not self._auto_stop.is_set():
                try: self.draw_plot()
                except Exception as e: log.error("auto draw failed: %s", e)
                t = interval
                for _ in range(t):
                    if self._auto_stop.is_set(): break
                    time.sleep(1)
        self._auto_thread = threading.Thread(target=runner, daemon=True)
        self._auto_thread.start()

    def _stop_auto(self):
        self._auto_stop.set()

    def _get_params(self):
        symbol = self.cmb_symbol.get()
        method = self.cmb_method.get()
        try:
            lookback = max(100, int(self.ent_lookback.get()))
        except:
            lookback = 3000
        try:
            N = max(1, int(self.ent_N.get()))
        except:
            N = 500
        try:
            K = max(1, int(self.ent_K.get()))
        except:
            K = 10
        try:
            R_pct = float(self.ent_R.get()) / 100.0  # GUI accepts percent; detector expects fraction
        except:
            R_pct = 0.005
        try:
            limit = max(100, int(self.ent_limit.get()))
        except:
            limit = 3000
        return symbol, method, lookback, N, K, R_pct, limit

    def run_detection(self):
        symbol, method, lookback, N, K, R_frac, _ = self._get_params()
        log.info("GUI: run_detection -> %s method=%s I=%s N=%s K=%s R=%.5f", symbol, method, lookback, N, K, R_frac)
        try:
            supp_resist.ensure_output_db(OUT_DB)
        except Exception as e:
            log.error("ensure_output_db failed before run: %s", e)
        supp_resist.run_once(IN_DB, OUT_DB, method, lookback, N, K, R_frac, symbols=symbol, table=TABLE)
        self.draw_plot()

    def draw_plot(self):
        symbol, method, lookback, N, K, R_frac, limit = self._get_params()
        self.ax.clear(); self.ax.grid(True, alpha=0.3)
        if symbol == "(no data)":
            self.ax.set_title("No data"); self.canvas.draw(); return

        dfp = read_latest_ohlc(IN_DB, symbol, limit=limit, table=TABLE)
        if dfp.empty or len(dfp) < 2:
            self.ax.set_title(f"{symbol} (no price data)"); self.canvas.draw(); return

        x = dfp["idx"].to_numpy(); y = dfp["close"].to_numpy()
        self.ax.plot(x, y, linewidth=1.2, label="close")
        tmin = str(dfp["close_time"].iloc[0]); tmax = str(dfp["close_time"].iloc[-1])

        dfl = read_sr_points(OUT_DB, symbol, method, lookback)
        if dfl is None or dfl.empty:
            self.ax.set_title(f"{symbol} | {method} | I={lookback} (no levels — run detection)")
            self.canvas.draw(); return

        dfl = dfl[(dfl["ts"] >= tmin) & (dfl["ts"] <= tmax)]
        if dfl.empty:
            self.ax.set_title(f"{symbol} | {method} | I={lookback} (no level points in window)")
            self.canvas.draw(); return

        mapdf = dfp[["close_time","idx"]].copy()
        dfl = dfl.merge(mapdf, left_on="ts", right_on="close_time", how="left").dropna(subset=["idx"])

        # Plot all levels: center solid, zone edges dashed
        for (lid, side), grp in dfl.groupby(["level_id","side"]):
            ls = "-" if side=="center" else "--"
            lw = 1.8 if side=="center" else 1.0
            tt = grp["idx"].to_numpy(); yy = grp["y"].to_numpy()
            order = np.argsort(tt)
            label = None
            if side=="center":
                kind = grp["kind"].iloc[0] if "kind" in grp.columns else ""
                p    = grp["price"].iloc[0] if "price" in grp.columns else yy[0]
                label = f"{kind} @ {p:,.4f}"
            self.ax.plot(tt[order], yy[order], linestyle=ls, linewidth=lw, alpha=1.0, label=label)

        self.ax.set_title(f"{symbol} | {method} | I={lookback}  (N={N}, K={K}, R={R_frac*100:.2f}%)")
        h, l = self.ax.get_legend_handles_labels()
        if h: self.ax.legend(loc="upper left", ncol=2, fontsize=8)
        self.canvas.draw(); log.info("GUI: drawn")

if __name__ == "__main__":
    App().mainloop()
