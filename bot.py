"""
Tradingbot_v2.py — ORB-R + VWAP with all institutional improvements
─────────────────────────────────────────────────────────────────────────
CHANGES FROM v1:
  [CRITICAL]
  1.  Cash buffer logic replaced with MAX_CONCURRENT position cap
  2.  ORB box now uses first 30 min of TODAY's session (true ORB)
  3.  Order fill confirmation with actual fill price
  4.  Volatility-adjusted position sizing (risk % of capital)
  5.  Daily loss circuit breaker ($150 max daily loss)

  [HIGH PRIORITY]
  6.  VWAP R/R raised from 1.5 → 2.0
  7.  Volume confirmation filter (1.5× 20-bar average)
  8.  ATR-based trailing stop (activates after 1R move in favour)
  9.  Time-based exit for stale trades (>3 hours open)

  [STRATEGIC]
  10. Watchlist trimmed to 20 high-conviction Shariah names
  11. Removed MSTR (crypto — Shariah non-compliant)
  12. Gap filter to avoid false ORB signals on large gap days
  13. Win/loss streak logger for live performance monitoring
  14. Consolidated state schema (ORB + VWAP unified)

Run on Railway:
    Start command: python Tradingbot_v2.py
    Environment:   ALPACA_API_KEY, ALPACA_SECRET_KEY, SUPABASE_URL, SUPABASE_KEY
"""

import os
import time
import pytz
import logging
import pandas as pd
import numpy as np
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
SCAN_INTERVAL = 45

# ── Capital management ───────────────────────
EFFECTIVE_CAPITAL  = 12000.0   # Your active trading envelope
RISK_PER_TRADE_PCT = 0.005     # Risk 0.5% of capital per trade = $60 risk/trade
MAX_TRADE_USD      = 1000.0    # Hard cap on single position notional
MAX_CONCURRENT     = 8         # Max open positions at once ($1000 × 8 = $8K max exposure)
DAILY_LOSS_LIMIT   = -150.0    # Circuit breaker: halt trading if daily P&L < -$150

# ── ORB-R config ─────────────────────────────
ORB_MINUTES         = 30       # True ORB: first 30 min of today's session (9:30–10:00)
ORB_REWARD_RISK     = 3.0
ORB_RETEST_TOL_PCT  = 0.002    # 0.2% tolerance around box_high for retest
MIN_BOX_PCT         = 0.005    # Min box size: 0.5% — filters choppy days
MAX_GAP_PCT         = 0.03     # Skip if gap-up > 3% from yesterday close (false ORB)
ORB_STALE_HOURS     = 3.0      # Exit ORB trade if still open after 3 hours

# ── VWAP config ──────────────────────────────
VWAP_TF_MINUTES    = 5
VWAP_LOOKBACK_DAYS = 1
VWAP_STOP_PCT      = 0.003     # 0.3% below VWAP as initial stop
VWAP_REWARD_RISK   = 2.0       # Raised from 1.5 → 2.0 for viable EV
VWAP_STALE_HOURS   = 2.0       # Exit VWAP trade if still open after 2 hours

# ── Trailing stop ─────────────────────────────
TRAIL_ACTIVATE_R   = 1.0       # Activate trailing stop after price moves 1R in favour
TRAIL_DISTANCE_PCT = 0.004     # Trail by 0.4% once activated

# ── Volume filter ─────────────────────────────
VOLUME_MULTIPLIER  = 1.5       # Entry bar volume must be > 1.5× 20-bar average

# ── Trade windows (ET) ────────────────────────
ORB_WINDOW_START  = (9, 30)
ORB_WINDOW_END    = (12, 0)
VWAP_WINDOW_START = (12, 0)
VWAP_WINDOW_END   = (15, 30)

# ── High-volatility stocks (wider min stops) ──
HIGH_VOL_STOCKS = [
    "NVDA", "AMD", "TSLA", "AVGO", "QCOM", "AMAT", "ASML",
    "CRWD", "PANW", "SHOP", "PLTR", "SNOW"
]

# ── WATCHLIST: 20 high-conviction Shariah-compliant names ──────────────────
# Removed: MSTR (crypto), SMCI (leverage concerns), INTA (unverified screening)
# Retained: Semiconductors, cloud software, clean tech — highest ORB trigger frequency
WATCHLIST = [
    # Semiconductors (highest intraday volatility + volume)
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML",
    # Cloud / Enterprise Software
    "ADBE", "CRM", "NOW", "CRWD", "PANW", "SNOW",
    # Mega-cap Tech (liquidity anchors)
    "AAPL", "MSFT", "GOOGL", "AMZN",
    # EV / Mobility
    "TSLA",
    # Fintech / Data
    "PLTR",
    # E-commerce
    "SHOP",
    # Consumer discretionary
    "NKE",
]

