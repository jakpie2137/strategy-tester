INDICATORS_TABLE = 'indicators_historical'
# backtester/engine.py
import threading
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from backtester.context import SymbolContext
from config import FEE_RATE, MAX_WORKER_CANDLES


# ---------- utils ----------
def _now_iso_local() -> str:
    # keep whatever local timezone the process has; do NOT force UTC
    try:
        return datetime.now().isoformat(timespec="seconds")
    except Exception:
        return datetime.now().isoformat()


def _safe_float(v, default=np.nan):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)
    except Exception:
        return default


# ---------- bias helpers (reused by worker) ----------
def _infer_open_side(open_signal):
    """Best-effort to read side from a signal dict/object."""
    def _get_str(v):
        return v.lower().strip() if isinstance(v, str) else None

    s = None
    if isinstance(open_signal, dict):
        s = _get_str(open_signal.get("signal_type") or open_signal.get("type") or open_signal.get("action"))
        if not s:
            s = _get_str(open_signal.get("side") or open_signal.get("direction"))
    else:
        s = _get_str(getattr(open_signal, "signal_type", None) or
                     getattr(open_signal, "type", None) or
                     getattr(open_signal, "action", None))
        if not s:
            s = _get_str(getattr(open_signal, "side", None) or getattr(open_signal, "direction", None))

    if not s:
        return None

    LONG = {"open_long", "long_open", "long", "buy", "go_long"}
    SHORT = {"open_short", "short_open", "short", "sell", "go_short"}
    if s in LONG:
        return "long"
    if s in SHORT:
        return "short"
    return None


def _apply_bias_to_open_signal(open_signal, bias):
    """If bias == 'long', suppress short; if 'short', suppress long."""
    if not bias or not open_signal:
        return open_signal
    side = _infer_open_side(open_signal)
    if side is None:
        return open_signal
    if bias == "long" and side == "short":
        return None
    if bias == "short" and side == "long":
        return None
    return open_signal


# -----------------------------------------------------

def _compute_base_need_from_strategy(strategy) -> int:
    """Avoid recomputing on a gigantic window. Tries strategy.get_required_base_need(), falls back to common attrs."""
    if hasattr(strategy, "get_required_base_need"):
        try:
            need = int(strategy.get_required_base_need())
            if need > 0:
                return need
        except Exception:
            pass
    for attr in ("lookback", "window", "period", "slow", "fast"):
        if hasattr(strategy, attr):
            try:
                iv = int(getattr(strategy, attr))
                if iv > 0:
                    return max(iv, 50)
            except Exception:
                pass
    return 60


# -------- column ordering (keeps groups together) --------
def order_columns(columns):
    """Return a stable, human-friendly column order."""
    cols = list(dict.fromkeys(columns or []))  # unique, preserve incoming order
    have = set(cols)
    out, used = [], set()

    def add(name):
        if name in have and name not in used:
            out.append(name); used.add(name)

    # base
    for n in ["inserted_at", "close_price", "TP", "SL", "TS", "TS_BENCHMARK"]:
        add(n)

    # MACD cluster
    for n in ["MACD", "MACD_SIGNAL", "MACD_HIST", "macd", "macd_signal", "macd_hist"]:
        add(n)

    # RSI
    for n in ["RSI", "rsi"]:
        add(n)

    # MA cluster (support both naming styles)
    for n in ["MAfast", "MA_FAST", "MAslow", "MA_SLOW"]:
        add(n)

    # BB cluster
    for n in ["BB_UPPER", "BB_MIDDLE", "BB_LOWER", "bb_upper", "bb_middle", "bb_lower"]:
        add(n)

    # ATR
    for n in ["ATR", "atr"]:
        add(n)

    # FEAR_GREED or others that are often used
    for n in ["FEAR_GREED"]:
        add(n)

    # any remaining
    for n in sorted([c for c in cols if c not in used], key=str.lower):
        out.append(n)
        used.add(n)

    return out


