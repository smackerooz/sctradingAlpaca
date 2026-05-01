import streamlit as st
import pytz
import time
import pandas as pd
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, AssetClass
# CORRECTED IMPORTS BELOW
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- 0. INITIALIZE CLIENTS ---
try:
    API_KEY = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
except Exception:
    st.error("Missing Alpaca API Keys in Streamlit Secrets.")

# --- 1. CONFIGURATION & RESET LOGIC ---
SGT = pytz.timezone('Asia/Singapore')
TARGET_PROFIT_USD = 150.0  # Approx 200 SGD
CASH_BUFFER_USD = 90000.0 
WATCHLIST = ["AAPL", "TSLA", "NVDA", "MSFT", "AMD", "META", "GOOGL", "AMZN"] 

now = datetime.now(SGT)
if 'nightly_baseline' not in st.session_state:
    try:
        st.session_state.nightly_baseline = float(trading_client.get_account().last_equity)
    except:
        st.session_state.nightly_baseline = 100000.0

if now.hour == 21 and now.minute == 30:
    st.session_state.nightly_baseline = float(trading_client.get_account().equity)

# --- 2. SIDEBAR CONTROLS ---
st.sidebar.header("🕹️ Bot Controls")
st.sidebar.metric("Session Baseline", f"${st.session_state.nightly_baseline:,.2f}")
if st.sidebar.button("🧹 MANUAL LIQUIDATION (EXTENDED)"):
    try:
        trading_client.cancel_orders()
        time.sleep(1)
        positions = trading_client.get_all_positions()
        for p in positions:
            limit_p = round(float(p.current_price) - 0.03, 2)
            trading_client.submit_order(LimitOrderRequest(
                symbol=p.symbol, qty=p.qty, side=OrderSide.SELL,
                limit_price=limit_p, time_in_force=TimeInForce.DAY, extended_hours=True
            ))
        st.sidebar.success("Liquidation orders sent.")
    except Exception as e:
        st.sidebar.error(f"Error: {e}")

# --- 3. LIVE SCORECARD (COMBINED DELTA) ---
try:
    account = trading_client.get_account()
    CURRENT_CASH = float(account.cash)
    CURRENT_EQUITY = float(account.equity)
    positions = trading_client.get_all_positions()
    unrealized_pl = round(sum(float(p.unrealized_pl) for p in positions), 2) if positions else 0.0
except:
    CURRENT_CASH, CURRENT_EQUITY, unrealized_pl = 0.0, 0.0, 0.0

total_net_performance = round(CURRENT_EQUITY - st.session_state.nightly_baseline, 2)
realized_pl = round(total_net_performance - unrealized_pl, 2)
progress_pct = min(max(realized_pl / TARGET_PROFIT_USD, 0.0), 1.0) if realized_pl > 0 else 0.0
combined_delta = round(unrealized_pl + realized_pl, 2)

st.write(f"## 🎯 Goal: ${TARGET_PROFIT_USD} USD (~200 SGD)")
c1, c2, c3 = st.columns(3)
c1.metric(label="Total Equity", value=f"${CURRENT_EQUITY:,.2f}", delta=float(combined_delta))
c2.metric("Cash Balance", f"${CURRENT_CASH:,.2f}")
c3.metric("Goal Progress", f"{int(progress_pct * 100)}%")
st.progress(progress_pct)

# --- 4. P&L SUMMARY INFO ---
st.write("### 💰 Profit & Loss Summary")
pl_col1, pl_col2 = st.columns(2)
pl_col1.metric("Realized P&L (Cash)", f"${realized_pl:,.2f}")
pl_col2.metric("Unrealized P&L (Paper)", f"${unrealized_pl:,.2f}")

# --- 5. MORNING REPORT (SUMMARY) ---
with st.expander("📊 Morning Report Summary", expanded=True):
    try:
        order_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200)
        closed_orders = trading_client.get_orders(order_filter)
        today_date = now.date()
        daily_trades = [o for o in closed_orders if o.filled_at and o.filled_at.astimezone(SGT).date() == today_date]
        if daily_trades:
            total_vol = sum(float(o.filled_avg_price) * float(o.filled_qty) for o in daily_trades if o.side == OrderSide.BUY)
            st.write(f"**Trades Today:** {len(daily_trades)} | **Buy Volume:** ${total_vol:,.2f}")
            st.table(pd.DataFrame([{"Symbol": o.symbol, "Qty": o.filled_qty, "Value": f"${(float(o.filled_avg_price)*float(o.filled_qty)):,.2f}"} for o in daily_trades[:5]]))
        else: st.info("No trades completed today yet.")
    except: st.write("Refreshing data...")

# --- 6. LIVE HOLDINGS & TREND (SCROLLABLE) ---
st.write("### 📦 Live Holdings & Trend Analysis")
if positions:
    pos_data = [{"Symbol": p.symbol, "Qty": p.qty, "Value": f"${float(p.market_value):,.2f}", "P&L ($)": f"${float(p.unrealized_pl):.2f}", "Trend (%)": f"{(float(p.unrealized_plpc)*100):+.2f}%"} for p in positions]
    st.dataframe(pd.DataFrame(pos_data), use_container_width=True, height=300)
else: st.success("Account is 100% Cash.")

# --- 7. AUTOMATED STRATEGY ENGINE ---
def run_trading_strategy():
    if CURRENT_CASH <= CASH_BUFFER_USD:
        return

    for symbol in WATCHLIST:
        try:
            # CORRECTED REQUEST CALL
            request_params = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                start=datetime.now() - timedelta(minutes=20)
            )
            bars = data_client.get_stock_bars(request_params)
            df = bars.df
            avg_price = df['close'].mean()
            current_p = df['close'].iloc[-1]

            if current_p > (avg_price * 1.005): # Trend logic
                if not any(p.symbol == symbol for p in positions):
                    trading_client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                    ))
        except: continue

# --- 8. BACKGROUND LOOP ---
st.write("---")
st.write("📡 **Live Bot Status:** Actively monitoring signals...")

if st.button("▶️ Run Strategy Scan Now"):
    run_trading_strategy()
    st.rerun()

while True:
    try:
        now_sgt = datetime.now(SGT)
        if now_sgt.hour == 3 and 45 <= now_sgt.minute < 55:
            p = trading_client.get_all_positions()
            for pos in p:
                trading_client.submit_order(MarketOrderRequest(symbol=pos.symbol, qty=pos.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
            time.sleep(600)
            continue
        time.sleep(15) 
        break 
    except: time.sleep(30)
