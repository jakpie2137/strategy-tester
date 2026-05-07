# LIVETEST_PANEL – Backend Documentation

_Scope: config.py, main.py, engine.py, livefetcher.py, db_pg.py, db_helpers.py, test_worker.py, context.py_

Generated helper documentation based on the current codebase. This is meant as a technical-but-readable guide for devs and quantitatively nastawionych traderów.

## 1. High‑level overview

LIVETEST_PANEL backend to zestaw modułów odpowiedzialnych za:

- **Pobieranie danych live** z giełdy (aktualnie Binance Futures) i zapisywanie ich w bazie (świece).
- **Backtestowanie strategii** na danych historycznych lub połączonych history + live (test_worker + engine).
- **Zarządzanie bazą danych** (świece, wskaźniki, trejdy, statystyki).
- **Dostarczanie danych i wyników** do GUI (wykresy, tabele, statystyki).

Moduły backendu są napisane tak, aby można je było odpalać w różnych trybach:

- Headless backtest (bez GUI, np. `headless_tester.py`).
- Backtest + GUI (wizualna inspekcja trejdów, wskaźników).
- Live data collection (`livefetcher.py`) – fundament pod przyszły paper/live trading.

### 1.1. Architektura z lotu ptaka

```text
        [ Binance / Exchange API ]
                   |
             livefetcher.py
                   |
             [ DB: candles ]
                   |
      +------------+------------+
      |                         |
  test_worker.py           live GUI
      |                   (main_window.py,
      |                    plot_widget.py, ...)
      |
   engine.py  <->  db_pg.py / db_helpers.py  <->  [ DB: indicators, trades, stats ]
      |
   context.py (state per symbol – pozycje, trejdy, buffery)
```

## 2. Główne komponenty backendu

### 2.1. `config.py` – centralna konfiguracja

Plik `config.py` trzyma **globalne parametry aplikacji**, podzielone logicznie na sekcje:

- Flagi zapisu wskaźników do DB.
- Ustawienia segmentacji/gapów (RSI segmentation).
- Tryb wstawiania wskaźników do DB (INSERT vs UPSERT).
- Parametry workerów testujących (batch size, max candles, itp.).
- Parametry agregacji wykresów i layoutu GUI (część frontendowa).

Backendowa część dokumentacji skupia się na parametrach związanych z DB, backtestem i workerami – parametry czysto wizualne zostaną szerzej opisane w dokumentacji GUI.

### 2.2. `main.py` – wejście do aplikacji z GUI

- Ładuje `.env`, odpala `QApplication` i tworzy `MainWindow`.
- Inicjalizuje połączenie z bazą (`Database` z `db_pg.py`).
- Spina backend (`MultiSymbolEngine`, strategie) z GUI:
  - Tworzy kolejki do komunikacji (np. logi, sygnały od workerów).
  - Startuje wątki testowe / live w zależności od trybu.
- Służy głównie do odpalania całej aplikacji okienkowej – w trybie headless backtest nie jest używany.

### 2.3. `engine.py` – MultiSymbolEngine i orkiestracja testów

`engine.py` dostarcza klasę **`MultiSymbolEngine`**, która:

- Zarządza testami na wielu symbolach jednocześnie.
- Utrzymuje **kontekst per symbol** (`SymbolContext` z `context.py`).
- Odpowiada za:
  - pobieranie świec z DB w paczkach,
  - przekazywanie danych do workerów (`test_worker.py`),
  - zbieranie wyników (trejdy, wskaźniki, statystyki),
  - obliczanie PnL z uwzględnieniem `FEE_RATE`.
- Jest mostem między „czystą” strategią (`backtester/strategies`) a infrastrukturą danych/DB.

### 2.4. `livefetcher.py` – pobieranie danych live

- Regularnie odpytuje API giełdy o nowe dane (np. Binance Futures).
- Na podstawie ticków / kline'ów buduje własne świece o zadanym interwale (`DEFAULT_CANDLE_INTERVAL`).
- Zapisuje świece do bazy (`live_data` / Postgres), używając spójnego formatu (symbol, open/close_time, OHLC).
- Dba o poprawne timezone’y i konwersję timestampów.
- Parametry częstotliwości odpytywania kontrolujesz przez `config.py` (np. `DEFAULT_FETCH_INTERVAL`).

