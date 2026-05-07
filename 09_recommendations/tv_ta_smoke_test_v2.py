#!/usr/bin/env python3
"""
Minimal TradingView‑TA collector (per‑symbol only, no batch) z twardymi domyślnymi ustawieniami:
- 5 symboli (BINANCE): BTCUSDT, ETHUSDT, SOLUSDT, ADAUSDT, LINKUSDT
- futures=True (na TV -> sufiks .P)
- interwał 15m
- odstęp między symbolami 10s
- odświeżanie co 5 min
- zapisuje CAŁE summary() i CAŁE indicators() (plus oscylatory i średnie ruchome) do SQLite .db

Uruchom:
  python tv_ta_collector_min_v3.py
Lub jednorazowo:
  python tv_ta_collector_min_v3.py --once

Możesz nadal nadpisać parametry flagsami, ale domyślne są ustawione zgodnie z Twoją prośbą.
"""
import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from tradingview_ta import TA_Handler, Interval
except Exception:
    print("FATAL: tradingview-ta not installed.\nInstall: pip install tradingview-ta", file=sys.stderr)
    raise

# ------------------------------
# Configuration (symbols list, frequency etc.)
# ------------------------------
DEFAULT_DB = "tv_ta.db"                  # .db (output) path
DEFAULT_EXCHANGE = "BINANCE"
DEFAULT_INTERVAL = "1h"                  # np. "15m" lub "1h"
DEFAULT_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "LINKUSDT"
]
DEFAULT_FUTURES = True                    # na TV: BINANCE:SYMBOL.P
DEFAULT_TIMEOUT = 8.0
DEFAULT_REFRESH = 900                     # 5 minut (300) lub 15 minut (900)
DEFAULT_BETWEEN_SYMBOL_MS = 10_000        # 10 sekund (10_000)
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_MS = 5_000
DEFAULT_MAX_429_PER_CYCLE = 1
DEFAULT_CIRCUIT_SLEEP_S = 600             # po zadziałaniu obwodu – 10 min pauzy

SUPPORTED_INTERVALS = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "2h": Interval.INTERVAL_2_HOURS,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
    "1W": Interval.INTERVAL_1_WEEK,
    "1M": Interval.INTERVAL_1_MONTH,
}

DDL = r"""
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retrieved_at TEXT NOT NULL,            -- UTC ISO8601
    tv_time TEXT,                          -- analysis.time (ISO8601) jeśli dostępny
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    price_open REAL,
    price_high REAL,
    price_low REAL,
    price_close REAL,
    volume REAL,
    recommendation TEXT,
    buy_count INTEGER,
    sell_count INTEGER,
    neutral_count INTEGER,
    summary_json TEXT NOT NULL,            -- CAŁE summary()
    oscillators_json TEXT NOT NULL,
    moving_averages_json TEXT NOT NULL,
    indicators_json TEXT NOT NULL          -- CAŁE indicators()
);
CREATE INDEX IF NOT EXISTS idx_snapshots_sym_int_time ON snapshots(symbol, interval, retrieved_at);
"""

# ------------------------------
# Helpers
# ------------------------------

def ensure_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    for stmt in filter(None, DDL.split(";")):
        s = stmt.strip()
        if s:
            conn.execute(s)
    return conn


def parse_symbols(s: str) -> List[str]:
    return [x.strip().upper() for x in s.split(',') if x.strip()]


def qualify(exchange: str, symbols: List[str], futures: bool) -> List[str]:
    ex = exchange.upper()
    return [f"{ex}:{sym}.P" if futures else f"{ex}:{sym}" for sym in symbols]


def snapshot_to_row(analysis, *, exchange: str, symbol: str, interval_key: str):
    indicators = analysis.indicators or {}
    summary = analysis.summary or {}
    oscillators = analysis.oscillators or {}
    moving_averages = analysis.moving_averages or {}

    tvt = getattr(analysis, "time", None)
    tv_time_iso = None
    if tvt is not None:
        try:
            tv_time_iso = tvt.isoformat()
        except Exception:
            tv_time_iso = str(tvt)

    row = (
        datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        tv_time_iso,
        exchange.upper(),
        symbol.upper(),
        interval_key,
        indicators.get("open"),
        indicators.get("high"),
        indicators.get("low"),
        indicators.get("close"),
        indicators.get("volume"),
        summary.get("RECOMMENDATION"),
        int(summary.get("BUY", 0) or 0),
        int(summary.get("SELL", 0) or 0),
        int(summary.get("NEUTRAL", 0) or 0),
        json.dumps(summary, separators=(',',':')),
        json.dumps(oscillators, separators=(',',':')),
        json.dumps(moving_averages, separators=(',',':')),
        json.dumps(indicators, separators=(',',':')),
    )
    return row


def fetch_one(qsym: str, *, interval, timeout: float, proxies: Optional[Dict[str,str]] = None):
    ex, sym = qsym.split(":", 1)
    h = TA_Handler(
        symbol=sym,
        exchange=ex,
        screener="crypto",
        interval=interval,
        timeout=timeout,
        proxies=proxies,
    )
    return h.get_analysis()