class MultiSymbolEngine:
    """Live/backtest engine writing indicators to a single, shared table `indicators_historical`.
       Keys: (symbol, open_time, close_time) – identical timestamps to `candles`.
    """

    def __init__(self, strategy, db, db_queue, symbols, strategy_name=None, bias=None):
        self.ind_table = INDICATORS_TABLE
        self.db = db
        self.db_queue = db_queue
        self.strategy = strategy
        self.symbols = list(symbols or [])
        self.contexts = {sym: SymbolContext(sym) for sym in self.symbols}
        self.trades = {sym: [] for sym in self.symbols}
        self.bias = bias

        # --- global trade id counter (unique across ALL symbols) ---
        self._global_trade_id = 0
        self._id_lock = threading.Lock()

        def _next_trade_id():
            with self._id_lock:
                self._global_trade_id += 1
                return self._global_trade_id

        # wire contexts
        for _ctx in self.contexts.values():
            try:
                _ctx.next_trade_id = _next_trade_id
            except Exception:
                pass

        # --- dynamic indicator schema ---
        base_cols = {"inserted_at", "close_price", "TP", "SL", "TS", "TS_BENCHMARK"}
        proposed = set()
        for meth in ("get_db_indicator_columns", "get_indicator_names"):
            if hasattr(strategy, meth):
                try:
                    proposed |= set(getattr(strategy, meth)() or [])
                except Exception:
                    pass
        self.db_indicator_names = order_columns(list(base_cols | proposed))
        self.indicator_names = list(self.db_indicator_names)

        strat_name = (strategy_name or getattr(strategy, "get_strategy_name", lambda: type(strategy).__name__)()).strip()
        self._strategy_name = strat_name

        # create the single shared historical table (idempotent)
        table_name = self.db.create_indicators_table(
            INDICATORS_TABLE,
            self.db_indicator_names,
            table_type="historical",
        )
        # mapping (symbol -> table) – always to the same logical table
        self.ind_tables_hist = {sym: INDICATORS_TABLE for sym in self.symbols}

        # fresh start for a new test
        try:
            self.db.clear_indicators_table(table_name)
        except Exception:
            pass

    # -------- internal helpers --------
    @staticmethod
    def _extract_vals(strategy, row):
        """Prefer strategy.extract_indicator_values(row) when available."""
        if hasattr(strategy, "extract_indicator_values"):
            try:
                v = strategy.extract_indicator_values(row)
                if isinstance(v, dict):
                    return v
            except Exception:
                pass
        try:
            return row.to_dict()
        except Exception:
            return {}

    def _ensure_columns_for_vals(self, vals: dict):
        """If strategy starts outputting new keys, add columns on the fly."""
        numeric_keys = set()
        for k, v in (vals or {}).items():
            try:
                float(v)
                numeric_keys.add(str(k))
            except Exception:
                continue

        required = set(self.db_indicator_names) | numeric_keys
        new_cols = order_columns(required)
        if new_cols == self.db_indicator_names:
            return  # nothing new

        # Idempotent (adds only missing)
        self.db.create_indicators_table(INDICATORS_TABLE, self.db_indicator_names, table_type='historical')
        self.db_indicator_names = new_cols  # keep consistent with worker/engine

    # -------- main path --------
    def on_tick(self, symbol, candles, tick=None):
        # Coerce to DataFrame
        df = candles if isinstance(candles, pd.DataFrame) else pd.DataFrame(candles)
        if df is None or df.empty:
            return

        # --- DO NOT force UTC; preserve incoming tz/naive as is ---
        if "close_time" not in df.columns and "timestamp" in df.columns:
            # timestamp (seconds) -> datetime; keep naive local
            df["close_time"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
        else:
            df["close_time"] = pd.to_datetime(df["close_time"], errors="coerce")

        if "open_time" in df.columns:
            df["open_time"] = pd.to_datetime(df["open_time"], errors="coerce")

        df = df.dropna(subset=["close_time"]).copy()

        # we don't set index to avoid losing open_time; sort by close_time only
        df.sort_values("close_time", inplace=True)

        # trailing window for perf
        base_need = _compute_base_need_from_strategy(self.strategy)
        N = max(60, base_need * 3)
        dfw = df.iloc[-N:].copy() if len(df) > N else df.copy()

        # compute indicators
        out = self.strategy.compute_indicators(dfw)
        if out is None or len(out) == 0:
            return

        # last row + values
        last = out.iloc[-1]

        # Get close_time and open_time for this bar (prefer columns, else index)
        close_ts = None
        if hasattr(last, "get"):
            close_ts = last.get("close_time", None)
        if close_ts is None:
            try:
                close_ts = dfw["close_time"].iloc[-1]
            except Exception:
                close_ts = getattr(last, "name", None)

        open_ts = None
        if hasattr(last, "get"):
            open_ts = last.get("open_time", None)
        if open_ts is None and "open_time" in dfw.columns:
            try:
                open_ts = dfw["open_time"].iloc[-1]
            except Exception:
                open_ts = None

        vals = self._extract_vals(self.strategy, last)

        # ensure new columns if strategy produced new keys
        self._ensure_columns_for_vals(vals)

        # compose row to store
        to_store = {name: np.nan for name in self.db_indicator_names}
        to_store["inserted_at"] = _now_iso_local()
        # accept close/ CLOSE
        cval = last.get("close", None) if hasattr(last, "get") else getattr(last, "close", None)
        if cval is None:
            cval = last.get("CLOSE", None) if hasattr(last, "get") else getattr(last, "CLOSE", None)
        to_store["close_price"] = _safe_float(cval)

        for k, v in (vals or {}).items():
            if k in to_store:
                to_store[k] = _safe_float(v)

        # project TP/SL/TS from current position if present
        ctx = self.contexts[symbol]
        pos = ctx.current_position
        if pos is not None:
            if bool(pos.get("ts_armed", False)):
                to_store["TP"] = np.nan
                to_store["SL"] = np.nan
                to_store["TS"] = _safe_float(pos.get("trailing_stop"))
                to_store["TS_BENCHMARK"] = _safe_float(pos.get("ts_benchmark"))
            else:
                to_store["TP"] = _safe_float(pos.get("tp_level"))
                to_store["SL"] = _safe_float(pos.get("sl_level"))
                to_store["TS"] = np.nan
                to_store["TS_BENCHMARK"] = np.nan

        # store a single row snapshot (with BOTH open_time and close_time)
        try:
            self.db.insert_indicator_row(
                self.ind_tables_hist[symbol],
                symbol=symbol,
                open_time=open_ts,
                close_time=close_ts,
                values=to_store,
                indicator_names=self.db_indicator_names,
            )
        except Exception as e:
            logging.debug(f"[ENGINE] insert_indicator_row failed: {e}", exc_info=True)

        # --- trading decisions (close then open) ---
        row_last = out.iloc[-1]

        candle = {
            "symbol": symbol,
            "close": _safe_float(row_last.get("close", row_last.get("CLOSE") if hasattr(row_last, "get") else None), 0.0),
            "close_time": close_ts,
        }
        # time-based close_after_x_candles if configured
        ctx = self.contexts[symbol]
        close_after = 0
        if hasattr(self.strategy, "get_close_after_x_candles"):
            try:
                close_after = int(self.strategy.get_close_after_x_candles() or 0)
            except Exception:
                close_after = 0

        if ctx.current_position is not None:
            # increment bar counter
            pos = ctx.current_position
            pos["bars_open"] = int(pos.get("bars_open", 0)) + 1

            if close_after > 0 and pos["bars_open"] >= close_after:
                trade = self.close_position(ctx, candle, "close_after_x_candles")
                if trade:
                    self.trades[symbol].append(trade)
                ctx.current_position = None

        # CLOSE first
        if ctx.current_position is not None:
            try:
                close_sig = self.strategy.close_position_signal(row_last, ctx.current_position)
            except Exception as e:
                close_sig = None
                logging.debug(f"[ENGINE] close_position_signal error: {e}", exc_info=True)
            if close_sig:
                trade = self.close_position(ctx, candle, close_sig.get("signal_type", "close"))
                if trade:
                    self.trades[symbol].append(trade)
                    ctx.current_position = None

        # OPEN if flat
        if ctx.current_position is None:
            try:
                open_sig = self.strategy.open_position_signal(row_last, None)
            except Exception as e:
                open_sig = None
                logging.debug(f"[ENGINE] open_position_signal error: {e}", exc_info=True)
            open_sig = _apply_bias_to_open_signal(open_sig, self.bias)
            if open_sig:
                side = _infer_open_side(open_sig) or (open_sig.get("side") if isinstance(open_sig, dict) else None)
                if side in ("long", "short"):
                    # optional filter: don't open if close-conditions already met
                    try:
                        if hasattr(self.strategy, "filter_open_with_close_signals"):
                            if not bool(self.strategy.filter_open_with_close_signals(row_last, side)):
                                open_sig = None
                    except Exception:
                        pass

            if open_sig:
                side = _infer_open_side(open_sig) or (open_sig.get("side") if isinstance(open_sig, dict) else None)
                if side in ("long", "short"):
                    with self._id_lock:
                        self._global_trade_id += 1
                        trade_id = self._global_trade_id

                    entry_price = float(candle["close"])
                    try:
                        risk = self.strategy.get_risk_params() or {}
                    except Exception:
                        risk = {}

                    if side == "long":
                        tp_mul = float(risk.get("tp_long", 1.0))
                        sl_mul = float(risk.get("sl_long", 1.0))
                        ts_mul = float(risk.get("trail_long", 0.0))
                    else:
                        tp_mul = float(risk.get("tp_short", 1.0))
                        sl_mul = float(risk.get("sl_short", 1.0))
                        ts_mul = float(risk.get("trail_short", 0.0))

                    pos = {
                        "trade_id": trade_id,
                        "symbol": symbol,
                        "side": side,
                        "entry_price": entry_price,
                        "entry_timestamp": candle["close_time"],
                        "signal_type": open_sig.get("signal_type", "") if isinstance(open_sig, dict) else "",
                        "tp_level": (entry_price * tp_mul) if tp_mul else None,
                        "sl_level": (entry_price * sl_mul) if sl_mul else None,
                        "ts_armed": False,
                        "trailing_stop": (entry_price * ts_mul) if ts_mul else None,
                        "ts_benchmark": entry_price,
                        "amount": float(open_sig.get("amount", 1.0)) if isinstance(open_sig, dict) else 1.0,
                        "tp_open": (entry_price * tp_mul) if tp_mul else None,
                        "sl_open": (entry_price * sl_mul) if sl_mul else None,
                        "initial_benchmark": None,
                        "initial_ts": None,
                    }
                    ctx.current_position = pos
                    # snapshot BB_close relation at entry (if strategy supports it)
                    try:
                        if hasattr(self.strategy, "compute_bb_close_initial_side"):
                            init_side, init_line = self.strategy.compute_bb_close_initial_side(row_last, side)
                            pos["bb_close_initial_side"] = init_side
                            pos["bb_close_entry_line"] = init_line
                    except Exception:
                        pass


    def close_position(self, context, candle, close_signal_type):
        entry = context.current_position
        if entry is None or candle is None or "close" not in candle or "close_time" not in candle:
            logging.warning("[ENGINE] close_position called improperly.")
            return None

        exit_price = float(candle["close"])
        exit_time = candle["close_time"]
        amount = float(entry.get("amount", 1.0))
        side = str(entry.get("side", ""))
        entry_price = float(entry.get("entry_price", exit_price))
        entry_time = entry.get("entry_timestamp", exit_time)
        trade_id = entry.get("trade_id")

        fee_open = abs(entry_price * amount) * FEE_RATE
        fee_close = abs(exit_price * amount) * FEE_RATE
        total_fee = fee_open + fee_close

        if side == "long":
            pnl = (exit_price - entry_price) * amount - total_fee
        elif side == "short":
            pnl = (entry_price - exit_price) * amount - total_fee
        else:
            pnl = 0.0

        return {
            "trade_id": trade_id,
            "symbol": getattr(context, "symbol", "UNKNOWN"),
            "signal_type": close_signal_type,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_timestamp": entry_time,
            "exit_timestamp": exit_time,
            "amount": amount,
            "fee": total_fee,
            "pnl": pnl,
            "tp_open": entry.get("tp_open"),
            "sl_open": entry.get("sl_open"),
            "initial_benchmark": entry.get("initial_benchmark"),
            "initial_ts": entry.get("initial_ts"),
            "close_signal_type": close_signal_type,
            "open_signal_type": entry.get("signal_type", ""),
            "close_reason": close_signal_type,
        }

    def get_all_trades(self):
        return [t for sym in self.trades for t in self.trades[sym]]

    def get_trades(self, symbol):
        return self.trades.get(symbol, [])

    def reset(self):
        for ctx in self.contexts.values():
            ctx.reset()
        for tr in self.trades.values():
            tr.clear()

    # -------- runner API (backtest) --------
    def run_backtest(self, progress_cb=None, log_cb=None):
        """Drive full backtest using the normal on_tick() path so that
        trading logic, indicator writes, and DB queue tasks behave
        exactly like in live mode.
        """
        def _log(msg):
            try:
                (log_cb or (lambda *a, **k: None))(msg)
            except Exception:
                pass

        # prefer ~200-candle progress cadence (not a big wall at the end)
        PROG_CHUNK = 200

        for sym in list(self.symbols or []):
            try:
                _log(f"[WORKER] Testing symbol {sym}")
                # Pull candles (do not hard-cap to 10k, honor config)
                raw = None
                if hasattr(self.db, "get_candles"):
                    try:
                        raw = self.db.get_candles(sym, as_df=True, limit=MAX_WORKER_CANDLES)
                    except TypeError:
                        raw = self.db.get_candles(sym, limit=MAX_WORKER_CANDLES)
                df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw or [])
                if df is None or df.empty:
                    _log(f"[ENGINE] No candles for {sym}")
                    continue

                # normalize time columns – DO NOT force UTC
                if "close_time" not in df.columns and "timestamp" in df.columns:
                    df["close_time"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
                else:
                    df["close_time"] = pd.to_datetime(df["close_time"], errors="coerce")
                if "open_time" in df.columns:
                    df["open_time"] = pd.to_datetime(df["open_time"], errors="coerce")

                df = df.dropna(subset=["close_time"]).copy()
                df.sort_values("close_time", inplace=True)

                total = len(df)
                step = max(1, min(PROG_CHUNK, total))
                window = []
                for i, row in enumerate(df.itertuples(index=False), 1):
                    # grow window with this closed candle
                    window.append({
                        "open": getattr(row, "open", getattr(row, "OPEN", None)),
                        "high": getattr(row, "high", getattr(row, "HIGH", None)),
                        "low": getattr(row, "low", getattr(row, "LOW", None)),
                        "close": getattr(row, "close", getattr(row, "CLOSE", None)),
                        "open_time": getattr(row, "open_time", None),
                        "close_time": getattr(row, "close_time"),
                        "symbol": getattr(row, "symbol", sym),
                    })
                    # call normal path
                    try:
                        self.on_tick(sym, window, tick=None)
                    except Exception as e:
                        _log(f"[ENGINE][WARN] on_tick failed for {sym} at {i}/{total}: {e}")

                    if progress_cb and (i % step == 0 or i == total):
                        try:
                            progress_cb(sym, i, total)
                        except Exception:
                            pass

                # symbol done
                _log(f"[WORKER] Finished symbol {sym}: {total} candles")

            except Exception as e:
                _log(f"[ENGINE][WARN] run_backtest symbol={sym} failed: {e}")

    def run(self, progress_cb=None, log_cb=None):
        """Alias for run_backtest to satisfy worker entrypoints."""
        return self.run_backtest(progress_cb=progress_cb, log_cb=log_cb)


# -------- Compatibility shim for older imports --------
class TradeTickRecorder:
    """Minimal helper to keep old imports working."""
    def __init__(self, db_queue):
        self.db_queue = db_queue

    def record(self, symbol, price, trade_id=None, relation=None, timestamp=None):
        tick = {
            "symbol": symbol,
            "timestamp": timestamp or datetime.now().isoformat(timespec="milliseconds"),
            "price": float(price),
            "trade_id": trade_id,
            "trade_relation": relation,
        }
        self.db_queue.put({"type": "insert_ticks", "ticks_list": [tick]})

    def record_many(self, ticks):
        batch = []
        for t in ticks or []:
            batch.append({
                "symbol": t.get("symbol"),
                "timestamp": t.get("timestamp") or datetime.now().isoformat(timespec="milliseconds"),
                "price": float(t.get("price", 0.0)),
                "trade_id": t.get("trade_id"),
                "trade_relation": t.get("trade_relation"),
            })
        if batch:
            self.db_queue.put({"type": "insert_ticks", "ticks_list": batch})
