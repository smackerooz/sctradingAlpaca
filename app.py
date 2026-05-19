"""
Dynamic Dashboard – -Works with any strategy defined in Supabase 'strategies' table
- Toggle between Last Completed Session and Current Session for trades
- Portfolio Backtest: select strategy from dropdown
- Manual override, liquidation, daily P&L charts, signal scanner
"""

import streamlit as st
import pytz
import time
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Trading Bot", page_icon="📈", layout="wide")

# ─────────────────────────────────────────────
# KEEPALIVE (prevents Streamlit Cloud sleep)
# ─────────────────────────────────────────────
import streamlit.components.v1 as components
import os
from supabase import create_client, Client

components.html(
    """
    <div style="
        font-family: monospace;
        font-size: 12px;
        color: #aaa;
        background: #1a1a2e;
        border: 1px solid #333;
        border-radius: 6px;
        padding: 5px 12px;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
    ">
        <span style="color:#26a65b; font-size:10px;">●</span>
        <span>Keepalive ping in: <strong id="countdown" style="color:#4f8ef7;">5:00</strong></span>
        <span id="ping_status" style="color:#aaa; font-size:11px;"></span>
    </div>
    <script>
    var totalSeconds = 300;
    var remaining = totalSeconds;

    function updateCountdown() {
        var mins = Math.floor(remaining / 60);
        var secs = remaining % 60;
        document.getElementById('countdown').textContent =
            mins + ':' + (secs < 10 ? '0' : '') + secs;

        if (remaining <= 10) {
            document.getElementById('countdown').style.color = '#e74c3c';
        } else if (remaining <= 60) {
            document.getElementById('countdown').style.color = '#f0a500';
        } else {
            document.getElementById('countdown').style.color = '#4f8ef7';
        }

        if (remaining <= 0) {
            try {
                fetch(window.location.href, {mode: 'no-cors', cache: 'no-store'});
            } catch(e) {}
            document.getElementById('ping_status').textContent = '✅ Pinged!';
            setTimeout(function() {
                document.getElementById('ping_status').textContent = '';
            }, 3000);
            remaining = totalSeconds;
        } else {
            remaining--;
        }
    }

    updateCountdown();
    setInterval(updateCountdown, 1000);
    </script>
    """,
    height=40,
)

# ─────────────────────────────────────────────
# INITIALIZE CLIENTS
# ─────────────────────────────────────────────
try:
    API_KEY = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
except Exception as e:
    st.error(f"Missing or invalid Alpaca API Keys: {e}")
    st.stop()

@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase = get_supabase()
SGT = pytz.timezone('Asia/Singapore')
ET = pytz.timezone('US/Eastern')

# ─────────────────────────────────────────────
# CONSTANTS & WATCHLIST (50 stocks)
# ─────────────────────────────────────────────
TARGET_PROFIT = 200.0
CASH_BUFFER = 95000.0
SCAN_INTERVAL = 10
MAX_TRADE_USD = 750.0

WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", "MSTR", "PANW",
    "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD", "ZS",
    "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", "SWKS", "FTNT", "ANET",
    "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

STOCK_PROFILES = {
    "NVDA": (0.018, 0.010, 0.008), "AMD": (0.015, 0.009, 0.007),
    "AVGO": (0.013, 0.008, 0.006), "QCOM": (0.013, 0.008, 0.006),
    "AMAT": (0.013, 0.008, 0.006), "ASML": (0.013, 0.008, 0.006),
    "MU": (0.015, 0.009, 0.007), "KLAC": (0.013, 0.008, 0.006),
    "SMCI": (0.020, 0.012, 0.009), "ARM": (0.018, 0.010, 0.008),
    "MSTR": (0.022, 0.014, 0.010), "PANW": (0.012, 0.007, 0.005),
    "TSM": (0.013, 0.008, 0.006), "LRCX": (0.013, 0.008, 0.006),
    "ON": (0.015, 0.009, 0.007), "MPWR": (0.013, 0.008, 0.006),
    "MRVL": (0.013, 0.008, 0.006), "NXPI": (0.013, 0.008, 0.006),
    "TEAM": (0.018, 0.010, 0.008), "INTA": (0.018, 0.010, 0.008),
    "CRWD": (0.018, 0.010, 0.008), "ZS": (0.018, 0.010, 0.008),
    "ADBE": (0.013, 0.008, 0.006), "WDAY": (0.013, 0.008, 0.006),
    "SNPS": (0.013, 0.008, 0.006), "NOW": (0.013, 0.008, 0.006),
    "SHOP": (0.018, 0.010, 0.008), "TXN": (0.012, 0.007, 0.005),
    "CDNS": (0.013, 0.008, 0.006), "MCHP": (0.013, 0.008, 0.006),
    "SWKS": (0.013, 0.008, 0.006), "FTNT": (0.015, 0.009, 0.007),
    "ANET": (0.013, 0.008, 0.006), "UBER": (0.015, 0.009, 0.007),
    "DASH": (0.018, 0.010, 0.008), "TSLA": (0.020, 0.012, 0.009),
    "ISRG": (0.015, 0.009, 0.007), "VRTX": (0.013, 0.008, 0.006),
    "LLY": (0.013, 0.008, 0.006), "MRK": (0.012, 0.007, 0.005),
    "AAPL": (0.012, 0.007, 0.005), "JNJ": (0.010, 0.006, 0.004),
    "PEP": (0.010, 0.006, 0.004), "LIN": (0.012, 0.007, 0.005),
    "REGN": (0.013, 0.008, 0.006), "INTC": (0.013, 0.008, 0.006),
    "PG": (0.010, 0.006, 0.004), "NKE": (0.015, 0.009, 0.007),
    "ADSK": (0.013, 0.008, 0.006), "MDT": (0.012, 0.007, 0.005),
}

# ─────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────
if "nightly_baseline" not in st.session_state:
    try:
        row = supabase.table("weekly_baseline").select("*").eq("id", 1).execute()
        if row.data:
            bl = row.data[0]
            saved_date = datetime.fromisoformat(bl["date"]).date()
            today = datetime.now(SGT).date()
            last_monday = today - timedelta(days=today.weekday())
            if saved_date >= last_monday:
                st.session_state.nightly_baseline = float(bl["baseline"])
            else:
                raise ValueError
        else:
            raise ValueError
    except:
        try:
            st.session_state.nightly_baseline = float(trading_client.get_account().last_equity)
        except:
            st.session_state.nightly_baseline = 10000.0

if "strategies" not in st.session_state:
    rows = supabase.table("strategies").select("*").eq("is_active", True).order("order_index").execute()
    st.session_state.strategies = rows.data

if "bot_running" not in st.session_state: st.session_state.bot_running = True
if "last_scan" not in st.session_state: st.session_state.last_scan = None
if "scan_log" not in st.session_state: st.session_state.scan_log = []
if "peak_prices" not in st.session_state: st.session_state.peak_prices = {}
if "signal_results" not in st.session_state: st.session_state.signal_results = None
if "live_signal_time" not in st.session_state: st.session_state.live_signal_time = None
if "realized_trades" not in st.session_state: st.session_state.realized_trades = []
if "forced_strategy" not in st.session_state: st.session_state.forced_strategy = "AUTO"
if "trade_display_mode" not in st.session_state:
    st.session_state.trade_display_mode = "Last Completed"

# Override & liquidation session states
if "override_step" not in st.session_state: st.session_state.override_step = "idle"
if "override_authorized" not in st.session_state: st.session_state.override_authorized = False
if "pending_strategy" not in st.session_state: st.session_state.pending_strategy = None
if "liq_step" not in st.session_state: st.session_state.liq_step = "idle"
if "pin_verified" not in st.session_state: st.session_state.pin_verified = False

# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
def parse_datetime(date_str: str, time_str: str) -> datetime:
    if "-" in date_str:
        dt_str = f"{date_str} {time_str}"
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    else:
        day, month, year = date_str.split("/")
        dt_str = f"{year}-{month.zfill(2)}-{day.zfill(2)} {time_str}"
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")

def get_trading_session_start(date_str: str, time_str: str) -> str:
    dt = parse_datetime(date_str, time_str)
    dt = SGT.localize(dt)
    if dt.hour >= 21 and dt.minute >= 30:
        return dt.date().isoformat()
    elif dt.hour < 4:
        return (dt.date() - timedelta(days=1)).isoformat()
    else:
        return dt.date().isoformat()

def get_last_completed_session() -> str:
    """Return the most recent COMPLETED trading session start date (always yesterday)."""
    return (datetime.now(SGT).date() - timedelta(days=1)).isoformat()

def get_current_session_start() -> str:
    """Return the start date of the current ongoing trading session (9:30pm SGT → 4:00am SGT next day)."""
    now = datetime.now(SGT)
    if now.hour >= 21 and now.minute >= 30:
        return now.date().isoformat()
    elif now.hour < 4:
        return (now.date() - timedelta(days=1)).isoformat()
    else:
        # Off-hours (4am–9:30pm): no current session, return today as fallback
        return now.date().isoformat()

