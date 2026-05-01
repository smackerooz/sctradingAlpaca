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
st.set_page_config(page_title="Trading Bot", page_icon="📈", layout="wide")

# ─────────────────────────────────────────────
# 1. INITIALIZE CLIENTS
# ─────────────────────────────────────────────
try:
    API_KEY    = st.secrets["ALPACA_API_KEY"]
    SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)
except Exception as e:
    st.error(f"Missing or invalid Alpaca API Keys in Streamlit Secrets: {e}")
    st.stop()

# ─────────────────────────────────────────────
# 2. CONSTANTS & CONFIG
# ─────────────────────────────────────────────
SGT              = pytz.timezone('Asia/Singapore')
TARGET_PROFIT    = 150.0     # USD (~200 SGD)
CASH_BUFFER      = 90_000.0  # Min cash before buying
WATCHLIST        = ["AAPL", "TSLA", "NVDA", "MSFT", "AMD", "META", "GOOGL", "AMZN"]
SCAN_INTERVAL    = 30        # seconds between auto-scans
TAKE_PROFIT_PCT  = 0.012     # +1.2% → sell for profit
STOP_LOSS_PCT    = 0.007     # -0.7% → cut loss
BUY_TREND_PCT    = 0.005     # price must be 0.5% above avg to trigger buy
BUY_QTY          = 5         # shares per order

# ─────────────────────────────────────────────
# 3. SESSION STATE INIT
# ─────────────────────────────────────────────
if "nightly_baseline" not in st.session_state:
    try:
        st.session_state.nightly_baseline = float(trading_client.get_account().last_equity)
    except:
        st.session_state.nightly_baseline = 100_000.0

if "bot_running" not in st.session_state:
    st.session_state.bot_running = False

if "last_scan" not in st.session_state:
    st.session_state.last_scan = None

if "scan_log" not in st.session_state:
    st.session_state.scan_log = []

# ─────────────────────────────────────────────
# 4. HELPERS
# ─────────────────────────────────────────────
def log(msg: str):
    ts  = datetime.now(SGT).strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    st.session_state.scan_log.insert(0, entry)
    st.session_state.scan_log = st.session_state.scan_log[:50]  # keep last 50

def get_bars(symbol: str, minutes: int = 20):
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=datetime.now() - timedelta(minutes=minutes),
    )
    bars = data_client.get_stock_bars(req)
    return bars.df

def is_eod_window():
    """True during 03:45–03:55 SGT (US market close area)."""
    now = datetime.now(SGT)
    return now.hour == 3 and 45 <= now.minute < 55

def reset_baseline_at_930():
    """Reset baseline at 21:30 SGT (US pre-market open)."""
    now = datetime.now(SGT)
    if now.hour == 21 and now.minute == 30:
        st.session_state.nightly_baseline = float(trading_client.get_account().equity)
        log("🔄 Baseline reset at 21:30 SGT")

# ─────────────────────────────────────────────
# 5. STRATEGY ENGINE
# ─────────────────────────────────────────────
def run_strategy():
    reset_baseline_at_930()

    try:
        account   = trading_client.get_account()
        cash      = float(account.cash)
        positions = trading_client.get_all_positions()
        held      = {p.symbol: p for p in positions}
    except Exception as e:
        log(f"⚠️ Account fetch error: {e}")
        return

    # ── End-of-day flat liquidation ──
    if is_eod_window():
        for p in positions:
            try:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
                log(f"🔔 EOD liquidation: SELL {p.qty} {p.symbol}")
            except Exception as e:
                log(f"⚠️ EOD sell error {p.symbol}: {e}")
        return

    # ── Intraday sell logic (TP / SL) ──
    for sym, p in held.items():
        pl_pct = float(p.unrealized_plpc)
        try:
            if pl_pct >= TAKE_PROFIT_PCT:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=p.qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
                log(f"✅ TAKE PROFIT: SELL {p.qty} {sym} @ +{pl_pct*100:.2f}%")

            elif pl_pct <= -STOP_LOSS_PCT:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=p.qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
                log(f"🛑 STOP LOSS: SELL {p.qty} {sym} @ {pl_pct*100:.2f}%")
        except Exception as e:
            log(f"⚠️ Sell error {sym}: {e}")

    # ── Buy logic ──
    if cash <= CASH_BUFFER:
        log("💤 Cash below buffer — skipping buy scan")
        return

    for symbol in WATCHLIST:
        if symbol in held:
            continue  # already holding
        try:
            df          = get_bars(symbol)
            if df.empty:
                continue
            avg_price   = df["close"].mean()
            current_p   = df["close"].iloc[-1]

            if current_p > avg_price * (1 + BUY_TREND_PCT):
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=BUY_QTY,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                ))
                log(f"🟢 BUY {BUY_QTY} {symbol} | price ${current_p:.2f} vs avg ${avg_price:.2f}")
        except Exception as e:
            log(f"⚠️ Buy scan error {symbol}: {e}")

    st.session_state.last_scan = datetime.now(SGT)

# ─────────────────────────────────────────────
# 6. FETCH LIVE ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account      = trading_client.get_account()
    CASH         = float(account.cash)
    EQUITY       = float(account.equity)
    positions    = trading_client.get_all_positions()
    unrealized   = round(sum(float(p.unrealized_pl) for p in positions), 2)
