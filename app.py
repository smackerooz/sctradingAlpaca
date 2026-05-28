"""
Dynamic Dashboard – Works with any strategy defined in Supabase 'strategies' table
- Toggle between Last Completed Session and Current Session for trades
- Portfolio Backtest: select strategy from dropdown
- Manual override, liquidation, daily P&L charts, signal scanner
- Individual liquidation moved to its own tab
- Strategy selection for manual liquidation
- Charts inside expander
- Strategy column in open positions
"""

import streamlit as st
import pytz
import time
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Trading Bot", page_icon="📈", layout="wide")

# ─────────────────────────────────────────────
# KEEPALIVE PING
# ─────────────────────────────────────────────
import streamlit.components.v1 as components
import os
from supabase import create_client, Client

components.html(
    """
    <div style="
        font-family: monospace;
        font-size: 12px;
        color: #aaa;
        background: #1a1a2e;
        border: 1px solid #333;
        border-radius: 6px;
        padding: 5px 12px;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
    ">
        <span style="color:#26a65b; font-size:10px;">●</span>
        <span>Keepalive ping in: <strong id="countdown" style="color:#4f8ef7;">5:00</strong></span>
        <span id="ping_status" style="color:#aaa; font-size:11px;"></span>
    </div>
    <script>
    var totalSeconds = 300;
    var remaining = totalSeconds;

    function updateCountdown() {
        var mins = Math.floor(remaining / 60);
        var secs = remaining % 60;
        document.getElementById('countdown').textContent =
            mins + ':' + (secs < 10 ? '0' : '') + secs;

        if (remaining <= 10) {
            document.getElementById('countdown').style.color = '#e74c3c';
        } else if (remaining <= 60) {
            document.getElementById('countdown').style.color = '#f0a500';
        } else {
            document.getElementById('countdown').style.color = '#4f8ef7';
        }

        if (remaining <= 0) {
            try {
                fetch(window.location.href, {mode: 'no-cors', cache: 'no-store'});
            } catch(e) {}
            document.getElementById('ping_status').textContent = '✅ Pinged!';
            setTimeout(function() {
                document.getElementById('ping_status').textContent = '';
            }, 3000);
            remaining = totalSeconds;
        } else {
            remaining--;
        }
    }

    updateCountdown();
    setInterval(updateCountdown, 1000);
    </script>
    """,
    height=40,
)

# ─────────────────────────────────────────────
# INITIALIZE CLIENTS
# ─────────────────────────────────────────────
try:
    API_KEY = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
except Exception as e:
    st.error(f"Missing or invalid Alpaca API Keys: {e}")
    st.stop()

@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase = get_supabase()
SGT = pytz.timezone('Asia/Singapore')
ET = pytz.timezone('US/Eastern')

# ─────────────────────────────────────────────
# CONSTANTS & WATCHLIST
# ─────────────────────────────────────────────
TARGET_PROFIT = 200.0
SCAN_INTERVAL = 10

WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", "MSTR", "PANW",
    "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD", "ZS",
    "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", "SWKS", "FTNT", "ANET",
    "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

# ─────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────
if "realized_trades" not in st.session_state:
    st.session_state.realized_trades = []
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = datetime.now(SGT)

# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
def load_realized_trades():
    try:
        response = supabase.table("realized_trades").select("*").order("date", desc=True).execute()
        return response.data
    except:
        return []

def compute_daily_pnl_overview():
    trades = load_realized_trades()
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    if "pl_usd" in df.columns:
        df["pl_usd"] = df["pl_usd"]
    elif "P&L ($)" in df.columns:
        df["pl_usd"] = df["P&L ($)"].apply(lambda x: float(x.split('$')[1].replace('+','').replace(',','')) if '$' in str(x) else 0)
    else:
        df["pl_usd"] = 0
    df["date"] = pd.to_datetime(df["date"]).dt.date
    daily = df.groupby("date")["pl_usd"].sum().reset_index()
    daily.columns = ["Trading Session Date", "Total"]
    return daily

