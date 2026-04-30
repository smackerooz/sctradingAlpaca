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
INITIAL_BALANCE_SGD = 10000.0
USD_SGD_RATE = 1.35  
TRADE_LIMIT_USD = 100.0
CASH_BUFFER_SGD = 5000.0 
LOG_FILE = "trading_log.csv"
SGT = pytz.timezone('Asia/Singapore')

# FETCH SECRETS
ALPACA_API_KEY = st.secrets["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

# UPDATED: The "Super 74" Shariah List (HES Removed)
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

# Initialize Session State
if 'balance' not in st.session_state:
    try:
        account = trading_client.get_account()
        st.session_state.balance = float(account.cash) * USD_SGD_RATE
    except Exception as e:
        st.session_state.balance = INITIAL_BALANCE_SGD
        st.warning(f"Note: Could not fetch Alpaca Balance, using local fallback. Error: {e}")
    
    st.session_state.portfolio = {ticker: 0.0 for ticker in SHARIAH_STOCKS}
    st.session_state.entry_prices = {ticker: 0.0 for ticker in SHARIAH_STOCKS}

# 2. EXECUTION LOGIC
def execute_trade(ticker, action, price_usd):
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    qty = max(1, int(TRADE_LIMIT_USD / price_usd))
    
    order_data = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    
    try:
        trading_client.submit_order(order_data=order_data)
        price_sgd = price_usd * USD_SGD_RATE
        if action == "BUY":
            st.session_state.balance -= (qty * price_sgd)
            st.session_state.portfolio[ticker] += qty
            st.session_state.entry_prices[ticker] = price_usd
        else:
            st.session_state.balance += (qty * price_sgd)
            st.session_state.portfolio[ticker] = 0.0
            st.session_state.entry_prices[ticker] = 0.0
        
        log_trade(ticker, action, qty, price_usd)
        st.toast(f"✅ Alpaca {action}: {ticker}")
        time.sleep(0.5) 
    except Exception as e:
        st.error(f"Alpaca Order Error: {e}")

def log_trade(ticker, action, qty, price):
    now_sgt = datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')
    new_entry = pd.DataFrame([[now_sgt, ticker, action, qty, price, st.session_state.balance]], 
                             columns=["Timestamp_SGT", "Stock", "Action", "Quantity", "Price_USD", "Balance_SGD"])
    new_entry.to_csv(LOG_FILE, mode='a', header=False, index=False)

# 3. DASHBOARD UI
st.set_page_config(page_title="AI Shariah Trader", layout="wide")
st.title("🌙 Alpaca-Linked AI Scalper")

# --- LIVE EQUITY CALCULATION ---
holdings_value_usd = 0.0
holdings_display_list = []

for ticker, qty in st.session_state.portfolio.items():
    if qty > 0:
        try:
            # Use yfinance for a quick price check for the dashboard
            h_ticker = yf.Ticker(ticker)
            # fast_info is efficient for dashboard refreshes
            last_p = h_ticker.fast_info['last_price']
            market_val_usd = last_p * qty
            holdings_value_usd += market_val_usd
            
            holdings_display_list.append({
                "Stock": ticker, 
                "Qty": round(qty, 4), 
                "Current Price": f"${last_p:.2f}",
                "Value (USD)": f"${market_val_usd:.2f}"
            })
        except:
            # Fallback to entry price if Yahoo Finance blips
            holdings_value_usd += (st.session_state.entry_prices[ticker] * qty)
            holdings_display_list.append({"Stock": ticker, "Qty": round(qty, 4), "Current Price": "Syncing..."})

total_holdings_sgd = holdings_value_usd * USD_SGD_RATE
grand_total_sgd = st.session_state.balance + total_holdings_sgd
net_pnl_sgd = grand_total_sgd - INITIAL_BALANCE_SGD

# --- RENDER TOP METRICS ---
m1, m2, m3, m4 = st.columns(4)
m1.metric("Alpaca Cash", f"${st.session_state.balance:,.2f} SGD")
m2.metric("Holdings Value", f"${total_holdings_sgd:,.2f} SGD")
m3.metric("GRAND TOTAL", f"${grand_total_sgd:,.2f} SGD", delta=f"${net_pnl_sgd:,.2f} vs Start")
m4.metric("Active Positions", len(holdings_display_list))

st.write("---")

col_left, col_right = st.columns(2)
with col_left:
    st.subheader("📈 Current Holdings")
    if holdings_display_list:
        st.table(pd.DataFrame(holdings_display_list))
    else:
        st.info("No active trades. Scanning for signals...")

with col_right:
    st.subheader("📜 Recent Logs")
    if os.path.exists(LOG_FILE):
        log_df = pd.read_csv(LOG_FILE).tail(10)
        st.dataframe(log_df.iloc[::-1], width='stretch')

# 4. TRADING LOOP
while True:
    current_signals = []
    with st.status(f"🚀 Scanning... ({datetime.now(SGT).strftime('%H:%M:%S')} SGT)", expanded=False):
        for stock in SHARIAH_STOCKS:
            try:
                data = yf.download(stock, period="1d", interval="1m", progress=False)
                if not data.empty and len(data) >= 20:
                    # Fix for Multi-index columns in newer yfinance
                    if isinstance(data.columns, pd.MultiIndex):
                        data.columns = data.columns.get_level_values(0)
                        
                    curr_p = float(data['Close'].iloc[-1])
                    s_ma = float(data['Close'].rolling(window=5).mean().iloc[-1])
                    l_ma = float(data['Close'].rolling(window=20).mean().iloc[-1])
                    
                    trend = "🟢 Bullish" if s_ma > l_ma else "🔴 Bearish"
                    current_signals.append({"Ticker": stock, "Price": round(curr_p, 2), "Trend": trend})

                    if st.session_state.portfolio[stock] > 0:
                        entry_p = st.session_state.entry_prices[stock]
                        if (curr_p - entry_p) / entry_p >= 0.02 or s_ma < l_ma:
                            execute_trade(stock, "SELL", curr_p)
                    elif s_ma > l_ma and st.session_state.balance > CASH_BUFFER_SGD:
                        execute_trade(stock, "BUY", curr_p)
            except:
                continue
    
    if current_signals:
        # 2026 Syntax Update: width='stretch'
        signal_table.dataframe(pd.DataFrame(current_signals), width='stretch')
    
    time.sleep(10)
    st.rerun()
