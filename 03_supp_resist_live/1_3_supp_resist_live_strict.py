# -*- coding: utf-8 -*-
"""
1_3_supp_resist_live_strict.py — STRICT rolling Support/Resistance detector (LIVE, no look-ahead).

Reads:
  - data/realtime_data.db (table 'candles': symbol, open_time, open, high, low, close, volume, close_time)

Writes (creates if missing):
  - data/realtime_suppres_detection.db
    * sr_tracks(id, symbol, method, lookback, n_per_level, k_cap, r_min_pct, ts_start, ts_end, state)
    * sr_track_points(id, track_id, ts, center, low, high)

Behavior:
  - Incremental append: continues from the last processed timestamp (per symbol/method/lookback).
  - 'stride' controls how often we recompute (bars step). 'include_current' decides whether to include in-progress candle.
  - R is FRACTION of price (e.g., 0.005 = 0.5%).

CLI:
  python 1_3_supp_resist_live_strict.py --symbols BTCUSDT,ETHUSDT --method wick_body \
      --lookback 3000 --N 500 --K 10 --R 0.005 --stride 3 --include_current 1 --loop 6
"""
from __future__ import annotations
import os, sys, sqlite3, argparse, time, logging, math
from typing import Optional, List, Tuple
import numpy as np, pandas as pd

