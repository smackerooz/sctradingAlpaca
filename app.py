import streamlit as st
import pytz
import time
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
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
SGT           = pytz.timezone('Asia/Singapore')
TARGET_PROFIT = 150.0      # USD (~200 SGD)
CASH_BUFFER   = 90_000.0   # Min cash before buying
SCAN_INTERVAL = 30         # seconds between auto-scans
BUY_QTY       = 5          # shares per order

# ── Per-stock volatility profiles ──────────────────────────────────────────
# (hard_stop_loss_pct, trailing_stop_pct, buy_trend_pct)
#
#  hard_stop_loss_pct : immediate sell if price drops this % from entry
#                       wider for volatile stocks (TSLA/NVDA) to avoid noise
#  trailing_stop_pct  : sell if price drops this % below its peak since entry
#                       locks in gains as price rises — replaces fixed take profit
#  buy_trend_pct      : price must be this % above 20-min avg to trigger a buy
# ───────────────────────────────────────────────────────────────────────────
STOCK_PROFILES = {
    "AAPL"  : (0.010, 0.006, 0.004),   # low volatility
    "MSFT"  : (0.010, 0.006, 0.004),   # low volatility
    "GOOGL" : (0.010, 0.006, 0.004),   # low volatility
    "AMZN"  : (0.012, 0.007, 0.005),   # low-mid volatility
    "META"  : (0.013, 0.008, 0.006),   # mid volatility
    "AMD"   : (0.015, 0.009, 0.007),   # mid-high volatility
    "NVDA"  : (0.018, 0.010, 0.008),   # high volatility
    "TSLA"  : (0.020, 0.012, 0.009),   # high volatility
}
WATCHLIST = list(STOCK_PROFILES.keys())

# ─────────────────────────────────────────────
# 3. SESSION STATE INIT
# ─────────────────────────────────────────────
if "nightly_baseline" not in st.session_state:
    try:
        st.session_state.nightly_baseline = float(trading_client.get_account().last_equity)
    except:
        st.session_state.nightly_baseline = 100_000.0

# ── Auto-start: bot is RUNNING by default ──
if "bot_running"  not in st.session_state: st.session_state.bot_running  = True
if "last_scan"    not in st.session_state: st.session_state.last_scan    = None
if "scan_log"     not in st.session_state: st.session_state.scan_log     = []
if "peak_prices"  not in st.session_state: st.session_state.peak_prices  = {}

# ─────────────────────────────────────────────
# 4. HELPERS
# ─────────────────────────────────────────────
def log(msg: str):
    ts    = datetime.now(SGT).strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    st.session_state.scan_log.insert(0, entry)
    st.session_state.scan_log = st.session_state.scan_log[:100]

def get_bars(symbol: str, minutes: int = 20) -> pd.DataFrame:
    req  = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=datetime.now() - timedelta(minutes=minutes),
    )
    bars = data_client.get_stock_bars(req)
    return bars.df

def is_eod_window() -> bool:
    now = datetime.now(SGT)
    return now.hour == 3 and 45 <= now.minute < 55

def reset_baseline_if_needed():
    now = datetime.now(SGT)
    if now.hour == 21 and now.minute == 30:
        st.session_state.nightly_baseline = float(trading_client.get_account().equity)
        log("🔄 Baseline reset at 21:30 SGT")

def profile(symbol: str):
    return STOCK_PROFILES.get(symbol, (0.013, 0.008, 0.006))

def sell_limit(symbol: str, qty, current_price: float, reason: str):
    limit_p = round(current_price - 0.05, 2)
    trading_client.submit_order(LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        limit_price=limit_p,
        time_in_force=TimeInForce.DAY,
    ))
    st.session_state.peak_prices.pop(symbol, None)
    log(f"{reason} | SELL {qty} {symbol} @ limit ${limit_p:.2f}")

