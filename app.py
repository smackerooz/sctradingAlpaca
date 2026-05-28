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
# INITIALIZE CLIENTS (unchanged)
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
# CONSTANTS & WATCHLIST (unchanged)
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
    # ... (keep your existing STOCK_PROFILES dictionary exactly as you have it) ...
    # I'm omitting for brevity, but you must keep your original.
}

# ─────────────────────────────────────────────
# SESSION STATE INIT (unchanged)
# ─────────────────────────────────────────────
# ... (keep all your session state initializations exactly as they are) ...

# ─────────────────────────────────────────────
# HELPER FUNCTIONS (all original functions remain unchanged)
# ─────────────────────────────────────────────
# ... (keep parse_datetime, get_trading_session_start, load_realized_trades,
#      load_all_trades, compute_daily_pnl_overview, get_current_strategy_display,
#      set_forced_strategy, log, get_bars, is_eod_window, save_baseline,
#      reset_baseline_if_needed, profile, calc_rsi, calc_macd, compute_signal_score,
#      rsi_macd_confirmed_buy, sell_limit, save_trade_to_supabase, run_strategy,
#      is_market_open, run_backtest, etc. exactly as they were) ...

# ─────────────────────────────────────────────
# FETCH LIVE ACCOUNT DATA (unchanged)
# ─────────────────────────────────────────────
# ... (keep your existing account data fetching and calculations) ...

# ─────────────────────────────────────────────
# AUTO-REFRESH TRADES (unchanged)
# ─────────────────────────────────────────────
# ... (keep your auto-refresh code) ...

# ─────────────────────────────────────────────
# SIDEBAR (unchanged)
# ─────────────────────────────────────────────
# ... (keep your sidebar exactly as it is) ...

# ─────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot")

# Bot health indicator (unchanged)
# ... (keep your bot health indicator code) ...

st.markdown("---")
strategy_title, strategy_desc = get_current_strategy_display()
st.markdown(f"📌 **Current Strategy:** {strategy_title}")
st.markdown(f"{strategy_desc}")
st.markdown("---")

# Manual Strategy Override (unchanged)
# ... (keep your override expander code) ...

st.markdown("---")

# ============================================
# TABS – FOURTH TAB ADDED FOR INDIVIDUAL LIQUIDATION
# ============================================
tab_live, tab_signals, tab_backtest, tab_portfolio, tab_liq = st.tabs(
    ["Live Trading", "Signal Scanner", "Portfolio Backtest", "Individual Liquidation"]
)

# ─────────────────────────────────────────────
# TAB 1 — LIVE TRADING (with charts inside expander)
# ─────────────────────────────────────────────
with tab_live:
    st.write(f"## 🎯 Weekly Goal: ${TARGET_PROFIT:.0f} USD")
    # ... (keep your metrics columns, live holdings, today's completed trades toggle) ...
    
    # ========== MOVED CHARTS INSIDE EXPANDER ==========
    with st.expander("📊 Daily P&L Charts (Bar + Cumulative)", expanded=True):
        st.markdown("### Daily P&L by Trading Session")
        daily_df = compute_daily_pnl_overview()
        if not daily_df.empty:
            fig = go.Figure()
            # ... (your existing bar chart code) ...
            st.plotly_chart(fig, use_container_width=True)
            
            # Cumulative chart
            daily_sorted = daily_df.sort_values("Trading Session Date", ascending=True)
            daily_sorted["Cumulative Total"] = daily_sorted["Total"].cumsum()
            fig_cum = go.Figure()
            # ... (your existing cumulative chart code) ...
            st.plotly_chart(fig_cum, use_container_width=True)
        else:
            st.info("No trade data available yet for daily P&L chart.")
    
    # ========== OPEN POSITIONS WITH STRATEGY COLUMN ==========
    with st.expander("📋 Open Positions (Unrealized)", expanded=False):
        try:
            positions = trading_client.get_all_positions()
            if positions:
                # Try to fetch open positions from Supabase (if bot stores them)
                open_pos_map = {}
                try:
                    open_rows = supabase.table("open_positions").select("symbol", "strategy").execute()
                    for row in open_rows.data:
                        open_pos_map[row["symbol"]] = row["strategy"]
                except:
                    pass  # Table may not exist yet
                
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
    
    # ... (rest of your Live Trading tab: Signal Rankings, Activity Log, etc.) ...

# ─────────────────────────────────────────────
# TAB 2 — SIGNAL SCANNER (unchanged)
# ─────────────────────────────────────────────
with tab_signals:
    # ... (keep your existing signal scanner code) ...

# ─────────────────────────────────────────────
# TAB 3 — PORTFOLIO BACKTEST (unchanged)
# ─────────────────────────────────────────────
with tab_portfolio:
    # ... (keep your existing portfolio backtest code) ...

# ════════════════════════════════════════════
# TAB 4 — INDIVIDUAL LIQUIDATION (NEW)
# ════════════════════════════════════════════
with tab_liq:
    st.write("## 🧹 Individual Position Liquidation")
    st.caption("Sell specific holdings using a limit order (supports extended hours).")

    # PIN state for this feature (reuses same PIN as other protected features)
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
                # Build dropdown options
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

                # Display position details
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Symbol", symbol)
                col2.metric("Quantity", f"{qty:.4f}")
                col3.metric("Current Price", f"${current_price:.2f}")
                col4.metric("Estimated Proceeds", f"${qty * current_price:.2f}")

                # ─── STRATEGY SELECTION (manual, because dashboard doesn't know automatically) ───
                strategy_options = ["ORB-R", "VWAP", "TOUCH_TURN", "MOM", "MANUAL_LIQUIDATION"]
                selected_strategy = st.selectbox("Strategy that opened this position (for correct P&L attribution)", strategy_options, key="liq_strategy")

                # Determine market session
                from datetime import datetime as dt
                now_et = dt.now(ET)
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

                            # --- LOG THE TRADE WITH SELECTED STRATEGY ---
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

                            # Optionally, remove from open_positions if that table exists
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

        # Lock button
        if st.button("🔒 Lock Individual Liquidation", use_container_width=True, key="liq_lock"):
            st.session_state.liq_individual_authorized = False
            st.rerun()

# ========== ADD P&L RECONCILIATION EXPANDER (optional) ==========
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