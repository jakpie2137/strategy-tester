# Docs 6 – System overview – `livetest_panel_7`

_Wysokopoziomowy opis architektury systemu, głównych modułów i typowych sposobów użycia._

## Spis treści

- [1. Misja i zakres systemu](#1.-misja-i-zakres-systemu)
- [2. High-level architektura](#2.-high-level-architektura)
  - [2.1. Widok ogólny](#2.1.-widok-ogolny)
  - [2.2. Warstwy logiczne](#2.2.-warstwy-logiczne)
- [3. Główne komponenty – „kto za co odpowiada”](#3.-glowne-komponenty-–-„kto-za-co-odpowiada”)
  - [3.1. Baza danych i dostęp do danych (`data/*`, `db_pg.py`)](#3.1.-baza-danych-i-dostep-do-danych-(`data/*`,-`db_pg.py`))
  - [3.2. Pobieranie danych live (`livefetcher.py`, skrypty FNG)](#3.2.-pobieranie-danych-live-(`livefetcher.py`,-skrypty-fng))
  - [3.3. Strategia – `RSIStrategy` i rodzina strategii (`backtester/strategies/*`)](#3.3.-strategia-–-`rsistrategy`-i-rodzina-strategii-(`backtester/strategies/*`))
  - [3.4. Silnik multi-symboli (`engine.py`, `context.py`)](#3.4.-silnik-multi-symboli-(`engine.py`,-`context.py`))
  - [3.5. Worker testowy (`test_worker.py`)](#3.5.-worker-testowy-(`test_worker.py`))
  - [3.6. GUI (`gui/*`)](#3.6.-gui-(`gui/*`))
- [4. Konfiguracja i parametryzacja](#4.-konfiguracja-i-parametryzacja)
  - [4.1. Konfiguracja aplikacji (`config.py`)](#4.1.-konfiguracja-aplikacji-(`config.py`))
  - [4.2. Konfiguracja strategii (`rsi_config.py` + dalsze strategie)](#4.2.-konfiguracja-strategii-(`rsi_config.py`-+-dalsze-strategie))
- [5. Typowe scenariusze użycia](#5.-typowe-scenariusze-uzycia)
  - [5.1. Szybki backtest wizualny w GUI](#5.1.-szybki-backtest-wizualny-w-gui)
  - [5.2. Headless backtest (rapid tester)](#5.2.-headless-backtest-(rapid-tester))
  - [5.3. Analiza konkretnych trejdów](#5.3.-analiza-konkretnych-trejdow)
  - [5.4. (Przyszłość) LiveTest / paper-trading](#5.4.-(przyszlosc)-livetest-/-paper-trading)
  - [5.5. (Przyszłość) Live trading z Executorem](#5.5.-(przyszlosc)-live-trading-z-executorem)
- [6. Rozszerzalność i roadmapa](#6.-rozszerzalnosc-i-roadmapa)
  - [6.1. Dodawanie nowych strategii / wskaźników](#6.1.-dodawanie-nowych-strategii-/-wskaznikow)
  - [6.2. Planowane / możliwe rozszerzenia (ideowe)](#6.2.-planowane-/-mozliwe-rozszerzenia-(ideowe))


## 1. Misja i zakres systemu

`livetest_panel_7` to **terminal do budowania, testowania i analizy strategii tradingowych** opartych o techniczną analizę świec.

System łączy kilka ról naraz:

- **data hub** – pobiera i przechowuje świece (historyczne i live),
- **silnik strategii i risk-engine** – liczy wskaźniki, generuje sygnały OPEN/CLOSE, liczy TP/SL/TS/SLU, PnL, statystyki,
- **środowisko badawcze** – pozwala na backtesty w GUI oraz szybkie headless testy (bez GUI) przy użyciu tego samego workera,
- w przyszłości: **signal-maker dla LiveTest / paper-tradingu / live tradingu**.

W tym dokumencie patrzymy na system „z lotu ptaka” – bez wchodzenia w każdy parametr configu (od tego są Docs 1–5), za to skupiamy się na:

- głównych **klockach architektury**,
- tym **jak do siebie pasują**,
- oraz na **konkretnych scenariuszach użycia** (backtest teraz / LiveTest w przyszłości).

## 2. High-level architektura

### 2.1. Widok ogólny

Z bardzo wysokiego poziomu system wygląda tak:

```text
          [ Binance Futures / inne źródła ]
                          │
                          ▼
                    livefetcher.py
                          │
                 (świece + ticki live)
                          │
                          ▼
                 [Baza danych (Postgres)]
                  ├───────────────┬─────────────────────┐
                  │               │                     │
                  ▼               ▼                     ▼
            candles / OHLC   indicators_historical   trades / stats


                        [Core engine]
            RSIStrategy (rsi.py + rsi_config.py)
			+ test_worker.py (StrategyTestWorker)
			(for both headless- and GUI-tests)
                + MultiSymbolEngine (engine.py)

                  ▲                          
                  │                          
        [StrategyTestWorker]   <------   [scripts/headless_tester.py]
           (GUI backtest)            (headless, bez GUI; ten sam worker)
                  ▲
                  │
                [GUI]
        main_window / plot_widget / ...
```

Główna idea: **jedna warstwa strategii (`RSIStrategy`) i jeden silnik (`MultiSymbolEngine`)** są używane przez różne „drivery”:

- GUI backtest (`StrategyTestWorker` uruchamiany z `main_window`),
- headless backtest (`scripts/headless_tester.py` tworzy `StrategyTestWorker` bez GUI – szybki tester),
- w przyszłości: LiveTest / paper-trading (LiveWorker wołający `engine.on_tick()` na danych z `livefetcher`).

### 2.2. Warstwy logiczne

Można wyróżnić cztery warstwy:

1. **Warstwa danych**
   - `data/db_pg.py` – adapter Postgresa (candles, indicators, trades, stats, meta),
   - `data/config_pg.py` – konfiguracja połączenia i schem bazy,
   - `data/live_data.db` – baza SQLite używana w niektórych trybach/lokalnie,
   - `livefetcher.py` – pobór i składanie świec live,
   - `scripts/*fng*` + `tools/fng_*` – integracja Fear&Greed.
2. **Warstwa strategii**
   - `backtester/strategies/rsi.py` – implementacja `RSIStrategy`,
   - `backtester/strategies/rsi_config.py` – konfiguracja: wskaźniki, sygnały, risk, CLOSE_SIGNALS itd.,
   - w przyszłości: kolejne strategie w `backtester/strategies/*.py`.
3. **Warstwa silnika testów**
   - `backtester/engine.py` – `MultiSymbolEngine`, `SymbolContext`, `on_tick`, `open_position`, `close_position`, (opcjonalnie) `run_backtest`,
   - `backtester/context.py` – per-symbol stan (open position, id trejdów, equity curve, stats),
   - `test_worker.py` – worker do backtestu w GUI **i headless** (pętla po świecach, TP/SL/TS/SLU, slippage, flush do DB).
4. **Warstwa prezentacji / interfejsu**
   - `gui/main_window.py` – główne okno aplikacji,
   - `gui/plot_widget.py`, `candlestick_item.py`, `chart_worker.py` – wykresy, świece, subchart’y,
   - `gui/performance_widget.py`, `global_stats_widget.py` – statystyki, equity, metryki,
   - `gui/trades_table.py`, `ticks_table.py`, `indicators_table.py` – tabele z danymi,
   - `gui/controls.py` – przyciski, przełączniki, filtry, wybór strategii/symboli/testów.

## 3. Główne komponenty – „kto za co odpowiada”

### 3.1. Baza danych i dostęp do danych (`data/*`, `db_pg.py`)

**Rola:** trzymać wszystkie „twarde fakty”: świece, wskaźniki, trejdy, statystyki, konfiguracje runów, dane FNG.

Kluczowe elementy:

- `data/db_pg.py` – klasa `PostgresDB` (lub podobna) z metodami typu:
  - `get_candles(symbol, start=None, end=None, limit=None, as_df=False)` – wczytuje świece,
  - `insert_trade_rows(rows)` – zapisuje trejdy z backtestów,
  - `create_indicators_table(name, columns, table_type="historical")` – przygotowuje tabelę wskaźników,
  - `insert_indicator_rows(table_name, rows, indicator_names)` – zapisuje wskaźniki,
  - `replace_stats_rows(rows)` – nadpisuje podsumowania statystyk (per symbol / per run),
  - helpery meta (`upsert_test_config_metadata`, `next_free_test_id` itd.).
- `data/config_pg.py` – parametry połączenia (host, user, password, DB, schema).
- `data/live_data.db` – alternatywne źródło danych (np. do szybkich lokalnych testów) w formacie zbliżonym do Postgresa.

System jest zaprojektowany tak, aby **wszystkie tryby (backtest GUI, headless, przyszły LiveTest)** korzystały z jednego, spójnego źródła prawdy – tzn. format i parametry świec/trejdów/wskaźników jest wszędzie taki sam (a także logika wyjść/wejść).

### 3.2. Pobieranie danych live (`livefetcher.py`, skrypty FNG)

**`livefetcher.py`**:

- łączy się z Binance Futures (lub innym źródłem),
- pobiera ticki / transakcje / kline’y,
- **sam składa świece** w formacie kompatybilnym z `candles` (Open, High, Low, Close, Volume, Timestamps),
- zapisuje je do bazy (SQLite / Postgres).

**Skrypty Fear&Greed (`scripts/*fng*`, `tools/*fng*`)**:

- okresowo pobierają historię indeksu FNG z zewnętrznego API,
- zapisują do tabeli (np. `public.fear_greed`),
- backtesty i strategie mogą to traktować jak kolejny wskaźnik (`FEAR_GREED`).

### 3.3. Strategia – `RSIStrategy` i rodzina strategii (`backtester/strategies/*`)

Główna strategia w projekcie to `RSIStrategy` w `backtester/strategies/rsi.py`, skonfigurowana przez `rsi_config.py`.

**RSIStrategy robi trzy główne rzeczy:**

1. **Liczenie wskaźników** – `compute_indicators(df)` / `compute_indicators_segmented(df)`:
   - bierze DF z OHLC,
   - wykonuje buckety (wg sekcji `BUCKET`),
   - liczy wszystkie zadeklarowane wskaźniki (`RSI`, `MA`, `MACD`, `BB`, `ATR`, `STOCH`, `STOCH_RSI`, `PCT_CHANGE`, `VOLUME`, `FEAR_GREED` itd.),
   - zwraca DF z kolumnami wskaźników + OHLC.
2. **Sygnały OPEN/CLOSE** –
   - `open_position_signal(row, position)` – zwraca sygnały wejścia na podstawie `PRIMARY_SIGNAL` + sekcji `primary/confirm` z `rsi_config.py`,
   - `close_position_signal(row, position)` – na podstawie `CLOSE_SIGNALS` (BB_close, RSI_close, FEAR_GREED_close) decyduje, czy z punktu widzenia strategii warto zamknąć pozycję przed TP/SL/TS/time_close.
3. **Risk parametry** – `get_risk_params(side, price, atr_here)`:
   - w trybie `FIXED` – mnożniki TP/SL/TS %,
   - w trybie `ATR` – TP/SL/TS wyrażone jako `k * ATR`, z opcjonalnym clampem min/max.

**Architektura jest rozszerzalna:**

- kolejne strategie można dodawać w `backtester/strategies/*.py`, dziedzicząc z `BaseStrategy` (`base.py`),
- każda strategia ma swój config (`*_config.py`),
- `MultiSymbolEngine` i `StrategyTestWorker` traktują je abstrakcyjnie – ważne są metody:
  - `compute_indicators(...)`,
  - `open_position_signal(...)`,
  - `close_position_signal(...)`,
  - `get_risk_params(...)`.

### 3.4. Silnik multi-symboli (`engine.py`, `context.py`)

**`MultiSymbolEngine`** to „serce” risk-engine’u i multi-symbolowego flow. Robi m.in.:

- utrzymuje słownik `contexts[symbol]` – po jednym `SymbolContext` na symbol,
- każdemu `SymbolContext` przechowuje:
  - `current_position` – otwarta pozycja (strona, entry, TP/SL/TS, SLU, timestamps),
  - historię trejdów (`trades`), pilnuje porządku id między symbolami podczas testów (lub test_worker to robi),
- implementuje:
  - `open_position(ctx, candle, signal)` – tworzy nową pozycję na symbolu,
  - `close_position(ctx, candle, reason)` – zamyka pozycję, liczy fee/PnL, aktualizuje stats, zwraca słownik `trade`,
  - `on_tick(symbol, candles, tick=None)` – pojedynczy krok czasowy (ten sam interfejs dla live i backtestu),
  - **opcjonalnie** `run_backtest(progress_cb=None, log_cb=None)` – metoda, która może być użyta jako alternatywny driver backtestu prosto z silnika (nie jest jednak tym, czego używa obecnie `scripts/headless_tester.py`).

**`context.py`** (np. `SymbolContext`):

- trzyma stan lokalny: equity curve, trade_id, parametry per symbol,
- służy też do przechowywania tymczasowych „flag” przy testach (np. cooldowny, pomocnicze timery).

### 3.5. Worker testowy (`test_worker.py`)

`StrategyTestWorker` to QThread odpalany z GUI **i z headless testera** – **driver backtestu**:

- wczytuje świece z DB per symbol (`db_pg.get_candles`),
- woła `strategy.compute_indicators(...)`,
- merges Fear&Greed, dokleja kolumny risk-limitów (TP/SL/TS/TS_BENCHMARK),
- w pętli po świecach:
  - obsługuje uzbrajanie i przesuwanie trailing stopu,
  - obsługuje 2 style zamykania pozycji: na zamknięciu świecy (w momencie wygenerowania sygnału) lub na przecięciu wskaźnika w tym samym close_time (tg. Exit_price = eg. TS)
  - obsługuje SLIPPAGE_ENTRY, SLIPPAGE_EXIT (parametry w rsi_config)
  - mechanizm `SL_UPDATER` (static jump + dynamic SL),
  - zbiera kandydatów CLOSE (TP/SL/TS/time_close + CLOSE_SIGNALS + close_after_x_candles),
  - wybiera finalne `exit_price` z globalnym slippage’em,
  - wywołuje `engine.close_position(...)` + kolekcjonuje trejdy,
  - na brak pozycji: wywołuje `strategy.open_position_signal(...)` + risk-check (min TP/SL) + ENTRY slippage → tworzy pozycję.
- co N świec flushuje wskaźniki i trejdy do DB przez `db_queue` (w headless testerze zazwyczaj wyłączasz zapis wskaźników dla prędkości).

Można o nim myśleć jak o: **„backtest driver + warstwa testowa dla risk-engine’u”**. Strategię traktuje jako czarną skrzynkę, a całą ciężką robotę z TP/SL/TS/SLU robi lokalnie, dzięki czemu łatwo eksperymentować z execution-mode’ami i slippage’em.

### 3.6. GUI (`gui/*`)

Warstwa GUI to głównie:

- `main_window.py` – setup całego okna, layout, integracja przycisków i workerów:
  - start/stop testu,
  - wybór symboli, zakresów dat, strategii,
  - połączenie z `StrategyTestWorker` (sygnały Qt).
- `plot_widget.py` + `candlestick_item.py` + `chart_worker.py`:
  - rysowanie świec (z własną, zoptymalizowaną implementacją „smart candles”),
  - subcharty na wskaźniki, equity curve, FNG itd.,
  - osobny wątek do aktualizacji wykresów, żeby UI nie klatkowalo.
- `performance_widget.py` + `global_stats_widget.py`:
  - globalne i per-symbol statystyki trejdów, winrate, maxDD, R-multiples, itp.
- `trades_table.py`, `ticks_table.py`, `indicators_table.py`:
  - tabele z listą trejdów, lokalnymi tickami (wokół wejść/wyjść), wartościami wskaźników.
- `controls.py`:
  - cała logika kontrolek, dropdownów, checkboksów, presetów testów itp.

W praktyce GUI ma dwie główne role:

1. **„Oscyloskop”** – podgląd tego, co naprawdę dzieje się na świecach, wskaźnikach i pozycjach.
2. **Panel sterowania** – wygodne odpalanie backtestów i debug nieintuicyjnych trejdów (dlaczego tu zamknęło? dlaczego tu nie otworzyło?).

## 4. Konfiguracja i parametryzacja

### 4.1. Konfiguracja aplikacji (`config.py`)

Plik `config.py` zawiera **parametry globalne aplikacji**, podzielone mniej więcej na:

- **techniczne**:
  - częstotliwość odświeżania ticków/live danych,
  - limity GUI (maksymalna liczba świec na wykresie, ilość danych w RAM),
  - ścieżki do baz (`DATA_DB_PATH`, ustawienia PG),
  - parametry stylu (kolory, grubość linii, rozmiary paneli).
- **tradingowe**:
  - fee / commission (%),
  - precision cen (miejsca dziesiętne),
  - lista symboli testowanych,
  - wybór strategii domyślnej,
  - globalne parametry testu (np. `CLOSE_EXECUTION_TYPE`, `CLOSE_EXECUTION_SLIPPAGE`, `ENTRY_EXECUTION_SLIPPAGE`).

To jest „**aplikacyjny**” poziom – mówisz, jak zachowuje się cały system, nie pojedyncza strategia.

### 4.2. Konfiguracja strategii (`rsi_config.py` + dalsze strategie)

Tu wchodzi cała ogromna parametryzacja opisana szerzej w Docs 3 i Docs 4. W skrócie:

- **`PRIMARY_SIGNAL`** – wybór głównego źródła sygnałów (RSI, MA, MACD, BB, STOCH, STOCH_RSI, PCT_CHANGE, FEAR_GREED, VOLUME...),
- **`RISK_MODE`** – FIXED vs ATR,
- **`BUCKET`** – sposób agregacji świec pod wskaźniki,
- **progi TP/SL/TS**, w tym `RISK_PARAMS_ATR_OVERRIDES`,
- **slippage** – osobno na wejście (`ENTRY_EXECUTION_SLIPPAGE`) i wyjście (`CLOSE_EXECUTION_SLIPPAGE`),
- **CLOSE_SIGNALS** – dodatkowe wyjścia z pozycji (BB_close, RSI_close, FEAR_GREED_close),
- **`SL_UPDATER`** – static jump (BE/pół-BE/custom) + dynamic SL (podążający za ceną),
- oraz szczegółowa konfiguracja każdej rodziny wskaźników (`INDICATOR_OVERRIDES`).

W praktyce `rsi_config.py` działa jako **DSL do opisywania strategii** – w jednym pliku określasz:

- co generuje sygnały,
- jak wygląda risk/RR,
- kiedy zamykamy pozycje,
- jak modelujemy wykonanie (execution).

## 5. Typowe scenariusze użycia

### 5.1. Szybki backtest wizualny w GUI

Scenariusz: chcesz szybko zobaczyć „jak to wygląda” na wykresie dla nowej strategii / configu.

1. Edytujesz `rsi_config.py` (lub config innej strategii) – ustawiasz:
   - PRIMARY_SIGNAL,
   - wskaźniki i ich parametry,
   - TP/SL/TS/SLU, CLOSE_SIGNALS, slippage.
2. Upewniasz się, że `config.py` wskazuje na właściwą bazę (`candles` są w DB / live_data.db).
3. Uruchamiasz aplikację (`python main.py` / `mw_cadence_bootstrap.py`).
4. W GUI wybierasz:
   - listę symboli,
   - zakres dat,
   - strategię (np. RSIStrategy),
   - ewentualnie tryb zapisu wskaźników do DB (on/off).
5. Klikasz „Start test” → `StrategyTestWorker` uruchamia pętlę, wyniki pojawiają się na wykresach.

W tym trybie najważniejsze jest: **intuicja** – patrzysz na konkretne trejdy, sprawdzasz, czy wizualnie sygnały i zamknięcia mają sens.

### 5.2. Headless backtest (rapid tester)

Scenariusz: chcesz szybko przepuścić strategię przez dane historyczne bez odpalania GUI – np. żeby:

- porównać różne ustawienia kilku kluczowych parametrów,
- wykluczyć ewidentnie słabe configi,
- mieć ~10× szybsze testy niż w GUI (bo nie ma renderowania i zapisu wskaźników).

Obecnie służy do tego skrypt **`scripts/headless_tester.py`**, który:

- odpala **dokładnie tę samą klasę `StrategyTestWorker`**, co GUI,
- ale robi to **bez głównego okna** (brak `main_window`, brak wykresów),
- zazwyczaj wyłączasz w nim zapis wskaźników do DB, zostawiając tylko:
  - trejdy,
  - statystyki,
  - konfigurację runu (żeby móc odtworzyć test później w GUI).

Efekt:

- logika strategii i risk-engine’u jest 1:1 z GUI (ten sam worker),
- wyniki są porównywalne z backtestem z GUI,
- testy są dużo szybsze, więc nadają się jako **rapid tester** na etapie developmentu strategii.

Na tym etapie **headless_tester nie robi jeszcze automatycznego grid-searcha 1000 configów** – do tego trzeba będzie napisać osobną „maszynkę” (np. pętlę w Pythonie, która generuje configi i odpala kolejne runy headless). 
Architektura jednak to umożliwia – bo `StrategyTestWorker` można parametryzować różnymi configami w kolejnych uruchomieniach.

### 5.3. Analiza konkretnych trejdów

Scenariusz: znalazłeś w statystykach dziwny trejd (np. duża strata) i chcesz go obejrzeć w szczegółach.

1. W tabelce statystyk (GUI) znajdujesz test_id + symbol, który cię interesuje.
2. Ładujesz ten test w GUI (filtr po `test_id` / symbolu).
3. Włączasz wyświetlanie:
   - świec,
   - wskaźnika PRIMARY_SIGNAL,
   - TP/SL/TS/SLU,
   - listy trejdów (trades table).
4. Klikasz w konkretny trejd → GUI przenosi cię na ten fragment wykresu, pokazując:
   - gdzie był entry,
   - jak szedł trailing stop,
   - które CLOSE_SIGNALS się włączyły (BB/RSI/FNG),
   - jaki był faktyczny `exit_price` po slippage.

To jest super narzędzie do **„post mortem” pojedynczych zagrań** i szukania bugów w logice.

### 5.4. (Przyszłość) LiveTest / paper-trading

Scenariusz: chcesz puścić strategię na żywych danych, ale jeszcze bez prawdziwych zleceń – tylko paper/PnL w DB.

1. `livefetcher` zbiera świece na bieżąco do `candles`.
2. Tworzysz `LiveWorker`, który:
   - co X sekund wczytuje nowe świece,
   - dla każdej nowej zamkniętej świecy wywołuje `engine.on_tick(symbol, window, tick=None)`,
   - korzysta z tej samej `RSIStrategy` i `rsi_config.py`.
3. `MultiSymbolEngine` generuje sygnały OPEN/CLOSE, a ty:
   - zapisujesz trejdy do DB (paper),
   - wykres w GUI może być odświeżany live (performance, świeczki, pozycje).

W tym scenariuszu **nie ma realnych zleceń** – ale logika sygnałów jest 1:1 z backtestem, co umożliwia bardzo uczciwe porównanie „backtest vs rzeczywistość”.

### 5.5. (Przyszłość) Live trading z Executorem

Scenariusz: po przejściu całej ścieżki (backtest → LiveTest → paper) chcesz włączyć prawdziwe algotradingowe wykonanie.
1. Reużywasz LiveWorker z poprzedniego punktu.

2. Dodajesz **router/executor** jako osobny proces/usługę:
   - LiveWorker wysyła eventy `open_signal` / `close_signal` (symbol, side, price modelowy, TP/SL, typ sygnału, max_slippage, max_delay),
   - Executor tłumaczy to na call’e do API giełdy (order / cancel / replace),
   - Executor zapisuje realne fill’e do DB (osobna tabela),
   - masz w DB zarówno „modelowe” trejdy (z engine’a), jak i realne (z giełdy).
3. Możesz na tej bazie liczyć **realistyczny slippage / execution risk** i sprawdzać, ile backtest „oszukuje” w zależności od warunków rynku.

## 6. Rozszerzalność i roadmapa

### 6.1. Dodawanie nowych strategii / wskaźników

Aby dodać **nową strategię**:

1. Tworzysz plik w `backtester/strategies/`, np. `ma_breakout.py`.
2. Dziedziczysz z `BaseStrategy` i implementujesz:
   - `compute_indicators`,
   - `open_position_signal`,
   - (opcjonalnie) `close_position_signal`, `get_risk_params`.
3. Tworzysz odpowiadający config `ma_breakout_config.py`.
4. Rejestrujesz strategię w `config.py` / GUI (dropdown z listą strategii).

Nowe wskaźniki można podpiąć na dwa sposoby:

- jako kolumny liczone w `compute_indicators(...)`,
- dodając do nich sekcję w globalnym słowniku `INDICATOR_OVERRIDES` (własne `primary`/`confirm`).

### 6.2. Planowane / możliwe rozszerzenia (ideowe)

Na bazie dotychczasowych rozmów, system ma naturalną ścieżkę rozwoju w kierunku:

- **partial exits** – np. 50% pozycji na BE-jump, reszta trzymana na oryginalnym SL/TS,
- **equity-based risk management** – globalne limity typu „max dd dzienny / tygodniowy”, „equity trailing stop” na portfel,
- **multi-timeframe logic** – wejścia z M1, risk z M5, filtry z H1,
- **portfolio-level manager** – zarządzanie łącznym ryzykiem na wiele strategii/symboli jednocześnie,
- bardziej zaawansowana **execution layer** – spread-aware, volatility-aware, orderbook-aware, z dynamicznym modelem slippage’u,
- rozbudowane **raporty i dashboardy** – np. zewnętrzny notebook/Jupyter nad bazą z trejdami/wskaźnikami/live OB z jakiegoś zaawansowanego feedu (w przyszłości)

I PRZEDE WSZYSTKIM:
- rozbudowane **PAPER-TRADING** / **LIVE-TESTING** - główny cel (teraz) na roadmapie

Obecna architektura (wspólny silnik, wspólna baza, oddzielne warstwy: strategia ↔ engine ↔ GUI ↔ executor) dobrze przygotowuje grunt pod te rozszerzenia – większość z nich to „tylko” nowe moduły na istniejącym szkielecie.

---

Na tym poziomie dokument 6 ma być mapą: **co jest gdzie i jak się ze sobą łączy**. Szczegóły implementacyjne poszczególnych klocków znajdziesz w Docs 1–5, ale do zrozumienia całości wystarczy właśnie ten bird’s-eye view.
