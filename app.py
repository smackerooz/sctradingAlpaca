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
st.set_page_config(page_title="Super 74 Bot V2.1", layout="wide")

SGT = pytz.timezone("Asia/Singapore")

SCAN_INTERVAL = 20
MAX_TRADES_PER_DAY = 15
MAX_OPEN_POSITIONS = 5
RISK_PER_TRADE = 0.004  # slightly lower per trade
COOLDOWN_SECONDS = 600

WATCHLIST = [
"AAPL","MSFT","NVDA","TSLA","META","AMZN","AMD","GOOGL","AVGO","NFLX",
"INTC","QCOM","TXN","ADBE","CRM","CSCO","ASML","MU","AMAT",
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
# INDICATORS
# ─────────────────────────────────────────────
def calculate_vwap(df):
    pv = (df['close'] * df['volume']).cumsum()
    vol = df['volume'].cumsum()
    return pv / vol

# ─────────────────────────────────────────────
# TREND CHECK (FOR DASHBOARD)
# ─────────────────────────────────────────────
def get_trend(symbol):
    try:
        bars = data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(minutes=30)
            )
        )
        df = bars.df
        if df.empty or len(df) < 20:
            return "Neutral"

        ma_short = df['close'].tail(10).mean()
        ma_long = df['close'].tail(20).mean()

        return "Bullish" if ma_short > ma_long else "Bearish"
    except:
        return "N/A"

# ─────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────
def run_bot():

    account = trading_client.get_account()
    equity = float(account.equity)

    positions = trading_client.get_all_positions()
    open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    open_symbols = {o.symbol for o in open_orders}

    for symbol in WATCHLIST:

        if len(positions) >= MAX_OPEN_POSITIONS:
            break

        if st.session_state.trade_count >= MAX_TRADES_PER_DAY:
            break

        if symbol in open_symbols:
            continue

        last_trade = st.session_state.cooldown.get(symbol)
        if last_trade and (datetime.now(SGT) - last_trade).seconds < COOLDOWN_SECONDS:
            continue

        try:
            bars = data_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=TimeFrame.Minute,
                    start=datetime.utcnow() - timedelta(minutes=20)
                )
            )

            df = bars.df
            if df.empty or len(df) < 10:
                continue

            df['vwap'] = calculate_vwap(df)

            curr = df['close'].iloc[-1]
            prev = df['close'].iloc[-2]

            vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].mean()

            # ⚡ SCALPING ENTRY (LESS STRICT)
            if curr > prev and curr > df['vwap'].iloc[-1] * 0.998:

                risk = equity * RISK_PER_TRADE
                qty = int(risk / (curr * 0.005))

                if qty <= 0:
                    continue

                trading_client.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY
                    )
                )

                st.session_state.trade_count += 1
                st.session_state.cooldown[symbol] = datetime.now(SGT)
                st.session_state.peak_prices[symbol] = curr

                log(f"🟢 BUY {symbol}")

        except:
            continue

    # TRAILING EXIT
    for p in positions:
        symbol = p.symbol
        curr = float(p.current_price)
        entry = float(p.avg_entry_price)

        peak = max(st.session_state.peak_prices.get(symbol, entry), curr)
        st.session_state.peak_prices[symbol] = peak

        if curr < peak * 0.995:
            trading_client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=p.qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
            )
            log(f"📉 SELL {symbol}")

# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────
st.title("🚀 Super 74 Bot V2.1 (Scalping Mode)")

# 📊 TREND DASHBOARD
st.write("## 📊 Market Trend (74 Stocks)")

trend_data = []
for s in WATCHLIST:
    trend = get_trend(s)
    trend_data.append({"Symbol": s, "Trend": trend})

df_trend = pd.DataFrame(trend_data)

def color_trend(val):
    if val == "Bullish":
        return "color: green"
    elif val == "Bearish":
        return "color: red"
    return ""

st.dataframe(df_trend.style.map(color_trend, subset=["Trend"]), height=400)

# 📋 LOG