def load_realized_trades(session_date: str = None) -> list:
    """Load trades for a specific trading session date. If None, use last completed session."""
    try:
        rows = supabase.table("realized_trades") \
                   .select("*") \
                   .order("id", desc=True) \
                   .limit(500) \
                   .execute()
        target_session = session_date if session_date else get_last_completed_session()
        result = []
        for r in rows.data:
            trade_session = get_trading_session_start(r["date"], r["time_sgt"])
            if trade_session == target_session:
                result.append({
                    "date": r["date"], "Symbol": r["symbol"], "Strategy": r.get("strategy", "Unknown"),
                    "Buy Price": r["buy_price"], "Sell Price": r["sell_price"], "Qty": r["qty"],
                    "P&L ($)": r["pl_display"], "P&L (%)": r["pl_pct"], "Time (SGT)": r["time_sgt"],
                    "Reason": r["reason"], "_pl_usd": float(r["pl_usd"]),
                })
        return result
    except Exception:
        return []

def load_all_trades() -> list:
    try:
        rows = supabase.table("realized_trades").select("*").order("id", desc=True).execute()
        result = []
        for r in rows.data:
            result.append({
                "date": r["date"], "Symbol": r["symbol"], "Strategy": r.get("strategy", "Unknown"),
                "Buy Price": r["buy_price"], "Sell Price": r["sell_price"], "Qty": r["qty"],
                "P&L ($)": r["pl_display"], "P&L (%)": r["pl_pct"], "Time (SGT)": r["time_sgt"],
                "Reason": r["reason"], "_pl_usd": float(r["pl_usd"]),
            })
        return result
    except Exception:
        return []

def compute_daily_pnl_overview() -> pd.DataFrame:
    all_trades = load_all_trades()
    if not all_trades:
        return pd.DataFrame()
    session_data = {}
    for trade in all_trades:
        session_start = get_trading_session_start(trade["date"], trade["Time (SGT)"])
        pl = trade["_pl_usd"]
        strategy = trade.get("Strategy", "Unknown")
        if session_start not in session_data:
            session_data[session_start] = {}
        if strategy not in session_data[session_start]:
            session_data[session_start][strategy] = 0.0
        session_data[session_start][strategy] += pl
    rows = []
    for session_start, strategies in session_data.items():
        row = {"Trading Session Date": session_start}
        total = 0.0
        for strat, pl_val in strategies.items():
            row[strat] = round(pl_val, 2)
            total += pl_val
        row["Total"] = round(total, 2)
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Trading Session Date", ascending=True)
    return df

def get_current_strategy_display():
    forced = st.session_state.forced_strategy
    if forced != "AUTO":
        for s in st.session_state.strategies:
            if s["name"] == forced:
                return f"🔧 FORCED: {s['display_name']}", f"📈 {s['reward_risk_ratio']}:1 risk‑reward | {s['description']}"
        return f"🔧 FORCED: {forced}", "No description available"
    else:
        now_et = datetime.now(ET)
        for s in st.session_state.strategies:
            start = datetime.strptime(s["time_window_start_et"], "%H:%M:%S").time()
            end = datetime.strptime(s["time_window_end_et"], "%H:%M:%S").time()
            if start <= now_et.time() <= end:
                return f"🤖 AUTO: {s['display_name']} ({start}–{end} ET)", f"📈 {s['reward_risk_ratio']}:1 risk‑reward | {s['description']}"
        return "⏸️ Market Closed", "No active strategy at this time."

def set_forced_strategy(strategy):
    try:
        supabase.table("bot_config").upsert({
            "id": 1, "forced_strategy": strategy, "updated_at": datetime.now(SGT).isoformat(),
        }).execute()
        st.session_state.forced_strategy = strategy
        st.success(f"Strategy override set to {strategy}")
    except Exception as e:
        st.error(f"Failed to set override: {e}")

def log(msg: str):
    ts = datetime.now(SGT).strftime("%H:%M:%S")
    st.session_state.scan_log.insert(0, f"[{ts}] {msg}")
    st.session_state.scan_log = st.session_state.scan_log[:100]

def get_bars(symbol: str) -> pd.DataFrame:
    try:
        end = datetime.now(pytz.utc)
        start = end - timedelta(days=2)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start, end=end, feed="iex",
        )
        bars = data_client.get_stock_bars(req).df
        if bars.empty:
            return pd.DataFrame()
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")
        bars.index = pd.to_datetime(bars.index, utc=True)
        return bars[["close"]].copy()
    except Exception:
        return pd.DataFrame()

def is_eod_window() -> bool:
    now = datetime.now(SGT)
    return now.weekday() == 4 and now.hour == 3 and 45 <= now.minute < 55

def save_baseline(value: float):
    try:
        supabase.table("weekly_baseline").upsert({
            "id": 1, "baseline": value, "date": datetime.now(SGT).date().isoformat(),
        }).execute()
    except Exception:
        pass

def reset_baseline_if_needed():
    now = datetime.now(SGT)
    if now.weekday() == 0 and now.hour == 21 and now.minute == 30:
        new_bl = float(trading_client.get_account().equity)
        st.session_state.nightly_baseline = new_bl
        save_baseline(new_bl)
        log("Weekly baseline reset — Monday 21:30 SGT")

def profile(symbol: str):
    return STOCK_PROFILES.get(symbol, (0.013, 0.008, 0.006))

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1, min_periods=period).mean()
    avg_l = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_g / avg_l.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def calc_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def compute_signal_score(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 30:
        return {"score": 50, "direction": "⚪ Neutral", "rsi": None, "macd_hist": None,
                "trend_score": 0, "rsi_score": 0, "macd_score": 0, "momentum_score": 0}
    close = df["close"] if "close" in df.columns else df["Close"]
    close = close.dropna()
    if len(close) < 30:
        return {"score": 50, "direction": "⚪ Neutral", "rsi": None, "macd_hist": None,
                "trend_score": 0, "rsi_score": 0, "macd_score": 0, "momentum_score": 0}
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(min(50, len(close))).mean().iloc[-1]
    price = close.iloc[-1]
    trend_score = 12
    if price > sma20 > sma50: trend_score = 25
    elif price > sma20: trend_score = 19
    elif price < sma20 < sma50: trend_score = 0
    elif price < sma20: trend_score = 6
    rsi_series = calc_rsi(close)
    rsi_val = rsi_series.iloc[-1] if not rsi_series.empty else 50.0
    if pd.isna(rsi_val): rsi_val = 50.0
    if rsi_val < 30: rsi_score = 22
    elif rsi_val < 45: rsi_score = 18
    elif rsi_val < 55: rsi_score = 12
    elif rsi_val < 70: rsi_score = 17
    else: rsi_score = 8
    macd_line, signal_line, histogram = calc_macd(close)
    hist_val = histogram.iloc[-1] if not histogram.empty else 0.0
    macd_val = macd_line.iloc[-1] if not macd_line.empty else 0.0
    sig_val = signal_line.iloc[-1] if not signal_line.empty else 0.0
    if pd.isna(hist_val): hist_val = 0.0
    if pd.isna(macd_val): macd_val = 0.0
    if pd.isna(sig_val): sig_val = 0.0
    if macd_val > sig_val and hist_val > 0:
        prev_hist = histogram.iloc[-2] if len(histogram) > 1 else hist_val
        macd_score = 25 if (not pd.isna(prev_hist) and hist_val > prev_hist) else 20
    elif macd_val > sig_val: macd_score = 15
    elif macd_val < sig_val and hist_val < 0:
        prev_hist = histogram.iloc[-2] if len(histogram) > 1 else hist_val
        macd_score = 0 if (not pd.isna(prev_hist) and hist_val < prev_hist) else 5
    else: macd_score = 10
    if len(close) >= 6:
        mom_pct = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6]
    else: mom_pct = 0.0
    if pd.isna(mom_pct): mom_pct = 0.0
    if mom_pct > 0.03: momentum_score = 25
    elif mom_pct > 0.01: momentum_score = 20
    elif mom_pct > 0: momentum_score = 15
    elif mom_pct > -0.01: momentum_score = 10
    elif mom_pct > -0.03: momentum_score = 5
    else: momentum_score = 0
    total = trend_score + rsi_score + macd_score + momentum_score
    if total >= 70: direction = "🟢 Bullish"
    elif total >= 55: direction = "🟡 Mild Bullish"
    elif total >= 45: direction = "⚪ Neutral"
    elif total >= 30: direction = "🟠 Mild Bearish"
    else: direction = "🔴 Bearish"
    return {"score": total, "direction": direction, "rsi": round(rsi_val, 1),
            "macd_hist": round(hist_val, 4), "trend_score": trend_score,
            "rsi_score": rsi_score, "macd_score": macd_score, "momentum_score": momentum_score}