_LOG_LEVEL = os.environ.get("TP_LOG", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("sr-live-strict")

HERE    = os.path.dirname(os.path.abspath(__file__))
IN_DB   = os.path.join(HERE, "data", "realtime_data.db")
OUT_DB  = os.path.join(HERE, "data", "realtime_suppres_detection.db")
TABLE   = "candles"

READ_LIMIT = int(os.environ.get("TP_READ_LIMIT", "20000"))
DEFAULT_METHOD = "wick_body"
METHODS = {"touch":"hit-count in [low,high]", "wick_body":"+wicks, -bodies density"}

def _setup_pragmas(con):
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA temp_store=MEMORY;")
        con.execute("PRAGMA mmap_size=134217728;")
    except Exception:
        pass

def ensure_output_db(out_db: str) -> str:
    os.makedirs(os.path.dirname(out_db) or ".", exist_ok=True)
    with sqlite3.connect(out_db) as con:
        _setup_pragmas(con)
        con.execute("""CREATE TABLE IF NOT EXISTS sr_tracks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            method TEXT NOT NULL,
            lookback INTEGER NOT NULL,
            n_per_level INTEGER NOT NULL,
            k_cap INTEGER NOT NULL,
            r_min_pct REAL NOT NULL,
            ts_start TEXT NOT NULL,
            ts_end TEXT NOT NULL,
            state TEXT NOT NULL  -- 'open' | 'closed'
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sr_track_points(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            center REAL NOT NULL,
            low REAL NOT NULL,
            high REAL NOT NULL,
            FOREIGN KEY(track_id) REFERENCES sr_tracks(id)
        )""")
        # Helpful indexes
        con.execute("CREATE INDEX IF NOT EXISTS idx_tracks_key ON sr_tracks(symbol, method, lookback)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tracks_ts ON sr_tracks(ts_end)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_track_points ON sr_track_points(track_id, ts)")
        con.commit()
    return out_db

def load_symbols(in_db: str, symbols_csv: Optional[str], table: str) -> List[str]:
    if symbols_csv and symbols_csv.strip():
        return [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
    with sqlite3.connect(in_db) as con:
        df = pd.read_sql_query(f"SELECT DISTINCT symbol FROM {table}", con)
    return sorted(df["symbol"].astype(str).tolist()) if not df.empty else []

def read_candles(in_db: str, symbol: str, limit: int, table: str) -> pd.DataFrame:
    lim = max(100, min(READ_LIMIT, int(limit)))
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
    if not (pmax > pmin):
        pmax = pmin + max(1e-9, abs(pmin)*1e-6)
    rng = max(1e-12, pmax - pmin)
    bw_target = max(1e-9, float(r_min_frac) * float(ref_price) / 2.0)
    nb = int(np.clip(math.ceil(rng / bw_target), 30, 600))
    edges = np.linspace(pmin, pmax, nb + 1, dtype=float)
    centers = (edges[:-1] + edges[1:]) * 0.5
    bw = float(edges[1] - edges[0])
    return centers, bw

def _score_touch(df: pd.DataFrame, centers: np.ndarray):
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
    return scores, touches, from_above, from_below, np.zeros_like(scores), np.zeros_like(scores)

def _score_wick_body(df: pd.DataFrame, centers: np.ndarray):
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

def _match_tracks(prev_centers, prev_ids, curr_centers, tol):
    from math import inf
    used_prev = set()
    mapping = [None] * len(curr_centers)
    for ci, c in enumerate(curr_centers):
        best = None; best_d = inf
        for pc, pid in zip(prev_centers, prev_ids):
            if pid in used_prev: continue
            d = abs(c - pc)
            if d < best_d and d <= tol:
                best_d = d; best = pid
        mapping[ci] = best
        if best is not None:
            used_prev.add(best)
    return mapping

def _last_processed_ts(con: sqlite3.Connection, symbol: str, method: str, lookback: int) -> Optional[str]:
    cur = con.cursor()
    cur.execute("SELECT MAX(ts_end) FROM sr_tracks WHERE symbol=? AND method=? AND lookback=?", (symbol, method, int(lookback)))
    r = cur.fetchone()
    return str(r[0]) if r and r[0] is not None else None

def run_once(in_db: str, out_db: str, method: str, lookback: int, N: int, K: int, R: float,
             symbols: Optional[str] = None, stride: int = 3, include_current: int = 1, table: str = "candles"):
    out_db = ensure_output_db(out_db)
    syms = load_symbols(in_db, symbols, table)
    if not syms:
        log.warning("No symbols found in %s", in_db); return

    con = sqlite3.connect(out_db, timeout=60)
    _setup_pragmas(con)
    cur = con.cursor()

    for sym in syms:
        df = read_candles(in_db, sym, limit=max(lookback*3, 2000), table=table)
        if df is None or df.empty or len(df) < max(50, min(lookback//10, 100)):
            log.info("%s: insufficient data (len=%s)", sym, 0 if df is None else len(df)); continue

        # Compute starting index based on last processed ts_end (incremental append)
        last_ts = _last_processed_ts(con, sym, method, lookback)
        start_i = max(lookback - 1, 0)
        if last_ts is not None:
            # find index of last_ts in df (or next one)
            pos = df.index[df["close_time"].astype(str) >= str(last_ts)]
            if len(pos) > 0:
                start_i = int(pos[0])
        BATCH = 500
        pending_rows = []

        # warm-start tracks from the immediate previous step if exists
        last_ids = []
        last_centers = []

        # reconstruct last active tracks (those whose ts_end == last_ts)
        if last_ts is not None:
            cur.execute("SELECT id FROM sr_tracks WHERE symbol=? AND method=? AND lookback=? AND ts_end=?",
                        (sym, method, int(lookback), str(last_ts)))
            rows = cur.fetchall()
            if rows:
                last_ids = [int(r[0]) for r in rows]
                # approximate centers by last points
                q = "SELECT track_id, center FROM sr_track_points WHERE ts=? AND track_id IN (%s)" % ",".join(["?"]*len(last_ids))
                cur.execute(q, [str(last_ts)] + last_ids)
                centers_map = {int(r[0]): float(r[1]) for r in cur.fetchall()}
                last_centers = [centers_map.get(tid, np.nan) for tid in last_ids]
                last_centers = [c for c in last_centers if not np.isnan(c)]

        r_frac = float(R)
        steps = list(range(start_i, len(df), max(1, int(stride))))
        for si, i in enumerate(steps):
            j_start = i - lookback + 1
            if j_start < 0: continue
            if int(include_current)==1:
                dfi = df.iloc[j_start:i+1]
            else:
                dfi = df.iloc[j_start:i]

            if dfi.empty: continue
            last_close = float(dfi["close"].iloc[-1])
            centers, bw = _build_bins(dfi[["low","high"]].to_numpy(dtype=float).ravel(), r_frac, last_close)
            if method == "touch":
                scores, touches, from_above, from_below, wick_score, body_pen = _score_touch(dfi, centers)
            else:
                scores, touches, from_above, from_below, wick_score, body_pen = _score_wick_body(dfi, centers)
            k_eff = _effective_k(K, lookback=len(dfi), N=N)
            pick = _pick_levels(centers, scores, last_close, k_eff, r_frac)
            curr_centers = [float(centers[j]) for j in pick]

            tol = max((centers[1]-centers[0])*2.0 if len(centers)>1 else last_close*r_frac*0.5, r_frac*last_close*0.5)
            mapping = _match_tracks(last_centers, last_ids, curr_centers, tol)

            ts_curr = str(dfi["close_time"].iloc[-1])

            new_last_ids = []
            new_last_centers = []

            used_prev = set([m for m in mapping if m is not None])
            # close unmatched previous
            for pid in list(last_ids):
                if pid not in used_prev:
                    cur.execute("UPDATE sr_tracks SET ts_end=?, state='closed' WHERE id=?", (ts_curr, int(pid)))

            for idx, c in enumerate(curr_centers):
                low = c - (centers[1]-centers[0])/2.0 if len(centers)>1 else c*0.0001
                high = c + (centers[1]-centers[0])/2.0 if len(centers)>1 else c*0.0001
                track_id = mapping[idx]
                if track_id is None:
                    cur.execute("""INSERT INTO sr_tracks(symbol, method, lookback, n_per_level, k_cap, r_min_pct, ts_start, ts_end, state)
                                   VALUES (?,?,?,?,?,?,?,?,?)""",
                                (sym, method, int(lookback), int(N), int(K), float(r_frac), ts_curr, ts_curr, 'open'))
                    track_id = int(cur.lastrowid)
                else:
                    cur.execute("UPDATE sr_tracks SET ts_end=? WHERE id=?", (ts_curr, int(track_id)))
                pending_rows.append((int(track_id), ts_curr, float(c), float(low), float(high)))
                new_last_ids.append(track_id); new_last_centers.append(c)

            if len(pending_rows) >= BATCH:
                cur.executemany("INSERT INTO sr_track_points(track_id, ts, center, low, high) VALUES (?,?,?,?,?)", pending_rows)
                pending_rows = []
                con.commit()

            if si % max(1, (len(steps)//20 or 1)) == 0:
                log.info("%s: progress %d/%d", sym, si, len(steps))

            last_ids, last_centers = new_last_ids, new_last_centers

        if pending_rows:
            cur.executemany("INSERT INTO sr_track_points(track_id, ts, center, low, high) VALUES (?,?,?,?,?)", pending_rows)
            con.commit()

        # ensure all open tracks are closed at tip if we included current
        if last_ids:
            tip_ts = str(df["close_time"].iloc[-1])
            cur.executemany("UPDATE sr_tracks SET ts_end=?, state='closed' WHERE id=?", [(tip_ts, int(pid)) for pid in last_ids])
            con.commit()

        log.info("%s: strict live update done (I=%d stride=%d)", sym, int(lookback), int(stride))

    con.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-db", default=IN_DB)
    ap.add_argument("--out-db", default=OUT_DB)
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--method", choices=list(METHODS.keys()), default=DEFAULT_METHOD)
    ap.add_argument("--lookback", type=int, default=3000)
    ap.add_argument("--N", type=int, default=500)
    ap.add_argument("--K", type=int, default=10)
    ap.add_argument("--R", type=float, default=0.005, help="fraction of price (0.005=0.5%)")
    ap.add_argument("--stride", type=int, default=3, help="compute every N-th bar")
    ap.add_argument("--include_current", type=int, default=1)
    ap.add_argument("--loop", type=int, default=0, help="seconds; if >0 run forever on interval")
    args = ap.parse_args()

    if args.N > args.lookback:
        print("[WARN] N > lookback; clamping N to lookback//2", file=sys.stderr)
        args.N = max(1, args.lookback//2)
    if args.R > 0.10:
        print("[WARN] R > 10%% is likely unreasonable; clamping to 5%%", file=sys.stderr)
        args.R = 0.05

    if args.loop and args.loop > 0:
        while True:
            run_once(args.in_db, args.out_db, args.method, args.lookback, args.N, args.K, args.R,
                     symbols=args.symbols, stride=args.stride, include_current=args.include_current, table=args.table)
            time.sleep(args.loop)
    else:
        run_once(args.in_db, args.out_db, args.method, args.lookback, args.N, args.K, args.R,
                 symbols=args.symbols, stride=args.stride, include_current=args.include_current, table=args.table)

if __name__ == "__main__":
    main()
