# -*- coding: utf-8 -*-
"""
0_1_feed_binancefut_live.py — REST poller for Binance Futures klines (display-only DB writer).

- Polls every --poll seconds (default 15s) for new closed klines for each symbol.
- Interval (default 1m). Warmup backfill on start (--warmup N).
- On start, optionally clears table (--clear 1 by default).
- Writes into SQLite DB: data/realtime_data.db, table 'candles' (compatible with history detector).
- Deduplicates via PRIMARY KEY(symbol, close_time).

Example:
  python 0_1_feed_binancefut_live.py --symbols BTCUSDT,ETHUSDT --interval 1m --poll 15 --warmup 1000

Env overrides:
  TP_LOG=DEBUG|INFO|WARNING
"""
from __future__ import annotations
import os, sys, time, sqlite3, argparse, logging, math
from typing import List, Optional, Tuple, Any, Dict
from datetime import datetime, timezone
import requests

_LOG_LEVEL = os.environ.get("TP_LOG", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("live-feed")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "realtime_data.db")
TABLE   = "candles"

BASE_URL = "https://fapi.binance.com"  # Futures API
KLINES   = "/fapi/v1/klines"

def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def ensure_db(db_path: str):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
            symbol TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            close_time INTEGER NOT NULL,
            PRIMARY KEY(symbol, close_time)
        )""")
        con.commit()
    log.info("ensure_db: %s ready (table=%s)", db_path, TABLE)

def last_close_time_ms(con: sqlite3.Connection, symbol: str) -> Optional[int]:
    cur = con.cursor()
    cur.execute(f"SELECT MAX(close_time) FROM {TABLE} WHERE symbol=?", (symbol,))
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else None

def fetch_klines(symbol: str, interval: str, limit: int, start_ms: Optional[int]=None, end_ms: Optional[int]=None,
                 max_retries: int=5, timeout: int=10) -> List[list]:
    params = {"symbol": symbol, "interval": interval, "limit": int(limit)}
    if start_ms is not None: params["startTime"] = int(start_ms)
    if end_ms   is not None: params["endTime"]   = int(end_ms)
    backoff = 1.0
    for attempt in range(1, max_retries+1):
        try:
            r = requests.get(BASE_URL + KLINES, params=params, timeout=timeout, headers={"User-Agent":"UTP-livefeed/1.0"})
            if r.status_code == 429 or r.status_code == 418:
                log.warning("Rate limited (HTTP %s). Sleeping %.1fs...", r.status_code, backoff)
                time.sleep(backoff); backoff = min(backoff*2, 30); continue
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected response: {data}")
            return data
        except Exception as e:
            if attempt == max_retries:
                log.error("fetch_klines FAILED %s (%s): %s", symbol, interval, e)
                return []
            log.warning("fetch_klines retry %d/%d %s: %s", attempt, max_retries, symbol, e)
            time.sleep(backoff); backoff = min(backoff*2, 30)
    return []

def upsert_klines(con: sqlite3.Connection, symbol: str, klines: List[list]) -> int:
    if not klines: return 0
    rows = []
    for k in klines:
        # Binance array: [openTime, open, high, low, close, volume, closeTime, ...]
        ot, o, h, l, c, v, ct = int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), int(k[6])
        rows.append((symbol, ot, o, h, l, c, v, ct))
    cur = con.cursor()
    cur.executemany(f"""INSERT OR IGNORE INTO {TABLE}
        (symbol, open_time, open, high, low, close, volume, close_time)
        VALUES (?,?,?,?,?,?,?,?)""", rows)
    con.commit()
    ins = cur.rowcount if cur.rowcount is not None else 0
    log.debug("upsert_klines: %s inserted=%d", symbol, ins)
    return ins

def run_once(db_path: str, symbols: List[str], interval: str, warmup: int, clear: bool=False):
    ensure_db(db_path)
    with sqlite3.connect(db_path) as con:
        if clear:
            con.execute(f"DELETE FROM {TABLE}")
            con.commit()
            log.warning("DB CLEARED on start.")
        for sym in symbols:
            last_ms = last_close_time_ms(con, sym)
            if last_ms is None:
                # backfill
                data = fetch_klines(sym, interval, limit=warmup)
                inserted = upsert_klines(con, sym, data)
                if inserted:
                    last_ms = last_close_time_ms(con, sym)
                log.info("[INIT] %s backfill=%d last_close=%s", sym, inserted, iso(last_ms) if last_ms else "None")
            else:
                # bring to tip (in case we were behind)
                data = fetch_klines(sym, interval, limit=1000, start_ms=last_ms+1)
                inserted = upsert_klines(con, sym, data)
                log.info("[CATCHUP] %s inserted=%d last_close=%s", sym, inserted, iso(last_ms))

def loop(db_path: str, symbols: List[str], interval: str, poll: int):
    ensure_db(db_path)
    with sqlite3.connect(db_path, timeout=30) as con:
        while True:
            total = 0
            for sym in symbols:
                last_ms = last_close_time_ms(con, sym)
                # request a small window ahead; Binance returns only CLOSED klines
                data = fetch_klines(sym, interval, limit=500, start_ms=(last_ms+1) if last_ms else None)
                inserted = upsert_klines(con, sym, data)
                total += inserted
                if data:
                    tip = int(data[-1][6])
                    log.info("[LIVE] %s new=%d tip=%s", sym, inserted, iso(tip))
                else:
                    log.debug("[LIVE] %s no new data", sym)
            time.sleep(max(1, int(poll)))

def parse_symbols(s: str) -> List[str]:
    return [x.strip().upper() for x in s.split(",") if x.strip()]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="CSV of symbols")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--poll", type=int, default=15)
    ap.add_argument("--warmup", type=int, default=1000)
    ap.add_argument("--clear", type=int, default=1, help="1=wipe table on start, 0=keep")
    ap.add_argument("--once", action="store_true", help="perform initial sync only and exit")
    args = ap.parse_args()

    symbols = parse_symbols(args.symbols)
    log.info("[LIVE-FEED] interval=%s poll=%ss warmup=%d symbols=%s", args.interval, args.poll, args.warmup, symbols)

    run_once(args.db, symbols, args.interval, args.warmup, clear=bool(args.clear))
    if not args.once:
        loop(args.db, symbols, args.interval, args.poll)

if __name__ == "__main__":
    main()
