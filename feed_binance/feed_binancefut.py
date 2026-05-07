# -*- coding: utf-8 -*-
import os
import sys
import time
import sqlite3
import requests
from typing import List
from datetime import datetime, timezone

DEFAULT_DB_PATH = "data/live_data.db"
BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"

# mapowanie interwałów Binance → ms
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,  # ~30 dni
}

def parse_utc(dt_str):
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> ms."""
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def format_utc(ms):
    """ms -> 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

def align_to_interval_ms(ts_ms, interval_ms):
    """Zaokrągla w dół do początku świecy (UTC)."""
    return (ts_ms // interval_ms) * interval_ms

def ensure_dir_for(db_path: str):
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)

def _column_exists(conn, table: str, col: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def create_table(db_path=DEFAULT_DB_PATH):
    def _migrate_drop_volume(conn):
        """Usuwa kolumnę 'volume' przez rekonstrukcję tabeli (idempotentnie)."""
        # jeśli nie ma kolumny 'volume', nic nie rób
        if not _column_exists(conn, "candles", "volume"):
            return
        conn.execute("BEGIN")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles__new (
                    symbol TEXT NOT NULL,
                    open   REAL NOT NULL,
                    high   REAL NOT NULL,
                    low    REAL NOT NULL,
                    close  REAL NOT NULL,
                    volume_quote REAL NOT NULL,
                    taker_buy_volume_quote REAL NOT NULL,
                    open_time  TEXT NOT NULL,
                    close_time TEXT NOT NULL,
                    PRIMARY KEY (symbol, open_time)
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO candles__new
                (symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time)
                SELECT
                    symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time
                FROM candles
            """)
            conn.execute("DROP TABLE candles")
            conn.execute("ALTER TABLE candles__new RENAME TO candles")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    ensure_dir_for(db_path)
    with sqlite3.connect(db_path) as conn:
        # docelowa definicja (BEZ 'volume')
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                open   REAL NOT NULL,
                high   REAL NOT NULL,
                low    REAL NOT NULL,
                close  REAL NOT NULL,
                volume_quote REAL NOT NULL,           -- QUOTE volume (k[7])
                taker_buy_volume_quote REAL NOT NULL, -- TAKER BUY QUOTE (k[10])
                open_time  TEXT NOT NULL,             -- UTC 'YYYY-MM-DD HH:MM:SS'
                close_time TEXT NOT NULL,             -- UTC 'YYYY-MM-DD HH:MM:SS'
                PRIMARY KEY (symbol, open_time)
            )
        """)
        # jeśli mamy starą tabelę z 'volume', zrzuć tę kolumnę
        if _column_exists(conn, "candles", "volume"):
            _migrate_drop_volume(conn)
        conn.commit()

def reset_database(db_path=DEFAULT_DB_PATH):
    """Usuwa istniejący plik bazy i tworzy czystą tabelę."""
    ensure_dir_for(db_path)
    if os.path.exists(db_path):
        os.remove(db_path)
    create_table(db_path)

def insert_candles(db_path, rows):
    """
    rows = list[tuple(
        symbol, open, high, low, close,
        volume_quote, taker_buy_volume_quote,
        open_time, close_time
    )]
    """
    if not rows:
        return
    with sqlite3.connect(db_path) as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO candles
            (symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()

def print_progress(symbol, done, total, width=36):
    done = min(done, total)
    frac = 0.0 if total == 0 else done / float(total)
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    msg = "\r[{sym}] [{bar}] {done}/{total} ({pct:5.1%})".format(
        sym=symbol, bar=bar, done=done, total=total, pct=frac
    )
    sys.stdout.write(msg)
    sys.stdout.flush()

def fmt_duration(td):
    total = int(td.total_seconds())
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def fetch_klines_stream(symbol, interval, start_ms, end_ms, limit=1000):
    """
    Generator pobierający świeczki z Binance Futures (fapi) batchami.

    Struktura elementu 'kline' (najważniejsze):
      [0] open time (ms)
      [1] open
      [2] high
      [3] low
      [4] close
      [5] volume (BASE asset)
      [6] close time (ms)
      [7] quote asset volume
      [8] number of trades
      [9] taker buy base asset volume
      [10] taker buy quote asset volume
      [11] ignore
    """
    if interval not in INTERVAL_MS:
        raise ValueError("Nieobsługiwany interwał: %s" % interval)

    interval_ms = INTERVAL_MS[interval]
    ts = align_to_interval_ms(start_ms, interval_ms)

    while ts < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": ts, "limit": limit}
        r = requests.get(BINANCE_FAPI_KLINES, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError("Binance error %s: %s" % (r.status_code, r.text))

        batch = r.json()
        if not batch:
            break

        yield batch

        last_open_ms = int(batch[-1][0])
        ts = last_open_ms + interval_ms
        time.sleep(0.15)

def fetch_and_store(
    start_date,
    end_date,
    interval,
    symbols: List[str],
    db_path=DEFAULT_DB_PATH,
    progress_step=1000,
    reset_db=True
):
    """
    start_date / end_date: 'YYYY-MM-DD HH:MM:SS' (UTC), zakres [start, end)
    interval: '1m' | '5m' | ...
    symbols: ['BTCUSDT', 'ETHUSDT', ...]
    """
    # --- reset DB na starcie (zgodnie z życzeniem) ---
    if reset_db:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[DB] Reset bazy '{db_path}' @ {stamp}")
        reset_database(db_path)
    else:
        ensure_dir_for(db_path)
        create_table(db_path)

    if interval not in INTERVAL_MS:
        raise ValueError("Nieobsługiwany interwał: %s" % interval)

    interval_ms = INTERVAL_MS[interval]
    start_ms = parse_utc(start_date)
    end_ms = parse_utc(end_date)

    start_aligned = align_to_interval_ms(start_ms, interval_ms)
    total_expected = max(0, (end_ms - start_aligned) // interval_ms)

    for sym in symbols:
        start_wall = datetime.now()
        print("\n[{s}] START @ {t}".format(s=sym, t=start_wall.strftime("%Y-%m-%d %H:%M:%S")))
        print("[{}] Pobieram {} od {} do < {} (UTC)".format(sym, interval, start_date, end_date))

        rows_buffer = []
        saved = 0
        next_tick = progress_step

        print_progress(sym, saved, total_expected)

        for batch in fetch_klines_stream(sym, interval, start_ms, end_ms, limit=1000):
            stop_outer = False
            for k in batch:
                open_ms = int(k[0])
                close_ms = int(k[6]) - 1  # np. 5m: 14:10:00–14:14:59
                if open_ms < start_ms and close_ms < start_ms:
                    continue
                if open_ms + interval_ms > end_ms:
                    stop_outer = True
                    break

                rows_buffer.append((
                    sym,
                    float(k[1]),  # open
                    float(k[2]),  # high
                    float(k[3]),  # low
                    float(k[4]),  # close
                    float(k[7]),  # volume_quote (QUOTE)
                    float(k[10]), # taker_buy_volume_quote (QUOTE)
                    format_utc(open_ms),
                    format_utc(close_ms),
                ))

                if len(rows_buffer) + saved >= next_tick:
                    print_progress(sym, len(rows_buffer) + saved, total_expected)
                    next_tick += progress_step

            if rows_buffer:
                insert_candles(db_path, rows_buffer)
                saved += len(rows_buffer)
                rows_buffer.clear()
                print_progress(sym, saved, total_expected)

            if stop_outer:
                break

        print_progress(sym, saved, total_expected); sys.stdout.write("\n"); sys.stdout.flush()
        end_wall = datetime.now()
        elapsed = fmt_duration(end_wall - start_wall)
        print("[{s}] END   @ {t} | elapsed: {d}".format(
            s=sym, t=end_wall.strftime("%Y-%m-%d %H:%M:%S"), d=elapsed
        ))
        print("[{}] Zapisano świeczek: {} → {}".format(sym, saved, db_path))

# ---------------------------
# PRZYKŁAD UŻYCIA:
# ---------------------------
if __name__ == "__main__":
    START = "2025-10-01 00:00:00"
    END   = "2025-10-10 00:00:00"
    INTERVAL = "1m"
    SYMBOLS = [
        # "BTCUSDT",
        "ETHUSDT",
        # "BNBUSDT",
        # "SOLUSDT",
        "ADAUSDT",
        "DOGEUSDT",
        # "XRPUSDT",
        # "HBARUSDT",
        # "XLMUSDT",
        # "SUSDT",
        "FARTCOINUSDT",
        "1000PEPEUSDT",
        # "1000BONKUSDT",
        "1000SHIBUSDT",
    ]

    fetch_and_store(START, END, INTERVAL, SYMBOLS, db_path=DEFAULT_DB_PATH)
