# -*- coding: utf-8 -*-
"""
1_2_supp_resist_history.py — HISTORY Support/Resistance detector (display-only friendly).

Reads:
  - data/history_data.db (table 'candles': symbol, open, high, low, close, open_time, close_time)

Writes (creates if missing):
  - data/history_suppres_detection.db
    * sr_levels(id, symbol, method, lookback, n_per_level, k_cap, r_min_pct, kind,
                price, band_low, band_high, score, touches, from_above, from_below,
                wick_score, body_penalty, ts_start, ts_end, n_candles,
                UNIQUE(symbol, method, lookback, price, ts_start, ts_end))
    * sr_points(id, level_id, ts, price, side) where side in ('center','zone_low','zone_high')

Design goals:
  - No look-ahead beyond the selected window: the computation uses only the last `lookback` candles
    in the provided LIMIT window. The LIMIT defines the plotting window; LOOKBACK defines how much
    price history within that window contributes to level scoring.
  - Output is dense & dashboard-friendly: for each selected level we write a horizontal series of
    points at every candle ts in the LIMIT window, so Matplotlib draws continuous lines.

CLI (examples):
  python 1_2_supp_resist_history.py --symbols BTCUSDT --method touch --lookback 3000 --N 500 --K 10 --R 0.5 --limit 5000
  python 1_2_supp_resist_history.py --symbols ETHUSDT --method wick_body --lookback 2000 --N 400 --K 12 --R 0.4 --limit 4000

Notes:
  - R is *minimum separation* between adjacent levels, in PERCENT of current price (CLI). Internally stored as fraction.
  - K is a hard cap; also capped by floor(lookback / N). N ≈ "candles per level".
  - Methods:
      * 'touch'     — counts how often price range [low, high] hits a price bin (option 1).
      * 'wick_body' — rewards wick hits, penalizes body overlap (option 2).
"""
from __future__ import annotations
import os, sys, sqlite3, argparse, time, logging, math
from typing import Optional, Dict, List, Tuple
import numpy as np, pandas as pd

