# gui/utils.py

import pyqtgraph as pg

def configure_pg_fast_defaults():
    """
    Globalne ustawienia pyqtgraph pod duże datasety.
    Jeśli nie ma PyOpenGL, automatycznie wyłączamy useOpenGL.
    """
    import logging, pyqtgraph as pg
    use_gl = False
    try:
        # sprawdź dostępność OpenGL (PyOpenGL)
        import OpenGL.GL  # noqa: F401
        use_gl = True
    except Exception as e:
        logging.info(f"[PG] OpenGL not available -> falling back to CPU. ({e})")
        use_gl = False

    try:
        pg.setConfigOptions(
            useOpenGL=use_gl,
            antialias=False,
            enableExperimental=True,
            imageAxisOrder='row-major',
        )
    except Exception:
        # awaryjnie ustaw bez GL
        pg.setConfigOptions(
            useOpenGL=False,
            antialias=False,
            enableExperimental=True,
            imageAxisOrder='row-major',
        )

def tune_curve_fast(curve):
    """
    Ustaw typowe flagi przyspieszające krzywe (wskaźniki, equity).
    Używaj zaraz po utworzeniu PlotCurveItem.
    """
    try:
        curve.setClipToView(True)  # renderuj tylko to, co widać
    except Exception:
        pass
    try:
        curve.setDownsampling(auto=True, method='peak')  # decymacja zachowująca piki
    except Exception:
        pass
    if hasattr(curve, "setSkipFiniteCheck"):  # PG 0.13+
        try:
            curve.setSkipFiniteCheck(True)
        except Exception:
            pass
    try:
        # rysuj linie tylko między finite punktami (bez segmentów na NaN)
        curve.opts['connect'] = 'finite'
    except Exception:
        pass