def get_trading_session_start_for_trade(trade_date, time_sgt_str):
    # Convert trade_date to date object if needed
    if isinstance(trade_date, str):
        trade_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
    elif hasattr(trade_date, 'date'):
        trade_date = trade_date.date()
    
    try:
        time_obj = datetime.strptime(time_sgt_str, "%H:%M:%S").time()
    except:
        return trade_date
    if time_obj >= datetime.strptime("21:30:00", "%H:%M:%S").time():
        return trade_date
    elif time_obj < datetime.strptime("04:00:00", "%H:%M:%S").time():
        return trade_date - timedelta(days=1)
    else:
        return trade_date

def filter_trades_by_session(trades_df, target_session_date):
    if trades_df.empty:
        return trades_df
    
    df = trades_df.copy()
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date']).dt.date
    
    df["session_start"] = df.apply(
        lambda row: get_trading_session_start_for_trade(row["date"], row.get("time_sgt", "12:00:00")),
        axis=1
    )
    return df[df["session_start"] == target_session_date].drop(columns=["session_start"])

def get_current_session_date(now_sgt):
    if now_sgt.time() >= datetime.strptime("21:30:00", "%H:%M:%S").time():
        return now_sgt.date()
    else:
        return (now_sgt - timedelta(days=1)).date()

def get_last_completed_session_date(now_sgt):
    return get_current_session_date(now_sgt) - timedelta(days=1)

def get_current_strategy_display():
    try:
        available = supabase.table("strategies").select("name", "description").execute()
        available_names = [s["name"] for s in available.data] if available.data else []
        
        row = supabase.table("bot_config").select("forced_strategy").eq("id", 1).execute()
        forced = row.data[0].get("forced_strategy") if row.data else None
        
        if forced and forced in available_names:
            desc = next((s["description"] for s in available.data if s["name"] == forced), "No description")
            return forced, desc
        elif available_names:
            default = available_names[0]
            desc = next((s["description"] for s in available.data if s["name"] == default), "")
            supabase.table("bot_config").update({"forced_strategy": default}).eq("id", 1).execute()
            return default, desc
        else:
            return "No Strategy", "No strategies defined in Supabase"
    except Exception as e:
        st.error(f"Error reading strategy: {e}")
        return "Error", "Could not load strategy"

def set_forced_strategy(strategy):
    try:
        supabase.table("bot_config").update({"forced_strategy": strategy}).eq("id", 1).execute()
    except Exception as e:
        st.error(f"Failed to save strategy: {e}")

def get_weekly_baseline():
    try:
        row = supabase.table("weekly_baseline").select("baseline_amount").order("created_at", desc=True).limit(1).execute()
        if row.data:
            return float(row.data[0]["baseline_amount"])
    except:
        pass
    return 0.0

def get_total_holdings_value():
    try:
        positions = trading_client.get_all_positions()
        total = sum(float(p.market_value) for p in positions)
        return total
    except:
        return 0.0

def get_bars(symbol, days=1):
    end = datetime.now(ET)
    start = end - timedelta(days=days)
    request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=end
    )
    bars = data_client.get_stock_bars(request).data.get(symbol, [])
    return pd.DataFrame([{"time": b.timestamp, "close": b.close, "high": b.high, "low": b.low} for b in bars])

def calc_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_macd(prices):
    exp1 = prices.ewm(span=12, adjust=False).mean()
    exp2 = prices.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal

def rsi_macd_confirmed_buy(symbol):
    bars = get_bars(symbol, days=5)
    if len(bars) < 50:
        return False
    rsi = calc_rsi(bars["close"]).iloc[-1]
    macd, signal = calc_macd(bars["close"])
    return rsi < 30 and (macd.iloc[-1] - signal.iloc[-1]) > 0

def save_trade_to_supabase(trade):
    try:
        supabase.table("realized_trades").insert(trade).execute()
    except Exception as e:
        print(f"Failed to save trade: {e}")

def is_market_open():
    clock = trading_client.get_clock()
    return clock.is_open

