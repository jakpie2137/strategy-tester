#!/usr/bin/env python
"""
Headless tester for Livetest_panel (full stats version).

Uruchamia StrategyTestWorker bez GUI i na koniec:
  - zapisuje trejdy do public.trades (przez db.insert_trade_rows),
  - liczy rozszerzone statystyki PnL (jak PerformanceWidget),
  - liczy statystyki "Trades info" (jak _TradesInfoTab w GlobalStatsWidget),
  - zapisuje wszystko do public.stats w jednym INSERT (per symbol).

Założenia:
  - korzystamy z tego samego formatu trade dictów, co w GUI,
  - jeżeli w DB brak wskaźników SL/TP/TS, metryki SL/TP/TSdist będą NULL (jak w GUI).
"""

import argparse
import logging
import math
import sys
from datetime import timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

from data.db_pg import Database
from gui.test_worker import StrategyTestWorker
import config as cfg

# Strategie dostępne w headlessie – dorzuć tu kolejne, jeśli będzie potrzeba
from backtester.strategies.rsi import RSIStrategy
from backtester.strategies.ma import MAStrategy

STRATEGIES = {
    "RSI": RSIStrategy,
    "MA": MAStrategy,
}

# korzystamy z dokładnie tej samej funkcji, co PerformanceWidget
from gui.performance_widget import compute_extended_metrics


# === helpers (skopiowane / uproszczone z global_stats_widget.py) ===

def _safe_float(x, default=0.0):
    try:
        if x is None:
            return float(default)
        if isinstance(x, str):
            x = x.replace("%", "").replace(",", "").strip()
        v = float(x)
        return v if math.isfinite(v) else float(default)
    except Exception:
        return float(default)


