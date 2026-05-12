"""
botbounce.py — Opening Range Breakout with Retest (ORB-R)
──────────────────────────────────────────────────────────
Strategy: Box yesterday's regular session high/low as key levels.
          Wait for a 15-min candle to CLOSE above yesterday's high (breakout).
          Wait for price to pull back and RETEST the broken level.
          Confirm with a bullish reversal candle on the 5-min chart
          (Hammer, Inverted Hammer, or Bullish Engulfing).
          Enter long with 3:1 R:R. Stop = below confirmation candle low.
          Target = entry + 3 x risk.
          Only trade within the first 2.5 hours of US market open.
          Long only (Shariah-compliant — no shorting).

Run on Railway:
    Start command: python botbounce.py
    Environment variables: ALPACA_API_KEY, ALPACA_SECRET_KEY,
                           SUPABASE_URL, SUPABASE_KEY
"""

import os
import time
import pytz
import logging
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client, Client
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
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
ET            = pytz.timezone("US/Eastern")
SGT           = pytz.timezone("Asia/Singapore")
SCAN_INTERVAL = 30           # seconds between scans (slower — pattern-based)
MAX_TRADE_USD = 300.0        # max dollars per trade (supports fractional shares)
CASH_BUFFER   = 95_000.0      # min buying power before buying
REWARD_RISK   = 3.0           # 3:1 R:R
TARGET_PROFIT = 200.0         # weekly target USD (not enforced, only baseline)

# Trade window: first 2.5 hours of US market (9:30–12:00 ET)
TRADE_WINDOW_START_H = 9
TRADE_WINDOW_START_M = 30
TRADE_WINDOW_END_H   = 12
TRADE_WINDOW_END_M   = 0

# Breakout confirmation: 15-min candle must CLOSE above box high
BREAKOUT_TF_MINUTES = 15

# Retest confirmation: 5-min candle pattern
RETEST_TF_MINUTES   = 5

# Retest tolerance: how close price must come to box high (% of box range)
RETEST_TOLERANCE_PCT = 0.002   # within 0.2% of the breakout level

# Minimum box size to trade (avoid tiny ranges)
MIN_BOX_PCT = 0.003            # yesterday's range must be >= 0.3% of price

# ── Full watchlist (12 original + 10 new Shariah-compliant) ─────────────
WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", "MSTR", "PANW",
    "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD", "ZS"
]

# ── Per-stock volatility profiles (hard_stop_pct, trailing_stop_pct, buy_trend_pct) ──
# Note: trailing_stop_pct not used in ORB-R (we use fixed stop/target), but kept for future
STOCK_PROFILES = {
    # Original 12
    "NVDA": (0.018, 0.010, 0.008),
    "AMD":  (0.015, 0.009, 0.007),
    "AVGO": (0.013, 0.008, 0.006),
    "QCOM": (0.013, 0.008, 0.006),
    "AMAT": (0.013, 0.008, 0.006),
    "ASML": (0.013, 0.008, 0.006),
    "MU":   (0.015, 0.009, 0.007),
    "KLAC": (0.013, 0.008, 0.006),
    "SMCI": (0.020, 0.012, 0.009),
    "ARM":  (0.018, 0.010, 0.008),
    "MSTR": (0.022, 0.014, 0.010),
    "PANW": (0.012, 0.007, 0.005),
    # New 10 Shariah-compliant
    "TSM":  (0.013, 0.008, 0.006),   # Taiwan Semi
    "LRCX": (0.013, 0.008, 0.006),   # Lam Research
    "ON":   (0.015, 0.009, 0.007),   # ON Semi
    "MPWR": (0.013, 0.008, 0.006),   # Monolithic Power
    "MRVL": (0.013, 0.008, 0.006),   # Marvell
    "NXPI": (0.013, 0.008, 0.006),   # NXP
    "TEAM": (0.018, 0.010, 0.008),   # Atlassian (volatile)
    "INTA": (0.018, 0.010, 0.008),   # Intapp
    "CRWD": (0.018, 0.010, 0.008),   # CrowdStrike
    "ZS":   (0.018, 0.010, 0.008),   # Zscaler
}