def run_backtest(strategy, start_date, end_date):
    # Placeholder – replace with your actual backtest logic
    return pd.DataFrame({"date": [start_date], "return": [0.05]})

# ─────────────────────────────────────────────
# FETCH LIVE ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account = trading_client.get_account()
    portfolio_value = float(account.portfolio_value)
    cash = float(account.cash)
    buying_power = float(account.buying_power)
    daily_pl_alpaca = float(account.equity) - float(account.last_equity) if hasattr(account, 'last_equity') else 0.0
    total_holdings = get_total_holdings_value()
    weekly_baseline = get_weekly_baseline()
except:
    portfolio_value = cash = buying_power = daily_pl_alpaca = total_holdings = weekly_baseline = 0.0

# ─────────────────────────────────────────────
# AUTO-REFRESH TRADES
# ─────────────────────────────────────────────
if (datetime.now(SGT) - st.session_state.last_refresh).seconds > SCAN_INTERVAL:
    st.session_state.realized_trades = load_realized_trades()
    st.session_state.last_refresh = datetime.now(SGT)

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
st.sidebar.image("https://alpaca.markets/docs/assets/images/alpaca-logo.png", width=200)
st.sidebar.markdown("## Account Summary")
st.sidebar.metric("Portfolio Value", f"${portfolio_value:,.2f}")
st.sidebar.metric("Cash", f"${cash:,.2f}")
st.sidebar.metric("Buying Power", f"${buying_power:,.2f}")
st.sidebar.metric("Today's P&L (Alpaca)", f"${daily_pl_alpaca:+.2f}")
st.sidebar.metric("Weekly Baseline", f"${weekly_baseline:,.2f}")
st.sidebar.markdown("---")
st.sidebar.markdown(f"**Market Open:** {'✅ Yes' if is_market_open() else '❌ No'}")

# ─────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot")
st.markdown("🟢 **Bot Status:** Active" if is_market_open() else "🔴 **Bot Status:** Market Closed")
st.markdown("---")

strategy_title, strategy_desc = get_current_strategy_display()
st.markdown(f"📌 **Current Strategy:** {strategy_title}")
st.markdown(f"{strategy_desc}")
st.markdown("---")

# ─────────────────────────────────────────────
# MANUAL STRATEGY OVERRIDE (with PIN)
# ─────────────────────────────────────────────
with st.expander("🔧 Manual Strategy Override (admin)"):
    if "override_authorized" not in st.session_state:
        st.session_state.override_authorized = False

    if not st.session_state.override_authorized:
        with st.form("override_pin_form"):
            override_pin = st.text_input("Enter PIN to change strategy:", type="password")
            col1, col2 = st.columns(2)
            with col1:
                verify_btn = st.form_submit_button("Unlock")
            with col2:
                cancel_btn = st.form_submit_button("Cancel")
            if verify_btn:
                try:
                    row = supabase.table("bot_config").select("pin").eq("id", 1).execute()
                    if row.data and row.data[0]["pin"] == override_pin:
                        st.session_state.override_authorized = True
                        st.success("Access granted")
                        st.rerun()
                    else:
                        st.error("Incorrect PIN")
                except:
                    st.error("PIN verification failed")
            if cancel_btn:
                st.rerun()
    else:
        available = supabase.table("strategies").select("name").execute()
        strategy_names = [s["name"] for s in available.data] if available.data else []
        if not strategy_names:
            st.warning("No strategies found in database. Add some to the 'strategies' table.")
            strategy_names = ["NONE"]
        
        current_strategy, _ = get_current_strategy_display()
        if current_strategy not in strategy_names and "NONE" not in strategy_names:
            strategy_names.insert(0, current_strategy)
        
        selected = st.selectbox("Override active strategy", strategy_names + ["NONE"], 
                                index=strategy_names.index(current_strategy) if current_strategy in strategy_names else 0)
        if st.button("Apply Override"):
            if selected == "NONE":
                first_available = strategy_names[0] if strategy_names and strategy_names[0] != "NONE" else None
                if first_available:
                    set_forced_strategy(first_available)
                    st.success(f"Strategy reset to default ({first_available})")
                else:
                    st.warning("No strategies available")
            else:
                set_forced_strategy(selected)
                st.success(f"Strategy set to {selected}")
            st.session_state.override_authorized = False
            st.rerun()
        if st.button("Lock & Cancel"):
            st.session_state.override_authorized = False
            st.rerun()

