"""
trading_bot.py — Combined ORB-R (morning) + VWAP Retest (afternoon)
─────────────────────────────────────────────────────────────────────
Strategy 1 (9:30–12:00 ET): Opening Range Breakout with Retest
    - Box yesterday's high/low
    - Wait for 15-min close above box high
    - Wait for retest + 5-min reversal candle
    - Entry at reversal candle close, stop below candle low with minimum 0.5% (1% for volatile)
    - Target 3x risk

Strategy 2 (12:00–15:30 ET): VWAP Retest & Continuation
    - Calculate intraday VWAP from 5-min bars
    - Price must be above VWAP then pull back to touch VWAP
    - Wait for bullish reversal candle at VWAP
    - Entry at candle close, stop with minimum 0.3% (0.6% for volatile) below VWAP or candle low
    - Target 1.5x risk

Run on Railway:
    Start command: python trading_bot.py
    Environment variables: ALPACA_API_KEY, ALPACA_SECRET_KEY,
                           SUPABASE_URL, SUPABASE_KEY
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
SCAN_INTERVAL = 45           # seconds
MAX_TRADE_USD = 750.0        # max dollars per trade (fractional)
CASH_BUFFER   = 95000.0      # min buying power before buying

# ORB-R config
ORB_REWARD_RISK = 3.0        # 3:1 R:R
ORB_BREAKOUT_TF = 15         # minutes
ORB_RETEST_TOLERANCE_PCT = 0.002   # 0.2%
MIN_BOX_PCT = 0.003

# VWAP config
VWAP_TF_MINUTES = 5
VWAP_LOOKBACK_DAYS = 2
VWAP_STOP_PCT = 0.002        # 0.2% below VWAP (baseline, will be adjusted)
VWAP_REWARD_RISK = 1.5       # 1.5:1 R:R
VWAP_END_HOUR = 15
VWAP_END_MINUTE = 30

# Trade windows (ET)
ORB_WINDOW_START = (9, 30)
ORB_WINDOW_END   = (12, 0)
VWAP_WINDOW_START = (12, 0)
VWAP_WINDOW_END   = (15, 30)

# High-volatility stocks that need wider stops
HIGH_VOL_STOCKS = [
    "NVDA", "AMD", "SMCI", "MSTR", "TSLA", "ARM", "SHOP", "DASH", "CRWD", "ZS", "TEAM",
    "MU", "AVGO", "QCOM"  # also moderately volatile
]

# ── FINAL WATCHLIST (50 Shariah-compliant stocks) ──────────────────────────
WATCHLIST = [
    # Original 22
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", "MSTR", "PANW",
    "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD", "ZS",
    # Priority picks (11)
    "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", "SWKS", "FTNT", "ANET",
    # Secondary expansion (7)
    "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    # New additions (9)
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK",
    # Final stock (1)
    "MDT"
]

# ── Per-stock volatility profiles (kept for reference, but stops are now dynamic) ──
STOCK_PROFILES = {
    "NVDA": (0.018, 0.010, 0.008), "AMD": (0.015, 0.009, 0.007),
    "AVGO": (0.013, 0.008, 0.006), "QCOM": (0.013, 0.008, 0.006),
    "AMAT": (0.013, 0.008, 0.006), "ASML": (0.013, 0.008, 0.006),
    "MU": (0.015, 0.009, 0.007), "KLAC": (0.013, 0.008, 0.006),
    "SMCI": (0.020, 0.012, 0.009), "ARM": (0.018, 0.010, 0.008),
    "MSTR": (0.022, 0.014, 0.010), "PANW": (0.012, 0.007, 0.005),
    "TSM": (0.013, 0.008, 0.006), "LRCX": (0.013, 0.008, 0.006),
    "ON": (0.015, 0.009, 0.007), "MPWR": (0.013, 0.008, 0.006),
    "MRVL": (0.013, 0.008, 0.006), "NXPI": (0.013, 0.008, 0.006),
    "TEAM": (0.018, 0.010, 0.008), "INTA": (0.018, 0.010, 0.008),
    "CRWD": (0.018, 0.010, 0.008), "ZS": (0.018, 0.010, 0.008),
    "ADBE": (0.013, 0.008, 0.006), "WDAY": (0.013, 0.008, 0.006),
    "SNPS": (0.013, 0.008, 0.006), "NOW": (0.013, 0.008, 0.006),
    "SHOP": (0.018, 0.010, 0.008), "TXN": (0.012, 0.007, 0.005),
    "CDNS": (0.013, 0.008, 0.006), "MCHP": (0.013, 0.008, 0.006),
    "SWKS": (0.013, 0.008, 0.006), "FTNT": (0.015, 0.009, 0.007),
    "ANET": (0.013, 0.008, 0.006), "UBER": (0.015, 0.009, 0.007),
    "DASH": (0.018, 0.010, 0.008), "TSLA": (0.020, 0.012, 0.009),
    "ISRG": (0.015, 0.009, 0.007), "VRTX": (0.013, 0.008, 0.006),
    "LLY": (0.013, 0.008, 0.006), "MRK": (0.012, 0.007, 0.005),
    "AAPL": (0.012, 0.007, 0.005), "JNJ": (0.010, 0.006, 0.004),
    "PEP": (0.010, 0.006, 0.004), "LIN": (0.012, 0.007, 0.005),
    "REGN": (0.013, 0.008, 0.006), "INTC": (0.013, 0.008, 0.006),
    "PG": (0.010, 0.006, 0.004), "NKE": (0.015, 0.009, 0.007),
    "ADSK": (0.013, 0.008, 0.006), "MDT": (0.012, 0.007, 0.005),
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
symbol_state: dict = {}
baseline: float = None

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

def save_trade(symbol, entry_price, exit_price, qty, reason, strategy):
    try:
        pl_usd = round((exit_price - entry_price) * float(qty), 2)
        pl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        today  = datetime.now(SGT).date().isoformat()
        supabase.table("realized_trades").insert({
            "date":       today,
            "symbol":     symbol,
            "strategy":   strategy,
            "buy_price":  f"${entry_price:.2f}",
            "sell_price": f"${exit_price:.2f}",
            "qty":        round(float(qty), 4),
            "pl_usd":     pl_usd,
            "pl_display": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
            "pl_pct":     f"{pl_pct:+.2f}%",
            "time_sgt":   datetime.now(SGT).strftime("%H:%M:%S"),
            "reason":     reason,
        }).execute()
        sb_log(f"💾 Trade saved: {symbol} {reason}")
    except Exception as e:
        sb_log(f"❌ save_trade error: {e}")

def load_baseline() -> float:
    try:
        row = supabase.table("weekly_baseline").select("*").eq("id", 1).execute()
        if row.data:
            bl = row.data[0]
            saved_date = datetime.fromisoformat(bl["date"]).date()
            today = datetime.now(SGT).date()
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
            "id": 1,
            "baseline": value,
            "date": datetime.now(SGT).date().isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"save_baseline error: {e}")

def send_heartbeat():
    try:
        supabase.table("bot_state").upsert({
            "id": 1,
            "last_heartbeat": datetime.now(SGT).isoformat(),
            "updated_at": datetime.now(SGT).isoformat(),
        }).execute()
    except Exception:
        pass

# ─────────────────────────────────────────────
# MARKET HOURS & SESSION DETECTION
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
        now_et = datetime.now(ET)
        weekday = now_et.weekday()
        hour, minute = now_et.hour, now_et.minute
        after_open = (hour == 9 and minute >= 31) or (hour >= 10)
        before_close = hour < 16
        return weekday < 5 and after_open and before_close

def get_current_session() -> str:
    now_et = datetime.now(ET)
    orb_start = now_et.replace(hour=ORB_WINDOW_START[0], minute=ORB_WINDOW_START[1], second=0, microsecond=0)
    orb_end   = now_et.replace(hour=ORB_WINDOW_END[0], minute=ORB_WINDOW_END[1], second=0, microsecond=0)
    vwap_start = now_et.replace(hour=VWAP_WINDOW_START[0], minute=VWAP_WINDOW_START[1], second=0, microsecond=0)
    vwap_end   = now_et.replace(hour=VWAP_WINDOW_END[0], minute=VWAP_WINDOW_END[1], second=0, microsecond=0)
    
    if orb_start <= now_et <= orb_end:
        return "ORB"
    elif vwap_start <= now_et <= vwap_end:
        return "VWAP"
    else:
        return "CLOSED"

def is_eod_window() -> bool:
    now_et = datetime.now(ET)
    return now_et.weekday() == 4 and now_et.hour == 15 and 45 <= now_et.minute < 55

# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────
def get_bars(symbol: str, timeframe_minutes: int, days_back: int = 3) -> pd.DataFrame:
    try:
        end = datetime.now(pytz.utc)
        start = end - timedelta(days=days_back)
        req = StockBarsRequest(
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
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_tp_vol = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum()
    vwap = (cumulative_tp_vol / cumulative_vol).iloc[-1]
    return round(float(vwap), 4)

# ─────────────────────────────────────────────
# ORB-R SPECIFIC FUNCTIONS
# ─────────────────────────────────────────────
def get_yesterday_box(symbol: str) -> tuple:
    df = get_bars(symbol, timeframe_minutes=15, days_back=5)
    if df.empty:
        return None, None
    today_et = datetime.now(ET).date()
    yesterday = today_et - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)
    session_bars = df[
        (df.index.date == yesterday) &
        (df.index.time >= pd.Timestamp("09:30").time()) &
        (df.index.time <= pd.Timestamp("16:00").time())
    ]
    if session_bars.empty or len(session_bars) < 4:
        return None, None
    box_high = round(float(session_bars["high"].max()), 4)
    box_low = round(float(session_bars["low"].min()), 4)
    return box_high, box_low

def is_hammer(candle: pd.Series) -> bool:
    body = abs(candle["close"] - candle["open"])
    total = candle["high"] - candle["low"]
    lower_wick = candle["open"] - candle["low"] if candle["close"] >= candle["open"] else candle["close"] - candle["low"]
    if total == 0 or body == 0:
        return False
    return (lower_wick >= 2 * body) and (body / total <= 0.35)

def is_inverted_hammer(candle: pd.Series) -> bool:
    body = abs(candle["close"] - candle["open"])
    total = candle["high"] - candle["low"]
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

def check_orb_reversal_candle(df_5m: pd.DataFrame, retest_level: float) -> bool:
    if df_5m is None or len(df_5m) < 2:
        return False
    latest = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]
    candle_low = latest["low"]
    candle_high = latest["high"]
    level_in_range = (candle_low <= retest_level * (1 + ORB_RETEST_TOLERANCE_PCT) and
                      candle_high >= retest_level * (1 - ORB_RETEST_TOLERANCE_PCT))
    if not level_in_range:
        return False
    return is_hammer(latest) or is_inverted_hammer(latest) or is_bullish_engulfing(prev, latest)

def check_orb_breakout(symbol: str, box_high: float) -> bool:
    df_15m = get_bars(symbol, timeframe_minutes=15, days_back=2)
    if df_15m.empty:
        return False
    today_et = datetime.now(ET).date()
    today_bars = df_15m[df_15m.index.date == today_et]
    if today_bars.empty:
        return False
    window_bars = today_bars[today_bars.index.time >= pd.Timestamp("09:30").time()]
    return not window_bars[window_bars["close"] > box_high].empty

def check_orb_retest(symbol: str, box_high: float) -> bool:
    df_5m = get_bars(symbol, timeframe_minutes=5, days_back=2)
    if df_5m.empty:
        return False
    today_et = datetime.now(ET).date()
    today_bars = df_5m[df_5m.index.date == today_et]
    if today_bars.empty:
        return False
    latest_low = float(today_bars["low"].iloc[-1])
    latest_high = float(today_bars["high"].iloc[-1])
    return (latest_low <= box_high * (1 + ORB_RETEST_TOLERANCE_PCT) and
            latest_high >= box_high * (1 - ORB_RETEST_TOLERANCE_PCT))

# ─────────────────────────────────────────────
# VWAP SPECIFIC FUNCTIONS
# ─────────────────────────────────────────────
def check_vwap_retest(symbol: str, current_vwap: float) -> tuple:
    df_5m = get_bars(symbol, timeframe_minutes=5, days_back=VWAP_LOOKBACK_DAYS)
    if df_5m.empty or len(df_5m) < 10:
        return False, 0, 0, 0, 0
    
    today_et = datetime.now(ET).date()
    today_bars = df_5m[df_5m.index.date == today_et]
    if today_bars.empty or len(today_bars) < 3:
        return False, 0, 0, 0, 0
    
    latest = today_bars.iloc[-1]
    prev = today_bars.iloc[-2] if len(today_bars) > 1 else latest
    
    candle_low = float(latest["low"])
    candle_high = float(latest["high"])
    vwap_near = (candle_low <= current_vwap * 1.001 and candle_high >= current_vwap * 0.999)
    
    if not vwap_near:
        return False, 0, 0, 0, 0
    
    hammer = is_hammer(latest)
    inv_hammer = is_inverted_hammer(latest)
    engulfing = is_bullish_engulfing(prev, latest) if len(today_bars) > 1 else False
    
    if not (hammer or inv_hammer or engulfing):
        return False, 0, 0, 0, 0
    
    entry_price = round(float(latest["close"]), 4)
    # Base stop: min(0.2% below VWAP, candle low)
    stop_price = round(min(current_vwap * (1 - VWAP_STOP_PCT), float(latest["low"])), 4)
    
    # Enforce minimum stop percentage
    min_stop_pct = 0.003  # 0.3% for normal stocks
    if symbol in HIGH_VOL_STOCKS:
        min_stop_pct = 0.006  # 0.6% for volatile stocks
    
    min_stop_distance = entry_price * min_stop_pct
    if entry_price - stop_price < min_stop_distance:
        stop_price = entry_price - min_stop_distance
        # Recalculate risk and target if stop changed
        risk = entry_price - stop_price
        if risk <= 0:
            return False, 0, 0, 0, 0
        target_price = round(entry_price + (VWAP_REWARD_RISK * risk), 4)
        sb_log(f"⚠️ Adjusted VWAP {symbol} stop to {stop_price:.2f} (min {min_stop_pct*100:.1f}%)")
        return True, entry_price, stop_price, target_price, float(latest["low"])
    
    risk = entry_price - stop_price
    if risk <= 0:
        return False, 0, 0, 0, 0
    target_price = round(entry_price + (VWAP_REWARD_RISK * risk), 4)
    
    return True, entry_price, stop_price, target_price, float(latest["low"])

# ─────────────────────────────────────────────
# TRADE EXECUTION (shared)
# ─────────────────────────────────────────────
def enter_trade(symbol: str, entry_price: float, stop_price: float,
                target_price: float, cash: float, strategy: str) -> float:
    qty = MAX_TRADE_USD / entry_price
    if qty <= 0:
        sb_log(f"⚠️ SKIP {symbol} — qty too small")
        return 0.0
    actual_cost = round(qty * entry_price, 2)
    if cash - actual_cost < CASH_BUFFER:
        sb_log(f"💤 SKIP {symbol} — would breach cash buffer (cash ${cash:.2f} - ${actual_cost:.2f} < ${CASH_BUFFER})")
        return 0.0
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        sb_log(
            f"🟢 {strategy} BUY {qty:.4f} {symbol} | "
            f"Entry:${entry_price:.2f} | "
            f"Stop:${stop_price:.2f} | "
            f"Target:${target_price:.2f}"
        )
        return actual_cost
    except Exception as e:
        sb_log(f"⚠️ Order error {symbol}: {e}")
        return 0.0

def exit_trade(symbol: str, qty: float, current_price: float, entry_price: float, reason: str, strategy: str):
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
        sb_log(f"⚠️ Exit error {symbol}: {e}")

# ─────────────────────────────────────────────
# MONITOR OPEN POSITIONS (shared)
# ─────────────────────────────────────────────
def monitor_positions(held: dict):
    for sym, p in held.items():
        s = symbol_state.get(sym, {})
        if not s.get("in_trade"):
            continue
        entry = s.get("entry", 0.0)
        stop = s.get("stop", 0.0)
        target = s.get("target", 0.0)
        qty = s.get("qty", 0.0)
        strategy = s.get("strategy", "UNKNOWN")
        curr_p = float(p.current_price)
        
        if entry == 0:
            continue
        
        if curr_p <= stop:
            exit_trade(sym, qty, curr_p, entry, f"🛑 STOP LOSS (hit ${stop:.2f})", strategy)
            symbol_state[sym]["in_trade"] = False
        elif curr_p >= target:
            exit_trade(sym, qty, curr_p, entry, f"🎯 TAKE PROFIT (hit ${target:.2f})", strategy)
            symbol_state[sym]["in_trade"] = False
        elif get_current_session() == "CLOSED":
            exit_trade(sym, qty, curr_p, entry, "⏰ Market closed — forced exit", strategy)
            symbol_state[sym]["in_trade"] = False

# ─────────────────────────────────────────────
# STRATEGY: ORB-R (morning)
# ─────────────────────────────────────────────
def run_orb_strategy(cash: float, held: dict) -> float:
    total_cost = 0.0
    for symbol in WATCHLIST:
        if symbol in held:
            continue
        
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None,
                "box_high": None,
                "box_low": None,
                "breakout_confirmed": False,
                "in_trade": False,
                "entry": 0.0,
                "stop": 0.0,
                "target": 0.0,
                "qty": 0.0,
                "traded_today": False,
            }
        
        s = symbol_state[symbol]
        if s["traded_today"] or s.get("in_trade"):
            continue
        
        # Get box
        if s["box_high"] is None:
            box_high, box_low = get_yesterday_box(symbol)
            if box_high is None:
                continue
            box_range = box_high - box_low
            mid_price = (box_high + box_low) / 2
            if box_range / mid_price < MIN_BOX_PCT:
                sb_log(f"⏭️ SKIP {symbol} — box too small")
                s["traded_today"] = True
                continue
            s["box_high"] = box_high
            s["box_low"] = box_low
        
        box_high = s["box_high"]
        
        # Check breakout
        if not s["breakout_confirmed"]:
            if check_orb_breakout(symbol, box_high):
                s["breakout_confirmed"] = True
                sb_log(f"🚀 {symbol} ORB BREAKOUT confirmed")
            else:
                continue
        
        # Check retest
        if not check_orb_retest(symbol, box_high):
            continue
        
        # Check reversal candle
        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=2)
        if df_5m.empty:
            continue
        today_et = datetime.now(ET).date()
        df_5m_today = df_5m[df_5m.index.date == today_et]
        
        if not check_orb_reversal_candle(df_5m_today, box_high):
            continue
        
        # Calculate entry, stop, target
        confirm_candle = df_5m_today.iloc[-1]
        entry_price = round(float(confirm_candle["close"]), 4)
        # Base stop: 0.1% below candle low
        stop_price = round(float(confirm_candle["low"]) * 0.999, 4)
        if entry_price - stop_price < 0.01:
            stop_price = entry_price - 0.01
        
        # Enforce minimum stop percentage
        min_stop_pct = 0.005  # 0.5% for normal stocks
        if symbol in HIGH_VOL_STOCKS:
            min_stop_pct = 0.01  # 1% for volatile stocks
        
        min_stop_distance = entry_price * min_stop_pct
        if entry_price - stop_price < min_stop_distance:
            stop_price = entry_price - min_stop_distance
            # Recalculate risk and target
            risk = entry_price - stop_price
            if risk <= 0:
                continue
            target_price = round(entry_price + (ORB_REWARD_RISK * risk), 4)
            sb_log(f"⚠️ Adjusted {symbol} stop to {stop_price:.2f} (min {min_stop_pct*100:.1f}%)")
        else:
            risk = entry_price - stop_price
            if risk <= 0 or risk / entry_price > 0.05:
                continue
            target_price = round(entry_price + (ORB_REWARD_RISK * risk), 4)
        
        # Enter trade
        cost = enter_trade(symbol, entry_price, stop_price, target_price, cash - total_cost, "ORB-R")
        if cost > 0:
            total_cost += cost
            s["in_trade"] = True
            s["strategy"] = "ORB-R"
            s["entry"] = entry_price
            s["stop"] = stop_price
            s["target"] = target_price
            s["qty"] = MAX_TRADE_USD / entry_price
            s["traded_today"] = True
    return total_cost

# ─────────────────────────────────────────────
# STRATEGY: VWAP Retest (afternoon)
# ─────────────────────────────────────────────
def run_vwap_strategy(cash: float, held: dict) -> float:
    total_cost = 0.0
    for symbol in WATCHLIST:
        if symbol in held:
            continue
        
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None,
                "in_trade": False,
                "entry": 0.0,
                "stop": 0.0,
                "target": 0.0,
                "qty": 0.0,
                "vwap_traded_today": False,
            }
        
        s = symbol_state[symbol]
        if s.get("vwap_traded_today") or s.get("in_trade"):
            continue
        
        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=1)
        if df_5m.empty or len(df_5m) < 10:
            continue
        
        vwap = calculate_vwap(df_5m)
        if vwap is None:
            continue
        
        current_price = float(df_5m["close"].iloc[-1])
        if current_price < vwap:
            continue
        
        is_retest, entry_price, stop_price, target_price, _ = check_vwap_retest(symbol, vwap)
        if not is_retest:
            continue
        
        sb_log(f"📊 {symbol} VWAP retest detected at ${vwap:.2f}")
        
        cost = enter_trade(symbol, entry_price, stop_price, target_price, cash - total_cost, "VWAP")
        if cost > 0:
            total_cost += cost
            s["in_trade"] = True
            s["strategy"] = "VWAP"
            s["entry"] = entry_price
            s["stop"] = stop_price
            s["target"] = target_price
            s["qty"] = MAX_TRADE_USD / entry_price
            s["vwap_traded_today"] = True
    return total_cost

# ─────────────────────────────────────────────
# RESET DAILY STATE
# ─────────────────────────────────────────────
def reset_daily_state():
    global symbol_state
    to_delete = []
    for sym, state in symbol_state.items():
        if not state.get("in_trade"):
            to_delete.append(sym)
        else:
            state["traded_today"] = False
            state["vwap_traded_today"] = False
    for sym in to_delete:
        del symbol_state[sym]
    sb_log("🔄 Daily state reset")

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
_last_reset_date = None

def run_strategy():
    global baseline, _last_reset_date
    
    now_et = datetime.now(ET)
    if now_et.weekday() == 0 and now_et.hour == 9 and now_et.minute == 30:
        new_bl = float(trading_client.get_account().equity)
        baseline = new_bl
        save_baseline(new_bl)
        sb_log("🔄 Weekly baseline reset")
    
    today = datetime.now(ET).date()
    if _last_reset_date != today:
        reset_daily_state()
        _last_reset_date = today
    
    if not is_market_open():
        log.info("💤 Market closed")
        return
    
    try:
        account = trading_client.get_account()
        cash = float(account.buying_power)
        positions = trading_client.get_all_positions()
        held = {p.symbol: p for p in positions}
    except Exception as e:
        sb_log(f"⚠️ Account error: {e}")
        return
    
    if is_eod_window():
        for p in positions:
            try:
                exit_trade(p.symbol, float(p.qty), float(p.current_price),
                           float(p.avg_entry_price), "EOW Liquidation", "ANY")
                if p.symbol in symbol_state:
                    symbol_state[p.symbol]["in_trade"] = False
            except Exception as e:
                sb_log(f"⚠️ EOW error {p.symbol}: {e}")
        return
    
    monitor_positions(held)
    
    if cash <= CASH_BUFFER:
        log.info(f"💤 Cash ${cash:.2f} below buffer")
        return
    
    session = get_current_session()
    
    if session == "ORB":
        total_cost = run_orb_strategy(cash, held)
        if total_cost:
            sb_log(f"💰 ORB-R trades placed, total cost: ${total_cost:.2f}")
    elif session == "VWAP":
        total_cost = run_vwap_strategy(cash, held)
        if total_cost:
            sb_log(f"💰 VWAP trades placed, total cost: ${total_cost:.2f}")
    else:
        log.info("⏰ Outside trading hours")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    sb_log("🤖 Combined Trading Bot started (ORB-R + VWAP Retest)")
    sb_log(f"   Watchlist: {len(WATCHLIST)} stocks | Max trade: ${MAX_TRADE_USD}")
    sb_log(f"   ORB-R window: {ORB_WINDOW_START[0]}:{ORB_WINDOW_START[1]:02d}–{ORB_WINDOW_END[0]}:{ORB_WINDOW_END[1]:02d} ET")
    sb_log(f"   VWAP window: {VWAP_WINDOW_START[0]}:{VWAP_WINDOW_START[1]:02d}–{VWAP_WINDOW_END[0]}:{VWAP_WINDOW_END[1]:02d} ET")
    sb_log(f"   Stop loss: minimum 0.5% for normal, 1.0% for volatile stocks")
    
    baseline = load_baseline()
    sb_log(f"📊 Weekly baseline: ${baseline:,.2f}")
    
    while True:
        try:
            run_strategy()
            send_heartbeat()
        except Exception as e:
            sb_log(f"🔥 Unhandled error: {e}")
        time.sleep(SCAN_INTERVAL)