"""
app_v2.py — Professional Trading Dashboard
─────────────────────────────────────────────────────────────────────────
CHANGES FROM v1:
  1. Professional dark terminal aesthetic — Bloomberg-inspired layout
  2. AUTO mode default — bot switches ORB→VWAP at 12:00 ET automatically
  3. Real ORB-R + VWAP backtester using Supabase realized_trades data
  4. Removed RSI/MACD single-scan (not aligned with ORB/VWAP strategy)
  5. Strategy status banner shows current session + time-to-next-switch
  6. Cleaner sidebar, metric cards, and tab layout
  7. Open positions table shows stop/target from open_positions table
  8. Bot health monitor (last heartbeat from bot_state)

Run on Streamlit Cloud:
    Secrets required: ALPACA_API_KEY, ALPACA_SECRET_KEY, supabase.url, supabase.key
"""

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

# ─────────────────────────────────────────────
# HTML TABLE HELPER — bypasses st.dataframe iframe rendering
# ─────────────────────────────────────────────
def render_html_table(df: pd.DataFrame, pl_col: str = "pl_usd") -> None:
    """
    Renders a DataFrame as a fully styled HTML table.
    CSS can reach this unlike st.dataframe which renders inside an iframe.
    Green/red colouring applied to pl_usd column automatically.
    """
    if df.empty:
        st.info("No data.")
        return

    def _cell(val, col):
        style = ""
        if col == pl_col:
            try:
                v = float(str(val).replace("$","").replace("+","").replace(",",""))
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
# PAGE CONFIG — must be first Streamlit call
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="AlgoBot Dashboard",
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

