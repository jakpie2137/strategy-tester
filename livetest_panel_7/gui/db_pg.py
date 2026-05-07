# -*- coding: utf-8 -*-
"""
Patched Postgres adapter (backwards-compatible).

- Single indicators table: public.indicators_historical
- No timezone normalization: store/use timestamps exactly as received.
- Keys unified: (symbol, open_time, close_time) with UNIQUE INDEX (not PK).
- Backfill missing open_time by joining candles (symbol, close_time).
- Dynamic indicator columns as quoted UPPERCASE.
- Public API kept compatible with GUI/workers.
"""
from __future__ import annotations

import os
import json
import socket
import time
from contextlib import contextmanager
from datetime import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Generator

import config as _cfg
DB_DEBUG = getattr(_cfg, "DB_DEBUG", False)
WRITE_INDICATORS_TO_DB = getattr(_cfg, "WRITE_INDICATORS_TO_DB", True)
WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST = getattr(
    _cfg, "WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST", False
)
INDICATOR_BATCH_SIZE = getattr(_cfg, "INDICATOR_BATCH_SIZE", 1000)

INDICATORS_BACKFILL_OPEN_TIME = getattr(_cfg, "INDICATORS_BACKFILL_OPEN_TIME", True)
INDICATOR_UPSERT = getattr(_cfg, "INDICATOR_UPSERT", True)
TRADE_BATCH_SIZE = getattr(_cfg, "TRADE_BATCH_SIZE", 500)

import psycopg
from psycopg.rows import dict_row

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover
    pd = None  # type: ignore

def _short_sql(sql: str, max_len: int = 200) -> str:
    """Shorten SQL for debug/perf logs – single line, trimmed to max_len."""
    try:
        one_line = " ".join(str(sql).split())
    except Exception:
        return "<invalid SQL>"
    if len(one_line) > max_len:
        return one_line[:max_len] + "..."
    return one_line



# --------------- best-effort dotenv -----------------


def _load_env_from_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