### 2.5. `db_pg.py` – adapter do Postgresa

- Odpowiada za połączenie z Postgres, tworzenie tabel, insert/flush danych.
- Ujednolica dostęp do:
  - tabel świec,
  - tabeli wskaźników (`indicators_historical`),
  - tabel trejdów/statystyk.
- Kluczowe cechy:
  - „Patched adapter” z obsługą dynamicznych kolumn wskaźników (UPPERCASE, cytowane nazwy).
  - Brak normalizowania timezone’ów – timestampy są używane „tak jak przyszły” (ważne dla spójności z GUI).
- API jest utrzymane tak, aby było kompatybilne z istniejącymi workerami i GUI.

### 2.6. `db_helpers.py` – pomocnicze funkcje DB

- Zawiera utility do:
  - tworzenia/aktualizacji schematów,
  - czyszczenia tabel (np. przed nowym testem),
  - zapisu batchowego wskaźników / trejdów.
- Jest wykorzystywany zarówno przez backtester, jak i skrypty maintenance’owe.

### 2.7. `context.py` – stan per symbol

`SymbolContext` trzyma cały **stan testu dla pojedynczego symbolu**:

- Aktualną pozycję (side, entry, SL/TP/TS, itp.).
- Listę zamkniętych trejdów.
- Bufory ticków / świec potrzebne do szczegółowych statystyk.
- Liczniki (`trade_id_counter`, `last_closed_trade_time` itd.).

Kontekst jest tworzony przez `MultiSymbolEngine` i przekazywany do workerów strategii, dzięki czemu testy na wielu symbolach mogą być wykonywane równolegle, ale z pełną separacją stanu.

### 2.8. `test_worker.py` – serce backtestu

- Worker, który bierze porcję świec (dla jednego symbolu) i:
  - oblicza wskaźniki,
  - generuje sygnały **open** / **close** na podstawie aktualnej strategii (np. `rsi.py` + `rsi_config.py`),
  - prowadzi risk-engine (SL, TP, TrailingStop, SLU, slippage, różne typy close),
  - generuje rekordy trejdów i statystyk.
- Może działać w trybie:
  - „batch” (gdy engine karmi go historycznymi świecami),
  - docelowo również w trybie zbliżonym do live (świeca po świecy).
- Wyniki zwraca w formie ramek pandas / struktur, które engine dalej zapisuje do DB i przekazuje GUI.

## 3. Konfiguracja backendu (`config.py`) – referencja

Poniżej zebrane są najważniejsze sekcje `config.py`. Parametry czysto wizualne (kolory świec, rozmiary markerów) są wymienione, ale szczegółowo omówimy je w dokumentacji GUI.

### 3.2 WRITE INDICATORS TO DB FLAG

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `WRITE_INDICATORS_TO_DB` | `True` | save indicators to database during test? |
| `WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST` | `False` | True = po teście zapisz wskaźniki z RAM do DB |


### 3.3 GAP DETECTION / SEGMENTATION

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `RSI_SEGMENTATION_ENABLED` | `False` | — |
| `RSI_SEGMENTATION_DEBUG` | `False` | — |

**Podsekcja: DB / indicators insert modes**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `INDICATOR_UPSERT` | `False` | True = bezpieczna opcja, False = performance, ale zakładamy brak duplikatów |
| `INDICATORS_BACKFILL_OPEN_TIME` | `False` | jeśli chcesz max performance na testach |

**Podsekcja: intervals**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `DEFAULT_FETCH_INTERVAL` | `5` | seconds |
| `DEFAULT_CANDLE_INTERVAL` | `1` | minutes |

**Podsekcja: strategies & symbols**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `STRATEGY_CHOICES` | `{` | — |
| `AVAILABLE_PAIRS` | `[` | — |
| `DEFAULT_PAIR` | `"BTCUSDT"` | — |
| `PAIR_PRICE_PRECISION` | `{` | — |

