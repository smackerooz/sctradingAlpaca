import streamlit as st
import pytz
import time
import pandas as pd
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ─────────────────────────────────────────────
# 0. PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Super 74 Bot", page_icon="🚀", layout="wide")

# ─────────────────────────────────────────────
# 1. INITIALIZE CLIENTS
# ─────────────────────────────────────────────
try:
    API_KEY    = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)
except Exception as e:
    st.error(f"Credentials Error: {e}")
    st.stop()

# ─────────────────────────────────────────────
# 2. THE SUPER 74 TUNING (Per-Stock SL & Trend)
# ─────────────────────────────────────────────
SGT           = pytz.timezone('Asia/Singapore')
TARGET_PROFIT = 150.0      # USD (~200 SGD)
CASH_BUFFER   = 90_000.0   # Min cash before buying
SCAN_INTERVAL = 30         
BUY_QTY       = 5          

# Format: (Hard Stop Loss %, Trailing Stop %, Buy Trend Trigger %)
# Global Hard Stop set to -1.0% as requested for the hunt.
P_LOW  = (0.010, 0.005, 0.004) # Stable (AAPL, MSFT)
P_MID  = (0.010, 0.006, 0.006) # Mid (META, AMZN)
P_HIGH = (0.010, 0.008, 0.009) # Volatile (TSLA, NVDA, AMD)

STOCK_PROFILES = {
    # TECH GIANTS
    "AAPL": P_LOW, "MSFT": P_LOW, "GOOGL": P_LOW, "AMZN": P_MID, "META": P_MID, 
    "NVDA": P_HIGH, "TSLA": P_HIGH, "AMD": P_HIGH, "AVGO": P_MID, "ORCL": P_LOW,
    # SEMIS & SOFTWARE
    "INTC": P_MID, "QCOM": P_MID, "TXN": P_LOW, "ADBE": P_MID, "CRM": P_MID, 
    "NFLX": P_HIGH, "CSCO": P_LOW, "ASML": P_MID, "MU": P_HIGH, "AMAT": P_MID,
    # FINANCE & RETAIL
    "JPM": P_LOW, "BAC": P_LOW, "WMT": P_LOW, "COST": P_LOW, "PG": P_LOW, 
    "V": P_LOW, "MA": P_LOW, "UNH": P_LOW, "HD": P_LOW, "DIS": P_MID,
    # ENERGY & INDUSTRIAL
    "XOM": P_MID, "CVX": P_MID, "CAT": P_MID, "GE": P_MID, "BA": P_HIGH, 
    "HON": P_LOW, "MMM": P_LOW, "UPS": P_LOW, "FDX": P_MID, "LMT": P_LOW,
    # THE REST OF THE 74 (Sample of others)
    "ABBV": P_LOW, "PEP": P_LOW, "KO": P_LOW, "PFE": P_LOW, "TMO": P_MID,
    "LLY": P_HIGH, "AZN": P_MID, "NKE": P_MID, "SBUX": P_MID, "T": P_LOW,
    "VZ": P_LOW, "TMUS": P_LOW, "PYPL": P_HIGH, "SQ": P_HIGH, "UBER": P_HIGH,
    "ABNB": P_HIGH, "SNOW": P_HIGH, "PLTR": P_HIGH, "BABA": P_HIGH, "JD": P_HIGH,
    "PDD": P_HIGH, "SHOP": P_HIGH, "LCID": P_HIGH, "RIVN": P_HIGH, "COIN": P_HIGH,
    "MSTR": P_HIGH, "MARA": P_HIGH, "RIOT": P_HIGH, "DKNG": P_HIGH, "PEN": P_MID,
    "ZM": P_MID, "ROKU": P_HIGH, "U": P_HIGH, "SNAP": P_HIGH
}
WATCHLIST = list(STOCK_PROFILES.keys())

# ─────────────────────────────────────────────
# 3. SESSION STATE
# ─────────────────────────────────────────────
if "nightly_baseline" not in st.session_state:
    st.session_state.nightly_baseline = float(trading_client.get_account().last_equity)

if "bot_running" not in st.session_state: st.session_state.bot_running = False
if "scan_log"    not in st.session_state: st.session_state.scan_log    = []
if "peak_prices" not in st.session_state: st.session_state.peak_prices = {}

# ─────────────────────────────────────────────
# 4. ENGINE FUNCTIONS
# ─────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now(SGT).strftime("%H:%M:%S")
    st.session_state.scan_log.insert(0, f"[{ts}] {msg}")
    st.session_state.scan_log = st.session_state.scan_log[:50]