st.markdown("---")

# ============================================
# MAIN METRICS – Daily Realized P&L (Current Session)
# ============================================
now_sgt = datetime.now(SGT)
current_session_date = get_current_session_date(now_sgt)
trades_df_full = pd.DataFrame(st.session_state.realized_trades)
if not trades_df_full.empty and "time_sgt" in trades_df_full.columns:
    current_session_trades = filter_trades_by_session(trades_df_full, current_session_date)
    current_session_realized_pl = current_session_trades["pl_usd"].sum() if "pl_usd" in current_session_trades.columns else 0.0
else:
    current_session_realized_pl = 0.0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Portfolio Value", f"${portfolio_value:,.2f}")
col2.metric("Cash", f"${cash:,.2f}")
col3.metric("Total Holdings", f"${total_holdings:,.2f}")
col4.metric("Daily Realized P&L (Current Session)", f"${current_session_realized_pl:+.2f}")

st.markdown("---")

# ============================================
# TABS
# ============================================
tab_live, tab_signals, tab_backtest, tab_liq = st.tabs(
    ["Live Trading", "Signal Scanner", "Portfolio Backtest", "Individual Liquidation"]
)

# ─────────────────────────────────────────────
# TAB 1 — LIVE TRADING (with session selector)
# ─────────────────────────────────────────────
with tab_live:
    st.write(f"## 🎯 Weekly Goal: ${TARGET_PROFIT:.0f} USD")
    
    session_option = st.radio(
        "Select trading session to view:",
        ["Current Session", "Last Completed Session"],
        horizontal=True,
        key="session_selector"
    )
    
    if session_option == "Current Session":
        selected_session = current_session_date
        session_label = f"Current Session (started {selected_session})"
    else:
        selected_session = get_last_completed_session_date(now_sgt)
        session_label = f"Last Completed Session ({selected_session})"
    
    st.subheader(f"📋 Realized Trades – {session_label}")
    
    if not trades_df_full.empty and "time_sgt" in trades_df_full.columns:
        session_trades = filter_trades_by_session(trades_df_full, selected_session)
        if session_trades.empty:
            st.info(f"No realized trades found for {session_label}.")
        else:
            # Display trades table
            st.dataframe(session_trades, use_container_width=True)
            
            # ---- PER-STRATEGY P&L ----
            # Find strategy column (case-insensitive) and pl_usd column
            strategy_col = None
            for col in session_trades.columns:
                if col.lower() == 'strategy':
                    strategy_col = col
                    break
            
            if "pl_usd" in session_trades.columns and strategy_col:
                pl_by_strategy = session_trades.groupby(strategy_col)["pl_usd"].sum().reset_index()
                pl_by_strategy.columns = ["Strategy", "Realized P&L (USD)"]
                pl_by_strategy["Realized P&L (USD)"] = pl_by_strategy["Realized P&L (USD)"].apply(lambda x: f"${x:+.2f}")
                st.markdown("### Per‑Strategy P&L for this Session")
                st.dataframe(pl_by_strategy, use_container_width=True, hide_index=True)
            else:
                missing = []
                if "pl_usd" not in session_trades.columns:
                    missing.append("'pl_usd'")
                if not strategy_col:
                    missing.append("'strategy' column (case-insensitive)")
                st.warning(f"P&L breakdown not available (missing {', '.join(missing)}). Available columns: {list(session_trades.columns)}")
    else:
        st.info("No trade data available or missing 'time_sgt' column. Please ensure trades are saved with 'time_sgt' field.")
    
    # Charts (all-time)
    with st.expander("📊 Daily P&L Charts (Bar + Cumulative)", expanded=True):
        daily_df = compute_daily_pnl_overview()
        if not daily_df.empty:
            fig_bar = go.Figure()
            fig_bar.add_trace(go.Bar(x=daily_df["Trading Session Date"], y=daily_df["Total"], name="Daily P&L"))
            fig_bar.update_layout(title="Daily Realized P&L", xaxis_title="Session Date", yaxis_title="P&L (USD)")
            st.plotly_chart(fig_bar, use_container_width=True)

            daily_sorted = daily_df.sort_values("Trading Session Date")
            daily_sorted["Cumulative Total"] = daily_sorted["Total"].cumsum()
            fig_cum = go.Figure()
            fig_cum.add_trace(go.Scatter(x=daily_sorted["Trading Session Date"], y=daily_sorted["Cumulative Total"], mode="lines+markers", name="Cumulative P&L"))
            fig_cum.update_layout(title="Cumulative Realized P&L", xaxis_title="Session Date", yaxis_title="Total P&L (USD)")
            st.plotly_chart(fig_cum, use_container_width=True)
        else:
            st.info("No trade data available yet for daily P&L chart.")
    
    # Open positions
    with st.expander("📋 Open Positions (Unrealized)", expanded=False):
        try:
            positions = trading_client.get_all_positions()
            if positions:
                open_pos_map = {}
                try:
                    open_rows = supabase.table("open_positions").select("symbol", "strategy").execute()
                    for row in open_rows.data:
                        open_pos_map[row["symbol"]] = row["strategy"]
                except:
                    pass
                open_data = []
                for p in positions:
                    entry = float(p.avg_entry_price)
                    current = float(p.current_price)
                    qty = float(p.qty)
                    pl_usd = (current - entry) * qty
                    pl_pct = (pl_usd / (entry * qty)) * 100 if entry * qty != 0 else 0
                    strategy = open_pos_map.get(p.symbol, "N/A")
                    open_data.append({
                        "Symbol": p.symbol,
                        "Strategy": strategy,
                        "Entry": f"${entry:.2f}",
                        "Current": f"${current:.2f}",
                        "Qty": round(qty, 4),
                        "Unrealized P&L ($)": f"${pl_usd:+.2f}",
                        "Unrealized P&L (%)": f"{pl_pct:+.2f}%",
                    })
                st.dataframe(pd.DataFrame(open_data), use_container_width=True, hide_index=True)
            else:
                st.info("No open positions.")
        except:
            st.info("Could not fetch positions.")