# ─────────────────────────────────────────────
# 5. STRATEGY ENGINE
# ─────────────────────────────────────────────
def run_strategy():
    reset_baseline_if_needed()

    try:
        account   = trading_client.get_account()
        cash      = float(account.cash)
        positions = trading_client.get_all_positions()
        held      = {p.symbol: p for p in positions}
    except Exception as e:
        log(f"⚠️ Account fetch error: {e}")
        return

    # ── End-of-day flat market liquidation ──────────────────────────────
    if is_eod_window():
        for p in positions:
            try:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
                st.session_state.peak_prices.pop(p.symbol, None)
                log(f"🔔 EOD liquidation: SELL {p.qty} {p.symbol}")
            except Exception as e:
                log(f"⚠️ EOD sell error {p.symbol}: {e}")
        return

    # ── Intraday exit: trailing stop + hard stop loss ────────────────────
    for sym, p in held.items():
        hard_sl, trail_pct, _ = profile(sym)
        entry_price = float(p.avg_entry_price)
        current_p   = float(p.current_price)
        pl_pct      = float(p.unrealized_plpc)

        # Update peak price tracker
        prev_peak = st.session_state.peak_prices.get(sym, entry_price)
        peak      = max(prev_peak, current_p)
        st.session_state.peak_prices[sym] = peak

        # Trailing stop activates once price moves up at least half the trail distance
        gain_from_entry   = (peak - entry_price) / entry_price
        trail_active      = gain_from_entry >= (trail_pct * 0.5)
        trail_stop_price  = peak * (1 - trail_pct)
        trail_hit         = trail_active and (current_p <= trail_stop_price)
        hard_sl_hit       = pl_pct <= -hard_sl

        try:
            if hard_sl_hit:
                sell_limit(sym, p.qty, current_p,
                    f"🛑 HARD STOP ({pl_pct*100:.2f}%, threshold -{hard_sl*100:.1f}%)")
            elif trail_hit:
                locked = (current_p - entry_price) / entry_price * 100
                sell_limit(sym, p.qty, current_p,
                    f"📉 TRAIL STOP (peak ${peak:.2f} → ${current_p:.2f}, locked {locked:+.2f}%)")
        except Exception as e:
            log(f"⚠️ Exit error {sym}: {e}")

    # ── Buy logic ────────────────────────────────────────────────────────
    if cash <= CASH_BUFFER:
        log("💤 Cash below buffer — skipping buy scan")
        st.session_state.last_scan = datetime.now(SGT)
        return

    for symbol in WATCHLIST:
        if symbol in held:
            continue
        _, _, buy_trend = profile(symbol)
        try:
            df = get_bars(symbol)
            if df.empty:
                continue
            avg_price = df["close"].mean()
            current_p = df["close"].iloc[-1]

            if current_p > avg_price * (1 + buy_trend):
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=BUY_QTY,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                ))
                st.session_state.peak_prices[symbol] = current_p
                log(f"🟢 BUY {BUY_QTY} {symbol} | ${current_p:.2f} vs avg ${avg_price:.2f} (trend +{buy_trend*100:.1f}%)")
        except Exception as e:
            log(f"⚠️ Buy scan error {symbol}: {e}")

    st.session_state.last_scan = datetime.now(SGT)

