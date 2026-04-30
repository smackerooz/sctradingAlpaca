import streamlit as st
import pandas as pd
import yfinance as yf
import time
from datetime import datetime
import pytz
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# 1. SETUP & CONFIGURATION
# Your baseline for the "Nightly" calculation
INITIAL_BALANCE_SGD = 136144.32  
USD_SGD_RATE = 1.35  
TRADE_LIMIT_USD = 100.0
# UPDATED: Buffer changed to 50,000 as requested
CASH_BUFFER_SGD = 50000.0 
LOG_FILE = "trading_log.csv"
SGT = pytz.timezone('Asia/Singapore')

# FETCH SECRETS
ALPACA_API_KEY = st.secrets["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

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
if 'portfolio' not in st.session_state:
    st.session_state.portfolio = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    st.session_state.entry_prices = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    st.session_state.last_reset_date = datetime.now(SGT).date()

# 3. DASHBOARD UI
st.set_page_config(page_title="AI Shariah Trader", layout="wide")
st.title("🌙 Alpaca-Linked AI Scalper")

# TRUTH-FIRST METRICS FROM ALPACA API
try:
    account = trading_client.get_account()
    
    # Live data from Alpaca converted to SGD
    alpaca_cash_sgd = float(account.cash) * USD_SGD_RATE
    alpaca_holdings_sgd = float(account.long_market_value) * USD_SGD_RATE
    grand_total_sgd = alpaca_cash_sgd + alpaca_holdings_sgd
    
    # Auto-Reset Logic for Nightly Target
    if 'nightly_start_total' not in st.session_state:
        st.session_state.nightly_start_total = grand_total_sgd
        
    nightly_profit = grand_total_sgd - st.session_state.nightly_start_total
    
    # Render Metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("Alpaca Cash (SGD)", f"${alpaca_cash_sgd:,.2f}")
    m2.metric("Total Holdings (SGD)", f"${alpaca_holdings_sgd:,.2f}")
    m3.metric("GRAND TOTAL (SGD)", f"${grand_total_sgd:,.2f}", delta=f"${nightly_profit:,.2f} Nightly")

except Exception as e:
    st.error(f"Alpaca API Sync Error: {e}")

st.write("---")

# 4. TRADING ENGINE
def execute_trade(ticker, action, price_usd):
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    qty = max(1, int(TRADE_LIMIT_USD / price_usd))
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY))
        
        # Local state update for the UI tracker
        if action == "BUY":
            st.session_state.portfolio[ticker] += qty
            st.session_state.entry_prices[ticker] = price_usd
        else:
            st.session_state.portfolio[ticker] = 0.0
            st.session_state.entry_prices[ticker] = 0.0
        
        # Log to CSV
        now_sgt = datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')
        pd.DataFrame([[now_sgt, ticker, action, qty, price_usd, grand_total_sgd]], 
                     columns=["Timestamp_SGT", "Stock", "Action", "Quantity", "Price_USD", "Grand_Total_SGD"]).to_csv(LOG_FILE, mode='a', header=False, index=False)
        st.toast(f"✅ Alpaca {action}: {ticker}")
        time.sleep(0.5) 
    except Exception as e:
        st.error(f"Alpaca Order Error: {e}")

# (UI for Holdings and Logs would continue here as before)