# ─────────────────────────────────────────────
# TAB 2 — SIGNAL SCANNER
# ─────────────────────────────────────────────
with tab_signals:
    st.write("## 🔍 Signal Scanner")
    st.caption("RSI + MACD oversold crossover signals")
    if st.button("Scan Now"):
        with st.spinner("Scanning watchlist..."):
            signals = []
            for symbol in WATCHLIST:
                if rsi_macd_confirmed_buy(symbol):
                    signals.append(symbol)
            if signals:
                st.success(f"🚨 Buy signals found for: {', '.join(signals)}")
            else:
                st.info("No strong buy signals at this time.")
    else:
        st.info("Click 'Scan Now' to evaluate all watchlist stocks.")

# ─────────────────────────────────────────────
# TAB 3 — PORTFOLIO BACKTEST
# ─────────────────────────────────────────────
with tab_backtest:
    st.write("## 📈 Portfolio Backtest")
    st.caption("Run a backtest for a selected strategy over a date range")
    available = supabase.table("strategies").select("name").execute()
    strategy_names = [s["name"] for s in available.data] if available.data else ["ORB-R", "VWAP"]
    strategy_choice = st.selectbox("Select strategy", strategy_names)
    start_date = st.date_input("Start date", datetime.now() - timedelta(days=30))
    end_date = st.date_input("End date", datetime.now())
    if st.button("Run Backtest"):
        results = run_backtest(strategy_choice, start_date, end_date)
        st.dataframe(results)

