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
st.set_page_config(page_title="Super 74 Bot V2", layout="wide")

SGT = pytz.timezone("Asia/Singapore")

TARGET_PROFIT = 150
MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 3
RISK_PER_TRADE = 0.005   # 0.5%
DAILY_STOP_LOSS = -1000
SCAN_INTERVAL = 60

WATCHLIST = ["AAPL","MSFT","NVDA","TSLA","META","AMZN","AMD","GOOGL","AVGO","NFLX"]

# ─────────────────────────────────────────────
# INIT CLIENTS
# ─────────────────────────────────────────────
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
if "bot_running" not in st.session_state:
    st.session_state.bot_running = False

if "trade_count" not in st.session_state:
    st.session_state.trade_count = 0

if "daily_pnl" not in st.session_state:
    st.session_state.daily_pnl = 0

if "cooldown" not in st.session_state:
    st.session_state.cooldown = {}

if "peak_prices" not in st.session_state:
    st.session_state.peak_prices = {}

if "log" not in st.session_state:
    st.session_state.log = []

# ─────────────────────────────────────────────
# LOGGING
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

def calculate_atr(df, period=14):
    df['h-l'] = df['high'] - df['low']
    df['h-c'] = abs(df['high'] - df['close'].shift())
    df['l-c'] = abs(df['low'] - df['close'].shift())
    tr = df[['h-l','h-c','l-c']].max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

# ─────────────────────────────────────────────
# MARKET FILTER (SPY)
# ─────────────────────────────────────────────
def market_is_bullish():
    try:
        bars = data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=["SPY"],
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(minutes=60)
            )
        )
        df = bars.df
        if df.empty or len(df) < 50:
            return False

        ma20 = df['close'].tail(20).mean()
        ma50 = df['close'].tail(50).mean()

        return ma20 > ma50
    except:
        return False

# ─────────────────────────────────────────────
# STRATEGY
# ─────────────────────────────────────────────
def run_bot():

    # Stop if hit daily loss
    if st.session_state.daily_pnl <= DAILY_STOP_LOSS:
        log("🛑 Daily loss hit. Stopping trading.")
        return

    # Market filter
    if not market_is_bullish():
        log("⏸ Market not bullish. Skipping trades.")
        return

    account = trading_client.get_account()
    equity = float(account.equity)

    positions = trading_client.get_all_positions()
    if len(positions) >= MAX_OPEN_POSITIONS:
        return

    open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    open_symbols = {o.symbol for o in open_orders}

    for symbol in WATCHLIST:

        # Limits
        if st.session_state.trade_count >= MAX_TRADES_PER_DAY:
            return

        if symbol in open_symbols:
            continue

        # Cooldown check
        last_trade = st.session_state.cooldown.get(symbol)
        if last_trade and (datetime.now(SGT) - last_trade).seconds < 1800:
            continue

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
                continue

            df['vwap'] = calculate_vwap(df)

            curr = df['close'].iloc[-1]
            prev1 = df['close'].iloc[-2]
            prev2 = df['close'].iloc[-3]

            vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].mean()

            atr = calculate_atr(df)

            # ENTRY CONDITIONS
            if (
                curr > df['vwap'].iloc[-1]
                and prev1 > prev2
                and curr > prev1
                and vol > avg_vol * 1.5
            ):

                stop_loss = curr - (1.2 * atr)
                risk_per_trade = equity * RISK_PER_TRADE

                qty = int(risk_per_trade / (curr - stop_loss))
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

                log(f"🟢 BUY {symbol} qty={qty}")

        except Exception as e:
            continue

    # MANAGE POSITIONS
    for p in positions:
        symbol = p.symbol
        entry = float(p.avg_entry_price)
        curr = float(p.current_price)

        peak = max(st.session_state.peak_prices.get(symbol, entry), curr)
        st.session_state.peak_prices[symbol] = peak

        # ATR-based trailing (approx)
        trail_price = peak * 0.98

        if curr < trail_price:
            trading_client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=p.qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
            )

            pnl = (curr - entry) * float(p.qty)
            st.session_state.daily_pnl += pnl

            log(f"📉 SELL {symbol} PnL={pnl:.2f}")

# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────
st.title("🚀 Super 74 Bot V2")

col1, col2, col3 = st.columns(3)

account = trading_client.get_account()

col1.metric("Equity", f"${float(account.equity):,.2f}")
col2.metric("Daily PnL", f"${st.session_state.daily_pnl:,.2f}")
col3.metric("Trades Today", st.session_state.trade_count)

if st.sidebar.button("Start"):
    st.session_state.bot_running = True

if st.sidebar.button("Stop"):
    st.session_state.bot_running = False

# Logs
st.write("### Logs")
for l in st.session_state.log:
    st.text(l)

# RUN LOOP
if st.session_state.bot_running:
    run_bot()
    time.sleep(SCAN_INTERVAL)
    st.rerun()
