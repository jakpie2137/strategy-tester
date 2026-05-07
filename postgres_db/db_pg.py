# -*- coding: utf-8 -*-
"""
Postgres utils — INSERT/COPY switchable via .env.
COPY uses psycopg3 context manager; optional fallback to INSERT per chunk.
"""
import os
import logging
import socket
import time
import contextlib
from io import StringIO
from typing import Iterable, Tuple, Optional, Dict
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from psycopg import connect
from pathlib import Path
from config_pg import load_env, get_dsn, mask_dsn

logger = logging.getLogger(__name__)
ENV_DIR = Path(__file__).resolve().parent

CONNECT_TIMEOUT_S = float(os.getenv("PG_CONNECT_TIMEOUT", "3"))
TCP_PRECHECK_TIMEOUT_S = float(os.getenv("PG_TCP_PRECHECK_TIMEOUT", "1.5"))
CONNECT_ATTEMPTS = int(os.getenv("PG_CONNECT_ATTEMPTS", "3"))
WRITE_MODE = os.getenv("PG_WRITE_MODE", "copy").lower()  # "copy" | "insert"

INSERT_CHUNK_SIZE = int(os.getenv("PG_INSERT_CHUNK_SIZE", "5000"))
COPY_CHUNK_SIZE   = int(os.getenv("PG_COPY_CHUNK_SIZE",   "50000"))

CandleRow = Tuple[str, float, float, float, float, float, float, object, object]

def _merge_query_params(url: str, extra: Dict[str, str]) -> str:
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in extra.items()})
    return urlunparse(u._replace(query=urlencode(q)))

def _tcp_precheck(dsn: str, timeout_s: float) -> None:
    u = urlparse(dsn)
    host = u.hostname or "127.0.0.1"
    port = u.port or 5432
    logger.debug("TCP precheck → %s:%s (timeout=%.1fs)", host, port, timeout_s)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return
    except OSError as e:
        raise ConnectionError(f"TCP precheck failed for {host}:{port}: {e}")

def ensure_database_exists(dsn: Optional[str] = None) -> None:
    load_env(ENV_DIR / ".env")
    dsn = dsn or get_dsn()
    u = urlparse(dsn)
    target_db = (u.path or '/').lstrip('/')
    if not target_db or target_db == 'postgres':
        logger.debug("ensure_database_exists: target=%r (skip create)", target_db or 'postgres')
        return
    maint = u._replace(path='/postgres')
    q = dict(parse_qsl(maint.query)); q["connect_timeout"] = str(CONNECT_TIMEOUT_S)
    maint_dsn = urlunparse(maint._replace(query=urlencode(q)))
    logger.info("Check DB exists %r (DSN=%s)", target_db, mask_dsn(dsn))
    _tcp_precheck(maint_dsn, TCP_PRECHECK_TIMEOUT_S)
    with connect(maint_dsn) as conn, conn.cursor() as cur:
        conn.autocommit = True
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
        if cur.fetchone():
            logger.info("DB %r exists.", target_db); return
        logger.warning("DB %r not found — creating…", target_db)
        cur.execute(f'CREATE DATABASE "{target_db}"')
        logger.info("DB %r created.", target_db)

