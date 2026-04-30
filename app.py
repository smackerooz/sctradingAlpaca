import streamlit as st
import pytz
import time
import pandas as pd
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# --- 0. INITIALIZE CLIENT ---
# Ensure your API Keys are set in Streamlit Cloud Secrets
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# --- 1. SETUP & CONFIGURATION ---
SGT = pytz.timezone('Asia/Singapore')

try:
    # Dynamically fetch "Truth" from yesterday's close for the baseline
    account = trading_client.get_account()
    INITIAL_EQUITY_USD = float(account.last_equity)
except Exception:
    INITIAL_EQUITY_USD = 100844.25 # Fallback baseline

# --- 2. SIDEBAR & EMERGENCY CONTROLS ---
st.sidebar.header("🕹️ Bot Control Panel")
st.sidebar.metric("Nightly Baseline", f"${INITIAL_EQUITY_USD:,.2f}")

st.sidebar.write("---")
st.sidebar.subheader("🌙 After-Hours Controls")
st.sidebar.info("Post-market is active until 8:00 AM SGT.")

if st.sidebar.button("FORCE POST-MARKET LIQUIDATION"):
    st.sidebar.warning("Executing emergency exit...")
    try:
        positions = trading_client.get_all_positions()
        if not positions:
            st.sidebar.success("No positions found!")
        else:
            for p in positions:
                # Post-market requires Limit Orders and extended_hours=True
                limit_p = float(p.current_price)
                sell_req = LimitOrderRequest(
                    symbol=p.symbol,
                    qty=p.qty,
                    side=OrderSide.SELL,
                    limit_price=limit_p,
                    time_in_force=TimeInForce.DAY,
                    extended_hours=True 
                )
                trading_client.submit_order(sell_req)
                st.sidebar.write(f"✅ Sent: {p.symbol} at ${limit_p}")
            st.sidebar.success("All orders sent. Monitor Alpaca app for fills.")
    except Exception as e:
        st.sidebar.error(f"Liquidation Error: {e}")

# --- 3. MORNING PERFORMANCE REPORT ---
st.write("## 📈 Nightly Performance Report")
try:
    # Refresh Account Data
    acc = trading_client.get_account()
    current_equity = float(acc.equity)
    total_cash = float(acc.cash)
    
    # Calculate Nightly Progress
    nightly_delta = current_equity - INITIAL_EQUITY_USD
    nightly_pct = (nightly_delta / INITIAL_EQUITY_USD) * 100

    col_a, col_b = st.columns(2)
    col_a.metric("Grand Total Equity", f"${current_equity:,.2f}", delta=f"${nightly_delta:,.2f}")
    col_b.metric("Nightly Progress", f"{nightly_pct:.2f}%")

    # Detailed Scorecard Logic
    order_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500)
    all_orders = trading_client.get_orders(order_filter)
    
    total_buy_vol = 0.0
    total_sell_vol = 0.0
    total_liq_vol = 0.0
    pl_trading = 0.0
    pl_liquidation = 0.0

    if all_orders:
        buys = [o for o in all_orders if o.side == OrderSide.BUY and o.filled_at]
        for o in all_orders:
            if not o.filled_at: continue
            
            time_diff = datetime.now(pytz.utc) - o.filled_at
            if time_diff.total_seconds() < 43200: # 12h window
                val = float(o.filled_avg_price) * float(o.filled_qty)
                order_time_sgt = o.filled_at.astimezone(SGT)
                
                # Check if trade happened in the 3:45 AM window
                is_liq = (order_time_sgt.hour == 3 and order_time_sgt.minute >= 45) and (order_time_sgt.date() == datetime.now(SGT).date())

                if o.side == OrderSide.BUY:
                    total_buy_vol += val
                elif o.side == OrderSide.SELL:
                    # Match entry price for P&L
                    entry_p = next((float(b.filled_avg_price) for b in buys if b.symbol == o.symbol and b.filled_at < o.filled_at), 0.0)
                    trade_pl = (float(o.filled_avg_price) - entry_p) * float(o.filled_qty) if entry_p > 0 else 0.0
                    
                    if is_liq:
                        total_liq_vol += val
                        pl_liquidation += trade_pl
                    else:
                        total_sell_vol += val
                        pl_trading += trade_pl

    with st.expander("📊 Detailed Nightly Scorecard", expanded=True):
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Buy Vol", f"${total_buy_vol:,.2f}")
        c2.metric("Total Sell Vol", f"${total_sell_vol:,.2f}")
        c3.metric("Trading P&L", f"${pl_trading:,.2f}")
        
        st.write("---")
        c4, c5 = st.columns(2)
        c4.metric("Liquidated at 3:45 AM", f"${total_liq_vol:,.2f}")
        c5.metric("Liquidation P&L", f"${pl_liquidation:,.2f}")
        
        st.write("---")
        st.metric("Final Cash Balance", f"${total_cash:,.2f}")

except Exception as e:
    st.info("Morning report will populate as trades are completed.")

# --- 4. THE TRADING ENGINE (Resilient Loop) ---
def run_trading_strategy():
    """
    PASTE YOUR 'SUPER 74' SCANNER AND BUY/SELL LOGIC HERE
    Example: 
    - Check prices
    - If condition met, submit_order
    """
    pass

# Main Execution
st.write("---")
st.write("📡 **Live Bot Status:** Active and monitoring...")

# Note: In Streamlit Cloud, the while loop runs as long as the app is awake.
# To ensure it stays awake, keep the browser tab open or use a ping service.
while True:
    try:
        now_sgt = datetime.now(SGT)
        
        # 1. 3:45 AM AUTOMATED LIQUIDATION
        if now_sgt.hour == 3 and now_sgt.minute >= 45 and now_sgt.minute < 55:
            pos = trading_client.get_all_positions()
            for p in pos:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
            print("Liquidation triggered at 3:45 AM")
            time.sleep(600) # Sleep 10 mins to avoid double triggering

        # 2. RUN YOUR STRATEGY
        run_trading_strategy()
        
        # 3. HEARTBEAT / THROTTLE
        time.sleep(15) 

    except Exception as e:
        print(f"Error in main loop: {e}")
        time.sleep(30) # Wait 30s before retry
