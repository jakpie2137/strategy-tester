#!/usr/bin/env python3
"""
TV-TA DASHBOARD (live, dark, synced)
- Auto-reload danych z DB co REFRESH_MS (bez restartu aplikacji).
- Dedup: 1 najnowszy snapshot per bucket 15m (tv_time preferowane; fallback retrieved_at).
- Wykres 1 (główny): świeczki + opcjonalne Pivots (Fibo/Classic) + Recommend.All & change na Y2.
  * TYLKO tutaj działa zoom/pan/scroll (X i Y).
- Wykresy 2-5 (summary / v2 / oscillators / MA):
  * brak zoomu/pana, osie Y zablokowane,
  * oś X synchronizowana z głównym; gdy nie było zoomu – autorange, więc nowy bucket pojawia się od razu.
- Słupki: BOTTOM→TOP = BUY (zielony) → NEUTRAL (szary) → SELL (czerwony).
- Wheel pass-through: przewijanie strony działa nawet gdy kursor jest nad zablokowanymi wykresami.

Uruchom:
  pip install dash plotly pandas
  python tv_ta_dashboard.py
"""
import json
import sqlite3
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output, State, no_update

DB_PATH = "data/tv_ta.db"
INTERVAL_MINUTES = 60  # 1-hour interval
REFRESH_MS = 900_000  # odświeżenie danych z DB co 60s (60_000) lub co 15min (900_000)

# --- Dark theme + kolory ---
BG_DARK = "#121212"
PANEL_DARK = "#1e1e1e"
FG_LIGHT = "#e0e0e0"
GRID = "#3a3a3a"
COLOR_BUY = "#66bb6a"
COLOR_SELL = "#ef5350"
COLOR_NEU = "#9e9e9e"

REC_MAP = {"STRONG_SELL": -2, "SELL": -1, "NEUTRAL": 0, "BUY": 1, "STRONG_BUY": 2}
REC_COLORS = {-2: "#b71c1c", -1: "#ef5350", 0: "#9e9e9e", 1: "#66bb6a", 2: "#1b5e20"}

FIB_KEYS = [
    "Pivot.M.Fibonacci.S3", "Pivot.M.Fibonacci.S2", "Pivot.M.Fibonacci.S1",
    "Pivot.M.Fibonacci.Middle",
    "Pivot.M.Fibonacci.R1", "Pivot.M.Fibonacci.R2", "Pivot.M.Fibonacci.R3",
]
CLASSIC_KEYS = [
    "Pivot.M.Classic.S3", "Pivot.M.Classic.S2", "Pivot.M.Classic.S1",
    "Pivot.M.Classic.Middle",
    "Pivot.M.Classic.R1", "Pivot.M.Classic.R2", "Pivot.M.Classic.R3",
]
INDICATOR_REC_KEYS = ["Recommend.Other", "Recommend.All", "Recommend.MA", "change"]


