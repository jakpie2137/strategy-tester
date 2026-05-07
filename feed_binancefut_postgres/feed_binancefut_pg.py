
# -*- coding: utf-8 -*-
import os
import sys
import time
import logging
import requests
from typing import List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- Paths & imports ---------------------------------------------------------
FEED_DIR = Path(__file__).resolve().parent
DATA_DIR = FEED_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PG_DIR = FEED_DIR.parent / "postgres_db"
sys.path.insert(0, str(PG_DIR))

from config_pg import load_env, get_dsn, mask_dsn  # type: ignore
from db_pg import (
    get_conn,
    ensure_schema,
    ensure_database_exists,
    upsert_candles,
    maybe_autostart_local_pg,
)  # type: ignore

# --- User-configurable knobs (kept in this script) ---------------------------
# Write mode: "append" or "clear"
WRITE_MODE = "clear"   # change to "append" to only append
CLEAR_ALL  = False     # if True and WRITE_MODE=="clear": TRUNCATE TABLE candles

# Symbols & time range (UTC) & interval
START    = "2025-01-01 00:00:00"   # UTC
END      = "2025-11-20 00:00:00"   # UTC
INTERVAL = "1m"
SYMBOLS  = ["ETHUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "FARTCOINUSDT"]

# Progress logging
PROGRESS_WIDTH = 42
PROGRESS_LOG_STEP = 1000  # also log every N rows even without TTY

# Batch size for INSERT fallback
INSERT_CHUNK = 2_000

# --- Logging -----------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    load_env(PG_DIR / ".env")
    LOG_FILE = os.getenv("LOG_FILE", str(DATA_DIR / "feed_binancefut_pg.log"))
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console noise OFF: progress bar handles stdout; only critical errors on stderr
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.CRITICAL)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    logger = logging.getLogger("feed_binancefut_pg")
    return logger

logger = _setup_logging()

# --- Progress bar ------------------------------------------------------------
def _enable_vt_mode(stream) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class ProgressBarSingle:
    """
    Jeden pasek postępu na stdout. Działa również w nie-TTY (PyCharm Run/Debug),
    bo zawsze rysujemy linię i nadpisujemy ją \r + flush.
    """
    def __init__(self, symbol: str, total: int, width: int = 38, stream=sys.stdout) -> None:
        self.symbol = symbol
        self.total = max(0, int(total))
        self.width = max(8, int(width))
        self.stream = stream
        self._last_len = 0  # do czyszczenia linii na finish

    def _render(self, done: int) -> str:
        done = max(0, min(int(done), self.total))
        frac = (done / self.total) if self.total else 0.0
        filled = int(self.width * frac)
        bar = "#" * filled + "-" * (self.width - filled)
        return f"[{self.symbol:<10}] [{bar}] {done}/{self.total} ({frac:5.1%})"

    def update(self, done: int) -> None:
        line = self._render(done)
        # nadpisz linię na stdout, bez logowania
        self.stream.write("\r" + line)
        # jeżeli nowa linia krótsza – nadpisz resztę spacjami
        pad = self._last_len - len(line)
        if pad > 0:
            self.stream.write(" " * pad)
        self.stream.flush()
        self._last_len = len(line)

    def finish(self) -> None:
        # pokaż finalny stan i przejdź do nowej linii
        line = self._render(self.total if self.total else 0)
        self.stream.write("\r" + line + "\n")
        self.stream.flush()
        self._last_len = 0


# --- Binance FAPI ------------------------------------------------------------
BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000, "1M": 2_592_000_000,
}

def parse_utc(dt_str: str) -> int:
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def align_to_interval_ms(ts_ms: int, interval_ms: int) -> int:
    return (ts_ms // interval_ms) * interval_ms

def fmt_duration(td) -> str:
    total = int(td.total_seconds())
    return f"{total//3600:02d}:{(total%3600)//60:02d}:{total%60:02d}"

def fetch_klines_stream(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1000):
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    interval_ms = INTERVAL_MS[interval]
    ts = align_to_interval_ms(start_ms, interval_ms)
    session = requests.Session()
    while ts < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": ts, "limit": limit}
        try:
            r = session.get(BINANCE_FAPI_KLINES, params=params, timeout=30)
        except Exception as e:
            logger.error("HTTP error for %s: %s", symbol, repr(e))
            time.sleep(1.0)
            continue
        if r.status_code != 200:
            logger.error("Binance %s %s: %s", r.status_code, r.reason, r.text[:300])
            time.sleep(1.0)
            continue
        batch = r.json()
        if not batch:
            logger.info("[%s] Empty batch – stopping.", symbol)
            break
        yield batch
        last_open_ms = int(batch[-1][0])
        ts = last_open_ms + interval_ms
        time.sleep(0.15)

# --- DB helpers --------------------------------------------------------------
def _ensure_conn(conn, dsn: str):
    """Make sure we have a usable connection after a failed transaction."""
    try:
        # psycopg3: .closed is True when closed
        if (conn is None) or getattr(conn, "closed", False):
            return get_conn(dsn)
        # try rollback to leave aborted transaction
        try:
            conn.rollback()
        except Exception:
            pass
        return conn
    except Exception:
        return get_conn(dsn)

def _clear_target(conn, symbols: List[str], start_ms: int, end_ms: int, interval: str) -> int:
    """Clear data according to WRITE_MODE settings."""
    if WRITE_MODE != "clear":
        return 0
    if CLEAR_ALL:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE public.candles")
        conn.commit()
        return 0

    start_dt = datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms/1000, tz=timezone.utc)
    deleted = 0
    with conn.cursor() as cur:
        for sym in symbols:
            cur.execute(
                "DELETE FROM public.candles WHERE symbol=%s AND open_time >= %s AND open_time < %s",
                (sym, start_dt, end_dt),
            )
            d = getattr(cur, "rowcount", 0) or 0
            deleted += d
    conn.commit()
    return deleted

