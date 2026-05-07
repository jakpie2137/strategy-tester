# gui/chart_worker.py
#
# Lekki worker do zasilania wykresu (historia + live) z opcjonalną agregacją świec
# pod potrzeby GUI:
# - osobny wątek (nie blokuje GUI)
# - na starcie ładuje historię świec z DB i emituje full_refresh(df_plot)
# - w pętli obsługuje tylko live (via LiveFetcher.get_inprogress_candle),
#   nie odpyta bazy w kółko
#
# Tryby:
# - bez fetchera (backtest/statycznie): tylko jednorazowy full_refresh, potem koniec
# - z fetcherem (live): full_refresh + cykliczne live_only(live_candle)
#
# Wskaźniki są ładowane/mergowane po stronie MainWindow (update_symbol_view / _on_full_refresh).
# Worker operuje zawsze na "raw" świecach z bazy (np. 1m), ale do GUI może wysłać
# zagregowany DataFrame (np. po 60 świec w jedną, jeśli config.PLOT_AGGREGATION > 1).

from typing import Optional, Any, Tuple

from PyQt5.QtCore import QThread, pyqtSignal
import logging
import time
import pandas as pd


def build_plot_df(
    df_raw: pd.DataFrame,
    agg_n: int,
    max_plot: int,
    symbol: Optional[str] = None,
    log_prefix: str = "[ChartDataWorker]",
) -> pd.DataFrame:
    """
    Buduje DF pod wykres na podstawie bazowego df_raw (np. 1m).

    - df_raw: świeczki bazowe (np. z public.candles) z kolumnami:
      [open, high, low, close, close_time, (opcjonalnie volume* i wskaźniki)]

    - agg_n:
      1  -> brak agregacji (każda świeca z df_raw osobno)
      >1 -> agregacja wiadrami po agg_n świec:

        dla bucket i (w ramach sortowania po close_time):
          - open = open z 1. świecy w wiadrze
          - close = close z ostatniej świecy w wiadrze
          - high = max(highów)
          - low  = min(lowów)
          - volume* = suma
          - wskaźniki numeryczne = średnia

    - max_plot: ile świec "plotowych" maksymalnie może trafić do wykresu
                (obcinamy najstarsze, jeśli jest więcej).

    Zwraca df_plot z:
      - kolumnami OHLC + ewentualnym wolumenem/wskaźnikami
      - indeksem = close_time (UTC)
      - kolumną 'timestamp' = epoch seconds (float)
    """
    if df_raw is None or df_raw.empty:
        return pd.DataFrame()

    df = df_raw.copy()

    # Upewnij się, że close_time jest datetime UTC + mamy timestamp (epoch seconds)
    if "close_time" not in df.columns:
        return pd.DataFrame()

    df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
    df = df.dropna(subset=["close_time"])
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("close_time")
    df["timestamp"] = df["close_time"].astype("int64") // 10 ** 9

    # Jeśli żadnej agregacji – tylko przytnij do max_plot
    if agg_n is None or agg_n <= 1:
        if max_plot and max_plot > 0:
            df = df.tail(int(max_plot))
        df = df.set_index("close_time")
        return df

    # --- agregacja po wiadrach po agg_n świec ---
    df = df.reset_index(drop=True)
    df["__bucket"] = df.index // int(agg_n)

    numeric_cols = []
    for c in df.columns:
        if c in ("open", "high", "low", "close", "timestamp", "__bucket"):
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            numeric_cols.append(c)

    agg_dict = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "close_time": "last",
        "timestamp": "last",
    }

    for c in numeric_cols:
        agg_dict[c] = "mean"

    grouped = df.groupby("__bucket", as_index=False).agg(agg_dict)

    if max_plot and max_plot > 0:
        grouped = grouped.tail(int(max_plot))

    grouped = grouped.drop(columns=["__bucket"], errors="ignore")
    grouped = grouped.set_index("close_time")

    return grouped


