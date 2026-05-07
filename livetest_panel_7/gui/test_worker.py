import importlib
import types
import json
import logging
import time
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from datetime import datetime as _dt, timezone as _tz

from PyQt5.QtCore import QThread, pyqtSignal

from config import MAX_WORKER_CANDLES  # always required
import config as _cfg  # for optional flags

# Optional performance/debug flags with safe defaults
PERF_DEBUG = getattr(_cfg, "PERF_DEBUG", False)
WORKER_RAM_DEBUG = getattr(_cfg, "WORKER_RAM_DEBUG", False)
DB_DEBUG = getattr(_cfg, "DB_DEBUG", False)
INDICATOR_FLUSH_ROWS = getattr(_cfg, "INDICATOR_FLUSH_ROWS", 4000)
WRITE_INDICATORS_TO_DB = getattr(_cfg, "WRITE_INDICATORS_TO_DB", False)
PERF_CHUNK_ROWS = getattr(_cfg, "PERF_CHUNK_ROWS", INDICATOR_FLUSH_ROWS or 4000)

from backtester.engine import MultiSymbolEngine, _apply_bias_to_open_signal, order_columns, INDICATORS_TABLE
from backtester.strategies.base import BaseStrategy
from backtester.utils import PerfTimer, perf_log
from backtester.strategies.base import BaseStrategy

try:
    import math
    import pandas as _pd
    pd = _pd
except Exception:
    # awaryjny minimalny substytut, żeby pd.isna działało
    class _PD:
        @staticmethod
        def isna(x):
            try:
                return isinstance(x, float) and math.isnan(x)
            except Exception:
                return False
    pd = _PD()

# ========================
# Helpers
# ========================

def _as_df(obj):
    """Coerce list[dict]/dict/DataFrame to DataFrame; None -> None."""
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        return obj
    try:
        if isinstance(obj, list):
            if not obj:
                return pd.DataFrame()
            if isinstance(obj[0], dict):
                return pd.DataFrame(obj)
            if isinstance(obj[0], (list, tuple)):
                cols = ['symbol','open_time','open','high','low','close','volume','close_time']
                try:
                    return pd.DataFrame(obj, columns=cols[:len(obj[0])])
                except Exception:
                    return pd.DataFrame(obj)
        if isinstance(obj, dict):
            return pd.DataFrame([obj])
    except Exception:
        pass
    return None

def _safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def _nanfilter(seq):
    out = []
    for v in (seq or []):
        try:
            f = float(v)
            if f == f:  # not NaN
                out.append(f)
        except Exception:
            continue
    return out

def _avg(vals):
    arr = _nanfilter(vals)
    return (sum(arr)/len(arr)) if arr else None

def _numeric_keys_from(vals: dict):
    """
    Return set of keys that look like numeric indicators (floatable and not NaN).
    NOTE: No special-casing of any single indicator here. Strategy controls content.
    """
    out = set()
    for k, v in (vals or {}).items():
        try:
            f = float(v)
            if f == f:
                out.add(str(k))
        except Exception:
            pass
    return out

def _has_overridden(obj, method_name: str, base_cls) -> bool:
    impl = getattr(type(obj), method_name, None)
    base_impl = getattr(base_cls, method_name, None)
    return (callable(impl) and callable(base_impl) and impl is not base_impl)

def _coerce_dt(series):
    """Coerce a pandas Series to UTC datetime. Accepts seconds int/float or string-like."""
    if series is None:
        return None
    s = series
    try:
        # numeric seconds?
        if np.issubdtype(s.dtype, np.integer) or np.issubdtype(s.dtype, np.floating):
            return pd.to_datetime(s, unit='s', errors="coerce")
    except Exception:
        pass
    # string or datetime
    return pd.to_datetime(s, errors="coerce")

