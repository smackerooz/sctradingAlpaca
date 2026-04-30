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

# --- 1. CONFIGURATION & TRUTH ANCHORING ---
SGT = pytz.timezone('Asia/Singapore')
TARGET_PROFIT_USD = 150.0  # Approx 200 SGD
CASH_BUFFER_USD = 90000.0  # New $90k Buffer

try:
    # Getting the "Truth" from yesterday's close
    account = trading_client.get_account()
    PREVIOUS_CLOSE_EQUITY = float(account.last_equity)
    CURRENT_CASH = float(account.cash)
    CURRENT_EQUITY = float(account.equity)
except Exception:
    PREVIOUS_CLOSE_EQUITY = 100844.25
    CURRENT_CASH = 0.0
    CURRENT_EQUITY = 0.0

# --- 2. SIDEBAR: CONTROLS & MANUAL LIQUIDATION ---
st.sidebar.header("🕹️ Bot Controls")
st.sidebar.metric("Yesterday's Close (Truth)", f"${PREVIOUS_CLOSE_EQUITY:,.2f}")
st.sidebar.write("---")

# Manual Emergency Button (Extended Hours Capable)
if st.sidebar.button("🧹 MANUAL LIQUIDATION (EXTENDED)"):
    st.sidebar.warning("Clearing all positions...")
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

# --- 3. LIVE SCORECARD & PROFIT TARGET ---
st.write(f"## 🎯 Goal: ${TARGET_PROFIT_USD} USD (~200 SGD)")
realized_pl_total = CURRENT_EQUITY - PREVIOUS_CLOSE_EQUITY
progress_pct = min(max(realized_pl_total / TARGET_PROFIT_USD, 0.0), 1.0) if realized_pl_total > 0 else 0.0

col1, col2, col3 = st.columns(3)
col1.metric("Total Value (Equity)", f"${CURRENT_EQUITY:,.2f}", delta=f"${realized_pl_total:,.2f}")
col2.metric("Cash Balance", f"${CURRENT_CASH:,.2f}")
col3.metric("Goal Progress", f"{int(progress_pct * 100)}%")
st.progress(progress_pct)

# --- 4. MORNING REPORT & REALIZED P&L ---
with st.expander("📊 Detailed Morning Report & Realized P&L", expanded=True):
    try:
        order_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100)
        closed_orders = trading_client.get_orders(order_filter)
        if closed_orders:
            # We filter for trades closed in the last 12 hours
            pl_list = []
            for o in closed_orders:
                if o.filled_at and (datetime.now(pytz.utc) - o.filled_at).total_seconds() < 43200:
                    pl_list.append({
                        "Time (SGT)": o.filled_at.astimezone(SGT).strftime('%H:%M:%S'),
                        "Symbol": o.symbol,
                        "Side": str(o.side).split('.')[-1].upper(),
                        "Qty": o.filled_qty,
                        "Price": f"${float(o.filled_avg_price):.2f}",
                        "Total Value": f"${(float(o.filled_avg_price) * float(o.filled_qty)):,.2f}"
                    })
            st.table(pd.DataFrame(pl_list))
        else:
            st.write("No trades closed this session yet.")
    except Exception:
        st.info("Loading trade history...")

# --- 5. HOLDINGS & UNREALIZED P&L ---
st.write("---")
st.write("### 📦 Live Holdings & Unrealized P&L")
try:
    positions = trading_client.get_all_positions()
    if positions:
        pos_df = pd.DataFrame([{
            "Symbol": p.symbol,
            "Qty": p.qty,
            "Market Value": f"${float(p.market_value):,.2f}",
            "Unrealized P&L": f"${float(p.unrealized_pl):.2f}",
            "Change %": f"{(float(p.unrealized_plpc)*100):.2f}%"
        } for p in positions])
        st.table(pos_df)
    else:
        st.success("Account is 100% Cash. No unrealized risk.")
except Exception:
    st.info("Searching for open positions...")

# --- 6. SIGNAL MONITORING & TRADING ENGINE ---
def run_trading_strategy():
    """
    ENGINE: MONITOR SIGNALS & EXECUTE
    """
    try:
        # SAFETY CHECK: Buffer of $90,000
        if CURRENT_CASH <= CASH_BUFFER_USD:
            # st.write("Safety Triggered: Cash below $90k buffer.")
            return

        # ---------------------------------------------------------
        # INSERT BULLISH / BEARISH SIGNAL LOGIC HERE
        # Example:
        # if signal == "BULLISH":
        #    # Submit Buy Order
        # elif signal == "BEARISH":
        #    # Submit Sell Order
        # ---------------------------------------------------------
        pass

    except Exception as e:
        print(f"Strategy Scan Error: {e}")

# --- 7. BACKGROUND LOOP ---
st.write("---")
st.write("📡 **Live Bot Status:** Actively monitoring bullish/bearish signals...")

while True:
    try:
        now_sgt = datetime.now(SGT)
        
        # AUTOMATED 3:45 AM LIQUIDATION (Truth/Safety)
        if now_sgt.hour == 3 and 45 <= now_sgt.minute < 55:
            pos = trading_client.get_all_positions()
            for p in pos:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
            time.sleep(600)
            continue

        run_trading_strategy()
        time.sleep(15) 

    except Exception as e:
        time.sleep(30)