def _try_load_dotenv() -> None:
    # look for .env in current dir and two parents
    here = os.path.abspath(os.path.dirname(__file__))
    candidates = [
        os.path.join(here, ".env"),
        os.path.join(os.path.dirname(here), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(here)), ".env"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            _load_env_from_file(p)
            break


_try_load_dotenv()

# --------------- config helpers -----------------

DEFAULT_HOST = os.getenv("PG_HOST") or os.getenv("PGHOST") or os.getenv("LIVE_DB_HOST") or "127.0.0.1"
DEFAULT_PORT = int(os.getenv("PG_PORT") or os.getenv("PGPORT") or os.getenv("LIVE_DB_PORT") or 2137)
DEFAULT_DB = os.getenv("PG_DB") or os.getenv("PGDATABASE") or os.getenv("LIVE_DB_NAME") or "livetest"
DEFAULT_USER = os.getenv("PG_USER") or os.getenv("PGUSER") or os.getenv("LIVE_DB_USER") or "postgres"
DEFAULT_PASS = os.getenv("PG_PASSWORD") or os.getenv("PGPASSWORD") or os.getenv("LIVE_DB_PASSWORD") or "postgres"


def _tcp_precheck(host: str, port: int, timeout: float = 1.5) -> None:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except Exception:
        # we don't fail hard here; DB might still be accessible via unix socket, etc.
        pass


def _json_dumps_or_none(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None


def _coerce_ts(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        # accept numeric epoch seconds
        if isinstance(val, (int, float)):
            return datetime.utcfromtimestamp(float(val))
    except Exception:
        pass
    try:
        # best-effort parsing string
        return datetime.fromisoformat(str(val))
    except Exception:
        return None


def _norm_cols(cols: Iterable[str]) -> List[str]:
    out: List[str] = []
    for c in cols or []:
        if not c:
            continue
        s = str(c).strip()
        if not s:
            continue
        out.append(s.upper())
    return out


# --------------- candle column lists -----------------

CANDLE_COLS = [
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume_quote",
    "taker_buy_volume_quote",
    "open_time",
    "close_time",
    "inserted_at",
]

INDICATOR_BASE_COLS = [
    "symbol",
    "open_time",
    "close_time",
    "close_price",
    "inserted_at",
]

INDICATOR_DYNAMIC_COLS = [
    "TP",
    "SL",
    "TS",
    "MACD",
    "MACD_SIGNAL",
    "MACD_HIST",
    "RSI",
    "MA_FAST",
    "MA_SLOW",
    "BB_UPPER",
    "BB_MIDDLE",
    "BB_LOWER",
    "ATR", "ATR_PCT",
    "BUY_VOL_AVG",
    "FEAR_GREED",
    "PCT_CHANGE",
    "STOCH_D", "STOCH_K", "STOCHRSI_D", "STOCHRSI_K",
    "VOL_AVG", "VOL_SMA",
    "SL_MAX", "SL_MIN", "TP_MAX", "TP_MIN",
    "TS_BENCHMARK",
]


# -------- pg_conn used by GUI --------
@contextmanager
def pg_conn(db: "Database") -> Generator[psycopg.Connection, None, None]:
    """Raw connection helper used by GUI code."""
    with psycopg.connect(db.dsn, row_factory=dict_row) as conn:
        yield conn


# ---------------- Database ----------------

class Database:
    def __init__(
            self,
            host: str = DEFAULT_HOST,
            port: int = DEFAULT_PORT,
            db: str = DEFAULT_DB,
            user: str = DEFAULT_USER,
            password: str = DEFAULT_PASS,
    ):
        self.host = host
        self.port = int(port)
        self.db = db
        self.user = user
        self.password = password
        self.dsn = f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}?connect_timeout=3"
        # cache of ensured indicator columns per table_name -> set(columns)
        self._indicator_columns_cache: Dict[str, set] = {}
        self._connection = None  # type: ignore[assignment]
        _tcp_precheck(self.host, self.port)
        self._ensure_core_tables()

    # ---- conn helpers ----
    def _get_connection(self):
        """Return a persistent psycopg connection (create/reconnect if needed)."""
        conn = getattr(self, "_connection", None)
        try:
            closed = bool(getattr(conn, "closed", False))
        except Exception:
            closed = True
        if conn is None or closed:
            conn = psycopg.connect(self.dsn, row_factory=dict_row)
            # autocommit is False by default; we manage commit/rollback manually
            self._connection = conn
        return conn

    @contextmanager
    def _conn(self):
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise


    def _execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        t0 = time.perf_counter() if DB_DEBUG else None
        with self._conn() as cur:
            cur.execute(sql, params or ())
        if DB_DEBUG and t0 is not None:
            elapsed = (time.perf_counter() - t0) * 1000.0
            logging.warning("[DB][PERF] _execute %s took %.1f ms", _short_sql(sql), elapsed)

    def _fetchall(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        t0 = time.perf_counter() if DB_DEBUG else None
        with self._conn() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
        if DB_DEBUG and t0 is not None:
            count = len(rows) if rows else 0
            elapsed = (time.perf_counter() - t0) * 1000.0
            logging.warning("[DB][PERF] _fetchall %s -> %d rows in %.1f ms", _short_sql(sql), count, elapsed)
        return rows or []

    def _fetchone(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Dict[str, Any]]:
        t0 = time.perf_counter() if DB_DEBUG else None
        with self._conn() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        if DB_DEBUG and t0 is not None:
            elapsed = (time.perf_counter() - t0) * 1000.0
            logging.warning("[DB][PERF] _fetchone %s -> %s in %.1f ms", _short_sql(sql), "1 row" if row else "0 rows", elapsed)
        return row

    # ---- DDL / ensure ----
    def _ensure_core_tables(self) -> None:
        # candles — keep your schema; PK(symbol, open_time)
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.candles (
                symbol TEXT NOT NULL,
                open DOUBLE PRECISION,
                high DOUBLE PRECISION,
                low DOUBLE PRECISION,
                close DOUBLE PRECISION,
                volume_quote DOUBLE PRECISION,
                taker_buy_volume_quote DOUBLE PRECISION,
                open_time TIMESTAMPTZ NOT NULL,
                close_time TIMESTAMPTZ NOT NULL,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(symbol, open_time)
            );
            """
        )
        self._execute(
            """
            ALTER TABLE public.candles
            ADD COLUMN IF NOT EXISTS inserted_at TIMESTAMPTZ NOT NULL DEFAULT now();
            """
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS candles_sym_ot_ct "
            "ON public.candles(symbol, open_time, close_time);"
        )

        # dodatkowy indeks pod zapytania po (symbol, close_time)
        self._execute(
            "CREATE INDEX IF NOT EXISTS ix_candles_symbol_close_time "
            "ON public.candles(symbol, close_time);"
        )

        # indicators (single universal table)
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.indicators_historical (
                symbol      TEXT NOT NULL,
                open_time   TIMESTAMPTZ,
                close_time  TIMESTAMPTZ,
                close_price DOUBLE PRECISION,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # dynamic columns added later via ensure_indicator_table()
        self._execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_indicators_historical_sym_ot_ct
            ON public.indicators_historical(symbol, open_time, close_time);
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS ix_indicators_historical_sym_ct
            ON public.indicators_historical(symbol, close_time);
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS ix_indicators_historical_sym_ot
            ON public.indicators_historical(symbol, open_time);
            """
        )

        # trades
        self._ensure_trades_table()
        # global stats / test metadata
        self._ensure_stats_tables()

    def _ensure_trades_table(self) -> None:
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.trades (
                id SERIAL PRIMARY KEY,
                test_id INT,
                symbol TEXT NOT NULL,
                side TEXT,
                signal_type TEXT,
                open_signal_type TEXT,
                close_signal_type TEXT,
                close_reason TEXT,
                open_time TIMESTAMPTZ,
                close_time TIMESTAMPTZ,
                entry_price DOUBLE PRECISION,
                exit_price DOUBLE PRECISION,
                amount DOUBLE PRECISION,
                fee DOUBLE PRECISION,
                pnl DOUBLE PRECISION,
                tp_open DOUBLE PRECISION,
                sl_open DOUBLE PRECISION,
                initial_benchmark DOUBLE PRECISION,
                initial_ts DOUBLE PRECISION,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        self._execute("CREATE INDEX IF NOT EXISTS ix_trades_test_id ON public.trades(test_id);")
        self._execute("CREATE INDEX IF NOT EXISTS ix_trades_symbol ON public.trades(symbol);")

    def _ensure_stats_tables(self) -> None:
        # very flexible key-value stats per test (k/v)
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.test_stats (
                test_id INT NOT NULL,
                symbol TEXT,
                metric_key TEXT NOT NULL,
                metric_value DOUBLE PRECISION,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )

        # docelowa tabela historyczna z pełnymi statystykami testów
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.stats (
                test_id INT NOT NULL,
                symbol TEXT NOT NULL,
                trades INT,
                win_rate DOUBLE PRECISION,
                total_pnl DOUBLE PRECISION,
                avg_pnl DOUBLE PRECISION,
                best DOUBLE PRECISION,
                worst DOUBLE PRECISION,
                total_vol_usd DOUBLE PRECISION,
                total_fee_usd DOUBLE PRECISION,
                avg_win_usd DOUBLE PRECISION,
                avg_loss_usd DOUBLE PRECISION,
                avg_gain_pct DOUBLE PRECISION,
                avg_loss_pct DOUBLE PRECISION,
                vwatr_pct DOUBLE PRECISION,
                roc_pct DOUBLE PRECISION,
                roi_pct DOUBLE PRECISION,
            
                -- TU ZMIANA:
                avg_duration INTERVAL,
                min_duration INTERVAL,
                max_duration INTERVAL,
            
                avg_price_delta_pct DOUBLE PRECISION,
                min_price_delta_pct DOUBLE PRECISION,
                max_price_delta_pct DOUBLE PRECISION,
                avg_sl_pct DOUBLE PRECISION,
                min_sl_pct DOUBLE PRECISION,
                max_sl_pct DOUBLE PRECISION,
                avg_tp_pct DOUBLE PRECISION,
                min_tp_pct DOUBLE PRECISION,
                max_tp_pct DOUBLE PRECISION,
                avg_tsdist_pct DOUBLE PRECISION,
                min_tsdist_pct DOUBLE PRECISION,
                max_tsdist_pct DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (test_id, symbol)
            );
            """
        )

        # w razie gdyby istniała stara wersja tabeli `public.stats` – dobij brakujące kolumny
        # (bez wywalania istniejących danych)
        for col, col_type in [
            ("trades", "INT"),
            ("win_rate", "DOUBLE PRECISION"),
            ("total_pnl", "DOUBLE PRECISION"),
            ("avg_pnl", "DOUBLE PRECISION"),
            ("best", "DOUBLE PRECISION"),
            ("worst", "DOUBLE PRECISION"),
            ("total_vol_usd", "DOUBLE PRECISION"),
            ("total_fee_usd", "DOUBLE PRECISION"),
            ("avg_win_usd", "DOUBLE PRECISION"),
            ("avg_loss_usd", "DOUBLE PRECISION"),
            ("avg_gain_pct", "DOUBLE PRECISION"),
            ("avg_loss_pct", "DOUBLE PRECISION"),
            ("vwatr_pct", "DOUBLE PRECISION"),
            ("roc_pct", "DOUBLE PRECISION"),
            ("roi_pct", "DOUBLE PRECISION"),
            ("avg_duration", "DOUBLE PRECISION"),
            ("min_duration", "DOUBLE PRECISION"),
            ("max_duration", "DOUBLE PRECISION"),
            ("avg_price_delta_pct", "DOUBLE PRECISION"),
            ("min_price_delta_pct", "DOUBLE PRECISION"),
            ("max_price_delta_pct", "DOUBLE PRECISION"),
            ("avg_sl_pct", "DOUBLE PRECISION"),
            ("min_sl_pct", "DOUBLE PRECISION"),
            ("max_sl_pct", "DOUBLE PRECISION"),
            ("avg_tp_pct", "DOUBLE PRECISION"),
            ("min_tp_pct", "DOUBLE PRECISION"),
            ("max_tp_pct", "DOUBLE PRECISION"),
            ("avg_tsdist_pct", "DOUBLE PRECISION"),
            ("min_tsdist_pct", "DOUBLE PRECISION"),
            ("max_tsdist_pct", "DOUBLE PRECISION"),
            ("created_at", "TIMESTAMPTZ"),
        ]:
            try:
                self._execute(
                    f"ALTER TABLE public.stats "
                    f"ADD COLUMN IF NOT EXISTS {col} {col_type};"
                )
            except Exception:
                # nie chcemy ubić całej inicjalizacji przez drobny błąd migracyjny
                pass

        # meta danych testów
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.test_config_meta (
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

        # tick-level table for trades if needed (zostaw bez zmian)
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS public.trades_ticks (
                id SERIAL PRIMARY KEY,
                trade_id INT NOT NULL,
                ts TIMESTAMPTZ NOT NULL,
                price DOUBLE PRECISION,
                qty DOUBLE PRECISION,
                side TEXT,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS ix_trades_ticks_trade_id "
            "ON public.trades_ticks(trade_id);"
        )

    # ---- candles operations ----

    def insert_candles(self, rows: Sequence[Dict[str, Any]]) -> None:
        """
        Insert a batch of candles into public.candles.
        Expected dict keys: as in CANDLE_COLS (some may be None).
        """
        if not rows:
            return

        cols = CANDLE_COLS
        col_sql = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))

        sql = f"""
        INSERT INTO public.candles ({col_sql})
        VALUES ({placeholders})
        ON CONFLICT (symbol, open_time) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low  = EXCLUDED.low,
            close = EXCLUDED.close,
            volume_quote = EXCLUDED.volume_quote,
            taker_buy_volume_quote = EXCLUDED.taker_buy_volume_quote,
            close_time = EXCLUDED.close_time,
            inserted_at = now();
        """

        with self._conn() as cur:
            for row in rows:
                vals = [row.get(c) for c in cols]
                cur.execute(sql, vals)

    # ---- LIVE-FETCHER Methods ----

    def insert_candle(self, symbol: str, candle: Dict[str, Any]) -> None:
        """
        Wygodny wrapper na insert_candles dla pojedynczej świecy.
        Używany m.in. przez db_writer -> 'insert_candle'.
        """
        if not candle:
            return

        volume_quote = candle.get("volume_quote")
        taker_buy_volume_quote = candle.get("taker_buy_volume_quote")

        # DB ma NOT NULL na volume_quote (i często też na taker_buy_volume_quote),
        # więc dla bezpieczeństwa wymuszamy 0.0 jeśli przyszło None.
        if volume_quote is None:
            volume_quote = 0.0
        if taker_buy_volume_quote is None:
            taker_buy_volume_quote = 0.0

        row = {
            "symbol": symbol,
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume_quote": volume_quote,
            "taker_buy_volume_quote": taker_buy_volume_quote,
            "open_time": candle.get("open_time"),
            "close_time": candle.get("close_time"),
            "inserted_at": candle.get("inserted_at") or datetime.utcnow(),
        }

        self.insert_candles([row])

    def upsert_live_candle(self, symbol: str, candle: Dict[str, Any]) -> None:
        """
        Upsert świecy LIVE do public.candles.
        Technicznie to to samo co insert_candle, ale nazwa jest
        czytelniejsza w kontekście db_writer / livefetcher.
        """
        if not candle:
            return

        volume_quote = candle.get("volume_quote")
        taker_buy_volume_quote = candle.get("taker_buy_volume_quote")

        # Dla świecy live nie mamy jeszcze wolumenów z klines,
        # więc zapisujemy 0.0 – potem zamknięta świeca je nadpisze.
        if volume_quote is None:
            volume_quote = 0.0
        if taker_buy_volume_quote is None:
            taker_buy_volume_quote = 0.0

        row = {
            "symbol": symbol,
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume_quote": volume_quote,
            "taker_buy_volume_quote": taker_buy_volume_quote,
            "open_time": candle.get("open_time"),
            "close_time": candle.get("close_time"),
            "inserted_at": candle.get("inserted_at") or datetime.utcnow(),
        }

        self.insert_candles([row])


    # ---- END OF LIVE-FETCHER Methods ----

    def get_candles(
            self,
            symbol: str,
            start: Optional[Any] = None,
            end: Optional[Any] = None,
            limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Zwraca świece dla danego symbolu z tabeli public.candles,
        posortowane po close_time (rosnąco).

        Gwarancje:
        - kolumny: symbol, open, high, low, close, volume_quote,
                   taker_buy_volume_quote, open_time, close_time, inserted_at
        - open_time / close_time / inserted_at -> pandas datetime64[ns, UTC]
        - dodatkowa kolumna 'timestamp' = close_time jako epoch seconds (float)
        - ZERO kombinowania z wierszami (żadnych dropów, żadnego “naprawiania”).
        """
        import logging
        t0 = time.perf_counter() if DB_DEBUG else None

        if pd is None:
            raise RuntimeError("pandas is required for get_candles()")

        params: List[Any] = [symbol]
        where = ["symbol = %s"]

        # zakres czasowy po close_time (jeśli podany)
        if start is not None:
            where.append("close_time >= %s")
            params.append(_coerce_ts(start))
        if end is not None:
            where.append("close_time <= %s")
            params.append(_coerce_ts(end))

        where_sql = " AND ".join(where) if where else "TRUE"
        lim_sql = f"LIMIT {int(limit)}" if limit else ""

        q = f"""
            SELECT
                symbol,
                open,
                high,
                low,
                close,
                volume_quote,
                taker_buy_volume_quote,
                open_time,
                close_time,
                inserted_at
            FROM public.candles
            WHERE {where_sql}
            ORDER BY close_time DESC
            {lim_sql};
        """

        # --- WAŻNE: czytamy klasycznym _fetchall(), a NIE pandas.read_sql ---
        # Psycopg3 z row_factory=dict_row + pandas.read_sql potrafi tworzyć
        # dziwne ramki, gdzie w wierszach lądują nazwy kolumn (tak jak w logu
        # z błędu). Żeby to obejść i mieć 100% kontroli, pobieramy dane
        # jako listę dictów, a potem sami budujemy DataFrame.
        rows = self._fetchall(q, params)

        if not rows:
            if DB_DEBUG and t0 is not None:
                elapsed = (time.perf_counter() - t0) * 1000.0
                logging.warning("[DB][PERF] get_candles(%s, 0 rows) took %.1f ms", symbol, elapsed)
            logging.warning("[DB] get_candles(%s) -> 0 rows", symbol)
            # Zwróć pustą ramkę z oczekiwanymi kolumnami
            return pd.DataFrame(columns=CANDLE_COLS)

        df = pd.DataFrame.from_records(rows)

        # Uporządkuj kolejność kolumn: najpierw standardowe, potem ewentualne dodatki
        try:
            ordered = [c for c in CANDLE_COLS if c in df.columns] + [
                c for c in df.columns if c not in CANDLE_COLS
            ]
            df = df[ordered]
        except Exception:
            # w razie czego użyj whatever pandas ustawił
            pass


        # Posortuj rosnąco po close_time, żeby GUI zawsze dostawało chronologiczny ciąg
        try:
            if "close_time" in df.columns:
                df = df.sort_values("close_time").reset_index(drop=True)
        except Exception:
            pass
        # >>> RAW log z DB <<<
        try:
            logging.warning(
                "[DB][DEBUG] get_candles(%s) RAW rows=%d, cols=%s",
                symbol,
                len(df),
                list(df.columns),
            )
            if not df.empty:
                dtypes_map = {c: str(df[c].dtype) for c in df.columns}
                logging.warning(
                    "[DB][DEBUG] get_candles(%s) RAW dtypes=%s",
                    symbol,
                    dtypes_map,
                )
                logging.warning(
                    "[DB][DEBUG] get_candles(%s) RAW head:\n%s",
                    symbol,
                    df.head(5).to_string(),
                )
        except Exception as e:
            logging.warning("[DB][DEBUG] get_candles(%s) logging failed: %s", symbol, e)

        # Konwersja kolumn czasowych do UTC datetime (bez żadnego przesuwania)
        for c in ("open_time", "close_time", "inserted_at"):
            if c in df.columns:
                try:
                    df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
                except Exception as e:
                    logging.warning(
                        "[DB] get_candles(%s) failed to normalize %s: %s",
                        symbol,
                        c,
                        e,
                    )

        # Dodatkowa kolumna 'timestamp' z close_time
        if "close_time" in df.columns:
            ct = df["close_time"]
            if pd.api.types.is_datetime64_any_dtype(ct):
                try:
                    ts = (ct.view("int64") // 10 ** 9).astype("float64")
                    df["timestamp"] = ts
                except Exception as e:
                    logging.warning(
                        "[DB] get_candles(%s) failed to derive timestamp: %s",
                        symbol,
                        e,
                    )
            else:
                logging.warning(
                    "[DB] get_candles(%s) close_time not datetime64_any (dtype=%s)",
                    symbol,
                    ct.dtype,
                )

        if DB_DEBUG and t0 is not None:
            elapsed = (time.perf_counter() - t0) * 1000.0
            logging.warning("[DB][PERF] get_candles(%s, %d rows) took %.1f ms", symbol, len(df), elapsed)
        else:
            logging.info("[DB] get_candles(%s) -> %d rows (post-normalize)", symbol, len(df))
        return df

    # ---- test config metadata ----

    def insert_test_config_metadata(
            self,
            test_id: int,
            strategy_name: Optional[str],
            symbols: Optional[Iterable[str]],
            start_date: Optional[Any],
            end_date: Optional[Any],
            candle_interval: Optional[str],
            status: str,
            config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.upsert_test_config_metadata(
            test_id=test_id,
            strategy_name=strategy_name,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            candle_interval=candle_interval,
            status=status,
        )

    def upsert_test_config_metadata(
            self,
            test_id: int,
            strategy_name: Optional[str] = None,
            symbols: Optional[Iterable[str]] = None,
            start_date: Optional[Any] = None,
            end_date: Optional[Any] = None,
            candle_interval: Optional[str] = None,
            status: str = "finished",
            config: Optional[Dict[str, Any]] = None,
    ) -> None:
        import logging

        sym_list = list(symbols) if symbols is not None else None

        # ustal typ kolumn symbols / config (jsonb vs ARRAY)
        try:
            col_info = self._fetchall(
                """
                SELECT column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'test_config_meta';
                """
            )
        except Exception:
            col_info = []
        existing = {row["column_name"]: (row["data_type"], row.get("udt_name")) for row in col_info}

        symbols_val = None
        if sym_list is not None:
            dt, udt = existing.get("symbols", (None, None))
            if udt in ("json", "jsonb") or dt == "jsonb":
                symbols_val = _json_dumps_or_none(sym_list)
            elif dt == "ARRAY":
                symbols_val = sym_list  # TEXT[]
            else:
                symbols_val = _json_dumps_or_none(sym_list)

        cfg_val = _json_dumps_or_none(config)

        payload: Dict[str, Any] = {
            "test_id": test_id,
            "strategy_name": strategy_name,
            "symbols": symbols_val,
            "start_date": _coerce_ts(start_date),
            "end_date": _coerce_ts(end_date),
            "candle_interval": candle_interval,
            "status": status,
            "config": cfg_val,
        }

        # sprawdź czy wiersz już istnieje
        row = None
        try:
            row = self._fetchone(
                "SELECT test_id FROM public.test_config_meta WHERE test_id = %s;",
                (test_id,),
            )
        except Exception as e:
            logging.warning("[DB] upsert_test_config_metadata: SELECT failed for test_id=%s: %s", test_id, e)

        # --- INSERT: rekord nie istnieje ---
        if row is None:
            cols_insert = ["test_id", "strategy_name", "symbols", "start_date",
                           "end_date", "candle_interval", "status", "config"]
            col_sql = ", ".join(cols_insert)
            placeholders = ", ".join(["%s"] * len(cols_insert))
            vals = [payload.get(c) for c in cols_insert]
            self._execute(
                f"INSERT INTO public.test_config_meta ({col_sql}) VALUES ({placeholders});",
                vals,
            )
            logging.info("[DB] upsert_test_config_metadata(test_id=%s, status=%s) [insert]", test_id, status)
            return

        # --- UPDATE: rekord istnieje – zmieniamy TYLKO pola != None ---
        update_cols = [
            c for c in ["strategy_name", "symbols", "start_date",
                        "end_date", "candle_interval", "status", "config"]
            if payload.get(c) is not None
        ]

        if not update_cols:
            logging.info("[DB] upsert_test_config_metadata(test_id=%s): nothing to update", test_id)
            return

        set_sql = ", ".join(f"{c} = %s" for c in update_cols)
        vals = [payload[c] for c in update_cols] + [test_id]
        self._execute(
            f"UPDATE public.test_config_meta SET {set_sql} WHERE test_id = %s;",
            vals,
        )
        logging.info("[DB] upsert_test_config_metadata(test_id=%s, status=%s) [update]", test_id, status)

    def save_run_config(self, test_id, strategy_name, settings) -> None:
        """
        Backwards-compatible helper for GUI.

        Zapisuje pełną konfigurację uruchomienia testu (strategy_name + settings)
        do kolumn `strategy_name` oraz `config` w public.test_config_meta.

        W praktyce jest to cienka nakładka na upsert_test_config_metadata,
        żeby zachować stary interfejs używany w GUI.
        """
        try:
            tid = int(test_id)
        except Exception:
            tid = test_id

        # Tutaj NIE nadpisujemy symboli / dat / interwału – to już zostało
        # zapisane chwilę wcześniej przy pierwszym wywołaniu
        # upsert_test_config_metadata w main_window.test_strategy().
        self.upsert_test_config_metadata(
            test_id=tid,
            strategy_name=strategy_name,
            config=settings,
        )
        logging.info("[DB] save_run_config(test_id=%s, strategy_name=%s)", tid, strategy_name)

    # ---- indicators table helpers ----

    def ensure_indicator_table(self, table_name: str, indicator_names: Optional[Iterable[str]] = None) -> None:
        """
        Ensure that the indicators table exists and has all requested dynamic columns.

        This version caches indicator columns per-table in memory to avoid
        running the heavy DDL / information_schema queries on every write.
        """
        ind_names = _norm_cols(indicator_names or [])

        # Drop control / time columns from dynamic numeric schema
        # These are handled as dedicated columns elsewhere.
        ind_names = {
            c
            for c in ind_names
            if c not in {"CLOSE_PRICE", "INSERTED_AT", "TIMESTAMP", "SYMBOL", "OPEN_TIME", "CLOSE_TIME"}
        }
        if not ind_names:
            # nothing dynamic to ensure
            return

        # In-memory cache: table_name -> set(columns already ensured)
        cache = getattr(self, "_indicator_columns_cache", None)
        if cache is None:
            self._indicator_columns_cache = {}
            cache = self._indicator_columns_cache

        have = cache.get(table_name)
        if have is not None and set(ind_names).issubset(have):
            # we already know these columns exist
            return

        # --- existing on-disk definition + alter-if-needed ---
        self._execute(f"""
            CREATE TABLE IF NOT EXISTS public.{table_name} (
                symbol      TEXT NOT NULL,
                open_time   TIMESTAMPTZ,
                close_time  TIMESTAMPTZ,
                close_price DOUBLE PRECISION,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        for base_sql in [
            f"ALTER TABLE public.{table_name} ADD COLUMN IF NOT EXISTS symbol TEXT;",
            f"ALTER TABLE public.{table_name} ADD COLUMN IF NOT EXISTS open_time TIMESTAMPTZ;",
            f"ALTER TABLE public.{table_name} ADD COLUMN IF NOT EXISTS close_time TIMESTAMPTZ;",
            f"ALTER TABLE public.{table_name} ADD COLUMN IF NOT EXISTS close_price DOUBLE PRECISION;",
            f"ALTER TABLE public.{table_name} ADD COLUMN IF NOT EXISTS inserted_at TIMESTAMPTZ NOT NULL DEFAULT now();",
        ]:
            self._execute(base_sql)

        existing = {
            row["column_name"].upper()
            for row in self._fetchall(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s;
                """,
                (table_name,),
            )
        }

        to_add = [c for c in ind_names if c not in existing]
        for col in to_add:
            self._execute(f'ALTER TABLE public.{table_name} ADD COLUMN IF NOT EXISTS "{col}" DOUBLE PRECISION;')

        self._execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table_name}_sym_ot_ct ON public.{table_name}(symbol, open_time, close_time);")
        self._execute(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_sym_ct ON public.{table_name}(symbol, close_time);")
        self._execute(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_sym_ot ON public.{table_name}(symbol, open_time);")

        # update cache
        cache[table_name] = set(existing).union(ind_names)

    def clear_indicators_table(self, table_name: str) -> None:
        self._execute(f"TRUNCATE TABLE public.{table_name};")

    def clear_table(self, table_name: str) -> None:
        self._execute(f"TRUNCATE TABLE public.{table_name};")

    def create_indicators_table(self, table_name: str, indicator_names: Optional[Iterable[str]] = None,
                                table_type: Optional[str] = None) -> str:
        self.ensure_indicator_table(table_name, indicator_names)
        return table_name

    def insert_indicator_rows(self, table_name: str, rows, indicator_names: Optional[Iterable[str]] = None) -> None:
        if not rows:
            return

        # Jeśli ani zapis "w trakcie testu", ani zapis "po teście z RAM-u" nie są włączone,
        # to bezpiecznie pomijamy inserty wskaźników.
        if not WRITE_INDICATORS_TO_DB and not WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST:
            if DB_DEBUG:
                logging.warning(
                    "[DB][SKIP] insert_indicator_rows(%s, %d rows) – disabled by config "
                    "(WRITE_INDICATORS_TO_DB=False, WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST=False)",
                    table_name, len(rows),
                )
            return

        t0 = time.perf_counter() if DB_DEBUG else None

        # Normalize to dict payloads with UPPERCASE keys
        norm: List[Dict[str, Any]] = []
        for r in rows:
            if isinstance(r, dict):
                payload = {(k.upper() if isinstance(k, str) else k): v for k, v in r.items()}
            else:
                # expected tuple: (symbol, close_time, dict, ordered_cols)
                try:
                    sym, ct, store, _ordered = r
                except Exception:
                    continue
                payload = {(k.upper() if isinstance(k, str) else k): v for k, v in (store or {}).items()}
                payload["SYMBOL"] = sym
                payload["CLOSE_TIME"] = ct

            payload.setdefault("CLOSE_PRICE", None)
            norm.append(payload)

        # Backfill OPEN_TIME via candles(symbol, close_time) – optional (can be disabled)
        if INDICATORS_BACKFILL_OPEN_TIME:
            missing = [
                (p.get("SYMBOL"), p.get("CLOSE_TIME"))
                for p in norm
                if p.get("SYMBOL") and p.get("CLOSE_TIME") and not p.get("OPEN_TIME")
            ]
            if missing:
                syms = [s for s, _ in missing]
                cts = [c for _, c in missing]
                placeholders_sym = ",".join(["%s"] * len(syms))
                placeholders_ct = ",".join(["%s"] * len(cts))
                sql = f"""
                    SELECT c.symbol, c.close_time, c.open_time
                    FROM public.candles c
                    JOIN (
                        SELECT UNNEST(ARRAY[{placeholders_sym}]) AS symbol,
                               UNNEST(ARRAY[{placeholders_ct}])  AS close_time
                        ) x
                      ON c.symbol = x.symbol AND c.close_time = x.close_time
                """
                # Map by real datetime objects (TIMESTAMPTZ); comparing aware datetimes
                # is timezone-agnostic (UTC-normalized), unlike string representations.
                mapping: Dict[Tuple[str, Any], Any] = {}
                for row in self._fetchall(sql, tuple(syms) + tuple(cts)):
                    mapping[(row["symbol"], row["close_time"])] = row["open_time"]

                for p in norm:
                    if not p.get("OPEN_TIME") and p.get("SYMBOL") and p.get("CLOSE_TIME"):
                        k = (p.get("SYMBOL"), p.get("CLOSE_TIME"))
                        if k in mapping:
                            p["OPEN_TIME"] = mapping[k]



        # Attach FEAR_GREED from public.fear_greed based on candle date (DATE(CLOSE_TIME)).
        # This lets us keep Fear&Greed as a separate daily time series and still
        # have FEAR_GREED available as a dynamic indicator column for each row.
        try:
            # collect unique days present in this batch
            fng_days = set()
            for p in norm:
                ct = p.get("CLOSE_TIME")
                # psycopg returns TIMESTAMPTZ as Python datetime; we only care about the date component here
                if ct is not None and hasattr(ct, "date"):
                    fng_days.add(ct.date())
            fng_map = {}
            if fng_days:
                placeholders = ",".join(["%s"] * len(fng_days))
                sql_fng = f"SELECT day, value FROM public.fear_greed WHERE day IN ({placeholders})"
                # use raw _fetchall to avoid hard-wiring pandas, etc.
                for row in self._fetchall(sql_fng, list(fng_days)):
                    d = row.get("day")
                    v = row.get("value")
                    if d is not None and v is not None:
                        try:
                            fng_map[d] = float(v)
                        except Exception:
                            continue
            if fng_map:
                for p in norm:
                    # If worker/strategy already filled FEAR_GREED explicitly, don't override it.
                    if p.get("FEAR_GREED") is not None:
                        continue
                    ct = p.get("CLOSE_TIME")
                    if ct is not None and hasattr(ct, "date"):
                        val = fng_map.get(ct.date())
                        if val is not None:
                            p["FEAR_GREED"] = val
        except Exception as e:
            if DB_DEBUG:
                logging.warning("[DB][FNG] attach FEAR_GREED to %s failed: %s", table_name, e)
        # Ensure dynamic columns (ignore core fields + TIMESTAMP in DB;
        # timestamp do GUI ogarniamy przy SELECT w get_indicator_table)
        # Ensure dynamic columns (ignore core fields + TIMESTAMP/TEST_ID in DB).
        # If indicator_names are provided (from worker), use them directly to avoid
        # rescanning all payload dicts on every batch.
        dyn: set = set()
        reserved = {
            "SYMBOL",
            "OPEN_TIME",
            "CLOSE_TIME",
            "CLOSE_PRICE",
            "INSERTED_AT",
            "TIMESTAMP",
            "TEST_ID",
        }
        if indicator_names:
            try:
                for name in indicator_names:
                    if not isinstance(name, str):
                        continue
                    k = name.upper()
                    if k in reserved:
                        continue
                    dyn.add(k)
            except Exception:
                # Fallback to scanning payload keys if something goes wrong.
                for p in norm:
                    for k in p.keys():
                        if k not in reserved:
                            dyn.add(k)
        else:
            for p in norm:
                for k in p.keys():
                    if k not in reserved:
                        dyn.add(k)


        self.ensure_indicator_table(table_name, dyn)

        dyn_cols_sorted = sorted(dyn)
        base_cols = ["symbol", "open_time", "close_time", "close_price"]
        all_cols_sql = base_cols + [f'"{c}"' for c in dyn_cols_sorted]
        placeholders = ",".join(["%s"] * len(all_cols_sql))

        values: List[Tuple[Any, ...]] = []
        for p in norm:
            row_vals: List[Any] = [
                p.get("SYMBOL"),
                p.get("OPEN_TIME"),
                p.get("CLOSE_TIME"),
                p.get("CLOSE_PRICE"),
            ]
            for c in dyn_cols_sorted:
                row_vals.append(p.get(c))
            values.append(tuple(row_vals))

        # Chunked UPSERT
        # Postgres has a hard limit of 65535 parameters per statement.
        # Each row contributes len(all_cols_sql) parameters, so we cap the effective
        # batch size accordingly to avoid OperationalError.
        requested_batch = int(INDICATOR_BATCH_SIZE) if INDICATOR_BATCH_SIZE else 1000
        cols_per_row = max(1, len(all_cols_sql))
        max_params = 60000  # stay safely below 65535
        max_rows_by_params = max(1, max_params // cols_per_row)
        batch_size = min(requested_batch, max_rows_by_params)

        for i in range(0, len(values), batch_size):
            chunk = values[i: i + batch_size]
            values_sql = ",".join(["(" + placeholders + ")"] * len(chunk))
            flat = tuple(v for row in chunk for v in row)

            upd_cols = ["close_price"] + dyn_cols_sorted
            set_sql = ", ".join(
                [
                    (f'"{c}"=EXCLUDED."{c}"' if c in dyn_cols_sorted else f"{c}=EXCLUDED.{c}")
                    for c in upd_cols
                ]
            )

            if INDICATOR_UPSERT:
                sql = f"""
                    INSERT INTO public.{table_name} ({", ".join(all_cols_sql)})
                    VALUES {values_sql}
                    ON CONFLICT (symbol, open_time, close_time) DO UPDATE SET {set_sql};
                """
            else:
                sql = f"""
                    INSERT INTO public.{table_name} ({", ".join(all_cols_sql)})
                    VALUES {values_sql};
                """

            self._execute(sql, flat)


        if DB_DEBUG and t0 is not None:
            elapsed = (time.perf_counter() - t0) * 1000.0
            logging.warning("[DB][PERF] insert_indicator_rows(%s, %d rows) took %.1f ms",
                table_name,
                len(rows),
                elapsed,
            )

    def get_indicators(self, symbol, start=None, end=None):
        """Convenience wrapper – always read from public.indicators_historical."""
        return self.get_indicator_table(symbol=symbol, start=start, end=end, table_name="indicators_historical")

    def get_indicator_table(
            self,
            symbol: str,
            start: Optional[Any] = None,
            end: Optional[Any] = None,
            table_name: str = "indicators_historical",
    ) -> pd.DataFrame:
        """
        Zwraca DataFrame ze wszystkimi wskaźnikami dla danego symbolu
        z podanej tabeli (domyślnie public.indicators_historical) i
        DODATKOWO gwarantuje istnienie oraz wypełnienie kolumny
        'timestamp' na podstawie close_time (UNIX epoch w sekundach, UTC).

        ZERO “magii” w danych – tylko normalizacja typów czasowych.
        """
        import logging

        if pd is None:
            raise RuntimeError("pandas is required for get_indicator_table()")

        params: List[Any] = [symbol]
        q: str

        if start is not None and end is not None:
            params.extend([_coerce_ts(start), _coerce_ts(end)])
            q = f"""
                SELECT *
                FROM public.{table_name}
                WHERE symbol = %s
                  AND close_time BETWEEN %s AND %s
                ORDER BY close_time;
            """
        else:
            q = f"""
                SELECT *
                FROM public.{table_name}
                WHERE symbol = %s
                ORDER BY close_time;
            """

        # Tak jak w get_candles – najpierw list[dict], potem DataFrame,
        # żeby nie mieszać pandas.read_sql z row_factory=dict_row.
        rows = self._fetchall(q, tuple(params))
        if not rows:
            logging.warning("[DB] get_indicator_table(%s, %s): EMPTY (rows=0)", table_name, symbol)
            return pd.DataFrame()

        df = pd.DataFrame.from_records(rows)

        # RAW log, tak jak dla świec
        try:
            logging.warning(
                "[DB][DEBUG] get_indicator_table(%s, %s) RAW rows=%d, cols=%s",
                table_name,
                symbol,
                len(df),
                list(df.columns),
            )
            dtypes_map = {c: str(df[c].dtype) for c in df.columns}
            logging.warning(
                "[DB][DEBUG] get_indicator_table(%s, %s) RAW dtypes=%s",
                table_name,
                symbol,
                dtypes_map,
            )
            logging.warning(
                "[DB][DEBUG] get_indicator_table(%s, %s) RAW head:\n%s",
                table_name,
                symbol,
                df.head(5).to_string(),
            )
        except Exception as e:
            logging.warning(
                "[DB][DEBUG] get_indicator_table(%s, %s) logging failed: %s",
                table_name,
                symbol,
                e,
            )

        # Normalizacja czasu
        if "close_time" in df.columns:
            try:
                ct = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
                df["close_time"] = ct
                if ct.notna().any():
                    logging.warning(
                        "[DB] get_indicator_table(%s, %s): rows=%d, close_time range: %s .. %s",
                        table_name,
                        symbol,
                        len(df),
                        ct.min(),
                        ct.max(),
                    )
                else:
                    logging.warning(
                        "[DB] get_indicator_table(%s, %s): rows=%d, ALL close_time NaT",
                        table_name,
                        symbol,
                        len(df),
                    )
            except Exception as e:
                logging.error(
                    "[DB] get_indicator_table(%s, %s): failed to normalize close_time: %s",
                    table_name,
                    symbol,
                    e,
                )
        else:
            logging.warning(
                "[DB] get_indicator_table(%s, %s): rows=%d, NO close_time column",
                table_name,
                symbol,
                len(df),
            )

        # timestamp tylko z close_time
        if "close_time" in df.columns and pd.api.types.is_datetime64_any_dtype(df["close_time"]):
            try:
                ts = (df["close_time"].view("int64") // 10 ** 9).astype("float64")
                df["timestamp"] = ts
            except Exception as e:
                logging.warning(
                    "[DB] get_indicator_table(%s, %s): failed to derive timestamp: %s",
                    table_name,
                    symbol,
                    e,
                )

        return df

    # ---- trades & stats ----
    def insert_trade_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        """
        Zapisuje listę trejdów do public.trades.

        Zakładamy, że każdy dict w rows ma (przynajmniej):
          - test_id (int) – jeśli None, wstawiamy NULL
          - symbol (TEXT)
          - side (TEXT, 'long'/'short')
          - signal_type / open_signal_type / close_signal_type / close_reason (opcjonalne)
          - open_time / close_time (TIMESTAMPTZ lub coś co psycopg ogarnie)
          - entry_price / exit_price / amount / fee / pnl (DOUBLE PRECISION)
          - tp_open / sl_open / initial_benchmark / initial_ts (DOUBLE PRECISION, opcjonalne)
        """
        if not rows:
            return

        t0 = time.perf_counter() if DB_DEBUG else None

        self._ensure_trades_table()

        # Batch INSERT trades to minimize round-trips
        base_sql = """
        INSERT INTO public.trades
        (test_id, symbol, side, signal_type, open_signal_type, close_signal_type, close_reason,
         open_time, close_time, entry_price, exit_price, amount, fee, pnl,
         tp_open, sl_open, initial_benchmark, initial_ts)
        VALUES {values}
        ON CONFLICT DO NOTHING;
        """
        # Prepare all value tuples first
        all_values = []
        for r in rows:
            all_values.append(
                (
                    r.get("test_id"),
                    r.get("symbol"),
                    r.get("side"),
                    r.get("signal_type"),
                    r.get("open_signal_type"),
                    r.get("close_signal_type"),
                    r.get("close_reason"),
                    r.get("open_time"),
                    r.get("close_time"),
                    r.get("entry_price"),
                    r.get("exit_price"),
                    r.get("amount"),
                    r.get("fee"),
                    r.get("pnl"),
                    r.get("tp_open"),
                    r.get("sl_open"),
                    r.get("initial_benchmark"),
                    r.get("initial_ts"),
                )
            )

        # Chunk into configurable batch size
        batch_size = int(TRADE_BATCH_SIZE) if TRADE_BATCH_SIZE else 500
        placeholders_row = "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        with self._conn() as cur:
            for i in range(0, len(all_values), batch_size):
                chunk = all_values[i: i + batch_size]
                values_sql = ",".join([placeholders_row] * len(chunk))
                flat_params = tuple(v for row in chunk for v in row)
                sql = base_sql.replace("{values}", values_sql)
                cur.execute(sql, flat_params)

        if DB_DEBUG and t0 is not None:
            elapsed = (time.perf_counter() - t0) * 1000.0
            logging.warning("[DB][PERF] insert_trade_rows(%d rows) took %.1f ms",
                len(rows),
                elapsed,
            )

    def insert_test_stats(self, test_id: int, rows: Sequence[Dict[str, Any]]) -> None:
        """
        Zapisuje zbiorcze statystyki testu do test_stats.
        Każdy row = {symbol?, metric_key, metric_value}.
        """
        if not rows:
            return

        sql = """
        INSERT INTO public.test_stats
        (test_id, symbol, metric_key, metric_value)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT DO NOTHING;
        """
        # batch w jednej transakcji – unikamy otwierania połączenia per wiersz
        with self._conn() as cur:
            for row in rows:
                symbol = row.get("symbol")
                metric_key = row.get("metric_key") or row.get("key") or row.get("name")
                metric_value = row.get("metric_value") or row.get("value")
                if metric_key is None:
                    continue
                cur.execute(sql, (test_id, symbol, metric_key, metric_value))

    def insert_stats_rows(self, test_id: int, rows: Sequence[Dict[str, Any]]) -> None:
        """
        Zapisuje zbiorcze statystyki testu do docelowej tabeli public.stats.

        Każdy row pochodzi bezpośrednio z StrategyTestWorker:
        {
            "symbol": ...,
            "trades": ...,
            "pnl_sum": ...,
            "pnl_avg": ...,
            "fee_sum": ...,
            "winrate": ...,
            "avg_win": ...,
            "avg_loss": ...,
        }
        """
        if not rows:
            return

        sql = """
        INSERT INTO public.stats
        (test_id, symbol, trades, pnl_sum, pnl_avg, fee_sum, winrate, avg_win, avg_loss)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (test_id, symbol) DO UPDATE
        SET trades   = EXCLUDED.trades,
            pnl_sum  = EXCLUDED.pnl_sum,
            pnl_avg  = EXCLUDED.pnl_avg,
            fee_sum  = EXCLUDED.fee_sum,
            winrate  = EXCLUDED.winrate,
            avg_win  = EXCLUDED.avg_win,
            avg_loss = EXCLUDED.avg_loss;
        """

        with self._conn() as cur:
            for row in rows:
                symbol = row.get("symbol")
                if not symbol:
                    continue
                cur.execute(
                    sql,
                    (
                        test_id,
                        symbol,
                        row.get("trades"),
                        row.get("pnl_sum"),
                        row.get("pnl_avg"),
                        row.get("fee_sum"),
                        row.get("winrate"),
                        row.get("avg_win"),
                        row.get("avg_loss"),
                    ),
                )

    def next_free_test_id(self) -> int:
        """
        Zwraca następne wolne test_id.

        Podstawowo liczymy po test_config_meta.
        Jeśli tabela jest pusta albo coś pójdzie nie tak,
        fallback na test_stats, a na końcu 1.
        """
        # podstawowe źródło – test_config_meta
        row = None
        try:
            row = self._fetchone(
                "SELECT COALESCE(MAX(test_id), 0) + 1 AS next_id FROM public.test_config_meta;"
            )
        except Exception:
            row = None

        if row and row.get("next_id") is not None:
            try:
                return int(row["next_id"])
            except Exception:
                pass

        # fallback – test_stats (gdyby meta była jeszcze nieużywana)
        try:
            row2 = self._fetchone(
                "SELECT COALESCE(MAX(test_id), 0) + 1 AS next_id FROM public.test_stats;"
            )
        except Exception:
            row2 = None

        if row2 and row2.get("next_id") is not None:
            try:
                return int(row2["next_id"])
            except Exception:
                pass

        # ostateczny fallback
        return 1

    def replace_stats_rows(
            self,
            rows: Sequence[Dict[str, Any]],
            test_id: Optional[int] = None,
    ) -> None:
        """
        Backwards-compatible API dla GUI (db_writer).

        Semantyka jak w wersji sqlite:
        - usuń istniejące statystyki dla danego test_id,
        - wstaw nowe wiersze.

        StrategyTestWorker przekazuje tu "szerokie" wiersze per symbol, np.:
        {
            "symbol": "BTCUSDT",
            "trades": 10,
            "pnl_sum": ...,
            "pnl_avg": ...,
            "fee_sum": ...,
            "winrate": ...,
            "avg_win": ...,
            "avg_loss": ...,
        }

        My musimy to zamienić na format {symbol, metric_key, metric_value}
        oczekiwany przez insert_test_stats().
        """
        if not rows:
            return
        if test_id is None:
            raise ValueError("replace_stats_rows() wymaga test_id")

        tid = int(test_id)

        # wyczyść stare statystyki tego testu (K/V)
        self._execute("DELETE FROM public.test_stats WHERE test_id = %s;", (tid,))

        kv_rows: List[Dict[str, Any]] = []

        for row in rows:
            symbol = row.get("symbol")
            # każdy klucz inny niż "symbol" traktujemy jako metric_key
            for key, value in row.items():
                if key == "symbol":
                    continue
                kv_rows.append(
                    {
                        "symbol": symbol,
                        "metric_key": key,
                        "metric_value": value,
                    }
                )

        # teraz zapisujemy już w formacie K/V
        self.insert_test_stats(tid, kv_rows)

    def get_last_ticks(self, trade_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            if trade_id is not None:
                return self._fetchall(
                    """
                    SELECT trade_id, ts, price, qty, side
                    FROM public.trades_ticks
                    WHERE trade_id = %s
                    ORDER BY ts DESC
                    LIMIT %s;
                    """,
                    (trade_id, int(limit)),
                )
            return self._fetchall(
                """
                SELECT trade_id, ts, price, qty, side
                FROM public.trades_ticks
                ORDER BY ts DESC
                LIMIT %s;
                """,
                (int(limit),),
            )
        except Exception:
            return []