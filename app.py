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
# Set these to your current Alpaca USD values
INITIAL_EQUITY_USD = 100847.64  # Total Equity (Cash + Market Value) from Alpaca
TRADE_LIMIT_USD = 100.0
CASH_BUFFER_USD = 37037.0       # $50,000 SGD is approx $37,037 USD
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
    if 'nightly_start_usd' not in st.session_state:
        st.session_state.nightly_start_usd = INITIAL_EQUITY_USD

# 3. DASHBOARD UI
st.set_page_config(page_title="AI Shariah Trader (USD)", layout="wide")
st.title("🌙 Alpaca AI Scalper - USD Dashboard")

try:
    # THE TRUTH: Pulling directly from Alpaca in USD
    account = trading_client.get_account()
    
    cash_usd = float(account.cash)
    mkt_val_usd = float(account.long_market_value)
    total_equity_usd = float(account.equity)
    
    # Calculate Nightly Progress
    nightly_pnl = total_equity_usd - st.session_state.nightly_start_usd
    
    # Render Metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("Alpaca Cash (USD)", f"${cash_usd:,.2f}")
    m2.metric("Market Value (USD)", f"${mkt_val_usd:,.2f}")
    m3.metric("GRAND TOTAL (USD)", f"${total_equity_usd:,.2f}", 
              delta=f"${nightly_pnl:,.2f} Nightly")

except Exception as e:
    st.error(f"Sync Error: {e}")

if st.button("Reset Nightly Start Point"):
    st.session_state.nightly_start_usd = total_equity_usd
    st.rerun()

st.write("---")

# 4. TRADING ENGINE
def execute_trade(ticker, action, price_usd):
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    qty = max(1, int(TRADE_LIMIT_USD / price_usd))
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY))
        
        # UI Logic
        if action == "BUY":
            st.session_state.portfolio[ticker] += qty
            st.session_state.entry_prices[ticker] = price_usd
        else:
            st.session_state.portfolio[ticker] = 0.0
            st.session_state.entry_prices[ticker] = 0.0
        
        st.toast(f"✅ {action} {ticker}")
        time.sleep(0.5) 
    except Exception as e:
        st.error(f"Alpaca Order Error: {e}")

# (Scanner Loop and Table logic remains the same, just checking CASH_BUFFER_USD)