**Podsekcja: Strategy global params**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `BIAS_CHOICES` | `["None", "Long", "Short"]` | — |
| `DEFAULT_BIAS` | `"None"` | — |
| `POSITION_SIZE` | `1000` | Docelowa wartość pozycji w USD |
| `LEVERAGE` | `10` | — |
| `FEE_RATE` | `0.00045` | Binance Futures fee rate (Regular User) = 0.05 % (0.0005), paid in BNB 10% off = 0.045 % (0.00045) |
| `TICKS_BEFORE_AFTER` | `40` | — |

**Podsekcja: STARTING BALANCE dla equity curve**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `STARTING_BALANCE` | `10000.0` | — |

**Podsekcja: - LIMITY GUI -**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `MAX_GUI_CANDLES` | `10000` | ile pobierać z bazy do GUI |
| `MAX_PLOT_CANDLES` | `10000` | ile rysować na wykresie świeczek |
| `MAX_WORKER_CANDLES` | `10000` | na ilu swieczkach puszczamy test strategii |
| `MAX_GUI_TRADES` | `15000` | ile wyświetlać trade’ów w tabeli i wykresie |
| `MAX_GUI_TICKS` | `100` | ile ticków do tabeli |
| `MAX_GUI_INDICATORS` | `15` | ile wskaźników do tabeli |

**Podsekcja: SOFT RESTART / MEMORY LIMITS**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `GUI_SOFT_RESTART_RAM_MB` | `3700` | RAM w MB – po przekroczeniu tego soft-restart GUI |
| `GUI_KILL_RAM_MB` | `4096` | RAM w MB – po przekroczeniu tego hard kill (sys.exit) |
| `GUI_MAX_SOFT_RESTARTS` | `4` | Ile razy próbować restartować zanim kill |

**Podsekcja: Charts visuals**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `CANDLE_BULL_COLOR` | `"` | 00E676"  # zielony (bullish) LIGHT |
| `CANDLE_BEAR_COLOR` | `"` | FF1744"  # czerwony (bearish) LIGHT |

**Podsekcja: TP/SL line width (px) & TrailingStop line width (px)**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `ORDER_LINE_WIDTH` | `1.0` | regular TP/SL width |
| `TS_LINE_MULTIPLIER` | `3.0` | TS width = ORDER_LINE_WIDTH * TS_LINE_MULTIPLIER |
| `CANDLE_BODY_ALPHA` | `255` | — |
| `CANDLE_WICK_ALPHA` | `255` | — |


### 3.4 Trade marker styling (GUI)

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `TRADE_ENTRY_MARKER_SIZE` | `20` | — |
| `TRADE_ENTRY_LONG_COLOR` | `"` | 00C800"   # ciemnozielony (0,200,0) |
| `TRADE_ENTRY_SHORT_COLOR` | `"` | DC0000"   # ciemnoczerwony (220,0,0) |
| `TRADE_ENTRY_BORDER_COLOR` | `"` | FFFFFF"  # biała obwódka |
| `TRADE_ENTRY_BORDER_WIDTH` | `1` | px |
| `TRADE_EXIT_MARKER_SIZE` | `20` | — |
| `TRADE_EXIT_BRUSH_COLOR` | `"` | 000000"    # czarne wypełnienie |
| `TRADE_EXIT_BORDER_COLOR` | `"` | FFFFFF"   # biała obwódka |
| `TRADE_EXIT_BORDER_WIDTH` | `1` | px |


### 3.5 AGREGACJA WYKRESÓW

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `PLOT_DYNAMIC_AGG_ENABLED` | `True` | — |
| `PLOT_AGGREGATION` | `60` | 60 świec bazowych = 1 świeca na wykresie |
| `PLOT_MAX_VISIBLE_CANDLES` | `240` | np. 800 / 1000 / 1200 – jak wolisz |
| `PLOT_TARGET_MAX_BINS` | `PLOT_MAX_VISIBLE_CANDLES` | — |
| `PLOT_TARGET_MIN_BINS` | `max(100, PLOT_MAX_VISIBLE_CANDLES // 3)` | — |
| `PLOT_PYRAMID_MINUTES` | `[1, 3, 5, 15, 60, 240, 1440, 10080]` | 1m,3m,5m,15m,1h,4h,1d,1W |
| `PLOT_LOD_DEBOUNCE_MS` | `150` | — |
| `PLOT_LOD_IMPROVEMENT_FACTOR` | `0.7` | err_best < err_cur * 0.7 → dopiero wtedy zmieniamy poziom |
| `PLOT_X_MARGIN_MIN_BARS` | `1000` | minimum świeczek marginesu po obu stronach |
| `PLOT_X_MARGIN_FRAC` | `0.10` | minimum 10% długości danych marginesu po obu stronach |

