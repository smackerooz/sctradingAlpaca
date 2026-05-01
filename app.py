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
st.set_page_config(page_title="Super 74 Bot V3 FIXED", layout="wide")

SGT = pytz.timezone("Asia/Singapore")

SCAN_INTERVAL = 20
MAX_TRADES_PER_DAY = 15
MAX_OPEN_POSITIONS = 5
RISK_PER_TRADE = 0.004
COOLDOWN_SECONDS = 600

# ✅ FIXED WATCHLIST (no broken strings)
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
# INIT CLIENTS
# ─────────────────────────────────────────────
try:
    API_KEY = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]

    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
except Exception as e:
    st.error(f"❌ Credential Error: {e}")
    st.stop()

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
    try:
        st.session_state.daily_baseline = float(trading_client.get_account().equity)
    except:
        st.session_state.daily_baseline = 0

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
# FETCH MARKET DATA (BATCHED)
# ─────────────────────────────────────────────
@st.cache_data(ttl=20)
def fetch_market_data(symbols):
    try:
        bars = data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(minutes=60)
            )
        )
        return bars.df
    except Exception:
        return pd.DataFrame()

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def calculate_vwap(df):
    pv = (df['close'] * df['volume']).cumsum()
    vol = df['volume'].cumsum()
    return pv / vol

# ─────────────────────────────────────────────
# TREND SCORING
# ─────────────────────────────────────────────
def compute_trends(df_all):

    results = {}

    if df_all.empty:
        return results

    for symbol in WATCHLIST:
        try:
            df = df_all.xs(symbol)

            if len(df) < 40:
                results[symbol] = (0, "N/A")
                continue

            df = df.copy()
            df['vwap'] = calculate_vwap(df)

            curr = df['close'].iloc[-1]
            ma_short = df['close'].tail(15).mean()
            ma_long = df['close'].tail(40).mean()

            score = 0

            if ma_short > ma_long:
                score += 30
            if curr > df['vwap'].iloc[-1]:
                score += 25
            if df['close'].iloc[-1] > df['close'].iloc[-2] > df['close'].iloc[-3]:
                score += 25
            if df['volume'].iloc[-1] > df['volume'].mean():
                score += 20

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

            results[symbol] = (score, label)

        except:
            results[symbol] = (0, "N/A")

    return results

# ─────────────────────────────────────────────
# MAIN UI
# ─────────────────────────────────────────────
st.write("🚀 Bot Running...")

with st.spinner("Fetching market data..."):
    df_all = fetch_market_data(WATCHLIST)

trend_map = compute_trends(df_all)

# ─────────────────────────────────────────────
# ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account = trading_client.get_account()
    positions = trading_client.get_all_positions()
except Exception as e:
    st.error(f"❌ Alpaca Error: {e}")
    st.stop()

cash = float(account.cash)
equity = float(account.equity)
holdings_value = sum(float(p.market_value) for p in positions)
num_positions = len(positions)
daily_delta = equity - st.session_state.daily_baseline

# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────
col1, col2, col3, col4 = st.columns(4)

col1.metric("💰 Cash", f"${cash:,.2f}")
col2.metric("📦 Holdings", f"${holdings_value:,.2f}")
col3.metric("📊 Daily Δ", f"${daily_delta:,.2f}")
col4.metric("🔢 Positions", num_positions)

# ─────────────────────────────────────────────
# TREND TABLE
# ─────────────────────────────────────────────
trend_rows = []

for s in WATCHLIST:
    score, label = trend_map.get(s, (0, "N/A"))
    trend_rows.append({"Symbol": s, "Score": score, "Trend": label})

df_trend = pd.DataFrame(trend_rows)

def color(val):
    if "Bullish" in val:
        return "color: green"
    elif "Bearish" in val:
        return "color: red"
    return ""

st.write("## 📊 Trend Dashboard")
st.dataframe(df_trend.style.map(color, subset=["Trend"]), height=400)

# ─────────────────────────────────────────────
# BOT LOGIC
# ─────────────────────────────────────────────
for symbol, (score, label) in trend_map.items():

    if score >= 60 and st.session_state.trade_count < MAX_TRADES_PER_DAY:

        if symbol in st.session_state.cooldown:
            continue

        try:
            price = df_all.xs(symbol)['close'].iloc[-1]

            qty = int((equity * RISK_PER_TRADE) / (price * 0.005))

            if qty > 0:
                trading_client.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                        extended_hours=True
                    )
                )

                st.session_state.trade_count += 1
                st.session_state.cooldown[symbol] = datetime.now(SGT)

                log(f"🟢 BUY {symbol} ({label})")

        except:
            continue

# ─────────────────────────────────────────────
# LOGS
# ─────────────────────────────────────────────
st.write("## 📋 Logs")
for l in st.session_state.log:
    st.text(l)

# ─────────────────────────────────────────────
# LOOP
# ─────────────────────────────────────────────
time.sleep(SCAN_INTERVAL)
st.rerun()
