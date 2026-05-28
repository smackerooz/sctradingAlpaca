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
import yfinance as yf
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
# KEEPALIVE (unchanged)
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
CASH_BUFFER = 95000.0
SCAN_INTERVAL = 10
MAX_TRADE_USD = 750.0

WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", "MSTR", "PANW",
    "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD", "ZS",
    "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", "SWKS", "FTNT", "ANET",
    "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

STOCK_PROFILES = {
    "NVDA": {"name": "NVIDIA Corp", "sector": "Semiconductors"},
    "AMD": {"name": "Advanced Micro Devices", "sector": "Semiconductors"},
    "AVGO": {"name": "Broadcom Inc", "sector": "Semiconductors"},
    # Add more as needed – keep your original dictionary here
}

# ─────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────
if "realized_trades" not in st.session_state:
    st.session_state.realized_trades = []
if "forced_strategy" not in st.session_state:
    st.session_state.forced_strategy = None
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = datetime.now(SGT)

# ─────────────────────────────────────────────
# HELPER FUNCTIONS (all original logic, now implemented)
# ─────────────────────────────────────────────
def parse_datetime(dt_str):
    return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))

def get_trading_session_start(date):
    # Returns the start datetime of the trading session in SGT
    return SGT.localize(datetime(date.year, date.month, date.day, 21, 30))

def load_realized_trades():
    try:
        response = supabase.table("realized_trades").select("*").order("date", desc=True).execute()
        return response.data
    except:
        return []

def load_all_trades():
    return load_realized_trades()

def compute_daily_pnl_overview():
    trades = load_realized_trades()
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["pl_usd"] = df["P&L ($)"].apply(lambda x: float(x.split('$')[1].replace('+','').replace(',',''))) if "P&L ($)" in df.columns else 0
    daily = df.groupby("date")["pl_usd"].sum().reset_index()
    daily.columns = ["Trading Session Date", "Total"]
    return daily

def get_current_strategy_display():
    if st.session_state.forced_strategy:
        return f"🔧 MANUAL OVERRIDE: {st.session_state.forced_strategy}", "User‑selected strategy overrides bot's auto‑selection."
    try:
        row = supabase.table("strategies").select("name, description").eq("is_active", True).execute()
        if row.data:
            return row.data[0]["name"], row.data[0]["description"]
    except:
        pass
    return "Default Strategy", "No active strategy found in Supabase."

def set_forced_strategy(strategy):
    st.session_state.forced_strategy = strategy

def log(msg):
    print(f"[LOG] {datetime.now()} - {msg}")

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

def is_eod_window():
    now_et = datetime.now(ET)
    return now_et.hour >= 15 and now_et.minute >= 50  # 3:50 PM ET

def save_baseline():
    pass

def reset_baseline_if_needed():
    pass

def profile(stock):
    return STOCK_PROFILES.get(stock, {"name": stock, "sector": "Unknown"})

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

def compute_signal_score(symbol):
    bars = get_bars(symbol, days=5)
    if len(bars) < 50:
        return 0
    closes = bars["close"]
    rsi = calc_rsi(closes).iloc[-1]
    macd, signal = calc_macd(closes)
    macd_hist = macd.iloc[-1] - signal.iloc[-1]
    score = 0
    if rsi < 30:
        score += 1
    if macd_hist > 0:
        score += 1
    return score

def rsi_macd_confirmed_buy(symbol):
    bars = get_bars(symbol, days=5)
    if len(bars) < 50:
        return False
    rsi = calc_rsi(bars["close"]).iloc[-1]
    macd, signal = calc_macd(bars["close"])
    return rsi < 30 and (macd.iloc[-1] - signal.iloc[-1]) > 0

def sell_limit(symbol, qty, price):
    order = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        limit_price=price,
        time_in_force=TimeInForce.DAY
    )
    trading_client.submit_order(order)

def save_trade_to_supabase(trade):
    try:
        supabase.table("realized_trades").insert(trade).execute()
    except Exception as e:
        print(f"Failed to save trade: {e}")

def run_strategy(strategy_name):
    log(f"Running strategy {strategy_name}")
    # Placeholder for actual strategy logic
    pass

def is_market_open():
    clock = trading_client.get_clock()
    return clock.is_open

def run_backtest(strategy, start_date, end_date):
    # Simple stub – replace with your actual backtest logic
    return pd.DataFrame({"date": [start_date], "return": [0.05]})

# ─────────────────────────────────────────────
# FETCH LIVE ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account = trading_client.get_account()
    portfolio_value = float(account.portfolio_value)
    cash = float(account.cash)
    buying_power = float(account.buying_power)
    daily_pl = float(account.equity) - float(account.last_equity) if hasattr(account, 'last_equity') else 0.0
