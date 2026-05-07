
# PATCH NOTES (Livetest Panel)

## What changed
- Rewrote `db_helpers.py` to a robust, idempotent version (UPSERTs + schema guard).
- Added `mw_posttest_hooks.py` with two helpers:
  - `persist_test_metadata(...)`
  - `persist_stats(...)`

## How to wire (minimal)
In `gui/test_worker.py`, after you compute the global range and interval and before `finished_signal`:
```python
from mw_posttest_hooks import persist_test_metadata, persist_stats
# ... inside the worker after a test is done:
persist_test_metadata(self.db, DATABASE_PATH, engine.test_id, self.symbols, start_s, end_s, interval_str)
persist_stats(self.db, DATABASE_PATH, engine.test_id, rows)  # 'rows' is your per-symbol summary
```
If you already put `self.db_queue.put({"type": "update_test_config", ...})` / `{"type": "replace_stats_rows", ...}` – keep it; the db writer will call the same helpers.

No other refactors required.
