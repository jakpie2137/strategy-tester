# Docs 5 – Test flow – `livetest_panel_7`

_Szczegółowy opis przepływu testów (backtest) oraz szkic tego, jak na tej bazie budować LiveTest / paper-trading._

## Spis treści

- [1. Cel dokumentu](#1.-cel-dokumentu)
- [2. Warstwy systemu testowego](#2.-warstwy-systemu-testowego)
  - [2.1. Big-picture: jak to ze sobą gada](#2.1.-big-picture:-jak-to-ze-soba-gada)
- [3. Backtest – GUI / `StrategyTestWorker`](#3.-backtest-–-gui-/-`strategytestworker`)
  - [3.1. Start testu i inicjalizacja](#3.1.-start-testu-i-inicjalizacja)
  - [3.2. Pętla per symbol](#3.2.-petla-per-symbol)
  - [3.3. Dynamiczne kolumny wskaźników (schema discovery)](#3.3.-dynamiczne-kolumny-wskaznikow-(schema-discovery))
  - [3.4. Per-candle trading loop](#3.4.-per-candle-trading-loop)
  - [3.5. Zakończenie symbolu / testu](#3.5.-zakonczenie-symbolu-/-testu)
- [4. Backtest – engine-native (`run_backtest`) i headless](#4.-backtest-–-engine-native-(`run_backtest`)-i-headless)
- [5. LiveTest / paper-trading – co już mamy, a czego brakuje](#5.-livetest-/-paper-trading-–-co-juz-mamy,-a-czego-brakuje)
  - [5.1. Warstwa danych live](#5.1.-warstwa-danych-live)
  - [5.2. Silnik live = ten sam `MultiSymbolEngine.on_tick`](#5.2.-silnik-live-=-ten-sam-`multisymbolengine.on_tick`)
  - [5.3. LiveWorker / LiveRunner – koncept](#5.3.-liveworker-/-liverunner-–-koncept)
  - [5.4. Router / Executor – jak go wpiąć bez psucia separacji](#5.4.-router-/-executor-–-jak-go-wpiac-bez-psucia-separacji)
  - [5.5. Jak porówniać backtest vs LiveTest vs live trading](#5.5.-jak-porowniac-backtest-vs-livetest-vs-live-trading)
- [6. TL;DR – jak myśleć o test flow w `livetest_panel_7`](#6.-tl;dr-–-jak-myslec-o-test-flow-w-`livetest_panel_7`)


## 1. Cel dokumentu

Ten dokument opisuje, **jak w aktualnej wersji projektu działa backtest**, krok po kroku – od pobierania świec z bazy, przez wyliczanie wskaźników, aż po generowanie i zapisywanie trejdów. 

Na końcu pokazuję też **jak ta sama architektura może zostać użyta do LiveTest / paper-tradingu** – co już jest gotowe, czego brakuje i jak spinać w głowie backtest vs live (żeby móc porówniać wyniki i realny slippage).

## 2. Warstwy systemu testowego

W test-flow biorą udział cztery główne warstwy:

1. **Baza danych (Postgres)** – moduł `db_pg.py`
   - tabele świec (`public.candles`),
   - tabele wskaźników (`public.indicators_historical`),
   - tabele trejdów (`public.trades` / `public.trades_ticks` + meta / stats),
   - helpery do zapisu/odczytu (`get_candles`, `insert_trade_rows`, `replace_stats_rows`, itd.).
2. **Strategia (`RSIStrategy`)** – `rsi.py` + `rsi_config.py`
   - liczenie wskaźników (`compute_indicators` / `compute_indicators_segmented`),
   - generowanie sygnałów OPEN (`open_position_signal`),
   - generowanie sygnałów CLOSE (`close_position_signal` + `CLOSE_SIGNALS`),
   - risk-parametry (`get_risk_params` – tryb FIXED/ATR).
3. **Silnik wielosymbolowy** – `MultiSymbolEngine` w `engine.py`
   - trzyma **kontekst per symbol** (`SymbolContext`),
   - implementuje uniwersalny **`on_tick()`** (identyczny dla live/backtest),
   - ma helpery do `open_position` / `close_position` + zapisuje trejdy,
   - potrafi samodzielnie wykonać backtest przez `run_backtest()` (używane w trybach headless).
4. **Worker testowy (backtest GUI)** – `StrategyTestWorker` w `test_worker.py`
   - QThread odpalany z GUI,
   - **czyta świece z DB**, liczy wskaźniki przez strategię,
   - robi całą logikę **TP/SL/TS/SLU, slippage, cooldown trailing stopów**,
   - zapisuje wskaźniki i trejdy do DB przez kolejkę `db_queue`.

W trybie **GUI backtest** to właśnie `StrategyTestWorker` jest „driverem” testu, a `MultiSymbolEngine` pełni głównie rolę wspólnej warstwy dla strategii + struktury trejdów (tak, żeby GUI i narzędzia stats miały jeden format).

### 2.1. Big-picture: jak to ze sobą gada

Prosty schemat przepływu (backtest, GUI):

```text
[config.py + rsi_config.py]
           │
           ▼
[GUI] main_window  ──► StrategyTestWorker (QThread)
                           │
                           ▼
                   MultiSymbolEngine(strategy, db, db_queue)
                           │
                 (per symbol: get_candles)
                           │
                           ▼
               RSIStrategy.compute_indicators(...)
                           │
                           ▼
                ≪per-candle trading loop≫
                           │
                  ├─► DB queue: insert_indicator_rows
                  └─► DB queue: insert_trade_rows
```

W headless backteście (np. skrypt `headless_tester.py`) układ jest podobny, tylko rolę `StrategyTestWorker` może przejąć prosty runner wywołujący `MultiSymbolEngine.run_backtest()` – wtedy dokładniej korzystasz z `on_tick()`.

## 3. Backtest – GUI / `StrategyTestWorker`

Ta sekcja opisuje to, co dzieje się w `StrategyTestWorker.run()` – to jest **główne serce backtestu** w Twoim środowisku.

### 3.1. Start testu i inicjalizacja

Kiedy w GUI klikasz „Start test”:

1. `main_window` tworzy instancję `StrategyTestWorker` z:
   - `db` (adapter Postgresa z `db_pg.py`),
   - `db_queue` (kolejka do asynchronicznego zapisu),
   - listą symboli,
   - referencją do klasy strategii (`RSIStrategy` lub innej),
   - opcjonalnym biasem (`bias="long"/"short"`).
2. Worker w `run()`:
   - rozwiązuje `test_id` (korzystając z `db.next_free_test_id()` jeśli jest),
   - instancjonuje strategię:
     ```python
     strategy = strategy_cls()  # np. RSIStrategy
     engine = MultiSymbolEngine(strategy, self.db, self.db_queue, self.symbols, bias=self.bias)
     ```
   - zapamiętuje `self.engine = engine` (żeby GUI miało dostęp do trejdów po zakończeniu),
   - zapisuje **snapshot konfiguracji strategii** (`rsi_config.py`) do Postgresa (`save_run_config` albo własne `test_config_meta`).
3. Worker próbuje **wyczyścić tabele wskaźników** (`indicators_historical`) powiązane z tym runem, żeby min/max i zakresy były świeże dla danego testu.
4. Worker robi jednorazowy **globalny refresh Fear&Greed**:
   - zbiera zakres dat z wszystkich symboli (`get_candles` → `_normalize_candles_df`),
   - pobiera historię F&G (`tools.altme_fng.fetch_history`),
   - zapisuje ją do `public.fear_greed` (prosta tabela `day, value`).

### 3.2. Pętla per symbol

Dalej w `run()` mamy główną pętlę:

```python
for symbol in self.symbols:
    raw = self.db.get_candles(symbol, limit=MAX_WORKER_CANDLES)
    df  = _as_df(raw)
    ...
```

Dla każdego symbolu:

1. **Pobranie świec** – przez `db_pg.get_candles(...)`:
   - zwraca DataFrame z kolumnami `open, high, low, close, volume_quote, open_time, close_time, inserted_at, ...`,
   - dba o to, żeby `close_time` był poprawnym `datetime`, posortowany rosnąco.
2. **Normalizacja czasu**:
   - `_normalize_candles_df(df, symbol)` – helper z `test_worker.py`,
   - ujednolica nazwy kolumn (np. `CLOSE_TIME` vs `close_time`),
   - ustawia indeks na `close_time` (TimeSeries).
3. **Liczenie wskaźników** – "one source of truth":

   ```python
   if hasattr(strategy, "compute_indicators_segmented"):
       df = strategy.compute_indicators_segmented(df)
   else:
       df = strategy.compute_indicators(df)
   ```

   - To jest **jedyna droga liczenia wskaźników** – worker nie liczy małych kawałków na własną rękę.
   - `RSIStrategy` wewnątrz:
     - robi buckety (jeśli trzeba),
     - liczy RSI, MA, MACD, BB, ATR, STC/RSI itd. wg `INDICATOR_OVERRIDES`,
     - zwraca DF z tymi kolumnami **plus** oryginalne OHLC.
4. **Merge Fear&Greed** (per symbol):
   - worker pobiera serię dzienną z `public.fear_greed` (cache na workerze),
   - mapuje ją po znormalizowanej dacie `DATE(close_time)` → `FEAR_GREED`,
   - dokleja kolumnę `FEAR_GREED` do DF (strategia może korzystać z tego jak z normalnego wskaźnika).
5. **Kolumny risk-limitów**:
   - worker upewnia się, że DF ma kolumny `TP`, `SL`, `TS`, `TS_BENCHMARK` (jeśli strategy ich nie stworzyła, inicjuje NaN),
   - to pozwala spójnie zapisać trailing stopy / TP/SL do `indicators_historical`.
6. **Cache wskaźników w RAM**:
   - `self.indicators_by_symbol[symbol] = df`,
   - GUI może później to wykorzystać do detalicznych wykresów / debugów nawet wtedy, gdy **nie zapisujemy wskaźników do DB** (`WRITE_INDICATORS_TO_DB=False`).

### 3.3. Dynamiczne kolumny wskaźników (schema discovery)

Jeśli w configu masz `WRITE_INDICATORS_TO_DB=True`, worker:

1. Trzyma zbiór `dynamic_cols` – zestaw **nazw kolumn wskaźników**, które już odkrył.
2. Na każdej świecy wywołuje `strategy.extract_indicator_values(row)` (jeśli strategia to implementuje):
   - zwraca słownik `{"RSI": 54.3, "MACD_HIST": 0.0021, ...}`.
3. `_numeric_keys_from(vals)` wybiera z tego te klucze, które są liczbami i nadają się do tabeli wskaźników.
4. Gdy pojawią się nowe klucze – `dynamic_cols` się rozszerza → worker woła:

   ```python
   ordered_cols = order_columns(dynamic_cols)
   engine.db.create_indicators_table(INDICATORS_TABLE, ordered_cols, table_type="historical")
   ```

5. W trakcie pętli po świecach worker zbiera `indicator_rows` i co `INDICATOR_FLUSH_ROWS` wrzuca je do `db_queue` jako:

   ```python
   self.db_queue.put({
       "type": "insert_indicator_rows",
       "table_name": INDICATORS_TABLE,
       "rows": indicator_rows,
       "indicator_names": ordered_cols,
   })
   ```

Wątek DB-consumera (poza zakresem tego dokumentu) faktycznie robi `INSERT` do Postgresa. Dzięki temu testy nie blokują się na I/O bazy.

### 3.4. Per-candle trading loop

Najważniejsza część flow – pętla po wszystkich świecach danego symbolu:

```python
position = None
trades, trade_rows, indicator_rows = [], [], []
...
for i, (idx, row) in enumerate(df.iterrows()):
    row_dict = row.to_dict()
    vals = eiv(row) if has_eiv else row_dict
    price = float(row_dict.get("close") or row_dict.get("CLOSE") or row_dict.get("close_price"))
    atr_here = _safe_float(vals.get("ATR")) or _safe_float(vals.get("atr")) or 0.0
    ...
```

W środku tej pętli worker wykonuje pełen **lifecykel pozycji**:

#### 3.4.1. Trailing stop – arming i przesuwanie

1. **Uzbrajanie TS** – jeśli `TRAILING_STOP_ENABLED` i pozycja istnieje, ale `ts_armed=False`:
   - worker sprawdza, czy cena dotknęła TP:
     - LONG: `price >= tp_level`,
     - SHORT: `price <= tp_level`.
   - jeśli tak, to:
     - ustawia `position["ts_armed"] = True`,
     - zapamiętuje bar `ts_armed_at = idx`,
     - ustawia cooldown barowy `ts_last_update_idx = idx`,
     - **czyści** `tp_level` i `sl_level` (od tego momentu gra już tylko TS),
     - liczy `ts_benchmark = price` i bazowy `trailing_stop = _ts_from_benchmark(...)` z uwzględnieniem ATR / RISK_MODE,
     - zapisuje `initial_benchmark` / `initial_ts` (do późniejszej analizy w statystykach).

2. **Przesuwanie benchmarku TS** – jeśli `ts_armed=True`:
   - LONG: jeśli cena robi nowe high (`price > ts_benchmark`), benchmark idzie w górę,
   - SHORT: jeśli cena robi nowe low (`price < ts_benchmark`), benchmark idzie w dół,
   - worker liczy nowy poziom TS przez `_ts_from_benchmark(...)` i nadpisuje `position["trailing_stop"]`.

3. **Cooldown 1 świeca** – w trybie `on_crossover` (Twoja poprawka):
   - po uzbrojeniu TS oraz po każdym podniesieniu/opuszczeniu benchmarku TS, worker zapamiętuje `ts_last_update_idx`,
   - dopóki `current_idx <= ts_last_update_idx` – **ignoruje** naruszenia TS (żeby TS nie zabił pozycji na tej samej świecy, która go właśnie ustawiła / podniosła).

#### 3.4.2. SL_UPDATER – static jump + dynamic SL

Worker implementuje oba mechanizmy z `SL_UPDATER` (opisane szerzej w Docs 4):

1. **Static jump (BE / pół-BE / custom)** – jeśli:
   - `SL_UPDATER["enabled"]` i `static_jump_enabled=True`,
   - SL nie był jeszcze „przeskoczony” (`sl_jump_triggered=False`),
   - cena dotknęła `trigger_move_SL` (np. 30% drogi entry→TP).

   Wtedy worker:
   - liczy `range_pts = tp_ref - entry_ref` (dla longa; odwrotnie dla shorta),
   - wyznacza `trigger_price` i `move_to` w cenach,
   - gdy high/low przebiją `trigger_price`, ustawia:
     - LONG: `sl_level = max(sl_initial, current_sl, move_to)` – nigdy niżej niż oryginalny SL / aktualny SL,
     - SHORT: `sl_level = min(sl_initial, current_sl, move_to)` – nigdy wyżej niż oryginalny SL / aktualny SL,
   - zapamiętuje `sl_floor` i `sl_jump_triggered=True`.

2. **Dynamic SL** – jeśli `dynamic_SL=True`:

   - LONG:
     - worker śledzi `sl_dyn_extreme` = najwyższe high od wejścia,
     - liczy kandydat `sl_candidate = sl_initial + (sl_dyn_extreme - entry_ref)`,
     - **podnosi** SL do `sl_candidate`, ale:
       - nie niżej niż `sl_floor`/`sl_initial`,
       - nie niżej niż aktualny SL (monotoniczne polepszanie).
   - SHORT analogicznie ze spadającym low (`sl_dyn_extreme = min_low`).

Ten mechanizm działa **równolegle** do static jumpa – static jump może np. przeskoczyć SL na BE, a dynamic SL potem już tylko poprawia go dalej w korzystnym kierunku.

#### 3.4.3. Kandydaci CLOSE – TP/SL/TS + strategy close signals

Na każdej świecy worker buduje listę `candidates` – potencjalnych zamknięć pozycji:

- **czasowe** (`CLOSE_AFTER_X_CANDLES`):
  - jeśli liczba barów od wejścia przekroczyła limit – dodawany jest kandydat `reason="time_close"`.
- **trailing stop**:
  - jeśli `ts_armed=True` i `trailing_stop` jest ustawiony,
  - `on_crossover`: sprawdza, czy TS znajduje się między `low` a `high` świecy,
  - `on_candle_close`: porównuje TS do `close` świecy,
  - dodaje `reason="trailing_stop_long/short"` z odpowiednim `base_price`.
- **hard SL**:
  - analogiczna logika jak TS, z poziomem `sl_level`,
  - `on_crossover` – intra-bar, `on_candle_close` – na close.
- **hard TP** (tylko gdy trailing stop jest wyłączony):
  - `tp_level` traktowane jako zwykły poziom take-profit,
  - w trybie TS włączonym TP pełni jedynie rolę „progu uzbrojenia TS”.
- **CLOSE_SIGNALS z RSIStrategy**:
  - worker woła `strategy.close_position_signal(row, position)`,
  - jeśli strategia zwróci np. `{"signal_type": "BB_close"}`,
  - worker przelicza to na odpowiedni `base_price` (w zależności od `CLOSE_EXECUTION_TYPE` i typu sygnału),
  - dopisuje kandydata do listy.

Do dodawania kandydata służy helper:

```python
def _add_close_candidate(reason: str, base_price):
    bp = _safe_float(base_price)
    if bp is None or not np.isfinite(bp):
        return
    candidates.append({"reason": str(reason), "side": side, "base_price": float(bp)})
```

#### 3.4.4. Wybór kandydata + globalny slippage CLOSE

Po zebraniu wszystkich kandydujących zamknięć:

```python
if candidates:
    for cand in candidates:
        cand["exec_price"] = _apply_slippage(cand["base_price"], cand["side"])

    if side == "long":
        chosen = min(candidates, key=lambda x: x["exec_price"])
    elif side == "short":
        chosen = max(candidates, key=lambda x: x["exec_price"])

    exec_price = chosen["exec_price"]
    reason = chosen["reason"]
```

- **slippage** jest aplikowany **globalnie** do wszystkich kandydatów przez `_apply_slippage` – dodatnia wartość zawsze pogarsza cenę z perspektywy strategii,
- dla LONG wybierany jest kandydat z **najniższą ceną exec** (najgorszy scenariusz),
- dla SHORT – kandydat z **najwyższą ceną exec**.

Następnie worker buduje sztuczny „candle”:

```python
fake = dict(row_dict)
fake["close_time"] = idx   # zawsze close_time świecy
fake["close"] = exec_price
tr = engine.close_position(engine.contexts[symbol], fake, reason)
```

`MultiSymbolEngine.close_position(...)` liczy fee, PnL, przypina `tp_open`, `sl_open`, `initial_benchmark`, `initial_ts` itd. Worker:

- mapuje timestampy do pól oczekiwanych przez `db_pg.insert_trade_rows` (`open_time`, `close_time`),
- dodaje `test_id`, `symbol`,
- dopisuje trejd do listy `trade_rows` oraz do `engine.trades[symbol]`,
- ustawia `position = None` i czyści `context.current_position`.

#### 3.4.5. Otwarcie pozycji (OPEN)

Jeśli `position is None`, worker próbuje wygenerować sygnał wejścia:

```python
open_sig = strategy.open_position_signal(row, None)
open_sig = _apply_bias_to_open_signal(open_sig, self.bias)
```

- `open_sig` jest słownikiem z informacją o stronie (`side`/`direction`, `signal_type` itd.),
- bias (`long`/`short`) może go wyzerować, jeśli nie zgadza się ze stroną (helper `_apply_bias_to_open_signal`).

Jeśli po tym kroku `open_sig` nadal istnieje i ma stronę `"long"` lub `"short"`:

1. Worker ustawia `entry_ref = price` (surowy close świecy).
2. `tp, sl, _ = _compute_open_risk(side, entry_ref, atr_here)` – helper korzystający z `RISK_MODE` i parametrów ATR/FIXED.
3. Worker sprawdza minimalne progi TP/SL (`min_tp_threshold`, `min_sl_threshold`). Jeśli odległość do TP/SL jest zbyt mała – **sygnał wejścia jest odrzucany**.
4. Następnie nakładany jest **ENTRY slippage** – osobny parametr od close_slippage:
   - `entry_price_exec = _apply_entry_slippage(entry_ref, side)`,
   - TP/SL są liczone z `entry_ref` (idealny close), więc slippage działa na naszą niekorzyść (oddala TP, przybliża SL).
5. Tworzona jest struktura pozycji:
   - `side`,
   - `entry_price` (po slippage), `entry_timestamp = idx`,
   - `tp_level`, `sl_level` (wyliczone z `entry_ref`),
   - pola pod trailing (`ts_armed=False`, `trailing_stop=None`, `ts_benchmark=None`),
   - pola pod SLU (`sl_initial`, `sl_jump_triggered`, `sl_dyn_enabled`, `sl_dyn_extreme`),
   - `signal_type`, `trade_id` (z globalnego licznika engine).

Po tym kroku `position` w workerze i `context.current_position` w `MultiSymbolEngine` wskazują na tę samą pozycję (słownik).

### 3.5. Zakończenie symbolu / testu

Po przejściu całej serii świec:

1. Jeśli nadal jest otwarta pozycja – worker **force-close**’uje ją na ostatniej świecy (`reason="end_of_data"`).
2. Jeśli są trejdy w `trade_rows` – wrzuca je do `db_queue` jako:
   ```python
   self.db_queue.put({"type": "insert_trade_rows", "rows": trade_rows})
   ```
3. Jeśli `WRITE_INDICATORS_TO_DB` i zostały jeszcze `indicator_rows` – wysyła ostatni flush `insert_indicator_rows`.
4. Ustawia `engine.trades[symbol] = trades` i `engine.contexts[symbol].trades = trades`.

Po przejściu wszystkich symboli:

1. Worker aktualizuje metadane testu w Postgresie (`upsert_test_config_metadata` + status `finished`).
2. Wylicza per-symbol statystyki (łączny PnL, liczba trejdów itd.) i przekazuje je przez GUI do `db.replace_stats_rows`.
3. Emituje sygnały Qt `finished_with_engine(engine)` oraz legacy `finished_signal(engine)`.

## 4. Backtest – engine-native (`run_backtest`) i headless

Poza workerem GUI istnieje jeszcze drugi sposób odpalania testu – **bez GUI**, na czysto przez `MultiSymbolEngine`:

- `MultiSymbolEngine.on_tick(symbol, candles, tick=None)` – jeden, wspólny punkt wejścia dla live/backtest,
- `MultiSymbolEngine.run_backtest(progress_cb, log_cb)` – metoda, która:
  - pobiera świece przez `db.get_candles(sym, as_df=True, limit=MAX_WORKER_CANDLES)`,
  - sortuje po `close_time`,
  - buduje okno `window` (listę candle’ów) i dla każdej świecy wywołuje `self.on_tick(sym, window, tick=None)`,
  - raportuje progres przez callbacki.

W trybach **headless** (np. `headless_tester.py`):

- typowy pattern to:
  - stworzyć `PostgresDB` + `MultiSymbolEngine`,
  - załadować `RSIStrategy`,
  - odpalić `engine.run_backtest(...)`,
  - na końcu z `engine.get_all_trades()` wziąć trejdy i policzyć własne statystyki / metryki.

**Kluczowe**: zarówno worker GUI, jak i `run_backtest()` używają **tej samej strategii i tych samych risk-parametrów**. Różnica jest tylko w „driverze” (kto robi pętlę po świecach) i w tym, że worker ma dodatkowe bajery (TS cooldown, SLU, debug logi, integrację z GUI).

## 5. LiveTest / paper-trading – co już mamy, a czego brakuje

Aktualnie środowisko ma w pełni działający **backtest**. LiveTest / paper-trading jeszcze nie istnieje, ale większość klocków jest już na miejscu. Poniżej opis, **jak to może wyglądać** przy wykorzystaniu istniejącej architektury.

### 5.1. Warstwa danych live

Masz już moduł **`livefetcher.py`**, który:

- pobiera ticki / trades z Binance Futures,
- składa z nich swoje świece (`open, high, low, close, volume, ...`),
- zapisuje je do bazy (`live_data.db` lub Postgres) w formacie kompatybilnym z resztą systemu.

To oznacza, że LiveTest może opierać się na **tym samym schema świec**, co backtest:

- `symbol`, `open_time`, `close_time`, `open`, `high`, `low`, `close`, `volume`, ...
- w LiveTeście różnicą jest tylko to, że świece dopływają w czasie rzeczywistym zamiast być załadowane hurtem.

### 5.2. Silnik live = ten sam `MultiSymbolEngine.on_tick`

Projektujesz silnik w taki sposób, że **live i backtest używają tej samej funkcji**:

- w backtestowym `run_backtest()` – `on_tick()` jest wołane w pętli po świecach historycznych,
- w LiveTeście/systemie paper-tradingowym – **dokładnie ten sam `on_tick()`** może być wołany, kiedy pojawia się nowa świeca z `livefetcher`/DB.

Dlatego przyszły LiveTest może wyglądać tak:

```text
[livefetcher]  ──►  [Postgres public.candles]
                        │  (nowe świeczki)
                        ▼
               [LiveWorker / LiveRunner]
                        │
              calls engine.on_tick(symbol, window, tick=None)
                        │
                        ▼
              RSIStrategy + risk logic (TP/SL/TS/SLU)
                        │
       ├───────────► zapis do DB (paper trades)
       └───────────► (opcjonalnie) Router/Executor → giełda
```

**Kluczowy plus**: identyczna logika strategii i risk-engine’u w backtest i live. Różni się tylko źródło danych (history vs stream).

### 5.3. LiveWorker / LiveRunner – koncept

Możliwy szkic implementacyjny LiveTestu na bazie tego, co już masz:

1. **LiveWorker (QThread / async)** – analog `StrategyTestWorker`, ale dla live:
   - zamiast `for symbol in self.symbols: get_candles(..., limit=MAX_WORKER_CANDLES)` ma pętlę „nasłuchującą” na nowe świece:
     - np. co X sekund pyta DB o świeże dane (`get_candles` od ostatniego `close_time`),
     - albo dostaje callbacki bezpośrednio z `livefetcher`.
   - dla każdego symbolu trzyma rolling window świec (ostatnie N dla wyliczania wskaźników).
   - przy każdym nowym `close` świecy woła:
     ```python
     engine.on_tick(symbol, window, tick=None)
     ```
   - korzysta z **tego samego RSIStrategy** i tej samej konfiguracji (`rsi_config.py`).
2. **Paper-trading**:
   - LiveWorker nie musi od razu wysyłać orderów na giełdę,
   - może jedynie wywoływać `open_position` / `close_position` w `MultiSymbolEngine` i wrzucać trejdy do `db_queue` (tak jak teraz robi worker backtestowy),
   - w ten sposób powstaje **„live backtest” na aktualnym strumieniu danych**, gdzie sygnały i PnL liczone są tak samo jak w historii, ale na realnych, bieżących świecach.

3. **Live trading (prawdziwe zlecenia)** – kolejny krok:
   - w momencie otwarcia/ zamknięcia pozycji przez engine, LiveWorker mógłby:
     - oprócz zapisu do DB,
     - wystawić event do **Routera/Executora** (zewnętrzny komponent).

### 5.4. Router / Executor – jak go wpiąć bez psucia separacji

Z punktu widzenia architektury:

- **Livetest_panel** powinien być przede wszystkim „**signal-makerem**”:
  - generuje sygnały OPEN/CLOSE,
  - liczy PnL, TP/SL/TS, statystyki,
  - zapisuje wszystko do DB dla analizy,
  - nie musi znać szczegółów API giełdy.
- **Executor** to osobna usługa / proces:
  - subskrybuje eventy: `{symbol, side, entry_price, allowed_slippage, max_delay, reason, risk_snapshot...}`,
  - zamienia je na realne ordery (lub mocki w paper-tradingu),
  - zajmuje się rate-limitami, reconnectami, retry close itd.,
  - może mieć dodatkowe globalne bezpieczniki:
    - „dzisiaj PnL < –2.5% → stop trading”,
    - limit trejdów dziennie,
    - max open risk na portfolio, itd.

W praktyce LiveWorker mógłby np. przy `open_position` zrobić:

```python
if live_mode and executor is not None:
    executor.publish_signal({
        "symbol": symbol,
        "side": side,
        "signal_type": open_sig.get("signal_type"),
        "signal_price": entry_ref,
        "exec_price_model": entry_price_exec,
        "tp_level": tp,
        "sl_level": sl,
        ...
    })
```

Z kolei Executor zapisuje w DB **rzeczywiste** fill-price’y i wolumeny, dzięki czemu później możesz porównać:

- „idealne” trejdy z LiveTestu (według modelu `on_crossover` + slippage),
- z rzeczywistymi fillami z giełdy (opóźnienia, spread, partial fill, itp.).

### 5.5. Jak porówniać backtest vs LiveTest vs live trading

Mając spójny model: **ta sama strategia, te same risk-parametry, ten sam silnik** – możesz dość łatwo robić porównania:

1. **Backtest (historyczny)** – na danych z bazy, z modelem slippage’u (`entry_slippage`, `CLOSE_EXECUTION_SLIPPAGE`).
2. **LiveTest / paper** – na żywych świecach z `livefetcher`, ale dalej w tym samym modelu egzekucji.
3. **Real trading** – Executor zapisuje realne zlecenia/filly; można je joinować po (`symbol`, okno czasowe, typ sygnału).

Dzięki temu możesz policzyć np.:

- średnią różnicę `entry_model_price` vs `entry_real_price`,
- średnią różnicę `exit_model_price` vs `exit_real_price` per typ wyjścia (TP, SL, TS, BB_close, RSI_close…),
- w jakich **volatility regimes** (np. według ATR / PCT_CHANGE) slippage rośnie,
- jak bardzo realna strategia „odkleja się” od backtestu w PnL i w jakich warunkach.

## 6. TL;DR – jak myśleć o test flow w `livetest_panel_7`

1. **Backtest (GUI)** – `StrategyTestWorker` + `MultiSymbolEngine` + `RSIStrategy`:
   - pełny lifecykel pozycji, TP/SL/TS, SLU, cooldowny,
   - zapis wskaźników i trejdów do DB,
   - wizualizacja w GUI, statystyki, logi.
2. **Backtest (headless)** – `MultiSymbolEngine.run_backtest`:
   - te same logiki strategii/risk,
   - wygodne do masowych testów / grid-search’y bez GUI.
3. **LiveTest / paper (przyszłość)**:
   - źródło świec: `livefetcher` → `public.candles`,
   - driver: `LiveWorker` wołający `engine.on_tick`,
   - zapis trejdów do DB dokładnie tak jak w backtest, ale na bieżących danych.
4. **Live trading (przyszłość)**:
   - ten sam LiveWorker,
   - dodatkowy Router/Executor, który bierze sygnały z engine’a i zamienia je na realne zlecenia,
   - pełny rozdział: signal-maker (Livetest_panel) vs wykonanie (Executor).

Tym sposobem środowisko, które dziś służy do **szalonych, głęboko parametryzowanych backtestów**, jest już praktycznie gotowe, żeby zostać rdzeniem LiveTestu / paper-tradingu – bez przepisywania logiki strategii od zera.