def rsi_macd_confirmed_buy(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 30: return True
    close = df["close"] if "close" in df.columns else df["Close"]
    close = close.dropna()
    rsi_series = calc_rsi(close)
    rsi_val = rsi_series.iloc[-1] if not rsi_series.empty else 50.0
    _, _, histogram = calc_macd(close)
    hist_val = histogram.iloc[-1] if not histogram.empty else 0.0
    if pd.isna(rsi_val): rsi_val = 50.0
    if pd.isna(hist_val): hist_val = 0.0
    return (rsi_val < 70) and (hist_val > 0)

def sell_limit(symbol: str, qty, current_price: float, reason: str, entry_price: float = 0.0):
    trading_client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
    st.session_state.peak_prices.pop(symbol, None)
    st.session_state.pop("local_cash", None)
    log(f"{reason} | SELL {qty} {symbol} @ MARKET (last ${current_price:.2f})")
    if entry_price > 0:
        pl_usd = round((current_price - entry_price) * float(qty), 2)
        pl_pct = round((current_price - entry_price) / entry_price * 100, 2)
        today = datetime.now(SGT).date().isoformat()
        if st.session_state.realized_trades and st.session_state.realized_trades[0].get("date") != today:
            st.session_state.realized_trades = []
        st.session_state.realized_trades.insert(0, {
            "date": today, "Symbol": symbol, "Buy Price": f"${entry_price:.2f}",
            "Sell Price": f"${current_price:.2f}", "Qty": round(float(qty), 4),
            "P&L ($)": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
            "P&L (%)": f"{pl_pct:+.2f}%", "Time (SGT)": datetime.now(SGT).strftime("%H:%M:%S"),
            "Reason": reason.split("|")[0].strip(), "_pl_usd": pl_usd,
        })
        save_trade_to_supabase(st.session_state.realized_trades[0])

def save_trade_to_supabase(trade: dict):
    try:
        supabase.table("realized_trades").insert({
            "date": trade["date"], "symbol": trade["Symbol"], "buy_price": trade["Buy Price"],
            "sell_price": trade["Sell Price"], "qty": trade["Qty"], "pl_usd": trade["_pl_usd"],
            "pl_display": trade["P&L ($)"], "pl_pct": trade["P&L (%)"],
            "time_sgt": trade["Time (SGT)"], "reason": trade["Reason"],
        }).execute()
    except Exception:
        pass

def run_strategy():
    reset_baseline_if_needed()
    if not is_market_open():
        log("Market closed — skipping scan")
        st.session_state.last_scan = datetime.now(SGT)
        return
    try:
        account = trading_client.get_account()
        alpaca_bp = float(account.buying_power)
        local_cash = st.session_state.get("local_cash", alpaca_bp)
        cash = min(alpaca_bp, local_cash)
        positions = trading_client.get_all_positions()
        held = {p.symbol: p for p in positions}
    except Exception as e:
        log(f"Account fetch error: {e}")
        return
    if is_eod_window():
        for p in positions:
            try:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                st.session_state.peak_prices.pop(p.symbol, None)
                log(f"End-of-Week liquidation: SELL {p.qty} {p.symbol}")
            except Exception as e:
                log(f"EOW sell error {p.symbol}: {e}")
        return
    for sym, p in held.items():
        try:
            df = get_bars(sym)
            if df.empty or len(df) < 20: continue
            curr_p = float(df["close"].iloc[-1])
            s_ma = float(df["close"].rolling(window=5).mean().iloc[-1])
            l_ma = float(df["close"].rolling(window=20).mean().iloc[-1])
            entry_p = float(p.avg_entry_price)
            profit_pct = (curr_p - entry_p) / entry_p
            hard_sl, trail_pct, _ = profile(sym)
            if profit_pct <= -hard_sl:
                sell_limit(sym, p.qty, curr_p, f"HARD STOP ({profit_pct*100:+.2f}% <= -{hard_sl*100:.1f}%)", entry_p)
                continue
            peak = max(st.session_state.peak_prices.get(sym, entry_p), curr_p)
            st.session_state.peak_prices[sym] = peak
            gain_from_entry = (peak - entry_p) / entry_p
            trail_active = gain_from_entry >= (trail_pct * 0.5)
            trail_stop = peak * (1 - trail_pct)
            if trail_active and curr_p <= trail_stop:
                sell_limit(sym, p.qty, curr_p, f"TRAIL STOP (peak ${peak:.2f} → ${curr_p:.2f}, P&L {profit_pct*100:+.2f}%)", entry_p)
                continue
            if profit_pct >= 0.02:
                sell_limit(sym, p.qty, curr_p, f"TARGET HIT (+{profit_pct*100:.2f}%)", entry_p)
                continue
            if s_ma < l_ma:
                sell_limit(sym, p.qty, curr_p, f"TREND REVERSED (SMA5 < SMA20, P&L {profit_pct*100:+.2f}%)", entry_p)
        except Exception as e:
            log(f"Exit error {sym}: {e}")
    if cash <= CASH_BUFFER:
        log("Cash below buffer — skipping buy scan")
        st.session_state.last_scan = datetime.now(SGT)
        return
    for symbol in WATCHLIST:
        if symbol in held: continue
        try:
            df = get_bars(symbol)
            if df.empty or len(df) < 20: continue
            curr_p = float(df["close"].iloc[-1])
            s_ma = float(df["close"].rolling(window=5).mean().iloc[-1])
            l_ma = float(df["close"].rolling(window=20).mean().iloc[-1])
            if s_ma > l_ma:
                if not rsi_macd_confirmed_buy(df):
                    log(f"SKIP {symbol} — RSI overbought or MACD negative")
                    continue
                qty = round(MAX_TRADE_USD / curr_p, 6)
                if qty <= 0: continue
                actual_cost = round(qty * curr_p, 2)
                if cash - actual_cost < CASH_BUFFER:
                    log(f"SKIP {symbol} — would breach cash buffer (${cash:.2f} - ${actual_cost:.2f} < ${CASH_BUFFER:,.0f})")
                    continue
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.IOC))
                cash -= actual_cost
                st.session_state.local_cash = cash
                st.session_state.peak_prices[symbol] = curr_p
                log(f"BUY {qty} {symbol} @ ${curr_p:.2f} = ${actual_cost:.2f} (SMA5 > SMA20, RSI+MACD ✓)")
        except Exception as e:
            log(f"Buy scan error {symbol}: {e}")
    st.session_state.last_scan = datetime.now(SGT)

def is_market_open() -> bool:
    try:
        clock = trading_client.get_clock()
        if not clock.is_open: return False
        if clock.next_open and clock.next_close: return clock.next_open > clock.next_close
        return clock.is_open
    except Exception:
        now_sgt = datetime.now(SGT)
        weekday = now_sgt.weekday()
        hour, minute = now_sgt.hour, now_sgt.minute
        after_open = (hour == 21 and minute >= 31) or (hour >= 22)
        before_close = (hour < 4) or (hour == 4 and minute == 0)
        is_weekday = weekday < 5
        if hour < 12:
            is_weekday = (weekday - 1) % 7 < 5
        return is_weekday and (after_open or before_close)