def run_cycle(*, db: sqlite3.Connection, exchange: str, interval_key: str, symbols: List[str], futures: bool,
              timeout: float, between_symbol_ms: int, max_retries: int, retry_base_ms: int,
              max_429_per_cycle: int, circuit_sleep_s: int, proxies: Optional[Dict[str,str]]):
    tv_interval = SUPPORTED_INTERVALS[interval_key]
    qsyms = qualify(exchange, symbols, futures)
    random.shuffle(qsyms)

    stored = 0
    errors = 0
    hits_429 = 0

    rows = []
    for q in qsyms:
        attempt = 0
        while True:
            attempt += 1
            t0 = time.time()
            try:
                a = fetch_one(q, interval=tv_interval, timeout=timeout, proxies=proxies)
                dt_ms = int((time.time() - t0)*1000)
                s = a.summary or {}
                print(f"[OK] {q:<24} {dt_ms}ms RECO={s.get('RECOMMENDATION')} (B={s.get('BUY')} S={s.get('SELL')} N={s.get('NEUTRAL')})")
                # podgląd kilku wskaźników w konsoli
                ind = a.indicators or {}
                peek_keys = ("close","RSI","MACD.macd","MACD.signal","EMA20")
                peek = {k: ind[k] for k in peek_keys if k in ind}
                if peek:
                    print(f"     ind: {json.dumps(peek, separators=(',',':'))}")
                rows.append(snapshot_to_row(a, exchange=exchange, symbol=q.split(":",1)[1], interval_key=interval_key))
                stored += 1
                break
            except Exception as e:
                dt_ms = int((time.time() - t0)*1000)
                msg = str(e)
                is429 = ("429" in msg) or ("Too Many" in msg)
                print(f"[ERR] {q:<24} {dt_ms}ms {e.__class__.__name__}: {msg}")
                errors += 1
                if is429:
                    hits_429 += 1
                if is429 and attempt < max_retries:
                    delay = (retry_base_ms/1000.0)*(2**(attempt-1)) + (random.uniform(0, retry_base_ms/1000.0))
                    print(f"      backoff {delay:.3f}s (attempt {attempt}/{max_retries})")
                    time.sleep(delay)
                    continue
                break
        if hits_429 >= max_429_per_cycle:
            print(f"[CIRCUIT] hit {hits_429}×429 this cycle → pausing {circuit_sleep_s}s")
            time.sleep(circuit_sleep_s)
            break
        time.sleep(max(0, between_symbol_ms)/1000.0)

    if rows:
        db.executemany(
            """
            INSERT INTO snapshots (
                retrieved_at, tv_time, exchange, symbol, interval,
                price_open, price_high, price_low, price_close, volume,
                recommendation, buy_count, sell_count, neutral_count,
                summary_json, oscillators_json, moving_averages_json, indicators_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    print(f"[SUMMARY] stored_rows={stored} errors={errors} 429s={hits_429}")


def main():
    ap = argparse.ArgumentParser(description="Minimal TradingView-TA per-symbol collector (hard defaults)")
    # domyślne zgodnie z konfiguracją wyżej – możesz nadpisać flagami
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--interval", choices=list(SUPPORTED_INTERVALS.keys()), default=DEFAULT_INTERVAL)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--futures", action="store_true", default=DEFAULT_FUTURES)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--between-symbol-ms", type=int, default=DEFAULT_BETWEEN_SYMBOL_MS)
    ap.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    ap.add_argument("--retry-base-ms", type=int, default=DEFAULT_RETRY_BASE_MS)
    ap.add_argument("--max-429-per-cycle", type=int, default=DEFAULT_MAX_429_PER_CYCLE)
    ap.add_argument("--circuit-sleep-s", type=int, default=DEFAULT_CIRCUIT_SLEEP_S)
    ap.add_argument("--proxy-http", default=None)
    ap.add_argument("--proxy-https", default=None)
    ap.add_argument("--refresh", type=int, default=DEFAULT_REFRESH, help="Seconds between cycles; use --once for single run")
    ap.add_argument("--once", action="store_true")

    args = ap.parse_args()
    symbols = parse_symbols(args.symbols)

    proxies = {k:v for k,v in {"http": args.proxy_http, "https": args.proxy_https}.items() if v}

    conn = ensure_db(args.db)

    print("== tv_ta_collector_min_v3 ==")
    print(f"UTC now: {datetime.utcnow().isoformat()}")
    print(f"exchange={args.exchange} interval={args.interval} futures={args.futures}")
    print(f"symbols={symbols}")

    if args.once:
        run_cycle(db=conn, exchange=args.exchange, interval_key=args.interval, symbols=symbols,
                  futures=args.futures, timeout=args.timeout, between_symbol_ms=args.between_symbol_ms,
                  max_retries=args.max_retries, retry_base_ms=args.retry_base_ms,
                  max_429_per_cycle=args.max_429_per_cycle, circuit_sleep_s=args.circuit_sleep_s, proxies=proxies)
        return

    try:
        while True:
            run_cycle(db=conn, exchange=args.exchange, interval_key=args.interval, symbols=symbols,
                      futures=args.futures, timeout=args.timeout, between_symbol_ms=args.between_symbol_ms,
                      max_retries=args.max_retries, retry_base_ms=args.retry_base_ms,
                      max_429_per_cycle=args.max_429_per_cycle, circuit_sleep_s=args.circuit_sleep_s, proxies=proxies)
            time.sleep(max(10, args.refresh))
    except KeyboardInterrupt:
        print("\nInterrupted. Bye.")


if __name__ == "__main__":
    main()