# ---------- DB IO & transform ----------
def load_raw_df(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM snapshots WHERE interval='15m'", conn)
    conn.close()
    if df.empty:
        return df
    df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    df["tv_time"] = pd.to_datetime(df["tv_time"], utc=True, errors="coerce")
    df["ts"] = df["tv_time"].fillna(df["retrieved_at"])
    df["bucket"] = df["ts"].dt.floor(f"{INTERVAL_MINUTES}min")
    for col in ["summary_json", "oscillators_json", "moving_averages_json", "indicators_json"]:
        df[col] = df[col].apply(lambda x: json.loads(x) if isinstance(x, str) else (x if isinstance(x, dict) else {}))
    return df


def dedupe_latest_per_bucket(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df_sorted = df.sort_values(["symbol", "bucket", "retrieved_at"])  # ASC → last = najnowszy
    return df_sorted.drop_duplicates(subset=["symbol", "bucket"], keep="last")


def extract_fields(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    # SUMMARY
    out["sum_reco_str"] = out["summary_json"].apply(lambda d: d.get("RECOMMENDATION"))
    out["sum_reco"] = out["sum_reco_str"].map(REC_MAP)
    out["sum_buy"] = out["summary_json"].apply(lambda d: int(d.get("BUY", 0) or 0))
    out["sum_sell"] = out["summary_json"].apply(lambda d: int(d.get("SELL", 0) or 0))
    out["sum_neu"] = out["summary_json"].apply(lambda d: int(d.get("NEUTRAL", 0) or 0))
    # INDICATORS (v2)
    for k in INDICATOR_REC_KEYS:
        out[k.replace(".", "_")] = out["indicators_json"].apply(lambda d, kk=k: d.get(kk))
    # PIVOTS
    for k in FIB_KEYS + CLASSIC_KEYS:
        out[k.replace(".", "_")] = out["indicators_json"].apply(lambda d, kk=k: d.get(kk))
    # OSCILLATORS
    out["osc_reco_str"] = out["oscillators_json"].apply(lambda d: d.get("RECOMMENDATION"))
    out["osc_reco"] = out["osc_reco_str"].map(REC_MAP)
    out["osc_buy"] = out["oscillators_json"].apply(lambda d: int(d.get("BUY", 0) or 0))
    out["osc_sell"] = out["oscillators_json"].apply(lambda d: int(d.get("SELL", 0) or 0))
    out["osc_neu"] = out["oscillators_json"].apply(lambda d: int(d.get("NEUTRAL", 0) or 0))
    # MAs
    out["ma_reco_str"] = out["moving_averages_json"].apply(lambda d: d.get("RECOMMENDATION"))
    out["ma_reco"] = out["ma_reco_str"].map(REC_MAP)
    out["ma_buy"] = out["moving_averages_json"].apply(lambda d: int(d.get("BUY", 0) or 0))
    out["ma_sell"] = out["moving_averages_json"].apply(lambda d: int(d.get("SELL", 0) or 0))
    out["ma_neu"] = out["moving_averages_json"].apply(lambda d: int(d.get("NEUTRAL", 0) or 0))
    return out


def load_df_prepared() -> pd.DataFrame:
    return extract_fields(dedupe_latest_per_bucket(load_raw_df(DB_PATH)))


# ---------- Figures ----------
def make_main_figure(df_sym: pd.DataFrame, title: str, show_pivots: bool = True) -> go.Figure:
    fig = go.Figure()
    # Candles
    fig.add_trace(go.Candlestick(
        x=df_sym["bucket"], open=df_sym["price_open"], high=df_sym["price_high"],
        low=df_sym["price_low"], close=df_sym["price_close"], name="Price"
    ))
    # Pivots opcjonalne
    if show_pivots:
        for k in FIB_KEYS:
            col = k.replace(".", "_")
            if col in df_sym:
                fig.add_trace(go.Scatter(x=df_sym["bucket"], y=df_sym[col], mode="lines", name=k, line=dict(width=1)))
        for k in CLASSIC_KEYS:
            col = k.replace(".", "_")
            if col in df_sym:
                fig.add_trace(go.Scatter(x=df_sym["bucket"], y=df_sym[col], mode="lines", name=k, line=dict(width=1, dash="dot")))
    # v2 + change na Y2
    if "Recommend_All" in df_sym:
        fig.add_trace(go.Scatter(x=df_sym["bucket"], y=df_sym["Recommend_All"], mode="lines", name="Recommend.All [-1..1]", yaxis="y2"))
    if "change" in df_sym:
        fig.add_trace(go.Scatter(x=df_sym["bucket"], y=df_sym["change"], mode="lines", name="change", yaxis="y2"))

    fig.update_layout(
        template="plotly_dark",
        title=title,
        dragmode="zoom",
        uirevision="keep",
        xaxis=dict(rangeslider=dict(visible=False), type="date", gridcolor=GRID, gridwidth=0.6, zerolinecolor=GRID),
        yaxis=dict(title="Price", gridcolor=GRID, gridwidth=0.6, fixedrange=False),
        yaxis2=dict(title="Rec.All / change", overlaying="y", side="right", showgrid=False, fixedrange=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color=FG_LIGHT)),
        margin=dict(l=40, r=40, t=50, b=40),
        paper_bgcolor=BG_DARK, plot_bgcolor=PANEL_DARK, font=dict(color=FG_LIGHT),
    )
    return fig


def make_reco_counts_figure(df_sym: pd.DataFrame, reco_col: str, buy_col: str, sell_col: str, neu_col: str, title: str) -> go.Figure:
    fig = go.Figure()
    # rec line
    fig.add_trace(go.Scatter(
        x=df_sym["bucket"], y=df_sym[reco_col], mode="lines+markers", name="rec [-2..2]",
        marker=dict(size=7, color=[REC_COLORS.get(v, COLOR_NEU) for v in df_sym[reco_col]]),
        line=dict(width=2, color="#cfd8dc")
    ))
    # BOTTOM->TOP: BUY, NEUTRAL, SELL
    fig.add_trace(go.Bar(x=df_sym["bucket"], y=df_sym[buy_col], name="BUY", yaxis="y2", opacity=0.75, marker_color=COLOR_BUY))
    fig.add_trace(go.Bar(x=df_sym["bucket"], y=df_sym[neu_col], name="NEUTRAL", yaxis="y2", opacity=0.6, marker_color=COLOR_NEU))
    fig.add_trace(go.Bar(x=df_sym["bucket"], y=df_sym[sell_col], name="SELL", yaxis="y2", opacity=0.75, marker_color=COLOR_SELL))

    fig.update_layout(
        template="plotly_dark",
        title=title,
        barmode="stack",
        yaxis=dict(title="rec [-2..2]", range=[-2.1, 2.1], gridcolor=GRID, gridwidth=0.6, fixedrange=True),
        yaxis2=dict(title="counts", overlaying="y", side="right", fixedrange=True),
        xaxis=dict(type="date", gridcolor=GRID, gridwidth=0.6, fixedrange=True),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color=FG_LIGHT)),
        margin=dict(l=40, r=40, t=50, b=40),
        paper_bgcolor=BG_DARK, plot_bgcolor=PANEL_DARK, font=dict(color=FG_LIGHT),
    )
    return fig


