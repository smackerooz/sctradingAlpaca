import streamlit as st
import pandas as pd
import yfinance as yf
import time
from datetime import datetime
import pytz
import os

# 1. SETUP & CONFIGURATION
INITIAL_BALANCE_SGD = 10000.0
USD_SGD_RATE = 1.35  
TRADE_LIMIT_USD = 100.0
CASH_BUFFER_SGD = 5000.0 
LOG_FILE = "trading_log.csv"
SGT = pytz.timezone('Asia/Singapore')

# The "Super 75" Shariah List
SHARIAH_STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AVGO", "ASML", "AMD", "INTC", "ADBE", "CRM", 
    "TXN", "QCOM", "AMAT", "LRCX", "MU", "ADI", "KLAC", "SNOW", "PLTR", "PANW", 
    "FTNT", "ZS", "DDOG", "NET", "OKTA", "MDB", "TEAM", "WDAY", "NOW", "SHOP",
    "LLY", "JNJ", "AMGN", "VRTX", "REGN", "MRNA", "ISRG", "GILD", "TMO", "DHR", 
    "IDXX", "A", "BIIB", "BSX", "ZTS", "EW", "ALGN", "DXCM", "MTD", "RMD",
    "EOG", "SLB", "COP", "HAL", "HES", "XOM", "CVX", "UPS", "FDX", "CAT", 
    "DE", "HON", "LMT", "GD", "NOC", "TSLA", "LOW", "TJX", "COST", "AZO", 
    "ORLY", "NKE", "SBUX", "CMG", "EL"
]

# Initialize Session State
if 'balance' not in st.session_state:
    try:
        # Check if secrets exist
        if "ALPACA_API_KEY" in st.secrets:
            account = trading_client.get_account()
            # Convert USD to SGD and store it
            st.session_state.balance = float(account.cash) * USD_SGD_RATE
        else:
            st.session_state.balance = INITIAL_BALANCE_SGD
            st.error("Secrets not found! Using default $10k.")
    except Exception as e:
        st.session_state.balance = INITIAL_BALANCE_SGD
        st.warning(f"Could not sync with Alpaca: {e}. Using default $10k.")

# 2. EXECUTION & LOGGING
def execute_trade(ticker, action, price_usd):
    price_sgd = price_usd * USD_SGD_RATE
    if action == "BUY":
        qty = TRADE_LIMIT_USD / price_usd
        st.session_state.balance -= (qty * price_sgd)
        st.session_state.portfolio[ticker] += qty
        st.session_state.entry_prices[ticker] = price_usd
        log_trade(ticker, "BUY", qty, price_usd)
    elif action == "SELL":
        qty = st.session_state.portfolio[ticker]
        st.session_state.balance += (qty * price_sgd)
        st.session_state.portfolio[ticker] = 0.0
        st.session_state.entry_prices[ticker] = 0.0
        log_trade(ticker, "SELL", qty, price_usd)

def log_trade(ticker, action, qty, price):
    now_sgt = datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')
    new_entry = pd.DataFrame([[now_sgt, ticker, action, qty, price, st.session_state.balance]], 
                             columns=["Timestamp_SGT", "Stock", "Action", "Quantity", "Price_USD", "Balance_SGD"])
    new_entry.to_csv(LOG_FILE, mode='a', header=False, index=False)

# 3. DASHBOARD UI
st.set_page_config(page_title="AI Shariah Trader", layout="wide")
st.title("🌙 Shariah-Compliant AI Trading in ALPACA (by Rooz)")

# Calculate Real-Time Equity
holdings_value_usd = 0.0
holdings_data = []
for ticker, qty in st.session_state.portfolio.items():
    if qty > 0:
        try:
            # Quick fetch for current value
            h_ticker = yf.Ticker(ticker)
            last_p = h_ticker.fast_info['last_price']
            holdings_value_usd += (last_p * qty)
            holdings_data.append({"Stock": ticker, "Qty": round(qty, 4), "Current Price": round(last_p, 2)})
        except:
            holdings_value_usd += (st.session_state.entry_prices[ticker] * qty)
            holdings_data.append({"Stock": ticker, "Qty": round(qty, 4), "Current Price": "Error"})

total_holdings_sgd = holdings_value_usd * USD_SGD_RATE
grand_total_sgd = st.session_state.balance + total_holdings_sgd
net_pnl = grand_total_sgd - INITIAL_BALANCE_SGD

# Render Metrics
m1, m2, m3, m4 = st.columns(4)
m1.metric("Cash Balance", f"${st.session_state.balance:,.2f} SGD")
m2.metric("Holdings Value", f"${total_holdings_sgd:,.2f} SGD")
m3.metric("GRAND TOTAL", f"${grand_total_sgd:,.2f} SGD", delta=f"${net_pnl:,.2f} SGD")
m4.metric("Active Positions", len(holdings_data))

st.write("---")

col_left, col_right = st.columns(2)
with col_left:
    st.subheader("📈 Current Holdings")
    if holdings_data:
        st.table(pd.DataFrame(holdings_data))
    else:
        st.info("No active trades. Scanning...")

with col_right:
    st.subheader("📜 Recent Logs (Memory Safe)")
    if os.path.exists(LOG_FILE):
        try:
            log_df = pd.read_csv(LOG_FILE).tail(20)
            st.dataframe(log_df.iloc[::-1], use_container_width=True)
        except:
            st.warning("Logs updating...")

st.write("---")
st.subheader("📡 Live Signal Tracker")
signal_table = st.empty()

# 4. TRADING ENGINE
while True:
    current_signals = []
    with st.status(f"🚀 Scanning Market... ({datetime.now(SGT).strftime('%H:%M:%S')} SGT)", expanded=False) as status:
        for stock in SHARIAH_STOCKS:
            try:
                data = yf.download(stock, period="1d", interval="1m", progress=False)
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = data.columns.get_level_values(0)

                if not data.empty and len(data) >= 20:
                    curr_p = float(data['Close'].iloc[-1])
                    s_ma = float(data['Close'].rolling(window=5).mean().iloc[-1])
                    l_ma = float(data['Close'].rolling(window=20).mean().iloc[-1])
                    
                    trend = "🟢 Bullish" if s_ma > l_ma else "🔴 Bearish"
                    current_signals.append({"Ticker": stock, "Price": round(curr_p, 2), "Trend": trend})

                    # LOGIC: EXIT
                    if st.session_state.portfolio[stock] > 0:
                        entry_p = st.session_state.entry_prices[stock]
                        profit_pct = (curr_p - entry_p) / entry_p
                        
                        if profit_pct >= 0.02:
                            execute_trade(stock, "SELL", curr_p)
                            st.toast(f"✅ SOLD {stock} (Target Hit!)")
                        elif s_ma < l_ma:
                            execute_trade(stock, "SELL", curr_p)
                            st.toast(f"📉 SOLD {stock} (Trend Reversed)")

                    # LOGIC: ENTRY
                    elif s_ma > l_ma and st.session_state.balance > CASH_BUFFER_SGD:
                        if st.session_state.portfolio[stock] == 0:
                            execute_trade(stock, "BUY", curr_p)
                            st.toast(f"🚀 BOUGHT {stock}")
            except:
                continue
    
    if current_signals:
        signal_table.dataframe(pd.DataFrame(current_signals), use_container_width=True)
    
    time.sleep(10)
    st.rerun()
