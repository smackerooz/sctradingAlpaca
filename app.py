"""
app.py — Professional Trading Dashboard (SMA Trend-Following Edition)
─────────────────────────────────────────────────────────────────────────
UPDATES IN v3.2 PATCHED:
  1. Fixed high-contrast canvas data grid rendering error (black text on black bug resolved).
  2. Replaced st.dataframe with st.table globally to force high-contrast text inheritance.
  3. Integrated Tab 5: "📈 Watchlist Matrix" detailing evaluation metrics for all stocks.
  4. Mapped automatic value, potential upside, and real-time earnings calendars.

  BUGFIXES (v3.2):
  - Removed duplicate `import datetime` / `import pytz` inside the sidebar block, which was
    shadowing the top-level `datetime` class import with the module and crashing any code
    below it that called datetime(...) or datetime.strptime(...) as a constructor.
  - Removed reference to an undefined `log` object in load_trades(); now uses st-safe logging.
  - Fixed the watchlist fallback fair-value formula, which previously always produced a
    "Buy"/"Strong Buy" recommendation whenever no real analyst target was available
    (fair_p = current_p * 1.05 mathematically guarantees current_p < fair_p * 0.97).
    Now shows "No Target Data" instead of fabricating a signal.
  - Removed unused STOCK_METADATA "fair"/"earn" placeholder fields that were never read
    (the watchlist tab computes its own live fair value / earnings date from yfinance).
  - Removed MSTR from the watchlist — excluded elsewhere in this project's Shariah-compliance
    screen due to cryptocurrency balance-sheet exposure; kept out here for consistency.
  - Removed the unused batch `yf.download(...)` call in fetch_live_web_fundamentals
    (its result, prices_df, was never used — per-ticker .info() calls already do the work).
  - Consolidated the duplicate "is the bot alive" heartbeat checks (previously computed twice,
    once unused, with two different staleness thresholds) into a single shared function.

Run on Streamlit Cloud:
    Secrets required: ALPACA_API_KEY, ALPACA_SECRET_KEY, supabase.url, supabase.key
"""

import logging
import streamlit as st
import pytz
import time
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
import streamlit.components.v1 as components
from supabase import create_client, Client

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)