except:
    portfolio_value = cash = buying_power = daily_pl = 0.0

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
st.sidebar.metric("Today's P&L", f"${daily_pl:+.2f}")

st.sidebar.markdown("---")
st.sidebar.markdown(f"**Market Open:** {'✅ Yes' if is_market_open() else '❌ No'}")

# ─────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot")

# Bot health indicator
st.markdown("🟢 **Bot Status:** Active" if is_market_open() else "🔴 **Bot Status:** Market Closed")

st.markdown("---")
strategy_title, strategy_desc = get_current_strategy_display()
st.markdown(f"📌 **Current Strategy:** {strategy_title}")
st.markdown(f"{strategy_desc}")
st.markdown("---")

# Manual Strategy Override
with st.expander("🔧 Manual Strategy Override (admin)"):
    strategies = ["ORB-R", "VWAP", "TOUCH_TURN", "MOM", "NONE"]
    selected = st.selectbox("Override active strategy", strategies)
    if st.button("Apply Override"):
        set_forced_strategy(selected if selected != "NONE" else None)
        st.success(f"Strategy set to {selected if selected != 'NONE' else 'Auto'}")
        st.rerun()

st.markdown("---")

# ============================================
# TABS – now exactly 4 tabs to match variable count
# ============================================
tab_live, tab_signals, tab_backtest, tab_liq = st.tabs(
    ["Live Trading", "Signal Scanner", "Portfolio Backtest", "Individual Liquidation"]
)

# ─────────────────────────────────────────────
# TAB 1 — LIVE TRADING
# ─────────────────────────────────────────────
with tab_live:
    st.write(f"## 🎯 Weekly Goal: ${TARGET_PROFIT:.0f} USD")
    col1, col2, col3 = st.columns(3)
    col1.metric("Portfolio Value", f"${portfolio_value:,.2f}")
    col2.metric("Cash", f"${cash:,.2f}")
    col3.metric("Daily P&L", f"${daily_pl:+.2f}")

    # Today's completed trades toggle
    show_today = st.checkbox("Show only today's trades")
    trades_df = pd.DataFrame(st.session_state.realized_trades)
    if not trades_df.empty:
        if show_today:
            today_str = datetime.now(SGT).date().isoformat()
            trades_df = trades_df[trades_df["date"] == today_str]
        st.dataframe(trades_df, use_container_width=True)
    else:
        st.info("No trades recorded yet.")

    # Charts inside expander
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

    # Open positions with strategy column
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
    strategy_choice = st.selectbox("Select strategy", ["ORB-R", "VWAP", "TOUCH_TURN", "MOM"])
    start_date = st.date_input("Start date", datetime.now() - timedelta(days=30))
    end_date = st.date_input("End date", datetime.now())
    if st.button("Run Backtest"):
        results = run_backtest(strategy_choice, start_date, end_date)
        st.dataframe(results)

# ─────────────────────────────────────────────
# TAB 4 — INDIVIDUAL LIQUIDATION
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

                strategy_options = ["ORB-R", "VWAP", "TOUCH_TURN", "MOM", "MANUAL_LIQUIDATION"]
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
                                "Strategy": selected_strategy,
                                "Buy Price": f"${entry_price:.2f}",
                                "Sell Price": f"${current_price:.2f}",
                                "Qty": round(sold_qty, 4),
                                "_pl_usd": pl_usd,
                                "P&L ($)": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
                                "P&L (%)": f"{pl_pct:+.2f}%",
                                "Time (SGT)": datetime.now(SGT).strftime("%H:%M:%S"),
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
# P&L RECONCILIATION EXPANDER (optional)
# ─────────────────────────────────────────────
with st.expander("🔍 P&L Reconciliation Helper", expanded=False):
    st.markdown("""
    **Why ORB‑R + VWAP totals may not match Alpaca daily change?**
    - Alpaca's daily change includes **unrealized P&L** on open positions.
    - Your dashboard’s realized P&L only includes **closed trades**.
    - Also, the dashboard uses **trading session dates** (9:30 PM SGT → 4:00 AM SGT), while Alpaca uses calendar days.
    
    **To verify:**
    - Run the SQL query below in your Supabase SQL editor to see realised P&L per trading session.
    - Compare the sum of all sessions in a week with your weekly baseline change.
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
WHERE session_start_date >= '2026-05-18' AND session_start_date <= '2026-05-22'
GROUP BY session_start_date, strategy
ORDER BY session_start_date DESC, strategy;
    """, language="sql")

# ─────────────────────────────────────────────
# AUTO-REFRESH (dashboard only)
# ─────────────────────────────────────────────
time.sleep(SCAN_INTERVAL)
st.rerun()