# ─────────────────────────────────────────────
# TAB 4 — INDIVIDUAL LIQUIDATION (with PIN)
# ─────────────────────────────────────────────
with tab_liq:
    st.write("## 🧹 Individual Position Liquidation")
    st.caption("Sell specific holdings using a limit order (supports extended hours).")

    if "liq_individual_authorized" not in st.session_state:
        st.session_state.liq_individual_authorized = False

    if not st.session_state.liq_individual_authorized:
        with st.form("indiv_liq_pin_form"):
            indiv_liq_pin = st.text_input("Enter PIN to access individual liquidation:", type="password")
            col_a, col_b = st.columns(2)
            with col_a:
                verify_btn = st.form_submit_button("🔓 Unlock", use_container_width=True)
            with col_b:
                cancel_btn = st.form_submit_button("❌ Cancel", use_container_width=True)

            if verify_btn:
                try:
                    row = supabase.table("bot_config").select("pin").eq("id", 1).execute()
                    if row.data and row.data[0]["pin"] == indiv_liq_pin:
                        st.session_state.liq_individual_authorized = True
                        st.success("Access granted!")
                        st.rerun()
                    else:
                        st.error("Incorrect PIN")
                except Exception:
                    st.error("Could not verify PIN")
            if cancel_btn:
                st.session_state.liq_individual_authorized = False
                st.rerun()
    else:
        st.success("✅ Access granted – you can liquidate individual positions")
        st.info("ℹ️ **Order type:** Market order during regular hours (9:30 AM – 4:00 PM ET) – supports fractional shares. Limit order during extended hours (integer shares only).")

        try:
            positions = trading_client.get_all_positions()
            if not positions:
                st.info("No open positions to liquidate.")
            else:
                position_options = {}
                for p in positions:
                    symbol = p.symbol
                    qty = float(p.qty)
                    current_price = float(p.current_price)
                    market_value = float(p.market_value)
                    unrealized_pl = float(p.unrealized_pl)
                    position_options[f"{symbol} | Qty: {qty:.4f} | Mkt Val: ${market_value:.2f} | P&L: ${unrealized_pl:+.2f}"] = {
                        "symbol": symbol,
                        "qty": qty,
                        "current_price": current_price,
                        "entry_price": float(p.avg_entry_price),
                    }

                selected_label = st.selectbox("Select position to liquidate", list(position_options.keys()), key="liq_select")
                selected = position_options[selected_label]
                symbol = selected["symbol"]
                qty = selected["qty"]
                current_price = selected["current_price"]
                entry_price_from_position = selected["entry_price"]

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Symbol", symbol)
                col2.metric("Quantity", f"{qty:.4f}")
                col3.metric("Current Price", f"${current_price:.2f}")
                col4.metric("Estimated Proceeds", f"${qty * current_price:.2f}")

                available = supabase.table("strategies").select("name").execute()
                strategy_options = [s["name"] for s in available.data] if available.data else ["ORB-R", "VWAP"]
                selected_strategy = st.selectbox("Strategy that opened this position (for correct P&L attribution)", strategy_options, key="liq_strategy")

                now_et = datetime.now(ET)
                regular_start = now_et.replace(hour=9, minute=30, second=0)
                regular_end = now_et.replace(hour=16, minute=0, second=0)
                is_regular_hours = regular_start <= now_et <= regular_end

                if is_regular_hours:
                    st.info("🟢 **Regular market hours** – will use a **market order** (supports fractional shares).")
                else:
                    st.warning("🟡 **Extended hours** – will use a **limit order** (only whole shares). Any fractional remainder will stay in your account.")

                confirm = st.checkbox("I confirm that I want to sell this position immediately.", key="liq_confirm")
                col_liq, col_cancel = st.columns(2)
                with col_liq:
                    if st.button("🔥 LIQUIDATE THIS POSITION", use_container_width=True, type="primary", disabled=not confirm, key="liq_execute"):
                        try:
                            sold_qty = qty
                            if not is_regular_hours:
                                whole_shares = int(qty)
                                fractional_remainder = qty - whole_shares
                                if fractional_remainder > 0:
                                    st.warning(f"⚠️ Extended hours only support whole shares. Will sell {whole_shares} shares. Remaining {fractional_remainder:.4f} shares will stay.")
                                if whole_shares == 0:
                                    st.error("No whole shares to sell during extended hours.")
                                else:
                                    limit_price = round(current_price * 0.99, 2)
                                    trading_client.submit_order(LimitOrderRequest(
                                        symbol=symbol,
                                        qty=whole_shares,
                                        side=OrderSide.SELL,
                                        limit_price=limit_price,
                                        time_in_force=TimeInForce.DAY,
                                        extended_hours=True
                                    ))
                                    sold_qty = whole_shares
                                    st.success(f"✅ Limit order placed to sell {whole_shares} shares of {symbol} at ${limit_price:.2f}.")
                            else:
                                trading_client.submit_order(MarketOrderRequest(
                                    symbol=symbol,
                                    qty=qty,
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY,
                                ))
                                st.success(f"✅ Market order placed to sell {qty:.4f} shares of {symbol}.")

                            entry_price = entry_price_from_position
                            pl_usd = (current_price - entry_price) * sold_qty
                            pl_pct = (pl_usd / (entry_price * sold_qty)) * 100 if entry_price * sold_qty != 0 else 0

                            trade_record = {
                                "date": datetime.now(SGT).date().isoformat(),
                                "Symbol": symbol,
                                "strategy": selected_strategy,      # lowercase to match Supabase
                                "Buy Price": f"${entry_price:.2f}",
                                "Sell Price": f"${current_price:.2f}",
                                "Qty": round(sold_qty, 4),
                                "pl_usd": pl_usd,
                                "P&L ($)": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
                                "P&L (%)": f"{pl_pct:+.2f}%",
                                "time_sgt": datetime.now(SGT).strftime("%H:%M:%S"),
                                "Reason": "Manual liquidation via dashboard",
                            }
                            save_trade_to_supabase(trade_record)
                            st.session_state.realized_trades.insert(0, trade_record)

                            try:
                                supabase.table("open_positions").delete().eq("symbol", symbol).execute()
                            except:
                                pass

                            st.rerun()
                        except Exception as e:
                            st.error(f"Error placing order: {e}")
                with col_cancel:
                    if st.button("❌ Cancel", use_container_width=True, key="liq_cancel"):
                        st.info("Liquidation cancelled.")
                        st.rerun()
        except Exception as e:
            st.error(f"Could not fetch positions: {e}")

        if st.button("🔒 Lock Individual Liquidation", use_container_width=True, key="liq_lock"):
            st.session_state.liq_individual_authorized = False
            st.rerun()

