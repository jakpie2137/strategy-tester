# -*- coding: utf-8 -*-
"""
CandleBatchWriter with detailed logging:
- Logs every enqueue (DEBUG) with bucket + symbol.
- Logs flush when READY (INFO) and when FORCED (WARNING) incl. missing symbols.
- Logs kline volume fetch outcome per symbol (DEBUG).
"""
import logging
import threading
import time
import queue
from typing import Dict, List, Optional
import requests
import pandas as pd

# ---------------- adapters & helpers ----------------

class _ListQueueAdapter:
    """Adapter to treat a list as a minimal queue interface (put/get_nowait)."""
    def __init__(self, backing: list):
        self._b = backing
        self._lock = threading.Lock()

    def put(self, item):
        with self._lock:
            self._b.append(item)

    def get_nowait(self):
        with self._lock:
            if not self._b:
                raise queue.Empty
            return self._b.pop(0)

def _binance_interval(minutes: int) -> str:
    m = int(minutes)
    mapping = {
        1:'1m',3:'3m',5:'5m',15:'15m',30:'30m',
        60:'1h',120:'2h',240:'4h',360:'6h',480:'8h',720:'12h',
        1440:'1d',4320:'3d'
    }

    return mapping.get(m, '1m')

def _to_ms(t):
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return int(t) if t > 1e12 else int(t)*1000
    ts = pd.to_datetime(t, utc=True, errors="coerce")
    return None if ts is pd.NaT else int(ts.value // 10**6)

def _fill_quote(symbol: str, candle: dict, minutes: int, session: requests.Session) -> dict:
    """
    Fill quote volumes from fapi klines for the candle's bucket; noop if already present.
    """
    if candle.get("volume_quote") is not None and candle.get("taker_buy_volume_quote") is not None:
        return candle
    s = _to_ms(candle.get("open_time"))
    e = _to_ms(candle.get("close_time"))
    if s is None or e is None:
        return candle
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
           f"&interval={_binance_interval(minutes)}&startTime={s}&endTime={e-1}&limit=2")
    try:
        r = session.get(url, timeout=3)
        r.raise_for_status()
        data = r.json() or []
        matched = False
        for k in data:
            if int(k[0]) == int(s):
                matched = True
                if candle.get("volume_quote") is None and k[7] is not None:
                    candle["volume_quote"] = float(k[7])
                if candle.get("taker_buy_volume_quote") is None and k[10] is not None:
                    candle["taker_buy_volume_quote"] = float(k[10])
                break
        logging.debug("[CBW] kline volume %s bucket=%s matched=%s", symbol, pd.to_datetime(s, unit='ms', utc=True), matched)
    except Exception as e:
        logging.debug("[CandleBatchWorker] volume fetch %s failed: %s", symbol, e)
    return candle

# ---------------- main writer ----------------