def _local_insert_upsert(conn, rows: List[Tuple]) -> int:
    """Fallback insert using INSERT ... ON CONFLICT on (symbol, open_time, close_time).
    rows: (symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time)
    """
    if not rows:
        return 0
    sql = (
        "INSERT INTO public.candles ("
        "symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time"
        ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (symbol, open_time, close_time) DO UPDATE SET "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, "
        "volume_quote=EXCLUDED.volume_quote, taker_buy_volume_quote=EXCLUDED.taker_buy_volume_quote"
    )
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[i:i+INSERT_CHUNK]
            cur.executemany(sql, chunk)
            total += len(chunk)
    conn.commit()
    return total

def _write_rows(conn, rows: List[Tuple], dsn: str) -> int:
    try:
        return upsert_candles(conn, rows)
    except Exception as e:
        logger.warning("upsert_candles() failed (%r) → fallback to local upsert", e)
        # make sure we have a usable connection
        conn = _ensure_conn(conn, dsn)
        return _local_insert_upsert(conn, rows)

# --- Main driver -------------------------------------------------------------
def fetch_and_store_pg(start_date: str, end_date: str, interval: str, symbols: List[str],
                       dsn: Optional[str] = None, reset_db: bool = False, auto_create_db: bool = True) -> None:
    if auto_create_db:
        ensure_database_exists(dsn)
    logger.info("Connecting to Postgres…")
    conn = get_conn(dsn)
    ensure_schema(conn, reset=reset_db)

    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    interval_ms = INTERVAL_MS[interval]
    start_ms = parse_utc(start_date)
    end_ms = parse_utc(end_date)
    start_aligned = (start_ms // interval_ms) * interval_ms
    total_expected = max(0, (end_ms - start_aligned) // interval_ms)

    # Optional clear
    if WRITE_MODE == "clear":
        deleted = _clear_target(conn, symbols, start_ms, end_ms, interval)
        if CLEAR_ALL:
            logger.info("Cleared target: TRUNCATE candles")
        else:
            logger.info("Cleared target range: deleted=%d rows", deleted)

    try:
        for sym in symbols:
            bar = ProgressBarSingle(sym, total_expected, width=PROGRESS_WIDTH, stream=sys.stdout)
            start_wall = datetime.now()
            logger.info("[%s] START @ %s | range %s → < %s UTC | interval=%s | write=%s",
                        sym, start_wall.strftime("%Y-%m-%d %H:%M:%S"), start_date, end_date, interval, "INSERT")
            saved = 0
            bar.update(saved)

            rows_buffer: List[Tuple] = []

            for batch in fetch_klines_stream(sym, interval, start_ms, end_ms, limit=1000):
                stop_outer = False
                for k in batch:
                    open_ms = int(k[0]); close_ms = int(k[6]) - 1
                    if open_ms < start_ms and close_ms < start_ms:
                        continue
                    if open_ms + INTERVAL_MS[interval] > end_ms:
                        stop_outer = True
                        break
                    rows_buffer.append((
                        sym, float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                        float(k[7]), float(k[10]),
                        datetime.fromtimestamp(open_ms/1000, tz=timezone.utc),
                        datetime.fromtimestamp(close_ms/1000, tz=timezone.utc),
                    ))
                if rows_buffer:
                    saved += _write_rows(conn, rows_buffer, dsn or get_dsn())
                    rows_buffer.clear()
                    # progress repaint
                    bar.update(saved)
                if stop_outer:
                    break

            bar.finish()
            end_wall = datetime.now()
            logger.info("[%s] END   @ %s | elapsed: %s | total_rows_saved: %d",
                        sym, end_wall.strftime("%Y-%m-%d %H:%M:%S"), fmt_duration(end_wall-start_wall), saved)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        logger.info("Closed PG connection.")

if __name__ == "__main__":
    dsn_env = get_dsn()
    logger.info("DSN: %s", mask_dsn(dsn_env))
    with maybe_autostart_local_pg(dsn_env) as dsn:
        os.environ["PG_DSN"] = dsn
        fetch_and_store_pg(START, END, INTERVAL, SYMBOLS, dsn=dsn, reset_db=False, auto_create_db=True)
