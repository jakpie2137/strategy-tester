# -*- coding: utf-8 -*-
"""
1_2_supp_resist_live.py — LIVE detector (only 'analytic' method).

- Reads:  data/realtime_data.db (table 'candles')
- Writes: data/realtime_trend_detection.db (tables 'trends', 'trend_points')
- Same no-lookahead live-like logic as history.
- Optional --loop SEC to re-run periodically.
"""
from __future__ import annotations
import os, sys, sqlite3, argparse, time, logging
from typing import Optional, Dict, List
import numpy as np, pandas as pd

_LOG_LEVEL = os.environ.get("TP_LOG", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("trend-live")

# Tuning
BREAK_CONFIRM_RUNS = 3
BREAK_RUN_OUT = int(os.environ.get("TP_BREAK_RUN_OUT", BREAK_CONFIRM_RUNS))
READ_LIMIT    = int(os.environ.get("TP_READ_LIMIT", 5000))

DEFAULT_METHOD = "analytic"
METHODS = {
    "analytic": "OLS(linear) + quantile residual bands",
}

ESTABLISH_LEN = {"short": 45, "mid": 180, "long": 720}
MIN_SEG_LEN   = {"short": 45, "mid": 180, "long": 720}

OUTER_LOWER = float(os.environ.get("TP_OUTER_LO", "0.05"))
OUTER_UPPER = float(os.environ.get("TP_OUTER_HI", "0.95"))
INNER_LOWER = float(os.environ.get("TP_INNER_LO", "0.25"))
INNER_UPPER = float(os.environ.get("TP_INNER_HI", "0.75"))

def ensure_output_db(out_db: str):
    os.makedirs(os.path.dirname(out_db) or ".", exist_ok=True)
    with sqlite3.connect(out_db) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS trends(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, seg_index INTEGER NOT NULL, method TEXT NOT NULL, level TEXT NOT NULL,
            status TEXT NOT NULL, confirmed INTEGER NOT NULL,
            start_ts TEXT NOT NULL, end_ts TEXT NOT NULL,
            n INTEGER NOT NULL,
            slope REAL NOT NULL, intercept REAL NOT NULL,
            lower_slope REAL NOT NULL, lower_intercept REAL NOT NULL,
            upper_slope REAL NOT NULL, upper_intercept REAL NOT NULL,
            inner_lower_slope REAL NOT NULL, inner_lower_intercept REAL NOT NULL,
            inner_upper_slope REAL NOT NULL, inner_upper_intercept REAL NOT NULL,
            band_low_q REAL NOT NULL, band_high_q REAL NOT NULL,
            inner_band_low_q REAL NOT NULL, inner_band_high_q REAL NOT NULL,
            trend_type TEXT NOT NULL, r2 REAL,
            UNIQUE(symbol, seg_index, method, level)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS trend_points(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trend_id INTEGER NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL, side TEXT NOT NULL,
            FOREIGN KEY(trend_id) REFERENCES trends(id)
        )""")
        con.commit()

def wipe_symbol(con: sqlite3.Connection, symbol: str, method: str, level: str):
    cur = con.cursor()
    cur.execute("SELECT id FROM trends WHERE symbol=? AND method=? AND level=?", (symbol, method, level))
    ids = [r[0] for r in cur.fetchall()]
    if ids:
        cur.execute("DELETE FROM trend_points WHERE trend_id IN (" + ",".join(["?"]*len(ids)) + ")", ids)
        cur.execute("DELETE FROM trends WHERE id IN (" + ",".join(["?"]*len(ids)) + ")", ids)
        con.commit()

def db_counts(con: sqlite3.Connection, symbol: Optional[str]=None, method: Optional[str]=None, level: Optional[str]=None):
    cur = con.cursor()
    if symbol and method and level:
        cur.execute("SELECT COUNT(*) FROM trends WHERE symbol=? AND method=? AND level=?", (symbol, method, level))
        t = cur.fetchone()[0]
        cur.execute("""SELECT COUNT(*)
                       FROM trend_points tp JOIN trends t ON tp.trend_id=t.id
                       WHERE t.symbol=? AND t.method=? AND t.level=?""", (symbol, method, level))
        p = cur.fetchone()[0]
        return t, p
    cur.execute("SELECT COUNT(*) FROM trends"); t = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM trend_points"); p = cur.fetchone()[0]
    return t, p

def load_symbols(in_db: str, symbols_csv: Optional[str], table: str) -> List[str]:
    if symbols_csv and symbols_csv.strip():
        syms = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
        return syms
    with sqlite3.connect(in_db) as con:
        df = pd.read_sql_query(f"SELECT DISTINCT symbol FROM {table}", con)
    return sorted(df["symbol"].astype(str).tolist()) if not df.empty else []

def read_candles(in_db: str, symbol: str, limit: int, table: str) -> pd.DataFrame:
    with sqlite3.connect(in_db) as con:
        df = pd.read_sql_query(
            f"SELECT symbol, close_time, close FROM {table} WHERE symbol=? ORDER BY close_time DESC LIMIT ?",
            con, params=[symbol, int(limit)]
        )
    if df.empty:
        return df
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close","close_time"]).sort_values("close_time").reset_index(drop=True)
    df["idx"] = df.index.values
    return df

def _fit_line(x: np.ndarray, y: np.ndarray):
    if len(x) < 2: return 0.0, float(y[-1] if len(y) else 0.0), 0.0
    A = np.vstack([x, np.ones_like(x)]).T
    a, b = np.linalg.lstsq(A, y, rcond=None)[0]
    yhat = a*x + b
    ss_res = float(np.sum((y - yhat)**2))
    ss_tot = float(np.sum((y - np.mean(y))**2)) + 1e-12
    return float(a), float(b), float(1.0 - ss_res/ss_tot)

def _bands_from_resid(slope: float, intercept: float, x: np.ndarray, y: np.ndarray,
                      qlow_outer: float, qhigh_outer: float, qlow_inner: float, qhigh_inner: float):
    yhat = slope*x + intercept
    resid = y - yhat
    resid = np.nan_to_num(resid, nan=0.0, posinf=0.0, neginf=0.0)
    lo_o = float(np.nanpercentile(resid, qlow_outer*100))
    hi_o = float(np.nanpercentile(resid, qhigh_outer*100))
    lo_i = float(np.nanpercentile(resid, qlow_inner*100))
    hi_i = float(np.nanpercentile(resid, qhigh_inner*100))
    return lo_o, hi_o, lo_i, hi_i

def build_channel(method: str, x: np.ndarray, y: np.ndarray,
                  qlow_outer: float, qhigh_outer: float,
                  qlow_inner: float, qhigh_inner: float) -> Dict:
    slope, intercept, r2 = _fit_line(x, y)
    lo_o, hi_o, lo_i, hi_i = _bands_from_resid(slope, intercept, x, y, qlow_outer, qhigh_outer, qlow_inner, qhigh_inner)
    return dict(
        slope=slope, intercept=intercept, r2=r2,
        lower_slope=slope, lower_intercept=intercept + lo_o,
        upper_slope=slope, upper_intercept=intercept + hi_o,
        inner_lower_slope=slope, inner_lower_intercept=intercept + lo_i,
        inner_upper_slope=slope, inner_upper_intercept=intercept + hi_i,
        lo_o=lo_o, hi_o=hi_o
    )

def classify_trend(slope: float, eps: float) -> str:
    if slope > eps: return "up"
    if slope < -eps: return "down"
    return "flat"

def breakout_detected(y: np.ndarray, x: np.ndarray, ch: Dict) -> bool:
    yhat = ch["slope"]*x + ch["intercept"]
    r = float((y - yhat)[-1])
    return (r < ch["lo_o"]) or (r > ch["hi_o"])

def upsert_trend(con: sqlite3.Connection, row: dict) -> int:
    cur = con.cursor()
    cur.execute("""SELECT id FROM trends WHERE symbol=? AND seg_index=? AND method=? AND level=?""",
                (row["symbol"], row["seg_index"], row["method"], row["level"]))
    r = cur.fetchone()
    fields = ("start_ts","end_ts","n","slope","intercept",
              "lower_slope","lower_intercept","upper_slope","upper_intercept",
              "inner_lower_slope","inner_lower_intercept","inner_upper_slope","inner_upper_intercept",
              "band_low_q","band_high_q","inner_band_low_q","inner_band_high_q",
              "trend_type","r2","status","confirmed")
    if r:
        tid = int(r[0])
        cur.execute(f"UPDATE trends SET {','.join([k+'=?' for k in fields])} WHERE id=?",
                    [row.get(k) for k in fields] + [tid])
    else:
        allf = ("symbol","seg_index","method","level") + fields
        cur.execute(f"INSERT INTO trends({','.join(allf)}) VALUES({','.join(['?']*len(allf))})",
                    [row.get(k) for k in allf])
        tid = cur.lastrowid
    con.commit()
    return int(tid)

def append_point(con: sqlite3.Connection, trend_id: int, ts: str,
                 center: float, lo_i: float, hi_i: float, lo_o: float, hi_o: float, phase: str):
    cur = con.cursor()
    rows = [
        (trend_id, ts, float(center), f"{phase}_center"),
        (trend_id, ts, float(lo_i),   f"{phase}_inner_lower"),
        (trend_id, ts, float(hi_i),   f"{phase}_inner_upper"),
        (trend_id, ts, float(lo_o),   f"{phase}_lower_outer"),
        (trend_id, ts, float(hi_o),   f"{phase}_upper_outer"),
    ]
    cur.executemany("INSERT INTO trend_points(trend_id, ts, price, side) VALUES (?,?,?,?)", rows)
    con.commit()

def close_active(con: sqlite3.Connection, symbol: str, method: str, level: str, end_ts: str):
    cur = con.cursor()
    cur.execute("""UPDATE trends SET status='closed', end_ts=? 
                   WHERE symbol=? AND method=? AND level=? AND status='active'""",
                (end_ts, symbol, method, level))
    con.commit()

def simulate_symbol_history(con: sqlite3.Connection, df: pd.DataFrame, level: str, method: str,
                            qlow_outer: float, qhigh_outer: float, qlow_inner: float, qhigh_inner: float):
    symbol = str(df["symbol"].iloc[0])
    x = df["idx"].to_numpy(dtype=float)
    y = df["close"].to_numpy(dtype=float)
    ts = df["close_time"].astype(str).tolist()

    seg = 0
    start = 0
    run_out = 0
    establish_n = int(ESTABLISH_LEN[level])
    min_len_cut = int(MIN_SEG_LEN[level])

    for i in range(len(df)):
        xs = x[start:i+1]; ys = y[start:i+1]
        if len(xs) < 1: continue
        ch = build_channel(method, xs, ys, qlow_outer, qhigh_outer, qlow_inner, qhigh_inner)
        end_ts = ts[i]; n = len(xs)
        phase = "post" if n >= establish_n else "pre"
        confirmed = 1 if n >= establish_n else 0

        trend_row = dict(
            symbol=symbol, seg_index=seg, method=method, level=level,
            start_ts=ts[start], end_ts=end_ts, n=n,
            slope=ch["slope"], intercept=ch["intercept"],
            lower_slope=ch["lower_slope"], lower_intercept=ch["lower_intercept"],
            upper_slope=ch["upper_slope"], upper_intercept=ch["upper_intercept"],
            inner_lower_slope=ch["inner_lower_slope"], inner_lower_intercept=ch["inner_lower_intercept"],
            inner_upper_slope=ch["inner_upper_slope"], inner_upper_intercept=ch["inner_upper_intercept"],
            band_low_q=qlow_outer, band_high_q=qhigh_outer,
            inner_band_low_q=qlow_inner, inner_band_high_q=qhigh_inner,
            trend_type=("up" if ch["slope"]>0 else ("down" if ch["slope"]<0 else "flat")),
            r2=float(ch.get("r2", 0.0)), status="active", confirmed=confirmed
        )
        tid = upsert_trend(con, trend_row)

        x_i = xs[-1]
        center = ch["slope"]*x_i + ch["intercept"]
        lo_i   = ch["inner_lower_slope"]*x_i + ch["inner_lower_intercept"]
        hi_i   = ch["inner_upper_slope"]*x_i + ch["inner_upper_intercept"]
        lo_o   = ch["lower_slope"]*x_i + ch["lower_intercept"]
        hi_o   = ch["upper_slope"]*x_i + ch["upper_intercept"]
        append_point(con, tid, end_ts, center, lo_i, hi_i, lo_o, hi_o, phase)

        br = False
        if n >= min_len_cut:
            yhat_last = ch["slope"]*xs[-1] + ch["intercept"]
            resid_last = ys[-1] - yhat_last
            br = (resid_last < ch["lo_o"]) or (resid_last > ch["hi_o"])
        if br:
            run_out += 1
        else:
            run_out = 0
        if run_out >= int(BREAK_RUN_OUT):
            close_active(con, symbol, method, level, end_ts)
            seg += 1
            start = i + 1
            run_out = 0

def run_once(in_db: str, out_db: str, level: str, method: str,
             band_low_q: float, band_high_q: float, inner_band_low_q: float, inner_band_high_q: float,
             symbols: Optional[str] = None, limit: int = READ_LIMIT, table: str = "candles"):
    ensure_output_db(out_db)
    syms = load_symbols(in_db, symbols, table)
    with sqlite3.connect(out_db) as con:
        for sym in syms:
            df = read_candles(in_db, sym, limit=max(ESTABLISH_LEN.get(level, 150), limit), table=table)
            if df.empty: 
                continue
            wipe_symbol(con, sym, method, level)
            simulate_symbol_history(con, df, level, method, band_low_q, band_high_q, inner_band_low_q, inner_band_high_q)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-db", default=os.path.join(os.path.dirname(__file__), "data", "realtime_data.db"))
    ap.add_argument("--out-db", default=os.path.join(os.path.dirname(__file__), "data", "realtime_trend_detection.db"))
    ap.add_argument("--table", default="candles")
    ap.add_argument("--level", choices=list(ESTABLISH_LEN.keys()), default="mid")
    ap.add_argument("--method", choices=list(METHODS.keys()), default=DEFAULT_METHOD)
    ap.add_argument("--symbols", default=None, help="CSV of symbols; default=all found in input DB")
    ap.add_argument("--limit", type=int, default=READ_LIMIT)
    ap.add_argument("--outer", default=f"{OUTER_LOWER},{OUTER_UPPER}")
    ap.add_argument("--inner", default=f"{INNER_LOWER},{INNER_UPPER}")
    ap.add_argument("--loop", type=int, default=0, help="seconds; if >0 run forever on interval")
    args = ap.parse_args()
    try:
        ol, oh = [float(x) for x in args.outer.split(",")]
        il, ih = [float(x) for x in args.inner.split(",")]
    except Exception:
        ol, oh, il, ih = OUTER_LOWER, OUTER_UPPER, INNER_LOWER, INNER_UPPER

    if args.loop and args.loop > 0:
        while True:
            run_once(args.in_db, args.out_db, args.level, args.method, ol, oh, il, ih, args.symbols, args.limit, args.table)
            time.sleep(args.loop)
    else:
        run_once(args.in_db, args.out_db, args.level, args.method, ol, oh, il, ih, args.symbols, args.limit, args.table)

if __name__ == "__main__":
    main()
