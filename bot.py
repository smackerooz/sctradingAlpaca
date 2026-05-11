"""
bot.py — Trading Bot (Railway)
Runs 24/7 as a pure Python process. No browser needed.
Reads secrets from environment variables.
Writes all state to Supabase.
"""

import os
import time
import pytz
import logging
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client, Client
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SGT            = pytz.timezone("Asia/Singapore")
SCAN_INTERVAL  = 10          # seconds between scans
MAX_TRADE_USD  = 300.0
CASH_BUFFER    = 95_000.0
TARGET_PROFIT  = 200.0

# UPDATED WATCHLIST
WATCHLIST = [
    "AMD", "NVDA", "QCOM", "MU", "ARM",
    "ASML", "PANW", "SMCI", "AVGO", "KLAC", "AMAT"
]

# Stock profiles remain the same for risk parameters
STOCK_PROFILES = {
    "AMD"   : (0.015, 0.009, 0.007),
    "NVDA"  : (0.018, 0.010, 0.008),
    "QCOM"  : (0.013, 0.008, 0.006),
    "MU"    : (0.013, 0.008, 0.006),
    "ARM"   : (0.018, 0.010, 0.008),
    "ASML"  : (0.013, 0.008, 0.006),
    "PANW"  : (0.013, 0.008, 0.006),
    "SMCI"  : (0.018, 0.010, 0.008),
    "AVGO"  : (0.013, 0.008, 0.006),
    "KLAC"  : (0.013, 0.008, 0.006),
    "AMAT"  : (0.013, 0.008, 0.006),
}
# Default profile for any missing symbols
DEFAULT_PROFILE = (0.013, 0.008, 0.006)

