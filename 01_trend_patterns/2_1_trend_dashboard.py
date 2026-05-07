# -*- coding: utf-8 -*-
"""
2_1_trend_dashboard.py — display-only dashboard.
- Startup: DO NOT run detection. Only draw existing results.
- Changing symbol/method/level: only redraws.
- "Run detection": calls detector once, then redraws.
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
log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
IN_DB = os.path.join(HERE, "data", "history_data.db")
OUT_DB = os.path.join(HERE, "data", "history_trend_detection.db")
TABLE = "candles"

# Safe import of detector (we only call it on button click)
spec = importlib.util.spec_from_file_location("trend_detection", os.path.join(HERE, "1_1_trend_detector.py"))
trend_detection = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = trend_detection
spec.loader.exec_module(trend_detection)

def get_symbols(db_path: str, table: str = TABLE):
    try:
        with sqlite3.connect(db_path) as con:
            df = pd.read_sql_query(f"SELECT DISTINCT symbol FROM {table}", con)
        return sorted(df["symbol"].astype(str).tolist()) if not df.empty else []
    except Exception as e:
        log.error("get_symbols: %s", e)
        return []

def read_latest_closes(db_path: str, symbol: str, limit: int = 5000, table: str = TABLE):
    try:
        with sqlite3.connect(db_path) as con:
            df = pd.read_sql_query(f"SELECT symbol, close_time, close FROM {table} WHERE symbol=? ORDER BY close_time DESC LIMIT ?",
                                con, params=[symbol, int(limit)])
        if df.empty: return df
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close","close_time"]).sort_values("close_time").reset_index(drop=True)
        df["idx"] = df.index.values
        return df
    except Exception as e:
        log.error("read_latest_closes: %s", e)
        return pd.DataFrame(columns=["symbol","close_time","close","idx"])

def read_trend_points(out_db: str, symbol: str, method: str, level: str):
    try:
        with sqlite3.connect(out_db) as con:
            q = ("SELECT t.seg_index, t.status, t.confirmed, t.start_ts, t.end_ts, p.ts, p.price, p.side "
                "FROM trends t JOIN trend_points p ON p.trend_id = t.id "
                "WHERE t.symbol=? AND t.method=? AND t.level=? ORDER BY t.seg_index, p.ts")
            df = pd.read_sql_query(q, con, params=[symbol, method, level])
        return df
    except Exception as e:
        log.error("read_trend_points: %s", e)
        return pd.DataFrame(columns=["seg_index","status","confirmed","start_ts","end_ts","ts","price","side"])

def side_style(side: str):
    phase = "post" if side.startswith("post_") else ("pre" if side.startswith("pre_") else "legacy")
    base = side.replace("pre_", "").replace("post_", "")
    if phase == "pre":  return base, {"ls": "--", "lw": 1.0, "alpha": 0.9}
    if phase == "post": return base, {"ls": "-",  "lw": 1.6, "alpha": 1.0}
    return base, {"ls": "-", "lw": 1.0, "alpha": 0.9}

def _level_keys():
    # Detector now exposes ESTABLISH_LEN instead of L_MIN.
    levels = None
    if hasattr(trend_detection, "ESTABLISH_LEN"):
        try:
            levels = list(getattr(trend_detection, "ESTABLISH_LEN").keys())
        except Exception:
            levels = None
    if not levels:
        levels = ["short","mid","long"]
    return levels

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Trend Dashboard (DISPLAY-ONLY)")
        self.geometry("1220x780")

        self.symbols = get_symbols(IN_DB, TABLE) or ["(no data)"]

        top = ttk.Frame(self); top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        ttk.Label(top, text="Symbol").pack(side=tk.LEFT)
        self.cmb_symbol = ttk.Combobox(top, values=self.symbols, width=18, state="readonly")
        self.cmb_symbol.pack(side=tk.LEFT, padx=6); self.cmb_symbol.set(self.symbols[0])
        self.cmb_symbol.bind('<<ComboboxSelected>>', lambda e: self.draw_plot())

        ttk.Label(top, text="Level").pack(side=tk.LEFT, padx=(18,2))
        levels = _level_keys()
        self.cmb_level = ttk.Combobox(top, values=levels, width=8, state="readonly")
        default_level = "mid" if "mid" in levels else levels[0]
        self.cmb_level.set(default_level); self.cmb_level.pack(side=tk.LEFT, padx=6)
        self.cmb_level.bind('<<ComboboxSelected>>', lambda e: self.draw_plot())

        ttk.Label(top, text="Method").pack(side=tk.LEFT, padx=(18,2))
        methods = list(getattr(trend_detection, "METHODS", {"analytic": "default"}).keys())
        self.cmb_method = ttk.Combobox(top, values=methods, width=12, state="readonly")
        default_method = getattr(trend_detection, "DEFAULT_METHOD", methods[0])
        self.cmb_method.set(default_method); self.cmb_method.pack(side=tk.LEFT, padx=6)
        self.cmb_method.bind('<<ComboboxSelected>>', lambda e: self.draw_plot())

        ttk.Label(top, text="Limit").pack(side=tk.LEFT, padx=(18,2))
        self.ent_limit = ttk.Entry(top, width=8); self.ent_limit.insert(0, "5000"); self.ent_limit.pack(side=tk.LEFT, padx=6)

        ttk.Button(top, text="Refresh chart", command=self.draw_plot).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Run detection", command=self.run_detection).pack(side=tk.LEFT, padx=10)

        fig = Figure(figsize=(10,5), dpi=100)
        self.ax = fig.add_subplot(111); self.ax.grid(True, alpha=0.3)
        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.canvas_widget = self.canvas.get_tk_widget(); self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, self); toolbar.update()

        # Startup: DISPLAY ONLY
        self.draw_plot()

    def _get_params(self):
        symbol = self.cmb_symbol.get()
        level  = self.cmb_level.get()
        method = self.cmb_method.get()
        try:
            limit = max(100, int(self.ent_limit.get()))
        except:
            limit = 5000
        return symbol, level, method, limit

    def run_detection(self):
        symbol, level, method, limit = self._get_params()
        log.info("GUI: run_detection clicked -> symbol=%s level=%s method=%s limit=%s", symbol, level, method, limit)
        # Trigger detector once; then redraw plot
        trend_detection.run_once(IN_DB, OUT_DB, level, method,
                                 getattr(trend_detection, "OUTER_LOWER", 0.05),
                                 getattr(trend_detection, "OUTER_UPPER", 0.95),
                                 getattr(trend_detection, "INNER_LOWER", 0.25),
                                 getattr(trend_detection, "INNER_UPPER", 0.75),
                                 symbols=symbol, limit=limit, table=TABLE)
        self.draw_plot()

    def draw_plot(self):
        symbol, level, method, limit = self._get_params()
        log.info("GUI: draw_plot symbol=%s level=%s method=%s limit=%s", symbol, level, method, limit)
        self.ax.clear(); self.ax.grid(True, alpha=0.3)

        if symbol == "(no data)":
            self.ax.set_title("No data"); self.canvas.draw(); return

        dfp = read_latest_closes(IN_DB, symbol, limit=limit, table=TABLE)
        if dfp.empty or len(dfp) < 2:
            self.ax.set_title(f"{symbol} (no price data)"); self.canvas.draw(); return

        x = dfp["idx"].to_numpy(); y = dfp["close"].to_numpy()
        self.ax.plot(x, y, linewidth=1.2, label="close")
        tmin = str(dfp["close_time"].iloc[0]); tmax = str(dfp["close_time"].iloc[-1])

        dft = read_trend_points(OUT_DB, symbol, method, level)
        log.info("GUI: trend_points rows=%s", 0 if dft is None else len(dft))
        if dft is None or dft.empty:
            self.ax.set_title(f"{symbol} | {method} | {level} (no trends — run detection)")
            self.canvas.draw(); return

        dft = dft[(dft["ts"] >= tmin) & (dft["ts"] <= tmax)]
        if dft.empty:
            self.ax.set_title(f"{symbol} | {method} | {level} (no trend points in window)")
            self.canvas.draw(); return

        mapdf = dfp[["close_time","idx"]].copy()
        dft = dft.merge(mapdf, left_on="ts", right_on="close_time", how="left").dropna(subset=["idx"])

        for (seg, side), grp in dft.groupby(["seg_index","side"]):
            base, style = side_style(side)
            tt = grp["idx"].to_numpy(); yy = grp["price"].to_numpy()
            order = np.argsort(tt)
            label = None
            if seg == 0:
                phase = "post" if side.startswith("post_") else "pre"
                label = f"{base} ({phase})"
            self.ax.plot(tt[order], yy[order], linestyle=style["ls"], linewidth=style["lw"], alpha=style["alpha"], label=label)

        self.ax.set_title(f"{symbol} | {method} | {level}")
        h, l = self.ax.get_legend_handles_labels()
        if h: self.ax.legend(loc="upper left")
        self.canvas.draw(); log.info("GUI: drawn")

if __name__ == "__main__":
    App().mainloop()
