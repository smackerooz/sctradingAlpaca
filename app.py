"""
app.py — Professional Trading Dashboard (SMA Trend-Following Edition)
─────────────────────────────────────────────────────────────────────────
UPDATES IN v3:
  1. Removed legacy ORB/VWAP intraday schedule tracking banners.
  2. Integrated full tracking matrix supporting your expanded 50-stock watchlist.
  3. Re-wired the native Backtester Engine to query and parse "SMA-CROSS" trade metrics.
  4. Patched strategy override models and liquidation P&L recording arrays.
  5. Fully fixed the HTML table generation loop syntax around line 58.

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

[data-testid="stDataFrame"] { border: 1px solid #1e2330; border-radius: 6px; }
[data-testid="stDataFrame"] > div { background: #131720 !important; }
.dvn-scroller { background: #131720 !important; }

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
# CONSTANTS & CONFIG
# ─────────────────────────────────────────────
SGT = pytz.timezone("Asia/Singapore")
ET  = pytz.timezone("US/Eastern")
WEEKLY_TARGET     = 200.0
EFFECTIVE_CAPITAL  = 12000.0

# Expanded watchlist to fit the core bot criteria
WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", 
    "MSTR", "PANW", "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", 
    "CRWD", "ZS", "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", 
    "SWKS", "FTNT", "ANET", "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK", 
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

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
# DASHBOARD RUNTIME KEEPALIVE
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
    except Exception as e: log.warning(f"Trade sync failure: {e}")
    return pd.DataFrame()

@st.cache_data(ttl=10)
def load_account():
    try:
        acct = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return acct, positions
    except Exception as e:
        st.warning(f"Account interface payload breakdown: {e}")
        return None, []

@st.cache_data(ttl=10)
def load_open_positions_meta() -> dict:
    try:
        r = supabase.table("open_positions").select("*").execute()
        return {row["symbol"]: row for row in r.data} if r.data else {}
    except: return {}

@st.cache_data(ttl=30)
def load_weekly_baseline() -> float:
    try:
        r = supabase.table("weekly_baseline").select("baseline").order("date", desc=True).limit(1).execute()
        if r.data: return float(r.data[0]["baseline"])
    except: pass
    return 0.0

@st.cache_data(ttl=10)
def load_bot_heartbeat():
    try:
        r = supabase.table("bot_state").select("last_heartbeat").eq("id", 1).execute()
        if r.data: return r.data[0]["last_heartbeat"]
    except: pass
    return None

def load_forced_strategy() -> str:
    try:
        r = supabase.table("bot_config").select("forced_strategy").eq("id", 1).execute()
        return r.data[0]["forced_strategy"] if r.data else "AUTO"
    except: return "AUTO"

def set_forced_strategy(val: str):
    try: supabase.table("bot_config").update({"forced_strategy": val}).eq("id", 1).execute()
    except Exception as e: st.error(f"Strategy allocation push failed: {e}")

def verify_pin(entered: str) -> bool:
    try:
        r = supabase.table("bot_config").select("pin").eq("id", 1).execute()
        return bool(r.data) and r.data[0]["pin"] == entered
    except: return False

# ─────────────────────────────────────────────
# STRATEGY LOGIC UPDATED FOR SMA ENGINE
# ─────────────────────────────────────────────
def get_auto_session(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "CLOSED"
    h, m = now_et.hour, now_et.minute
    after_open   = (h == 9 and m >= 30) or h >= 10
    before_close = h < 16
    return "SMA-CROSS" if (after_open and before_close) else "CLOSED"

def get_effective_session(forced: str, now_et: datetime) -> tuple:
    auto = get_auto_session(now_et)
    if forced == "AUTO" or forced not in ["SMA-CROSS", "CLOSED"]:
        return auto, "AUTO"
    return forced, "MANUAL"

def time_to_next_switch(now_et: datetime) -> str:
    if now_et.weekday() >= 5: return "Weekend"
    if now_et.hour >= 16: return "Market Closed"
    target = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = target - now_et
    total_s = int(delta.total_seconds())
    return f"{total_s // 3600:02d}h {(total_s % 3600) // 60:02d}m to EOD"

def get_session_date(ts_sgt_str: str, date_val) -> "date":
    try: t = datetime.strptime(ts_sgt_str, "%H:%M:%S").time()
    except: return date_val
    if isinstance(date_val, str): date_val = datetime.strptime(date_val, "%Y-%m-%d").date()
    elif hasattr(date_val, "date"): date_val = date_val.date()
    if t >= datetime.strptime("21:30", "%H:%M").time(): return date_val
    return date_val - timedelta(days=1)

def annotate_sessions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "time_sgt" not in df.columns: return df
    df = df.copy()
    df["session_date"] = df.apply(lambda r: get_session_date(r.get("time_sgt", "12:00:00"), r["date"]), axis=1)
    return df

# ─────────────────────────────────────────────
# BACKTEST PROCESSING PIPELINE
# ─────────────────────────────────────────────
def run_backtest(strategy: str, df: pd.DataFrame, start_date, end_date) -> dict:
    if df.empty: return None

    mask = ((df["date"] >= start_date) & (df["date"] <= end_date))
    if "strategy" in df.columns and strategy != "ALL":
        mask &= df["strategy"].str.upper().str.contains(strategy.upper(), na=False)

    filt = df[mask].copy()
    if filt.empty: return {"trades": pd.DataFrame(), "summary": {}}

    filt = filt.sort_values("date")
    filt["cumulative_pl"] = filt["pl_usd"].cumsum()

    wins, losses = filt[filt["pl_usd"] > 0], filt[filt["pl_usd"] <= 0]
    total_trades = len(filt)
    win_count = len(wins)
    win_rate = win_count / total_trades * 100 if total_trades > 0 else 0
    total_pl = filt["pl_usd"].sum()
    avg_win  = wins["pl_usd"].mean() if not wins.empty else 0
    avg_loss = losses["pl_usd"].mean() if not losses.empty else 0
    profit_factor = abs(wins["pl_usd"].sum() / losses["pl_usd"].sum()) if losses["pl_usd"].sum() != 0 else float("inf")

    cumpl = filt["cumulative_pl"].values
    max_dd = (cumpl - np.maximum.accumulate(cumpl)).min() if len(cumpl) > 0 else 0

    filt_dated = filt.copy()
    filt_dated["week"] = pd.to_datetime(filt_dated["date"]).dt.isocalendar().week
    weekly_pl = filt_dated.groupby("week")["pl_usd"].sum()
    sharpe = (weekly_pl.mean() / weekly_pl.std() * np.sqrt(52)) if weekly_pl.std() > 0 else 0

    return {
        "trades": filt,
        "summary": {
            "Total Trades": total_trades, "Win Rate": f"{win_rate:.1f}%", "Total P&L": f"${total_pl:+.2f}",
            "Avg Win": f"${avg_win:+.2f}", "Avg Loss": f"${avg_loss:+.2f}", "Profit Factor": f"{profit_factor:.2f}x",
            "Max Drawdown": f"${max_dd:.2f}", "Sharpe (weekly)": f"{sharpe:.2f}"
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
market_open       = eff_session != "CLOSED"

bot_alive = False
heartbeat_display = "—"
if heartbeat_str:
    try:
        hb_dt = datetime.fromisoformat(heartbeat_str).replace(tzinfo=pytz.utc)
        age_s = (now_sgt - hb_dt.astimezone(SGT)).total_seconds()
        bot_alive = age_s < 120  
        heartbeat_display = f"{int(age_s // 60)}m {int(age_s % 60)}s ago"
    except: pass

# ─────────────────────────────────────────────
# SIDEBAR DASHBOARD DISPLAY MODULE
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div class="section-header">⚡ SMA SWING BOT</div>', unsafe_allow_html=True)
    
    health_color = "#4ade80" if bot_alive else "#f87171"
    st.markdown(f"""
    <div class="status-card {'sma' if bot_alive else 'closed'}" style="margin-bottom:12px;">
        <div class="label">System Core State</div>
        <div class="value" style="color:{health_color};font-size:16px;">● { "ACTIVE" if bot_alive else "OFFLINE" }</div>
        <div class="sub">Heartbeat delta: {heartbeat_display}</div>
    </div>""", unsafe_allow_html=True)

    st.markdown('<div class="section-header">Account Matrix</div>', unsafe_allow_html=True)
    st.metric("Portfolio Value", f"${portfolio_value:,.2f}")
    st.metric("Cash Balance", f"${cash:,.2f}")
    st.metric("Buying Power", f"${buying_power:,.2f}")
    st.metric("Intraday Open P&L", f"${daily_pl_alpaca:+.2f}", delta="Live Alpaca Profile")

    st.markdown("---")
    _wk_color = "#4ade80" if weekly_delta >= 0 else "#f87171"
    _wk_arrow = "▲" if weekly_delta >= 0 else "▼"
    _wk_pct   = (weekly_delta / baseline * 100) if baseline and baseline != 0 else 0
    _wk_prog  = min(max(weekly_delta / WEEKLY_TARGET, 0), 1) * 100 if WEEKLY_TARGET > 0 else 0
    st.markdown(f"""
        <div style="background:#0f1219;border:1px solid #1e2330;border-left:3px solid {_wk_color};border-radius:6px;padding:12px 14px;font-family:'IBM Plex Mono',monospace;">
            <div style="font-size:10px;letter-spacing:0.1em;color:#5a6478;text-transform:uppercase;margin-bottom:8px;">📅 Weekly Objective Tracking</div>
            <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:4px;">
                <div><div style="font-size:9px;color:#5a6478;margin-bottom:2px;">START BASE</div><div style="font-size:13px;font-weight:600;color:#8899bb;">${baseline:,.2f}</div></div>
                <div style="text-align:right;"><div style="font-size:9px;color:#5a6478;margin-bottom:2px;">CURRENT</div><div style="font-size:13px;font-weight:600;color:#e2e8f0;">${portfolio_value:,.2f}</div></div>
            </div>
            <div style="font-size:15px;font-weight:700;color:{_wk_color};margin:6px 0 2px 0;">{_wk_arrow} ${weekly_delta:+.2f} <span style="font-size:11px;font-weight:400;">({_wk_pct:+.2f}%)</span></div>
            <div style="font-size:9px;color:#5a6478;margin-bottom:4px;">Target ${WEEKLY_TARGET:.0f} &nbsp;·&nbsp; {_wk_prog:.0f}% reached</div>
            <div style="background:#1e2330;border-radius:3px;height:4px;width:100%;"><div style="height:4px;border-radius:3px;width:{_wk_prog:.1f}%;background:linear-gradient(90deg,#4ade80,#22d3ee);"></div></div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(f'<div class="sub mono">Market: {"🟢 TRADING HOURS" if market_open else "🔴 EX-MARKET HOURS"}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sub mono">SGT Local: {now_sgt.strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sub mono">EST Floor: {now_et.strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)

    st.markdown("---")
    if st.button("🔄 Reload Matrices", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown('<div class="section-header" style="margin-top:12px;">Admin Engine</div>', unsafe_allow_html=True)
    with st.expander("Calibrate Baseline"):
        if not st.session_state.baseline_auth:
            pin = st.text_input("PIN Code Override", type="password", key="bl_pin")
            if st.button("Unlock Admin Mode", key="bl_unlock"):
                if verify_pin(pin): st.session_state.baseline_auth = True; st.rerun()
                else: st.error("Verification Denied")
        else:
            new_bl = st.number_input("Adjust Base ($)", value=float(baseline), step=100.0, format="%.2f")
            if st.button("Commit Core Baseline"):
                try:
                    monday = now_sgt.date() - timedelta(days=now_sgt.date().weekday())
                    supabase.table("weekly_baseline").insert({"baseline": new_bl, "date": monday.isoformat(), "updated_at": now_sgt.isoformat()}).execute()
                    st.success("Baseline Synchronized")
                    st.cache_data.clear(); st.rerun()
                except Exception as e: st.error(e)
            if st.button("Lock Calibration Block"): st.session_state.baseline_auth = False; st.rerun()

# ─────────────────────────────────────────────
# CORE CONTAINER LAYOUTS
# ─────────────────────────────────────────────
st.markdown('<h1 style="font-family:\'IBM Plex Mono\',monospace;font-size:24px;font-weight:600;color:#e2e8f0;letter-spacing:0.04em;margin-bottom:4px;">⚡ ALGOBOT DASHBOARD v3</h1>', unsafe_allow_html=True)
st.markdown(f'<div class="sub mono" style="margin-bottom:16px;">Trend Verification Platform • Daily SMA Cross Systems • {len(WATCHLIST)} Targets Tracked • Capital Frame: ${EFFECTIVE_CAPITAL:,.0f}</div>', unsafe_allow_html=True)

# ── Dynamic Status Banner ─────────────────────
sc = "#4ade80" if eff_session == "SMA-CROSS" else "#ef4444"
mode_badge = f'<span style="background:rgba(74,222,128,0.1);color:#4ade80;font-size:10px;padding:2px 8px;border-radius:3px;border:1px solid #4ade80;margin-left:8px;">AUTO</span>' if mode == "AUTO" else f'<span style="background:rgba(245,158,11,0.1);color:#f59e0b;font-size:10px;padding:2px 8px;border-radius:3px;border:1px solid #f59e0b;margin-left:8px;">OVERRIDE ACTIVE</span>'

st.markdown(f"""
<div class="status-card" style="border-left:3px solid {sc};margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;">
    <div>
        <div class="label">Operational Tracking Matrix {mode_badge}</div>
        <div class="value" style="color:{sc};">{"SMA TREND INTERFACE" if eff_session=="SMA-CROSS" else "CLOSED"}</div>
        <div class="sub">Time-frame remaining: {switch_in} &nbsp;|&nbsp; Runs fully unified calculations continuously across live market hours.</div>
    </div>
    <div style="text-align:right; width: 300px;">
        <div class="label">Objective P&L Target Tracker</div>
        <div class="value" style="font-size:16px;color:#e2e8f0;">${weekly_delta:+.2f} / ${WEEKLY_TARGET:.0f}</div>
        <div class="goal-bar-bg"><div class="goal-bar-fill" style="width:{min(max(weekly_delta/WEEKLY_TARGET,0),1)*100:.1f}%"></div></div>
    </div>
</div>""", unsafe_allow_html=True)

# ── Manual Configuration Override Layer ───────
with st.expander("🔧 Strategic Core Override"):
    if not st.session_state.override_auth:
        with st.form("ov_form"):
            ov_pin = st.text_input("Enter Credentials PIN", type="password")
            if st.form_submit_button("Authenticate Override Channels"):
                if verify_pin(ov_pin): st.session_state.override_auth = True; st.rerun()
                else: st.error("Invalid Configuration Credentials")
    else:
        cols = st.columns([2, 1, 1, 1])
        choice = cols[0].selectbox("Force Strategy Core State", ["AUTO", "SMA-CROSS", "CLOSED"], index=["AUTO", "SMA-CROSS", "CLOSED"].index(forced_strategy if forced_strategy in ["AUTO", "SMA-CROSS", "CLOSED"] else "AUTO"))
        if cols[1].button("Commit State Update"):
            set_forced_strategy(choice)
            st.success(f"System State Altered to {choice}")
            st.session_state.override_auth = False
            st.cache_data.clear(); st.rerun()
        if cols[2].button("Lock Interface Module"): st.session_state.override_auth = False; st.rerun()

st.markdown("---")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Net Liquidity", f"${portfolio_value:,.2f}")
m2.metric("Total Exposure", f"${total_mv:,.2f}")
m3.metric("Unrealized Delta", f"${total_unrl:+.2f}", delta="Active Open P&L")
m4.metric("Weekly Variance", f"${weekly_delta:+.2f}", delta=f"Delta to Target")
m5.metric("Active Configurations", f"{len(positions)}", delta="Limit Bound 8")
st.markdown("---")

# ─────────────────────────────────────────────
# TAB MANAGEMENT DESIGN STRATEGY
# ─────────────────────────────────────────────
tab_live, tab_positions, tab_backtest, tab_liq = st.tabs([
    "📊  Live Strategy Log", "📋  Active Core Inventory", "🧪  SMA Backtest Engine", "🧹  Manual Execution Box"
])

# ══════════════════════════════════════════════
# TAB 1 — LIVE ANALYTICAL LOG
# ══════════════════════════════════════════════
with tab_live:
    trades_ann = annotate_sessions(trades_df) if not trades_df.empty else pd.DataFrame()
    c1, c2 = st.columns([2, 3])
    session_view = c1.radio("Active Data Segment", ["Current Session", "Historical Log Pass"], horizontal=True)
    
    target_sess = now_sgt.date() if now_sgt.time() >= datetime.strptime("21:30", "%H:%M").time() else now_sgt.date() - timedelta(days=1)
    if session_view != "Current Session": target_sess = target_sess - timedelta(days=1)
    
    c2.markdown(f'<div class="sub mono" style="padding-top:8px;">Isolating Session Context Block: <strong style="color:#e2e8f0;">{target_sess}</strong></div>', unsafe_allow_html=True)

    sess_trades = trades_ann[trades_ann["session_date"] == target_sess].copy() if not trades_ann.empty and "session_date" in trades_ann.columns else pd.DataFrame()

    if not sess_trades.empty and "pl_usd" in sess_trades.columns:
        wins, losses = sess_trades[sess_trades["pl_usd"] > 0], sess_trades[sess_trades["pl_usd"] <= 0]
        st.markdown('<div class="section-header" style="margin-top:8px;">Session Aggregations</div>', unsafe_allow_html=True)
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Realised Session P&L", f"${sess_trades['pl_usd'].sum():+.2f}")
        sm2.metric("Orders Filled", len(sess_trades))
        sm3.metric("Distribution (W/L)", f"{len(wins)} / {len(losses)}")
        sm4.metric("Win Metrics Percentage", f"{len(wins)/len(sess_trades)*100:.0f}%")
    else:
        st.info(f"No transactions recorded under the tracking session window: {target_sess}.")

    st.markdown('<div class="section-header" style="margin-top:16px;">Realised Execution Registers</div>', unsafe_allow_html=True)
    if not sess_trades.empty:
        display_cols = [c for c in ["date", "symbol", "strategy", "buy_price", "sell_price", "qty", "pl_usd", "pl_display", "reason", "time_sgt"] if c in sess_trades.columns]
        render_html_table(sess_trades[display_cols], pl_col="pl_usd")
    
    st.markdown('<div class="section-header" style="margin-top:16px;">Historical Performance Charts</div>', unsafe_allow_html=True)
    with st.expander("Display Equity Curves and Variance Graphs", expanded=True):
        if not trades_ann.empty and "session_date" in trades_ann.columns:
            daily = trades_ann.groupby("session_date")["pl_usd"].sum().reset_index().sort_values("session_date")
            # 1. Rename the columns first
            daily.columns = ["date", "pl"]
            # 2. Now calculate the cumulative sum using the newly assigned "pl" label
            daily["cumpl"] = daily["pl"].cumsum()
            
            st.plotly_chart(go.Figure(go.Bar(x=daily["date"], y=daily["pl"], marker_color=daily["pl"].apply(lambda x: "#4ade80" if x >= 0 else "#f87171"), marker_line_width=0)).update_layout(title="Daily Segment Realized P&L", plot_bgcolor="#0d0f14", paper_bgcolor="#0d0f14", font=dict(family="IBM Plex Mono", color="#8899bb", size=11), xaxis=dict(gridcolor="#1e2330", color="#5a6478"), yaxis=dict(gridcolor="#1e2330", color="#5a6478")), use_container_width=True)
            st.plotly_chart(go.Figure(go.Scatter(x=daily["date"], y=daily["cumpl"], mode="lines+markers", line=dict(color="#4ade80", width=2), fill="tozeroy", fillcolor="rgba(74,222,128,0.06)")).update_layout(title="Total Cumulative Strategy Yield Curve", plot_bgcolor="#0d0f14", paper_bgcolor="#0d0f14", font=dict(family="IBM Plex Mono", color="#8899bb", size=11), xaxis=dict(gridcolor="#1e2330", color="#5a6478"), yaxis=dict(gridcolor="#1e2330", color="#5a6478", tickprefix="$")), use_container_width=True)
        else: st.info("Insufficient performance timeline matrix to project curves.")

# ══════════════════════════════════════════════
# TAB 2 — ACTIVE INVENTORY METRICS
# ══════════════════════════════════════════════
with tab_positions:
    st.markdown('<div class="section-header">Live Market Exposure Positions</div>', unsafe_allow_html=True)
    if not positions: st.info("Zero inventory units currently deployed.")
    else:
        rows = []
        for p in positions:
            sym, entry, current, qty, pl_usd = p.symbol, float(p.avg_entry_price), float(p.current_price), float(p.qty), float(p.unrealized_pl)
            meta = open_meta.get(sym, {})
            stop_p, tgt_p = meta.get("stop_price", None), meta.get("target_price", None)
            
            # Clean up calculation formatting to avoid silent canvas rendering crashes
            if entry * qty != 0:
                pct_calc = (pl_usd / (entry * qty)) * 100
                pct_str = f"{pct_calc:+.2f}%"
            else:
                pct_str = "0.00%"

            rows.append({
                "Symbol": sym, 
                "Strategy Frame": meta.get("strategy", "—"), 
                "Execution Entry": f"${entry:.2f}",
                "Current Price": f"${current:.2f}", 
                "Stop Bound": f"${float(stop_p):.2f}" if stop_p else "—",
                "Target Objective": f"${float(tgt_p):.2f}" if tgt_p else "—", 
                "Quantity": round(qty, 4),
                "Unrealized P&L": f"${pl_usd:+.2f}", 
                "Unrealized %": pct_str
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════
# TAB 3 — PRODUCTION BACKTEST MODULE
# ══════════════════════════════════════════════
with tab_backtest:
    st.markdown('<div class="section-header">Analytical Strategy Performance Backtester</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns([2, 2, 2])
    bt_strategy = c1.selectbox("Filter Strategy Vector Target", ["SMA-CROSS", "ALL"])
    bt_start, bt_end = c2.date_input("Start Window", datetime.now().date() - timedelta(days=90)), c3.date_input("End Window", datetime.now().date())

    if st.button("▶  Execute Performance Evaluation", use_container_width=True):
        with st.spinner("Processing Matrix Yield Architectures..."):
            strats = ["SMA-CROSS"] if bt_strategy == "ALL" else [bt_strategy]
            res_blocks = {s: run_backtest(s, trades_df, bt_start, bt_end) for s in strats}
            
            st.markdown('<div class="section-header" style="margin-top:12px;">Evaluation Metric Breakdown</div>', unsafe_allow_html=True)
            for strat, res in res_blocks.items():
                if res and res.get("summary"):
                    st.markdown(f"**Strategy Matrix Block: {strat}**")
                    for k, v in res["summary"].items():
                        style = "color:#4ade80;" if "$" in str(v) and "-" not in str(v) else "color:#f87171;" if "$" in str(v) and "-" in str(v) else ""
                        st.markdown(f'<div style="font-family:IBM Plex Mono;font-size:12px;padding:4px 0;border-bottom:1px solid #1e2330;display:flex;justify-content:space-between;"><span style="color:#5a6478;">{k}</span><span style="{style}font-weight:600;">{v}</span></div>', unsafe_allow_html=True)

# ══════════════════════════════════════════════
# TAB 4 — EMERGENCY LIQUIDATION MODULATION
# ══════════════════════════════════════════════
with tab_liq:
    st.markdown('<div class="section-header">Individual Position Liquidation Core</div>', unsafe_allow_html=True)
    if not st.session_state.liq_auth:
        with st.form("liq_form"):
            l_pin = st.text_input("Enter Execution Credentials PIN", type="password")
            if st.form_submit_button("Authorize Core Fire Controls"):
                if verify_pin(l_pin): st.session_state.liq_auth = True; st.rerun()
                else: st.error("Verification Failure")
    else:
        st.success("Manual Override Channel Decoupled")
        if not positions: st.info("Zero inventory units available for dynamic closeout processing.")
        else:
            pos_map = {p.symbol: p for p in positions}
            labels = {f"{sym} | Val: ${float(pos_map[sym].market_value):,.2f} | Unrl P&L: {float(pos_map[sym].unrealized_pl):+.2f}": sym for sym in sorted(pos_map.keys())}
            
            sel_label = st.selectbox("Isolate Liquidation Asset", list(labels.keys()))
            sel_sym = labels[sel_label]
            
            p = pos_map[sel_sym]
            entry, current, qty = float(p.avg_entry_price), float(p.current_price), float(p.qty)
            
            lc1, lc2, lc3 = st.columns(3)
            lc1.metric("Selected Symbol Asset", sel_sym)
            lc2.metric("Inventory Volume Units", f"{qty:.4f}")
            lc3.metric("Projected Closed Variance", f"${((current - entry) * qty):+.2f}")
            
            is_reg = datetime.now(ET).replace(hour=9, minute=30) <= datetime.now(ET) <= datetime.now(ET).replace(hour=16, minute=0)
            st.caption("🟢 Live Routing Active" if is_reg else "🟡 Ex-Market Window Layer Engaged")
            
            strategy_for_record = st.selectbox("Strategy Allocation Tag", ["SMA-CROSS", "MANUAL"])
            confirm = st.checkbox("Confirm full strategic structural liquidation parameters deployment.")
            
            if st.button("🔥 LIQUIDATE SPECIFIC ASSET POSITION", type="primary", disabled=not confirm):
                try:
                    if is_reg:
                        trading_client.submit_order(MarketOrderRequest(symbol=sel_sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                    else:
                        trading_client.submit_order(LimitOrderRequest(symbol=sel_sym, qty=int(qty), side=OrderSide.SELL, limit_price=round(current * 0.99, 2), time_in_force=TimeInForce.DAY, extended_hours=True))
                    
                    supabase.table("realized_trades").insert({
                        "date": now_sgt.date().isoformat(), "symbol": sel_sym, "strategy": strategy_for_record,
                        "buy_price": f"${entry:.2f}", "sell_price": f"${current:.2f}", "qty": round(qty, 4),
                        "pl_usd": (current - entry) * qty, "pl_display": f"{'🟢' if (current - entry) >= 0 else '🔴'} ${((current - entry) * qty):+.2f}",
                        "pl_pct": f"{(((current - entry) / entry) * 100):+.2f}%", "time_sgt": now_sgt.strftime("%H:%M:%S"), "reason": "Dashboard Liquidation Override"
                    }).execute()
                    
                    try: supabase.table("open_positions").delete().eq("symbol", sel_sym).execute()
                    except: pass
                    st.success(f"Closeout pipeline executed for {sel_sym}"); st.cache_data.clear(); st.rerun()
                except Exception as e: st.error(f"Execution Error: {e}")

st.markdown("---")
st.markdown(f'<div class="sub mono" style="text-align:center;">AlgoBot Framework v3 • Production Trend UI Envelopes • Base Envelope Capital Limit ${EFFECTIVE_CAPITAL:,.0f} • {now_sgt.strftime("%Y-%m-%d %H:%M SGT")}</div>', unsafe_allow_html=True)
