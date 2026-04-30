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
except Exception as e:
    st.error("Missing Alpaca API Keys in Streamlit Secrets.")

# --- 1. SETUP & CONFIGURATION ---
SGT = pytz.timezone('Asia/Singapore')

try:
    account = trading_client.get_account()
    INITIAL_EQUITY_USD = float(account.last_equity)
except Exception:
    INITIAL_EQUITY_USD = 100844.25 

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
                limit_p = round(float(p.current_price) - 0.01, 2)
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
            st.sidebar.success("All orders sent. Check Alpaca app for fills.")
    except Exception as e:
        st.sidebar.error(f"Liquidation Error: {e}")

if st.sidebar.button("🧹 CANCEL & LIQUIDATE EVERYTHING"):
    st.sidebar.warning("Initiating full account clearing...")
    try:
        trading_client.cancel_orders()
        st.sidebar.write("✅ All open orders cancelled.")
        time.sleep(2) 
        positions = trading_client.get_all_positions()
        if not positions:
            st.sidebar.success("Account is already clear!")
        else:
            for p in positions:
                limit_p = round(float(p.current_price) - 0.03, 2)
                sell_req = LimitOrderRequest(
                    symbol=p.symbol, qty=p.qty, side=OrderSide.SELL,
                    limit_price=limit_p, time_in_force=TimeInForce.DAY, extended_hours=True 
                )
                trading_client.submit_order(sell_req)
                st.sidebar.write(f"📤 Sent: {p.symbol} at ${limit_p}")
            st.sidebar.success("Final liquidation sweep complete!")
    except Exception as e:
        st.sidebar.error(f"Shutdown Failed: {e}")

# --- 3. MORNING PERFORMANCE REPORT ---
st.write("## 📈 Nightly Performance Report")
try:
    acc = trading_client.get_account()
    current_equity = float(acc.equity)
    total_cash = float(acc.cash)
    
    nightly_delta = current_equity - INITIAL_EQUITY_USD
    nightly_pct = (nightly_delta / INITIAL_EQUITY_USD) * 100 if INITIAL_EQUITY_USD > 0 else 0

    col_a, col_b = st.columns(2)
    col_a.metric("Grand Total Equity", f"${current_equity:,.2f}", delta=f"${nightly_delta:,.2f}")
    col_b.metric("Nightly Progress", f"{nightly_pct:.2f}%")

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
            if time_diff.total_seconds() < 43200: 
                val = float(o.filled_avg_price) * float(o.filled_qty)
                order_time_sgt = o.filled_at.astimezone(SGT)
                is_liq = (order_time_sgt.hour == 3 and order_time_sgt.minute >= 45) and (order_time_sgt.date() == datetime.now(SGT).date())

                if o.side == OrderSide.BUY:
                    total_buy_vol += val
                elif o.side == OrderSide.SELL:
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
    st.info("Morning report will update as trades occur.")

# --- 4. LIVE HOLDINGS & TRADE LOG TABLES ---
st.write("---")
st.write("### 📦 Current Holdings (Live Inventory)")
try:
    positions = trading_client.get_all_positions()
    if positions:
        pos_list = []
        for p in positions:
            pos_list.append({
                "Symbol": p.symbol,
                "Qty": p.qty,
                "Entry Price": f"${float(p.avg_entry_price):.2f}",
                "Current Price": f"${float(p.current_price):.2f}",
                "Market Value": f"${float(p.market_value):.2f}",
                "Unrealized P&L": f"${float(p.unrealized_pl):.2f}"
            })
        st.table(pd.DataFrame(pos_list))
    else:
        st.success("✅ No positions held. Account is 100% Cash.")
except Exception as e:
    st.info("Searching for positions...")

st.write("### 📜 Recent Activity (Trade Log)")
try:
    log_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=20)
    recent_orders = trading_client.get_orders(log_filter)
    if recent_orders:
        log_list = []
        for o in recent_orders:
            log_list.append({
                "Time (SGT)": o.filled_at.astimezone(SGT).strftime('%H:%M:%S'),
                "Symbol": o.symbol,
                "Side": str(o.side).split('.')[-1],
                "Qty": o.filled_qty,
                "Price": f"${float(o.filled_avg_price):.2f}"
            })
        st.dataframe(pd.DataFrame(log_list), use_container_width=True)
except Exception:
    st.info("No recent trades to display.")

# --- 5. THE TRADING ENGINE (Resilient Loop) ---
def run_trading_strategy():
    # PASTE YOUR 'SUPER 74' SCANNER / TRADING LOGIC HERE
    pass

st.write("---")
st.write("📡 **Live Bot Status:** Monitoring Markets...")

while True:
    try:
        now_sgt = datetime.now(SGT)
        if now_sgt.hour == 3 and 45 <= now_sgt.minute < 55:
            pos = trading_client.get_all_positions()
            for p in pos:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
            time.sleep(600) 

        run_trading_strategy()
        time.sleep(15) 

    except Exception as e:
        print(f"Loop error: {e}")
        time.sleep(30)
