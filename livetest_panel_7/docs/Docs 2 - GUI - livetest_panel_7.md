# LIVETEST_PANEL – Dokumentacja GUI / Frontend

_Scope: main_window.py, plot_widget.py, plot_aggregation.py, candlestick_item.py, chart_worker.py, performance_widget.py, trades_table.py, ticks_table.py, indicators_table.py, mw_cadence_bootstrap.py, candle_batch_worker.py, global_stats_widget.py, controls.py, utils.py_

## Spis treści

- [1. High-level overview GUI](#1.-high-level-overview-gui)
  - [1.1. Architektura GUI (zgrubny schemat)](#1.1.-architektura-gui-(zgrubny-schemat))
- [2. Główne komponenty GUI](#2.-glowne-komponenty-gui)
  - [2.1. `main_window.py` – szkielet aplikacji okienkowej](#2.1.-`main_window.py`-–-szkielet-aplikacji-okienkowej)
  - [2.2. `plot_widget.py` – główny wykres i subcharty](#2.2.-`plot_widget.py`-–-glowny-wykres-i-subcharty)
  - [2.3. `plot_aggregation.py` – agregacja świec (LOD)](#2.3.-`plot_aggregation.py`-–-agregacja-swiec-(lod))
  - [2.4. `candlestick_item.py` – rysowanie świec](#2.4.-`candlestick_item.py`-–-rysowanie-swiec)
  - [2.5. `chart_worker.py` – wątek odświeżania wykresów](#2.5.-`chart_worker.py`-–-watek-odswiezania-wykresow)
  - [2.6. `performance_widget.py` – panel wyników](#2.6.-`performance_widget.py`-–-panel-wynikow)
  - [2.7. Tabelki: `trades_table.py`, `ticks_table.py`, `indicators_table.py`](#2.7.-tabelki:-`trades_table.py`,-`ticks_table.py`,-`indicators_table.py`)
  - [2.8. `global_stats_widget.py` – globalne statystyki](#2.8.-`global_stats_widget.py`-–-globalne-statystyki)
  - [2.9. `controls.py` – kontrolki GUI (przyciski, filtry, itp.)](#2.9.-`controls.py`-–-kontrolki-gui-(przyciski,-filtry,-itp.))
  - [2.10. `mw_cadence_bootstrap.py` – cykl życia okna](#2.10.-`mw_cadence_bootstrap.py`-–-cykl-zycia-okna)
  - [2.11. `candle_batch_worker.py` – worker świec dla GUI](#2.11.-`candle_batch_worker.py`-–-worker-swiec-dla-gui)
  - [2.12. `utils.py` – drobne helpery](#2.12.-`utils.py`-–-drobne-helpery)
- [3. Interakcja GUI ↔ backend (overview)](#3.-interakcja-gui-↔-backend-(overview))
- [4. Mini „user guide” dla tradera](#4.-mini-„user-guide”-dla-tradera)
  - [4.1. Typowy workflow w GUI](#4.1.-typowy-workflow-w-gui)
  - [4.2. Gdzie co jest na ekranie (logicznie)](#4.2.-gdzie-co-jest-na-ekranie-(logicznie))
- [5. Rozszerzanie GUI w przyszłości (ideas)](#5.-rozszerzanie-gui-w-przyszlosci-(ideas))


GUI w LIVETEST_PANELU to warstwa odpowiedzialna za **wizualizację danych** (świece, wskaźniki, trejdy, statystyki) oraz **interakcję użytkownika** z backendem (uruchamianie testów, filtrowanie, zmiana layoutu, itp.).

## 1. High-level overview GUI

Najważniejsze role GUI:

- Wyświetlanie świec (historycznych i live) na głównym wykresie.
- Overlay wskaźników technicznych oraz poziomów SL/TP/TrailingStop / SLU.
- Prezentacja trejdów (wejścia/wyjścia) oraz statystyk globalnych i per-symbol.
- Kontrola nad testami (start/stop, wybór strategii, filtr symboli, parametry layoutu).

GUI jest zbudowane na **PyQt5** + **pyqtgraph** i komunikuje się z backendem przez kolejki/worker’y. Backtesty masowe mogą lecieć headless, a GUI służy głównie do:

- wizualnej walidacji działania strategii,
- analizy pojedynczych trejdów i nietypowych sytuacji,
- live-podglądu sygnałów w trybie livetestu.

### 1.1. Architektura GUI (zgrubny schemat)

```text
             +----------------------+
             |      main.py        |
             |  (startuje GUI)     |
             +----------+-----------+
                        |
                        v
              main_window.MainWindow
                        |
           +------------+------------+
           |                         |
           v                         v
    plot_widget.PlotWidget      widgets (tabele,
    + plot_aggregation          statystyki, logi)
           |
   +-------+--------+
   |                |
   v                v
candlestick_item   chart_worker (wątek odświeżania)
```

Dodatkowe komponenty:

- `performance_widget.py` – panel statystyk wydajności i wyników strategii.
- `trades_table.py`, `ticks_table.py`, `indicators_table.py` – tabelki z danymi szczegółowymi.
- `global_stats_widget.py` – globalne metryki testu / portfela.
- `controls.py` – wszelkie przyciski, checkboxy, filtry sterujące zachowaniem GUI i backendu.
- `mw_cadence_bootstrap.py` – pomoc w „bootstrappingu” głównego okna i cyklicznych refreshy.
- `candle_batch_worker.py` – worker odpowiedzialny za podawanie świec do GUI partiami.
- `utils.py` – drobne helpery używane w kilku miejscach (formatowanie, kolory, itp.).

## 2. Główne komponenty GUI

### 2.1. `main_window.py` – szkielet aplikacji okienkowej

Moduł `main_window.py` zawiera klasę **`MainWindow`**, która:

- Dziedziczy po `QMainWindow` i składa całą aplikację z:
  - głównego wykresu cenowego,
  - subchartów na wskaźniki / equity curve,
  - paneli z tabelami trejdów, ticków, wskaźników,
  - panelu globalnych statystyk i logów,
  - górnych/dolnych kontrolek (przyciski, wybór strategii, symboli itd.).
- Inicjalizuje połączenie z backendem (kolejki, worker’y testowe, DB context).
- Zarządza layoutem okna (presety layoutu, zapisywanie/odtwarzanie ustawień).
- Reaguje na sygnały z workerów (np. gotowe batch’e świec, nowe trejdy, logi) i przekazuje je dalej do odpowiednich widoków.

Pod kątem użytkownika `MainWindow` to „centrum sterowania” – w jednym miejscu:

- wybierasz **co** testujesz (strategia, symbole, zakres),
- widzisz **jak** to się zachowuje na wykresie,
- możesz filtrować i przeglądać wszystkie trejdy/wskaźniki/statystyki.

### 2.2. `plot_widget.py` – główny wykres i subcharty

`plot_widget.py` odpowiada za **obsługę wykresów** opartych na pyqtgraph:

- Główny wykres cenowy (świece + overlay wskaźników).
- Subcharty: np. RSI, MACD, wolumen, equity curve.
- Skalowanie, pan/zoom, obsługa osi czasu/ceny.
- Nakładanie:
  - pozycji (wejścia/wyjścia jako markery),
  - poziomów SL/TP/TS,
  - dodatkowych linii (np. FNG, BB, MAs).

Kluczowe funkcje tego modułu:

- Tworzenie i konfiguracja `PlotWidget`/`PlotItem` dla poszczególnych chartów.
- Definiowanie kolorów/pen’ów dla różnych serii (price, wskaźniki, equity).
- Współpraca z `plot_aggregation.py` przy dynamicznej agregacji świec (LOD – level of detail).

### 2.3. `plot_aggregation.py` – agregacja świec (LOD)

Przy dużej liczbie świec rysowanie wszystkiego 1:1 byłoby wolne i mało czytelne. `plot_aggregation.py` oferuje logikę:

- **Dynamicznej agregacji świec** w zależności od zoomu:
  - przy mocnym zoom-out: łączymy wiele świec źródłowych w jedną świecę wyświetlaną,
  - przy zoom-in: przechodzimy do pełnej rozdzielczości.
- Wyliczanie optymalnej liczby „binów” / świec na ekran (`PLOT_TARGET_MIN_BINS`, `PLOT_TARGET_MAX_BINS` z `config.py`).
- Budowanie piramidy interwałów (np. 1m, 3m, 5m, 15m, 1h, 4h, 1d, 1w) i mapowanie ich na aktualny zakres widoku.

Dzięki temu GUI pozostaje responsywne nawet przy bardzo długich okresach historycznych, a użytkownik widzi „podsumowanie przebiegu ceny” na odpowiednim poziomie detalu.

### 2.4. `candlestick_item.py` – rysowanie świec

- Odpowiada za **rendering pojedynczych świec** w pyqtgraph (custom `GraphicsItem`).
- Implementuje tzw. „smart candles”: optymalizacje pod kątem szybkości rysowania i ilości obiektów graficznych.
- Wykorzystuje parametry stylów z `config.py` (kolory świeczek wzrostowych/spadkowych, grubość, cienie itd.).

W praktyce to tu decyduje się, jak dokładnie wygląda świeca na ekranie – zarówno przy pojedynczych tickach, jak i przy mocno zagęszczonych wykresach.

### 2.5. `chart_worker.py` – wątek odświeżania wykresów

`chart_worker.py` zawiera wątek/wrapper, który:

- W regularnych odstępach czasu (lub po sygnale) odświeża dane na wykresach.
- Pobiera z backendu najnowsze:
  - świece,
  - wskaźniki,
  - markery trejdów,
i przekazuje je do `PlotWidget`/`candlestick_item`.
- Stara się odciążyć główny wątek GUI poprzez wykonywanie cięższych operacji (np. agregacji) poza główną pętlą zdarzeń.

### 2.6. `performance_widget.py` – panel wyników

Moduł `performance_widget.py` rysuje i aktualizuje panel z **performancem strategii**:

- Equity curve (krzywa kapitału) w czasie.
- Podstawowe metryki typu:
  - liczba trejdów,
  - winrate,
  - łączny PnL / PnL per symbol,
  - max drawdown (jeśli liczony).
- Może korzystać z danych z `SymbolContext` / zapisanych trejdów w DB.

To jest miejsce, gdzie trader jednym rzutem oka widzi, „czy ta konfiguracja robi sens”, zanim zacznie kopać w pojedynczych trejdach.

### 2.7. Tabelki: `trades_table.py`, `ticks_table.py`, `indicators_table.py`

Te moduły dostarczają **widoki tabelaryczne** dla:

- `trades_table.py` – lista trejdów:
  - symbol, side (long/short), entry/exit, SL/TP/TS/SLU poziomy, fees, PnL, itp.
- `ticks_table.py` – zrzut tików w okolicy trejdów (do analizy micro-struktury wejść/wyjść).
- `indicators_table.py` – wartości wskaźników w czasie (do debugowania logiki strategii).

Każda tabelka typowo pozwala:

- filtrować po symbolu / czasie,
- sortować po kolumnach (np. po PnL, czasie wejścia),
- w przyszłości powiązać kliknięcie w wiersz z podświetleniem danego trejda na wykresie.

### 2.8. `global_stats_widget.py` – globalne statystyki

- Wyświetla zagregowane metryki dla całej sesji testowej / portfela symboli:
  - łączny PnL,
  - PnL per symbol/strategia,
  - liczba trejdów, winrate, average R, itp. (w zależności od zaimplementowanych metryk).
- Może też pokazywać status workerów, czasu działania, zużycia pamięci – w zależności od aktualnej wersji kodu.

### 2.9. `controls.py` – kontrolki GUI (przyciski, filtry, itp.)

Moduł `controls.py` opakowuje logikę wszystkich **elementów sterujących**:

- przyciski start/stop testu,
- wybór strategii/configu,
- wybór symboli, zakresu dat, interwału świec,
- togglowanie wskaźników / overlayów,
- filtry (np. po PnL, po typie sygnału).

Zadaniem `controls.py` jest rozmawiać w obie strony:

- z użytkownikiem – reaguje na kliknięcia i zmiany,
- z backendem – odpala odpowiednie akcje w engine/test_worker, przekazuje nowe parametry strategii lub testu.

### 2.10. `mw_cadence_bootstrap.py` – cykl życia okna

- Zawiera logikę „bootstrappingu” głównego okna:
  - wstępne ustawienie layoutu,
  - start cyklicznych timerów odświeżania,
  - podpięcie callbacków pod eventy PyQt.
- Upraszcza utrzymanie `MainWindow`, wyciągając część logiki startowej do osobnego modułu.

### 2.11. `candle_batch_worker.py` – worker świec dla GUI

- Odpowiada za dostarczanie **porcji świec** do GUI (np. przy przesuwaniu się okna czasowego, zoomie).
- Może korzystać z `plot_aggregation` i limitów z `config.py`, aby nie zasypywać wykresu zbyt dużą liczbą punktów.
- Dzięki temu GUI może pracować na tym samym DB co backtester, ale z własnym buforem zakresu czasowego.

### 2.12. `utils.py` – drobne helpery

- Zawiera pomocnicze funkcje używane przez pozostałe moduły GUI, np.:
  - formatowanie wartości (ceny, daty, procenty),
  - mapowanie kolorów/penów,
  - drobne adaptery pod pyqtgraph / Qt.

## 3. Interakcja GUI ↔ backend (overview)

W uproszczeniu komunikacja wygląda tak:

1. Użytkownik w GUI (kontrolki z `controls.py`) odpala test / wybiera strategię / zmienia zakres danych.
2. `MainWindow` konfiguruje `MultiSymbolEngine` / worker’y (`test_worker.py`) i zleca start testu.
3. Worker’y przeliczają dane i zapisują wyniki do DB (świece, wskaźniki, trejdy, statystyki).
4. `chart_worker` + `candle_batch_worker` dociągają z DB zakres świec/wskaźników potrzebnych do wyświetlenia.
5. `plot_widget` + `candlestick_item` rysują wykres, a tabelki (`trades_table`, `ticks_table`, `indicators_table`) oraz widgety statystyk (`performance_widget`, `global_stats_widget`) pokazują dane w formie tabelarycznej/liczbowej.

Dzięki temu GUI jest w dużej mierze **klientem** danych trzymanych w DB/backendzie – można odłączyć je od masowych headless testów i używać przede wszystkim jako narzędzia do wizualnej analizy.

## 4. Mini „user guide” dla tradera

Ta sekcja jest po to, żeby ktoś, kto nie siedzi w kodzie, mógł zrozumieć, **co gdzie jest** i **jak tego używać**.

### 4.1. Typowy workflow w GUI

1. **Uruchom aplikację** (main.py → MainWindow).
2. W górnym panelu (kontrolki):
   - wybierz **strategię** i **config** (np. `RSI` + konkretny `rsi_config`),
   - wybierz **symbole** oraz **zakres dat** / typ danych (history/live).
3. Kliknij **Start test**.
4. Obserwuj na głównym wykresie:
   - świece,
   - entry/exit trejdów,
   - poziomy SL/TP/TS,
   - wskaźniki (w subchartach).
5. W tabeli trejdów możesz sprawdzić szczegóły poszczególnych transakcji (P&L, czasy, poziomy).
6. W performance/global stats widzisz zagregowane metryki – czy strategia/konfiguracja ma sens.

### 4.2. Gdzie co jest na ekranie (logicznie)

- **Górna część** – kontrolki sterujące (strategia, symbole, start/stop, filtry).
- **Centralny panel** – główny wykres świec + overlay wskaźników / trade markers.
- **Dolne/subpane** – subcharty (wskaźniki takie jak RSI/MACD, equity curve).
- **Prawy/dolny panel** – tabele trejdów/ticków/wskaźników, globalne statystyki, logi.

## 5. Rozszerzanie GUI w przyszłości (ideas)

Frontend jest już wystarczająco elastyczny, żeby można było relatywnie łatwo dodać:

- Podświetlanie trejda na wykresie po kliknięciu w tabelę `trades_table` | - JUŻ JEST (zaznacz trejd z tabeli + kliknij "J" *jump*)!
- Dodatkowe panele pod live/paper trading (np. real-time status executora, kolejka zleceń) | - Do monitoringu executora raczej osobny panel powstanie
- Screenshot/export wykresów i raportów wprost z GUI | - Snapshoty/exporty wykresów i tabelek są już dostępne