import streamlit as st
import pytz
import time
import pandas as pd
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Super 74 Bot V3", layout="wide")

SGT = pytz.timezone("Asia/Singapore")

SCAN_INTERVAL = 20
MAX_TRADES_PER_DAY = 15
MAX_OPEN_POSITIONS = 5
RISK_PER_TRADE = 0.004
COOLDOWN_SECONDS = 600

# ─────────────────────────────────────────────
# WATCHLIST (74)
# ─────────────────────────────────────────────
WATCHLIST = [
"AAPL","MSFT","NVDA","TSLA","META","AMZN","AMD","GOOGL","AVGO","ORCL",
"INTC","QCOM","TXN","ADBE","CRM","NFLX","CSCO","ASML","MU","AMAT",
"JPM","BAC","WMT","COST","PG","V","MA","UNH","HD","DIS",
"XOM","CVX","CAT","GE","BA","HON","MMM","UPS","FDX","LMT",
"ABBV","PEP","KO","PFE","TMO","LLY","AZN","NKE","SBUX","T",
"VZ","TMUS","PYPL","SQ","UBER","ABNB","SNOW","PLTR","BABA","JD",
"PDD","SHOP","LCID","RIVN","COIN","MSTR","MARA","RIOT","DKNG","PEN",
"ZM","ROKU","U","SNAP"
]

# ─────────────────────────────────────────────
# INIT
# ─────────────────────────────────────────────
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
if "trade_count" not in st.session_state:
    st.session_state.trade_count = 0

if "cooldown" not in st.session_state:
    st.session_state.cooldown = {}

if "peak_prices" not in st.session_state:
    st.session_state.peak_prices = {}

if "log" not in st.session_state:
    st.session_state.log = []

if "daily_baseline" not in st.session_state:
    st.session_state.daily_baseline = float(trading_client.get_account().equity)

# AUTO START
st.session_state.bot_running = True

# ─────────────────────────────────────────────
# LOG
# ─────────────────────────────────────────────
def log(msg):
    ts = datetime.now(SGT).strftime("%H:%M:%S")
    st.session_state.log.insert(0, f"[{ts}] {msg}")
    st.session_state.log = st.session_state.log[:50]

# ─────────────────────────────────────────────
# RESET DAILY BASELINE
# ─────────────────────────────────────────────
def reset_daily_baseline():
    clock = trading_client.get_clock()

    if clock.is_open and not st.session_state.get("baseline_reset_done", False):
        st.session_state.daily_baseline = float(trading_client.get_account().equity)
        st.session_state.baseline_reset_done = True

    if not clock.is_open:
        st.session_state.baseline_reset_done = False

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def calculate_vwap(df):
    pv = (df['close'] * df['volume']).cumsum()
    vol = df['volume'].cumsum()
    return pv / vol

# ─────────────────────────────────────────────
# TREND SCORING SYSTEM
# ─────────────────────────────────────────────
def get_trend_score(symbol):
    try:
        bars = data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(minutes=60)
            )
        )

        df = bars.df

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol)

        if df.empty or len(df) < 40:
            return 0, "N/A"

        df['vwap'] = calculate_vwap(df)

        curr = df['close'].iloc[-1]
        ma_short = df['close'].tail(15).mean()
        ma_long = df['close'].tail(40).mean()

        score = 0

        # MA Trend (30)
        if ma_short > ma_long:
            score += 30

        # VWAP (25)
        if curr > df['vwap'].iloc[-1]:
            score += 25

        # Momentum (25)
        if df['close'].iloc[-1] > df['close'].iloc[-2] > df['close'].iloc[-3]:
            score += 25

        # Volume (20)
        if df['volume'].iloc[-1] > df['volume'].mean():
            score += 20

        # LABEL
        if score >= 80:
            label = "Strong Bullish"
        elif score >= 60:
            label = "Bullish"
        elif score >= 40:
            label = "Neutral"
        elif score >= 20:
            label = "Bearish"
        else:
            label = "Strong Bearish"

        return score, label

    except:
        return 0, "N/A"

# ─────────────────────────────────────────────
# BOT
# ─