_LOG_LEVEL = os.environ.get("TP_LOG", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("sr-history")

HERE    = os.path.dirname(os.path.abspath(__file__))
IN_DB   = os.path.join(HERE, "data", "history_data.db")
OUT_DB  = os.path.join(HERE, "data", "history_suppres_detection.db")
TABLE   = "candles"

READ_LIMIT = int(os.environ.get("TP_READ_LIMIT", "5000"))

DEFAULT_METHOD = "touch"
METHODS = {"touch":"hit-count in [low,high]", "wick_body":"+wicks, -bodies density"}

def ensure_output_db(out_db: str) -> str:
    dirp = os.path.dirname(out_db) or "."
    try:
        os.makedirs(dirp, exist_ok=True)
    except FileExistsError:
        # If 'data' exists as a FILE on Windows, fall back to module folder.
        if not os.path.isdir(dirp):
            alt = os.path.join(os.path.dirname(__file__), os.path.basename(out_db))
            out_db = alt
            os.makedirs(os.path.dirname(out_db) or ".", exist_ok=True)
    with sqlite3.connect(out_db) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS sr_levels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            method TEXT NOT NULL,
            lookback INTEGER NOT NULL,
            n_per_level INTEGER NOT NULL,
            k_cap INTEGER NOT NULL,
            r_min_pct REAL NOT NULL,
            kind TEXT NOT NULL,  -- 'support' or 'resistance' (relative to last close of the window)
            price REAL NOT NULL,
            band_low REAL NOT NULL,
            band_high REAL NOT NULL,
            score REAL NOT NULL,
            touches INTEGER NOT NULL,
            from_above INTEGER NOT NULL,
            from_below INTEGER NOT NULL,
            wick_score REAL NOT NULL,
            body_penalty REAL NOT NULL,
            ts_start TEXT NOT NULL,
            ts_end TEXT NOT NULL,
            n_candles INTEGER NOT NULL,
            UNIQUE(symbol, method, lookback, price, ts_start, ts_end)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sr_points(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            price REAL NOT NULL,
            side TEXT NOT NULL, -- 'center' | 'zone_low' | 'zone_high'
            FOREIGN KEY(level_id) REFERENCES sr_levels(id)
        )""")
        con.commit()
    return out_db

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
            f"""SELECT symbol, close_time, open, high, low, close
                FROM {table} WHERE symbol=? ORDER BY close_time DESC LIMIT ?""",
            con, params=[symbol, int(limit)])
    if df.empty: return df
    df = df.dropna().sort_values("close_time").reset_index(drop=True)
    df["idx"] = df.index.values
    return df

def _build_bins(prices: np.ndarray, r_min_frac: float, ref_price: float) -> Tuple[np.ndarray, float]:
    pmin, pmax = float(np.min(prices)), float(np.max(prices))
    if not (pmax > pmin):
        pmax = pmin + max(1e-9, abs(pmin)*1e-6)
    rng = max(1e-12, pmax - pmin)
    # target bin width from R%, but ensure at least 30 bins across range
    bw_target = max(1e-9, float(r_min_frac) * float(ref_price) / 2.0)
    nb = int(np.clip(math.ceil(rng / bw_target), 30, 400))
    edges = np.linspace(pmin, pmax, nb + 1, dtype=float)
    centers = (edges[:-1] + edges[1:]) * 0.5
    bw = float(edges[1] - edges[0])
    return centers, bw

def _score_touch(df: pd.DataFrame, centers: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    lo = df["low"].to_numpy(dtype=float); hi = df["high"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    scores = np.zeros_like(centers, dtype=float)
    touches = np.zeros_like(centers, dtype=int)
    from_above = np.zeros_like(centers, dtype=int)
    from_below = np.zeros_like(centers, dtype=int)
    for i in range(len(df)):
        m = (centers >= lo[i]) & (centers <= hi[i])
        if not np.any(m): continue
        touches[m] += 1
        from_above[m & (cl[i] < centers)] += 1
        from_below[m & (cl[i] > centers)] += 1
    scores = touches.astype(float)
    if len(scores) >= 3:
        scores = np.convolve(scores, np.ones(3)/3.0, mode="same")
    wick_score = np.zeros_like(scores); body_pen = np.zeros_like(scores)
    return scores, touches, from_above, from_below, wick_score, body_pen

def _score_wick_body(df: pd.DataFrame, centers: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    op = df["open"].to_numpy(dtype=float); cl = df["close"].to_numpy(dtype=float)
    hi = df["high"].to_numpy(dtype=float); lo = df["low"].to_numpy(dtype=float)
    upper_w = np.maximum(op, cl); lower_w = np.minimum(op, cl)

    wick_score = np.zeros_like(centers, dtype=float)
    body_pen   = np.zeros_like(centers, dtype=float)
    touches    = np.zeros_like(centers, dtype=int)
    from_above = np.zeros_like(centers, dtype=int)
    from_below = np.zeros_like(centers, dtype=int)

    for i in range(len(df)):
        m_up = (centers >= upper_w[i]) & (centers <= hi[i])
        m_lo = (centers >= lo[i])      & (centers <= lower_w[i])
        m_bd = (centers >= lower_w[i]) & (centers <= upper_w[i])

        wick_score[m_up] += 1.0
        wick_score[m_lo] += 1.0
        body_pen[m_bd]   += 1.0

        m_touch = (centers >= lo[i]) & (centers <= hi[i])
        touches[m_touch] += 1
        from_above[m_touch & (cl[i] < centers)] += 1
        from_below[m_touch & (cl[i] > centers)] += 1

    scores = wick_score - 0.5*body_pen
    if len(scores) >= 3:
        scores = np.convolve(scores, np.ones(3)/3.0, mode="same")
    return scores, touches, from_above, from_below, wick_score, body_pen

def _pick_levels(centers: np.ndarray, scores: np.ndarray, last_price: float,
                 k_cap: int, r_min_frac: float) -> List[int]:
    idxs = []
    used = np.zeros_like(scores, dtype=bool)
    min_sep = float(r_min_frac) * float(last_price)
    order = list(np.argsort(scores)[::-1])
    for j in order:
        if scores[j] <= 0: break
        if used[j]: continue
        ok = True
        for ii in idxs:
            if abs(centers[j] - centers[ii]) < min_sep:
                ok = False; break
        if ok:
            idxs.append(j)
            used |= (np.abs(centers - centers[j]) < min_sep)
        if len(idxs) >= int(k_cap):
            break
    return sorted(idxs)

def _effective_k(k_cap: int, lookback: int, N: int) -> int:
    return max(1, min(int(k_cap), max(1, int(lookback // max(1, N)))))

def wipe_symbol(con: sqlite3.Connection, symbol: str, method: str, lookback: int):
    cur = con.cursor()
    cur.execute("SELECT id FROM sr_levels WHERE symbol=? AND method=? AND lookback=?", (symbol, method, int(lookback)))
    ids = [r[0] for r in cur.fetchall()]
    if ids:
        cur.execute("DELETE FROM sr_points WHERE level_id IN (" + ",".join(["?"]*len(ids)) + ")", ids)
        cur.execute("DELETE FROM sr_levels WHERE id IN (" + ",".join(["?"]*len(ids)) + ")", ids)
    con.commit()

def upsert_level(con: sqlite3.Connection, row: dict) -> int:
    cur = con.cursor()
    fields = ("symbol","method","lookback","n_per_level","k_cap","r_min_pct","kind","price","band_low","band_high",
              "score","touches","from_above","from_below","wick_score","body_penalty","ts_start","ts_end","n_candles")
    cur.execute(f"INSERT INTO sr_levels({','.join(fields)}) VALUES({','.join(['?']*len(fields))})",
                [row.get(k) for k in fields])
    tid = int(cur.lastrowid); con.commit()
    return tid

def append_points(con: sqlite3.Connection, level_id: int, ts_list: List[str], center: float, half_bw: float):
    cur = con.cursor()
    low = center - half_bw
    high = center + half_bw
    rows = []
    for ts in ts_list:
        rows.append((level_id, ts, float(center), "center"))
        rows.append((level_id, ts, float(low),    "zone_low"))
        rows.append((level_id, ts, float(high),   "zone_high"))
    cur.executemany("INSERT INTO sr_points(level_id, ts, price, side) VALUES (?,?,?,?)", rows)
    con.commit()

def detect_for_window(df: pd.DataFrame, method: str, lookback: int, N: int, K: int, R_frac: float):
    """
    Compute levels using only the last 'lookback' candles from df, but return points spanning the entire df window.
    """
    if df is None or df.empty or len(df) < max(50, min(lookback//10, 100)):
        return []
    end = len(df) - 1
    start = max(0, end - int(lookback) + 1)
    dflb = df.iloc[start:end+1].copy()
    last_close = float(dflb["close"].iloc[-1])

    centers, bw = _build_bins(dflb[["low","high"]].to_numpy(dtype=float).ravel(), float(R_frac), last_close)
    if method == "touch":
        scores, touches, from_above, from_below, wick_score, body_pen = _score_touch(dflb, centers)
    elif method == "wick_body":
        scores, touches, from_above, from_below, wick_score, body_pen = _score_wick_body(dflb, centers)
    else:
        raise ValueError("Unknown method: %s" % method)

    k_eff = _effective_k(K, lookback=len(dflb), N=N)
    pick = _pick_levels(centers, scores, last_close, k_eff, float(R_frac))
    out = []
    for j in pick:
        center = float(centers[j])
        low    = center - bw/2.0
        high   = center + bw/2.0
        out.append(dict(center=center, low=low, high=high, score=float(scores[j]),
                        touches=int(touches[j]), from_above=int(from_above[j]), from_below=int(from_below[j]),
                        wick_score=float(wick_score[j]), body_penalty=float(body_pen[j])))
    return out

def run_once(in_db: str, out_db: str, method: str, lookback: int, N: int, K: int, R_pct: float,
             symbols: Optional[str] = None, limit: int = READ_LIMIT, table: str = "candles"):
    out_db = ensure_output_db(out_db)
    syms = load_symbols(in_db, symbols, table)
    if not syms:
        log.warning("No symbols found in %s", in_db); return
    with sqlite3.connect(out_db) as con:
        for sym in syms:
            df = read_candles(in_db, sym, limit, table)
            if df is None or df.empty or len(df) < max(50, min(lookback//10, 100)):
                log.info("%s: insufficient data (len=%s)", sym, 0 if df is None else len(df)); continue
            # wipe existing rows for this config & symbol
            wipe_symbol(con, sym, method, lookback)

            levels = detect_for_window(df, method, lookback, N, K, float(R_pct)/100.0)
            if not levels:
                log.info("%s: no levels selected", sym); continue

            ts = df["close_time"].astype(str).tolist()
            ts_start, ts_end = ts[0], ts[-1]
            last_close = float(df["close"].iloc[-1])

            for lv in levels:
                kind = "support" if lv["center"] <= last_close else "resistance"
                row = dict(
                    symbol=sym, method=method, lookback=int(lookback), n_per_level=int(N), k_cap=int(K), r_min_pct=float(R_pct)/100.0,
                    kind=kind, price=float(lv["center"]), band_low=float(lv["low"]), band_high=float(lv["high"]),
                    score=float(lv["score"]), touches=int(lv["touches"]), from_above=int(lv["from_above"]), from_below=int(lv["from_below"]),
                    wick_score=float(lv["wick_score"]), body_penalty=float(lv["body_penalty"]),
                    ts_start=ts_start, ts_end=ts_end, n_candles=int(len(df))
                )
                lid = upsert_level(con, row)
                append_points(con, lid, ts, lv["center"], (lv["high"]-lv["low"])/2.0)

            log.info("%s: %d levels stored (%s, I=%d, limit=%d)", sym, len(levels), method, lookback, limit)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-db", default=IN_DB)
    ap.add_argument("--out-db", default=OUT_DB)
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--symbols", default=None, help="CSV of symbols; default=all found in input DB")
    ap.add_argument("--method", choices=list(METHODS.keys()), default=DEFAULT_METHOD)
    ap.add_argument("--lookback", type=int, default=3000, help="I — candles to look back (within LIMIT window)")
    ap.add_argument("--N", type=int, default=500, help="N — candles per level (soft cap via floor(I/N))")
    ap.add_argument("--K", type=int, default=10, help="K — max number of levels")
    ap.add_argument("--R", type=float, default=0.5, help="R — min distance between levels as PERCENT of price (0.5 = 0.5%)")
    ap.add_argument("--limit", type=int, default=READ_LIMIT, help="How many candles to plot (window); detection uses last I within this window")
    args = ap.parse_args()
    run_once(args.in_db, args.out_db, args.method, args.lookback, args.N, args.K, args.R,
             symbols=args.symbols, limit=args.limit, table=args.table)

if __name__ == "__main__":
    main()