except:
    CASH, EQUITY, unrealized, positions = 0.0, 0.0, 0.0, []

total_delta  = round(EQUITY - st.session_state.nightly_baseline, 2)
realized     = round(total_delta - unrealized, 2)
progress_pct = min(max(realized / TARGET_PROFIT, 0.0), 1.0) if realized > 0 else 0.0
combined     = round(unrealized + realized, 2)

# ─────────────────────────────────────────────
# 7. SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("🕹️ Bot Controls")
    st.metric("Session Baseline", f"${st.session_state.nightly_baseline:,.2f}")

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("▶️ Start", use_container_width=True, disabled=st.session_state.bot_running):
            st.session_state.bot_running = True
            log("🤖 Bot STARTED")
    with col_b:
        if st.button("⏹ Stop", use_container_width=True, disabled=not st.session_state.bot_running):
            st.session_state.bot_running = False
            log("🛑 Bot STOPPED")

    st.divider()
    if st.button("▶️ Run Single Scan", use_container_width=True):
        run_strategy()
        st.rerun()

    st.divider()
    if st.button("🧹 Manual Liquidation", use_container_width=True):
        try:
            trading_client.cancel_orders()
            time.sleep(1)
            for p in trading_client.get_all_positions():
                lp = round(float(p.current_price) - 0.03, 2)
                trading_client.submit_order(LimitOrderRequest(
                    symbol=p.symbol, qty=p.qty, side=OrderSide.SELL,
                    limit_price=lp, time_in_force=TimeInForce.DAY,
                    extended_hours=True
                ))
            log("🧹 Manual liquidation sent")
            st.sidebar.success("Liquidation orders sent.")
        except Exception as e:
            st.sidebar.error(f"Error: {e}")

    st.divider()
    status_color = "🟢" if st.session_state.bot_running else "🔴"
    st.write(f"**Status:** {status_color} {'RUNNING' if st.session_state.bot_running else 'STOPPED'}")
    if st.session_state.last_scan:
        st.write(f"**Last scan:** {st.session_state.last_scan.strftime('%H:%M:%S')} SGT")
    st.write(f"**Scan interval:** {SCAN_INTERVAL}s")
    st.write(f"**TP:** +{TAKE_PROFIT_PCT*100:.1f}% | **SL:** -{STOP_LOSS_PCT*100:.1f}%")

# ─────────────────────────────────────────────
# 8. MAIN DASHBOARD
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot")
st.write(f"## 🎯 Goal: ${TARGET_PROFIT:.0f} USD (~200 SGD)")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Equity",    f"${EQUITY:,.2f}",   delta=float(combined))
c2.metric("Cash Balance",    f"${CASH:,.2f}")
c3.metric("Realized P&L",    f"${realized:,.2f}")
c4.metric("Unrealized P&L",  f"${unrealized:,.2f}")
st.progress(progress_pct, text=f"Goal Progress: {int(progress_pct*100)}%")

# ── Holdings ──
st.write("### 📦 Live Holdings")
if positions:
    pos_data = [{
        "Symbol":    p.symbol,
        "Qty":       p.qty,
        "Avg Cost":  f"${float(p.avg_entry_price):.2f}",
        "Current":   f"${float(p.current_price):.2f}",
        "Value":     f"${float(p.market_value):,.2f}",
        "P&L ($)":   f"${float(p.unrealized_pl):.2f}",
        "P&L (%)":   f"{float(p.unrealized_plpc)*100:+.2f}%",
    } for p in positions]
    st.dataframe(pd.DataFrame(pos_data), use_container_width=True, height=280)
else:
    st.success("✅ Account is 100% Cash.")

# ── Today's trades ──
with st.expander("📊 Today's Completed Trades", expanded=False):
    try:
        orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200))
        today  = datetime.now(SGT).date()
        daily  = [o for o in orders if o.filled_at and o.filled_at.astimezone(SGT).date() == today]
        if daily:
            vol = sum(float(o.filled_avg_price)*float(o.filled_qty) for o in daily if o.side == OrderSide.BUY)
            st.write(f"**Trades today:** {len(daily)} | **Buy volume:** ${vol:,.2f}")
            rows = [{"Symbol": o.symbol, "Side": o.side.value, "Qty": o.filled_qty,
                     "Price": f"${float(o.filled_avg_price):.2f}",
                     "Value": f"${float(o.filled_avg_price)*float(o.filled_qty):,.2f}",
                     "Time":  o.filled_at.astimezone(SGT).strftime("%H:%M:%S")} for o in daily]
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.info("No trades completed today yet.")
    except:
        st.write("Refreshing data...")

# ── Activity log ──
with st.expander("📋 Activity Log", expanded=True):
    if st.session_state.scan_log:
        for entry in st.session_state.scan_log:
            st.text(entry)
    else:
        st.info("No activity yet. Start the bot or run a scan.")

# ─────────────────────────────────────────────
# 9. AUTO-RERUN LOOP (THE KEY FIX)
# ─────────────────────────────────────────────
if st.session_state.bot_running:
    run_strategy()
    time.sleep(SCAN_INTERVAL)
    st.rerun()