def _get_first(d: dict, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _ind_df_for(symbol, db: Database, engine, cache: dict):
    """
    Headless odpowiednik _TradesInfoTab._ind_df_for:
    - wybiera tabelę wskaźników z engine.ind_tables_hist (jeśli istnieje),
    - pobiera dane przez db.get_indicator_table(...),
    - normalizuje kolumny: close_time, TP, SL, TS, TS_BENCHMARK, close_price.
    """
    if symbol in cache:
        return cache[symbol]

    table = None
    try:
        if engine is not None and getattr(engine, "ind_tables_hist", None):
            table = engine.ind_tables_hist.get(symbol)
    except Exception:
        table = None

    df = None
    if db is not None and hasattr(db, "get_indicator_table") and table:
        try:
            # Uwaga: sygnatura get_indicator_table może być różna między wersjami.
            # W _TradesInfoTab wywoływana była jako: get_indicator_table(table, symbol, limit=200000)
            # a w MainWindow – z parametrami nazwanymi (symbol,start,end,table_name).
            # Spróbujemy najpierw wariantu "nowego", potem fallback do starego.
            try:
                df = db.get_indicator_table(
                    symbol=symbol,
                    start=None,
                    end=None,
                    table_name=table,
                )
            except TypeError:
                # fallback: stary wariant
                df = db.get_indicator_table(table, symbol, limit=200000)
        except Exception:
            df = None

    if df is None or df.empty:
        cache[symbol] = None
        return None

    df = df.copy()
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
    for c in ("TP", "SL", "TS", "TS_BENCHMARK", "close_price"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.sort_values("close_time", inplace=True)
    cache[symbol] = df
    return df


def _duration_sec(tr: dict) -> float:
    d = _safe_float(tr.get("duration_secs"), float("nan"))
    if math.isfinite(d):
        return d
    try:
        e = pd.to_datetime(tr.get("entry_timestamp"), utc=True, errors="coerce")
        x = pd.to_datetime(tr.get("exit_timestamp"), utc=True, errors="coerce")
        if e is not None and x is not None and not (pd.isna(e) or pd.isna(x)):
            return float((x - e).total_seconds())
    except Exception:
        pass
    return float("nan")


def _price_delta_pct(tr: dict) -> float:
    ep = _safe_float(
        _get_first(
            tr,
            [
                "entry_price",
                "open_price",
                "price_in",
                "open price",
                "buy price",
                "in price",
            ],
            float("nan"),
        ),
        float("nan"),
    )
    xp = _safe_float(
        _get_first(
            tr,
            [
                "exit_price",
                "close_price",
                "price_out",
                "close price",
                "sell price",
                "out price",
            ],
            float("nan"),
        ),
        float("nan"),
    )
    if not (math.isfinite(ep) and math.isfinite(xp)) or ep == 0:
        return float("nan")
    return abs(xp - ep) / abs(ep) * 100.0


def _sl_tp_pct_at_entry(symbol, entry_ts, entry_price, db, engine, cache):
    ep = _safe_float(entry_price, float("nan"))

    def pct(level):
        lv = _safe_float(level, float("nan"))
        return (
            abs(lv - ep) / abs(ep) * 100.0
            if (math.isfinite(ep) and ep != 0 and math.isfinite(lv))
            else None
        )

    df = _ind_df_for(symbol, db, engine, cache)
    if df is None or entry_ts is None:
        return None, None

    ts = pd.to_datetime(entry_ts, utc=True, errors="coerce")
    try:
        i = (df["close_time"] - ts).abs().idxmin()
        row = df.loc[i]
        sl = row.get("SL", np.nan)
        tp = row.get("TP", np.nan)
    except Exception:
        sl = np.nan
        tp = np.nan

    return pct(sl), pct(tp)


def _ts_distances_pct(symbol, entry_ts, exit_ts, db, engine, cache):
    df = _ind_df_for(symbol, db, engine, cache)
    if df is None or entry_ts is None or exit_ts is None:
        return []
    s = pd.to_datetime(entry_ts, utc=True, errors="coerce")
    e = pd.to_datetime(exit_ts, utc=True, errors="coerce")
    sub = df[(df["close_time"] >= s) & (df["close_time"] <= e)].copy()
    if sub.empty or not set(["TS", "TS_BENCHMARK", "close_price"]).issubset(
        sub.columns
    ):
        return []
    sub = sub.dropna(subset=["TS", "TS_BENCHMARK", "close_price"])
    if sub.empty:
        return []
    changed = sub["TS_BENCHMARK"].diff().fillna(1.0).abs() > 1e-12
    filt = sub[changed]
    if filt.empty:
        return []
    vals = (
        np.abs(filt["TS"] - filt["TS_BENCHMARK"])
        / np.abs(filt["close_price"]).replace(0, np.nan)
        * 100.0
    )
    vals = vals.replace([np.inf, -np.inf], np.nan).dropna()
    return list(vals.astype(float))


def _agg3(vec):
    clean = [v for v in vec if v is not None and math.isfinite(v)]
    if not clean:
        return (float("nan"), float("nan"), float("nan"))
    arr = np.asarray(clean, dtype=float)
    return (float(np.mean(arr)), float(np.min(arr)), float(np.max(arr)))



def _sl_tp_pct_from_trade(tr: dict):
    """Return (SL_pct, TP_pct) based purely on trade fields.

    Both values are absolute distance from entry_price to sl_open/tp_open
    expressed as % of entry_price. If required fields are missing or invalid,
    returns (None, None).
    """
    ep = _safe_float(tr.get("entry_price"), float("nan"))
    if not (math.isfinite(ep) and ep != 0.0):
        return None, None

    def pct(level):
        lv = _safe_float(level, float("nan"))
        if not math.isfinite(lv):
            return None
        return abs(lv - ep) / abs(ep) * 100.0

    sl_open = tr.get("sl_open")
    tp_open = tr.get("tp_open")
    return pct(sl_open), pct(tp_open)


def _tsdist_pct_from_trade(tr: dict):
    """Return initial TS distance in % of initial_benchmark.

    Distance is |initial_ts - initial_benchmark| / |initial_benchmark| * 100.
    If any required field is missing or invalid, returns None.
    """
    bench = _safe_float(tr.get("initial_benchmark"), float("nan"))
    ts = _safe_float(tr.get("initial_ts"), float("nan"))
    if not (math.isfinite(bench) and bench != 0.0 and math.isfinite(ts)):
        return None
    return abs(ts - bench) / abs(bench) * 100.0



def compute_trades_info_metrics_per_symbol(trades_by_symbol, db: Database, engine):
    """Compute Trades-info metrics per symbol based only on trade dicts.

    Headless odpowiednik _TradesInfoTab.update, ale zamiast QTableWidget
    zwraca dict:
        {
          symbol: {
            "avg_duration": ...,
            "min_duration": ...,
            "max_duration": ...,
            "avg_price_delta_pct": ...,
            "min_price_delta_pct": ...,
            "max_price_delta_pct": ...,
            "avg_sl_pct": ...,
            "min_sl_pct": ...,
            "max_sl_pct": ...,
            "avg_tp_pct": ...,
            "min_tp_pct": ...,
            "max_tp_pct": ...,
            "avg_tsdist_pct": ...,
            "min_tsdist_pct": ...,
            "max_tsdist_pct": ...,
          },
          ...
        }

    Parametry ``db`` i ``engine`` są zachowane dla kompatybilności sygnatury,
    ale w tej wersji nie są używane – wszystkie metryki liczymy z pól trejdu
    (entry_price, sl_open, tp_open, initial_benchmark, initial_ts).
    """
    result = {}

    for sym, arr in trades_by_symbol.items():
        durs = []
        deltas = []
        sls = []
        tps = []
        tsd = []

        for t in arr or []:
            durs.append(_duration_sec(t))
            deltas.append(_price_delta_pct(t))

            slpct, tppct = _sl_tp_pct_from_trade(t)
            if slpct is not None:
                sls.append(slpct)
            if tppct is not None:
                tps.append(tppct)

            ts_val = _tsdist_pct_from_trade(t)
            if ts_val is not None:
                tsd.append(ts_val)

        A_d, I_d, X_d = _agg3(durs)
        A_p, I_p, X_p = _agg3(deltas)
        A_sl, I_sl, X_sl = _agg3(sls)
        A_tp, I_tp, X_tp = _agg3(tps)
        A_ts, I_ts, X_ts = _agg3(tsd)

        result[sym] = {
            "avg_duration": A_d,
            "min_duration": I_d,
            "max_duration": X_d,
            "avg_price_delta_pct": A_p,
            "min_price_delta_pct": I_p,
            "max_price_delta_pct": X_p,
            "avg_sl_pct": A_sl,
            "min_sl_pct": I_sl,
            "max_sl_pct": X_sl,
            "avg_tp_pct": A_tp,
            "min_tp_pct": I_tp,
            "max_tp_pct": X_tp,
            "avg_tsdist_pct": A_ts,
            "min_tsdist_pct": I_ts,
            "max_tsdist_pct": X_ts,
        }

    return result




class DirectDbQueue:
    """
    Adapter udający queue.Queue, ale zamiast wrzucać taski do osobnego wątku,
    od razu wykonuje operacje na db (tak jak MainWindow.db_writer, ale w wersji okrojonej).

    Obsługuje:
      - insert_trade_rows   -> db.insert_trade_rows(...)
    Statystyk z replace_stats_rows NIE używamy, bo i tak liczymy wszystko samodzielnie
    z trejdów na końcu (compute_extended_metrics + compute_trades_info_metrics_per_symbol).
    """

    def __init__(self, db: Database):
        self.db = db

    def put(self, task):
        t = (task or {}).get("type")
        if not t:
            logging.debug("[HEADLESS][DB_QUEUE] Task bez 'type': %r", task)
            return

        if t == "insert_trade_rows":
            rows = task.get("rows") or []
            if not rows:
                logging.debug("[HEADLESS][DB_QUEUE] insert_trade_rows skipped (no rows)")
            else:
                try:
                    self.db.insert_trade_rows(rows)
                    logging.info("[HEADLESS] insert_trade_rows OK (rows=%d)", len(rows))
                except Exception as e:
                    logging.error(
                        "[HEADLESS] insert_trade_rows failed: %s",
                        e,
                        exc_info=True,
                    )
        else:
            logging.debug("[HEADLESS][DB_QUEUE] ignoring task type=%s", t)

    def qsize(self) -> int:
        """Zgodność z queue.Queue API (nie używamy w headless, więc zawsze 0)."""
        return 0


# === CLI / main ===


def _maybe_sync_fear_greed(db):
    """
    Opcjonalny sync Fear & Greed przed testem.
    W razie błędu tylko loguje warning i NIE zatrzymuje testu.
    """
    try:
        from tools.fng_integration import sync_fear_greed
    except Exception as e:
        logging.warning("[HEADLESS][FNG] tools.fng_integration not available: %s", e)
        return

    try:
        logging.info("[HEADLESS][FNG] Syncing Fear & Greed before test...")
        n = sync_fear_greed(db=db)
        if isinstance(n, int):
            logging.info("[HEADLESS][FNG] Synced %d rows into public.fear_greed", n)
        else:
            logging.info("[HEADLESS][FNG] Fear & Greed sync completed")
    except Exception as e:
        logging.warning("[HEADLESS][FNG] sync_fear_greed failed: %s", e)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Headless Livetest backtest runner (bez GUI, z pełnymi statystykami)."
    )
    p.add_argument(
        "--strategy",
        "-s",
        default="RSI",
        choices=STRATEGIES.keys(),
        help="Nazwa strategii (domyślnie: RSI)",
    )
    p.add_argument(
        "--symbols",
        "-S",
        default="ALL",
        help="Lista symboli rozdzielona przecinkami, albo 'ALL' żeby użyć cfg.AVAILABLE_PAIRS",
    )
    p.add_argument(
        "--bias",
        "-b",
        default=None,
        choices=[None, "long", "short"],
        help="Opcjonalny bias: long/short (domyślnie brak)",
    )
    p.add_argument(
        "--log-level", default="INFO", help="Poziom logowania (DEBUG, INFO, WARNING...)"
    )
    return p.parse_args(argv)


def resolve_symbols(arg_symbols: str):
    if not arg_symbols or arg_symbols.upper() == "ALL":
        return list(getattr(cfg, "AVAILABLE_PAIRS", []) or [])
    return [s.strip() for s in arg_symbols.split(",") if s.strip()]


def main(argv=None):
    args = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    strat_name = args.strategy.upper()
    strat_cls = STRATEGIES.get(strat_name)
    if strat_cls is None:
        logging.error("Nieznana strategia: %s", strat_name)
        return 1

    symbols = resolve_symbols(args.symbols)
    if not symbols:
        logging.error(
            "Brak symboli do testu (AVAILABLE_PAIRS jest puste i nie podano --symbols)."
        )
        return 1

    bias = args.bias
    if isinstance(bias, str):
        b = bias.lower()
        if b not in ("long", "short"):
            bias = None
        else:
            bias = b

    logging.info(
        "[HEADLESS] Start testu: strategy=%s, symbols=%s, bias=%s",
        strat_name,
        symbols,
        bias,
    )

    db = Database()
    # Opcjonalny, bezpieczny sync Fear & Greed przed testem
    if getattr(cfg, "SYNC_FEAR_GREED_ON_TEST_START", True):
        _maybe_sync_fear_greed(db)

    db_queue = DirectDbQueue(db)

    if getattr(cfg, "WRITE_INDICATORS_TO_DB", None) is not False:
        logging.warning(
            "[HEADLESS] WRITE_INDICATORS_TO_DB != False – "
            "dla szybkich testów zewnętrznych zalecam ustawić na False w config.py"
        )

    worker = StrategyTestWorker(
        db=db,
        db_queue=db_queue,
        symbols=symbols,
        strategy_class=strat_cls,
        bias=bias,
        parent=None,
    )

    worker.run()

    engine = getattr(worker, "engine", None)
    trades_by_symbol = getattr(engine, "trades", {}) if engine is not None else {}

    # test_id – spróbujmy najpierw z worker'a, potem z Database.next_free_test_id()
    test_id = getattr(worker, "test_id", None)
    if not test_id:
        try:
            if hasattr(db, "next_free_test_id"):
                next_id = int(db.next_free_test_id())
                test_id = next_id - 1 if next_id > 0 else None
        except Exception:
            test_id = None

    if not trades_by_symbol or not test_id:
        logging.warning(
            "[HEADLESS] Brak trejdów (%d symboli) lub test_id=%r – statystyki nie zostaną zapisane.",
            len(trades_by_symbol),
            test_id,
        )
        return 0

    logging.info(
        "[HEADLESS] Liczenie statów dla test_id=%s (symboli=%d)...",
        test_id,
        len(trades_by_symbol),
    )

    # --- PnL / volume / fee / ROI etc. (PerformanceWidget.compute_extended_metrics) ---
    pnl_stats = {}
    for sym, arr in trades_by_symbol.items():
        metrics = compute_extended_metrics(arr or [])
        pnl_stats[sym] = metrics

    # --- Trades info (duration, price delta, SL/TP/TSdist) ---
    trades_info_stats = compute_trades_info_metrics_per_symbol(
        trades_by_symbol, db=db, engine=engine
    )

    # --- Zapis do public.stats (DELETE + INSERT per symbol) ---
    try:
        with db._conn() as cur:
            cur.execute("DELETE FROM public.stats WHERE test_id = %s;", (int(test_id),))

            sql = """
            INSERT INTO public.stats (
                test_id,
                symbol,
                trades,
                win_rate,
                total_pnl,
                avg_pnl,
                best,
                worst,
                total_vol_usd,
                total_fee_usd,
                avg_win_usd,
                avg_loss_usd,
                avg_gain_pct,
                avg_loss_pct,
                vwatr_pct,
                roc_pct,
                roi_pct,
                avg_duration,
                min_duration,
                max_duration,
                avg_price_delta_pct,
                min_price_delta_pct,
                max_price_delta_pct,
                avg_sl_pct,
                min_sl_pct,
                max_sl_pct,
                avg_tp_pct,
                min_tp_pct,
                max_tp_pct,
                avg_tsdist_pct,
                min_tsdist_pct,
                max_tsdist_pct,
                created_at
            )
            VALUES (
                %(test_id)s,
                %(symbol)s,
                %(trades)s,
                %(win_rate)s,
                %(total_pnl)s,
                %(avg_pnl)s,
                %(best)s,
                %(worst)s,
                %(total_vol_usd)s,
                %(total_fee_usd)s,
                %(avg_win_usd)s,
                %(avg_loss_usd)s,
                %(avg_gain_pct)s,
                %(avg_loss_pct)s,
                %(vwatr_pct)s,
                %(roc_pct)s,
                %(roi_pct)s,
                %(avg_duration)s,
                %(min_duration)s,
                %(max_duration)s,
                %(avg_price_delta_pct)s,
                %(min_price_delta_pct)s,
                %(max_price_delta_pct)s,
                %(avg_sl_pct)s,
                %(min_sl_pct)s,
                %(max_sl_pct)s,
                %(avg_tp_pct)s,
                %(min_tp_pct)s,
                %(max_tp_pct)s,
                %(avg_tsdist_pct)s,
                %(min_tsdist_pct)s,
                %(max_tsdist_pct)s,
                now()
            )
            """

            for sym, arr in trades_by_symbol.items():
                pnl = pnl_stats.get(sym, {}) or {}
                info = trades_info_stats.get(sym, {}) or {}

                # Durations jako timedelta (Postgres mapuje na interval)
                def _to_td(seconds):
                    return (
                        timedelta(seconds=float(seconds))
                        if seconds is not None and math.isfinite(float(seconds))
                        else None
                    )

                row = {
                    "test_id": int(test_id),
                    "symbol": sym,
                    "trades": int(pnl.get("n", len(arr or []))),
                    "win_rate": float(pnl.get("win_rate", 0.0) or 0.0),
                    "total_pnl": float(pnl.get("total_pnl", 0.0) or 0.0),
                    "avg_pnl": float(pnl.get("avg_pnl", 0.0) or 0.0),
                    "best": float(pnl.get("best", 0.0) or 0.0),
                    "worst": float(pnl.get("worst", 0.0) or 0.0),
                    "total_vol_usd": float(pnl.get("total_volume_usd", 0.0) or 0.0),
                    "total_fee_usd": float(pnl.get("total_fee_paid", 0.0) or 0.0),
                    "avg_win_usd": float(pnl.get("avg_win_usd", 0.0) or 0.0),
                    "avg_loss_usd": float(pnl.get("avg_loss_usd", 0.0) or 0.0),
                    "avg_gain_pct": float(pnl.get("avg_gain_pct", 0.0) or 0.0),
                    "avg_loss_pct": float(pnl.get("avg_loss_pct", 0.0) or 0.0),
                    "vwatr_pct": float(pnl.get("total_avg_gain_pct", 0.0) or 0.0),
                    "roc_pct": float(pnl.get("equity_return_pct", 0.0) or 0.0),
                    "roi_pct": float(pnl.get("roi_pct", 0.0) or 0.0),
                    "avg_duration": _to_td(info.get("avg_duration")),
                    "min_duration": _to_td(info.get("min_duration")),
                    "max_duration": _to_td(info.get("max_duration")),
                    "avg_price_delta_pct": float(
                        info.get("avg_price_delta_pct", 0.0) or 0.0
                    ),
                    "min_price_delta_pct": float(
                        info.get("min_price_delta_pct", 0.0) or 0.0
                    ),
                    "max_price_delta_pct": float(
                        info.get("max_price_delta_pct", 0.0) or 0.0
                    ),
                    "avg_sl_pct": float(info.get("avg_sl_pct", 0.0) or 0.0),
                    "min_sl_pct": float(info.get("min_sl_pct", 0.0) or 0.0),
                    "max_sl_pct": float(info.get("max_sl_pct", 0.0) or 0.0),
                    "avg_tp_pct": float(info.get("avg_tp_pct", 0.0) or 0.0),
                    "min_tp_pct": float(info.get("min_tp_pct", 0.0) or 0.0),
                    "max_tp_pct": float(info.get("max_tp_pct", 0.0) or 0.0),
                    "avg_tsdist_pct": float(info.get("avg_tsdist_pct", 0.0) or 0.0),
                    "min_tsdist_pct": float(info.get("min_tsdist_pct", 0.0) or 0.0),
                    "max_tsdist_pct": float(info.get("max_tsdist_pct", 0.0) or 0.0),
                }

                cur.execute(sql, row)

        logging.info(
            "[HEADLESS] Zapisano statystyki do public.stats (test_id=%s, symboli=%d).",
            test_id,
            len(trades_by_symbol),
        )
    except Exception as e:
        logging.error(
            "[HEADLESS] Błąd przy zapisie do public.stats: %s",
            e,
            exc_info=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
