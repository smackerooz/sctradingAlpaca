import streamlit as st
import pytz
import time
import pandas as pd
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# --- 0. INITIALIZE CLIENT ---
try:
    API_KEY = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
except Exception:
    st.error("Check Streamlit Secrets for API Keys.")

# --- 1. SETUP & CONFIGURATION ---
SGT = pytz.timezone('Asia/Singapore')
NIGHTLY_TARGET_USD = 150.0  # Approx 200 SGD
CASH_BUFFER_USD = 50000.0

try:
    account = trading_client.get_account()
    INITIAL_EQUITY_USD = float(account.last_equity)
except Exception:
    INITIAL_EQUITY_USD = 100844.25 

# --- 2. SIDEBAR CONTROLS ---
st.sidebar.header("🕹️ Command Center")
st.sidebar.metric("Starting Baseline", f"${INITIAL_EQUITY_USD:,.2f}")
st.sidebar.write("---")

if st.sidebar.button("🧹 EMERGENCY: CANCEL & LIQUIDATE"):
    trading_client.cancel_orders()
    pos = trading_client.get_all_positions()
    for p in pos:
        limit_p = round(float(p.current_price) - 0.03, 2)
        trading_client.submit_order(LimitOrderRequest(
            symbol=p.symbol, qty=p.qty, side=OrderSide.SELL,
            limit_price=limit_p, time_in_force=TimeInForce.DAY, extended_hours=True
        ))
    st.sidebar.success("Liquidation triggered.")

# --- 3. LIVE PERFORMANCE & TARGET TRACKING ---
st.write(f"## 🎯 Target: ${NIGHTLY_TARGET_USD} USD (~200 SGD)")

try:
    acc = trading_client.get_account()
    curr_equity = float(acc.equity)
    realized_pl = curr_equity - INITIAL_EQUITY_USD
    progress_pct = min(max(realized_pl / NIGHTLY_TARGET_USD, 0.0), 1.0) if realized_pl > 0 else 0.0

    col1, col2, col3 = st.columns(3)
    col1.metric("Current Equity", f"${curr_equity:,.2f}")
    col2.metric("Net Profit (USD)", f"${realized_pl:,.2f}", delta=f"{((realized_pl/INITIAL_EQUITY_USD)*100):.2f}%")
    col3.metric("Goal Progress", f"{int(progress_pct * 100)}%")
    
    st.progress(progress_pct)

    # Detailed Realized P&L Table
    st.write("### 💵 Realized P&L Breakdown")
    order_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100)
    closed_orders = trading_client.get_orders(order_filter)
    
    if closed_orders:
        pl_data = []
        for o in closed_orders:
            if o.side == OrderSide.SELL and o.filled_at:
                # Basic calculation: this assumes a simple buy/sell match
                # For high-accuracy P&L, Alpaca's 'get_portfolio_history' is used
                val = float(o.filled_avg_price) * float(o.filled_qty)
                pl_data.append({
                    "Time": o.filled_at.astimezone(SGT).strftime('%H:%M'),
                    "Symbol": o.symbol,
                    "Proceeds": f"${val:,.2f}",
                    "Status": "Closed"
                })
        st.dataframe(pd.DataFrame(pl_data), use_container_width=True)

except Exception as e:
    st.info("Performance data loading...")

# --- 4. HOLDINGS & ACTIVITY ---
st.write("---")
col_left, col_right = st.columns(2)

with col_left:
    st.write("### 📦 Live Holdings")
    pos = trading_client.get_all_positions()
    if pos:
        st.table(pd.DataFrame([{
            "Symbol": p.symbol, "Qty": p.qty, "Value": f"${float(p.market_value):,.2f}"
        } for p in pos]))
    else:
        st.write("No active positions.")

with col_right:
    st.write("### 📜 Recent Trades")
    log_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=10)
    recent = trading_client.get_orders(log_filter)
    if recent:
        st.table(pd.DataFrame([{
            "Sym": o.symbol, "Side": str(o.side).split('.')[-1], "Price": f"${float(o.filled_avg_price):.2f}"
        } for o in recent]))

# --- 5. THE SIGNAL & TRADING ENGINE ---
def run_trading_strategy():
    """
    THIS IS WHERE THE SIGNALS LIVE
    """
    try:
        # A. Check Cash Buffer Safety
        acc = trading_client.get_account()
        available_cash = float(acc.cash)
        
        if available_cash <= CASH_BUFFER_USD:
            return # Stop trading if we hit the $50k floor

        # B. Signal Monitoring (Example Logic)
        # You will insert your Bullish/Bearish indicators here
        # is_bullish = check_signals(...) 
        
        # Example Buy Trigger:
        # if is_bullish:
        #     trading_client.submit_order(MarketOrderRequest(
        #         symbol="AAPL", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
        #     ))

    except Exception as e:
        print(f"Strategy Error: {e}")

# --- 6. BACKGROUND EXECUTION ---
st.write("---")
st.write("📡 **Bot Status:** Scanning for Bullish/Bearish signals...")

while True:
    try:
        now = datetime.now(SGT)
        
        # 3:45 AM Liquidation Logic
        if now.hour == 3 and 45 <= now.minute < 55:
            p = trading_client.get_all_positions()
            for pos in p:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=pos.symbol, qty=pos.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
            time.sleep(600)
            continue

        run_trading_strategy()
        time.sleep(15) # Pulse every 15 seconds

    except Exception as e:
        time.sleep(30)
