import streamlit as st
import pandas as pd
import yfinance as yf
import time
from datetime import datetime, timedelta
import pytz
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# 1. SETUP & CONFIGURATION
INITIAL_EQUITY_USD = 100844.25  
TRADE_LIMIT_USD = 100.0
CASH_BUFFER_USD = 50000.0       
SGT = pytz.timezone('Asia/Singapore')

# FETCH SECRETS
try:
    ALPACA_API_KEY = st.secrets["ALPACA_API_KEY"]
    ALPACA_SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
except Exception as e:
    st.error("Missing Alpaca Secrets! Check your Streamlit Cloud Settings.")
    st.stop()

SHARIAH_STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AVGO", "ASML", "AMD", "INTC", "ADBE", "CRM", 
    "TXN", "QCOM", "AMAT", "LRCX", "MU", "ADI", "KLAC", "SNOW", "PLTR", "PANW", 
    "FTNT", "ZS", "DDOG", "NET", "OKTA", "MDB", "TEAM", "WDAY", "NOW", "SHOP",
    "LLY", "JNJ", "AMGN", "VRTX", "REGN", "MRNA", "ISRG", "GILD", "TMO", "DHR", 
    "IDXX", "A", "BIIB", "BSX", "ZTS", "EW", "ALGN", "DXCM", "MTD", "RMD",
    "EOG", "SLB", "COP", "HAL", "XOM", "CVX", "UPS", "FDX", "CAT", 
    "DE", "HON", "LMT", "GD", "NOC", "TSLA", "LOW", "TJX", "COST", "AZO", 
    "ORLY", "NKE", "SBUX", "CMG", "EL"
]

# 2. SESSION INITIALIZATION
if 'portfolio_tracker' not in st.session_state:
    st.session_state.portfolio_tracker = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    st.session_state.entry_prices = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    st.session_state.nightly_start_usd = INITIAL_EQUITY_USD

# 3. DASHBOARD UI
st.set_page_config(page_title="Alpaca AI Scalper (USD)", layout="wide")
st.title("🌙 Alpaca AI Scalper - USD Dashboard")

# --- MORNING REPORT SECTION ---
st.write("---")
try:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    
    # Logic to fetch and sum the 3:45 AM SGT (19:45 UTC) SELL orders
    # ... (Rest of the report code from my previous message)
except Exception as e:
    st.write(f"Morning Report Syncing... {e}")
st.write("---")

try:
    # 1. ACCOUNT OVERVIEW - FETCHING TRUTH FROM ALPACA
    account = trading_client.get_account()
    current_cash_usd = float(account.cash)
    mkt_val_usd = float(account.long_market_value)
    total_equity_usd = float(account.equity)
    
    # FETCH ALL POSITIONS TO CALCULATE TOTAL UNREALIZED P&L
    positions = trading_client.get_all_positions()
    total_unrealized_pl = sum(float(p.unrealized_pl) for p in positions) if positions else 0.0
    
    # NIGHTLY PROGRESS CALCULATION
    if 'nightly_start_usd' not in st.session_state:
        st.session_state.nightly_start_usd = total_equity_usd
        
    nightly_pnl = total_equity_usd - st.session_state.nightly_start_usd
    
    # 2. RENDER THE 4 METRIC PILLARS
    m1, m2, m3, m4 = st.columns(4)
    
    m1.metric("Alpaca Cash (USD)", f"${current_cash_usd:,.2f}")
    
    m2.metric("Market Value (USD)", f"${mkt_val_usd:,.2f}")

    # GRAND TOTAL: Shows total wealth and nightly gain/loss
    m3.metric("GRAND TOTAL (USD)", f"${total_equity_usd:,.2f}", 
              delta=round(nightly_pnl, 2)) # Numeric delta ensures correct Red/Green color
    
    # UNREALIZED P&L: Shows paper profit/loss of current holdings
    # Passing a float to delta automatically handles the Red/Down or Green/Up coloring
    m4.metric("Unrealized P&L", f"${total_unrealized_pl:,.2f}", 
              delta=f"{((total_unrealized_pl/total_equity_usd)*100):.2f}%" if total_equity_usd > 0 else "0.00%")
    


    if st.button("Reset Nightly Start Point"):
        st.session_state.nightly_start_usd = total_equity_usd
        st.rerun()

    st.write("---")

    col_l, col_r = st.columns(2)
    with col_l:
        st.subheader("📈 Current Holdings")
        positions = trading_client.get_all_positions()
        if positions:
            holdings_df = pd.DataFrame([{
                "Symbol": p.symbol, "Qty": p.qty,
                "Avg Entry": f"${float(p.avg_entry_price):,.2f}",
                "Current Price": f"${float(p.current_price):,.2f}",
                "Unrealized P&L": f"${float(p.unrealized_pl):,.2f}"
            } for p in positions])
            st.table(holdings_df)
        else:
            st.info("No active positions.")

    with col_r:
        st.subheader("📜 Recent Trade Activity")
        order_filter = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=10)
        orders = trading_client.get_orders(order_filter)
        if orders:
            logs_df = pd.DataFrame([{
                "Time (UTC)": o.filled_at.strftime('%H:%M:%S') if o.filled_at else "Pending",
                "Symbol": o.symbol, "Side": o.side.value.upper(),
                "Qty": o.filled_qty, "Price": f"${float(o.filled_avg_price):,.2f}" if o.filled_avg_price else "N/A"
            } for o in orders])
            st.dataframe(logs_df, width='stretch')
        else:
            st.info("No recent filled orders.")