def make_ind_v2_figure(df_sym: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for key in ["Recommend_All", "Recommend_MA", "Recommend_Other"]:
        if key in df_sym:
            fig.add_trace(go.Scatter(x=df_sym["bucket"], y=df_sym[key], mode="lines", name=key))
    fig.update_layout(
        template="plotly_dark",
        title="Indicators Recommendation v2 (All / MA / Other)",
        yaxis=dict(title="[-1..1]", range=[-1.05, 1.05], gridcolor=GRID, gridwidth=0.6, fixedrange=True),
        xaxis=dict(type="date", gridcolor=GRID, gridwidth=0.6, fixedrange=True),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color=FG_LIGHT)),
        margin=dict(l=40, r=40, t=50, b=40),
        paper_bgcolor=BG_DARK, plot_bgcolor=PANEL_DARK, font=dict(color=FG_LIGHT),
    )
    return fig


# ---------- Dash app ----------
app = Dash(__name__)
app.index_string = """
<!DOCTYPE html>
<html>
  <head>
    {%metas%}
    <title>TV-TA Dashboard</title>
    {%favicon%}
    {%css%}
    <style>
      html, body, #_dash-app-content, #_dash-app-layout, #react-entry-point { 
        background-color: #121212; color: #e0e0e0; margin: 0; padding: 0; 
      }
      .app-container { padding: 16px; font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, 'Helvetica Neue', Arial; }
      .card { background:#1e1e1e; border:1px solid #333; border-radius:10px; padding:12px; margin-bottom:16px; }
      .dash-dropdown .Select, .dash-dropdown .Select-control, .dash-dropdown .Select-menu-outer, .dash-dropdown .Select-value, .dash-dropdown .Select-value-label, .dash-dropdown .Select-placeholder, .dash-dropdown .Select-input > input { 
        background-color:#1e1e1e !important; color:#e0e0e0 !important; border-color:#333 !important; 
      }
      .dash-dropdown .Select-menu-outer { border:1px solid #333; }
      .dash-dropdown .Select-option { background-color:#1e1e1e; color:#e0e0e0; }
      .dash-dropdown .Select-option.is-focused { background-color:#2a2a2a; }
      .dash-dropdown .Select-option.is-selected { background-color:#333333; }
      .dash-dropdown .Select-arrow { border-top-color:#e0e0e0 !important; }
    </style>
    <script>
      document.addEventListener('DOMContentLoaded', function () {
        const ids = ['sum-chart','indv2-chart','osc-chart','ma-chart'];
        function wireWheelPassThrough(id){
          const el = document.getElementById(id);
          if(!el) return;
          if(el.__wheelPassThrough) return;
          el.__wheelPassThrough = true;
          el.addEventListener('wheel', function(e){
            if (!e.ctrlKey && !e.metaKey) {
              e.preventDefault();
              window.scrollBy({top: e.deltaY, left: 0, behavior: 'auto'});
            }
          }, {passive: false, capture: true});
        }
        ids.forEach(wireWheelPassThrough);
        new MutationObserver(function(){ ids.forEach(wireWheelPassThrough); })
          .observe(document.body, {childList: true, subtree: true});
      });
    </script>
  </head>
  <body>
    {%app_entry%}
    <footer>
      {%config%}
      {%scripts%}
      {%renderer%}
    </footer>
  </body>
</html>
"""

