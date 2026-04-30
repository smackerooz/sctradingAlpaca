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
    st.error("Missing Alpaca API Keys in Streamlit Secrets.")

# --- 1. CONFIGURATION ---
SGT = pytz.timezone('Asia/Singapore')
TARGET_PROFIT_USD = 150.0  # Approx 200 SGD
CASH_BUFFER_USD = 90000.0 

try:
    account = trading_client.get_account()
    PREVIOUS_CLOSE_EQUITY = float(account.last_equity)
    CURRENT_CASH = float(account.cash)
    CURRENT_EQUITY = float(account.equity)
except Exception:
    PREVIOUS_CLOSE_EQUITY = 100844.25
    CURRENT_CASH = 0.0
    CURRENT_EQUITY = 0.0

# --- 2. SIDEBAR ---
st.sidebar.header("🕹️ Bot Controls")
st.sidebar.metric("Yesterday's Close (Truth)", f"${PREVIOUS_CLOSE_EQUITY:,.2f}")
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
        st.sidebar.success("Orders sent.")
    except Exception as e:
        st.sidebar.error(f"Error: {e}")

# --- 3. LIVE SCORECARD ---
st.write(f"## 🎯 Goal: ${TARGET_PROFIT_USD} USD (~200 SGD)")
realized_pl_total = CURRENT_EQUITY - PREVIOUS_CLOSE_EQUITY
progress_pct = min(max(realized_pl_total / TARGET_PROFIT_USD, 0.0), 1.0) if realized_pl_total > 0 else 0.0

c1, c2, c3 = st.columns(3)
c1.metric("Total Equity", f"${CURRENT_EQUITY:,.2f}", delta=f"${realized_pl_total:,.2f}")
c2.metric("Cash Balance", f"${CURRENT_CASH:,.2f}")
c3.metric("Goal Progress", f"{int(progress_pct * 100)}%")
st.progress(progress_pct)

# --- 4. MORNING REPORT (SUMMARY VERSION) ---
st.write("---")
with st.expander("📊 Morning Report Summary", expanded=True):
    try:
        order_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200)
        closed_orders = trading_client.get_orders(order_filter)
        
        # Filtering for trades closed today (SGT)
        today_date = datetime.now(SGT).date()
        daily_trades = [o for o in closed_orders if o.filled_at and o.filled_at.astimezone(SGT).date() == today_date]
        
        if daily_trades:
            total_vol = sum(float(o.filled_avg_price) * float(o.filled_qty) for o in daily_trades if o.side == OrderSide.SELL)
            trade_count = len(daily_trades)
            
            # Summary Metrics
            sm1, sm2, sm3 = st.columns(3)
            sm1.write(f"**Trades Today:** {trade_count}")
            sm2.write(f"**Total Volume:** ${total_vol:,.2f}")
            sm3.write(f"**Status:** {'✅ Target Met' if realized_pl_total >= TARGET_PROFIT_USD else '⏳ Trading'}")
            
            # Brief table of the last 5 major exits
            st.write("**Recent Major Exits:**")
            summary_df = pd.DataFrame([{
                "Symbol": o.symbol, "Qty": o.filled_qty, "Value": f"${(float(o.filled_avg_price)*float(o.filled_qty)):,.2f}"
            } for o in daily_trades[:5]])
            st.table(summary_df)
        else:
            st.info("No trades completed today yet.")
    except Exception as e:
        st.write(f"Gathering report data... {e}")

# --- 5. HOLDINGS & P&L ---
st.write("### 📦 Live Holdings & Unrealized P&L")
try:
    positions = trading_client.get_all_positions()
    if positions:
        st.table(pd.DataFrame([{
            "Symbol": p.symbol, "Qty": p.qty, "Market Value": f"${float(p.market_value):,.2f}",
            "Unrealized P&L": f"${float(p.unrealized_pl):.2f}"
        } for p in positions]))
    else:
        st.success("Account is 100% Cash.")
except Exception:
    st.info("No active positions.")

# --- 6. BACKGROUND LOOP ---
def run_trading_strategy():
    # Signal monitoring logic goes here
    if CURRENT_CASH <= CASH_BUFFER_USD:
        return
    pass

while True:
    try:
        now = datetime.now(SGT)
        if now.hour == 3 and 45 <= now.minute < 55:
            p = trading_client.get_all_positions()
            for pos in p:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=pos.symbol, qty=pos.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
            time.sleep(600)
            continue
        run_trading_strategy()
        time.sleep(15) 
    except Exception:
        time.sleep(30)
