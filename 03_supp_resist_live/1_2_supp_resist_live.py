# -*- coding: utf-8 -*-
"""
1_2_supp_resist_live.py — LIVE horizontal Support/Resistance detector (no lookahead).

Reads:
  - data/realtime_data.db (table 'candles': symbol, open_time, open, high, low, close, volume, close_time)

Writes (creates if missing):
  - data/realtime_suppres_detection.db
    * sr_levels(id, symbol, method, lookback, n_per_level, k_cap, r_min_pct, kind, price, band_low, band_high,
                score, touches, from_above, from_below, wick_score, body_penalty, ts_start, ts_end, n_candles,
                UNIQUE(symbol, method, lookback, price))
    * sr_points(id, level_id, ts, price, side) where side in ('center','zone_low','zone_high')

CLI (examples):
  python 1_2_supp_resist_live.py --symbols BTCUSDT,ETHUSDT --lookback 1500 --N 500 --K 10 --R 0.005 --method touch
  python 1_2_supp_resist_live.py --symbols BTCUSDT --loop 6 --method wick_body --lookback 3000 --N 400 --K 12 --R 0.004

Notes:
  - No forward-looking: at each run we consider only the *latest* --lookback candles per symbol.
  - R is *minimum separation* between adjacent levels, in FRACTION of current price (e.g., 0.005 = 0.5%).
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
log = logging.getLogger("sr-live")

HERE    = os.path.dirname(os.path.abspath(__file__))
IN_DB   = os.path.join(HERE, "data", "realtime_data.db")
OUT_DB  = os.path.join(HERE, "data", "realtime_suppres_detection.db")
TABLE   = "candles"

READ_LIMIT = int(os.environ.get("TP_READ_LIMIT", "10000"))

DEFAULT_METHOD = "touch"
METHODS = {"touch":"hit-count in [low,high]", "wick_body":"+wicks, -bodies density"}

def ensure_output_db(out_db: str):
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
            kind TEXT NOT NULL,  -- 'support' or 'resistance' (relative to last close at run time)
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
            UNIQUE(symbol, method, lookback, price)
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

def load_symbols(in_db: str, symbols_csv: Optional[str], table: str) -> List[str]:
    if symbols_csv and symbols_csv.strip():
        syms = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
        return syms
    with sqlite3.connect(in_db) as con:
        df = pd.read_sql_query(f"SELECT DISTINCT symbol FROM {table}", con)
    return sorted(df["symbol"].astype(str).tolist()) if not df.empty else []

def read_candles(in_db: str, symbol: str, lookback: int, table: str) -> pd.DataFrame:
    lim = max(100, min(READ_LIMIT, int(lookback)))
    with sqlite3.connect(in_db) as con:
        df = pd.read_sql_query(
            f"""SELECT symbol, close_time, open, high, low, close
                FROM {table} WHERE symbol=? ORDER BY close_time DESC LIMIT ?""",
            con, params=[symbol, lim])
    if df.empty: return df
    df = df.dropna().sort_values("close_time").reset_index(drop=True)
    df["idx"] = df.index.values
    return df

def _build_bins(prices: np.ndarray, r_min_frac: float, ref_price: float) -> Tuple[np.ndarray, float]:
    pmin, pmax = float(np.min(prices)), float(np.max(prices))
    pmin, pmax = (pmin, pmax) if pmax>pmin else (pmin, pmin+max(1e-9, pmin*1e-6))
    # Bin width tied to R (half of R-separation)
    bw = max(1e-9, float(r_min_frac) * float(ref_price) / 2.0)
    nb = int(max(10, math.ceil((pmax - pmin) / bw)))
    edges = pmin + np.arange(nb+1, dtype=float) * bw
    centers = (edges[:-1] + edges[1:]) * 0.5
    return centers, bw

def _score_touch(df: pd.DataFrame, centers: np.ndarray, bw: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    # Count any candle that encloses a center as a "touch".
    lo = df["low"].to_numpy(dtype=float); hi = df["high"].to_numpy(dtype=float)
    op = df["open"].to_numpy(dtype=float); cl = df["close"].to_numpy(dtype=float)
    scores = np.zeros_like(centers, dtype=float)
    touches = np.zeros_like(centers, dtype=int)
    from_above = np.zeros_like(centers, dtype=int)
    from_below = np.zeros_like(centers, dtype=int)
    for i in range(len(df)):
        m = (centers >= lo[i]) & (centers <= hi[i])
        if not np.any(m): continue
        touches[m] += 1
        # Crude directionality for "respect from above/below"
        from_above[m & (cl[i] < centers)] += 1
        from_below[m & (cl[i] > centers)] += 1
    scores = touches.astype(float)  # pure touch count
    # light smoothing to avoid ragged peaks
    if len(scores) >= 3:
        scores = np.convolve(scores, np.ones(3)/3.0, mode="same")
    return scores, from_above, from_below

def _score_wick_body(df: pd.DataFrame, centers: np.ndarray, bw: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    op = df["open"].to_numpy(dtype=float); cl = df["close"].to_numpy(dtype=float)
    hi = df["high"].to_numpy(dtype=float); lo = df["low"].to_numpy(dtype=float)
    upper_w = np.maximum(op, cl); lower_w = np.minimum(op, cl)

    wick_score = np.zeros_like(centers, dtype=float)
    body_pen   = np.zeros_like(centers, dtype=float)
    touches    = np.zeros_like(centers, dtype=int)
    from_above = np.zeros_like(centers, dtype=int)
    from_below = np.zeros_like(centers, dtype=int)

    for i in range(len(df)):
        # bins in wicks
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

    # Prefer places with many wicks but few bodies
    scores = wick_score - 0.5*body_pen
    if len(scores) >= 3:
        scores = np.convolve(scores, np.ones(3)/3.0, mode="same")
    return scores, touches, from_above, from_below, wick_score, body_pen

def _pick_levels(centers: np.ndarray, scores: np.ndarray, last_price: float,
                 k_cap: int, r_min_frac: float) -> List[int]:
    """Return indices of selected centers, greedily, separated by >= R% of last_price."""
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
            # suppress neighborhood within min_sep
            used |= (np.abs(centers - centers[j]) < min_sep)
        if len(idxs) >= int(k_cap):
            break
    return sorted(idxs)

def _effective_k(k_cap: int, lookback: int, N: int) -> int:
    # at most one level per N candles
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

def run_once(in_db: str, out_db: str, method: str, lookback: int, N: int, K: int, R: float,
             symbols: Optional[str] = None, table: str = "candles"):
    ensure_output_db(out_db)
    syms = load_symbols(in_db, symbols, table)
    if not syms:
        log.warning("No symbols found in %s", in_db); return
    with sqlite3.connect(out_db) as con:
        for sym in syms:
            df = read_candles(in_db, sym, lookback, table)
            if df is None or df.empty or len(df) < max(50, min(lookback//10, 100)):
                log.info("%s: insufficient data (len=%s)", sym, 0 if df is None else len(df)); continue
            last_close = float(df["close"].iloc[-1])
            centers, bw = _build_bins(df[["low","high"]].to_numpy(dtype=float).ravel(), float(R), last_close)
            # Score per method
            if method == "touch":
                scores, from_above, from_below = _score_touch(df, centers, bw)
                touches = scores.astype(int)
                wick_score = np.zeros_like(scores); body_pen = np.zeros_like(scores)
            elif method == "wick_body":
                scores, touches, from_above, from_below, wick_score, body_pen = _score_wick_body(df, centers, bw)
            else:
                log.error("Unknown method=%s", method); continue

            k_eff = _effective_k(K, lookback=len(df), N=N)
            pick = _pick_levels(centers, scores, last_close, k_eff, float(R))
            if not pick:
                log.info("%s: no peaks selected", sym); continue

            # reset symbol/method/lookback rows
            wipe_symbol(con, sym, method, lookback)

            ts = df["close_time"].astype(str).tolist()
            ts_start, ts_end = ts[0], ts[-1]

            for j in pick:
                center = float(centers[j])
                low    = center - bw/2.0
                high   = center + bw/2.0
                kind   = "support" if center <= last_close else "resistance"
                row = dict(
                    symbol=sym, method=method, lookback=int(lookback), n_per_level=int(N), k_cap=int(K), r_min_pct=float(R),
                    kind=kind, price=center, band_low=low, band_high=high,
                    score=float(scores[j]), touches=int(touches[j] if method=="wick_body" else touches[j]),
                    from_above=int(from_above[j]), from_below=int(from_below[j]),
                    wick_score=float(wick_score[j] if method=="wick_body" else 0.0),
                    body_penalty=float(body_pen[j] if method=="wick_body" else 0.0),
                    ts_start=ts_start, ts_end=ts_end, n_candles=int(len(df))
                )
                lid = upsert_level(con, row)
                append_points(con, lid, ts, center, bw/2.0)
            log.info("%s: %d levels stored (%s)", sym, len(pick), method)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-db", default=IN_DB)
    ap.add_argument("--out-db", default=OUT_DB)
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--symbols", default=None, help="CSV of symbols; default=all found in input DB")
    ap.add_argument("--method", choices=list(METHODS.keys()), default=DEFAULT_METHOD)
    ap.add_argument("--lookback", type=int, default=3000, help="I — candles to look back")
    ap.add_argument("--N", type=int, default=500, help="N — candles per level (soft cap via floor(I/N))")
    ap.add_argument("--K", type=int, default=10, help="K — max number of levels")
    ap.add_argument("--R", type=float, default=0.005, help="R — min distance between levels as FRACTION of price (0.005 = 0.5%)")
    ap.add_argument("--loop", type=int, default=0, help="seconds; if >0 run forever on interval")
    args = ap.parse_args()

    if args.loop and args.loop > 0:
        while True:
            run_once(args.in_db, args.out_db, args.method, args.lookback, args.N, args.K, args.R,
                     symbols=args.symbols, table=args.table)
            time.sleep(args.loop)
    else:
        run_once(args.in_db, args_out_db := args.out_db, args.method, args.lookback, args.N, args.K, args.R,
                 symbols=args.symbols, table=args.table)

if __name__ == "__main__":
    main()