# Initial list of symbols (for dropdown), res will be reloaded in callbacks
DF0 = load_df_prepared()
SYMBOLS = sorted(DF0["symbol"].unique()) if not DF0.empty else []

app.layout = html.Div([
    html.Div([html.H2("TV-TA Dashboard", style={"color": FG_LIGHT, "margin": "0"})], className="card"),
    html.Div([
        html.Div([
            html.Label("Symbol", style={"color": FG_LIGHT}),
            dcc.Dropdown(options=[{"label": s, "value": s} for s in SYMBOLS], value=(SYMBOLS[0] if SYMBOLS else None), id="sym-dd", className="dash-dropdown")
        ], style={"width": "300px", "display": "inline-block", "verticalAlign": "top", "marginRight": "20px"}),
        html.Div([
            html.Label("Timeframe", style={"color": FG_LIGHT}),
            dcc.Dropdown(options=[
                {"label": "1D", "value": "1D"},
                {"label": "3D", "value": "3D"},
                {"label": "1W", "value": "1W"},
                {"label": "1M", "value": "1M"},
                {"label": "ALL", "value": "ALL"},
            ], value="1W", id="tf-dd", className="dash-dropdown")
        ], style={"width": "200px", "display": "inline-block", "verticalAlign": "top"}),
        html.Div([
            html.Label("Pivots", style={"color": FG_LIGHT}),
            dcc.Checklist(
                options=[{"label": "Show pivots", "value": "on"}],
                value=["on"],
                id="pivots-toggle",
                inputStyle={"margin-right": "8px"},
                labelStyle={"display": "inline-block", "color": FG_LIGHT}
            )
        ], style={"width": "200px", "display": "inline-block", "verticalAlign": "top", "marginLeft": "20px"})
    ], className="card"),

    html.Div([dcc.Graph(id="main-chart", config={"scrollZoom": True, "displayModeBar": True, "modeBarButtonsToAdd": ["zoom2d","pan2d","autoscale2d","resetScale2d"]})], className="card"),
    html.Div([dcc.Graph(id="sum-chart", config={"scrollZoom": False, "displayModeBar": True, "modeBarButtonsToRemove": ["zoom2d","select2d","lasso2d","zoomIn2d","zoomOut2d","autoScale2d","resetScale2d","pan2d"]})], className="card"),
    html.Div([dcc.Graph(id="indv2-chart", config={"scrollZoom": False, "displayModeBar": True, "modeBarButtonsToRemove": ["zoom2d","select2d","lasso2d","zoomIn2d","zoomOut2d","autoScale2d","resetScale2d","pan2d"]})], className="card"),
    html.Div([dcc.Graph(id="osc-chart", config={"scrollZoom": False, "displayModeBar": True, "modeBarButtonsToRemove": ["zoom2d","select2d","lasso2d","zoomIn2d","zoomOut2d","autoScale2d","resetScale2d","pan2d"]})], className="card"),
    html.Div([dcc.Graph(id="ma-chart", config={"scrollZoom": False, "displayModeBar": True, "modeBarButtonsToRemove": ["zoom2d","select2d","lasso2d","zoomIn2d","zoomOut2d","autoScale2d","resetScale2d","pan2d"]})], className="card"),

    dcc.Interval(id="refresh", interval=REFRESH_MS, n_intervals=0),
    dcc.Store(id="xrange-store"),
], className="app-container")


