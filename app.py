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
SCAN_INTERVAL   = 30        # seconds between auto-scans
MAX_TRADE_USD   = 500.0     # max dollars to spend per trade (dollar-based sizing)

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
                # Dollar-based sizing: buy as many whole shares as fit in MAX_TRADE_USD
                qty = int(MAX_TRADE_USD // current_p)
                if qty < 1:
                    log(f"⚠️ SKIP {symbol} — price ${current_p:.2f} exceeds trade budget ${MAX_TRADE_USD:.0f}")
                    continue
                actual_cost = round(qty * current_p, 2)
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=qty,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                ))
                st.session_state.peak_prices[symbol] = current_p
                log(f"🟢 BUY {qty} {symbol} @ ${current_p:.2f} = ${actual_cost:.2f} (budget ${MAX_TRADE_USD:.0f}, trend +{buy_trend*100:.1f}%)")
        except Exception as e:
            log(f"⚠️ Buy scan error {symbol}: {e}")

    st.session_state.last_scan = datetime.now(SGT)

# ─────────────────────────────────────────────
# 6. BACKTESTING ENGINE (Yahoo Finance)
# ─────────────────────────────────────────────
def run_backtest(symbol: str, period: str, hard_sl: float, trail_pct: float, buy_trend: float, max_trade_usd: float):
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
    position    = 0       # shares held
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
        elif position == 0:
            if price > avg_20 * (1 + buy_trend):
                qty  = int(max_trade_usd // price)   # dollar-based sizing
                cost = qty * price
                if qty < 1 or cash < cost:
                    continue
                cash        -= cost
                position     = qty
                entry_price  = price
                peak_price   = price
                trades.append({
                    "Date":       str(ts)[:16],
                    "Action":     "BUY",
                    "Price":      round(price, 2),
                    "Qty":        qty,
                    "Cost ($)":   round(cost, 2),
                    "P&L ($)":    0.0,
                    "P&L (%)":    "0.00%",
                    "Cash":       round(cash, 2),
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
    st.write(f"**Trade budget:** ${MAX_TRADE_USD:,.0f} per trade")

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
    st.write("Simulates the trailing stop + hard stop strategy on historical hourly data.")

    # ── Mode toggle ──
    bt_mode = st.radio("Mode", ["📊 All Stocks (Leaderboard)", "🔍 Single Stock (Deep Dive)"],
                       horizontal=True)

    st.divider()

    # ── Shared settings ──
    cfg1, cfg2, cfg3 = st.columns(3)
    with cfg1:
        bt_period = st.selectbox("Period", ["1mo", "3mo", "6mo", "1y", "2y"], index=1)
    with cfg2:
        bt_max_usd = st.number_input("Max $ per trade", min_value=100, max_value=50000,
                                     value=500, step=100,
                                     help="Dollar budget per trade — bot buys as many whole shares as fit")
    with cfg3:
        use_profile = st.checkbox("Use per-stock profiles", value=True,
                                  help="Uncheck to apply the same params to all stocks")

    # Override sliders shown only when per-stock profiles are OFF
    if not use_profile:
        ov1, ov2, ov3 = st.columns(3)
        with ov1:
            ov_hard_sl = st.slider("Hard Stop Loss %",   0.005, 0.05, 0.013, 0.001, format="%.3f")
        with ov2:
            ov_trail   = st.slider("Trailing Stop %",    0.005, 0.05, 0.008, 0.001, format="%.3f")
        with ov3:
            ov_trend   = st.slider("Buy Trend Signal %", 0.001, 0.02, 0.006, 0.001, format="%.3f")

    # ── Single stock: symbol + param overrides ──
    if bt_mode == "🔍 Single Stock (Deep Dive)":
        ss1, ss2 = st.columns([1, 3])
        with ss1:
            bt_symbol = st.selectbox("Symbol", WATCHLIST, index=0)
            if use_profile:
                hard_sl_d, trail_d, trend_d = profile(bt_symbol)
                st.caption(f"Profile: SL -{hard_sl_d*100:.1f}% | Trail -{trail_d*100:.1f}% | Trend +{trend_d*100:.1f}%")

    run_bt = st.button("▶️ Run Backtest", type="primary", use_container_width=True)

    # ════════════════════════════════
    # ALL-STOCKS LEADERBOARD
    # ════════════════════════════════
    if run_bt and bt_mode == "📊 All Stocks (Leaderboard)":
        all_results = []
        all_trades  = {}

        progress_bar = st.progress(0, text="Starting...")
        for idx, sym in enumerate(WATCHLIST):
            progress_bar.progress((idx) / len(WATCHLIST),
                                  text=f"Running {sym} ({idx+1}/{len(WATCHLIST)})...")
            hard_sl, trail, trend = profile(sym) if use_profile else (ov_hard_sl, ov_trail, ov_trend)
            res, tlog = run_backtest(sym, bt_period, hard_sl, trail, trend, bt_max_usd)
            if res:
                all_results.append(res)
                all_trades[sym] = tlog
        progress_bar.progress(1.0, text="✅ All done!")

        if not all_results:
            st.error("No data returned for any symbol. Check your internet connection.")
        else:
            # ── Aggregate header metrics ──
            st.write("### 🏆 Portfolio Summary")
            total_combined_pl = sum(r["total_pl"] for r in all_results)
            all_wins          = sum(r["wins"]     for r in all_results)
            all_trades_count  = sum(r["total_trades"] for r in all_results)
            overall_wr        = round(all_wins / all_trades_count * 100, 1) if all_trades_count else 0
            best              = max(all_results, key=lambda r: r["total_pl"])
            worst             = min(all_results, key=lambda r: r["total_pl"])

            h1, h2, h3, h4, h5 = st.columns(5)
            h1.metric("Combined P&L",   f"${total_combined_pl:+,.2f}")
            h2.metric("Total Trades",   all_trades_count)
            h3.metric("Overall Win Rate", f"{overall_wr}%")
            h4.metric("🥇 Best Stock",  best["symbol"],  delta=f"${best['total_pl']:+,.2f}")
            h5.metric("🥀 Worst Stock", worst["symbol"], delta=f"${worst['total_pl']:+,.2f}")

            # ── Leaderboard table ──
            st.write("### 📋 Leaderboard (ranked by P&L)")
            lb_rows = sorted(all_results, key=lambda r: r["total_pl"], reverse=True)
            lb_df   = pd.DataFrame([{
                "Rank":          i + 1,
                "Symbol":        r["symbol"],
                "Total P&L ($)": f"${r['total_pl']:+,.2f}",
                "Final Equity":  f"${r['final_equity']:,.2f}",
                "Trades":        r["total_trades"],
                "Wins":          r["wins"],
                "Losses":        r["losses"],
                "Win Rate":      f"{r['win_rate']}%",
                "Avg Win ($)":   f"${r['avg_win']:+,.2f}",
                "Avg Loss ($)":  f"${r['avg_loss']:,.2f}",
            } for i, r in enumerate(lb_rows)])
            st.dataframe(lb_df, use_container_width=True, hide_index=True)

            # ── P&L bar chart across all stocks ──
            st.write("### 📊 P&L Comparison Chart")
            sorted_syms = [r["symbol"]   for r in lb_rows]
            sorted_pls  = [r["total_pl"] for r in lb_rows]
            bar_colors  = ["#26a65b" if v >= 0 else "#e74c3c" for v in sorted_pls]

            fig_bar = go.Figure(go.Bar(
                x=sorted_syms, y=sorted_pls,
                marker_color=bar_colors,
                text=[f"${v:+,.0f}" for v in sorted_pls],
                textposition="outside",
            ))
            fig_bar.update_layout(
                height=350, template="plotly_dark",
                yaxis_title="Total P&L (USD)",
                margin=dict(l=0, r=0, t=30, b=0),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

            # ── Win rate comparison ──
            st.write("### 🎯 Win Rate by Stock")
            wr_syms   = [r["symbol"]   for r in lb_rows]
            wr_vals   = [r["win_rate"] for r in lb_rows]
            wr_colors = ["#26a65b" if v >= 50 else "#e74c3c" for v in wr_vals]

            fig_wr = go.Figure(go.Bar(
                x=wr_syms, y=wr_vals,
                marker_color=wr_colors,
                text=[f"{v}%" for v in wr_vals],
                textposition="outside",
            ))
            fig_wr.add_hline(y=50, line_dash="dot", line_color="white",
                             annotation_text="50% break-even line")
            fig_wr.update_layout(
                height=320, template="plotly_dark",
                yaxis_title="Win Rate (%)", yaxis_range=[0, 105],
                margin=dict(l=0, r=0, t=30, b=0),
            )
            st.plotly_chart(fig_wr, use_container_width=True)

            # ── Per-stock drill-down expanders ──
            st.write("### 🔎 Per-Stock Trade Logs")
            for r in lb_rows:
                sym  = r["symbol"]
                tlog = all_trades.get(sym)
                label = f"{'🟢' if r['total_pl'] >= 0 else '🔴'} {sym}  |  P&L: ${r['total_pl']:+,.2f}  |  Trades: {r['total_trades']}  |  Win rate: {r['win_rate']}%"
                with st.expander(label, expanded=False):
                    if tlog is not None and not tlog.empty:
                        st.dataframe(tlog, use_container_width=True)
                    else:
                        st.info("No trades triggered.")

    # ════════════════════════════════
    # SINGLE STOCK DEEP DIVE
    # ════════════════════════════════
    elif run_bt and bt_mode == "🔍 Single Stock (Deep Dive)":
        hard_sl, trail, trend = profile(bt_symbol) if use_profile else (ov_hard_sl, ov_trail, ov_trend)

        with st.spinner(f"Downloading {bt_symbol} data and simulating..."):
            results, trade_log = run_backtest(bt_symbol, bt_period, hard_sl, trail, trend, bt_max_usd)

        if results is None:
            st.error("No data returned from Yahoo Finance. Try a different symbol or period.")
        else:
            st.write(f"### 📊 Results — {bt_symbol}")
            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Final Equity",   f"${results['final_equity']:,.2f}",
                      delta=f"${results['total_pl']:+,.2f}")
            m2.metric("Total Trades",   results["total_trades"])
            m3.metric("Win Rate",       f"{results['win_rate']}%")
            m4.metric("Avg Win",        f"${results['avg_win']:+,.2f}")
            m5.metric("Avg Loss",       f"${results['avg_loss']:,.2f}")
            m6.metric("Wins / Losses",  f"{results['wins']} / {results['losses']}")

            # ── Price chart with trade markers ──
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
                    x=pd.to_datetime(buys["Date"]),  y=buys["Price"],
                    mode="markers", name="Buy",
                    marker=dict(color="lime", size=9, symbol="triangle-up")
                ))
                fig.add_trace(go.Scatter(
                    x=pd.to_datetime(sells["Date"]), y=sells["Price"],
                    mode="markers", name="Sell",
                    marker=dict(color="red", size=9, symbol="triangle-down")
                ))
            fig.update_layout(
                height=400, template="plotly_dark",
                margin=dict(l=0, r=0, t=30, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02)
            )
            st.plotly_chart(fig, use_container_width=True)

            with st.expander("📋 Full Trade Log", expanded=False):
                if trade_log is not None and not trade_log.empty:
                    st.dataframe(trade_log, use_container_width=True)
                else:
                    st.info("No trades were triggered in this period.")

    elif not run_bt:
        st.info("👆 Choose a mode, configure settings, then click **Run Backtest**.")

# ─────────────────────────────────────────────
# 10. AUTO-RERUN LOOP (bot runs automatically on page load)
# ─────────────────────────────────────────────
if st.session_state.bot_running:
    run_strategy()
    time.sleep(SCAN_INTERVAL)
    st.rerun()