def run_backtest(symbol: str, period: str, hard_sl: float, trail_pct: float, buy_trend: float, max_trade_usd: float):
    df = yf.download(symbol, period=period, interval="1h", progress=False)
    if df.empty: return None, None
    df = df[["Close"]].copy()
    df.columns = ["close"]
    df["avg_20"] = df["close"].rolling(20).mean()
    df.dropna(inplace=True)
    cash = 10000.0
    position = 0.0
    entry_price = 0.0
    peak_price = 0.0
    trades = []
    for i, (ts, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        avg_20 = float(row["avg_20"])
        if position > 0:
            peak_price = max(peak_price, price)
            gain_from_entry = (peak_price - entry_price) / entry_price
            trail_active = gain_from_entry >= (trail_pct * 0.5)
            trail_stop_price = peak_price * (1 - trail_pct)
            trail_hit = trail_active and (price <= trail_stop_price)
            pl_pct = (price - entry_price) / entry_price
            hard_hit = pl_pct <= -hard_sl
            if hard_hit or trail_hit:
                reason = "HARD SL" if hard_hit else "TRAIL STOP"
                pl_usd = round((price - entry_price) * position, 2)
                cash += price * position
                trades.append({
                    "Date": str(ts)[:16], "Action": f"SELL ({reason})",
                    "Price": round(price, 2), "Qty": position,
                    "P&L ($)": pl_usd, "P&L (%)": f"{pl_pct*100:+.2f}%",
                    "Cash": round(cash, 2),
                })
                position = 0
                entry_price = 0.0
                peak_price = 0.0
        elif position == 0:
            if price > avg_20 * (1 + buy_trend):
                qty = round(max_trade_usd / price, 6)
                cost = qty * price
                if qty <= 0 or cash < cost: continue
                df_slice = df.iloc[:i+1]
                rsi_s = calc_rsi(df_slice["close"])
                rsi_at_buy = round(rsi_s.iloc[-1], 1) if not rsi_s.empty else None
                _, _, hist_s = calc_macd(df_slice["close"])
                hist_at_buy = round(hist_s.iloc[-1], 4) if not hist_s.empty else None
                cash -= cost
                position = qty
                entry_price = price
                peak_price = price
                trades.append({
                    "Date": str(ts)[:16], "Action": "BUY", "Price": round(price, 2),
                    "Qty": qty, "Cost ($)": round(cost, 2), "RSI": rsi_at_buy,
                    "MACD Hist": hist_at_buy, "P&L ($)": 0.0, "P&L (%)": "0.00%",
                    "Cash": round(cash, 2),
                })
    final_equity = cash + (position * float(df["close"].iloc[-1]))
    sells = [t for t in trades if "SELL" in t["Action"]]
    wins = [t for t in sells if t["P&L ($)"] > 0]
    losses = [t for t in sells if t["P&L ($)"] <= 0]
    total_pl = sum(t["P&L ($)"] for t in sells)
    win_rate = len(wins) / len(sells) * 100 if sells else 0
    results = {
        "symbol": symbol, "period": period, "start_cash": 10000.0,
        "final_equity": round(final_equity, 2), "total_pl": round(total_pl, 2),
        "total_trades": len(sells), "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_win": round(sum(t["P&L ($)"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["P&L ($)"] for t in losses) / len(losses), 2) if losses else 0,
        "df": df,
    }
    return results, pd.DataFrame(trades)

# ─────────────────────────────────────────────
# FETCH LIVE ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account = trading_client.get_account()
    CASH = float(account.cash)
    EQUITY = float(account.equity)
    positions = trading_client.get_all_positions()
    unrealized = round(sum(float(p.unrealized_pl) for p in positions), 2)
    total_holdings = round(sum(float(p.market_value) for p in positions), 2)
except:
    CASH, EQUITY, unrealized, total_holdings, positions = 0.0, 0.0, 0.0, 0.0, []

total_delta = round(EQUITY - st.session_state.nightly_baseline, 2)
realized = round(total_delta - unrealized, 2)
progress_pct = min(max(realized / TARGET_PROFIT, 0.0), 1.0) if realized > 0 else 0.0
combined = round(unrealized + realized, 2)

# ─────────────────────────────────────────────
# AUTO-REFRESH TRADES (every 60 seconds) & INITIAL LOAD
# ─────────────────────────────────────────────
if "last_trade_refresh" not in st.session_state:
    st.session_state.last_trade_refresh = datetime.now(SGT)

# Initial load based on current mode
if st.session_state.trade_display_mode == "Last Completed":
    st.session_state.realized_trades = load_realized_trades()
else:
    current_session = get_current_session_start()
    st.session_state.realized_trades = load_realized_trades(current_session)

# Auto-refresh every 60 seconds
if (datetime.now(SGT) - st.session_state.last_trade_refresh).seconds >= 60:
    if st.session_state.trade_display_mode == "Last Completed":
        st.session_state.realized_trades = load_realized_trades()
    else:
        st.session_state.realized_trades = load_realized_trades(get_current_session_start())
    st.session_state.last_trade_refresh = datetime.now(SGT)
    st.rerun()

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("Bot Controls")
    st.metric("Weekly Baseline", f"${st.session_state.nightly_baseline:,.2f}")
    st.caption("Bot runs on Railway — start/stop from Railway dashboard.")
    st.divider()
    if st.button("▶️ Run Single Scan", use_container_width=True):
        run_strategy()
        st.rerun()
    st.divider()

    st.write("### 🧹 Manual Liquidation")
    if st.session_state.liq_step == "idle" and not st.session_state.pin_verified:
        if st.button("⚠️ Manual Liquidation", use_container_width=True, type="secondary"):
            st.session_state.liq_step = "pin_entered"
            st.rerun()
    elif st.session_state.liq_step == "pin_entered" and not st.session_state.pin_verified:
        st.warning("⚠️ Enter PIN to unlock liquidation")
        with st.form("liq_pin_form"):
            liq_pin = st.text_input("PIN:", type="password", key="liq_pin_input")
            col_a, col_b = st.columns(2)
            with col_a:
                verify_btn = st.form_submit_button("🔓 Verify PIN", use_container_width=True)
            with col_b:
                cancel_btn = st.form_submit_button("❌ Cancel", use_container_width=True)
            if verify_btn:
                try:
                    row = supabase.table("bot_config").select("pin").eq("id", 1).execute()
                    if row.data and row.data[0]["pin"] == liq_pin:
                        st.session_state.pin_verified = True
                        st.session_state.liq_step = "confirmed"
                        st.success("PIN verified! Click final confirmation to sell.")
                        st.rerun()
                    else:
                        st.error("Incorrect PIN")
                except Exception:
                    st.error("Could not verify PIN")
            if cancel_btn:
                st.session_state.liq_step = "idle"
                st.session_state.pin_verified = False
                st.rerun()
    elif st.session_state.liq_step == "confirmed" and st.session_state.pin_verified:
        st.error("⚠️⚠️⚠️ FINAL STEP ⚠️⚠️⚠️")
        st.warning("You have verified your PIN. Click below to SELL ALL positions.")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔥 FINAL CONFIRM - SELL ALL", use_container_width=True, type="primary"):
                try:
                    trading_client.cancel_orders()
                    time.sleep(1)
                    positions = trading_client.get_all_positions()
                    if positions:
                        for p in positions:
                            trading_client.submit_order(MarketOrderRequest(
                                symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                            ))
                        st.session_state.peak_prices = {}
                        log("🔴 Manual liquidation executed")
                        st.success(f"✅ Sold {len(positions)} position(s)!")
                    else:
                        st.info("No positions to sell.")
                    st.session_state.liq_step = "idle"
                    st.session_state.pin_verified = False
                    st.rerun()
                except Exception as e:
                    st.error(f"Liquidation error: {e}")
                    st.session_state.liq_step = "idle"
                    st.session_state.pin_verified = False
                    st.rerun()
        with col_b:
            if st.button("❌ Cancel", use_container_width=True):
                st.session_state.liq_step = "idle"
                st.session_state.pin_verified = False
                st.rerun()

    st.divider()
    status_color = "🟢" if st.session_state.bot_running else "🔴"
    st.write(f"**Status:** {status_color} {'AUTO-RUNNING' if st.session_state.bot_running else 'STOPPED'}")
    if st.session_state.last_scan:
        st.write(f"**Last scan:** {st.session_state.last_scan.strftime('%H:%M:%S')} SGT")
    st.write(f"**Scan interval:** {SCAN_INTERVAL}s")
    st.write(f"**Trade budget:** ${MAX_TRADE_USD:,.0f} per trade")
    st.divider()
    st.write("**📋 Bot Logs (from Railway)**")
    try:
        logs = supabase.table("bot_logs").select("message,created_at").order("created_at", desc=True).limit(30).execute()
        for l in logs.data:
            st.caption(l["message"])
    except:
        st.caption("No logs yet.")
    st.divider()
    st.write("**Per-stock profiles:**")
    profile_rows = [{"Symbol": sym, "Hard SL": f"-{v[0]*100:.1f}%",
                     "Trail": f"-{v[1]*100:.1f}%", "Buy Trend": f"+{v[2]*100:.1f}%"}
                    for sym, v in STOCK_PROFILES.items()]
    st.dataframe(pd.DataFrame(profile_rows), use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot")

try:
    hb_row = supabase.table("bot_state").select("last_heartbeat").eq("id", 1).execute()
    if hb_row.data and hb_row.data[0].get("last_heartbeat"):
        last_hb = datetime.fromisoformat(hb_row.data[0]["last_heartbeat"])
        if last_hb.tzinfo is None:
            last_hb = SGT.localize(last_hb)
        seconds_ago = (datetime.now(SGT) - last_hb).total_seconds()
        if seconds_ago < 60:
            st.success(f"🟢 BOT ALIVE — Last heartbeat {int(seconds_ago)}s ago", icon="🤖")
        elif seconds_ago < 300:
            st.warning(f"🟡 BOT SLOW — Last heartbeat {int(seconds_ago//60)}m ago — may be scanning", icon="⚠️")
        else:
            st.error(f"🔴 BOT POSSIBLY CRASHED — No heartbeat for {int(seconds_ago//60)} minutes! Check Railway logs.", icon="🚨")
    else:
        st.info("👁️ DASHBOARD MODE — Waiting for first heartbeat from bot...", icon="🤖")
except Exception:
    st.info("👁️ DASHBOARD MODE — Bot is running autonomously on Railway.", icon="🤖")

st.markdown("---")
strategy_title, strategy_desc = get_current_strategy_display()
st.markdown(f"📌 **Current Strategy:** {strategy_title}")
st.markdown(f"{strategy_desc}")
st.markdown("---")

# Manual Strategy Override (dynamic)
st.write("### 🔧 Manual Strategy Override")
if st.session_state.override_step == "idle" and not st.session_state.override_authorized:
    if st.button("🔧 Change Strategy", use_container_width=True, type="primary"):
        st.session_state.override_step = "pin_entered"
        st.rerun()
elif st.session_state.override_step == "pin_entered" and not st.session_state.override_authorized:
    st.warning("🔒 Enter PIN to change strategy")
    with st.form("override_pin_form"):
        override_pin = st.text_input("PIN:", type="password", key="override_pin_input")
        col_a, col_b = st.columns(2)
        with col_a:
            verify_btn = st.form_submit_button("🔓 Verify PIN", use_container_width=True)
        with col_b:
            cancel_btn = st.form_submit_button("❌ Cancel", use_container_width=True)
        if verify_btn:
            try:
                row = supabase.table("bot_config").select("pin").eq("id", 1).execute()
                if row.data and row.data[0]["pin"] == override_pin:
                    st.session_state.override_authorized = True
                    st.session_state.override_step = "authorized"
                    st.success("PIN verified! You can now change the strategy.")
                    st.rerun()
                else:
                    st.error("Incorrect PIN")
            except Exception:
                st.error("Could not verify PIN")
        if cancel_btn:
            st.session_state.override_step = "idle"
            st.session_state.override_authorized = False
            st.rerun()
elif st.session_state.override_step == "authorized" and st.session_state.override_authorized:
    st.success("✅ Access granted – you can change the strategy")
    cur_title, _ = get_current_strategy_display()
    st.info(f"Current strategy: {cur_title}")
    strategy_options = [{"name": "AUTO", "display_name": "🤖 AUTO"}] + st.session_state.strategies
    cols = st.columns(min(len(strategy_options), 4))
    for idx, strat in enumerate(strategy_options):
        with cols[idx % 4]:
            if st.button(strat["display_name"], use_container_width=True):
                st.session_state.pending_strategy = strat["name"]
                st.rerun()
    with st.expander("🔐 Change PIN (admin only)", expanded=False):
        with st.form("change_pin_form"):
            current_pin = st.text_input("Current PIN:", type="password", key="current_pin")
            new_pin = st.text_input("New PIN (4-6 digits):", type="password", max_chars=6, key="new_pin")
            confirm_pin = st.text_input("Confirm New PIN:", type="password", max_chars=6, key="confirm_pin")
            col_pin1, col_pin2 = st.columns(2)
            with col_pin1:
                change_submitted = st.form_submit_button("✅ Update PIN", use_container_width=True)
            with col_pin2:
                change_cancel = st.form_submit_button("❌ Cancel", use_container_width=True)
            if change_submitted:
                try:
                    row = supabase.table("bot_config").select("pin").eq("id", 1).execute()
                    if row.data and row.data[0]["pin"] == current_pin:
                        if new_pin and new_pin == confirm_pin and new_pin.isdigit() and 4 <= len(new_pin) <= 6:
                            supabase.table("bot_config").update({"pin": new_pin}).eq("id", 1).execute()
                            st.success("✅ PIN updated successfully!")
                            st.rerun()
                        else:
                            st.error("New PIN must be 4-6 digits and match")
                    else:
                        st.error("Current PIN is incorrect")
                except Exception:
                    st.error("Could not verify current PIN")
            if change_cancel:
                st.rerun()
    if st.button("✅ Confirm and Relock", use_container_width=True):
        st.session_state.override_step = "idle"
        st.session_state.override_authorized = False
        st.success("Strategy locked. Changes are active.")
        st.rerun()

if st.session_state.pending_strategy is not None:
    strategy_name = st.session_state.pending_strategy
    if strategy_name == "AUTO":
        strategy_display = "AUTO (time‑based)"
    else:
        strat = next((s for s in st.session_state.strategies if s["name"] == strategy_name), None)
        strategy_display = strat["display_name"] if strat else strategy_name
    st.markdown("---")
    st.warning(f"⚠️ **You are changing the strategy to: {strategy_display}**")
    st.caption("Please confirm this action.")
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("✅ Confirm", use_container_width=True, type="primary"):
            set_forced_strategy(st.session_state.pending_strategy)
            st.session_state.override_step = "idle"
            st.session_state.override_authorized = False
            st.session_state.pending_strategy = None
            st.success(f"Strategy changed to {strategy_display}")
            st.rerun()
    with col_cancel:
        if st.button("❌ Cancel", use_container_width=True):
            st.session_state.override_step = "idle"
            st.session_state.override_authorized = False
            st.session_state.pending_strategy = None
            st.info("Strategy change cancelled.")
            st.rerun()

st.markdown("---")

# TABS
tab_live, tab_signals, tab_portfolio = st.tabs(["Live Trading", "Signal Scanner", "Portfolio Backtest"])

# ─────────────────────────────────────────────
# TAB 1 — LIVE TRADING
# ─────────────────────────────────────────────
with tab_live:
    st.write(f"## 🎯 Weekly Goal: ${TARGET_PROFIT:.0f} USD")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Equity", f"${EQUITY:,.2f}", delta=float(combined))
    c2.metric("Cash Balance", f"${CASH:,.2f}")
    c3.metric("Total in Holdings", f"${total_holdings:,.2f}")
    c4.metric("Realized P&L", f"${realized:,.2f}")
    c5.metric("Unrealized P&L", f"${unrealized:,.2f}")
    st.progress(progress_pct, text=f"Weekly Goal Progress: ${realized:.2f} / ${TARGET_PROFIT:.0f} ({int(progress_pct*100)}%)")

    st.write("### 📦 Live Holdings")
    if positions:
        pos_data = []
        for p in positions:
            hard_sl, trail_pct, _ = profile(p.symbol)
            entry = float(p.avg_entry_price)
            current = float(p.current_price)
            peak = st.session_state.peak_prices.get(p.symbol, entry)
            trail_stop = round(peak * (1 - trail_pct), 2)
            hard_stop = round(entry * (1 - hard_sl), 2)
            pos_data.append({
                "Symbol": p.symbol, "Qty": p.qty, "Avg Cost": f"${entry:.2f}",
                "Current": f"${current:.2f}", "Peak": f"${peak:.2f}",
                "Trail Stop": f"${trail_stop:.2f}", "Hard SL": f"${hard_stop:.2f}",
                "Value": f"${float(p.market_value):,.2f}",
                "P&L ($)": f"${float(p.unrealized_pl):.2f}",
                "P&L (%)": f"{float(p.unrealized_plpc)*100:+.2f}%",
            })
        st.dataframe(pd.DataFrame(pos_data), use_container_width=True, height=280)
        st.caption(f"📊 Total holdings value: **${total_holdings:,.2f}** across {len(positions)} position(s)")
    else:
        st.success("✅ Account is 100% Cash.")

    # Today's Completed Trades with toggle
    with st.expander("📊 Today's Completed Trades", expanded=True):
        col_refresh, col_toggle = st.columns([3, 1])
        with col_toggle:
            mode = st.radio(
                "Show trades from:",
                ["Last Completed", "Current Session"],
                index=0 if st.session_state.trade_display_mode == "Last Completed" else 1,
                horizontal=True,
                key="trade_mode_radio",
                label_visibility="collapsed"
            )
            if mode != st.session_state.trade_display_mode:
                st.session_state.trade_display_mode = mode
                if mode == "Last Completed":
                    st.session_state.realized_trades = load_realized_trades()
                else:
                    st.session_state.realized_trades = load_realized_trades(get_current_session_start())
                st.rerun()

        with col_refresh:
            if st.button("🔄 Refresh Trades", use_container_width=True):
                if st.session_state.trade_display_mode == "Last Completed":
                    st.session_state.realized_trades = load_realized_trades()
                else:
                    st.session_state.realized_trades = load_realized_trades(get_current_session_start())
                st.rerun()

        if st.session_state.trade_display_mode == "Last Completed":
            display_session = get_last_completed_session()
            session_label = f"**{display_session}** (completed session)"
        else:
            display_session = get_current_session_start()
            session_label = f"**{display_session}** (ongoing session – updates in real time)"

        st.caption(f"📅 Showing trades from trading session: {session_label} (9:30 PM SGT → 4:00 AM SGT)")

        trades = st.session_state.realized_trades
        if trades:
            strategy_names = list(set(t.get("Strategy", "Unknown") for t in trades if isinstance(t, dict)))
            for strat_name in strategy_names:
                strat_trades = [t for t in trades if isinstance(t, dict) and str(t.get("Strategy", "")).upper() == strat_name.upper()]
                if strat_trades:
                    strat_display = strat_name
                    for s in st.session_state.strategies:
                        if s["name"].upper() == strat_name.upper():
                            strat_display = s["display_name"]
                            break
                    st.markdown(f"**{strat_display} Trades**")
                    df = pd.DataFrame(strat_trades)[["Symbol", "Buy Price", "Sell Price", "Qty", "P&L ($)", "P&L (%)", "Time (SGT)", "Reason"]]
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    total = sum(t.get("_pl_usd", 0) for t in strat_trades)
                    st.write(f"**Total {strat_display} P&L: ${total:+.2f}**")
        else:
            st.info("No completed trades in this session yet.")

    # Daily P&L Bar Chart
    st.markdown("### 📊 Daily P&L by Trading Session")
    daily_df = compute_daily_pnl_overview()
    if not daily_df.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=daily_df["Trading Session Date"],
            y=daily_df["Total"],
            name="Total P&L",
            marker_color=["#26a65b" if x >= 0 else "#e74c3c" for x in daily_df["Total"]],
            text=[f"${x:+.2f}" for x in daily_df["Total"]], textposition="outside",
        ))
        for col in daily_df.columns:
            if col not in ["Trading Session Date", "Total"]:
                fig.add_trace(go.Bar(
                    x=daily_df["Trading Session Date"],
                    y=daily_df[col],
                    name=col,
                    opacity=0.7,
                ))
        fig.update_layout(barmode="group", height=400, template="plotly_dark",
                          xaxis_title="Trading Session (start evening SGT)", yaxis_title="P&L (USD)",
                          legend=dict(orientation="h", yanchor="bottom", y=1.02),
                          margin=dict(l=0,r=0,t=30,b=0))
        st.plotly_chart(fig, use_container_width=True)

        daily_sorted = daily_df.sort_values("Trading Session Date", ascending=True)
        daily_sorted["Cumulative Total"] = daily_sorted["Total"].cumsum()
        fig_cum = go.Figure()
        fig_cum.add_trace(go.Scatter(
            x=daily_sorted["Trading Session Date"], y=daily_sorted["Cumulative Total"],
            mode="lines+markers", name="Cumulative P&L", line=dict(color="#f39c12", width=3),
            marker=dict(size=8, color="#e67e22"), fill="tozeroy", fillcolor="rgba(243,156,18,0.1)",
            text=[f"${x:+.2f}" for x in daily_sorted["Cumulative Total"]], textposition="top center",
        ))
        fig_cum.update_layout(height=300, template="plotly_dark",
                              xaxis_title="Trading Session (start evening SGT)",
                              yaxis_title="Cumulative P&L (USD)", margin=dict(l=0,r=0,t=30,b=0),
                              hovermode="x unified")
        st.plotly_chart(fig_cum, use_container_width=True)
    else:
        st.info("No trade data available yet for daily P&L chart.")

    with st.expander("📋 Open Positions (Unrealized)", expanded=False):
        try:
            positions = trading_client.get_all_positions()
            if positions:
                open_data = []
                for p in positions:
                    entry = float(p.avg_entry_price)
                    current = float(p.current_price)
                    qty = float(p.qty)
                    pl_usd = (current - entry) * qty
                    pl_pct = (pl_usd / (entry * qty)) * 100 if entry * qty != 0 else 0
                    open_data.append({
                        "Symbol": p.symbol, "Entry": f"${entry:.2f}", "Current": f"${current:.2f}",
                        "Qty": round(qty, 4), "Unrealized P&L ($)": f"${pl_usd:+.2f}",
                        "Unrealized P&L (%)": f"{pl_pct:+.2f}%",
                    })
                st.dataframe(pd.DataFrame(open_data), use_container_width=True, hide_index=True)
            else:
                st.info("No open positions.")
        except:
            st.info("Could not fetch positions.")

    st.write("### 📡 Live Signal Rankings")
    lsr_col1, lsr_col2 = st.columns([3, 1])
    with lsr_col2:
        if st.button("🔄 Refresh Rankings", use_container_width=True, key="live_sig_btn"):
            live_scan_rows = []
            live_bar = st.progress(0, text="Scanning signals...")
            for idx, sym in enumerate(WATCHLIST):
                live_bar.progress(idx / len(WATCHLIST), text=f"Scanning {sym}...")
                try:
                    df_ls = yf.download(sym, period="1mo", interval="1h", progress=False)
                    if df_ls.empty: continue
                    df_ls = df_ls[["Close"]].copy()
                    df_ls.columns = ["close"]
                    sig = compute_signal_score(df_ls)
                    live_scan_rows.append({"Symbol": sym, "Score": sig["score"], "Signal": sig["direction"], "RSI": sig["rsi"], "MACD Hist": sig["macd_hist"]})
                except: pass
            live_bar.progress(1.0, text="Done!")
            df_live_sig = pd.DataFrame(live_scan_rows).sort_values("Score", ascending=False).reset_index(drop=True)
            df_live_sig.insert(0, "Rank", range(1, len(df_live_sig)+1))
            st.session_state.signal_results = df_live_sig
            st.session_state.live_signal_time = datetime.now(SGT)
    if st.session_state.signal_results is not None:
        df_ls_display = st.session_state.signal_results.copy()
        ts_label = st.session_state.live_signal_time.strftime("%H:%M:%S SGT") if st.session_state.live_signal_time else "—"
        with lsr_col1:
            bull_n = len(df_ls_display[df_ls_display["Score"] >= 55])
            bear_n = len(df_ls_display[df_ls_display["Score"] <= 45])
            neut_n = len(df_ls_display) - bull_n - bear_n
            st.caption(f"Last updated: **{ts_label}** | 🟢 Bullish: {bull_n} | ⚪ Neutral: {neut_n} | 🔴 Bearish: {bear_n}")
        top_bull_df = df_ls_display.head(10)[["Rank", "Symbol", "Score", "Signal", "RSI", "MACD Hist"]]
        top_bear_df = df_ls_display.tail(10).sort_values("Score")[["Rank", "Symbol", "Score", "Signal", "RSI", "MACD Hist"]]
        bull_col, bear_col = st.columns(2)
        with bull_col:
            st.markdown("**🟢 Top 10 Bullish**")
            st.dataframe(top_bull_df, use_container_width=True, hide_index=True, height=370)
        with bear_col:
            st.markdown("**🔴 Top 10 Bearish**")
            st.dataframe(top_bear_df, use_container_width=True, hide_index=True, height=370)
        bar_syms_l = df_ls_display["Symbol"].tolist()
        bar_scores_l = df_ls_display["Score"].tolist()
        bar_cols_l = ["#26a65b" if s >= 55 else ("#e74c3c" if s <= 45 else "#868e96") for s in bar_scores_l]
        fig_ls = go.Figure(go.Bar(x=bar_syms_l, y=bar_scores_l, marker_color=bar_cols_l, text=[str(s) for s in bar_scores_l], textposition="outside"))
        fig_ls.add_hline(y=55, line_dash="dot", line_color="#26a65b", annotation_text="Bullish (55)")
        fig_ls.add_hline(y=45, line_dash="dot", line_color="#e74c3c", annotation_text="Bearish (45)")
        fig_ls.update_layout(height=300, template="plotly_dark", yaxis_title="Signal Score", yaxis_range=[0,115], margin=dict(l=0,r=0,t=20,b=0))
        st.plotly_chart(fig_ls, use_container_width=True)
        if positions:
            held_syms = [p.symbol for p in positions]
            held_sigs = df_ls_display[df_ls_display["Symbol"].isin(held_syms)][["Symbol", "Rank", "Score", "Signal", "RSI", "MACD Hist"]]
            if not held_sigs.empty:
                st.markdown("**📦 Signal Scores for Current Holdings**")
                st.dataframe(held_sigs, use_container_width=True, hide_index=True)
    else:
        st.info("👆 Click **Refresh Rankings** to load signal scores for all stocks.")

    st.divider()
    with st.expander("📋 Activity Log", expanded=True):
        try:
            logs = supabase.table("bot_logs").select("message, created_at").order("created_at", desc=True).limit(50).execute()
            if logs.data:
                for log_entry in logs.data:
                    utc_time = datetime.fromisoformat(log_entry["created_at"].replace('Z', '+00:00'))
                    sgt_time = utc_time.astimezone(SGT).strftime("%H:%M:%S")
                    st.text(f"[{sgt_time}] {log_entry['message']}")
            else:
                st.info("No logs yet.")
        except Exception as e:
            st.error(f"Could not load logs: {e}")

# ─────────────────────────────────────────────
# TAB 2 — SIGNAL SCANNER
# ─────────────────────────────────────────────
with tab_signals:
    st.write("## 📡 Signal Scanner — Bullish/Bearish Rankings (1–100)")
    st.write("Scores each stock from 1 to 100 using Trend, RSI, MACD, and Momentum.")
    sig_col1, sig_col2 = st.columns([2, 1])
    with sig_col1:
        sig_period = st.selectbox("Data period", ["5d", "1mo", "3mo"], index=1, key="sig_period")
    with sig_col2:
        run_scanner = st.button("🔍 Run Signal Scan", type="primary", use_container_width=True)
    if "signal_results" not in st.session_state:
        st.session_state.signal_results = None
    if run_scanner:
        scan_rows = []
        scan_bar = st.progress(0, text="Scanning...")
        for idx, sym in enumerate(WATCHLIST):
            scan_bar.progress(idx / len(WATCHLIST), text=f"Scanning {sym} ({idx+1}/{len(WATCHLIST)})...")
            try:
                df_sig = yf.download(sym, period=sig_period, interval="1h", progress=False)
                if df_sig.empty: continue
                df_sig = df_sig[["Close"]].copy()
                df_sig.columns = ["close"]
                sig = compute_signal_score(df_sig)
                hard_sl, trail_pct, buy_trend = profile(sym)
                scan_rows.append({
                    "Symbol": sym, "Score": sig["score"], "Signal": sig["direction"],
                    "RSI": sig["rsi"], "MACD Hist": sig["macd_hist"],
                    "Trend Pts": sig["trend_score"], "RSI Pts": sig["rsi_score"],
                    "MACD Pts": sig["macd_score"], "Momentum Pts": sig["momentum_score"],
                    "Hard SL": f"-{hard_sl*100:.1f}%", "Trail Stop": f"-{trail_pct*100:.1f}%",
                    "Buy Trend": f"+{buy_trend*100:.1f}%",
                })
            except Exception:
                scan_rows.append({
                    "Symbol": sym, "Score": 50, "Signal": "⚪ N/A", "RSI": None, "MACD Hist": None,
                    "Trend Pts": 0, "RSI Pts": 0, "MACD Pts": 0, "Momentum Pts": 0,
                    "Hard SL": "-", "Trail Stop": "-", "Buy Trend": "-",
                })
        scan_bar.progress(1.0, text="Scan complete!")
        df_signals = pd.DataFrame(scan_rows).sort_values("Score", ascending=False).reset_index(drop=True)
        df_signals.insert(0, "Rank", range(1, len(df_signals) + 1))
        st.session_state.signal_results = df_signals
    if st.session_state.signal_results is not None:
        df_sig_display = st.session_state.signal_results.copy()
        bullish_count = len(df_sig_display[df_sig_display["Score"] >= 55])
        bearish_count = len(df_sig_display[df_sig_display["Score"] <= 45])
        neutral_count = len(df_sig_display) - bullish_count - bearish_count
        top_bull = df_sig_display.iloc[0]["Symbol"] if len(df_sig_display) > 0 else "—"
        top_bear = df_sig_display.iloc[-1]["Symbol"] if len(df_sig_display) > 0 else "—"
        sm1, sm2, sm3, sm4, sm5 = st.columns(5)
        sm1.metric("🟢 Bullish", bullish_count)
        sm2.metric("⚪ Neutral", neutral_count)
        sm3.metric("🔴 Bearish", bearish_count)
        sm4.metric("🥇 Top Bull", top_bull, delta=f"Score {df_sig_display.iloc[0]['Score']}" if len(df_sig_display) > 0 else "")
        sm5.metric("🥀 Top Bear", top_bear, delta=f"Score {df_sig_display.iloc[-1]['Score']}" if len(df_sig_display) > 0 else "")
        st.write("### 📋 Full Rankings Table")
        st.dataframe(df_sig_display, use_container_width=True, hide_index=True, height=500)
        st.write("### 📊 Signal Score Chart (1–100)")
        bar_syms = df_sig_display["Symbol"].tolist()
        bar_scores = df_sig_display["Score"].tolist()
        bar_cols = []
        for s in bar_scores:
            if s >= 70: bar_cols.append("#26a65b")
            elif s >= 55: bar_cols.append("#82c91e")
            elif s >= 45: bar_cols.append("#868e96")
            elif s >= 30: bar_cols.append("#fd7e14")
            else: bar_cols.append("#e74c3c")
        fig_sig = go.Figure(go.Bar(x=bar_syms, y=bar_scores, marker_color=bar_cols, text=[str(s) for s in bar_scores], textposition="outside"))
        fig_sig.add_hline(y=70, line_dash="dot", line_color="#26a65b", annotation_text="Bullish threshold (70)")
        fig_sig.add_hline(y=50, line_dash="dot", line_color="gray", annotation_text="Neutral (50)")
        fig_sig.add_hline(y=30, line_dash="dot", line_color="#e74c3c", annotation_text="Bearish threshold (30)")
        fig_sig.update_layout(height=400, template="plotly_dark", yaxis_title="Signal Score", yaxis_range=[0,110], margin=dict(l=0,r=0,t=30,b=0))
        st.plotly_chart(fig_sig, use_container_width=True)
        st.write("### 🔵 RSI vs Score Scatter")
        df_rsi_plot = df_sig_display.dropna(subset=["RSI"])
        fig_rsi = go.Figure(go.Scatter(
            x=df_rsi_plot["RSI"], y=df_rsi_plot["Score"],
            mode="markers+text", text=df_rsi_plot["Symbol"], textposition="top center",
            marker=dict(color=df_rsi_plot["Score"], colorscale="RdYlGn", size=10, showscale=True, colorbar=dict(title="Score"))
        ))
        fig_rsi.add_vline(x=30, line_dash="dot", line_color="#26a65b", annotation_text="RSI 30 (oversold)")
        fig_rsi.add_vline(x=70, line_dash="dot", line_color="#e74c3c", annotation_text="RSI 70 (overbought)")
        fig_rsi.update_layout(height=400, template="plotly_dark", xaxis_title="RSI", yaxis_title="Signal Score", margin=dict(l=0,r=0,t=30,b=0))
        st.plotly_chart(fig_rsi, use_container_width=True)
    else:
        st.info("👆 Click **Run Signal Scan** to score all stocks.")

# ─────────────────────────────────────────────
# TAB 3 — PORTFOLIO BACKTEST (dynamic strategy selection)
# ─────────────────────────────────────────────
with tab_portfolio:
    st.write("## 📂 Portfolio Backtest — Shared Capital Simulation")
    st.write("Simulates all stocks trading simultaneously from a single shared capital pool using one strategy.")
    st.info("How it works: At each hourly bar, the engine checks every stock in the watchlist. It sells positions that hit their trailing/hard stop, then uses freed cash to buy new signals — all from the same shared $10,000 pool.")

    # Strategy selection dropdown (from Supabase)
    strategy_options = [{"name": s["name"], "display_name": s["display_name"], "buy_trend": 0.006} for s in st.session_state.strategies]
    strategy_names = [s["display_name"] for s in strategy_options]
    selected_strategy_idx = st.selectbox("Select Strategy for Backtest", range(len(strategy_names)), format_func=lambda i: strategy_names[i])
    selected_strategy = strategy_options[selected_strategy_idx]

    st.caption(f"Using buy trend signal: +0.6% above 20-bar average (default).")

    pcfg1, pcfg2, pcfg3, pcfg4 = st.columns(4)
    with pcfg1: p_period = st.selectbox("Period", ["1mo", "3mo", "6mo", "1y", "2y"], index=1, key="p_period")
    with pcfg2: p_capital = st.number_input("Starting Capital ($)", min_value=1000, max_value=100000, value=10000, step=1000, key="p_capital")
    with pcfg3: p_max_trade = st.number_input("Max $ per trade", min_value=50, max_value=10000, value=100, step=50, key="p_max_trade")
    with pcfg4: p_use_profile = st.checkbox("Use per-stock profiles", value=True, key="p_use_profile")

    if not p_use_profile:
        pov1, pov2, pov3 = st.columns(3)
        with pov1: p_hard_sl = st.slider("Hard Stop Loss %", 0.005, 0.05, 0.013, 0.001, format="%.3f", key="p_hard_sl")
        with pov2: p_trail = st.slider("Trailing Stop %", 0.005, 0.05, 0.008, 0.001, format="%.3f", key="p_trail")
        with pov3: p_trend = st.slider("Buy Trend %", 0.001, 0.02, 0.006, 0.001, format="%.3f", key="p_trend")

    run_portfolio = st.button("▶️ Run Portfolio Backtest", type="primary", use_container_width=True, key="run_portfolio")

    if run_portfolio:
        with st.spinner("📥 Downloading data for all stocks..."):
            all_data = {}
            dl_bar = st.progress(0, text="Downloading...")
            for idx, sym in enumerate(WATCHLIST):
                dl_bar.progress(idx / len(WATCHLIST), text=f"Downloading {sym}...")
                try:
                    df_raw = yf.download(sym, period=p_period, interval="1h", progress=False)
                    if df_raw.empty: continue
                    df_raw = df_raw[["Close"]].copy()
                    df_raw.columns = ["close"]
                    df_raw["avg_20"] = df_raw["close"].rolling(20).mean()
                    df_raw.dropna(inplace=True)
                    all_data[sym] = df_raw
                except: continue
            dl_bar.progress(1.0, text=f"✅ Downloaded {len(all_data)}/{len(WATCHLIST)} stocks")

        if not all_data:
            st.error("No data downloaded.")
        else:
            all_timestamps = sorted(set(ts for df in all_data.values() for ts in df.index))
            with st.spinner("⚙️ Simulating portfolio..."):
                cash = float(p_capital)
                positions = {}
                trade_log = []
                equity_curve = []
                sym_pl = {sym: 0.0 for sym in all_data}
                for ts in all_timestamps:
                    holdings_value = 0.0
                    for sym, pos in positions.items():
                        if ts in all_data[sym].index:
                            holdings_value += all_data[sym].loc[ts, "close"] * pos["qty"]
                        else:
                            holdings_value += pos["entry_price"] * pos["qty"]
                    equity_curve.append({"timestamp": ts, "equity": round(cash + holdings_value, 2), "cash": round(cash, 2), "positions": len(positions)})
                    to_close = []
                    for sym, pos in positions.items():
                        if ts not in all_data[sym].index: continue
                        price = float(all_data[sym].loc[ts, "close"])
                        if p_use_profile:
                            hard_sl, trail_pct, _ = profile(sym)
                        else:
                            hard_sl, trail_pct = p_hard_sl, p_trail
                        entry = pos["entry_price"]
                        peak = pos["peak_price"]
                        peak = max(peak, price)
                        positions[sym]["peak_price"] = peak
                        gain_from_entry = (peak - entry) / entry
                        trail_active = gain_from_entry >= (trail_pct * 0.5)
                        trail_stop_price = peak * (1 - trail_pct)
                        trail_hit = trail_active and (price <= trail_stop_price)
                        pl_pct = (price - entry) / entry
                        hard_hit = pl_pct <= -hard_sl
                        if hard_hit or trail_hit:
                            reason = "HARD SL" if hard_hit else "TRAIL STOP"
                            pl_usd = round((price - entry) * pos["qty"], 4)
                            proceeds = price * pos["qty"]
                            sym_pl[sym] += pl_usd
                            to_close.append((sym, price, pos["qty"], reason, pl_usd, pl_pct, proceeds))
                    for sym, price, qty, reason, pl_usd, pl_pct, proceeds in to_close:
                        cash += proceeds
                        del positions[sym]
                        trade_log.append({"Timestamp": str(ts)[:16], "Symbol": sym, "Action": f"SELL ({reason})", "Price": round(price,4), "Qty": round(qty,6), "Cost/Proceeds": round(proceeds,2), "P&L ($)": pl_usd, "P&L (%)": f"{pl_pct*100:+.2f}%", "Cash After": round(cash,2)})
                    for sym in WATCHLIST:
                        if sym in positions: continue
                        if sym not in all_data: continue
                        if ts not in all_data[sym].index: continue
                        row = all_data[sym].loc[ts]
                        price = float(row["close"])
                        avg20 = float(row["avg_20"]) if not pd.isna(row["avg_20"]) else None
                        if avg20 is None: continue
                        if p_use_profile:
                            _, _, buy_trend = profile(sym)
                        else:
                            buy_trend = p_trend
                        if price > avg20 * (1 + buy_trend):
                            qty = round(p_max_trade / price, 6)
                            cost = qty * price
                            if qty <= 0 or cash < cost: continue
                            cash -= cost
                            positions[sym] = {"qty": qty, "entry_price": price, "peak_price": price}
                            trade_log.append({"Timestamp": str(ts)[:16], "Symbol": sym, "Action": "BUY", "Price": round(price,4), "Qty": round(qty,6), "Cost/Proceeds": round(cost,2), "P&L ($)": 0.0, "P&L (%)": "0.00%", "Cash After": round(cash,2)})
                for sym, pos in positions.items():
                    last_price = float(all_data[sym]["close"].iloc[-1]) if sym in all_data else pos["entry_price"]
                    pl_usd = round((last_price - pos["entry_price"]) * pos["qty"], 4)
                    proceeds = last_price * pos["qty"]
                    pl_pct = (last_price - pos["entry_price"]) / pos["entry_price"]
                    sym_pl[sym] += pl_usd
                    cash += proceeds
                    trade_log.append({"Timestamp": str(all_timestamps[-1])[:16], "Symbol": sym, "Action": "SELL (END)", "Price": round(last_price,4), "Qty": round(pos["qty"],6), "Cost/Proceeds": round(proceeds,2), "P&L ($)": pl_usd, "P&L (%)": f"{pl_pct*100:+.2f}%", "Cash After": round(cash,2)})
                final_equity = cash
                total_pl = round(final_equity - p_capital, 2)
                total_return = round(total_pl / p_capital * 100, 2)
                df_trades = pd.DataFrame(trade_log)
                df_equity = pd.DataFrame(equity_curve)
                sells_all = [t for t in trade_log if "SELL" in t["Action"]]
                wins_all = [t for t in sells_all if t["P&L ($)"] > 0]
                losses_all = [t for t in sells_all if t["P&L ($)"] <= 0]
                win_rate = round(len(wins_all) / len(sells_all) * 100, 1) if sells_all else 0
                avg_win = round(sum(t["P&L ($)"] for t in wins_all) / len(wins_all), 2) if wins_all else 0
                avg_loss = round(sum(t["P&L ($)"] for t in losses_all) / len(losses_all), 2) if losses_all else 0
                eq_vals = df_equity["equity"].values
                peak_eq = eq_vals[0]
                max_dd = 0.0
                for eq in eq_vals:
                    peak_eq = max(peak_eq, eq)
                    dd = (peak_eq - eq) / peak_eq * 100
                    max_dd = max(max_dd, dd)

            st.write("### 🏆 Portfolio Results")
            r1, r2, r3, r4, r5, r6 = st.columns(6)
            r1.metric("Final Equity", f"${final_equity:,.2f}", delta=f"${total_pl:+,.2f}")
            r2.metric("Total Return", f"{total_return:+.2f}%")
            r3.metric("Win Rate", f"{win_rate}%")
            r4.metric("Max Drawdown", f"-{max_dd:.2f}%")
            r5.metric("Avg Win", f"${avg_win:+.2f}")
            r6.metric("Avg Loss", f"${avg_loss:.2f}")
            r7, r8, r9 = st.columns(3)
            r7.metric("Total Trades", len(sells_all))
            r8.metric("Wins", len(wins_all))
            r9.metric("Losses", len(losses_all))
            st.write("### 📈 Portfolio Equity Curve")
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(x=df_equity["timestamp"], y=df_equity["equity"], mode="lines", name="Portfolio Equity", line=dict(color="#4f8ef7", width=2), fill="tozeroy", fillcolor="rgba(79,142,247,0.08)"))
            fig_eq.add_hline(y=p_capital, line_dash="dot", line_color="gray", annotation_text=f"Start: ${p_capital:,}")
            fig_eq.update_layout(height=380, template="plotly_dark", yaxis_title="Portfolio Value ($)", margin=dict(l=0,r=0,t=30,b=0))
            st.plotly_chart(fig_eq, use_container_width=True)
            st.write("### 💵 Cash & Open Positions Over Time")
            fig_cash = go.Figure()
            fig_cash.add_trace(go.Scatter(x=df_equity["timestamp"], y=df_equity["cash"], mode="lines", name="Cash", line=dict(color="#f0a500", width=1.5)))
            fig_cash.add_trace(go.Bar(x=df_equity["timestamp"], y=df_equity["positions"], name="Open Positions", yaxis="y2", marker_color="rgba(100,200,100,0.3)"))
            fig_cash.update_layout(height=280, template="plotly_dark", margin=dict(l=0,r=0,t=30,b=0), yaxis=dict(title="Cash ($)"), yaxis2=dict(title="# Positions", overlaying="y", side="right", showgrid=False), legend=dict(orientation="h", yanchor="bottom", y=1.02))
            st.plotly_chart(fig_cash, use_container_width=True)
            st.write("### 📋 Per-Symbol P&L (Shared Capital)")
            sym_rows = sorted([{"Symbol": s, "Realized P&L ($)": round(v, 2), "Result": "🟢 Profit" if v > 0 else ("🔴 Loss" if v < 0 else "⚪ Flat")} for s, v in sym_pl.items() if v != 0.0], key=lambda x: x["Realized P&L ($)"], reverse=True)
            if sym_rows:
                st.dataframe(pd.DataFrame(sym_rows), use_container_width=True, hide_index=True)
            with st.expander("📋 Full Portfolio Trade Log", expanded=False):
                if not df_trades.empty:
                    st.dataframe(df_trades, use_container_width=True)
                else:
                    st.info("No trades executed.")
    elif not run_portfolio:
        st.info("👆 Configure settings above and click **Run Portfolio Backtest**.")

# ─────────────────────────────────────────────
# AUTO-REFRESH (dashboard only)
# ─────────────────────────────────────────────
time.sleep(SCAN_INTERVAL)
st.rerun()