/* ── Base ── */
html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
    background-color: #0a0c10;
    color: #c8cdd6;
}
.main { background-color: #0a0c10; }
.block-container { padding: 1.2rem 2rem 2rem 2rem; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background-color: #0d0f14;
    border-right: 1px solid #1e2330;
}
[data-testid="stSidebar"] * { font-family: 'IBM Plex Mono', monospace; font-size: 12px; }

/* ── Metric cards ── */
[data-testid="stMetric"] {
    background: #0f1219;
    border: 1px solid #1e2330;
    border-radius: 6px;
    padding: 14px 18px;
}
[data-testid="stMetricLabel"] { color: #5a6478 !important; font-size: 11px !important; letter-spacing: 0.08em; text-transform: uppercase; font-family: 'IBM Plex Mono', monospace; }
[data-testid="stMetricValue"] { color: #e2e8f0 !important; font-size: 22px !important; font-family: 'IBM Plex Mono', monospace; font-weight: 600; }
[data-testid="stMetricDelta"] { font-family: 'IBM Plex Mono', monospace; font-size: 12px !important; }

/* ── Tabs ── */
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

/* ── Buttons ── */
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

/* ── Dataframe — force light background so text is visible ── */
[data-testid="stDataFrame"] {
    border: 1px solid #1e2330;
    border-radius: 6px;
}
/* The inner glide-data-grid canvas renders its own colours — don't override */
[data-testid="stDataFrame"] > div { background: #131720 !important; }
/* Fallback for non-canvas cells */
.dvn-scroller { background: #131720 !important; }
[data-testid="stDataFrame"] iframe { background: #131720 !important; }

/* ── Expander — ensure content area has visible background ── */
[data-testid="stExpander"] {
    background: #0d0f14;
    border: 1px solid #1e2330;
    border-radius: 6px;
}
[data-testid="stExpander"] summary {
    color: #8899bb;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
}
/* Content inside expander — must not inherit #0a0c10 which hides text */
[data-testid="stExpander"] > div > div {
    background: #0d0f14 !important;
    color: #c8cdd6 !important;
}
/* Tables inside expanders */
[data-testid="stExpander"] [data-testid="stDataFrame"] > div {
    background: #131720 !important;
}
[data-testid="stExpander"] [data-testid="stDataFrame"] iframe {
    background: #131720 !important;
    color-scheme: dark;
}

/* ── Select / Input ── */
[data-baseweb="select"] > div { background: #0d0f14 !important; border-color: #1e2330 !important; }
[data-baseweb="input"] > div { background: #0d0f14 !important; border-color: #1e2330 !important; }
[data-baseweb="input"] input { color: #c8cdd6 !important; font-family: 'IBM Plex Mono', monospace !important; }

/* ── Status cards ── */
.status-card {
    background: #0d0f14;
    border: 1px solid #1e2330;
    border-radius: 6px;
    padding: 16px 20px;
    font-family: 'IBM Plex Mono', monospace;
}
.status-card.orb { border-left: 3px solid #f59e0b; }
.status-card.vwap { border-left: 3px solid #3b82f6; }
.status-card.auto { border-left: 3px solid #4ade80; }
.status-card.closed { border-left: 3px solid #ef4444; }

.label { font-size: 10px; letter-spacing: 0.1em; color: #5a6478; text-transform: uppercase; margin-bottom: 4px; }
.value { font-size: 20px; font-weight: 600; color: #e2e8f0; }
.sub   { font-size: 11px; color: #5a6478; margin-top: 4px; }

/* ── Goal bar ── */
.goal-bar-bg {
    background: #1e2330;
    border-radius: 3px;
    height: 6px;
    width: 100%;
    margin-top: 8px;
}
.goal-bar-fill {
    height: 6px;
    border-radius: 3px;
    background: linear-gradient(90deg, #4ade80, #22d3ee);
    transition: width 0.5s ease;
}

/* ── Divider ── */
hr { border-color: #1e2330; margin: 1rem 0; }

/* ── Section headers ── */
.section-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: #5a6478;
    border-bottom: 1px solid #1e2330;
    padding-bottom: 8px;
    margin-bottom: 16px;
}

/* ── P&L colors ── */
.pnl-pos { color: #4ade80; font-family: 'IBM Plex Mono', monospace; }
.pnl-neg { color: #f87171; font-family: 'IBM Plex Mono', monospace; }
.mono    { font-family: 'IBM Plex Mono', monospace; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
SGT = pytz.timezone("Asia/Singapore")
ET  = pytz.timezone("US/Eastern")
WEEKLY_TARGET      = 200.0
EFFECTIVE_CAPITAL  = 12000.0
ORB_END_HOUR       = 12   # AUTO switches ORB → VWAP at 12:00 ET
VWAP_END_HOUR      = 15
VWAP_END_MINUTE    = 30

WATCHLIST = [
    "NVDA","AMD","AVGO","QCOM","AMAT","ASML",
    "ADBE","CRM","NOW","CRWD","PANW","SNOW",
    "AAPL","MSFT","GOOGL","AMZN",
    "TSLA","PLTR","SHOP","NKE",
]

# ─────────────────────────────────────────────
# CLIENTS
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
# KEEPALIVE
# ─────────────────────────────────────────────
components.html("""
<div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#5a6478;
    background:#0d0f14;border:1px solid #1e2330;border-radius:4px;
    padding:4px 12px;display:inline-flex;align-items:center;gap:8px;">
    <span style="color:#4ade80;font-size:9px;">●</span>
    KEEPALIVE — next ping: <strong id="cd" style="color:#4f8ef7;">5:00</strong>
    <span id="ps" style="color:#4ade80;font-size:11px;"></span>
</div>
<script>
var r=300;
setInterval(function(){
    r--;
    if(r<=0){
        try{fetch(window.location.href,{mode:'no-cors',cache:'no-store'});}catch(e){}
        document.getElementById('ps').textContent='✓ pinged';
        setTimeout(()=>document.getElementById('ps').textContent='',3000);
        r=300;
    }
    var m=Math.floor(r/60),s=r%60;
    document.getElementById('cd').textContent=m+':'+(s<10?'0':'')+s;
    document.getElementById('cd').style.color=r<=60?'#f87171':r<=120?'#f59e0b':'#4f8ef7';
},1000);
</script>""", height=32)

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
if "trades_cache"          not in st.session_state: st.session_state.trades_cache = None
if "liq_auth"              not in st.session_state: st.session_state.liq_auth = False
if "override_auth"         not in st.session_state: st.session_state.override_auth = False
if "baseline_auth"         not in st.session_state: st.session_state.baseline_auth = False
if "liq_selected_symbol"   not in st.session_state: st.session_state.liq_selected_symbol = None

# ─────────────────────────────────────────────
# DATA LOADERS
# ─────────────────────────────────────────────
@st.cache_data(ttl=30)
def load_trades() -> pd.DataFrame:
    try:
        r = supabase.table("realized_trades").select("*").order("date", desc=True).execute()
        if r.data:
            df = pd.DataFrame(r.data)
            df["pl_usd"] = pd.to_numeric(df.get("pl_usd", 0), errors="coerce").fillna(0)
            df["date"]   = pd.to_datetime(df["date"]).dt.date
            return df
    except Exception as e:
        st.warning(f"Trade load error: {e}")
    return pd.DataFrame()

@st.cache_data(ttl=15)
def load_account():
    try:
        acct = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return acct, positions
    except Exception as e:
        st.warning(f"Account error: {e}")
        return None, []

@st.cache_data(ttl=15)
def load_open_positions_meta() -> dict:
    try:
        r = supabase.table("open_positions").select("*").execute()
        return {row["symbol"]: row for row in r.data} if r.data else {}
    except:
        return {}

@st.cache_data(ttl=20)
def load_weekly_baseline() -> float:
    try:
        r = supabase.table("weekly_baseline").select("baseline").order("date", desc=True).limit(1).execute()
        if r.data:
            return float(r.data[0]["baseline"])
    except:
        pass
    return 0.0

@st.cache_data(ttl=20)
def load_bot_heartbeat():
    try:
        r = supabase.table("bot_state").select("last_heartbeat").eq("id", 1).execute()
        if r.data:
            return r.data[0]["last_heartbeat"]
    except:
        pass
    return None

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
        st.error(f"Strategy update failed: {e}")

def verify_pin(entered: str) -> bool:
    try:
        r = supabase.table("bot_config").select("pin").eq("id", 1).execute()
        return bool(r.data) and r.data[0]["pin"] == entered
    except:
        return False

# ─────────────────────────────────────────────
# SESSION / STRATEGY LOGIC
# ─────────────────────────────────────────────
def get_auto_session(now_et: datetime) -> str:
    """
    AUTO mode schedule (ET):
      09:30 – 12:00  →  ORB-R
      12:00 – 15:30  →  VWAP
      otherwise      →  CLOSED
    Best switch time: 12:00 ET (noon) — ORB momentum fades,
    VWAP reversion setups peak in early-afternoon drift.
    """
    h, m = now_et.hour, now_et.minute
    after_open   = (h == 9 and m >= 30) or h >= 10
    before_close = (h < 15) or (h == 15 and m < 30)
    in_session   = now_et.weekday() < 5 and after_open and before_close

    if not in_session:
        return "CLOSED"
    if h < ORB_END_HOUR:
        return "ORB"
    return "VWAP"

def get_effective_session(forced: str, now_et: datetime) -> tuple:
    """Returns (effective_session, display_mode)."""
    auto = get_auto_session(now_et)
    if forced == "AUTO" or forced not in ("ORB-R", "VWAP"):
        return auto, "AUTO"
    if forced == "ORB-R":
        return "ORB", "MANUAL"
    return "VWAP", "MANUAL"

def time_to_next_switch(now_et: datetime) -> str:
    """How long until the next ORB→VWAP or VWAP→close switch."""
    h, m = now_et.hour, now_et.minute
    if now_et.weekday() >= 5:
        return "Weekend"
    if h < 9 or (h == 9 and m < 30):
        target = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    elif h < ORB_END_HOUR:
        target = now_et.replace(hour=ORB_END_HOUR, minute=0, second=0, microsecond=0)
    elif (h < VWAP_END_HOUR) or (h == VWAP_END_HOUR and m < VWAP_END_MINUTE):
        target = now_et.replace(hour=VWAP_END_HOUR, minute=VWAP_END_MINUTE, second=0, microsecond=0)
    else:
        return "Market closed"
    delta = target - now_et
    total_s = int(delta.total_seconds())
    return f"{total_s // 3600:02d}h {(total_s % 3600) // 60:02d}m"

# ─────────────────────────────────────────────
# TRADE SESSION HELPERS
# ─────────────────────────────────────────────
def get_session_date(ts_sgt_str: str, date_val) -> "date":
    """Map a SGT timestamp to its US trading session date."""
    try:
        t = datetime.strptime(ts_sgt_str, "%H:%M:%S").time()
    except:
        return date_val
    if isinstance(date_val, str):
        date_val = datetime.strptime(date_val, "%Y-%m-%d").date()
    elif hasattr(date_val, "date"):
        date_val = date_val.date()
    # SGT is UTC+8; US market opens at 21:30 SGT
    if t >= datetime.strptime("21:30", "%H:%M").time():
        return date_val
    return date_val - timedelta(days=1)

def annotate_sessions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "time_sgt" not in df.columns:
        return df
    df = df.copy()
    df["session_date"] = df.apply(lambda r: get_session_date(r.get("time_sgt", "12:00:00"), r["date"]), axis=1)
    return df

# ─────────────────────────────────────────────
# BACKTEST ENGINE — real trades data
# ─────────────────────────────────────────────
def run_backtest(strategy: str, df: pd.DataFrame, start_date, end_date) -> dict:
    """
    Compute backtest metrics from realised trades for a given strategy.
    Uses actual Supabase data — no simulation.
    """
    if df.empty:
        return None

    mask = (
        (df["date"] >= start_date) &
        (df["date"] <= end_date)
    )
    if "strategy" in df.columns:
        mask &= df["strategy"].str.upper().str.contains(strategy.upper(), na=False)

    filt = df[mask].copy()
    if filt.empty:
        return {"trades": pd.DataFrame(), "summary": {}}

    filt = filt.sort_values("date")
    filt["cumulative_pl"] = filt["pl_usd"].cumsum()

    wins   = filt[filt["pl_usd"] > 0]
    losses = filt[filt["pl_usd"] <= 0]

    total_trades  = len(filt)
    win_count     = len(wins)
    loss_count    = len(losses)
    win_rate      = win_count / total_trades * 100 if total_trades > 0 else 0
    total_pl      = filt["pl_usd"].sum()
    avg_win       = wins["pl_usd"].mean()    if not wins.empty   else 0
    avg_loss      = losses["pl_usd"].mean()  if not losses.empty else 0
    profit_factor = abs(wins["pl_usd"].sum() / losses["pl_usd"].sum()) if losses["pl_usd"].sum() != 0 else float("inf")

    # Max drawdown on cumulative equity curve
    cumpl  = filt["cumulative_pl"].values
    peak   = np.maximum.accumulate(cumpl)
    dd     = cumpl - peak
    max_dd = dd.min() if len(dd) > 0 else 0

    # Sharpe (weekly returns)
    filt_dated = filt.copy()
    filt_dated["week"] = pd.to_datetime(filt_dated["date"]).dt.isocalendar().week
    weekly_pl = filt_dated.groupby("week")["pl_usd"].sum()
    sharpe = (weekly_pl.mean() / weekly_pl.std() * np.sqrt(52)) if weekly_pl.std() > 0 else 0

    return {
        "trades": filt,
        "summary": {
            "Total Trades":    total_trades,
            "Win Rate":        f"{win_rate:.1f}%",
            "Total P&L":       f"${total_pl:+.2f}",
            "Avg Win":         f"${avg_win:+.2f}",
            "Avg Loss":        f"${avg_loss:+.2f}",
            "Profit Factor":   f"{profit_factor:.2f}x",
            "Max Drawdown":    f"${max_dd:.2f}",
            "Sharpe (weekly)": f"{sharpe:.2f}",
        },
    }

# ─────────────────────────────────────────────
# FETCH LIVE DATA
# ─────────────────────────────────────────────
acct, positions  = load_account()
open_meta        = load_open_positions_meta()
baseline         = load_weekly_baseline()
heartbeat_str    = load_bot_heartbeat()
forced_strategy  = load_forced_strategy()
trades_df        = load_trades()

now_et  = datetime.now(ET)
now_sgt = datetime.now(SGT)

portfolio_value = float(acct.portfolio_value) if acct else 0.0
cash            = float(acct.cash)            if acct else 0.0
buying_power    = float(acct.buying_power)    if acct else 0.0
daily_pl_alpaca = (float(acct.equity) - float(acct.last_equity)) if acct and hasattr(acct, "last_equity") else 0.0
weekly_delta    = portfolio_value - baseline if baseline else 0.0

total_mv   = sum(float(p.market_value) for p in positions)
total_unrl = sum(float(p.unrealized_pl) for p in positions)

eff_session, mode = get_effective_session(forced_strategy, now_et)
switch_in         = time_to_next_switch(now_et)

try:
    market_open = trading_client.get_clock().is_open
except:
    market_open = eff_session != "CLOSED"

# Bot health
bot_alive = False
heartbeat_display = "—"
if heartbeat_str:
    try:
        hb_dt = datetime.fromisoformat(heartbeat_str)
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=pytz.utc)
        hb_sgt = hb_dt.astimezone(SGT)
        age_s  = (now_sgt - hb_sgt).total_seconds()
        bot_alive = age_s < 300   # healthy if heartbeat < 5 min ago
        heartbeat_display = f"{int(age_s // 60)}m {int(age_s % 60)}s ago"
    except:
        pass

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div class="section-header">⚡ ALGOBOT</div>', unsafe_allow_html=True)

    # Bot health
    health_color = "#4ade80" if bot_alive else "#f87171"
    health_label = "LIVE" if bot_alive else "OFFLINE"
    st.markdown(f"""
    <div class="status-card {'auto' if bot_alive else 'closed'}" style="margin-bottom:12px;">
        <div class="label">Bot Status</div>
        <div class="value" style="color:{health_color};font-size:16px;">● {health_label}</div>
        <div class="sub">Last heartbeat: {heartbeat_display}</div>
    </div>""", unsafe_allow_html=True)

    st.markdown('<div class="section-header">Account</div>', unsafe_allow_html=True)
    st.metric("Portfolio",    f"${portfolio_value:,.2f}")
    st.metric("Cash",         f"${cash:,.2f}")
    st.metric("Buying Power", f"${buying_power:,.2f}")
    st.metric("Today P&L",    f"${daily_pl_alpaca:+.2f}",
              delta=f"{'▲' if daily_pl_alpaca >= 0 else '▼'} Alpaca")

    # ── Weekly baseline comparison card ────────────────────────
    st.markdown("---")
    _wk_color = "#4ade80" if weekly_delta >= 0 else "#f87171"
    _wk_arrow = "▲" if weekly_delta >= 0 else "▼"
    _wk_pct   = (weekly_delta / baseline * 100) if baseline and baseline != 0 else 0
    _wk_prog  = min(max(weekly_delta / WEEKLY_TARGET, 0), 1) * 100 if WEEKLY_TARGET > 0 else 0
    st.markdown(
        f"""
        <div style="background:#0f1219;border:1px solid #1e2330;border-left:3px solid {_wk_color};
            border-radius:6px;padding:12px 14px;font-family:'IBM Plex Mono',monospace;margin-bottom:4px;">
            <div style="font-size:10px;letter-spacing:0.1em;color:#5a6478;
                text-transform:uppercase;margin-bottom:8px;">📅 Weekly Progress</div>
            <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:4px;">
                <div>
                    <div style="font-size:9px;color:#5a6478;margin-bottom:2px;">START OF WEEK</div>
                    <div style="font-size:14px;font-weight:600;color:#8899bb;">${baseline:,.2f}</div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:9px;color:#5a6478;margin-bottom:2px;">NOW</div>
                    <div style="font-size:14px;font-weight:600;color:#e2e8f0;">${portfolio_value:,.2f}</div>
                </div>
            </div>
            <div style="font-size:15px;font-weight:700;color:{_wk_color};margin:6px 0 2px 0;">
                {_wk_arrow} ${weekly_delta:+.2f}
                <span style="font-size:11px;font-weight:400;">({_wk_pct:+.2f}%)</span>
            </div>
            <div style="font-size:9px;color:#5a6478;margin-bottom:4px;">
                Target ${WEEKLY_TARGET:.0f} &nbsp;·&nbsp; {_wk_prog:.0f}% reached
            </div>
            <div style="background:#1e2330;border-radius:3px;height:4px;width:100%;">
                <div style="height:4px;border-radius:3px;width:{_wk_prog:.1f}%;
                    background:linear-gradient(90deg,#4ade80,#22d3ee);"></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown(f'<div class="sub mono">Market: {"🟢 OPEN" if market_open else "🔴 CLOSED"}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sub mono">SGT: {now_sgt.strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sub mono">ET:  {now_et.strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)

    st.markdown("---")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    # Admin: set baseline
    st.markdown('<div class="section-header" style="margin-top:12px;">Admin</div>', unsafe_allow_html=True)
    with st.expander("Set Weekly Baseline"):
        if not st.session_state.baseline_auth:
            pin = st.text_input("PIN", type="password", key="bl_pin")
            if st.button("Unlock", key="bl_unlock"):
                if verify_pin(pin):
                    st.session_state.baseline_auth = True
                    st.rerun()
                else:
                    st.error("Wrong PIN")
        else:
            new_bl = st.number_input("Baseline ($)", value=float(baseline), step=100.0, format="%.2f")
            if st.button("Save Baseline"):
                try:
                    today = now_sgt.date()
                    monday = today - timedelta(days=today.weekday())
                    supabase.table("weekly_baseline").insert({
                        "baseline": new_bl,
                        "date": monday.isoformat(),
                        "updated_at": now_sgt.isoformat(),
                    }).execute()
                    st.success("Saved")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(e)
            if st.button("Lock", key="bl_lock"):
                st.session_state.baseline_auth = False
                st.rerun()

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────
st.markdown('<h1 style="font-family:\'IBM Plex Mono\',monospace;font-size:24px;font-weight:600;color:#e2e8f0;letter-spacing:0.04em;margin-bottom:4px;">⚡ ALGOBOT DASHBOARD</h1>', unsafe_allow_html=True)
st.markdown(f'<div class="sub mono" style="margin-bottom:16px;">Paper trading • {len(WATCHLIST)} stocks • ${EFFECTIVE_CAPITAL:,.0f} capital envelope</div>', unsafe_allow_html=True)

# ── Strategy Status Banner ────────────────────
session_colors = {"ORB": "#f59e0b", "VWAP": "#3b82f6", "CLOSED": "#ef4444"}
session_labels = {"ORB": "ORB-R", "VWAP": "VWAP", "CLOSED": "CLOSED"}
sc = session_colors.get(eff_session, "#5a6478")
sl = session_labels.get(eff_session, eff_session)
mode_badge = f'<span style="background:rgba(74,222,128,0.1);color:#4ade80;font-size:10px;padding:2px 8px;border-radius:3px;border:1px solid #4ade80;margin-left:8px;">AUTO</span>' if mode == "AUTO" else f'<span style="background:rgba(245,158,11,0.1);color:#f59e0b;font-size:10px;padding:2px 8px;border-radius:3px;border:1px solid #f59e0b;margin-left:8px;">MANUAL OVERRIDE</span>'

st.markdown(f"""
<div class="status-card" style="border-left:3px solid {sc};margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;">
    <div>
        <div class="label">Active Strategy {mode_badge}</div>
        <div class="value" style="color:{sc};">{sl}</div>
        <div class="sub">Next switch: {switch_in} &nbsp;|&nbsp; ORB-R 09:30–12:00 ET → VWAP 12:00–15:30 ET</div>
    </div>
    <div style="text-align:right;">
        <div class="label">Week Target</div>
        <div class="value" style="font-size:16px;color:#e2e8f0;">${weekly_delta:+.2f} / ${WEEKLY_TARGET:.0f}</div>
        <div class="goal-bar-bg"><div class="goal-bar-fill" style="width:{min(max(weekly_delta/WEEKLY_TARGET,0),1)*100:.1f}%"></div></div>
    </div>
</div>
""", unsafe_allow_html=True)

# ── Strategy Override ─────────────────────────
with st.expander("🔧 Strategy Override"):
    if not st.session_state.override_auth:
        with st.form("ov_form"):
            ov_pin = st.text_input("PIN", type="password")
            c1, c2 = st.columns(2)
            if c1.form_submit_button("Unlock"):
                if verify_pin(ov_pin):
                    st.session_state.override_auth = True
                    st.rerun()
                else:
                    st.error("Wrong PIN")
            c2.form_submit_button("Cancel")
    else:
        cols = st.columns([2, 1, 1, 1])
        current_display = forced_strategy if forced_strategy in ("AUTO", "ORB-R", "VWAP") else "AUTO"
        choice = cols[0].selectbox("Mode", ["AUTO", "ORB-R", "VWAP"],
                                   index=["AUTO", "ORB-R", "VWAP"].index(current_display))
        if cols[1].button("Apply"):
            set_forced_strategy(choice)
            st.success(f"Set to {choice}")
            st.session_state.override_auth = False
            st.cache_data.clear()
            st.rerun()
        if cols[2].button("Lock"):
            st.session_state.override_auth = False
            st.rerun()
        st.caption("AUTO: bot follows schedule (ORB 9:30–12:00 → VWAP 12:00–15:30 ET). Manual forces a single strategy regardless of time.")

st.markdown("---")

# ── Top Metrics Row ───────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Portfolio",    f"${portfolio_value:,.2f}")
c2.metric("Holdings",     f"${total_mv:,.2f}")
c3.metric("Unrealized",   f"${total_unrl:+.2f}", delta="open P&L")
c4.metric("Weekly Δ",     f"${weekly_delta:+.2f}", delta=f"goal ${WEEKLY_TARGET:.0f}")
c5.metric("Positions",    f"{len(positions)}",   delta=f"max 8")

st.markdown("---")

# ─────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────
tab_live, tab_positions, tab_backtest, tab_liq = st.tabs([
    "📊  Live Trading",
    "📋  Open Positions",
    "🧪  Backtest",
    "🧹  Liquidate",
])

# ══════════════════════════════════════════════
# TAB 1 — LIVE TRADING
# ══════════════════════════════════════════════
with tab_live:
    trades_ann = annotate_sessions(trades_df) if not trades_df.empty else pd.DataFrame()

    # Session selector
    c1, c2 = st.columns([2, 3])
    with c1:
        session_view = st.radio("Session view", ["Current", "Last Completed"], horizontal=True)

    now_sgt_date = now_sgt.date()
    # Current session starts at 21:30 SGT the previous night
    if now_sgt.time() >= datetime.strptime("21:30", "%H:%M").time():
        current_sess = now_sgt_date
    else:
        current_sess = now_sgt_date - timedelta(days=1)
    last_sess = current_sess - timedelta(days=1)
    target_sess = current_sess if session_view == "Current" else last_sess

    with c2:
        st.markdown(f'<div class="sub mono" style="padding-top:8px;">Showing session: <strong style="color:#e2e8f0;">{target_sess}</strong></div>', unsafe_allow_html=True)

    # Filter trades
    if not trades_ann.empty and "session_date" in trades_ann.columns:
        sess_trades = trades_ann[trades_ann["session_date"] == target_sess].copy()
    else:
        sess_trades = pd.DataFrame()

    # Session summary cards
    if not sess_trades.empty and "pl_usd" in sess_trades.columns:
        wins   = sess_trades[sess_trades["pl_usd"] > 0]
        losses = sess_trades[sess_trades["pl_usd"] <= 0]
        s_pl   = sess_trades["pl_usd"].sum()
        st.markdown('<div class="section-header" style="margin-top:8px;">Session Summary</div>', unsafe_allow_html=True)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Session P&L",   f"${s_pl:+.2f}")
        m2.metric("Trades",        len(sess_trades))
        m3.metric("Win / Loss",    f"{len(wins)} / {len(losses)}")
        m4.metric("Win Rate",      f"{len(wins)/len(sess_trades)*100:.0f}%" if len(sess_trades) > 0 else "—")
    else:
        st.info(f"No trades found for session {target_sess}.")

    st.markdown('<div class="section-header" style="margin-top:16px;">Realized Trades</div>', unsafe_allow_html=True)

    if not sess_trades.empty:
        display_cols = [c for c in ["date","symbol","strategy","buy_price","sell_price","qty","pl_usd","pl_display","reason","time_sgt"] if c in sess_trades.columns]
        render_html_table(sess_trades[display_cols], pl_col="pl_usd")

        # Per-strategy breakdown
        if "strategy" in sess_trades.columns:
            st.markdown('<div class="section-header" style="margin-top:16px;">Per-Strategy Breakdown</div>', unsafe_allow_html=True)
            strat_summary = sess_trades.groupby("strategy").agg(
                Trades=("pl_usd", "count"),
                Total_PL=("pl_usd", "sum"),
                Avg_PL=("pl_usd", "mean"),
                Win_Rate=("pl_usd", lambda x: f"{(x > 0).sum() / len(x) * 100:.0f}%")
            ).reset_index()
            strat_summary["Total_PL"] = strat_summary["Total_PL"].apply(lambda x: f"${x:+.2f}")
            strat_summary["Avg_PL"]   = strat_summary["Avg_PL"].apply(lambda x: f"${x:+.2f}")
            render_html_table(strat_summary, pl_col="Total_PL")
    else:
        st.info("No trades for this session.")

    # P&L Charts
    st.markdown('<div class="section-header" style="margin-top:16px;">Historical P&L</div>', unsafe_allow_html=True)
    with st.expander("Daily & Cumulative P&L Charts", expanded=True):
        if not trades_ann.empty and "session_date" in trades_ann.columns:
            daily = trades_ann.groupby("session_date")["pl_usd"].sum().reset_index().sort_values("session_date")
            daily.columns = ["date", "pl"]
            daily["cumpl"] = daily["pl"].cumsum()
            daily["color"] = daily["pl"].apply(lambda x: "#4ade80" if x >= 0 else "#f87171")

            fig_bar = go.Figure(go.Bar(
                x=daily["date"], y=daily["pl"],
                marker_color=daily["color"],
                marker_line_width=0,
            ))
            fig_bar.update_layout(
                title="Daily Session P&L", plot_bgcolor="#0d0f14", paper_bgcolor="#0d0f14",
                font=dict(family="IBM Plex Mono", color="#8899bb", size=11),
                xaxis=dict(gridcolor="#1e2330", color="#5a6478"),
                yaxis=dict(gridcolor="#1e2330", color="#5a6478"),
                margin=dict(l=40, r=20, t=40, b=40),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

            fig_cum = go.Figure(go.Scatter(
                x=daily["date"], y=daily["cumpl"],
                mode="lines+markers",
                line=dict(color="#4ade80", width=2),
                marker=dict(color="#4ade80", size=5),
                fill="tozeroy",
                fillcolor="rgba(74,222,128,0.06)",
            ))
            fig_cum.update_layout(
                title="Cumulative P&L", plot_bgcolor="#0d0f14", paper_bgcolor="#0d0f14",
                font=dict(family="IBM Plex Mono", color="#8899bb", size=11),
                xaxis=dict(gridcolor="#1e2330", color="#5a6478"),
                yaxis=dict(gridcolor="#1e2330", color="#5a6478", tickprefix="$"),
                margin=dict(l=40, r=20, t=40, b=40),
            )
            st.plotly_chart(fig_cum, use_container_width=True)
        else:
            st.info("No historical data yet.")

# ══════════════════════════════════════════════
# TAB 2 — OPEN POSITIONS
# ══════════════════════════════════════════════
with tab_positions:
    st.markdown('<div class="section-header">Open Positions</div>', unsafe_allow_html=True)

    if not positions:
        st.info("No open positions.")
    else:
        rows = []
        for p in positions:
            sym     = p.symbol
            entry   = float(p.avg_entry_price)
            current = float(p.current_price)
            qty     = float(p.qty)
            pl_usd  = float(p.unrealized_pl)
            pl_pct  = (pl_usd / (entry * qty)) * 100 if entry * qty != 0 else 0
            meta    = open_meta.get(sym, {})
            strategy   = meta.get("strategy",    "—")
            stop_price = meta.get("stop_price",  None)
            tgt_price  = meta.get("target_price", None)

            # Risk/reward status
            if stop_price and tgt_price:
                risk   = entry - float(stop_price)
                reward = float(tgt_price) - entry
                rr     = f"{reward/risk:.1f}R" if risk > 0 else "—"
                stop_d = f"${float(stop_price):.2f}"
                tgt_d  = f"${float(tgt_price):.2f}"
            else:
                rr = stop_d = tgt_d = "—"

            rows.append({
                "Symbol":   sym,
                "Strategy": strategy,
                "Entry":    f"${entry:.2f}",
                "Current":  f"${current:.2f}",
                "Stop":     stop_d,
                "Target":   tgt_d,
                "R/R":      rr,
                "Qty":      round(qty, 4),
                "Unrl P&L": f"${pl_usd:+.2f}",
                "Unrl %":   f"{pl_pct:+.2f}%",
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # Unrealized P&L mini chart
        if len(rows) > 0:
            syms = [r["Symbol"] for r in rows]
            pls  = [float(r["Unrl P&L"].replace("$","").replace("+","")) for r in rows]
            colors = ["#4ade80" if x >= 0 else "#f87171" for x in pls]
            fig = go.Figure(go.Bar(x=syms, y=pls, marker_color=colors, marker_line_width=0))
            fig.update_layout(
                title="Unrealized P&L by Position",
                plot_bgcolor="#0d0f14", paper_bgcolor="#0d0f14",
                font=dict(family="IBM Plex Mono", color="#8899bb", size=11),
                xaxis=dict(gridcolor="#1e2330", color="#5a6478"),
                yaxis=dict(gridcolor="#1e2330", color="#5a6478", tickprefix="$"),
                margin=dict(l=40, r=20, t=40, b=30), height=250,
            )
            st.plotly_chart(fig, use_container_width=True)

# ══════════════════════════════════════════════
# TAB 3 — BACKTEST (real data)
# ══════════════════════════════════════════════
with tab_backtest:
    st.markdown('<div class="section-header">Strategy Backtest — Realised Trades</div>', unsafe_allow_html=True)
    st.caption("Uses actual executed trades from Supabase. Shows real win rate, P&L, drawdown and Sharpe.")

    c1, c2, c3 = st.columns([2, 2, 2])
    bt_strategy  = c1.selectbox("Strategy", ["ORB-R", "VWAP", "ALL"])
    bt_start     = c2.date_input("From", datetime.now().date() - timedelta(days=90))
    bt_end       = c3.date_input("To",   datetime.now().date())

    if st.button("▶  Run Backtest", use_container_width=False):
        with st.spinner("Calculating..."):
            strategies_to_run = ["ORB-R", "VWAP"] if bt_strategy == "ALL" else [bt_strategy]
            all_results = {}
            for s in strategies_to_run:
                all_results[s] = run_backtest(s, trades_df, bt_start, bt_end)

        # Summary metrics side-by-side
        st.markdown('<div class="section-header" style="margin-top:12px;">Performance Summary</div>', unsafe_allow_html=True)
        cols = st.columns(len(all_results))
        for i, (strat, res) in enumerate(all_results.items()):
            with cols[i]:
                st.markdown(f'<div class="section-header">{strat}</div>', unsafe_allow_html=True)
                if res and res.get("summary"):
                    for k, v in res["summary"].items():
                        color = ""
                        if "$" in str(v):
                            try:
                                val = float(str(v).replace("$","").replace("+","").replace(",",""))
                                color = "color:#4ade80;" if val >= 0 else "color:#f87171;"
                            except:
                                pass
                        st.markdown(f'<div style="font-family:IBM Plex Mono,monospace;font-size:12px;padding:4px 0;border-bottom:1px solid #1e2330;display:flex;justify-content:space-between;"><span style="color:#5a6478;">{k}</span><span style="{color}font-weight:600;">{v}</span></div>', unsafe_allow_html=True)
                else:
                    st.info(f"No {strat} trades in date range.")

        # Equity curves
        st.markdown('<div class="section-header" style="margin-top:16px;">Equity Curves</div>', unsafe_allow_html=True)
        fig_eq = go.Figure()
        line_colors = {"ORB-R": "#f59e0b", "VWAP": "#3b82f6"}
        has_data = False
        for strat, res in all_results.items():
            if res and not res["trades"].empty:
                t = res["trades"].sort_values("date")
                fig_eq.add_trace(go.Scatter(
                    x=t["date"], y=t["cumulative_pl"],
                    mode="lines", name=strat,
                    line=dict(color=line_colors.get(strat, "#8899bb"), width=2),
                ))
                has_data = True
        if has_data:
            fig_eq.update_layout(
                plot_bgcolor="#0d0f14", paper_bgcolor="#0d0f14",
                font=dict(family="IBM Plex Mono", color="#8899bb", size=11),
                xaxis=dict(gridcolor="#1e2330", color="#5a6478"),
                yaxis=dict(gridcolor="#1e2330", color="#5a6478", tickprefix="$"),
                legend=dict(bgcolor="#0d0f14", bordercolor="#1e2330"),
                margin=dict(l=40, r=20, t=20, b=40),
            )
            st.plotly_chart(fig_eq, use_container_width=True)

            # Trade log
            with st.expander("Trade Log"):
                for strat, res in all_results.items():
                    if res and not res["trades"].empty:
                        st.markdown(f"**{strat}**")
                        disp = [c for c in ["date","symbol","buy_price","sell_price","qty","pl_usd","reason"] if c in res["trades"].columns]
                        st.dataframe(res["trades"][disp], use_container_width=True, hide_index=True)
        else:
            st.info("No trades found in this date range for the selected strategy.")
    else:
        st.markdown("""
        <div class="status-card" style="color:#5a6478;font-family:'IBM Plex Mono',monospace;font-size:12px;">
            Select a strategy and date range, then click Run Backtest.<br><br>
            • <strong style="color:#f59e0b;">ORB-R</strong> — Opening Range Breakout with Retest (09:30–12:00 ET)<br>
            • <strong style="color:#3b82f6;">VWAP</strong>  — VWAP Retest (12:00–15:30 ET)<br>
            • <strong style="color:#e2e8f0;">ALL</strong>   — Both strategies side-by-side comparison
        </div>
        """, unsafe_allow_html=True)

# ══════════════════════════════════════════════
# TAB 4 — INDIVIDUAL LIQUIDATION
# ══════════════════════════════════════════════
with tab_liq:
    st.markdown('<div class="section-header">Individual Position Liquidation</div>', unsafe_allow_html=True)
    st.caption("Sell a specific position. Market order during regular hours; limit order (whole shares only) during extended hours.")

    if not st.session_state.liq_auth:
        with st.form("liq_pin_form"):
            liq_pin = st.text_input("PIN", type="password")
            c1, c2 = st.columns(2)
            if c1.form_submit_button("🔓 Unlock"):
                if verify_pin(liq_pin):
                    st.session_state.liq_auth = True
                    st.rerun()
                else:
                    st.error("Wrong PIN")
            c2.form_submit_button("Cancel")
    else:
        st.success("✅ Access granted")

        if not positions:
            st.info("No open positions.")
        else:
            pos_map = {p.symbol: p for p in positions}
            syms    = sorted(pos_map.keys())

            if st.session_state.liq_selected_symbol not in syms:
                st.session_state.liq_selected_symbol = syms[0]

            labels = {}
            for sym in syms:
                p   = pos_map[sym]
                mv  = float(p.market_value)
                upl = float(p.unrealized_pl)
                labels[f"{sym}  |  ${mv:,.2f}  |  {'+' if upl>=0 else ''}{upl:.2f}"] = sym

            sel_label = st.selectbox("Select position", list(labels.keys()),
                                     index=syms.index(st.session_state.liq_selected_symbol))
            sel_sym = labels[sel_label]
            if sel_sym != st.session_state.liq_selected_symbol:
                st.session_state.liq_selected_symbol = sel_sym
                st.rerun()

            p = pos_map[sel_sym]
            entry   = float(p.avg_entry_price)
            current = float(p.current_price)
            qty     = float(p.qty)
            est_pl  = (current - entry) * qty

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Symbol",    sel_sym)
            c2.metric("Qty",       f"{qty:.4f}")
            c3.metric("Current",   f"${current:.2f}")
            c4.metric("Est. P&L",  f"${est_pl:+.2f}")

            now_et_liq    = datetime.now(ET)
            reg_start     = now_et_liq.replace(hour=9, minute=30, second=0, microsecond=0)
            reg_end       = now_et_liq.replace(hour=16, minute=0,  second=0, microsecond=0)
            is_reg_hours  = reg_start <= now_et_liq <= reg_end

            order_type_note = "🟢 Regular hours — market order (fractional shares OK)" if is_reg_hours else "🟡 Extended hours — limit order at 1% below market (whole shares only)"
            st.caption(order_type_note)

            strategy_for_record = st.selectbox("Strategy (for P&L attribution)", ["ORB-R", "VWAP", "MANUAL"])
            confirm = st.checkbox("I confirm I want to liquidate this position")

            if st.button("🔥 LIQUIDATE", type="primary", disabled=not confirm):
                try:
                    sold_qty = qty
                    if is_reg_hours:
                        trading_client.submit_order(MarketOrderRequest(
                            symbol=sel_sym, qty=qty,
                            side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                        ))
                        st.success(f"Market sell order placed — {qty:.4f} {sel_sym}")
                    else:
                        whole = int(qty)
                        if whole == 0:
                            st.error("No whole shares to sell in extended hours.")
                            st.stop()
                        limit_p = round(current * 0.99, 2)
                        trading_client.submit_order(LimitOrderRequest(
                            symbol=sel_sym, qty=whole,
                            side=OrderSide.SELL, limit_price=limit_p,
                            time_in_force=TimeInForce.DAY, extended_hours=True,
                        ))
                        sold_qty = whole
                        st.success(f"Limit sell order placed — {whole} {sel_sym} @ ${limit_p:.2f}")

                    pl_usd = (current - entry) * sold_qty
                    pl_pct = (pl_usd / (entry * sold_qty)) * 100 if entry * sold_qty != 0 else 0

                    supabase.table("realized_trades").insert({
                        "date":      now_sgt.date().isoformat(),
                        "symbol":    sel_sym,
                        "strategy":  strategy_for_record,
                        "buy_price": f"${entry:.2f}",
                        "sell_price": f"${current:.2f}",
                        "qty":       round(sold_qty, 4),
                        "pl_usd":    pl_usd,
                        "pl_display": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
                        "pl_pct":    f"{pl_pct:+.2f}%",
                        "time_sgt":  now_sgt.strftime("%H:%M:%S"),
                        "reason":    "Manual liquidation via dashboard",
                    }).execute()
                    try:
                        supabase.table("open_positions").delete().eq("symbol", sel_sym).execute()
                    except:
                        pass
                    st.session_state.liq_selected_symbol = None
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Liquidation error: {e}")

        if st.button("🔒 Lock", use_container_width=True):
            st.session_state.liq_auth = False
            st.session_state.liq_selected_symbol = None
            st.rerun()

# ─────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────
st.markdown("---")
st.markdown(f'<div class="sub mono" style="text-align:center;">AlgoBot v2 • Paper Trading • Capital ${EFFECTIVE_CAPITAL:,.0f} • Target ${WEEKLY_TARGET:.0f}/week • {now_sgt.strftime("%Y-%m-%d %H:%M SGT")}</div>', unsafe_allow_html=True)