# ─────────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────────
API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
SB_URL     = os.environ["SUPABASE_URL"]
SB_KEY     = os.environ["SUPABASE_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)
supabase: Client = create_client(SB_URL, SB_KEY)

# ─────────────────────────────────────────────
# BOT STATE
# ─────────────────────────────────────────────
# Tracks per-symbol state across scans
# {symbol: {"box_high": float, "box_low": float, "breakout_confirmed": bool,
#            "retest_zone": float, "in_trade": bool, "entry": float,
#            "stop": float, "target": float, "qty": float}}
symbol_state: dict = {}
local_cash: float  = None
baseline: float    = None

# ─────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────
def sb_log(msg: str):
    try:
        supabase.table("bot_logs").insert({
            "message":    msg,
            "created_at": datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass
    log.info(msg)

def save_trade(symbol, entry_price, exit_price, qty, reason):
    try:
        pl_usd = round((exit_price - entry_price) * float(qty), 2)
        pl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        today  = datetime.now(SGT).date().isoformat()
        supabase.table("realized_trades").insert({
            "date":       today,
            "symbol":     symbol,
            "buy_price":  f"${entry_price:.2f}",
            "sell_price": f"${exit_price:.2f}",
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
    try:
        row = supabase.table("weekly_baseline").select("*").eq("id", 1).execute()
        if row.data:
            bl          = row.data[0]
            saved_date  = datetime.fromisoformat(bl["date"]).date()
            today       = datetime.now(SGT).date()
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
    try:
        supabase.table("weekly_baseline").upsert({
            "id":       1,
            "baseline": value,
            "date":     datetime.now(SGT).date().isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"save_baseline error: {e}")

def send_heartbeat():
    try:
        supabase.table("bot_state").upsert({
            "id":             1,
            "last_heartbeat": datetime.now(SGT).isoformat(),
            "updated_at":     datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass

# ─────────────────────────────────────────────
# MARKET HOURS
# ─────────────────────────────────────────────
def is_market_open() -> bool:
    """True only during regular US market hours."""
    try:
        clock = trading_client.get_clock()
        if not clock.is_open:
            return False
        if clock.next_open and clock.next_close:
            return clock.next_open > clock.next_close
        return clock.is_open
    except Exception:
        now_et       = datetime.now(ET)
        weekday      = now_et.weekday()
        hour, minute = now_et.hour, now_et.minute
        after_open   = (hour == 9 and minute >= 31) or (hour >= 10)
        before_close = hour < 16
        return weekday < 5 and after_open and before_close

def is_in_trade_window() -> bool:
    """True only within the first 2.5 hours of market open (9:30–12:00 ET)."""
    now_et  = datetime.now(ET)
    open_t  = now_et.replace(hour=TRADE_WINDOW_START_H, minute=TRADE_WINDOW_START_M,
                              second=0, microsecond=0)
    close_t = now_et.replace(hour=TRADE_WINDOW_END_H, minute=TRADE_WINDOW_END_M,
                              second=0, microsecond=0)
    return open_t <= now_et <= close_t

def is_eod_window() -> bool:
    now_et = datetime.now(ET)
    return now_et.weekday() == 4 and now_et.hour == 15 and 45 <= now_et.minute < 55

# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────
def get_bars(symbol: str, timeframe_minutes: int, days_back: int = 3) -> pd.DataFrame:
    """Fetch intraday bars from Alpaca."""
    try:
        end   = datetime.now(pytz.utc)
        start = end - timedelta(days=days_back)
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
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
        # Convert to ET for session filtering
        bars.index = bars.index.tz_convert(ET)
        return bars[["open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        log.warning(f"get_bars error {symbol}: {e}")
        return pd.DataFrame()

def get_yesterday_box(symbol: str) -> tuple:
    """
    Get yesterday's regular session high and low (9:30–16:00 ET).
    Returns (box_high, box_low) or (None, None) if insufficient data.
    """
    df = get_bars(symbol, timeframe_minutes=15, days_back=5)
    if df.empty:
        return None, None

    today_et    = datetime.now(ET).date()
    yesterday   = today_et - timedelta(days=1)
    # Skip weekends — find last trading day
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)

    # Filter yesterday's regular session bars
    session_bars = df[
        (df.index.date == yesterday) &
        (df.index.time >= pd.Timestamp("09:30").time()) &
        (df.index.time <= pd.Timestamp("16:00").time())
    ]

    if session_bars.empty or len(session_bars) < 4:
        log.warning(f"{symbol}: insufficient yesterday bars ({len(session_bars)})")
        return None, None

    box_high = round(float(session_bars["high"].max()), 4)
    box_low  = round(float(session_bars["low"].min()), 4)
    return box_high, box_low

# ─────────────────────────────────────────────
# CANDLESTICK PATTERN DETECTION
# ─────────────────────────────────────────────
def is_hammer(candle: pd.Series) -> bool:
    body      = abs(candle["close"] - candle["open"])
    total     = candle["high"] - candle["low"]
    lower_wick = candle["open"] - candle["low"] if candle["close"] >= candle["open"] \
                 else candle["close"] - candle["low"]
    if total == 0 or body == 0:
        return False
    return (lower_wick >= 2 * body) and (body / total <= 0.35)

def is_inverted_hammer(candle: pd.Series) -> bool:
    body       = abs(candle["close"] - candle["open"])
    total      = candle["high"] - candle["low"]
    upper_wick = candle["high"] - max(candle["close"], candle["open"])
    if total == 0 or body == 0:
        return False
    return (upper_wick >= 2 * body) and (body / total <= 0.35)

def is_bullish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    prev_bearish = prev["close"] < prev["open"]
    curr_bullish = curr["close"] > curr["open"]
    if not prev_bearish or not curr_bullish:
        return False
    return (curr["open"] <= prev["close"]) and (curr["close"] >= prev["open"])

def check_reversal_candle(df_5m: pd.DataFrame, retest_level: float) -> bool:
    if df_5m is None or len(df_5m) < 2:
        return False

    latest = df_5m.iloc[-1]
    prev   = df_5m.iloc[-2]

    candle_low  = latest["low"]
    candle_high = latest["high"]
    level_in_range = (candle_low <= retest_level * (1 + RETEST_TOLERANCE_PCT) and
                      candle_high >= retest_level * (1 - RETEST_TOLERANCE_PCT))

    if not level_in_range:
        return False

    hammer         = is_hammer(latest)
    inv_hammer     = is_inverted_hammer(latest)
    engulfing      = is_bullish_engulfing(prev, latest)

    if hammer:
        sb_log(f"🔨 Hammer detected at retest level ${retest_level:.2f}")
    if inv_hammer:
        sb_log(f"🔄 Inverted Hammer detected at retest level ${retest_level:.2f}")
    if engulfing:
        sb_log(f"🟰 Bullish Engulfing detected at retest level ${retest_level:.2f}")

    return hammer or inv_hammer or engulfing

# ─────────────────────────────────────────────
# BREAKOUT CONFIRMATION (15-min)
# ─────────────────────────────────────────────
def check_breakout_confirmed(symbol: str, box_high: float) -> bool:
    df_15m = get_bars(symbol, timeframe_minutes=15, days_back=2)
    if df_15m.empty:
        return False

    today_et    = datetime.now(ET).date()
    today_bars  = df_15m[df_15m.index.date == today_et]

    if today_bars.empty:
        return False

    window_bars = today_bars[
        today_bars.index.time >= pd.Timestamp(
            f"{TRADE_WINDOW_START_H:02d}:{TRADE_WINDOW_START_M:02d}").time()
    ]

    breakout_bars = window_bars[window_bars["close"] > box_high]
    return not breakout_bars.empty

# ─────────────────────────────────────────────
# RETEST CHECK (5-min)
# ─────────────────────────────────────────────
def check_retest(symbol: str, box_high: float) -> bool:
    df_5m = get_bars(symbol, timeframe_minutes=5, days_back=2)
    if df_5m.empty:
        return False

    today_et   = datetime.now(ET).date()
    today_bars = df_5m[df_5m.index.date == today_et]

    if today_bars.empty:
        return False

    latest_low  = float(today_bars["low"].iloc[-1])
    latest_high = float(today_bars["high"].iloc[-1])

    touched_level = (latest_low <= box_high * (1 + RETEST_TOLERANCE_PCT) and
                     latest_high >= box_high * (1 - RETEST_TOLERANCE_PCT))
    return touched_level

# ─────────────────────────────────────────────
# TRADE EXECUTION (NO BRACKET – fractional shares allowed)
# ─────────────────────────────────────────────
def enter_trade(symbol: str, entry_price: float, stop_price: float,
                target_price: float, cash: float) -> float:
    """
    Submit a simple market buy order (supports fractional shares).
    Returns actual cost deducted from cash, or 0 if failed.
    No bracket – exit will be managed manually in run_strategy().
    """
    qty = MAX_TRADE_USD / entry_price   # fractional allowed
    if qty <= 0:
        sb_log(f"⚠️ SKIP {symbol} — qty too small")
        return 0.0

    actual_cost = round(qty * entry_price, 2)
    if cash - actual_cost < CASH_BUFFER:
        sb_log(f"💤 SKIP {symbol} — would breach cash buffer")
        return 0.0

    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        sb_log(
            f"🟢 ORB-R BUY {qty:.4f} {symbol} | "
            f"Entry:${entry_price:.2f} | "
            f"Stop:${stop_price:.2f} | "
            f"Target:${target_price:.2f} | "
            f"Risk:${(entry_price - stop_price)*qty:.2f} | "
            f"R:R 3:1"
        )
        return actual_cost
    except Exception as e:
        sb_log(f"⚠️ Order error {symbol}: {e}")
        return 0.0

def exit_trade(symbol: str, qty: float, current_price: float, entry_price: float, reason: str):
    """Sell at market (emergency or planned exit)."""
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        sb_log(f"🔴 EXIT {symbol} @ ${current_price:.2f} — {reason}")
        save_trade(symbol, entry_price, current_price, qty, reason)
    except Exception as e:
        sb_log(f"⚠️ Exit error {symbol}: {e}")

# ─────────────────────────────────────────────
# RESET DAILY STATE
# ─────────────────────────────────────────────
def reset_daily_state():
    global symbol_state
    symbol_state = {}
    sb_log("🔄 Daily state reset — new trading day")

# ─────────────────────────────────────────────
# MAIN STRATEGY
# ─────────────────────────────────────────────
_last_reset_date = None

def run_strategy():
    global local_cash, baseline, _last_reset_date

    # ── Weekly baseline reset ────────────────────────────────────────
    now_et = datetime.now(ET)
    if now_et.weekday() == 0 and now_et.hour == 9 and now_et.minute == 30:
        new_bl = float(trading_client.get_account().equity)
        baseline = new_bl
        save_baseline(new_bl)
        sb_log("🔄 Weekly baseline reset — Monday market open")

    # ── Daily state reset (once per day before market open) ─────────
    today = datetime.now(ET).date()
    if _last_reset_date != today:
        reset_daily_state()
        _last_reset_date = today

    # ── Market hours guard ───────────────────────────────────────────
    if not is_market_open():
        log.info("💤 Market closed — skipping scan")
        return

    # ── Fetch account ────────────────────────────────────────────────
    try:
        account   = trading_client.get_account()
        alpaca_bp = float(account.buying_power)
        cash      = min(alpaca_bp, local_cash) if local_cash is not None else alpaca_bp
        positions = trading_client.get_all_positions()
        held      = {p.symbol: p for p in positions}
    except Exception as e:
        sb_log(f"⚠️ Account fetch error: {e}")
        return

    # ── End-of-week liquidation ──────────────────────────────────────
    if is_eod_window():
        for p in positions:
            try:
                exit_trade(p.symbol, float(p.qty), float(p.current_price),
                           float(p.avg_entry_price), "EOW Liquidation")
                symbol_state.pop(p.symbol, None)
            except Exception as e:
                sb_log(f"⚠️ EOW error {p.symbol}: {e}")
        return

    # ── Monitor existing positions (manual stop/target) ──────────────
    for sym, p in held.items():
        s = symbol_state.get(sym, {})
        if not s.get("in_trade"):
            continue
        entry = s.get("entry", 0.0)
        stop  = s.get("stop", 0.0)
        target = s.get("target", 0.0)
        qty   = s.get("qty", 0.0)
        curr_p = float(p.current_price)

        if entry == 0:
            continue

        # Stop loss hit
        if curr_p <= stop:
            exit_trade(sym, qty, curr_p, entry, f"🛑 STOP LOSS (hit ${stop:.2f})")
            symbol_state[sym]["in_trade"] = False
            continue

        # Take profit hit
        if curr_p >= target:
            exit_trade(sym, qty, curr_p, entry, f"🎯 TAKE PROFIT (hit ${target:.2f})")
            symbol_state[sym]["in_trade"] = False
            continue

        # Trade window closed but position still open -> forced exit
        if not is_in_trade_window():
            exit_trade(sym, qty, curr_p, entry, "⏰ Trade window closed — forced exit")
            symbol_state[sym]["in_trade"] = False

    # ── Only look for new entries within trade window ─────────────────
    if not is_in_trade_window():
        log.info("⏰ Outside trade window (9:30–12:00 ET) — no new entries")
        return

    if cash <= CASH_BUFFER:
        log.info(f"💤 Cash ${cash:.2f} below buffer — skipping")
        return

    # ── Scan watchlist for ORB-R setups ──────────────────────────────
    for symbol in WATCHLIST:
        if symbol in held:
            continue  # already in a trade for this symbol

        # Init state for symbol if needed
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "box_high":           None,
                "box_low":            None,
                "breakout_confirmed": False,
                "in_trade":           False,
                "entry":              0.0,
                "stop":               0.0,
                "target":             0.0,
                "qty":                0.0,
                "traded_today":       False,
            }

        s = symbol_state[symbol]

        # Only one trade per symbol per day
        if s["traded_today"]:
            continue

        try:
            # ── STEP 1: Get/cache yesterday's box ───────────────────
            if s["box_high"] is None:
                box_high, box_low = get_yesterday_box(symbol)
                if box_high is None:
                    continue
                box_range = box_high - box_low
                mid_price = (box_high + box_low) / 2
                if box_range / mid_price < MIN_BOX_PCT:
                    sb_log(f"⏭️ SKIP {symbol} — box too small (range {box_range/mid_price*100:.2f}%)")
                    s["traded_today"] = True
                    continue
                s["box_high"] = box_high
                s["box_low"]  = box_low
                sb_log(f"📦 {symbol} box set: High=${box_high:.2f} Low=${box_low:.2f} Range=${box_range:.2f}")

            box_high = s["box_high"]
            box_low  = s["box_low"]

            # ── STEP 2: Check for 15-min breakout ───────────────────
            if not s["breakout_confirmed"]:
                if check_breakout_confirmed(symbol, box_high):
                    s["breakout_confirmed"] = True
                    sb_log(f"🚀 {symbol} BREAKOUT confirmed above ${box_high:.2f} (15-min close)")
                else:
                    continue  # no breakout yet — keep waiting

            # ── STEP 3: Wait for retest of box_high ─────────────────
            if not check_retest(symbol, box_high):
                log.info(f"⏳ {symbol} waiting for retest of ${box_high:.2f}")
                continue

            sb_log(f"🎯 {symbol} RETEST detected at ${box_high:.2f} — checking candle pattern...")

            # ── STEP 4: Confirm reversal candle on 5-min ─────────────
            df_5m = get_bars(symbol, timeframe_minutes=5, days_back=2)
            if df_5m.empty:
                continue
            today_et   = datetime.now(ET).date()
            df_5m_today = df_5m[df_5m.index.date == today_et]

            if not check_reversal_candle(df_5m_today, box_high):
                log.info(f"⏳ {symbol} retest but no reversal candle yet")
                continue

            # ── STEP 5: Calculate entry, stop, target ────────────────
            confirm_candle = df_5m_today.iloc[-1]
            entry_price    = round(float(confirm_candle["close"]), 4)
            stop_price     = round(float(confirm_candle["low"]) * 0.999, 4)  # just below candle low
            # Ensure stop is at least $0.01 below entry (Alpaca rule)
            if entry_price - stop_price < 0.01:
                stop_price = entry_price - 0.01
            risk           = entry_price - stop_price

            if risk <= 0 or risk / entry_price > 0.05:
                sb_log(f"⚠️ SKIP {symbol} — risk invalid or too wide (${risk:.4f})")
                continue

            target_price = round(entry_price + (REWARD_RISK * risk), 4)
            qty          = MAX_TRADE_USD / entry_price   # fractional allowed

            sb_log(
                f"📐 {symbol} setup: Entry=${entry_price:.2f} "
                f"Stop=${stop_price:.2f} Target=${target_price:.2f} "
                f"Risk/share=${risk:.4f} Qty={qty:.4f}"
            )

            # ── STEP 6: Enter trade (simple market order, no bracket) ──
            cost = enter_trade(symbol, entry_price, stop_price, target_price, cash)
            if cost > 0:
                cash                      -= cost
                local_cash                 = cash
                s["in_trade"]              = True
                s["entry"]                 = entry_price
                s["stop"]                  = stop_price
                s["target"]                = target_price
                s["qty"]                   = qty
                s["traded_today"]          = True

        except Exception as e:
            sb_log(f"⚠️ Scan error {symbol}: {e}")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    sb_log("🤖 ORB-R Bot started (Opening Range Breakout with Retest)")
    sb_log(f"   Watchlist: {len(WATCHLIST)} stocks | R:R {REWARD_RISK}:1 | Window: 9:30–12:00 ET")

    baseline = load_baseline()
    sb_log(f"📊 Weekly baseline: ${baseline:,.2f}")

    while True:
        try:
            run_strategy()
            send_heartbeat()
        except Exception as e:
            sb_log(f"🔥 Unhandled error: {e}")
        time.sleep(SCAN_INTERVAL)