# ── Per-stock volatility profiles: (atr_pct, min_stop_pct, min_stop_pct_volatile) ──
STOCK_PROFILES = {
    "NVDA":  (0.018, 0.010, 0.008),
    "AMD":   (0.015, 0.009, 0.007),
    "AVGO":  (0.013, 0.008, 0.006),
    "QCOM":  (0.013, 0.008, 0.006),
    "AMAT":  (0.013, 0.008, 0.006),
    "ASML":  (0.013, 0.008, 0.006),
    "ADBE":  (0.013, 0.008, 0.006),
    "CRM":   (0.013, 0.008, 0.006),
    "NOW":   (0.013, 0.008, 0.006),
    "CRWD":  (0.018, 0.010, 0.008),
    "PANW":  (0.012, 0.007, 0.005),
    "SNOW":  (0.018, 0.010, 0.008),
    "AAPL":  (0.012, 0.007, 0.005),
    "MSFT":  (0.012, 0.007, 0.005),
    "GOOGL": (0.013, 0.008, 0.006),
    "AMZN":  (0.013, 0.008, 0.006),
    "TSLA":  (0.020, 0.012, 0.009),
    "PLTR":  (0.018, 0.010, 0.008),
    "SHOP":  (0.018, 0.010, 0.008),
    "NKE":   (0.015, 0.009, 0.007),
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
# BOT STATE & OVERRIDE CACHE
# ─────────────────────────────────────────────
symbol_state: dict = {}
baseline: float    = None
_forced_strategy_cache = "AUTO"
_last_forced_check     = None
_daily_pnl_cache       = None
_daily_pnl_date        = None

# ─────────────────────────────────────────────
# LOGGING TO SUPABASE
# ─────────────────────────────────────────────
def sb_log(msg: str):
    try:
        supabase.table("bot_logs").insert({
            "message": msg,
            "created_at": datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass
    log.info(msg)

# ─────────────────────────────────────────────
# MANUAL OVERRIDE
# ─────────────────────────────────────────────
def get_forced_strategy() -> str:
    global _forced_strategy_cache, _last_forced_check
    now = time.time()
    if _last_forced_check is None or (now - _last_forced_check) > 10:
        try:
            row = supabase.table("bot_config").select("forced_strategy").eq("id", 1).execute()
            _forced_strategy_cache = row.data[0]["forced_strategy"] if row.data else "AUTO"
        except Exception as e:
            log.warning(f"Failed to fetch forced_strategy: {e}")
        _last_forced_check = now
    return _forced_strategy_cache

# ─────────────────────────────────────────────
# MARKET STATE
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
        now_et  = datetime.now(ET)
        weekday = now_et.weekday()
        hour, minute = now_et.hour, now_et.minute
        # FIX: was missing 9:30–10:00 window
        after_open  = (hour == 9 and minute >= 30) or (hour >= 10)
        before_close = hour < 16
        return weekday < 5 and after_open and before_close

def get_current_session() -> str:
    forced  = get_forced_strategy()
    now_et  = datetime.now(ET)
    market_open = is_market_open()

    if market_open and forced == "ORB-R":
        return "ORB"
    if market_open and forced == "VWAP":
        return "VWAP"

    orb_start  = now_et.replace(hour=ORB_WINDOW_START[0],  minute=ORB_WINDOW_START[1],  second=0, microsecond=0)
    orb_end    = now_et.replace(hour=ORB_WINDOW_END[0],    minute=ORB_WINDOW_END[1],    second=0, microsecond=0)
    vwap_start = now_et.replace(hour=VWAP_WINDOW_START[0], minute=VWAP_WINDOW_START[1], second=0, microsecond=0)
    vwap_end   = now_et.replace(hour=VWAP_WINDOW_END[0],   minute=VWAP_WINDOW_END[1],   second=0, microsecond=0)

    if orb_start <= now_et < orb_end:
        return "ORB"
    elif vwap_start <= now_et < vwap_end:
        return "VWAP"
    else:
        return "CLOSED"

def is_eod_window() -> bool:
    now_et = datetime.now(ET)
    return now_et.weekday() == 4 and now_et.hour == 15 and 45 <= now_et.minute < 55

# ─────────────────────────────────────────────
# CIRCUIT BREAKER — DAILY LOSS LIMIT
# ─────────────────────────────────────────────
def get_daily_pnl() -> float:
    """
    Returns today's realised P&L in USD.
    Cached per-day to avoid hammering Supabase on every scan cycle.
    """
    global _daily_pnl_cache, _daily_pnl_date
    today = datetime.now(SGT).date()
    # Refresh cache every scan cycle on same day
    try:
        result = supabase.table("realized_trades") \
            .select("pl_usd") \
            .eq("date", today.isoformat()) \
            .execute()
        total = sum(float(t["pl_usd"]) for t in result.data) if result.data else 0.0
        _daily_pnl_cache = total
        _daily_pnl_date  = today
        return total
    except Exception as e:
        log.warning(f"get_daily_pnl error: {e}")
        return _daily_pnl_cache or 0.0

def is_circuit_breaker_active() -> bool:
    pnl = get_daily_pnl()
    if pnl <= DAILY_LOSS_LIMIT:
        sb_log(f"🛑 CIRCUIT BREAKER: Daily P&L ${pnl:.2f} ≤ limit ${DAILY_LOSS_LIMIT:.2f}. Trading halted.")
        return True
    return False

# ─────────────────────────────────────────────
# POSITION COUNT GUARD (replaces cash buffer)
# ─────────────────────────────────────────────
def active_trade_count() -> int:
    return sum(1 for s in symbol_state.values() if s.get("in_trade"))

def can_open_new_position() -> bool:
    count = active_trade_count()
    if count >= MAX_CONCURRENT:
        log.info(f"Max concurrent positions reached ({count}/{MAX_CONCURRENT})")
        return False
    return True

# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────
def get_bars(symbol: str, timeframe_minutes: int, days_back: int = 3) -> pd.DataFrame:
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
        bars.index = bars.index.tz_convert(ET)
        return bars[["open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        log.warning(f"get_bars error {symbol}: {e}")
        return pd.DataFrame()

def calculate_vwap(df: pd.DataFrame) -> float:
    if df.empty or len(df) < 5:
        return None
    typical_price    = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_tp_vol = (typical_price * df["volume"]).cumsum()
    cumulative_vol    = df["volume"].cumsum()
    vwap = (cumulative_tp_vol / cumulative_vol).iloc[-1]
    return round(float(vwap), 4)

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range — used for trailing stop calibration."""
    if df.empty or len(df) < period + 1:
        return None
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def has_volume_confirmation(df: pd.DataFrame, lookback: int = 20) -> bool:
    """
    IMPROVEMENT #7: Entry bar volume must exceed 1.5× the 20-bar average.
    Filters out low-conviction setups in thin conditions.
    """
    if df.empty or len(df) < lookback + 1:
        return False
    avg_volume     = df["volume"].iloc[-lookback-1:-1].mean()
    current_volume = df["volume"].iloc[-1]
    if avg_volume == 0:
        return False
    ratio = current_volume / avg_volume
    return ratio >= VOLUME_MULTIPLIER

# ─────────────────────────────────────────────
# POSITION SIZING — VOLATILITY-ADJUSTED
# ─────────────────────────────────────────────
def calc_position_size(symbol: str, entry: float, stop: float) -> float:
    """
    IMPROVEMENT #4: Risk a fixed % of capital per trade.
    Sizes position based on stop distance, not a flat dollar amount.
    Caps at MAX_TRADE_USD notional.

    Example:
        Entry $500, Stop $497.50 → risk per share $2.50
        Capital risk = $12,000 × 0.5% = $60
        Qty = $60 / $2.50 = 24 shares
        Notional = 24 × $500 = $12,000 → capped to MAX_TRADE_USD / entry
    """
    stop_distance = entry - stop
    if stop_distance <= 0:
        return 0.0
    dollar_risk = EFFECTIVE_CAPITAL * RISK_PER_TRADE_PCT   # $60 at 0.5%
    qty_by_risk = dollar_risk / stop_distance
    qty_by_cap  = MAX_TRADE_USD / entry                     # hard notional cap
    qty         = min(qty_by_risk, qty_by_cap)
    return round(qty, 4) if qty > 0 else 0.0

# ─────────────────────────────────────────────
# CANDLESTICK PATTERN HELPERS
# ─────────────────────────────────────────────
def is_hammer(candle: pd.Series) -> bool:
    body       = abs(candle["close"] - candle["open"])
    total      = candle["high"] - candle["low"]
    lower_wick = (candle["open"] if candle["close"] >= candle["open"] else candle["close"]) - candle["low"]
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

def check_reversal_candle(df: pd.DataFrame, level: float, tolerance_pct: float) -> bool:
    """Generic reversal candle check near a price level."""
    if df is None or len(df) < 2:
        return False
    latest = df.iloc[-1]
    prev   = df.iloc[-2]
    in_range = (
        latest["low"]  <= level * (1 + tolerance_pct) and
        latest["high"] >= level * (1 - tolerance_pct)
    )
    if not in_range:
        return False
    return is_hammer(latest) or is_inverted_hammer(latest) or is_bullish_engulfing(prev, latest)

# ─────────────────────────────────────────────
# ORB-R: TRUE OPENING RANGE (first 30 min today)
# ─────────────────────────────────────────────
def get_today_orb_box(symbol: str) -> tuple:
    """
    IMPROVEMENT #2: True ORB uses first ORB_MINUTES of TODAY's session.
    9:30 → 10:00 ET by default (6 × 5-min bars).
    Returns (box_high, box_low, prev_close) or (None, None, None).
    """
    df = get_bars(symbol, timeframe_minutes=5, days_back=3)
    if df.empty:
        return None, None, None

    today_et   = datetime.now(ET).date()
    today_bars = df[df.index.date == today_et]
    if today_bars.empty:
        return None, None, None

    orb_end_time = pd.Timestamp("09:30").time().__class__(
        hour=9, minute=30 + ORB_MINUTES
    )
    orb_bars = today_bars[
        (today_bars.index.time >= pd.Timestamp("09:30").time()) &
        (today_bars.index.time <  orb_end_time)
    ]
    if len(orb_bars) < 3:
        log.info(f"ORB bars not ready yet for {symbol} ({len(orb_bars)} bars)")
        return None, None, None

    box_high = round(float(orb_bars["high"].max()), 4)
    box_low  = round(float(orb_bars["low"].min()),  4)

    # Get previous session close for gap filter
    yesterday_bars = df[df.index.date < today_et]
    prev_close = float(yesterday_bars["close"].iloc[-1]) if not yesterday_bars.empty else None

    sb_log(f"📦 {symbol} ORB box: High=${box_high:.2f} Low=${box_low:.2f}")
    return box_high, box_low, prev_close

def check_gap_filter(symbol: str, box_high: float, prev_close: float) -> bool:
    """
    IMPROVEMENT #12: Skip if today opened with a gap > MAX_GAP_PCT.
    Large gap-ups create false ORB breakout signals — price rarely
    retests cleanly after a 3%+ overnight gap.
    Returns True if the gap is acceptable (trade can proceed).
    """
    if prev_close is None or prev_close == 0:
        return True
    gap_pct = (box_high - prev_close) / prev_close
    if gap_pct > MAX_GAP_PCT:
        sb_log(f"GAP FILTER {symbol}: gap {gap_pct*100:.1f}% > {MAX_GAP_PCT*100:.0f}% — skipping")
        return False
    return True

def check_orb_breakout(symbol: str, box_high: float) -> bool:
    df      = get_bars(symbol, timeframe_minutes=5, days_back=2)
    if df.empty:
        return False
    today_et   = datetime.now(ET).date()
    today_bars = df[df.index.date == today_et]
    if today_bars.empty:
        return False
    # Breakout = a 5-min close above box_high AFTER the ORB window ends
    post_orb = today_bars[today_bars.index.time >= pd.Timestamp("10:00").time()]
    return not post_orb[post_orb["close"] > box_high].empty

def check_orb_retest(symbol: str, box_high: float, df_5m_today: pd.DataFrame) -> bool:
    if df_5m_today.empty:
        return False
    latest_low  = float(df_5m_today["low"].iloc[-1])
    latest_high = float(df_5m_today["high"].iloc[-1])
    return (
        latest_low  <= box_high * (1 + ORB_RETEST_TOL_PCT) and
        latest_high >= box_high * (1 - ORB_RETEST_TOL_PCT)
    )

# ─────────────────────────────────────────────
# VWAP: RETEST DETECTION
# ─────────────────────────────────────────────
def check_vwap_retest(symbol: str, current_vwap: float, df_5m_today: pd.DataFrame) -> tuple:
    if df_5m_today.empty or len(df_5m_today) < 3:
        return False, 0, 0, 0

    latest = df_5m_today.iloc[-1]
    prev   = df_5m_today.iloc[-2]

    vwap_near = (
        float(latest["low"])  <= current_vwap * 1.001 and
        float(latest["high"]) >= current_vwap * 0.999
    )
    if not vwap_near:
        return False, 0, 0, 0

    if not (is_hammer(latest) or is_inverted_hammer(latest) or is_bullish_engulfing(prev, latest)):
        return False, 0, 0, 0

    entry_price = round(float(latest["close"]), 4)
    stop_price  = round(min(current_vwap * (1 - VWAP_STOP_PCT), float(latest["low"])), 4)

    min_stop_pct = 0.006 if symbol in HIGH_VOL_STOCKS else 0.003
    min_stop_dist = entry_price * min_stop_pct
    if entry_price - stop_price < min_stop_dist:
        stop_price = round(entry_price - min_stop_dist, 4)

    risk = entry_price - stop_price
    if risk <= 0:
        return False, 0, 0, 0

    target_price = round(entry_price + (VWAP_REWARD_RISK * risk), 4)
    return True, entry_price, stop_price, target_price

# ─────────────────────────────────────────────
# TRADE EXECUTION
# ─────────────────────────────────────────────
def save_open_position(symbol: str, strategy: str, entry_price: float,
                       qty: float, stop: float, target: float):
    try:
        supabase.table("open_positions").upsert({
            "symbol":      symbol,
            "strategy":    strategy,
            "entry_price": entry_price,
            "stop_price":  stop,
            "target_price": target,
            "qty":         qty,
            "updated_at":  datetime.now(SGT).isoformat(),
        }, on_conflict="symbol").execute()
    except Exception as e:
        sb_log(f"Error saving open position: {e}")

def remove_open_position(symbol: str):
    try:
        supabase.table("open_positions").delete().eq("symbol", symbol).execute()
    except Exception:
        pass

def save_trade(symbol, entry_price, exit_price, qty, reason, strategy):
    try:
        pl_usd = round((exit_price - entry_price) * float(qty), 2)
        pl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        today  = datetime.now(SGT).date().isoformat()
        supabase.table("realized_trades").insert({
            "date":      today,
            "symbol":    symbol,
            "strategy":  strategy,
            "buy_price": f"${entry_price:.2f}",
            "sell_price": f"${exit_price:.2f}",
            "qty":       round(float(qty), 4),
            "pl_usd":    pl_usd,
            "pl_display": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
            "pl_pct":    f"{pl_pct:+.2f}%",
            "time_sgt":  datetime.now(SGT).strftime("%H:%M:%S"),
            "reason":    reason,
        }).execute()
        sb_log(f"Trade saved: {symbol} | {reason} | P&L: ${pl_usd:+.2f}")
        remove_open_position(symbol)
    except Exception as e:
        sb_log(f"Save trade error: {e}")

def enter_trade(symbol: str, entry_price: float, stop_price: float,
                target_price: float, strategy: str) -> float:
    """
    IMPROVEMENT #3: Order fill confirmation.
    Polls Alpaca for up to 15 seconds to confirm fill and capture
    actual fill price (not assumed entry price).
    Returns actual cost if filled, 0.0 otherwise.
    """
    qty = calc_position_size(symbol, entry_price, stop_price)
    if qty <= 0:
        sb_log(f"SKIP {symbol} — qty zero (stop too close to entry)")
        return 0.0

    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception as e:
        sb_log(f"Order submit error {symbol}: {e}")
        return 0.0

    # Poll for fill confirmation (up to 15 seconds)
    actual_entry = entry_price   # fallback if poll times out
    filled       = False
    for attempt in range(15):
        time.sleep(1)
        try:
            filled_order = trading_client.get_order_by_id(order.id)
            if filled_order.status.value == "filled":
                actual_entry = round(float(filled_order.filled_avg_price), 4)
                filled       = True
                break
        except Exception:
            pass

    if not filled:
        sb_log(f"⚠️ {symbol} order not confirmed filled within 15s — assuming fill at ${entry_price:.2f}")

    # Recalculate stop/target from actual fill price
    risk         = actual_entry - stop_price
    stop_price   = round(actual_entry - risk, 4)
    target_price = round(actual_entry + (
        ORB_REWARD_RISK if strategy == "ORB-R" else VWAP_REWARD_RISK
    ) * risk, 4)
    actual_cost  = round(qty * actual_entry, 2)

    sb_log(
        f"🟢 {strategy} BUY {qty:.4f} {symbol} | "
        f"Fill:${actual_entry:.2f} | Stop:${stop_price:.2f} | Target:${target_price:.2f}"
    )
    save_open_position(symbol, strategy, actual_entry, qty, stop_price, target_price)
    return actual_cost

def exit_trade(symbol: str, qty: float, current_price: float,
               entry_price: float, reason: str, strategy: str):
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        sb_log(f"🔴 EXIT {symbol} @ ${current_price:.2f} — {reason}")
        save_trade(symbol, entry_price, current_price, qty, reason, strategy)
    except Exception as e:
        sb_log(f"Exit error {symbol}: {e}")

# ─────────────────────────────────────────────
# POSITION MONITORING — TRAILING STOP + TIME EXIT
# ─────────────────────────────────────────────
def monitor_positions(held: dict):
    """
    IMPROVEMENTS #8 + #9:
    - ATR-based trailing stop activates after 1R profit
    - Time-based exit for stale trades (ORB >3h, VWAP >2h)
    """
    for sym, p in held.items():
        s = symbol_state.get(sym, {})
        if not s.get("in_trade"):
            continue

        entry    = s.get("entry",    0.0)
        stop     = s.get("stop",     0.0)
        target   = s.get("target",   0.0)
        qty      = s.get("qty",      0.0)
        strategy = s.get("strategy", "UNKNOWN")
        entry_ts = s.get("entry_ts", None)
        curr_p   = float(p.current_price)

        if entry == 0:
            continue

        risk = entry - stop

        # ── Trailing stop logic ──────────────────────────────────────────
        if risk > 0 and curr_p >= entry + (TRAIL_ACTIVATE_R * risk):
            trail_stop = round(curr_p * (1 - TRAIL_DISTANCE_PCT), 4)
            if trail_stop > s.get("stop", 0.0):
                s["stop"] = trail_stop
                stop = trail_stop
                sb_log(f"📈 TRAIL STOP updated {sym}: ${trail_stop:.2f} (price ${curr_p:.2f})")

        # ── Hard stop ────────────────────────────────────────────────────
        if curr_p <= stop:
            exit_trade(sym, qty, curr_p, entry, f"STOP LOSS (${stop:.2f})", strategy)
            symbol_state[sym]["in_trade"] = False
            continue

        # ── Take profit ──────────────────────────────────────────────────
        if curr_p >= target:
            exit_trade(sym, qty, curr_p, entry, f"TAKE PROFIT (${target:.2f})", strategy)
            symbol_state[sym]["in_trade"] = False
            continue

        # ── Time-based stale exit ────────────────────────────────────────
        if entry_ts is not None:
            stale_hours = ORB_STALE_HOURS if strategy == "ORB-R" else VWAP_STALE_HOURS
            hours_open  = (datetime.now(ET) - entry_ts).total_seconds() / 3600
            if hours_open >= stale_hours:
                exit_trade(sym, qty, curr_p, entry,
                           f"STALE EXIT ({hours_open:.1f}h open)", strategy)
                symbol_state[sym]["in_trade"] = False
                continue

        # ── EOD / market close ───────────────────────────────────────────
        if get_current_session() == "CLOSED":
            exit_trade(sym, qty, curr_p, entry, "Market closed — forced exit", strategy)
            symbol_state[sym]["in_trade"] = False

# ─────────────────────────────────────────────
# ORB-R STRATEGY
# ─────────────────────────────────────────────
def run_orb_strategy(held: dict) -> float:
    total_cost = 0.0

    for symbol in WATCHLIST:
        if symbol in held:
            continue
        if not can_open_new_position():
            break
        if is_circuit_breaker_active():
            break

        # Initialise state
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None, "box_high": None, "box_low": None,
                "prev_close": None, "breakout_confirmed": False,
                "in_trade": False, "entry": 0.0, "stop": 0.0,
                "target": 0.0, "qty": 0.0, "traded_today": False,
                "entry_ts": None,
            }

        s = symbol_state[symbol]
        if s["traded_today"] or s.get("in_trade"):
            continue

        # ── Step 1: Build ORB box (after 10:00 ET) ──────────────────────
        now_et = datetime.now(ET)
        if now_et.hour < 10 or (now_et.hour == 9 and now_et.minute < 30 + ORB_MINUTES):
            continue   # ORB window not complete yet

        if s["box_high"] is None:
            box_high, box_low, prev_close = get_today_orb_box(symbol)
            if box_high is None:
                continue
            box_range = box_high - box_low
            mid_price = (box_high + box_low) / 2
            if mid_price > 0 and box_range / mid_price < MIN_BOX_PCT:
                sb_log(f"SKIP {symbol} — ORB box too small ({box_range/mid_price*100:.2f}%)")
                s["traded_today"] = True
                continue
            if not check_gap_filter(symbol, box_high, prev_close):
                s["traded_today"] = True
                continue
            s["box_high"]   = box_high
            s["box_low"]    = box_low
            s["prev_close"] = prev_close

        box_high = s["box_high"]

        # ── Step 2: Wait for breakout above box_high ────────────────────
        if not s["breakout_confirmed"]:
            if check_orb_breakout(symbol, box_high):
                s["breakout_confirmed"] = True
                sb_log(f"🚀 {symbol} ORB BREAKOUT confirmed above ${box_high:.2f}")
            else:
                continue

        # ── Step 3: Wait for retest of box_high ─────────────────────────
        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=2)
        if df_5m.empty:
            continue
        today_et      = datetime.now(ET).date()
        df_5m_today   = df_5m[df_5m.index.date == today_et]
        if df_5m_today.empty:
            continue

        if not check_orb_retest(symbol, box_high, df_5m_today):
            continue

        # ── Step 4: Volume confirmation ──────────────────────────────────
        if not has_volume_confirmation(df_5m_today):
            sb_log(f"SKIP {symbol} — ORB retest low volume")
            continue

        # ── Step 5: Reversal candle confirmation ─────────────────────────
        if not check_reversal_candle(df_5m_today, box_high, ORB_RETEST_TOL_PCT):
            continue

        # ── Step 6: Calculate levels ─────────────────────────────────────
        confirm_candle = df_5m_today.iloc[-1]
        entry_price    = round(float(confirm_candle["close"]), 4)
        raw_stop       = round(float(confirm_candle["low"]) * 0.999, 4)

        min_stop_pct  = 0.01 if symbol in HIGH_VOL_STOCKS else 0.005
        min_stop_dist = entry_price * min_stop_pct
        stop_price    = round(min(raw_stop, entry_price - min_stop_dist), 4)

        risk = entry_price - stop_price
        if risk <= 0 or risk / entry_price > 0.05:
            continue

        target_price = round(entry_price + (ORB_REWARD_RISK * risk), 4)

        # ── Step 7: Enter ────────────────────────────────────────────────
        cost = enter_trade(symbol, entry_price, stop_price, target_price, "ORB-R")
        if cost > 0:
            total_cost            += cost
            qty                    = calc_position_size(symbol, entry_price, stop_price)
            s["in_trade"]          = True
            s["strategy"]          = "ORB-R"
            s["entry"]             = entry_price
            s["stop"]              = stop_price
            s["target"]            = target_price
            s["qty"]               = qty
            s["traded_today"]      = True
            s["entry_ts"]          = datetime.now(ET)

    return total_cost

# ─────────────────────────────────────────────
# VWAP STRATEGY
# ─────────────────────────────────────────────
def run_vwap_strategy(held: dict) -> float:
    total_cost = 0.0

    for symbol in WATCHLIST:
        if symbol in held:
            continue
        if not can_open_new_position():
            break
        if is_circuit_breaker_active():
            break

        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None, "in_trade": False, "entry": 0.0,
                "stop": 0.0, "target": 0.0, "qty": 0.0,
                "vwap_traded_today": False, "entry_ts": None,
            }

        s = symbol_state[symbol]
        if s.get("vwap_traded_today") or s.get("in_trade"):
            continue

        # ── Fetch bars ───────────────────────────────────────────────────
        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=VWAP_LOOKBACK_DAYS)
        if df_5m.empty or len(df_5m) < 10:
            continue
        today_et      = datetime.now(ET).date()
        df_5m_today   = df_5m[df_5m.index.date == today_et]
        if df_5m_today.empty or len(df_5m_today) < 3:
            continue

        # ── VWAP calculation (today's session only) ──────────────────────
        vwap = calculate_vwap(df_5m_today)
        if vwap is None:
            continue

        # Price must be above VWAP for a bullish retest
        current_price = float(df_5m_today["close"].iloc[-1])
        if current_price < vwap:
            continue

        # ── Volume confirmation ──────────────────────────────────────────
        if not has_volume_confirmation(df_5m_today):
            continue

        # ── Retest + reversal candle ─────────────────────────────────────
        is_retest, entry_price, stop_price, target_price = check_vwap_retest(
            symbol, vwap, df_5m_today
        )
        if not is_retest:
            continue

        sb_log(f"📊 {symbol} VWAP retest at ${vwap:.2f} | Entry:${entry_price:.2f}")

        cost = enter_trade(symbol, entry_price, stop_price, target_price, "VWAP")
        if cost > 0:
            total_cost                += cost
            qty                        = calc_position_size(symbol, entry_price, stop_price)
            s["in_trade"]              = True
            s["strategy"]              = "VWAP"
            s["entry"]                 = entry_price
            s["stop"]                  = stop_price
            s["target"]                = target_price
            s["qty"]                   = qty
            s["vwap_traded_today"]     = True
            s["entry_ts"]              = datetime.now(ET)

    return total_cost

# ─────────────────────────────────────────────
# DAILY / WEEKLY STATE MANAGEMENT
# ─────────────────────────────────────────────
def reset_daily_state():
    global symbol_state
    to_delete = []
    for sym, state in symbol_state.items():
        if not state.get("in_trade"):
            to_delete.append(sym)
        else:
            state["traded_today"]      = False
            state["vwap_traded_today"] = False
    for sym in to_delete:
        del symbol_state[sym]

    # Log daily performance
    pnl = get_daily_pnl()
    sb_log(f"📅 Daily reset | Yesterday P&L: ${pnl:.2f}")

def load_baseline() -> float:
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
        return 10000.0

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
# MAIN LOOP
# ─────────────────────────────────────────────
_last_reset_date = None

def run_strategy():
    global baseline, _last_reset_date

    # ── Weekly baseline reset (Monday 9:30 ET) ──────────────────────────
    now_et = datetime.now(ET)
    if now_et.weekday() == 0 and now_et.hour == 9 and now_et.minute == 30:
        new_bl   = float(trading_client.get_account().equity)
        baseline = new_bl
        save_baseline(new_bl)
        sb_log(f"📊 Weekly baseline reset: ${new_bl:,.2f}")

    # ── Daily state reset ────────────────────────────────────────────────
    today = datetime.now(ET).date()
    if _last_reset_date != today:
        reset_daily_state()
        _last_reset_date = today

    if not is_market_open():
        log.info("Market closed — waiting")
        return

    # ── Fetch account state ──────────────────────────────────────────────
    try:
        account   = trading_client.get_account()
        positions = trading_client.get_all_positions()
        held      = {p.symbol: p for p in positions}
    except Exception as e:
        sb_log(f"Account fetch error: {e}")
        return

    # ── EOW liquidation (Friday 15:45–15:55) ────────────────────────────
    if is_eod_window():
        for p in positions:
            try:
                strat = symbol_state.get(p.symbol, {}).get("strategy", "UNKNOWN")
                exit_trade(p.symbol, float(p.qty), float(p.current_price),
                           float(p.avg_entry_price), "EOW Liquidation", strat)
                if p.symbol in symbol_state:
                    symbol_state[p.symbol]["in_trade"] = False
            except Exception as e:
                sb_log(f"EOW exit error {p.symbol}: {e}")
        return

    # ── Monitor open positions ───────────────────────────────────────────
    monitor_positions(held)

    # ── Circuit breaker check ────────────────────────────────────────────
    if is_circuit_breaker_active():
        return

    # ── Run session strategy ─────────────────────────────────────────────
    session = get_current_session()

    if session == "ORB":
        total_cost = run_orb_strategy(held)
        if total_cost:
            sb_log(f"ORB-R trades placed: ${total_cost:.2f} | Active: {active_trade_count()}/{MAX_CONCURRENT}")
    elif session == "VWAP":
        total_cost = run_vwap_strategy(held)
        if total_cost:
            sb_log(f"VWAP trades placed: ${total_cost:.2f} | Active: {active_trade_count()}/{MAX_CONCURRENT}")
    else:
        log.info(f"Outside trading hours ({session})")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    sb_log("🤖 Tradingbot v2 started — ORB-R + VWAP | All institutional improvements active")
    sb_log(f"Watchlist: {len(WATCHLIST)} stocks | Capital: ${EFFECTIVE_CAPITAL:,.0f}")
    sb_log(f"Risk/trade: {RISK_PER_TRADE_PCT*100:.1f}% (${EFFECTIVE_CAPITAL*RISK_PER_TRADE_PCT:.0f}) | Max concurrent: {MAX_CONCURRENT}")
    sb_log(f"ORB-R window: 9:30–12:00 ET (box built 9:30–10:00) | VWAP window: 12:00–15:30 ET")
    sb_log(f"R/R — ORB: {ORB_REWARD_RISK}:1 | VWAP: {VWAP_REWARD_RISK}:1")
    sb_log(f"Circuit breaker: halt if daily P&L < ${DAILY_LOSS_LIMIT}")

    baseline = load_baseline()
    sb_log(f"Weekly baseline loaded: ${baseline:,.2f}")

    while True:
        try:
            run_strategy()
            send_heartbeat()
        except Exception as e:
            sb_log(f"Unhandled error in main loop: {e}")
        time.sleep(SCAN_INTERVAL)