def filter_by_time(df_sym: pd.DataFrame, tf: str) -> pd.DataFrame:
    if df_sym.empty:
        return df_sym
    end = df_sym["bucket"].max()
    if tf == "ALL":
        start = df_sym["bucket"].min()
    else:
        delta = {"1D": timedelta(days=1), "3D": timedelta(days=3), "1W": timedelta(weeks=1), "1M": timedelta(days=30)}[tf]
        start = end - delta
    return df_sym[(df_sym["bucket"] >= start) & (df_sym["bucket"] <= end)]


# --- helpers ---

def compute_initial_xrange(df_sym: pd.DataFrame):
    """Zwraca (x0, x1) z pół-bucket paddingiem, żeby wszystkie wykresy startowały idealnie wyrównane.
    Jeśli df pusty → None.
    """
    if df_sym is None or df_sym.empty:
        return None
    start = pd.to_datetime(df_sym["bucket"].min())
    end = pd.to_datetime(df_sym["bucket"].max())
    pad = pd.Timedelta(minutes=INTERVAL_MINUTES) / 2
    return (start - pad), (end + pad)
def _extract_xrange(relayout):
    if not relayout:
        return None
    if "xaxis.range[0]" in relayout and "xaxis.range[1]" in relayout:
        return relayout["xaxis.range[0]"], relayout["xaxis.range[1]"]
    if "xaxis.range" in relayout and isinstance(relayout["xaxis.range"], (list, tuple)) and len(relayout["xaxis.range"]) == 2:
        return relayout["xaxis.range"][0], relayout["xaxis.range"][1]
    if relayout.get("xaxis.autorange"):
        return None
    return None


# --- callbacks ---
@app.callback(
    Output("main-chart", "figure"),
    Input("sym-dd", "value"),
    Input("tf-dd", "value"),
    Input("pivots-toggle", "value"),
    Input("refresh", "n_intervals"),
    Input("xrange-store", "data"),
)
def update_main(sym: str, tf: str, pivots_value, _n: int, xr_store):
    DF_local = load_df_prepared()
    if not sym or DF_local.empty:
        empty = pd.DataFrame({"bucket": [], "price_open": [], "price_high": [], "price_low": [], "price_close": []})
        show_pivots = bool(pivots_value) and ("on" in pivots_value)
        return make_main_figure(empty, "No data", show_pivots=show_pivots)
    df_sym = DF_local[DF_local["symbol"] == sym].copy()
    df_sym = filter_by_time(df_sym, tf)
    title = f"{sym} — 15m (deduped per bucket)"
    show_pivots = bool(pivots_value) and ("on" in pivots_value)
    fig = make_main_figure(df_sym, title, show_pivots=show_pivots)
    # Jeśli NIE ma zapamiętanego zoomu, ustaw spójny startowy zakres z pół-bucket paddingiem (lepsze wyrównanie z subami)
    if not (isinstance(xr_store, dict) and "x0" in xr_store and "x1" in xr_store):
        xr = compute_initial_xrange(df_sym)
        if xr:
            x0, x1 = xr
            fig.update_layout(xaxis=dict(range=[x0, x1]))
    return fig