except Exception as e:
    st.error(f"Sync Error: {e}")

# 4. TRADING ENGINE
def execute_trade(ticker, action, price_usd):
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    qty = max(1, int(TRADE_LIMIT_USD / price_usd))
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY))
        if action == "BUY":
            st.session_state.portfolio_tracker[ticker] += qty
            st.session_state.entry_prices[ticker] = price_usd
        else:
            st.session_state.portfolio_tracker[ticker] = 0.0
            st.session_state.entry_prices[ticker] = 0.0
        st.toast(f"✅ {action} {ticker}")
        time.sleep(0.5) 
    except Exception as e:
        st.error(f"Alpaca Order Error: {e}")

# NEW: LIQUIDATION FUNCTION
def liquidate_all():
    st.warning("⚠️ Market closing soon. Liquidating all positions...")
    positions = trading_client.get_all_positions()
    for p in positions:
        try:
            trading_client.submit_order(MarketOrderRequest(
                symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
            ))
            st.session_state.portfolio_tracker[p.symbol] = 0.0
            time.sleep(0.5)
        except Exception as e:
            st.error(f"Error liquidating {p.symbol}: {e}")
    st.success("Liquidation complete. Fresh start ready for next session.")

def check_market_closing():
    now_sgt = datetime.now(SGT)
    # Market closes at 4:00 AM SGT (Adjust to 5:00 AM if DST is off)
    # We trigger liquidation at 3:45 AM SGT
    if now_sgt.hour == 3 and now_sgt.minute >= 45:
        return True
    return False

# 5. SCANNER LOOP
st.write("---")
st.subheader("📡 Live Signal Tracker")
signal_placeholder = st.empty()

while True:
    # Check for liquidation first
    if check_market_closing():
        liquidate_all()
        st.info("Scanner paused until next session.")
        time.sleep(3600) # Sleep for an hour until market is fully closed
        st.rerun()

    current_signals = []
    with st.status(f"🚀 Scanning... ({datetime.now(SGT).strftime('%H:%M:%S')} SGT)", expanded=False):
        for stock in SHARIAH_STOCKS:
            try:
                data = yf.download(stock, period="1d", interval="1m", progress=False)
                if not data.empty and len(data) >= 20:
                    if isinstance(data.columns, pd.MultiIndex):
                        data.columns = data.columns.get_level_values(0)
                    curr_p = float(data['Close'].iloc[-1])
                    s_ma = data['Close'].rolling(5).mean().iloc[-1]
                    l_ma = data['Close'].rolling(20).mean().iloc[-1]
                    
                    trend = "🟢 Bullish" if s_ma > l_ma else "🔴 Bearish"
                    current_signals.append({"Ticker": stock, "Price": round(curr_p, 2), "Trend": trend})

                    if st.session_state.portfolio_tracker[stock] > 0:
                        entry_p = st.session_state.entry_prices[stock]
                        if (curr_p - entry_p) / entry_p >= 0.02 or s_ma < l_ma:
                            execute_trade(stock, "SELL", curr_p)
                    elif s_ma > l_ma and current_cash_usd > CASH_BUFFER_USD:
                        execute_trade(stock, "BUY", curr_p)
            except: continue
    
    if current_signals:
        signal_placeholder.dataframe(pd.DataFrame(current_signals), width='stretch')
    
    time.sleep(10)
    st.rerun()