# ─────────────────────────────────────────────
# 6. BACKTESTING ENGINE (Yahoo Finance)
# ─────────────────────────────────────────────
def run_backtest(symbol: str, period: str, hard_sl: float, trail_pct: float, buy_trend: float, qty: int):
    """
    Simulates the trailing stop strategy on historical Yahoo Finance data.
    Returns a results dict and trade log DataFrame.
    """
    df = yf.download(symbol, period=period, interval="1h", progress=False)
    if df.empty:
        return None, None

    df = df[["Close"]].copy()
    df.columns = ["close"]
    df["avg_20"] = df["close"].rolling(20).mean()
    df.dropna(inplace=True)

    cash        = 100_000.0
    position    = 0
    entry_price = 0.0
    peak_price  = 0.0
    trades      = []

    for i, (ts, row) in enumerate(df.iterrows()):
        price   = float(row["close"])
        avg_20  = float(row["avg_20"])

        # ── Sell logic ──
        if position > 0:
            peak_price = max(peak_price, price)
            gain_from_entry  = (peak_price - entry_price) / entry_price
            trail_active     = gain_from_entry >= (trail_pct * 0.5)
            trail_stop_price = peak_price * (1 - trail_pct)
            trail_hit        = trail_active and (price <= trail_stop_price)
            pl_pct           = (price - entry_price) / entry_price
            hard_hit         = pl_pct <= -hard_sl

            if hard_hit or trail_hit:
                reason   = "HARD SL" if hard_hit else "TRAIL STOP"
                pl_usd   = round((price - entry_price) * position, 2)
                cash    += price * position
                trades.append({
                    "Date":       str(ts)[:16],
                    "Action":     f"SELL ({reason})",
                    "Price":      round(price, 2),
                    "Qty":        position,
                    "P&L ($)":    pl_usd,
                    "P&L (%)":    f"{pl_pct*100:+.2f}%",
                    "Cash":       round(cash, 2),
                })
                position    = 0
                entry_price = 0.0
                peak_price  = 0.0

        # ── Buy logic ──
        elif position == 0 and cash > price * qty:
            if price > avg_20 * (1 + buy_trend):
                cost         = price * qty
                cash        -= cost
                position     = qty
                entry_price  = price
                peak_price   = price
                trades.append({
                    "Date":    str(ts)[:16],
                    "Action":  "BUY",
                    "Price":   round(price, 2),
                    "Qty":     qty,
                    "P&L ($)": 0.0,
                    "P&L (%)": "0.00%",
                    "Cash":    round(cash, 2),
                })

    # Close any open position at last price
    final_equity = cash + (position * float(df["close"].iloc[-1]))

    sells       = [t for t in trades if "SELL" in t["Action"]]
    wins        = [t for t in sells if t["P&L ($)"] > 0]
    losses      = [t for t in sells if t["P&L ($)"] <= 0]
    total_pl    = sum(t["P&L ($)"] for t in sells)
    win_rate    = len(wins) / len(sells) * 100 if sells else 0

    results = {
        "symbol":        symbol,
        "period":        period,
        "start_cash":    100_000.0,
        "final_equity":  round(final_equity, 2),
        "total_pl":      round(total_pl, 2),
        "total_trades":  len(sells),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 1),
        "avg_win":       round(sum(t["P&L ($)"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss":      round(sum(t["P&L ($)"] for t in losses) / len(losses), 2) if losses else 0,
        "df":            df,
    }
    return results, pd.DataFrame(trades)


# ─────────────────────────────────────────────
# 7. FETCH LIVE ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account        = trading_client.get_account()
    CASH           = float(account.cash)
    EQUITY         = float(account.equity)
    positions      = trading_client.get_all_positions()
    unrealized     = round(sum(float(p.unrealized_pl)  for p in positions), 2)
    total_holdings = round(sum(float(p.market_value)   for p in positions), 2)
except:
    CASH, EQUITY, unrealized, total_holdings, positions = 0.0, 0.0, 0.0, 0.0, []

total_delta  = round(EQUITY - st.session_state.nightly_baseline, 2)
realized     = round(total_delta - unrealized, 2)
progress_pct = min(max(realized / TARGET_PROFIT, 0.0), 1.0) if realized > 0 else 0.0
combined     = round(unrealized + realized, 2)

# ─────────────────────────────────────────────
# 8. SIDEBAR
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
            st.session_state.peak_prices = {}
            log("🧹 Manual liquidation sent")
            st.sidebar.success("Liquidation orders sent.")
        except Exception as e:
            st.sidebar.error(f"Error: {e}")

    st.divider()
    status_color = "🟢" if st.session_state.bot_running else "🔴"
    st.write(f"**Status:** {status_color} {'AUTO-RUNNING' if st.session_state.bot_running else 'STOPPED'}")
    if st.session_state.last_scan:
        st.write(f"**Last scan:** {st.session_state.last_scan.strftime('%H:%M:%S')} SGT")
    st.write(f"**Scan interval:** {SCAN_INTERVAL}s")

    st.divider()
    st.write("**Per-stock profiles:**")
    profile_rows = [{"Symbol": sym, "Hard SL": f"-{v[0]*100:.1f}%",
                     "Trail":   f"-{v[1]*100:.1f}%",
                     "Buy Trend": f"+{v[2]*100:.1f}%"}
                    for sym, v in STOCK_PROFILES.items()]
    st.dataframe(pd.DataFrame(profile_rows), use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────
# 9. MAIN DASHBOARD — TABS
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot")

tab_live, tab_backtest = st.tabs(["🔴 Live Trading", "🧪 Backtesting"])

# ════════════════════════════════════════════
# TAB 1 — LIVE TRADING
# ════════════════════════════════════════════
with tab_live:
    st.write(f"## 🎯 Goal: ${TARGET_PROFIT:.0f} USD (~200 SGD)")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Equity",      f"${EQUITY:,.2f}",        delta=float(combined))
    c2.metric("Cash Balance",      f"${CASH:,.2f}")
    c3.metric("Total in Holdings", f"${total_holdings:,.2f}")
    c4.metric("Realized P&L",      f"${realized:,.2f}")
    c5.metric("Unrealized P&L",    f"${unrealized:,.2f}")
    st.progress(progress_pct, text=f"Goal Progress: {int(progress_pct*100)}%")

    # ── Holdings ──
    st.write("### 📦 Live Holdings")
    if positions:
        pos_data = []
        for p in positions:
            hard_sl, trail_pct, _ = profile(p.symbol)
            entry      = float(p.avg_entry_price)
            current    = float(p.current_price)
            peak       = st.session_state.peak_prices.get(p.symbol, entry)
            trail_stop = round(peak * (1 - trail_pct), 2)
            hard_stop  = round(entry * (1 - hard_sl), 2)
            pos_data.append({
                "Symbol":      p.symbol,
                "Qty":         p.qty,
                "Avg Cost":    f"${entry:.2f}",
                "Current":     f"${current:.2f}",
                "Peak":        f"${peak:.2f}",
                "Trail Stop":  f"${trail_stop:.2f}",
                "Hard SL":     f"${hard_stop:.2f}",
                "Value":       f"${float(p.market_value):,.2f}",
                "P&L ($)":     f"${float(p.unrealized_pl):.2f}",
                "P&L (%)":     f"{float(p.unrealized_plpc)*100:+.2f}%",
            })
        st.dataframe(pd.DataFrame(pos_data), use_container_width=True, height=280)
        st.caption(f"📊 Total holdings value: **${total_holdings:,.2f}** across {len(positions)} position(s)")
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
                         "Price":  f"${float(o.filled_avg_price):.2f}",
                         "Value":  f"${float(o.filled_avg_price)*float(o.filled_qty):,.2f}",
                         "Time":   o.filled_at.astimezone(SGT).strftime("%H:%M:%S")} for o in daily]
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
            st.info("No activity yet. Bot is auto-running every 30s.")

# ════════════════════════════════════════════
# TAB 2 — BACKTESTING
# ════════════════════════════════════════════
with tab_backtest:
    st.write("## 🧪 Backtest Strategy on Yahoo Finance Data")
    st.write("Simulates the trailing stop strategy on historical hourly data.")

    col1, col2 = st.columns([1, 2])

    with col1:
        st.write("### ⚙️ Backtest Settings")
        bt_symbol   = st.selectbox("Symbol",  WATCHLIST, index=0)
        bt_period   = st.selectbox("Period",  ["1mo", "3mo", "6mo", "1y", "2y"], index=1)
        bt_qty      = st.number_input("Qty per trade", min_value=1, max_value=100, value=5)

        hard_sl_d, trail_d, trend_d = profile(bt_symbol)
        st.write("**Strategy params** (from per-stock profile — editable):")
        bt_hard_sl  = st.slider("Hard Stop Loss %",   0.005, 0.05, hard_sl_d, 0.001, format="%.3f")
        bt_trail    = st.slider("Trailing Stop %",    0.005, 0.05, trail_d,   0.001, format="%.3f")
        bt_trend    = st.slider("Buy Trend Signal %", 0.001, 0.02, trend_d,   0.001, format="%.3f")

        run_bt = st.button("▶️ Run Backtest", use_container_width=True, type="primary")

    with col2:
        if run_bt:
            with st.spinner(f"Downloading {bt_symbol} data and simulating..."):
                results, trade_log = run_backtest(
                    bt_symbol, bt_period, bt_hard_sl, bt_trail, bt_trend, bt_qty
                )

            if results is None:
                st.error("No data returned from Yahoo Finance. Try a different symbol or period.")
            else:
                # ── Summary metrics ──
                st.write("### 📊 Results")
                m1, m2, m3, m4 = st.columns(4)
                pl_color = "normal" if results["total_pl"] >= 0 else "inverse"
                m1.metric("Final Equity",  f"${results['final_equity']:,.2f}",
                          delta=f"${results['total_pl']:+,.2f}")
                m2.metric("Total Trades",  results["total_trades"])
                m3.metric("Win Rate",      f"{results['win_rate']}%")
                m4.metric("Avg Win / Loss",
                          f"${results['avg_win']:+,.2f} / ${results['avg_loss']:,.2f}")

                w2, w3 = st.columns(2)
                w2.metric("Winning Trades", results["wins"])
                w3.metric("Losing Trades",  results["losses"])

                # ── Price chart with buy/sell markers ──
                st.write("### 📈 Price Chart with Trades")
                df_chart = results["df"].copy()

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=df_chart.index, y=df_chart["close"],
                    mode="lines", name="Price",
                    line=dict(color="#4f8ef7", width=1.5)
                ))
                fig.add_trace(go.Scatter(
                    x=df_chart.index, y=df_chart["avg_20"],
                    mode="lines", name="20-bar Avg",
                    line=dict(color="#f0a500", width=1, dash="dot")
                ))

                if trade_log is not None and not trade_log.empty:
                    buys  = trade_log[trade_log["Action"] == "BUY"]
                    sells = trade_log[trade_log["Action"].str.contains("SELL")]

                    fig.add_trace(go.Scatter(
                        x=pd.to_datetime(buys["Date"]), y=buys["Price"],
                        mode="markers", name="Buy",
                        marker=dict(color="lime", size=9, symbol="triangle-up")
                    ))
                    fig.add_trace(go.Scatter(
                        x=pd.to_datetime(sells["Date"]), y=sells["Price"],
                        mode="markers", name="Sell",
                        marker=dict(color="red", size=9, symbol="triangle-down")
                    ))

                fig.update_layout(
                    height=380, template="plotly_dark",
                    margin=dict(l=0, r=0, t=30, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02)
                )
                st.plotly_chart(fig, use_container_width=True)

                # ── Trade log ──
                with st.expander("📋 Full Trade Log", expanded=False):
                    if trade_log is not None and not trade_log.empty:
                        st.dataframe(trade_log, use_container_width=True)
                    else:
                        st.info("No trades were triggered in this period.")
        else:
            st.info("👈 Configure settings and click **Run Backtest** to simulate the strategy.")

# ─────────────────────────────────────────────
# 10. AUTO-RERUN LOOP (bot runs automatically on page load)
# ─────────────────────────────────────────────
if st.session_state.bot_running:
    run_strategy()
    time.sleep(SCAN_INTERVAL)
    st.rerun()
