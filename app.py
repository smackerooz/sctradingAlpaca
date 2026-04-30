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
# This is your absolute starting point, but the app will now auto-pivot nightly
INITIAL_BALANCE_SGD = 136144.32  
USD_SGD_RATE = 1.35  
TRADE_LIMIT_USD = 100.0
CASH_BUFFER_SGD = 5000.0 
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
if 'balance' not in st.session_state:
    try:
        account = trading_client.get_account()
        st.session_state.balance = float(account.cash) * USD_SGD_RATE
    except:
        st.session_state.balance = INITIAL_BALANCE_SGD
    
    st.session_state.portfolio = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    st.session_state.entry_prices = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    # Auto-Reset Logic: Store the date and the starting total
    st.session_state.last_reset_date = datetime.now(SGT).date()
    st.session_state.nightly_start_total = INITIAL_BALANCE_SGD

# 3. HELPER FUNCTIONS
def execute_trade(ticker, action, price_usd):
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    qty = max(1, int(TRADE_LIMIT_USD / price_usd))
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY))
        price_sgd = price_usd * USD_SGD_RATE
        if action == "BUY":
            st.session_state.balance -= (qty * price_sgd)
            st.session_state.portfolio[ticker] += qty
            st.session_state.entry_prices[ticker] = price_usd
        else:
            st.session_state.balance += (qty * price_sgd)
            st.session_state.portfolio[ticker] = 0.0
            st.session_state.entry_prices[ticker] = 0.0
        
        now_sgt = datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')
        pd.DataFrame([[now_sgt, ticker, action, qty, price_usd, st.session_state.balance]], 
                     columns=["Timestamp_SGT", "Stock", "Action", "Quantity", "Price_USD", "Balance_SGD"]).to_csv(LOG_FILE, mode='a', header=False, index=False)
        st.toast(f"✅ Alpaca {action}: {ticker}")
        time.sleep(0.5) 
    except Exception as e:
        st.error(f"Alpaca Order Error: {e}")

# 4. DASHBOARD UI
st.set_page_config(page_title="AI Shariah Trader", layout="wide")
st.title("🌙 Alpaca-Linked AI Scalper")

# CALCULATE LIVE EQUITY
holdings_value_usd = 0.0
holdings_display = []
for ticker, qty in st.session_state.portfolio.items():
    if qty > 0:
        try:
            val = yf.Ticker(ticker).fast_info['last_price'] * qty
            holdings_value_usd += val
            holdings_display.append({"Stock": ticker, "Qty": round(qty, 2), "Val (USD)": f"${val:.2f}"})
        except:
            holdings_value_usd += (st.session_state.entry_prices[ticker] * qty)

grand_total_sgd = st.session_state.balance + (holdings_value_usd * USD_SGD_RATE)

# --- AUTO-RESET LOGIC ---
today = datetime.now(SGT).date()
# If the date changes, capture the new 'nightly start' to reset the delta to 0
if today > st.session_state.last_reset_date:
    st.session_state.nightly_start_total = grand_total_sgd
    st.session_state.last_reset_date = today

nightly_profit = grand_total_sgd - st.session_state.nightly_start_total

# Metrics
m1, m2, m3, m4 = st.columns(4)
m1.metric("Alpaca Cash", f"${st.session_state.balance:,.2f} SGD")
m2.metric("Holdings Value", f"${(holdings_value_usd * USD_SGD_RATE):,.2f} SGD")
m3.metric("GRAND TOTAL", f"${grand_total_sgd:,.2f} SGD", delta=f"${nightly_profit:,.2f} Nightly")
m4.metric("Active Positions", len(holdings_display))

if st.button("Force Manual Reset for Tonight"):
    st.session_state.nightly_start_total = grand_total_sgd
    st.rerun()

st.write("---")
# (The rest of your UI: Holdings table and Recent Logs follows here...)
col_l, col_r = st.columns(2)
with col_l:
    st.subheader("📈 Current Holdings")
    if holdings_display: st.table(pd.DataFrame(holdings_display))
    else: st.info("No active trades.")
with col_r:
    st.subheader("📜 Recent Logs")
    if os.path.exists(LOG_FILE):
        st.dataframe(pd.read_csv(LOG_FILE).tail(10).iloc[::-1], width='stretch')

# 5. SCANNER LOOP
st.write("---")
st.subheader("📡 Live Signal Tracker")
signal_placeholder = st.empty()

while True:
    signals = []
    with st.status(f"🚀 Scanning... ({datetime.now(SGT).strftime('%H:%M:%S')} SGT)", expanded=False):
        for stock in SHARIAH_STOCKS:
            try:
                data = yf.download(stock, period="1d", interval="1m", progress=False)
                if not data.empty and len(data) >= 20:
                    if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
                    cp = float(data['Close'].iloc[-1])
                    s_ma = data['Close'].rolling(5).mean().iloc[-1]
                    l_ma = data['Close'].rolling(20).mean().iloc[-1]
                    signals.append({"Ticker": stock, "Price": round(cp, 2), "Trend": "🟢 Bull" if s_ma > l_ma else "🔴 Bear"})

                    # Trade Logic
                    if st.session_state.portfolio[stock] > 0:
                        if (cp - st.session_state.entry_prices[stock]) / st.session_state.entry_prices[stock] >= 0.02 or s_ma < l_ma:
                            execute_trade(stock, "SELL", cp)
                    elif s_ma > l_ma and st.session_state.balance > CASH_BUFFER_SGD:
                        execute_trade(stock, "BUY", cp)
            except: continue
    signal_placeholder.dataframe(pd.DataFrame(signals), width='stretch')
    time.sleep(10)
    st.rerun()