def _log_target(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("select current_database(), current_schema(), current_setting('port'), inet_server_addr(), current_user, current_setting('search_path')")
        db, schema, port, ip, user, sp = cur.fetchone()
        logger.warning("TARGET → db=%s schema=%s port=%s host=%s user=%s search_path=%s", db, schema, port, ip, user, sp)

def get_conn(dsn: Optional[str] = None):
    load_env(ENV_DIR / ".env")
    base_dsn = dsn or get_dsn()
    dsn_ct = _merge_query_params(base_dsn, {"connect_timeout": str(CONNECT_TIMEOUT_S)})
    _tcp_precheck(dsn_ct, TCP_PRECHECK_TIMEOUT_S)

    last_err = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            logger.info("Łączenie z PG (próba %d/%d): %s", attempt, CONNECT_ATTEMPTS, mask_dsn(dsn_ct))
            conn = connect(dsn_ct)
            prev_autocommit = conn.autocommit
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute("CREATE SCHEMA IF NOT EXISTS public AUTHORIZATION CURRENT_USER;")
                    cur.execute("SET search_path TO public;")
                    cur.execute("SET TIME ZONE 'UTC';")
                    cur.execute("SET application_name = 'feed_binancefut_pg';")
            finally:
                conn.autocommit = prev_autocommit
            _log_target(conn)
            return conn
        except Exception as e:
            last_err = e
            logger.error("PG connect FAIL (próba %d/%d): %r", attempt, CONNECT_ATTEMPTS, e)
            time.sleep(0.5)
    logger.critical("PG connect FINAL FAIL: %r | DSN=%s", last_err, mask_dsn(dsn_ct))
    raise last_err

def ensure_schema(conn, reset: bool = False):
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS public AUTHORIZATION CURRENT_USER;")
        cur.execute("SET LOCAL search_path TO public; SET LOCAL TIME ZONE 'UTC';")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.candles (
            symbol TEXT NOT NULL,
            open   DOUBLE PRECISION NOT NULL,
            high   DOUBLE PRECISION NOT NULL,
            low    DOUBLE PRECISION NOT NULL,
            close  DOUBLE PRECISION NOT NULL,
            volume_quote DOUBLE PRECISION NOT NULL,
            taker_buy_volume_quote DOUBLE PRECISION NOT NULL,
            open_time  TIMESTAMPTZ NOT NULL,
            close_time TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (symbol, open_time)
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_candles_symbol_open_time ON public.candles(symbol, open_time);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_candles_open_time       ON public.candles(open_time);")
        if reset: cur.execute("TRUNCATE TABLE public.candles;")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.candles')")
        if cur.fetchone()[0] is None:
            logger.critical("Tabela public.candles NIE istnieje po DDL."); raise RuntimeError("DDL failed: public.candles missing")
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        logger.warning("DDL OK → tables_in_public=%s", cur.fetchone()[0])
    logger.info("Schemat gotowy (reset=%s).", reset)

def insert_candles(conn, rows: Iterable[CandleRow], chunk_size: int = INSERT_CHUNK_SIZE) -> int:
    rows = list(rows)
    if not rows: return 0
    total = 0
    sql = """
        INSERT INTO public.candles
            (symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, open_time) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL search_path TO public; SET LOCAL TIME ZONE 'UTC';")
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i+chunk_size]
            cur.executemany(sql, chunk)
            total += len(chunk)
        conn.commit()
    logger.info("INSERT: wysłano %d wierszy (duplikaty ignorowane).", total)
    return total

def copy_upsert_candles(conn, rows: Iterable[CandleRow], chunk_size: int = COPY_CHUNK_SIZE) -> int:
    rows = list(rows)
    if not rows: return 0

    fallback_insert = os.getenv("PG_COPY_FALLBACK_INSERT", "0") == "1"
    total_staged = 0
    total_inserted = 0

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS candles_stage(
                symbol TEXT NOT NULL,
                open   DOUBLE PRECISION NOT NULL,
                high   DOUBLE PRECISION NOT NULL,
                low    DOUBLE PRECISION NOT NULL,
                close  DOUBLE PRECISION NOT NULL,
                volume_quote DOUBLE PRECISION NOT NULL,
                taker_buy_volume_quote DOUBLE PRECISION NOT NULL,
                open_time  TIMESTAMPTZ NOT NULL,
                close_time TIMESTAMPTZ NOT NULL
            ) ON COMMIT PRESERVE ROWS;
        """)
        cur.execute("SET LOCAL TIME ZONE 'UTC'; SET LOCAL search_path TO public;")

        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i+chunk_size]

            buf = StringIO()
            for (sym, o, h, l, c, vq, tbvq, ot, ct) in chunk:
                buf.write(f'"{sym}",{o},{h},{l},{c},{vq},{tbvq},"{ot.isoformat()}","{ct.isoformat()}"\n')
            data = buf.getvalue()

            cur.execute("TRUNCATE TABLE candles_stage;")
            copy_sql = (
                "COPY candles_stage (symbol, open, high, low, close, "
                "volume_quote, taker_buy_volume_quote, open_time, close_time) "
                "FROM STDIN WITH (FORMAT csv, HEADER false, DELIMITER ',', QUOTE '\"')"
            )
            with cur.copy(copy_sql) as cp:
                cp.write(data)

            cur.execute("SELECT count(*), min(open_time), max(open_time) FROM candles_stage;")
            staged_count, staged_min, staged_max = cur.fetchone()
            logger.debug("COPY stage ok: rows=%s, window=[%s .. %s]", staged_count, staged_min, staged_max)

            cur.execute("""
                WITH ins AS (
                    INSERT INTO public.candles(symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time)
                    SELECT symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time
                    FROM candles_stage
                    ON CONFLICT (symbol, open_time) DO NOTHING
                    RETURNING 1
                )
                SELECT count(*) FROM ins;
            """)
            inserted = cur.fetchone()[0]

            if inserted == 0 and staged_count > 0 and fallback_insert:
                logger.warning("COPY inserted=0 (staged=%d). Fallback to INSERT for this chunk.", staged_count)
                cur.execute("""
                    SELECT symbol, open, high, low, close, volume_quote, taker_buy_volume_quote, open_time, close_time
                    FROM candles_stage
                """)
                rows_fallback = cur.fetchall()
                conn.commit()
                total_inserted += insert_candles(conn, rows_fallback, chunk_size=max(1000, min(INSERT_CHUNK_SIZE, len(rows_fallback))))
            else:
                total_inserted += inserted

            total_staged += staged_count
            logger.info("COPY: staged=%d, inserted=%d (sum=%d)", staged_count, inserted, total_inserted)

        conn.commit()

    if total_inserted == 0 and total_staged > 0:
        logger.warning("COPY: 0 rows inserted out of %d staged. Sprawdź search_path/uprawnienia/klucz PK.", total_staged)
    return total_inserted

def upsert_candles(conn, rows: Iterable[CandleRow]) -> int:
    mode = (os.getenv("PG_WRITE_MODE", WRITE_MODE) or "copy").lower()
    if mode == "insert":
        return insert_candles(conn, rows, INSERT_CHUNK_SIZE)
    return copy_upsert_candles(conn, rows, COPY_CHUNK_SIZE)

@contextlib.contextmanager
def maybe_autostart_local_pg(dsn: Optional[str] = None):
    dsn = dsn or get_dsn()
    if os.getenv("PG_AUTOSTART", "") != "1":
        yield dsn; return
    try:
        from pgserver import Postgres  # type: ignore
    except Exception as e:
        raise RuntimeError("PG_AUTOSTART=1, ale brak 'pgserver'. pip install pgserver") from e
    pg = Postgres(); pg.start()
    try:
        logger.warning("pgserver: start lokalnego Postgresa @ %s", pg.dsn())
        yield pg.dsn()
    finally:
        pg.stop()
        logger.warning("pgserver: stop serwera")