# ─────────────────────────────────────────────
# CLIENTS (from environment variables)
# ─────────────────────────────────────────────
API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
SB_URL     = os.environ["SUPABASE_URL"]
SB_KEY     = os.environ["SUPABASE_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)
supabase: Client = create_client(SB_URL, SB_KEY)

# ─────────────────────────────────────────────
# BOT STATE (in-memory, backed by Supabase)
# ─────────────────────────────────────────────
peak_prices  = {}
local_cash   = None
baseline     = None

# ─────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────
def sb_log(msg: str):
    """Write a log entry to Supabase for dashboard to display."""
    try:
        supabase.table("bot_logs").insert({
            "message":    msg,
            "created_at": datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass
    log.info(msg)

def save_trade(symbol, buy_p, sell_p, qty, entry_price, current_price, reason):
    """Save a completed trade to Supabase."""
    try:
        pl_usd = round((current_price - entry_price) * float(qty), 2)
        pl_pct = round((current_price - entry_price) / entry_price * 100, 2)
        today  = datetime.now(SGT).date().isoformat()
        supabase.table("realized_trades").insert({
            "date":       today,
            "symbol":     symbol,
            "buy_price":  f"${entry_price:.2f}",
            "sell_price": f"${current_price:.2f}",
            "qty":        round(float(qty), 4),
            "pl_usd":     pl_usd,
            "pl_display": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
            "pl_pct":     f"{pl_pct:+.2f}%",
            "time_sgt":   datetime.now(SGT).strftime("%H:%M:%S"),
            "reason":     reason,
        }).execute()
    except Exception as e:
        log.error(f"save_trade error: {e}")

def load_baseline() -> float:
    """Load weekly baseline from Supabase."""
    try:
        row = supabase.table("weekly_baseline").select("*").eq("id", 1).execute()
        if row.data:
            bl         = row.data[0]
            saved_date = datetime.fromisoformat(bl["date"]).date()
            today      = datetime.now(SGT).date()
            last_monday = today - timedelta(days=today.weekday())
            if saved_date >= last_monday:
                return float(bl["baseline"])
    except Exception:
        pass
    try:
        return float(trading_client.get_account().last_equity)
    except Exception:
        return 10_000.0

def save_baseline(value: float):
    """Save weekly baseline to Supabase."""
    try:
        supabase.table("weekly_baseline").upsert({
            "id":       1,
            "baseline": value,
            "date":     datetime.now(SGT).date().isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"save_baseline error: {e}")

def save_peak_prices():
    """Persist peak_prices to Supabase for dashboard display."""
    try:
        supabase.table("bot_state").upsert({
            "id":          1,
            "peak_prices": str(peak_prices),
            "updated_at":  datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass

def send_heartbeat():
    """Write current timestamp to Supabase so dashboard can detect crashes."""
    try:
        supabase.table("bot_state").upsert({
            "id":            1,
            "last_heartbeat": datetime.now(SGT).isoformat(),
            "updated_at":    datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass

# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────
def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def calc_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def rsi_macd_confirmed_buy(df: pd.DataFrame) -> bool:
    """Setup Layer: RSI < 70 and MACD histogram > 0"""
    if df is None or len(df) < 30:
        return True
    close      = df["close"].dropna()
    rsi_val    = calc_rsi(close).iloc[-1]
    _, _, hist = calc_macd(close)
    hist_val   = hist.iloc[-1]
    if pd.isna(rsi_val):  rsi_val  = 50.0
    if pd.isna(hist_val): hist_val = 0.0
    return (rsi_val < 70) and (hist_val > 0)

def profile(symbol: str):
    return STOCK_PROFILES.get(symbol, DEFAULT_PROFILE)

# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────
def get_bars(symbol: str, days_back: int = 2) -> pd.DataFrame:
    """Fetch 5-minute bars for the given symbol."""
    try:
        end   = datetime.now(pytz.utc)
        start = end - timedelta(days=days_back)
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed="iex",
        )
        bars = data_client.get_stock_bars(req).df
        if bars.empty:
            return pd.DataFrame()
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")
        bars.index = pd.to_datetime(bars.index, utc=True)
        return bars[["close"]].copy()
    except Exception:
        return pd.DataFrame()

# ─────────────────────────────────────────────
# MARKET HOURS
# ─────────────────────────────────────────────
def is_market_open() -> bool:
    try:
        clock = trading_client.get_clock()
        if not clock.is_open:
            return False
        if clock.next_open and clock.next_close:
            return clock.next_open > clock.next_close
        return clock.is_open
    except Exception:
        now_sgt      = datetime.now(SGT)
        weekday      = now_sgt.weekday()
        hour, minute = now_sgt.hour, now_sgt.minute
        after_open   = (hour == 21 and minute >= 31) or (hour >= 22)
        before_close = hour < 4 or (hour == 4 and minute == 0)
        is_weekday   = weekday < 5
        if hour < 12:
            is_weekday = (weekday - 1) % 7 < 5
        return is_weekday and (after_open or before_close)

def is_eod_window() -> bool:
    now = datetime.now(SGT)
    return now.weekday() == 4 and now.hour == 3 and 45 <= now.minute < 55

# ─────────────────────────────────────────────
# 3-LAYER ARCHITECTURE
# ─────────────────────────────────────────────

# LAYER 1: REGIME FILTER
def get_market_regime() -> bool:
    """
    Check if QQQ is above its 50-period SMA (5-minute bars) and RSI is not overbought.
    Returns True if regime is favorable for longs.
    """
    try:
        df = get_bars("QQQ", days_back=3)
        if df.empty or len(df) < 50:
            log.warning("⚠️ QQQ data insufficient – defaulting to bullish regime")
            return True
        close = df["close"]
        sma50 = close.rolling(50).mean().iloc[-1]
        current_price = close.iloc[-1]
        if pd.isna(sma50):
            return True
        # Price above 50-SMA and RSI < 70
        rsi = calc_rsi(close).iloc[-1]
        if pd.isna(rsi):
            rsi = 50.0
        regime_ok = (current_price > sma50) and (rsi < 70)
        if not regime_ok:
            log.info(f"🚫 Regime filter: QQQ price=${current_price:.2f} SMA50=${sma50:.2f} RSI={rsi:.1f}")
        return regime_ok
    except Exception as e:
        log.error(f"Regime filter error: {e}")
        return True  # fail open

# LAYER 2: SETUP LOGIC
def is_buy_setup(symbol: str) -> tuple:
    """
    Returns (bool, price) – True if setup conditions are met, and the current price.
    Conditions: Short MA (5) > Long MA (20) + RSI/MACD confirmation.
    """
    df = get_bars(symbol)
    if df.empty or len(df) < 20:
        return False, None
    close = df["close"]
    s_ma  = close.rolling(5).mean().iloc[-1]
    l_ma  = close.rolling(20).mean().iloc[-1]
    curr_p = close.iloc[-1]
    if pd.isna(s_ma) or pd.isna(l_ma):
        return False, None
    if s_ma <= l_ma:
        return False, None
    if not rsi_macd_confirmed_buy(df):
        return False, None
    return True, curr_p

# LAYER 3: EXECUTION (risk management)
def manage_exit(symbol, position, df):
    """
    Evaluate exit conditions for a held position.
    Returns True if position was sold.
    """
    if df.empty or len(df) < 20:
        return False
    close   = df["close"]
    curr_p  = close.iloc[-1]
    s_ma    = close.rolling(5).mean().iloc[-1]
    l_ma    = close.rolling(20).mean().iloc[-1]
    entry_p = float(position.avg_entry_price)
    profit_pct = (curr_p - entry_p) / entry_p
    hard_sl, trail_pct, _ = profile(symbol)

    # Hard stop loss
    if profit_pct <= -hard_sl:
        sell_market(symbol, position.qty, curr_p, f"🛑 HARD STOP ({profit_pct*100:+.2f}%)", entry_p)
        return True

    # Trailing stop
    peak = max(peak_prices.get(symbol, entry_p), curr_p)
    peak_prices[symbol] = peak
    gain_from_entry = (peak - entry_p) / entry_p
    if gain_from_entry >= (trail_pct * 0.5) and curr_p <= peak * (1 - trail_pct):
        sell_market(symbol, position.qty, curr_p, f"📉 TRAIL STOP (peak ${peak:.2f})", entry_p)
        return True

    # Target profit
    if profit_pct >= 0.02:
        sell_market(symbol, position.qty, curr_p, f"✅ TARGET HIT (+{profit_pct*100:.2f}%)", entry_p)
        return True

    # Trend reversal (MA death cross)
    if s_ma < l_ma:
        sell_market(symbol, position.qty, curr_p, f"📉 TREND REVERSED (P&L {profit_pct*100:+.2f}%)", entry_p)
        return True

    return False

# ─────────────────────────────────────────────
# SELLS (helper)
# ─────────────────────────────────────────────
def sell_market(symbol, qty, current_price, reason, entry_price=0.0):
    global local_cash
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty,
            side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        ))
        peak_prices.pop(symbol, None)
        local_cash = None   # force re-fetch after sell
        msg = f"{reason} | SELL {qty} {symbol} @ MARKET (last ${current_price:.2f})"
        sb_log(msg)
        if entry_price > 0:
            save_trade(symbol, entry_price, current_price, qty, entry_price, current_price, reason)
    except Exception as e:
        sb_log(f"⚠️ Sell error {symbol}: {e}")

# ─────────────────────────────────────────────
# MAIN STRATEGY
# ─────────────────────────────────────────────
def run_strategy():
    global local_cash, baseline

    # ── Weekly baseline reset (Monday 21:30 SGT) ────────────────────────
    now = datetime.now(SGT)
    if now.weekday() == 0 and now.hour == 21 and now.minute == 30:
        new_bl = float(trading_client.get_account().equity)
        baseline = new_bl
        save_baseline(new_bl)
        sb_log("🔄 Weekly baseline reset — Monday 21:30 SGT")

    # ── Market hours guard ───────────────────────────────────────────────
    if not is_market_open():
        log.info("💤 Market closed — skipping scan")
        return

    # ── Fetch account ────────────────────────────────────────────────────
    try:
        account   = trading_client.get_account()
        alpaca_bp = float(account.buying_power)
        cash      = min(alpaca_bp, local_cash) if local_cash is not None else alpaca_bp
        positions = trading_client.get_all_positions()
        held      = {p.symbol: p for p in positions}
    except Exception as e:
        sb_log(f"⚠️ Account fetch error: {e}")
        return

    # ── End-of-week liquidation ──────────────────────────────────────────
    if is_eod_window():
        for p in positions:
            try:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                ))
                peak_prices.pop(p.symbol, None)
                sb_log(f"🔔 EOW liquidation: SELL {p.qty} {p.symbol}")
            except Exception as e:
                sb_log(f"⚠️ EOW sell error {p.symbol}: {e}")
        return

    # ── LAYER 3: EXECUTION – Exit existing positions ─────────────────────
    for sym, p in held.items():
        try:
            df = get_bars(sym)
            manage_exit(sym, p, df)
        except Exception as e:
            sb_log(f"⚠️ Exit error {sym}: {e}")

    # ── LAYER 1: REGIME FILTER (check market before new buys) ────────────
    if not get_market_regime():
        log.info("⛔ Regime filter blocks new buys")
        return

    # ── Buy logic (only if cash buffer allows) ───────────────────────────
    if cash <= CASH_BUFFER:
        log.info(f"💤 Cash ${cash:.2f} below buffer — skipping buys")
        return

    for symbol in WATCHLIST:
        if symbol in held:
            continue
        try:
            # LAYER 2: SETUP LOGIC
            setup_ok, curr_p = is_buy_setup(symbol)
            if not setup_ok or curr_p is None:
                continue

            qty         = round(MAX_TRADE_USD / curr_p, 6)
            actual_cost = round(qty * curr_p, 2)
            if qty <= 0 or cash - actual_cost < CASH_BUFFER:
                sb_log(f"💤 SKIP {symbol} — would breach buffer")
                continue

            trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty,
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            ))
            cash        -= actual_cost
            local_cash   = cash
            peak_prices[symbol] = curr_p
            sb_log(f"🟢 BUY {qty} {symbol} @ ${curr_p:.2f} = ${actual_cost:.2f}")
        except Exception as e:
            sb_log(f"⚠️ Buy error {symbol}: {e}")

    save_peak_prices()

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🤖 Trading bot started (3-layer + QQQ filter)")
    baseline = load_baseline()
    log.info(f"📊 Weekly baseline loaded: ${baseline:,.2f}")

    while True:
        try:
            run_strategy()
            send_heartbeat()
        except Exception as e:
            sb_log(f"🔥 Unhandled error in run_strategy: {e}")
        time.sleep(SCAN_INTERVAL)