@app.callback(
    Output("xrange-store", "data"),
    Input("main-chart", "relayoutData"),
    prevent_initial_call=True,
)
def keep_last_xrange(relayout):
    xr = _extract_xrange(relayout)
    if xr:
        x0, x1 = xr
        return {"x0": x0, "x1": x1}
    return no_update


@app.callback(
    Output("sum-chart", "figure"),
    Output("indv2-chart", "figure"),
    Output("osc-chart", "figure"),
    Output("ma-chart", "figure"),
    Input("sym-dd", "value"),
    Input("tf-dd", "value"),
    Input("main-chart", "relayoutData"),
    Input("xrange-store", "data"),
    Input("refresh", "n_intervals"),
)
def update_sub(sym: str, tf: str, relayout, xr_store, _n: int):
    DF_local = load_df_prepared()
    if not sym or DF_local.empty:
        empty = pd.DataFrame({"bucket": []})
        f1 = make_reco_counts_figure(empty, "sum_reco", "sum_buy", "sum_sell", "sum_neu", "Summary")
        f2 = make_ind_v2_figure(empty)
        f3 = make_reco_counts_figure(empty, "osc_reco", "osc_buy", "osc_sell", "osc_neu", "Oscillators")
        f4 = make_reco_counts_figure(empty, "ma_reco", "ma_buy", "ma_sell", "ma_neu", "Moving Averages")
        return f1, f2, f3, f4

    df_sym = DF_local[DF_local["symbol"] == sym].copy()
    df_sym = filter_by_time(df_sym, tf)
    sum_fig = make_reco_counts_figure(df_sym, "sum_reco", "sum_buy", "sum_sell", "sum_neu", "Summary recommendation & counts")
    indv2_fig = make_ind_v2_figure(df_sym)
    osc_fig = make_reco_counts_figure(df_sym, "osc_reco", "osc_buy", "osc_sell", "osc_neu", "Oscillators recommendation & counts")
    ma_fig = make_reco_counts_figure(df_sym, "ma_reco", "ma_buy", "ma_sell", "ma_neu", "Moving Averages recommendation & counts")

    # Priorytet: zapisany zoom; w innym wypadku autorange (łapie natychmiast nowy bucket)
    xr = _extract_xrange(relayout)
    if xr_store and isinstance(xr_store, dict) and "x0" in xr_store and "x1" in xr_store:
        x0, x1 = xr_store["x0"], xr_store["x1"]
        for fig in (sum_fig, indv2_fig, osc_fig, ma_fig):
            fig.update_layout(xaxis=dict(range=[x0, x1], fixedrange=True))
    elif xr:
        x0, x1 = xr
        for fig in (sum_fig, indv2_fig, osc_fig, ma_fig):
            fig.update_layout(xaxis=dict(range=[x0, x1], fixedrange=True))
    else:
        # Użyj tego samego inicjalnego zakresu co główny (z pół-bucket paddingiem)
        xr = compute_initial_xrange(df_sym)
        if xr:
            x0, x1 = xr
            for fig in (sum_fig, indv2_fig, osc_fig, ma_fig):
                fig.update_layout(xaxis=dict(range=[x0, x1], fixedrange=True))
        else:
            for fig in (sum_fig, indv2_fig, osc_fig, ma_fig):
                fig.update_layout(xaxis=dict(autorange=True, fixedrange=True))

    # Zablokuj osie Y i ewentualne y2 w subach
    for fig in (sum_fig, indv2_fig, osc_fig, ma_fig):
        fig.update_layout(yaxis=dict(fixedrange=True))
        if "yaxis2" in fig.layout:
            fig.update_layout(yaxis2=dict(fixedrange=True))

    return sum_fig, indv2_fig, osc_fig, ma_fig


if __name__ == "__main__":
    app.run_server(debug=True, port='8501')