# ─────────────────────────────────────────────
# P&L RECONCILIATION HELPER (optional)
# ─────────────────────────────────────────────
with st.expander("🔍 P&L Reconciliation Helper", expanded=False):
    st.markdown("""
    **Why realized P&L may not match Alpaca daily change?**
    - Alpaca's daily change includes **unrealized P&L** on open positions.
    - Your dashboard’s realized P&L only includes **closed trades**.
    - Trading session dates (9:30 PM SGT → 4:00 AM SGT) differ from calendar days.
    
    **SQL to verify trading session P&L:**
    """)
    st.code("""
WITH session_trades AS (
    SELECT *,
        CASE 
            WHEN time_sgt >= '21:30:00' THEN date::DATE
            WHEN time_sgt < '04:00:00' THEN (date::DATE - INTERVAL '1 day')::DATE
            ELSE date::DATE
        END AS session_start_date
    FROM realized_trades
)
SELECT session_start_date, strategy, SUM(pl_usd) AS total_pl
FROM session_trades
GROUP BY session_start_date, strategy
ORDER BY session_start_date DESC, strategy;
    """, language="sql")

# ─────────────────────────────────────────────
# AUTO-REFRESH DASHBOARD
# ─────────────────────────────────────────────
time.sleep(SCAN_INTERVAL)
st.rerun()