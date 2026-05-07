# -*- coding: utf-8 -*-
import os, sys, threading, time, queue, logging
from config import AVAILABLE_PAIRS, DEFAULT_FETCH_INTERVAL, DEFAULT_CANDLE_INTERVAL

ROOT_DIR = os.path.dirname(os.path.abspath(os.path.join(__file__, os.pardir)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from candle_batch_worker import CandleBatchWriter

def attach_live_cadence(main_window):
    main_window.candle_queue = queue.Queue()

    expected = list(AVAILABLE_PAIRS)

    main_window.candle_batcher = CandleBatchWriter(
        db=main_window.db,
        candle_queue=main_window.candle_queue,
        candle_interval_minutes=int(DEFAULT_CANDLE_INTERVAL),
        flush_delay_sec=3,
        expected_symbols=expected,
        force_flush_timeout_mult=2
    )
    main_window.candle_batcher.start()

    def _cadence_loop():
        cadence = int(DEFAULT_FETCH_INTERVAL)
        while True:
            try:
                sleeps = []
                for _, f in getattr(main_window, "fetchers", {}).items():
                    try:
                        sleeps.append(f.next_cadence_sleep(cadence))
                    except Exception:
                        sleeps.append(1.0)
                to_sleep = max(0.0, min(sleeps) if sleeps else 1.0)
                time.sleep(to_sleep)
                edge_ts = int(time.time())
                logging.info("[CADENCE] edge=%s cadence=%ss", edge_ts, cadence)

                for sym, f in getattr(main_window, "fetchers", {}).items():
                    _, closed = f.tick()
                    if closed:
                        logging.debug("[CADENCE] closed sym=%s ot=%s ct=%s", sym, closed.get("open_time"), closed.get("close_time"))
                        main_window.candle_queue.put({"symbol": sym, "candle": closed})

                    live = f.get_inprogress_candle()
                    if live and hasattr(main_window, "plot_widget"):
                        try:
                            main_window.plot_widget.update_live(live)
                        except Exception:
                            pass
            except Exception as e:
                logging.error("[mw_cadence_bootstrap] loop error: %s", e, exc_info=True)
                time.sleep(0.5)

    t = threading.Thread(target=_cadence_loop, name="LIVE_LOOP", daemon=True)
    t.start()