def run_strategy():
    now_sgt = datetime.now(SGT)
    # 9:30 PM Reset
    if now_sgt.hour == 21 and now_sgt.minute == 30:
        st.session_state.nightly_baseline = float(trading_client.get_account().equity)

    try:
        account = trading_client.get_account()
        cash = float(account.cash)
        positions = trading_client.get_all_positions()
        held_symbols = {p.symbol: p for p in positions}
    except Exception as e:
        log(f"Fetch Error: {e}")
        return

    # A. END OF DAY LIQUIDATION (3:45 AM)
    if now_sgt.hour == 3 and 45 <= now_sgt.minute < 55:
        for p in positions:
            trading_client.submit_order(MarketOrderRequest(symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
            log(f"🧹 EOD Clear: {p.symbol}")
        return

    # B. TRAILING STOP & HARD STOP LOSS LOGIC
    for sym, p in held_symbols.items():
        hard_sl_pct, trail_pct, _ = STOCK_PROFILES.get(sym, P_MID)
        entry = float(p.avg_entry_price)
        curr  = float(p.current_price)
        
        # Update Trailing Peak
        current_peak = max(st.session_state.peak_prices.get(sym, entry), curr)
        st.session_state.peak_prices[sym] = current_peak
        
        # Check Triggers
        trail_stop_price = current_peak * (1 - trail_pct)
        hard_stop_price  = entry * (1 - hard_sl_pct)
        
        if curr <= hard_stop_price:
            trading_client.submit_order(MarketOrderRequest(symbol=sym, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
            log(f"🛑 HARD STOP: {sym} at {curr} (Limit -1.0%)")
            st.session_state.peak_prices.pop(sym, None)
        elif curr <= trail_stop_price and curr > entry:
            trading_client.submit_order(MarketOrderRequest(symbol=sym, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
            log(f"📉 TRAIL EXIT: {sym} locked profit at {curr}")
            st.session_state.peak_prices.pop(sym, None)

    # C. BUY LOGIC (BUFFER PROTECTED)
    if cash > CASH_BUFFER:
        for symbol in WATCHLIST:
            if symbol not in held_symbols:
                try:
                    bars = data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=datetime.now()-timedelta(minutes=20)))
                    avg = bars.df['close'].mean()
                    curr = bars.df['close'].iloc[-1]
                    _, _, trend_trigger = STOCK_PROFILES.get(symbol, P_MID)
                    
                    if curr > avg * (1 + trend_trigger):
                        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=BUY_QTY, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                        st.session_state.peak_prices[symbol] = curr
                        log(f"🟢 BUY: {symbol} (Trend +{trend_trigger*100}%)")
                except: continue

# ─────────────────────────────────────────────
# 5. UI DASHBOARD
# ─────────────────────────────────────────────
st.title("🚀 Super 74 Trailing Bot")
st.write(f"### Target: $150 USD (~200 SGD) | Buffer: $90k")

# Stats Calculation
account = trading_client.get_account()
equity = float(account.equity)
delta = round(equity - st.session_state.nightly_baseline, 2)
progress = min(max(delta / TARGET_PROFIT, 0.0), 1.0)

c1, c2, c3 = st.columns(3)
c1.metric("Total Equity", f"${equity:,.2f}", delta=f"${delta:,.2f}")
c2.metric("Cash Balance", f"${float(account.cash):,.2f}")
c3.metric("Goal Progress", f"{int(progress*100)}%")
st.progress(progress)

# Sidebar Controls
if st.sidebar.button("▶️ Start Bot"): st.session_state.bot_running = True
if st.sidebar.button("⏹ Stop Bot"): st.session_state.bot_running = False
if st.sidebar.button("🧹 Manual Liquidation"):
    for p in trading_client.get_all_positions():
        trading_client.submit_order(MarketOrderRequest(symbol=p.symbol, qty=p.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
    log("🧹 Manual Clear All")

# Holdings Table
st.write("#### 📦 Active Positions (Trailing Stop Monitoring)")
pos = trading_client.get_all_positions()
if pos:
    df_p = pd.DataFrame([{
        "Symbol": x.symbol, "Current": f"${float(x.current_price):.2f}",
        "Peak": f"${st.session_state.peak_prices.get(x.symbol, 0):.2f}",
        "P&L %": f"{float(x.unrealized_plpc)*100:.2f}%"
    } for x in pos])
    st.dataframe(df_p, use_container_width=True, height=250)

# Logs
with st.expander("📋 Activity Log", expanded=True):
    for l in st.session_state.scan_log: st.text(l)

# Auto-run
if st.session_state.bot_running:
    run_strategy()
    time.sleep(SCAN_INTERVAL)
    st.rerun()