**Podsekcja: Performance / debug flags**

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `PERF_DEBUG` | `True` | ogólne pomiary czasu (np. w StrategyTestWorker) |
| `DB_DEBUG` | `False` | dodatkowe logi i pomiary czasu zapytań SQL w db_pg |
| `WORKER_RAM_DEBUG` | `False` | opcjonalne logi użycia RAM w workerach |


### 3.7 BATCH SIZE / FLUSH SETTINGS

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `INDICATOR_FLUSH_ROWS` | `10000` | — |
| `INDICATOR_BATCH_SIZE` | `10000` | — |
| `PERF_CHUNK_ROWS` | `10000` | — |
| `TRADE_BATCH_SIZE` | `500` | — |


### 3.8 GUI Layout presets

| Parametr | Domyślna wartość | Opis (z komentarza w config.py) |
|---------|-------------------|----------------------------------|
| `LAYOUT_DEFAULT` | `2` | — |
| `LAYOUT_LIMITS` | `{` | — |
| `MAINWINDOW_MIN_WIDTH` | `1250` | — |
| `MAINWINDOW_MIN_HEIGHT` | `850` | — |
| `SYNC_FEAR_GREED_ON_TEST_START` | `True` | — |


## 4. Flow danych i life‑cycle testu

### 4.1. Dane historyczne / live → świece

1. **Historyczne świece** pobierane są wcześniej przez osobne skrypty (np. fetchery historyczne) i zapisane w bazie w spójnym formacie.
2. **`livefetcher.py`** może dokleić do nich świeczki live, korzystając z tego samego formatu: `(symbol, open_time, close_time, O, H, L, C, volume)`.
3. Dzięki temu testy mogą być uruchamiane na:
   - samych danych historycznych,
   - historii z doklejonym kawałkiem live (z przerwą w danych, obsługiwaną przez strategie).

### 4.2. Pętla testowa (engine + test_worker)

W uproszczeniu:

1. `MultiSymbolEngine` z `engine.py` pobiera **batch** świec z DB dla danego symbolu (max `MAX_WORKER_CANDLES`).
2. Tworzy/aktualizuje `SymbolContext` (stan pozycji, trejdów).
3. Przekazuje dane do `test_worker.py`, który:
   - oblicza wskaźniki,
   - generuje sygnały open/close,
   - aktualizuje pozycję w kontekście (entry/exit, SL/TP/TS/SLU),
   - buduje rekordy trejdów i (opcjonalnie) wskaźników do zapisu w DB.
4. Engine zbiera zwrotkę od workerów i przy użyciu `db_pg.py` / `db_helpers.py` zapisuje:
   - wskaźniki (jeśli `WRITE_INDICATORS_TO_DB`),
   - trejdy,
   - statystyki. 
5. W trybie z GUI wyniki są również przekazywane do `main_window.py` / `plot_widget.py`, żeby można było je obejrzeć na wykresach.

### 4.3. Gdzie szukać czego w razie debugowania

- **Problemy z danymi / brak świec** → sprawdź `livefetcher.py`, konfigurację DB (`config.py`, sekcja DB), logi z `db_pg.py`.
- **Dziwne trejdy / logika wejść/wyjść** → zacznij od `test_worker.py` + `rsi.py` / `rsi_config.py`, w razie potrzeby zajrzyj do `SymbolContext` w `context.py`.
- **Wydajność / RAM** → parametry batchy (`MAX_WORKER_CANDLES`, `INDICATOR_BATCH_SIZE`, `PERF_CHUNK_ROWS`), logi perf z engine.