class CandleBatchWriter(threading.Thread):
    """
    CandleBatchWriter(
        db,
        candle_queue,                   # Queue() preferred; list supported
        candle_interval_minutes: int,  # e.g. 1
        flush_delay_sec: int = 3,      # grace after bucket close
        expected_symbols: List[str] = None,  # full set of symbols
        force_flush_timeout_mult: int = 2    # force after N * interval + grace
    )
    """
    def __init__(self, db, candle_queue, candle_interval_minutes: int,
                 flush_delay_sec: int = 3, expected_symbols: Optional[List[str]] = None,
                 force_flush_timeout_mult: int = 2):
        super().__init__(daemon=True, name="CandleBatchWriter")
        self.db = db
        # normalize queue
        if isinstance(candle_queue, list):
            self.candle_queue = _ListQueueAdapter(candle_queue)
        elif hasattr(candle_queue, "get_nowait") and hasattr(candle_queue, "put"):
            self.candle_queue = candle_queue
        else:
            self.candle_queue = queue.Queue()

        self.mins = int(candle_interval_minutes or 1)
        self.grace = int(flush_delay_sec or 2)
        self.expected = list(expected_symbols or [])
        self.force_mult = max(1, int(force_flush_timeout_mult))
        self._stop = threading.Event()

        self._sess = requests.Session()
        self._sess.headers.update({"User-Agent": "UtpCBW/1.3"})

        # bucket -> { symbol -> candle }
        self._buckets: Dict[pd.Timestamp, Dict[str, dict]] = {}
        self._first_seen: Dict[pd.Timestamp, pd.Timestamp] = {}

    def stop(self):
        self._stop.set()

    def _bucket_key(self, c: dict) -> Optional[pd.Timestamp]:
        # Use open_time as bucket anchor; floor to interval for safety
        ot = c.get("open_time") or c.get("bucket") or c.get("close_time")
        ts = pd.to_datetime(ot, utc=True, errors="coerce")
        return None if ts is pd.NaT else ts.floor(f"{self.mins}min")

    def _ready(self, b: pd.Timestamp) -> bool:
        if not self.expected:
            return False
        have = set((self._buckets.get(b) or {}).keys())
        need = set(self.expected)
        return need.issubset(have)

    def _expired(self, b: pd.Timestamp) -> bool:
        fs = self._first_seen.get(b)
        if fs is None:
            return False
        now = pd.Timestamp.now(tz="UTC")
        limit = fs + pd.Timedelta(minutes=self.mins * self.force_mult, seconds=self.grace)
        return now >= limit

    def _flush_bucket(self, b: pd.Timestamp, forced: bool):
        slot = self._buckets.pop(b, {})
        self._first_seen.pop(b, None)
        if not slot:
            return

        have_syms = sorted(list(slot.keys()))
        missing = sorted(list(set(self.expected) - set(have_syms)))
        if forced and missing:
            logging.warning("[CBW] FORCED FLUSH bucket=%s have=%s missing=%s", str(b), have_syms, missing)
        else:
            logging.info("[CBW] READY FLUSH bucket=%s count=%d syms=%s", str(b), len(have_syms), have_syms)

        rows = []
        for sym, c in slot.items():
            c = dict(c)
            c["symbol"] = sym
            c = _fill_quote(sym, c, self.mins, self._sess)
            if c.get("volume_quote") is None:
                c["volume_quote"] = 0.0
            if c.get("taker_buy_volume_quote") is None:
                c["taker_buy_volume_quote"] = 0.0
            rows.append(c)

        try:
            if hasattr(self.db, "bulk_insert_candles"):
                self.db.bulk_insert_candles(rows)
            else:
                # fallback
                for r in rows:
                    self.db.insert_candle(r.get("symbol"), r)
            logging.info("[CBW] wrote %d candles bucket=%s", len(rows), str(b))
        except Exception as e:
            logging.error("[CandleBatchWorker] bulk insert failed: %s", e, exc_info=True)

    def run(self):
        logging.info("[CandleBatchWriter] start mins=%s grace=%s expected=%d",
                     self.mins, self.grace, len(self.expected))
        while not self._stop.is_set():
            try:
                drained = False
                while True:
                    try:
                        item = self.candle_queue.get_nowait()
                    except queue.Empty:
                        break
                    if not isinstance(item, dict) or "candle" not in item or "symbol" not in item:
                        continue
                    sym = item["symbol"]
                    c = dict(item["candle"])
                    c["symbol"] = sym
                    b = self._bucket_key(c)
                    if b is None:
                        continue
                    if b not in self._buckets:
                        self._buckets[b] = {}
                        self._first_seen[b] = pd.Timestamp.now(tz="UTC")
                    self._buckets[b][sym] = c
                    drained = True
                    logging.debug("[CBW] enqueue bucket=%s sym=%s", str(b), sym)

                if drained:
                    # flush all ready/expired buckets
                    for b in sorted(list(self._buckets.keys())):
                        if self._ready(b):
                            self._flush_bucket(b, forced=False)
                        elif self._expired(b):
                            self._flush_bucket(b, forced=True)

                time.sleep(0.1)
            except Exception as e:
                logging.error("[CandleBatchWriter] loop error: %s", e, exc_info=True)
                time.sleep(0.25)