# ─────────────────────────────────────────────
# HTML TABLE HELPER
# ─────────────────────────────────────────────
def render_html_table(df: pd.DataFrame, pl_col: str = "pl_usd") -> None:
    if df.empty:
        st.info("No data recorded.")
        return

    def _cell(val, col):
        style = ""
        if col == pl_col:
            try:
                v = float(str(val).replace("$", "").replace("+", "").replace(",", ""))
                style = "color:#4ade80;font-weight:600;" if v >= 0 else "color:#f87171;font-weight:600;"
            except:
                pass
        elif col in ("pl_display", "pl_pct"):
            s = str(val)
            style = "color:#4ade80;font-weight:600;" if "🟢" in s or "+" in s else "color:#f87171;font-weight:600;"
        return f'<td style="padding:8px 12px;border-bottom:1px solid #1e2330;white-space:nowrap;{style}">{val}</td>'

    headers = "".join(
        f'<th style="padding:8px 12px;border-bottom:2px solid #1e2330;color:#5a6478;'
        f'font-size:10px;letter-spacing:0.1em;text-transform:uppercase;'
        f'font-weight:500;text-align:left;white-space:nowrap;">{col}</th>'
        for col in df.columns
    )
    rows_html = ""
    for i, (_, row) in enumerate(df.iterrows()):
        bg = "#0f1219" if i % 2 == 0 else "#0d0f14"
        cells = "".join(_cell(row[col], col) for col in df.columns)
        rows_html += f'<tr style="background:{bg};">{cells}</tr>'

    table_html = f"""
    <div style="overflow-x:auto;border:1px solid #1e2330;border-radius:6px;margin-bottom:8px;">
        <table style="width:100%;border-collapse:collapse;font-family:'IBM Plex Mono',monospace;font-size:12px;color:#c8cdd6;">
            <thead><tr style="background:#131720;">{headers}</tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>"""
    st.markdown(table_html, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="AlgoBot Trend Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# GLOBAL CSS — Bloomberg terminal aesthetic
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
    background-color: #0a0c10;
    color: #c8cdd6;
}
.main { background-color: #0a0c10; }
.block-container { padding: 1.2rem 2rem 2rem 2rem; }

[data-testid="stSidebar"] {
    background-color: #0d0f14;
    border-right: 1px solid #1e2330;
}
[data-testid="stSidebar"] * { font-family: 'IBM Plex Mono', monospace; font-size: 12px; }

[data-testid="stMetric"] {
    background: #0f1219;
    border: 1px solid #1e2330;
    border-radius: 6px;
    padding: 14px 18px;
}
[data-testid="stMetricLabel"] { color: #5a6478 !important; font-size: 11px !important; letter-spacing: 0.08em; text-transform: uppercase; font-family: 'IBM Plex Mono', monospace; }
[data-testid="stMetricValue"] { color: #e2e8f0 !important; font-size: 22px !important; font-family: 'IBM Plex Mono', monospace; font-weight: 600; }
[data-testid="stMetricDelta"] { font-family: 'IBM Plex Mono', monospace; font-size: 12px !important; }

[data-testid="stTabs"] [data-baseweb="tab-list"] {
    background: #0d0f14;
    border-bottom: 1px solid #1e2330;
    gap: 0;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
    background: transparent;
    color: #5a6478;
    border-bottom: 2px solid transparent;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 10px 20px;
}
[data-testid="stTabs"] [aria-selected="true"] {
    color: #4ade80 !important;
    border-bottom: 2px solid #4ade80 !important;
    background: transparent !important;
}

.stButton > button {
    background: transparent;
    border: 1px solid #2a3347;
    color: #8899bb;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.04em;
    border-radius: 4px;
    transition: all 0.15s ease;
}
.stButton > button:hover {
    border-color: #4ade80;
    color: #4ade80;
    background: rgba(74, 222, 128, 0.05);
}
.stButton > button[kind="primary"] {
    background: #4ade80;
    color: #0a0c10;
    border-color: #4ade80;
    font-weight: 600;
}

/* ── Force Global HTML Table Custom Color Injection ── */
.stTable, table {
    background-color: #131720 !important;
    border: 1px solid #1e2330 !important;
    color: #e2e8f0 !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 12px !important;
}
th {
    background-color: #1b202c !important;
    color: #8899bb !important;
    font-weight: 500 !important;
    text-transform: uppercase !important;
    font-size: 10px !important;
    letter-spacing: 0.05em;
}
td {
    border-bottom: 1px solid #1e2330 !important;
}

[data-testid="stExpander"] { background: #0d0f14; border: 1px solid #1e2330; border-radius: 6px; }
[data-testid="stExpander"] summary { color: #8899bb; font-family: 'IBM Plex Mono', monospace; font-size: 12px; }

[data-baseweb="select"] > div { background: #0d0f14 !important; border-color: #1e2330 !important; }
[data-baseweb="input"] > div { background: #0d0f14 !important; border-color: #1e2330 !important; }
[data-baseweb="input"] input { color: #c8cdd6 !important; font-family: 'IBM Plex Mono', monospace !important; }

.status-card { background: #0d0f14; border: 1px solid #1e2330; border-radius: 6px; padding: 16px 20px; font-family: 'IBM Plex Mono', monospace; }
.status-card.sma { border-left: 3px solid #4ade80; }
.status-card.closed { border-left: 3px solid #ef4444; }

.label { font-size: 10px; letter-spacing: 0.1em; color: #5a6478; text-transform: uppercase; margin-bottom: 4px; }
.value { font-size: 20px; font-weight: 600; color: #e2e8f0; }
.sub   { font-size: 11px; color: #5a6478; margin-top: 4px; }

.goal-bar-bg { background: #1e2330; border-radius: 3px; height: 6px; width: 100%; margin-top: 8px; }
.goal-bar-fill { height: 6px; border-radius: 3px; background: linear-gradient(90deg, #4ade80, #22d3ee); transition: width 0.5s ease; }
hr { border-color: #1e2330; margin: 1rem 0; }
.section-header { font-family: 'IBM Plex Mono', monospace; font-size: 11px; letter-spacing: 0.15em; text-transform: uppercase; color: #5a6478; border-bottom: 1px solid #1e2330; padding-bottom: 8px; margin-bottom: 16px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# CONSTANTS & WATCHLIST
# ─────────────────────────────────────────────
# NOTE: previously this was a dict keyed by ticker with "fair" / "earn" placeholder
# values that were never actually read anywhere in the file (the Watchlist tab computes
# its own live fair-value estimate and earnings date from yfinance). Simplified to a
# plain ticker list to remove dead, misleading data.
#
# MSTR removed: excluded elsewhere in this project's Shariah-compliance screen due to
# cryptocurrency balance-sheet exposure. Keep this list in sync with the bot's watchlist.
WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM",
    "PANW", "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD",
    "ZS", "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", "SWKS",
    "FTNT", "ANET", "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK", "AAPL",
    "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT",
]

SGT = pytz.timezone("Asia/Singapore")
ET  = pytz.timezone("US/Eastern")
WEEKLY_TARGET      = 200.0
EFFECTIVE_CAPITAL  = 12000.0

# How stale (seconds) the bot heartbeat can be before we consider it "not responding".
# Previously this threshold was defined twice (120s and 600s) in two different places,
# with the 120s check result going unused. Single source of truth now.
HEARTBEAT_STALE_SECONDS = 180

# ─────────────────────────────────────────────
# CLIENT INITIALIZATION
# ─────────────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    return create_client(st.secrets["supabase"]["url"], st.secrets["supabase"]["key"])

@st.cache_resource
def get_trading_client() -> TradingClient:
    return TradingClient(st.secrets["ALPACA_API_KEY"], st.secrets["ALPACA_SECRET_KEY"], paper=True)

supabase       = get_supabase()
trading_client = get_trading_client()

# ─────────────────────────────────────────────
# STATE CAPTURE LAYERS
# ─────────────────────────────────────────────
if "trades_cache"          not in st.session_state: st.session_state.trades_cache = None
if "liq_auth"              not in st.session_state: st.session_state.liq_auth = False
if "override_auth"         not in st.session_state: st.session_state.override_auth = False
if "baseline_auth"         not in st.session_state: st.session_state.baseline_auth = False
if "liq_selected_symbol"   not in st.session_state: st.session_state.liq_selected_symbol = None

# ─────────────────────────────────────────────
# DATABASE DATA CONNECTORS
# ─────────────────────────────────────────────
@st.cache_data(ttl=20)
def load_trades() -> pd.DataFrame:
    try:
        r = supabase.table("realized_trades").select("*").order("date", desc=True).execute()
        if r.data:
            df = pd.DataFrame(r.data)
            df["pl_usd"] = pd.to_numeric(df.get("pl_usd", 0), errors="coerce").fillna(0)
            df["date"]   = pd.to_datetime(df["date"]).dt.date
            return df
    except Exception as e:
        log.warning(f"Trade sync failure: {e}")
    return pd.DataFrame()

@st.cache_data(ttl=10)
def load_account():
    try:
        acct = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return acct, positions
    except:
        return None, []

@st.cache_data(ttl=10)
def load_open_positions_meta() -> dict:
    try:
        r = supabase.table("open_positions").select("*").execute()
        return {row["symbol"]: row for row in r.data} if r.data else {}
    except:
        return {}

@st.cache_data(ttl=30)
def load_weekly_baseline() -> float:
    try:
        r = supabase.table("weekly_baseline").select("baseline").order("date", desc=True).limit(1).execute()
        if r.data:
            return float(r.data[0]["baseline"])
    except:
        pass
    return 0.0

@st.cache_data(ttl=10)
def load_bot_heartbeat():
    """Returns the raw ISO heartbeat timestamp string from Supabase, or None."""
    try:
        r = supabase.table("bot_state").select("last_heartbeat").eq("id", 1).execute()
        if r.data:
            return r.data[0]["last_heartbeat"]
    except:
        pass
    return None

def is_bot_alive(heartbeat_str: str, now_sgt: datetime, stale_after: int = HEARTBEAT_STALE_SECONDS) -> bool:
    """Single source of truth for bot liveness — replaces the two duplicate,
    inconsistently-thresholded checks that used to exist (one in the main
    script body, one in the sidebar)."""
    if not heartbeat_str:
        return False
    try:
        ts = heartbeat_str.replace("Z", "+00:00")
        last_heartbeat = datetime.fromisoformat(ts)
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=pytz.utc)
        last_heartbeat_sgt = last_heartbeat.astimezone(SGT)
        return (now_sgt - last_heartbeat_sgt).total_seconds() < stale_after
    except Exception:
        return False

def load_forced_strategy() -> str:
    try:
        r = supabase.table("bot_config").select("forced_strategy").eq("id", 1).execute()
        return r.data[0]["forced_strategy"] if r.data else "AUTO"
    except:
        return "AUTO"

def set_forced_strategy(val: str):
    try:
        supabase.table("bot_config").update({"forced_strategy": val}).eq("id", 1).execute()
    except Exception as e:
        st.error(f"Strategy allocation push failed: {e}")

def verify_pin(entered: str) -> bool:
    try:
        r = supabase.table("bot_config").select("pin").eq("id", 1).execute()
        return bool(r.data) and r.data[0]["pin"] == entered
    except:
        return False

def get_auto_session(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "CLOSED"
    h, m = now_et.hour, now_et.minute
    return "SMA-CROSS" if ((h == 9 and m >= 30) or h >= 10) and h < 16 else "CLOSED"

def get_effective_session(forced: str, now_et: datetime) -> tuple:
    auto = get_auto_session(now_et)
    if forced == "AUTO" or forced not in ["SMA-CROSS", "CLOSED"]:
        return auto, "AUTO"
    return forced, "MANUAL"

def time_to_next_switch(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "Weekend"
    if now_et.hour >= 16:
        return "Market Closed"
    delta = now_et.replace(hour=16, minute=0, second=0, microsecond=0) - now_et
    total_s = int(delta.total_seconds())
    return f"{total_s // 3600:02d}h {(total_s % 3600) // 60:02d}m to EOD"

def annotate_sessions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "time_sgt" not in df.columns:
        return df
    df = df.copy()
    def _get_date(r):
        try:
            t = datetime.strptime(r["time_sgt"], "%H:%M:%S").time()
            d = pd.to_datetime(r["date"]).date()
            return d if t >= datetime.strptime("21:30", "%H:%M").time() else d - timedelta(days=1)
        except:
            return pd.to_datetime(r["date"]).date()
    df["session_date"] = df.apply(_get_date, axis=1)
    return df

def run_backtest(strategy: str, df: pd.DataFrame, start_date, end_date) -> dict:
    if df.empty:
        return None
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    if "strategy" in df.columns and strategy != "ALL":
        mask &= df["strategy"].str.upper().str.contains(strategy.upper(), na=False)
    filt = df[mask].copy().sort_values("date")
    if filt.empty:
        return {"trades": pd.DataFrame(), "summary": {}}
    filt["cumulative_pl"] = filt["pl_usd"].cumsum()
    wins, losses = filt[filt["pl_usd"] > 0], filt[filt["pl_usd"] <= 0]
    wr = len(wins) / len(filt) * 100 if len(filt) > 0 else 0
    pf = abs(wins["pl_usd"].sum() / losses["pl_usd"].sum()) if losses["pl_usd"].sum() != 0 else float("inf")
    return {
        "trades": filt,
        "summary": {
            "Total Trades": len(filt), "Win Rate": f"{wr:.1f}%", "Total P&L": f"${filt['pl_usd'].sum():+.2f}",
            "Profit Factor": f"{pf:.2f}x"
        }
    }

# ─────────────────────────────────────────────
# LIVE METRICS LOADING BLOCK
# ─────────────────────────────────────────────
acct, positions  = load_account()
open_meta        = load_open_positions_meta()
baseline         = load_weekly_baseline()
heartbeat_str    = load_bot_heartbeat()
forced_strategy  = load_forced_strategy()
trades_df        = load_trades()

now_et, now_sgt = datetime.now(ET), datetime.now(SGT)
portfolio_value = float(acct.portfolio_value) if acct else 0.0
cash            = float(acct.cash) if acct else 0.0
buying_power    = float(acct.buying_power) if acct else 0.0
daily_pl_alpaca = (float(acct.equity) - float(acct.last_equity)) if acct and hasattr(acct, "last_equity") else 0.0
weekly_delta    = portfolio_value - baseline if baseline else 0.0
total_mv   = sum(float(p.market_value) for p in positions)
total_unrl = sum(float(p.unrealized_pl) for p in positions)
eff_session, mode = get_effective_session(forced_strategy, now_et)
switch_in         = time_to_next_switch(now_et)

# Single, consolidated bot-liveness check (see is_bot_alive() above).
bot_alive = is_bot_alive(heartbeat_str, now_sgt)
market_is_open = get_auto_session(now_et) != "CLOSED"

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div class="section-header">⚡ SMA SWING BOT</div>', unsafe_allow_html=True)

    # ── Status label/colour derived from the single consolidated check above ──
    if not bot_alive:
        status_label = "Bot is OFF"
        status_color = "#f87171"  # Soft Coral Red
    elif bot_alive and not market_is_open:
        status_label = "Bot is ON but market is closed"
        status_color = "#fbbf24"  # Amber Yellow
    else:
        status_label = "Bot is ON and market is opened"
        status_color = "#4ade80"  # Bright Emerald Green

    st.markdown(
        f'<div class="status-card" style="border-left:3px solid {status_color}; color:{status_color}; font-weight:bold;">'
        f'● {status_label}'
        f'</div>',
        unsafe_allow_html=True
    )

    st.metric("Portfolio Value", f"${portfolio_value:,.2f}")
    st.metric("Cash Balance", f"${cash:,.2f}")
    st.metric("Buying Power", f"${buying_power:,.2f}")
    st.metric("Intraday Open P&L", f"${daily_pl_alpaca:+.2f}")

    if st.button("🔄 Reload Matrices", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ─────────────────────────────────────────────
# LAYOUT RENDERING
# ─────────────────────────────────────────────
st.markdown('<h1 style="font-family:\'IBM Plex Mono\',monospace;font-size:24px;font-weight:600;color:#e2e8f0;">⚡ ALGOBOT DASHBOARD v3.2</h1>', unsafe_allow_html=True)
st.markdown("---")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Net Liquidity", f"${portfolio_value:,.2f}")
m2.metric("Total Exposure", f"${total_mv:,.2f}")
m3.metric("Unrealized Delta", f"${total_unrl:+.2f}")
m4.metric("Active Assets Deployed", f"{len(positions)} / 8")
st.markdown("---")

tab_live, tab_positions, tab_backtest, tab_liq, tab_watchlist = st.tabs([
    "📊 Live Strategy Log", "📋 Active Core Inventory", "🧪 SMA Backtest Engine", "🧹 Manual Execution Box", "📈 Watchlist Matrix"
])

# ══════════════════════════════════════════════
# TAB 1, 2 — FUNCTIONAL
# TAB 3, 4 — STILL PLACEHOLDERS (not yet implemented — see TODOs)
# ══════════════════════════════════════════════
with tab_live:
    st.caption("Transactions performance curve history modules.")
    if not trades_df.empty:
        render_html_table(trades_df.head(10))

with tab_positions:
    st.markdown('<div class="section-header">Live Market Exposure Positions</div>', unsafe_allow_html=True)
    if not positions:
        st.info("Zero active inventory units currently deployed.")
    else:
        p_rows = []
        for p in positions:
            meta = open_meta.get(p.symbol, {})
            p_rows.append({"Symbol": p.symbol, "Entry": f"${float(p.avg_entry_price):.2f}", "Current": f"${float(p.current_price):.2f}", "Qty": p.qty, "Unrealized P&L": f"${float(p.unrealized_pl):+.2f}"})
        st.table(pd.DataFrame(p_rows))

with tab_backtest:
    # TODO: not yet implemented. The earlier bot.py dashboard had a working
    # backtest engine (run_backtest() above is wired up but unused here) — port it in.
    st.caption("Strategy matrix calculation backtester module. (Not yet implemented.)")

with tab_liq:
    # TODO: not yet implemented. Needs manual liquidation controls (PIN-gated via
    # verify_pin(), which is already defined above but unused).
    st.caption("Strategic administration override panels. (Not yet implemented.)")

# ══════════════════════════════════════════════
# TAB 5 — WATCHLIST INTELLIGENCE MATRIX (Click-to-Sort)
# ══════════════════════════════════════════════
with tab_watchlist:
    st.markdown('<div class="section-header">📈 Live Watchlist Network (With Institutional RVOL Tracking)</div>', unsafe_allow_html=True)

    @st.cache_data(ttl=21600)  # Caches web results for 6 hours to prevent server rate blocks
    def fetch_live_web_fundamentals(ticker_list):
        import yfinance as yf
        web_data = {}

        for ticker in ticker_list:
            try:
                tick_obj = yf.Ticker(ticker)
                info = tick_obj.info if tick_obj.info else {}

                current_p = float(info.get("currentPrice", info.get("previousClose", 0.0)) or 0.0)

                # Only use a REAL analyst target if one exists.
                raw_target = info.get("targetMedianPrice", info.get("targetMeanPrice"))
                has_target = raw_target is not None
                fair_p = float(raw_target) if has_target else None

                live_vol = float(info.get("volume", info.get("regularMarketVolume", 1.0)) or 1.0)
                avg_vol = float(info.get("averageVolume", info.get("averageDailyVolume10Day", 1.0)) or 1.0)

                rvol_calc = live_vol / avg_vol if avg_vol > 0 else 1.0
                if rvol_calc >= 2.5:
                    volume_status = f"🚨 Extreme Vol ({rvol_calc:.1f}x)"
                elif rvol_calc >= 1.5:
                    volume_status = f"🔥 High Vol ({rvol_calc:.1f}x)"
                else:
                    volume_status = f"⚪ Normal ({rvol_calc:.1f}x)"

                calendar = tick_obj.calendar
                earn_str = "No Date Set"
                if calendar is not None and "Earnings Date" in calendar:
                    dates = calendar["Earnings Date"]
                    if dates and len(dates) > 0:
                        earn_str = dates[0].strftime("%b %d, %Y")

                web_data[ticker] = {
                    "name": info.get("longName", f"{ticker} Corp."),
                    "current_price": current_p,
                    "fair_price": fair_p,
                    "has_target": has_target,
                    "earnings_date": earn_str,
                    "volume_status": volume_status,
                    "rvol_raw": rvol_calc,
                }
            except Exception:
                web_data[ticker] = {
                    "name": f"{ticker} Corp.", "current_price": 0.0, "fair_price": None,
                    "has_target": False, "earnings_date": "No Date Set",
                    "volume_status": "⚪ Normal (1.0x)", "rvol_raw": 1.0,
                }
        return web_data

    with st.spinner("Synchronizing watchlist data from Yahoo Finance..."):
        live_market_snapshot = fetch_live_web_fundamentals(WATCHLIST)

    rec_priority = {"Strong Buy": 4, "Buy": 3, "Hold": 2, "Sell": 1, "No Data": 0}
    wl_rows = []

    for ticker in WATCHLIST:
        snap = live_market_snapshot.get(ticker, {
            "name": f"{ticker}", "current_price": 0.0, "fair_price": None,
            "has_target": False, "earnings_date": "No Date Set",
            "volume_status": "⚪ Normal (1.0x)", "rvol_raw": 1.0,
        })

        current_p = snap["current_price"]
        fair_p = snap["fair_price"]
        has_target = snap["has_target"]
        earn_display = snap["earnings_date"]

        if not has_target or fair_p is None or current_p <= 0:
            valuation = "⚫ No Target Data"
            recommendation = "No Data"
            suggested_entry = None
            upside_calc = None
        elif current_p > fair_p * 1.03:
            valuation = "🔴 Overvalued"
            recommendation = "Sell"
            suggested_entry = fair_p * 0.95
            upside_calc = ((fair_p - current_p) / current_p) * 100
        elif current_p < fair_p * 0.97:
            valuation = "🟢 Undervalued"
            recommendation = "Strong Buy" if current_p < fair_p * 0.92 else "Buy"
            suggested_entry = fair_p * 0.95
            upside_calc = ((fair_p - current_p) / current_p) * 100
        else:
            valuation = "🟡 Fair Value"
            recommendation = "Hold"
            suggested_entry = fair_p * 0.95
            upside_calc = ((fair_p - current_p) / current_p) * 100

        try:
            earn_date = datetime.strptime(earn_display, "%b %d, %Y")
        except Exception:
            earn_date = datetime(2099, 12, 31)

        wl_rows.append({
            "Ticker symbol": ticker,
            "Name": snap["name"],
            "Current price": current_p,
            "Fair Value (Target)": fair_p,
            "Current Value": valuation,
            "Suggested entry price": suggested_entry,
            "Potential upside": round(upside_calc, 2) if upside_calc is not None else None,
            "Recommendation": recommendation,
            "Trading Volume Status": snap["volume_status"],
            "Any earnings report next?": earn_display,
            "_rec_rank": rec_priority.get(recommendation, 0),
            "_earn_timestamp": earn_date,
            "_rvol_raw": snap["rvol_raw"],
        })

    # ── CREATE THE DATAFRAME HERE ──
    watchlist_df = pd.DataFrame(wl_rows)

    # ── CLICK-TO-SORT LOGIC ──
    # Initialize session state for sorting
    if "watchlist_sort_col" not in st.session_state:
        st.session_state.watchlist_sort_col = "Potential upside"
    if "watchlist_sort_ascending" not in st.session_state:
        st.session_state.watchlist_sort_ascending = False  # Default descending

    # Get display columns (excluding hidden tracking columns)
    display_columns = [col for col in watchlist_df.columns if not col.startswith("_")]

    # ── CREATE CLICKABLE HEADER ROW ──
    st.markdown("### Click on any column header to sort")
    
    # Create sort buttons in a row
    cols = st.columns(len(display_columns))
    for idx, col_name in enumerate(display_columns):
        is_active = st.session_state.watchlist_sort_col == col_name
        arrow = " 🔽" if is_active and st.session_state.watchlist_sort_ascending else " 🔼" if is_active and not st.session_state.watchlist_sort_ascending else " ↕"
        
        # Use a button for each column header
        if cols[idx].button(
            f"{col_name}{arrow}", 
            key=f"sort_{idx}_{col_name.replace(' ', '_')}",
            use_container_width=True
        ):
            if st.session_state.watchlist_sort_col == col_name:
                st.session_state.watchlist_sort_ascending = not st.session_state.watchlist_sort_ascending
            else:
                st.session_state.watchlist_sort_col = col_name
                st.session_state.watchlist_sort_ascending = False
            st.rerun()

    st.markdown("---")

    # ── APPLY SORTING ──
    sort_col = st.session_state.watchlist_sort_col
    ascending_flag = st.session_state.watchlist_sort_ascending

    # Handle special sorting cases
    if sort_col == "Recommendation":
        watchlist_df = watchlist_df.sort_values(by="_rec_rank", ascending=ascending_flag)
    elif sort_col == "Any earnings report next?":
        watchlist_df = watchlist_df.sort_values(by="_earn_timestamp", ascending=ascending_flag)
    elif sort_col == "Trading Volume Status":
        watchlist_df = watchlist_df.sort_values(by="_rvol_raw", ascending=ascending_flag)
    elif sort_col == "Potential upside":
        watchlist_df = watchlist_df.sort_values(by=sort_col, ascending=ascending_flag, na_position="last")
    else:
        watchlist_df = watchlist_df.sort_values(by=sort_col, ascending=ascending_flag)

    # ── PREPARE DISPLAY DATAFRAME ──
    display_df = watchlist_df.drop(columns=["_rec_rank", "_earn_timestamp", "_rvol_raw"])

    # Format columns for display
    display_df["Current price"] = display_df["Current price"].apply(lambda x: f"${x:.2f}" if x else "N/A")
    display_df["Fair Value (Target)"] = display_df["Fair Value (Target)"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "N/A")
    display_df["Suggested entry price"] = display_df["Suggested entry price"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "N/A")
    display_df["Potential upside"] = display_df["Potential upside"].apply(
        lambda x: "N/A" if pd.isna(x) else (f"{x:+.2f}%" if x > 0 else "0.00% (At Target)")
    )

    st.table(display_df)