class ChartDataWorker(QThread):
    """
    Worker do ładowania świec z DB i emitowania ich do PlotWidgeta.

    Sygnały:
    - full_refresh(df_raw, df_plot): na starcie, pełny set świec + widok pod wykres
    - live_only(live_candle_dict): tylko świeca in-progress (dict) z LiveFetcher
    - append_closed(closed, new_live): (opcjonalnie) zamknięta świeca + nowa live
    """

    full_refresh = pyqtSignal(object, object)
    live_only = pyqtSignal(object)
    append_closed = pyqtSignal(object, object)

    def __init__(self, db, symbol: str, fetcher: Optional[Any], max_plot: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.symbol = symbol
        self.fetcher = fetcher
        try:
            self.max_plot = int(max_plot) if max_plot else 4000
        except Exception:
            self.max_plot = 4000

        # ile świec bazowych składamy w jedną świecę "plotową"
        # W trybie dynamicznego LOD (PLOT_DYNAMIC_AGG_ENABLED = True) nie robimy
        # statycznej agregacji po stronie workera – agg_n ustawiamy na 1.
        # Przy wyłączonym LOD używamy wartości z config.PLOT_AGGREGATION.
        try:
            from config import PLOT_DYNAMIC_AGG_ENABLED, PLOT_AGGREGATION
        except Exception:
            PLOT_DYNAMIC_AGG_ENABLED = False
            PLOT_AGGREGATION = 1

        if PLOT_DYNAMIC_AGG_ENABLED:
            self.agg_n = 1
        else:
            try:
                self.agg_n = max(1, int(PLOT_AGGREGATION))
            except Exception:
                self.agg_n = 1

        self._running = True

    def stop(self):
        self._running = False

    # ---------- helpery do DB ----------

    def _fetch_candles_once(self) -> Optional[pd.DataFrame]:
        """
        Pobiera z DB świeczki dla self.symbol, respektując max_plot i ewentualną
        statyczną agregację po stronie workera.

        Zakłada istnienie metody:
          - db.get_candles(symbol, limit=raw_limit)
          - normalizacja close_time/timestamp

        Zwraca DF na interwale bazowym (np. 1m).
        Jeśli włączona jest statyczna agregacja w ChartDataWorker (self.agg_n > 1
        i PLOT_DYNAMIC_AGG_ENABLED == False), pobieramy raw_limit ~= max_plot * agg_n,
        żeby nie uciąć historii przed agregacją.
        Przy dynamicznej agregacji (LOD w PlotWidget) pobieramy po prostu max_plot
        świec bazowych (ostatnie), bo resztą zajmuje się GUI.
        """
        if not hasattr(self.db, "get_candles"):
            logging.warning(
                "[ChartDataWorker] db.get_candles missing, cannot load history for %s",
                self.symbol,
            )
            return None

        # --- wyliczenie raw_limit ---
        try:
            from config import MAX_PLOT_CANDLES, PLOT_DYNAMIC_AGG_ENABLED
        except Exception:
            MAX_PLOT_CANDLES = 5000
            PLOT_DYNAMIC_AGG_ENABLED = False

        # bazowy limit: to, z czym uruchomiono workera, albo fallback do MAX_PLOT_CANDLES
        raw_limit = getattr(self, "max_plot", None)
        try:
            raw_limit = int(raw_limit) if raw_limit is not None else 0
        except Exception:
            raw_limit = 0

        if raw_limit <= 0:
            raw_limit = MAX_PLOT_CANDLES

        if not PLOT_DYNAMIC_AGG_ENABLED and self.agg_n > 1:
            raw_limit = int(raw_limit) * int(self.agg_n)

        try:
            df_raw = self.db.get_candles(self.symbol, limit=raw_limit)
        except Exception:
            logging.exception("[ChartDataWorker] db.get_candles failed for %s", self.symbol)
            return None

        if df_raw is None:
            return None
        if isinstance(df_raw, pd.DataFrame):
            if df_raw.empty:
                return None
            return df_raw

        try:
            df_raw = pd.DataFrame(df_raw)
        except Exception:
            logging.exception("[ChartDataWorker] could not convert result to DataFrame for %s", self.symbol)
            return None

        if df_raw.empty:
            return None

        return df_raw

    def _build_plot_df(self, df_raw: pd.DataFrame) -> pd.DataFrame:
        """
        Deleguje do build_plot_df z parametrami workera.

        df_raw jest na interwale bazowym (np. 1m), a wynik to df_plot:

          - z OHLC (ew. volume, wskaźniki)
          - posortowany po close_time
          - indeks = close_time (UTC)
          - kolumna 'timestamp' = epoch seconds

        Przy agg_n > 1: wiadra po agg_n świec:
          - open = open z 1. świecy w wiadrze
          - close = close z ostatniej świecy w wiadrze
          - high  = max(highów)
          - low   = min(lowów)
          - volume* = suma
          - wskaźniki numeryczne = średnia
        """
        return build_plot_df(
            df_raw=df_raw,
            agg_n=self.agg_n,
            max_plot=self.max_plot,
            symbol=self.symbol,
            log_prefix="[ChartDataWorker]",
        )

    def _resolve_fetcher(self):
        """Zwraca aktualny fetcher.

        - jeśli self.fetcher jest callable (np. lambda zwracająca LiveFetcher
          z MainWindow.live_fetchers), wywołuje ją i zwraca wynik;
        - w przeciwnym razie traktuje self.fetcher jako bezpośrednią referencję.
        """
        f = getattr(self, "fetcher", None)
        try:
            if callable(f):
                f = f()
        except Exception:
            f = None
        return f

    def _fetch_live_snapshot(self):
        """Pobierz aktualną świecę in-progress od LiveFetcher, jeśli jest podpięty."""
        fetcher = self._resolve_fetcher()
        if not fetcher or not hasattr(fetcher, "get_inprogress_candle"):
            return None
        try:
            live = fetcher.get_inprogress_candle()
            return live
        except Exception as e:
            logging.debug("[ChartDataWorker] get_inprogress_candle(%s) error: %s", self.symbol, e)
            return None

    # ---------- główna pętla wątku ----------

    def run(self):
        logging.debug("[ChartDataWorker] start for %s (agg_n=%s, max_plot=%s)", self.symbol, self.agg_n, self.max_plot)

        # 1) Jednorazowy pełny load świec z DB (na interwale bazowym)
        df_raw = self._fetch_candles_once()
        if df_raw is not None:
            df_plot = self._build_plot_df(df_raw)
            try:
                # wysyłamy osobno raw_df (bazowe świece) i plot_df (widok pod wykres)
                self.full_refresh.emit(df_raw, df_plot)
            except Exception:
                logging.exception("[ChartDataWorker] full_refresh.emit failed for %s", self.symbol)

        # 2) jeśli nie mamy fetchera (tryb statyczny/backtest), kończymy
        if not self.fetcher:
            logging.debug("[ChartDataWorker] no fetcher for %s – static mode, exiting thread", self.symbol)
            return

        # 3) tryb live – pętla z pobieraniem in-progress candle
        while self._running:
            live = self._fetch_live_snapshot()
            if live is not None:
                try:
                    self.live_only.emit(live)
                except Exception:
                    logging.exception("[ChartDataWorker] live_only.emit failed for %s", self.symbol)

            # delikatne odciążenie CPU; można kiedyś powiązać z config.refresh_time_ms
            time.sleep(0.3)

        logging.debug("[ChartDataWorker] stopped for %s", self.symbol)


def _heal_indicators(df, params=None):
    """
    NO-OP: w nowej infrastrukturze wskaźniki bierzemy z DB / backtestu
    (indicators_historical), a nie liczymy tutaj.
    Zostawione dla zgodności z ewentualnym starym kodem.
    """
    return df