def _normalize_candles_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Make sure we have:
      - 'close_time' column (UTC tz-aware) and set it as index
      - 'close' column (float)
      - 'symbol' column
    Accepts aliases: 'timestamp' / 'TS' for time; 'CLOSE' / 'close_price' for price.
    """
    if df is None or df.empty:
        return df

    cols = {c.lower(): c for c in df.columns}

    # --- time ---
    close_time = None
    if 'close_time' in df.columns:
        close_time = _coerce_dt(df['close_time'])
    elif 'timestamp' in df.columns:
        close_time = _coerce_dt(df['timestamp'])
    elif 'ts' in cols:
        close_time = _coerce_dt(df[cols['ts']])
    elif 'open_time' in df.columns:
        # fall back to open_time if it's the only timestamp
        close_time = _coerce_dt(df['open_time'])
    else:
        # maybe index is already datetime
        if isinstance(df.index, pd.DatetimeIndex):
            close_time = df.index if df.index.tz is not None else df.index

    if close_time is None:
        # last resort: try any column that looks like time
        cand = None
        for name in ('time', 't', 'date'):
            if name in cols:
                cand = _coerce_dt(df[cols[name]])
                if cand is not None:
                    break
        close_time = cand

    if close_time is None:
        raise KeyError("close_time")

    df = df.copy()
    df['close_time'] = close_time
    df = df.dropna(subset=['close_time'])

    # --- price ---
    if 'close' in df.columns:
        pass
    elif 'CLOSE' in df.columns:
        df['close'] = df['CLOSE']
    elif 'close_price' in df.columns:
        df['close'] = df['close_price']
    else:
        raise KeyError("close")

    # symbol
    if 'symbol' not in df.columns:
        df['symbol'] = str(symbol)

    # index = close_time
    if not isinstance(df.index, pd.DatetimeIndex):
        df.set_index('close_time', inplace=True)
    else:
        if 'close_time' not in df.columns:
            df['close_time'] = df.index

    # enforce tz-aware (UTC)
    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')
    except Exception:
        pass

    return df.sort_index()

# ========================
# Risk config (overrides via rsi_config.py)
# ========================

try:
    from backtester.strategies import rsi_config as _scfg
    RISK_MODE = getattr(_scfg, "RISK_MODE", "FIXED").upper()
    RISK_PARAMS_ATR_OVERRIDES = dict(getattr(_scfg, "RISK_PARAMS_ATR_OVERRIDES", {}))
    RISK_PARAMS_FIXED_OVERRIDES = dict(getattr(_scfg, "RISK_PARAMS_FIXED_OVERRIDES", {}))
    TRAILING_STOP_ENABLED = bool(getattr(_scfg, "TRAILING_STOP_ENABLED", True))
    CLOSE_AFTER_X_CANDLES = int(getattr(_scfg, "CLOSE_AFTER_X_CANDLES", 0) or 0)
    CLOSE_EXECUTION_TYPE = getattr(_scfg, "CLOSE_EXECUTION_TYPE", "on_candle_close")
    CLOSE_EXECUTION_SLIPPAGE = float(getattr(_scfg, "CLOSE_EXECUTION_SLIPPAGE", 0.0005))
    ENTRY_EXECUTION_SLIPPAGE = float(getattr(_scfg, "ENTRY_EXECUTION_SLIPPAGE", 0.0))

    # StopLoss Updater configuration (static jump + dynamic SL)
    SL_UPDATER_CFG = dict(getattr(_scfg, "SL_UPDATER", {}) or {})
    SLU_ENABLED = bool(SL_UPDATER_CFG.get("enabled", False))
    SLU_STATIC_ENABLED = bool(SL_UPDATER_CFG.get("static_jump_enabled", True))
    SLU_DYNAMIC = bool(SL_UPDATER_CFG.get("dynamic_SL", False))
    SLU_TRIGGER = _safe_float(SL_UPDATER_CFG.get("trigger_move_SL")) or 0.0
    SLU_MOVE_TO = _safe_float(SL_UPDATER_CFG.get("move_SL_to")) or 0.0
    SLU_RANGE_TYPE = (SL_UPDATER_CFG.get("range_type") or "entry_to_tp")

    # global enabled flag dominates both static + dynamic
    if not SLU_ENABLED:
        SLU_STATIC_ENABLED = False
        SLU_DYNAMIC = False

    # sanity for static jump: trigger/move fractions must be sensible
    if SLU_TRIGGER <= 0.0 or SLU_TRIGGER > 1.0 or SLU_MOVE_TO < 0.0 or SLU_MOVE_TO > 1.0 or SLU_MOVE_TO >= SLU_TRIGGER:
        SLU_STATIC_ENABLED = False
        SLU_TRIGGER = 0.0
except Exception:
    RISK_MODE = "FIXED"
    RISK_PARAMS_ATR_OVERRIDES = {}
    RISK_PARAMS_FIXED_OVERRIDES = {}
    TRAILING_STOP_ENABLED = True
    CLOSE_AFTER_X_CANDLES = 0
    CLOSE_EXECUTION_TYPE = "on_candle_close"
    CLOSE_EXECUTION_SLIPPAGE = 0.0005
    ENTRY_EXECUTION_SLIPPAGE = 0.0
    SL_UPDATER_CFG = {}
    SLU_ENABLED = False
    SLU_STATIC_ENABLED = False
    SLU_DYNAMIC = False
    SLU_TRIGGER = 0.0
    SLU_MOVE_TO = 0.0
    SLU_RANGE_TYPE = "entry_to_tp"

def _risk_levels_fixed(side: str, entry: float, fixed: dict):
    tp_long   = float(fixed.get("tp_long",   1.01))
    sl_long   = float(fixed.get("sl_long",   0.99))
    tp_short  = float(fixed.get("tp_short",  0.99))
    sl_short  = float(fixed.get("sl_short",  1.01))
    trail_long  = float(fixed.get("trail_long",  0.9975))
    trail_short = float(fixed.get("trail_short", 1.0025))
    if side == "long":
        return entry * tp_long, entry * sl_long, entry * trail_long
    else:
        return entry * tp_short, entry * sl_short, entry * trail_short

def _risk_levels_atr(side: str, entry: float, atr_val: float, atr_cfg: dict):
    k_tp_l  = float(atr_cfg.get("tp_k_long",  1.5))
    k_sl_l  = float(atr_cfg.get("sl_k_long",  1.5))
    k_tp_s  = float(atr_cfg.get("tp_k_short", 1.5))
    k_sl_s  = float(atr_cfg.get("sl_k_short", 1.5))
    k_ts_l  = float(atr_cfg.get("ts_k_long",  0.5))
    k_ts_s  = float(atr_cfg.get("ts_k_short", 0.5))

    # limits_* to UŁAMKI ceny (0.05 = 5%, 0.01 = 1% itd.)
    lim_min = dict(atr_cfg.get("limits_min", {}) or {})
    lim_max = dict(atr_cfg.get("limits_max", {}) or {})

    if side == "long":
        tp_delta = _clamp_by_ref(
            k_tp_l * atr_val,
            entry,
            float(lim_min.get("tp_long", 0.0) or 0.0),
            float(lim_max.get("tp_long", 0.0) or 0.0),
        )
        sl_delta = _clamp_by_ref(
            k_sl_l * atr_val,
            entry,
            float(lim_min.get("sl_long", 0.0) or 0.0),
            float(lim_max.get("sl_long", 0.0) or 0.0),
        )
        tp = entry + tp_delta
        sl = entry - sl_delta
        ts = entry - (k_ts_l * atr_val)
    else:  # short
        tp_delta = _clamp_by_ref(
            k_tp_s * atr_val,
            entry,
            float(lim_min.get("tp_short", 0.0) or 0.0),
            float(lim_max.get("tp_short", 0.0) or 0.0),
        )
        sl_delta = _clamp_by_ref(
            k_sl_s * atr_val,
            entry,
            float(lim_min.get("sl_short", 0.0) or 0.0),
            float(lim_max.get("sl_short", 0.0) or 0.0),
        )
        tp = entry - tp_delta
        sl = entry + sl_delta
        ts = entry + (k_ts_s * atr_val)

    return tp, sl, ts


def _clamp_by_ref(delta: float, ref: float, min_frac: float, max_frac: float):
    """
    Ogranicza |delta| względem ceny referencyjnej 'ref', używając UŁAMKÓW ceny jako limitów.

    min_frac / max_frac:
      0.05  ->  5% * ref
      0.01  ->  1% * ref
      0.001 -> 0.1% * ref

    Jeśli min_frac/max_frac == 0 -> brak limitu w daną stronę.
    """
    if not np.isfinite(delta) or not np.isfinite(ref) or ref == 0.0:
        return delta

    sign = 1.0 if delta >= 0 else -1.0
    abs_delta = abs(delta)

    # dolny limit (jeśli >0) – minimalna odległość od referencji
    if min_frac:
        min_abs = abs(ref) * float(min_frac)
        if abs_delta < min_abs:
            abs_delta = min_abs

    # górny limit (jeśli >0) – maksymalna odległość od referencji
    if max_frac:
        max_abs = abs(ref) * float(max_frac)
        if abs_delta > max_abs:
            abs_delta = max_abs

    return sign * abs_delta



def _apply_slippage(base_price: float, side: str) -> float:
    """Apply global CLOSE_EXECUTION_SLIPPAGE in the 'worse' direction.

    - side == 'long'  -> worse price is LOWER than base_price
    - side == 'short' -> worse price is HIGHER than base_price

    CLOSE_EXECUTION_SLIPPAGE is a FRACTION of price (0.0005 = 0.05%%).
    If slippage is 0.0 or base_price is not finite, returns base_price unchanged.
    """
    try:
        p = float(base_price)
    except Exception:
        return base_price
    if not np.isfinite(p):
        return base_price

    try:
        slip = float(CLOSE_EXECUTION_SLIPPAGE or 0.0)
    except Exception:
        slip = 0.0

    if slip <= 0.0:
        return p

    s = (side or "").lower()
    if s == "long":
        return p * (1.0 - slip)
    elif s == "short":
        return p * (1.0 + slip)
    return p



def _apply_entry_slippage(base_price: float, side: str) -> float:
    """Apply ENTRY_EXECUTION_SLIPPAGE in the 'worse' direction for entries.

    - side == 'long'  -> worse entry is HIGHER than base_price
    - side == 'short' -> worse entry is LOWER than base_price

    ENTRY_EXECUTION_SLIPPAGE is a FRACTION of price (0.0005 = 0.05%%).
    If slippage is 0.0 or base_price is not finite, returns base_price unchanged.
    """
    try:
        p = float(base_price)
    except Exception:
        return base_price
    if not np.isfinite(p):
        return base_price

    try:
        slip = float(ENTRY_EXECUTION_SLIPPAGE or 0.0)
    except Exception:
        slip = 0.0

    if slip <= 0.0:
        return p

    s = (side or "").lower()
    if s == "long":
        return p * (1.0 + slip)
    elif s == "short":
        return p * (1.0 - slip)
    return p


def _compute_open_risk(side: str, entry: float, atr_val: float):
    """Compute TP/SL/TS for a new position based on current RISK_MODE and overrides.

    This mirrors legacy worker behaviour; it delegates math to _risk_levels_* helpers and uses
    the global ATR/FIXED override dicts.
    """
    if RISK_MODE == "ATR":
        return _risk_levels_atr(side, entry, atr_val, RISK_PARAMS_ATR_OVERRIDES)
    return _risk_levels_fixed(side, entry, RISK_PARAMS_FIXED_OVERRIDES)


# Trailing stop from benchmark with ATR clamps by benchmark
def _ts_from_benchmark(side: str, benchmark: float, atr_val: float, entry_price: float = None):
    """Project trailing-stop level from a benchmark price.

    ATR mode:
        - scale ATR by ts_k_long/ts_k_short from RISK_PARAMS_ATR_OVERRIDES
        - clamp resulting distance by ts_atr_min_pct/ts_atr_max_pct, using BENCHMARK as reference
    FIXED mode:
        - simple multiplicative trail_long/trail_short applied to benchmark.
    """
    if RISK_MODE == "ATR":
        k = float(RISK_PARAMS_ATR_OVERRIDES.get("ts_k_long", 0.5)) if side == "long" else float(RISK_PARAMS_ATR_OVERRIDES.get("ts_k_short", 0.5))
        minp = float(RISK_PARAMS_ATR_OVERRIDES.get("ts_atr_min_pct", 0.0) or 0.0)
        maxp = float(RISK_PARAMS_ATR_OVERRIDES.get("ts_atr_max_pct", 0.0) or 0.0)
        raw = k * float(atr_val or 0.0)
        # clamp by BENCHMARK (legacy semantics)
        d = _clamp_by_ref(raw, benchmark, minp, maxp)
        return (float(benchmark) - d) if side == "long" else (float(benchmark) + d)
    else:
        if side == "long":
            return float(benchmark) * float(RISK_PARAMS_FIXED_OVERRIDES.get("trail_long", 0.9975))
        else:
            return float(benchmark) * float(RISK_PARAMS_FIXED_OVERRIDES.get("trail_short", 1.0025))

# ========================
# Worker
# ========================

class StrategyTestWorker(QThread):

    # Qt signals used by GUI to monitor worker status/progress
    progress = pyqtSignal(int)
    progress_text = pyqtSignal(str)
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    finished_signal = pyqtSignal(object)
    finished_with_engine = pyqtSignal(object)
    error_signal = pyqtSignal(str)


    def _save_test_config_direct(self, test_id: int, strategy_name: str, settings: dict):
        """Minimalny zapis konfiguracji testu do Postgresa (fallback, gdy db.save_run_config nie istnieje)."""
        try:
            cfg_json = json.dumps(settings, ensure_ascii=False)
        except Exception:
            cfg_json = "{}"
        try:
            with self.db._conn() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.test_config_meta(
                        test_id INT PRIMARY KEY,
                        strategy_name TEXT,
                        symbols JSONB,
                        start_date TIMESTAMPTZ,
                        end_date TIMESTAMPTZ,
                        candle_interval TEXT,
                        status TEXT,
                        config JSONB,
                        inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
                # tu nadpisujemy tylko strategy_name + config, resztę zostawiamy jak jest
                cur.execute(
                    """
                    INSERT INTO public.test_config_meta (test_id, strategy_name, config)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (test_id) DO UPDATE SET
                        strategy_name = EXCLUDED.strategy_name,
                        config        = EXCLUDED.config;
                    """,
                    (int(test_id), strategy_name, cfg_json),
                )
        except Exception as e:
            logging.warning("[WORKER] _save_test_config_direct failed for test_id=%s: %s", test_id, e)

    def __init__(self, db, db_queue, symbols, strategy_class, bias=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.db_queue = db_queue
        self.symbols = symbols
        self.strategy_class = strategy_class
        self.bias = bias
        self.engine = None
        # in-memory storage of indicators per symbol (used when WRITE_INDICATORS_TO_DB is False)
        self.indicators_by_symbol = {}

    def _resolve_test_id(self):
        """
        Return current test_id preferring db.next_free_test_id() if available.
        """
        try:
            cur = getattr(self.db, "current_test_id", None)
            if isinstance(cur, int) and cur > 0:
                return cur
        except Exception:
            pass
        try:
            return int(self.db.next_free_test_id())
        except Exception:
            return 1

    # ---------- logging helpers ----------
    def _emit(self, msg):
        try:
            self.log.emit(msg); self.status.emit(msg)
        except Exception:
            pass
        logging.info(msg)

    def _emit_progress(self, symbol, i, total):
        pct = int((i + 1) * 100 / total) if total else 100
        try:
            now = _dt.now(_tz.utc).astimezone()
            ts = now.strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm
        except Exception:
            ts = ""
        prefix = f"{ts} " if ts else ""
        txt = f"{prefix}[WORKER] Progress {symbol}: {i + 1}/{total} ({pct}%)"
        try:
            self.progress.emit(pct)
        except Exception:
            pass
        try:
            self.progress_text.emit(txt)
        except Exception:
            pass
        # also log via standard logger
        logging.info(txt)


    # ---------- runner ----------
    def run(self):
        import gc, psutil, os, traceback, importlib, inspect
        from tools.altme_fng import fetch_history

        p = psutil.Process(os.getpid())
        try:
            tid = self._resolve_test_id()
            try:
                test_id = int(tid) if tid is not None else None
            except Exception:
                test_id = None
            self._emit(f"[WORKER] Strategy test test_id={test_id if test_id is not None else '?'} started")

            # Resolve strategy class
            if inspect.isclass(self.strategy_class):
                strategy_cls = self.strategy_class
            else:
                try:
                    mod = importlib.import_module(f"backtester.strategies.{str(self.strategy_class).lower()}")
                    strategy_cls = next((obj for name, obj in vars(mod).items() if isinstance(obj, type) and name.endswith("Strategy")), None)
                except Exception:
                    strategy_cls = None
            if strategy_cls is None:
                from backtester.strategies.rsi import RSIStrategy
                strategy_cls = RSIStrategy

            strategy = strategy_cls()
            engine = MultiSymbolEngine(strategy, self.db, self.db_queue, self.symbols, bias=self.bias)
            self.engine = engine
            # Persist full config snapshot for this test_id
            try:
                import backtester.strategies.rsi_config as rsi_conf
                settings = {}
                for k in dir(rsi_conf):
                    if k and k[0].isalpha() and k.isupper():
                        v = getattr(rsi_conf, k)
                        if isinstance(v, (int, float, bool, str, list, tuple, dict)):
                            settings[k] = v
                # prefer db.save_run_config if available, otherwise fallback
                if hasattr(self.db, "save_run_config"):
                    self.db.save_run_config(int(tid), getattr(strategy_cls, '__name__', 'RSI'), settings)
                else:
                    self._save_test_config_direct(int(tid), getattr(strategy_cls, '__name__', 'RSI'), settings)
            except Exception as e:
                self._emit(f"[WORKER] WARNING: couldn't persist test config for test_id={tid}: {e}")

            # Clear indicators table(s) so min/max reflect THIS run only
            try:
                for tname in set(engine.ind_tables_hist.values()):
                    self.db.clear_indicators_table(tname)
                    self._emit(f"[WORKER] Cleared indicator table {tname}")
            except Exception as _e:
                logging.warning(f"[WORKER] Could not clear indicator tables: {_e}")

            # ---------- GLOBAL F&G REFRESH (once) ----------
            try:
                all_ranges = []
                for s in self.symbols:
                    raw = self.db.get_candles(s, limit=MAX_WORKER_CANDLES)
                    df = _as_df(raw)
                    if df is not None and not df.empty:
                        try:
                            dft = _normalize_candles_df(df, s)
                            _idx = dft.index
                            if len(_idx):
                                all_ranges.append((_idx[0].normalize().date().isoformat(),
                                                   _idx[-1].normalize().date().isoformat()))
                        except Exception:
                            pass
                if all_ranges:
                    global_start = min(r[0] for r in all_ranges)
                    global_end   = max(r[1] for r in all_ranges)
                    rows = []
                    try:
                        rows = fetch_history(limit=0)
                    except Exception as _e:
                        logging.debug(f"[WORKER] altme fetch failed: {_e}")
                    if rows:
                        batch = [{"day": r["day"], "value": float(r["value"])}
                                 for r in rows if global_start <= r["day"] <= global_end]
                        with self.db._conn() as cur:
                            cur.execute("""
                                CREATE TABLE IF NOT EXISTS public.fear_greed (
                                    day DATE PRIMARY KEY,
                                    value DOUBLE PRECISION
                                )
                            """)
                            cur.execute("DELETE FROM public.fear_greed")
                            if batch:
                                cur.executemany(
                                    "INSERT INTO public.fear_greed(day, value) VALUES (%s, %s)",
                                    [(r["day"], r["value"]) for r in batch]
                                )
                    logging.debug(f"[WORKER] F&G global refresh range={global_start}.{global_end}")
            except Exception as _e:
                logging.debug(f"[WORKER] F&G global refresh skipped: {_e}")
            # ---------- /GLOBAL F&G REFRESH ----------

            # Global test range inferred from processed rows
            global_first_ts, global_last_ts = None, None
            interval_sec = None

            for symbol in self.symbols:
                ram_before = p.memory_info().rss / (1024 * 1024) if WORKER_RAM_DEBUG else None
                self._emit(f"[WORKER] Testing symbol {symbol}")

                raw = self.db.get_candles(symbol, limit=MAX_WORKER_CANDLES)
                df = _as_df(raw)

                # DEBUG: inspect raw candles coming from DB into the worker
                try:
                    # use module-level logging / pandas
                    if isinstance(df, pd.DataFrame):
                        logging.warning(
                            "[WORKER][DEBUG] RAW candles %s: rows=%d, cols=%s",
                            symbol,
                            len(df),
                            list(df.columns),
                        )
                        logging.warning(
                            "[WORKER][DEBUG] RAW dtypes %s: %s",
                            symbol,
                            {c: str(df[c].dtype) for c in df.columns},
                        )
                        logging.warning(
                            "[WORKER][DEBUG] RAW head %s:\n%s",
                            symbol,
                            df.head(5).to_string(),
                        )
                    else:
                        logging.warning("[WORKER][DEBUG] RAW candles %s: df is not a DataFrame (%r)", symbol, type(df))
                except Exception as _e:
                    # do not break the test if logging fails for any reason
                    try:
                        logging.warning("[WORKER][DEBUG] RAW candles logging failed for %s: %s", symbol, _e)
                    except Exception:
                        pass

                if df is None or df.empty:
                    self._emit(f"[WORKER] No data for {symbol} – skipping")
                    continue

                # ---- FIX: robust time handling (accepts timestamp/TS/index) ----
                try:
                    df = _normalize_candles_df(df, symbol)
                except KeyError:
                    self._emit(f"[WORKER] No 'close_time' (or alias) for {symbol} – skipping")
                    continue

                # Compute indicators (strategy is the single source of truth)
                with PerfTimer(f"{symbol} – compute_indicators", PERF_DEBUG):
                    if hasattr(strategy, "compute_indicators_segmented"):
                        df = strategy.compute_indicators_segmented(df)
                    else:
                        df = strategy.compute_indicators(df)
                if df is None or df.empty:
                    self._emit(f"[WORKER] Indicators empty for {symbol} – skipping")
                    continue


                # FEAR_GREED merge
                with PerfTimer(f"{symbol} – merge_fear_greed", PERF_DEBUG):
                    try:
                        # Cache daily Fear&Greed series once per worker (shared across symbols)
                        if not hasattr(self, "_fear_greed_series") or self._fear_greed_series is None:
                            fg_rows = self.db._fetchall(
                                "SELECT day AS date, value::float8 AS value FROM public.fear_greed ORDER BY day"
                            )
                            fg = pd.DataFrame(fg_rows)
                            if not fg.empty:
                                # konwertujemy do daty bez strefy czasowej i normalizujemy do północy
                                fg["date"] = pd.to_datetime(fg["date"], errors="coerce")
                                fg["date"] = fg["date"].dt.tz_localize(None).dt.normalize()
                                fg = fg.dropna(subset=["date"]).sort_values("date")
                                fg["FEAR_GREED"] = fg["value"]
                                self._fear_greed_series = (
                                    fg.drop_duplicates(subset=["date"], keep="last")
                                      .set_index("date")["FEAR_GREED"]
                                )
                            else:
                                self._fear_greed_series = None

                        last_series = getattr(self, "_fear_greed_series", None)
                        if last_series is not None and len(last_series) > 0:
                            # mapujemy po dacie bez strefy czasowej: DATE(close_time) -> FEAR_GREED
                            idx_dates = pd.to_datetime(df.index)
                            try:
                                idx_dates = idx_dates.tz_localize(None)
                            except Exception:
                                # jeśli już bez tz, to w porządku
                                pass
                            idx_dates = idx_dates.normalize()
                            mapped = last_series.reindex(idx_dates).ffill()
                            df["FEAR_GREED"] = mapped.values
                    except Exception as _e:
                        logging.debug(f"[WORKER] FEAR_GREED merge skipped: {_e}")
                # ensure risk-limit columns are present in indicators DataFrame
                try:
                    for _col in ("TP", "SL", "TS", "TS_BENCHMARK"):
                        if _col not in df.columns:
                            df[_col] = np.nan
                except Exception:
                    pass

                # keep full indicators DataFrame in memory (even if not writing to DB)
                try:
                    self.indicators_by_symbol[symbol] = df
                except Exception:
                    pass
                # default schema holders; may stay unused if WRITE_INDICATORS_TO_DB is False
                ordered_cols = None
                dynamic_cols = set()
                table_name = INDICATORS_TABLE

                if WRITE_INDICATORS_TO_DB:
                    # Dynamic indicator schema from first row of strategy output
                    dynamic_cols = {
                        "inserted_at", "close_price", "TP", "SL", "TS", "TS_BENCHMARK",
                        "TP_MIN", "TP_MAX", "SL_MIN", "SL_MAX",
                    }
                    if hasattr(strategy, "extract_indicator_values"):
                        first_vals = strategy.extract_indicator_values(df.iloc[0])
                    else:
                        first_vals = df.iloc[0].to_dict()
                    if "FEAR_GREED" in df.columns:
                        dynamic_cols |= {"FEAR_GREED"}
                    dynamic_cols |= _numeric_keys_from(first_vals)

                    # establish stable ordered column list once (will only change when dynamic_cols grows)
                    ordered_cols = order_columns(dynamic_cols)
                    dynamic_cols = set(ordered_cols)

                    table_name = INDICATORS_TABLE
                    engine.db.create_indicators_table(INDICATORS_TABLE, ordered_cols, table_type="historical")

                # Per-symbol state
                position = None
                trades, trade_rows, indicator_rows = [], [], []
                sym_first_ts, sym_last_ts = None, None

                t_calc_acc = 0.0; t_logic_acc = 0.0; t_db_acc = 0.0
                loop_start = time.perf_counter() if PERF_DEBUG else None
                chunk_start = loop_start
                has_eiv = hasattr(strategy, "extract_indicator_values")
                eiv = getattr(strategy, "extract_indicator_values", None)
                no_new_keys_counter = 0
                schema_locked = False
                for i, (idx, row) in enumerate(df.iterrows()):
                    if sym_first_ts is None:
                        sym_first_ts = idx
                    sym_last_ts = idx

                    row_dict = row.to_dict()
                    vals = eiv(row) if has_eiv else row_dict

                    # PERF: per-chunk timing to investigate slowdown after first ~2000 rows
                    if PERF_DEBUG and chunk_start is not None and (
                        (i + 1) % PERF_CHUNK_ROWS == 0 or i == len(df) - 1
                    ):
                        now = time.perf_counter()
                        elapsed_chunk = now - chunk_start
                        # Czytelny log dla analizy: czas chunku + skumulowany czas DB put
                        self._emit(
                            f"[Perf][CHUNK] {symbol} chunk_end={i+1}, "
                            f"elapsed={elapsed_chunk:.4f}s, db_put_acc={t_db_acc:.4f}s"
                        )
                        # Dotychczasowy log na PerfTimer
                        perf_log(f"{symbol} – main_loop_chunk_{i+1}", chunk_start, enabled=True)
                        chunk_start = now


                    # keep FEAR_GREED if present (prefer vals, fallback to raw row_dict)
                    try:
                        fg_val = vals.get("FEAR_GREED")
                        if fg_val is None and "row_dict" in locals():
                            fg_val = row_dict.get("FEAR_GREED")
                        if fg_val is not None and not (isinstance(fg_val, float) and np.isnan(fg_val)):
                            vals.setdefault("FEAR_GREED", float(fg_val))
                    except Exception:
                        pass

                    # Expand schema on the fly as strategy starts emitting new numeric keys.
                    # After a while without new keys we stop scanning vals to save CPU.
                    if WRITE_INDICATORS_TO_DB and not schema_locked and dynamic_cols:
                        new_keys = _numeric_keys_from(vals) - set(dynamic_cols)
                        if new_keys:
                            dynamic_cols |= new_keys
                            ordered_cols = order_columns(dynamic_cols)
                            engine.db.create_indicators_table(INDICATORS_TABLE, ordered_cols, table_type="historical")
                            no_new_keys_counter = 0
                        else:
                            no_new_keys_counter += 1
                            # heuristic: if there's no new key for 1000 candles, treat schema as stable
                            if no_new_keys_counter >= 1000:
                                schema_locked = True

                    # Price
                    price_raw = row_dict.get("close")
                    if price_raw is None or (isinstance(price_raw, float) and pd.isna(price_raw)):
                        # try fallbacks
                        price_raw = row_dict.get("CLOSE") or row_dict.get("close_price")
                    if price_raw is None:
                        continue
                    price = float(price_raw)

                    atr_here = _safe_float(vals.get("ATR")) or _safe_float(vals.get("atr")) or 0.0

                                                            # trailing stop: arm when TP hit
                    if position is not None and TRAILING_STOP_ENABLED and not bool(position.get("ts_armed", False)):
                        side = position["side"]
                        tp = _safe_float(position.get("tp_level"))
                        if tp is not None:
                            hit = (side == "long" and price >= tp) or (side == "short" and price <= tp)
                            if hit:
                                position["ts_armed"] = True
                                position["ts_armed_at"] = idx
                                # remember bar when TS was armed (for cooldown)
                                position["ts_last_update_idx"] = idx
                                position["tp_level"] = None
                                position["sl_level"] = None
                                # benchmark at the moment TS is armed
                                position["ts_benchmark"] = price
                                position["trailing_stop"] = _ts_from_benchmark(
                                    side,
                                    price,
                                    atr_here,
                                    position.get("entry_price", price),
                                )
                                # store initial TS snapshot (used later for stats)
                                if position.get("initial_benchmark") is None:
                                    position["initial_benchmark"] = position.get("ts_benchmark")
                                if position.get("initial_ts") is None:
                                    position["initial_ts"] = position.get("trailing_stop")

                    # move trailing benchmark if armed
                    if position is not None and bool(position.get("ts_armed", False)):
                        side = position["side"]
                        old_bench = _safe_float(position.get("ts_benchmark"))
                        bench = old_bench or price
                        if side == "long" and price > bench:
                            bench = price
                        elif side == "short" and price < bench:
                            bench = price
                        position["ts_benchmark"] = bench
                        position["trailing_stop"] = _ts_from_benchmark(
                            side,
                            bench,
                            atr_here,
                            position.get("entry_price", bench),
                        )
                        # if benchmark actually moved, remember bar index (for TS cooldown)
                        if old_bench is None or bench != old_bench:
                            position["ts_last_update_idx"] = idx

                    # StopLoss updater: static jump (BE/custom) + dynamic SL
                    if position is not None and bool(position.get("sl_updater_enabled", False)) and not bool(position.get("ts_armed", False)):
                        side_sl = position.get("side")
                        if side_sl in ("long", "short"):
                            entry_ref = _safe_float(position.get("entry_price_raw")) or _safe_float(position.get("entry_price"))
                            tp_open = _safe_float(position.get("tp_open"))
                            sl_initial = _safe_float(position.get("sl_initial"))
                            current_sl = _safe_float(position.get("sl_level"))
                            if current_sl is None and sl_initial is not None:
                                current_sl = sl_initial

                            if entry_ref is not None and sl_initial is not None:
                                # basic OHLC for SL updater (independent of unified close logic block)
                                high = _safe_float(row_dict.get("high")) or _safe_float(row_dict.get("HIGH")) or price
                                low = _safe_float(row_dict.get("low")) or _safe_float(row_dict.get("LOW")) or price

                                # --- static jump: move SL once when price reaches trigger fraction of entry->TP range ---
                                if bool(position.get("sl_jump_enabled", False)) and not bool(position.get("sl_jump_triggered", False)):
                                    if SLU_RANGE_TYPE == "entry_to_tp":
                                        tp_ref = tp_open
                                    else:
                                        tp_ref = None

                                    if tp_ref is not None and SLU_TRIGGER > 0.0 and tp_ref != entry_ref:
                                        if side_sl == "long":
                                            range_pts = tp_ref - entry_ref
                                            if range_pts > 0:
                                                trigger_price = entry_ref + SLU_TRIGGER * range_pts
                                                move_to = entry_ref + SLU_MOVE_TO * range_pts
                                                if high is not None and high >= trigger_price:
                                                    new_sl = move_to
                                                    # SL tylko w górę, nigdy poniżej sl_initial ani poniżej aktualnego SL
                                                    floor = max(sl_initial, current_sl if current_sl is not None else sl_initial)
                                                    if new_sl < floor:
                                                        new_sl = floor
                                                    position["sl_level"] = new_sl
                                                    position["sl_floor"] = new_sl
                                                    position["sl_jump_triggered"] = True
                                                    current_sl = new_sl
                                        elif side_sl == "short":
                                            range_pts = entry_ref - tp_ref
                                            if range_pts > 0:
                                                trigger_price = entry_ref - SLU_TRIGGER * range_pts
                                                move_to = entry_ref - SLU_MOVE_TO * range_pts
                                                if low is not None and low <= trigger_price:
                                                    new_sl = move_to
                                                    # SL tylko w dół, nigdy powyżej sl_initial ani powyżej aktualnego SL
                                                    floor = min(sl_initial, current_sl if current_sl is not None else sl_initial)
                                                    if new_sl > floor:
                                                        new_sl = floor
                                                    position["sl_level"] = new_sl
                                                    position["sl_floor"] = new_sl
                                                    position["sl_jump_triggered"] = True
                                                    current_sl = new_sl

                                # --- dynamic SL: background mover based on new local HIGH/LOW ---
                                if bool(position.get("sl_dyn_enabled", False)):
                                    extreme = _safe_float(position.get("sl_dyn_extreme"))
                                    if extreme is None:
                                        extreme = entry_ref

                                    # long: track new highs
                                    if side_sl == "long" and high is not None:
                                        if high > extreme:
                                            extreme = high
                                            position["sl_dyn_extreme"] = extreme
                                            # podnosimy SL o różnicę między HIGH a ENTRY (na bazie sl_initial)
                                            sl_candidate = sl_initial + (extreme - entry_ref)
                                            floor = max(sl_initial, _safe_float(position.get("sl_floor")) or sl_initial)
                                            new_sl = sl_candidate
                                            if new_sl < floor:
                                                new_sl = floor
                                            if current_sl is not None and new_sl < current_sl:
                                                new_sl = current_sl
                                            position["sl_level"] = new_sl
                                            current_sl = new_sl

                                    # short: track new lows
                                    if side_sl == "short" and low is not None:
                                        if low < extreme:
                                            extreme = low
                                            position["sl_dyn_extreme"] = extreme
                                            # obniżamy SL o różnicę między ENTRY a LOW (na bazie sl_initial)
                                            sl_candidate = sl_initial + (extreme - entry_ref)
                                            floor = min(sl_initial, _safe_float(position.get("sl_floor")) or sl_initial)
                                            new_sl = sl_candidate
                                            if new_sl > floor:
                                                new_sl = floor
                                            if current_sl is not None and new_sl > current_sl:
                                                new_sl = current_sl
                                            position["sl_level"] = new_sl
                                            current_sl = new_sl

# unified close logic: CLOSE_AFTER_X_CANDLES + strategy close_signals + risk limits
                    if position is not None:
                        side = position.get("side")
                        # increment bars_open counter for this position
                        position["bars_open"] = int(position.get("bars_open", 0) or 0) + 1
                        bars_open = position["bars_open"]

                        # helper to collect close candidates
                        candidates = []

                        def _add_close_candidate(reason: str, base_price):
                            try:
                                bp = _safe_float(base_price)
                            except Exception:
                                bp = None
                            if bp is None or not np.isfinite(bp):
                                return
                            candidates.append({
                                "reason": str(reason),
                                "side": side,
                                "base_price": float(bp),
                            })

                        # basic OHLC for intra-bar checks (fallback to close if missing)
                        o = _safe_float(row_dict.get("open"))
                        h = _safe_float(row_dict.get("high"))
                        l = _safe_float(row_dict.get("low"))
                        c = float(price)
                        if not np.isfinite(o):
                            o = c
                        if not np.isfinite(h):
                            h = c
                        if not np.isfinite(l):
                            l = c

                        exec_mode = str(CLOSE_EXECUTION_TYPE or "on_candle_close").lower()
                        side_l = (side or "").lower()

                        # --- layer 3: time-based close_after_x_candles ---
                        if CLOSE_AFTER_X_CANDLES > 0 and bars_open >= CLOSE_AFTER_X_CANDLES:
                            _add_close_candidate("close_after_x_candles", c)

                        # --- risk limits: TS / SL / TP ---
                        tp_level = _safe_float(position.get("tp_level"))
                        sl_level = _safe_float(position.get("sl_level"))
                        ts_level = _safe_float(position.get("trailing_stop")) if bool(position.get("ts_armed", False)) else None

                        # trailing stop (if armed)
                        if ts_level is not None:
                            if exec_mode == "on_crossover":
                                # cooldown: ignore TS on the bar where it was (re)armed or benchmark moved
                                ts_last_upd = position.get("ts_last_update_idx") or position.get("ts_armed_at")
                                skip_ts_this_bar = (ts_last_upd is not None and ts_last_upd == idx)
                                if not skip_ts_this_bar:
                                    if side_l == "long" and l <= ts_level <= h:
                                        _add_close_candidate("trailing_stop_long", ts_level)
                                    elif side_l == "short" and l <= ts_level <= h:
                                        _add_close_candidate("trailing_stop_short", ts_level)
                            else:
                                if side_l == "long" and c <= ts_level:
                                    _add_close_candidate("trailing_stop_long", c)
                                elif side_l == "short" and c >= ts_level:
                                    _add_close_candidate("trailing_stop_short", c)

                        # hard stop-loss
                        if sl_level is not None:
                            if exec_mode == "on_crossover":
                                if side_l == "long" and l <= sl_level <= h:
                                    _add_close_candidate("stop_loss_long", sl_level)
                                elif side_l == "short" and l <= sl_level <= h:
                                    _add_close_candidate("stop_loss_short", sl_level)
                            else:
                                if side_l == "long" and c <= sl_level:
                                    _add_close_candidate("stop_loss_long", c)
                                elif side_l == "short" and c >= sl_level:
                                    _add_close_candidate("stop_loss_short", c)

                        # take-profit as hard limit only when trailing stop is disabled
                        if not TRAILING_STOP_ENABLED and tp_level is not None:
                            if exec_mode == "on_crossover":
                                if side_l == "long" and l <= tp_level <= h:
                                    _add_close_candidate("take_profit_long", tp_level)
                                elif side_l == "short" and l <= tp_level <= h:
                                    _add_close_candidate("take_profit_short", tp_level)
                            else:
                                if side_l == "long" and c >= tp_level:
                                    _add_close_candidate("take_profit_long", c)
                                elif side_l == "short" and c <= tp_level:
                                    _add_close_candidate("take_profit_short", c)

                        # --- strategy-level close_signals (BB/RSI/FEAR_GREED) ---
                        # DEBUG: log BB_close line vs price na każdej świecy
                        try:
                            bb_line = None
                            if hasattr(strategy, "_bb_close_line") and position is not None:
                                bb_line = strategy._bb_close_line(row, side)
                            self._emit(
                                "[BB_DEBUG][CHECK][%s] ts=%s side=%s price=%.4f bb_line=%s init_side=%s"
                                % (
                                    symbol,
                                    str(idx),
                                    str(side),
                                    float(c),
                                    str(bb_line),
                                    str(position.get("bb_close_initial_side") if position is not None else None),
                                )
                            )
                        except Exception:
                            pass

                        close_sig = None
                        try:
                            if hasattr(strategy, "close_position_signal"):
                                close_sig = strategy.close_position_signal(row, position)
                        except Exception:
                            close_sig = None

                        if close_sig:
                            reason = close_sig.get("signal_type", "strategy_close")

                            # DEBUG: sygnał trafiony
                            try:
                                self._emit(
                                    "[BB_DEBUG][HIT][%s] ts=%s side=%s price=%.4f reason=%s"
                                    % (
                                        symbol,
                                        str(idx),
                                        str(side),
                                        float(c),
                                        str(reason),
                                    )
                                )
                            except Exception:
                                pass

                            # map strategy close signals to price-level candidates
                            if reason == "BB_close":
                                base = None
                                try:
                                    if hasattr(strategy, "_bb_close_line"):
                                        base = strategy._bb_close_line(row, side)
                                except Exception:
                                    base = None
                                if exec_mode == "on_crossover" and base is not None and np.isfinite(_safe_float(base)):
                                    _add_close_candidate("BB_close", base)
                                else:
                                    _add_close_candidate("BB_close", c)
                            elif reason == "RSI_close":
                                _add_close_candidate("RSI_close", c)
                            elif reason == "FEAR_GREED_close":
                                _add_close_candidate("FEAR_GREED_close", c)
                            else:
                                _add_close_candidate(reason, c)

                        # --- choose worst (least profitable) candidate and close position ---
                        if candidates:
                            # apply global slippage
                            for cand in candidates:
                                cand["exec_price"] = float(_apply_slippage(cand["base_price"], cand["side"]))

                            chosen = None
                            if side_l == "long":
                                chosen = min(candidates, key=lambda x: x["exec_price"])
                            elif side_l == "short":
                                chosen = max(candidates, key=lambda x: x["exec_price"])
                            else:
                                # fallback: just take first
                                chosen = candidates[0]

                            exec_price = float(chosen["exec_price"])
                            reason = chosen["reason"]

                            fake = dict(row_dict)
                            fake["close_time"] = idx  # ALWAYS candle close time
                            fake["close"] = exec_price

                            try:
                                tr = engine.close_position(engine.contexts[symbol], fake, reason)
                            except Exception:
                                tr = None

                            if tr:
                                tr["symbol"] = symbol
                                if "entry_timestamp" in tr and "open_time" not in tr:
                                    tr["open_time"] = tr["entry_timestamp"]
                                if "exit_timestamp" in tr and "close_time" not in tr:
                                    tr["close_time"] = tr["exit_timestamp"]
                                if 'test_id' not in tr and 'test_id' in locals() and test_id is not None:
                                    tr['test_id'] = int(test_id)
                                # make sure risk snapshot fields are attached to the trade row
                                try:
                                    for _k in ("tp_open", "sl_open", "initial_benchmark", "initial_ts"):
                                        if position is not None:
                                            _val = position.get(_k)
                                        else:
                                            _val = None
                                        if _val is not None and _k not in tr:
                                            tr[_k] = _val
                                except Exception:
                                    pass

                                trade_rows.append(tr); trades.append(tr)
                                try:
                                    side_str = 'LONG' if str(tr.get('side')) == 'long' else 'SHORT'
                                    price_f = float(exec_price)
                                    reason_str = tr.get('close_reason') or tr.get('close_signal_type') or reason
                                    self._emit("[CLOSE][%s][%s]#%s %s close_px=%s reason=%s amount=%s fee=%s pnl=%s" % (
                                        str(fake["close_time"]), str(symbol), str(tr.get('trade_id')), side_str, price_f, str(reason_str),
                                        str(tr.get('amount')), str(tr.get('fee')), str(tr.get('pnl'))
                                    ))
                                except Exception:
                                    pass

                            # after successful close, clear position both locally and in engine context
                            position = None
                            ctx = engine.contexts.get(symbol)
                            if ctx is not None:
                                ctx.current_position = None


                    # OPEN signal
                    if position is None:
                        try:
                            open_sig = strategy.open_position_signal(row, None)
                        except Exception as e:
                            open_sig = None
                            logging.debug(f"[WORKER] open_position_signal error for {symbol}: {e}", exc_info=True)

                        open_sig = _apply_bias_to_open_signal(open_sig, self.bias)

                        if open_sig:
                            side = (open_sig.get("side") or open_sig.get("direction") or "").lower()
                            if not side:
                                side = "long" if "long" in str(open_sig.get("signal_type", "")).lower() else "short"
                            if side in ("long", "short"):
                                # reference entry is raw candle close (used for TP/SL calculations)
                                entry_ref = price
                                # compute TP/SL (and optionally TS) using shared helper, like in legacy worker
                                tp, sl, _ts_dummy = _compute_open_risk(side, entry_ref, atr_here)
                                # apply optional entry slippage for actual fill price
                                entry = _apply_entry_slippage(entry_ref, side)
                                # build position state compatible with engine.close_position and old analytics
                                trade_id = None
                                ctx = engine.contexts.get(symbol)
                                if ctx is not None and hasattr(ctx, "next_trade_id"):
                                    try:
                                        trade_id = ctx.next_trade_id()
                                    except Exception:
                                        trade_id = None

                                position = {
                                    "side": side,
                                    "entry_price": entry,
                                    "entry_price_raw": entry_ref,
                                    "entry_timestamp": idx,
                                    "entry_atr": atr_here,
                                    "amount": float(open_sig.get("amount", 1.0)),
                                    "trade_id": trade_id,
                                    "tp_level": tp,
                                    "sl_level": sl,
                                    # trailing stop state
                                    "ts_armed": False,
                                    "trailing_stop": None,
                                    "ts_benchmark": None,
                                    "ts_armed_at": None,
                                    "ts_last_update_idx": None,
                                    # original TP/SL snapshot (as computed from entry_ref)
                                    "tp_open": tp,
                                    "sl_open": sl,
                                    "initial_benchmark": None,
                                    "initial_ts": None,
                                    # SL updater state
                                    "sl_initial": sl,
                                    "sl_updater_enabled": SLU_ENABLED,
                                    "sl_jump_enabled": SLU_ENABLED and SLU_STATIC_ENABLED,
                                    "sl_jump_triggered": False,
                                    "sl_dyn_enabled": SLU_ENABLED and SLU_DYNAMIC,
                                    "sl_dyn_extreme": entry_ref,
                                    "sl_floor": sl,
                                    "signal_type": open_sig.get("signal_type", ""),
                                    # pola pod BB_close (snapshot przy wejściu)
                                    "bb_close_initial_side": None,
                                    "bb_close_entry_line": None,
                                }
                                if ctx is not None:
                                    ctx.current_position = position

                                # snapshot BB_close relation at entry (if supported)
                                try:
                                    if hasattr(strategy, "compute_bb_close_initial_side"):
                                        init_side, init_line = strategy.compute_bb_close_initial_side(row, side)
                                        position["bb_close_initial_side"] = init_side
                                        position["bb_close_entry_line"] = init_line
                                except Exception:
                                    pass

                                # DEBUG: log BB_close snapshot na wejściu
                                try:
                                    self._emit(
                                        "[BB_DEBUG][OPEN][%s] side=%s entry=%.4f bb_line=%s init_side=%s"
                                        % (
                                            symbol,
                                            side,
                                            float(entry),
                                            str(position.get("bb_close_entry_line")),
                                            str(position.get("bb_close_initial_side")),
                                        )
                                    )
                                except Exception:
                                    pass

                                try:
                                    self._emit("[OPEN][%s][%s]#%s %s entry=%s tp=%s sl=%s amount=%s" % (
                                        str(idx), str(symbol), str(trade_id), side.upper(), float(entry), str(tp), str(sl),
                                        str(position.get("amount"))
                                    ))
                                except Exception:
                                    pass


                    if WRITE_INDICATORS_TO_DB and ordered_cols:
                        to_store = {name: None for name in ordered_cols}
                        to_store["close_price"] = float(price)

                        # zawsze zapisuj czas w UTC (ISO, sekundy)
                        to_store["inserted_at"] = _dt.now(_tz.utc).isoformat(timespec="seconds")

                        # --- DOPISZ KLUCZE CZASOWE (spójne z PK: symbol, open_time, close_time) ---
                        _ot = row_dict.get("open_time", None)
                        _ct = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx

                        to_store["open_time"] = _ot
                        to_store["close_time"] = _ct

                        # 'timestamp' = close_time w sekundach UNIX (UTC)
                        _ts = None
                        try:
                            # jeśli indeks jest już Timestamp z tz, korzystamy bezpośrednio
                            if isinstance(idx, pd.Timestamp) and idx.tz is not None:
                                _cts = idx
                            else:
                                _cts = pd.Timestamp(_ct)
                            if _cts.tzinfo is None:
                                _cts = _cts.tz_localize("UTC")
                            else:
                                _cts = _cts.tz_convert("UTC")
                            _ts = int(_cts.value // 10 ** 9)
                        except Exception:
                            _ts = None
                        to_store["timestamp"] = _ts

                        # pozycja/TS/TP/SL jak było
                        if position is not None and bool(position.get("ts_armed", False)):
                            to_store["TP"] = None
                            to_store["SL"] = None
                            to_store["TS"] = position.get("trailing_stop")
                            to_store["TS_BENCHMARK"] = position.get("ts_benchmark")
                            to_store["TP_MIN"] = None
                            to_store["TP_MAX"] = None
                            to_store["SL_MIN"] = None
                            to_store["SL_MAX"] = None
                        elif position is not None:
                            to_store["TP"] = position.get("tp_level")
                            to_store["SL"] = position.get("sl_level")
                            to_store["TS"] = None
                            to_store["TS_BENCHMARK"] = None
                        else:
                            to_store["TP"] = None
                            to_store["SL"] = None
                            to_store["TS"] = None
                            to_store["TS_BENCHMARK"] = None

                        # mirror risk limits into in-memory indicators DataFrame
                        try:
                            if "TP" in df.columns:
                                if position is not None and bool(position.get("ts_armed", False)):
                                    df.at[idx, "TP"] = np.nan
                                    df.at[idx, "SL"] = np.nan
                                    df.at[idx, "TS"] = _safe_float(position.get("trailing_stop"))
                                    df.at[idx, "TS_BENCHMARK"] = _safe_float(position.get("ts_benchmark"))
                                elif position is not None:
                                    df.at[idx, "TP"] = _safe_float(position.get("tp_level"))
                                    df.at[idx, "SL"] = _safe_float(position.get("sl_level"))
                                    df.at[idx, "TS"] = np.nan
                                    df.at[idx, "TS_BENCHMARK"] = np.nan
                                else:
                                    pass
                        except Exception:
                            pass

                        # copy strategy indicators into row payload (no hard-coded names)
                        for k, v in (vals or {}).items():
                            fv = _safe_float(v)
                            if fv is None:
                                continue
                            if k not in ordered_cols:
                                dynamic_cols.add(k)
                                ordered_cols = order_columns(dynamic_cols)
                                engine.db.create_indicators_table(INDICATORS_TABLE, ordered_cols, table_type="historical")
                                to_store.update({name: None for name in ordered_cols if name not in to_store})
                            to_store[k] = fv

                        indicator_rows.append({
                            "symbol": symbol,
                            **to_store,
                        })

                    # flush batch
                    if WRITE_INDICATORS_TO_DB and len(indicator_rows) >= INDICATOR_FLUSH_ROWS:
                        _t_db0 = time.perf_counter()
                        self.db_queue.put({
                            "type": "insert_indicator_rows",
                            "table_name": table_name,
                            "rows": indicator_rows,
                            "indicator_names": ordered_cols
                        })
                        dt_db = time.perf_counter() - _t_db0
                        t_db_acc += dt_db
                        perf_log(f"{symbol} – db_queue.put(indicators_flush)", _t_db0, enabled=PERF_DEBUG)
                        indicator_rows = []

                        # PERF: rozmiar kolejki po flushu batcha wskaźników
                        if PERF_DEBUG:
                            try:
                                qsz = self.db_queue.qsize()
                                logging.warning("[Perf][QUEUE] size after flush for %s: %d", symbol, qsz)
                            except Exception:
                                pass


                    if (i % 200) == 0 or i == (len(df) - 1):
                        self._emit_progress(symbol, i, len(df))

                # force close at end
                if position is not None:
                    fake = df.iloc[-1].to_dict(); fake["close_time"] = df.index[-1]; fake["close"] = float(df["close"].iloc[-1])
                    tr = engine.close_position(engine.contexts[symbol], fake, "end_of_data")
                    if tr:
                        tr["symbol"] = symbol
                        # map engine timestamps to DB fields expected by db_pg.insert_trade_rows
                        if "entry_timestamp" in tr and "open_time" not in tr:
                            tr["open_time"] = tr["entry_timestamp"]
                        if "exit_timestamp" in tr and "close_time" not in tr:
                            tr["close_time"] = tr["exit_timestamp"]
                        if 'test_id' not in tr and 'test_id' in locals() and test_id is not None:
                            tr['test_id'] = int(test_id)
                        # make sure risk snapshot fields are attached to the trade row
                        if position is not None:
                            for _k in ("tp_open", "sl_open", "initial_benchmark", "initial_ts"):
                                _val = position.get(_k)
                                if _val is not None and _k not in tr:
                                    tr[_k] = _val
                        trade_rows.append(tr); trades.append(tr)
                        try:
                            side_str = 'LONG' if str(tr.get('side'))=='long' else 'SHORT'
                            price_f = float(fake["close"])
                            reason = tr.get('close_reason') or tr.get('close_signal_type')
                            self._emit("[CLOSE][%s][%s]#%s %s close_px=%s reason=%s amount=%s fee=%s pnl=%s" % (
                                str(fake["close_time"]), str(symbol), str(tr.get('trade_id')), side_str, price_f, str(reason),
                                str(tr.get('amount')), str(tr.get('fee')), str(tr.get('pnl'))
                            ))
                        except Exception:
                            pass
                    position = None
                    engine.contexts[symbol].current_position = None

                # flush DB writes
                if trade_rows:
                    _t_db0 = time.perf_counter()
                    self.db_queue.put({"type": "insert_trade_rows", "rows": trade_rows})
                    dt_db = time.perf_counter() - _t_db0
                    t_db_acc += dt_db
                    perf_log(f"{symbol} – db_queue.put(trades_final)", _t_db0, enabled=PERF_DEBUG)

                if WRITE_INDICATORS_TO_DB and indicator_rows:
                    _t_db0 = time.perf_counter()
                    self.db_queue.put({
                        "type": "insert_indicator_rows",
                        "table_name": table_name,
                        "rows": indicator_rows,  # LISTA dictów
                        "indicator_names": ordered_cols
                    })
                    dt_db = time.perf_counter() - _t_db0
                    t_db_acc += dt_db
                    perf_log(f"{symbol} – db_queue.put(indicators_final)", _t_db0, enabled=PERF_DEBUG)

                    # PERF: rozmiar kolejki po finalnym flushu dla symbolu
                    if PERF_DEBUG:
                        try:
                            qsz = self.db_queue.qsize()
                            logging.warning("[Perf][QUEUE] size after flush for %s: %d", symbol, qsz)
                        except Exception:
                            pass


                engine.trades[symbol] = trades
                engine.contexts[symbol].trades = trades
                if PERF_DEBUG:
                    if loop_start is not None:
                        total_elapsed = time.perf_counter() - loop_start
                        t_calc_acc = max(0.0, total_elapsed - t_db_acc)
                    self._emit(f"[Perf][SUM] {symbol}: calc={t_calc_acc:.4f}s, db_put={t_db_acc:.4f}s, rows={len(df)}")
                    if loop_start is not None:
                        perf_log(f"{symbol} – main_loop", loop_start, enabled=True)

                # update global range + sample interval
                try:
                    if sym_first_ts is not None:
                        global_first_ts = sym_first_ts if global_first_ts is None else min(global_first_ts, sym_first_ts)
                    if sym_last_ts is not None:
                        global_last_ts = sym_last_ts if global_last_ts is None else max(global_last_ts, sym_last_ts)
                    if df.index.size >= 2:
                        diffs = (df.index.view("int64")//10**9)
                        import pandas as _pd2
                        dd = _pd2.Series(diffs).diff().dropna().astype(int)
                        if not dd.empty:
                            interval_sec = int(dd.mode().iloc[0])
                except Exception:
                    pass

                if WORKER_RAM_DEBUG and ram_before is not None:
                    gc.collect()
                    ram_after = p.memory_info().rss / (1024 * 1024)
                    self._emit(f"[RAM][WORKER][{symbol}] +{ram_after - ram_before:.2f} MB | Total: {ram_after:.2f} MB")

            # Emit + persist test metadata
            try:
                def _fmt_ts(x):
                    try:
                        return pd.to_datetime(x, utc=True).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        return None

                start_s = _fmt_ts(global_first_ts)
                end_s   = _fmt_ts(global_last_ts)
                s = interval_sec or 0
                if s % 3600 == 0 and s > 0:
                    interval_str = f"{s // 3600}h"
                elif s % 60 == 0 and 0 < s < 3600:
                    interval_str = f"{s // 60}m"
                elif s > 0:
                    interval_str = f"{s}s"
                else:
                    interval_str = None

                # 1) asynchroniczny sygnał przez db_queue (jak wcześniej)
                _t_db0 = time.perf_counter()
                self.db_queue.put({
                    "type": "update_test_meta",
                    "symbols": list(self.symbols or []),
                    "start_date": start_s,
                    "end_date": end_s,
                    "candle_interval": interval_str
                })
                perf_log("db_queue.put(update_test_meta)", _t_db0, enabled=PERF_DEBUG)

                # 2) bezpośredni zapis do public.test_config_meta
                try:
                    tid_for_meta = None
                    try:
                        # test_id z początku run()
                        tid_for_meta = int(test_id) if test_id is not None else None
                    except Exception:
                        tid_for_meta = None

                    if tid_for_meta is not None and hasattr(self.db, "upsert_test_config_metadata"):
                        self.db.upsert_test_config_metadata(
                            test_id=tid_for_meta,
                            symbols=list(self.symbols or []),
                            start_date=start_s,
                            end_date=end_s,
                            candle_interval=interval_str,
                            status="finished",
                        )
                except Exception as _e:
                    logging.warning("[WORKER] couldn't upsert test_config_meta for test_id=%s: %s",
                                    test_id, _e)
            except Exception as e:
                logging.error("[WORKER] error while emitting test metadata: %s", e, exc_info=True)


            # Emit per-symbol stats; GUI will call db.replace_stats_rows
            try:
                rows = []
                for sym, arr in (engine.trades or {}).items():
                    if not arr:
                        rows.append({"symbol": sym, "trades": 0})
                        continue
                    pnls = _nanfilter([t.get("pnl") for t in arr])
                    fees = _nanfilter([t.get("fee") for t in arr])
                    wins = [p for p in pnls if p > 0]
                    losses = [p for p in pnls if p < 0]
                    rows.append({
                        "symbol": sym,
                        "trades": len(arr),
                        "pnl_sum": sum(pnls) if pnls else 0.0,
                        "pnl_avg": _avg(pnls) or 0.0,
                        "fee_sum": sum(fees) if fees else 0.0,
                        "winrate": (len(wins) * 100.0 / len(arr)) if arr else 0.0,
                        "avg_win": _avg(wins) or 0.0,
                        "avg_loss": _avg(losses) or 0.0,
                    })
                _t_db0 = time.perf_counter()
                self.db_queue.put({"type": "replace_stats_rows", "rows": rows, "test_id": test_id})
                perf_log("db_queue.put(replace_stats_rows)", _t_db0, enabled=PERF_DEBUG)
            except Exception:
                pass

            # --- propagate in-memory indicators into engine for GUI / post-test DB write ---
            try:
                if engine is not None:
                    try:
                        # symbol -> DataFrame z pełnymi wskaźnikami
                        engine.indicators_by_symbol = dict(self.indicators_by_symbol or {})
                    except Exception:
                        # nie blokuj zakończenia testu, jeśli coś tu pójdzie nie tak
                        pass
            except Exception:
                pass

            # Emit signals with engine object
            try:
                # preferowany sygnał – MainWindow podpięte pod finished_with_engine
                try:
                    self.finished_with_engine.emit(engine)
                except Exception:
                    pass
                # legacy sygnał – zostawiamy dla kompatybilności
                self.finished_signal.emit(engine)
            except Exception:
                pass

            self._emit(f"[WORKER] Strategy test finished.")
        except Exception as e:
            msg = f"[WORKER] ERROR: {e}"
            try:
                self.error_signal.emit(msg)
            except Exception:
                pass
            logging.exception(msg)

