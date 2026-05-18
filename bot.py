"""
botscalable.py — Scalable trading bot with strategy registry
Run on Railway:
    Start command: python botscalable.py
    Environment: ALPACA_API_KEY, ALPACA_SECRET_KEY, SUPABASE_URL, SUPABASE_KEY
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
MAX_TRADE_USD = 750.0
CASH_BUFFER   = 95000.0

# Strategy configs (used by individual strategies)
ORB_RETEST_TOLERANCE_PCT = 0.002
MIN_BOX_PCT = 0.003
VWAP_TF_MINUTES = 5
VWAP_LOOKBACK_DAYS = 2
VWAP_STOP_PCT = 0.002

# High‑volatility stocks (wider stops)
HIGH_VOL_STOCKS = [
    "NVDA", "AMD", "SMCI", "MSTR", "TSLA", "ARM", "SHOP", "DASH", "CRWD", "ZS", "TEAM",
    "MU", "AVGO", "QCOM"
]

# ── FINAL WATCHLIST (50 stocks) ──────────────────────────────────────────
WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", "MSTR", "PANW",
    "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", "CRWD", "ZS",
    "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", "SWKS", "FTNT", "ANET",
    "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

# ── Per-stock volatility profiles ─────────────────────────────────────────
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
# BOT STATE & OVERRIDE CACHE
# ─────────────────────────────────────────────
symbol_state: dict = {}
baseline: float = None
_forced_strategy_cache = "AUTO"
_last_forced_check = None

# ─────────────────────────────────────────────
# SHARED UTILITIES
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

def save_trade(symbol, entry_price, exit_price, qty, reason, strategy):
    try:
        pl_usd = round((exit_price - entry_price) * float(qty), 2)
        pl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        today = datetime.now(SGT).date().isoformat()
        supabase.table("realized_trades").insert({
            "date": today,
            "symbol": symbol,
            "strategy": strategy,
            "buy_price": f"${entry_price:.2f}",
            "sell_price": f"${exit_price:.2f}",
            "qty": round(float(qty), 4),
            "pl_usd": pl_usd,
            "pl_display": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
            "pl_pct": f"{pl_pct:+.2f}%",
            "time_sgt": datetime.now(SGT).strftime("%H:%M:%S"),
            "reason": reason,
        }).execute()
        sb_log(f"Trade saved: {symbol} {reason}")
    except Exception as e:
        sb_log(f"Save trade error: {e}")

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1, min_periods=period).mean()
    avg_l = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_g / avg_l.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def enter_trade(symbol: str, entry_price: float, stop_price: float,
                target_price: float, cash: float, strategy: str) -> float:
    qty = MAX_TRADE_USD / entry_price
    if qty <= 0:
        sb_log(f"SKIP {symbol} — qty too small")
        return 0.0
    actual_cost = round(qty * entry_price, 2)
    if cash - actual_cost < CASH_BUFFER:
        sb_log(f"SKIP {symbol} — would breach cash buffer (cash ${cash:.2f} - ${actual_cost:.2f} < ${CASH_BUFFER})")
        return 0.0
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        sb_log(f"🟢 {strategy} BUY {qty:.4f} {symbol} | Entry:${entry_price:.2f} | Stop:${stop_price:.2f} | Target:${target_price:.2f}")
        return actual_cost
    except Exception as e:
        sb_log(f"Order error {symbol}: {e}")
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
        sb_log(f"Exit error {symbol}: {e}")

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
            exit_trade(sym, qty, curr_p, entry, f"STOP LOSS (hit ${stop:.2f})", strategy)
            symbol_state[sym]["in_trade"] = False
        elif curr_p >= target:
            exit_trade(sym, qty, curr_p, entry, f"TAKE PROFIT (hit ${target:.2f})", strategy)
            symbol_state[sym]["in_trade"] = False
        elif get_current_session() == "CLOSED":
            exit_trade(sym, qty, curr_p, entry, "Market closed — forced exit", strategy)
            symbol_state[sym]["in_trade"] = False

def reset_daily_state():
    global symbol_state
    to_delete = []
    for sym, state in symbol_state.items():
        if not state.get("in_trade"):
            to_delete.append(sym)
        else:
            # Reset all daily flags (any key ending with _traded_today)
            for key in list(state.keys()):
                if key.endswith("_traded_today"):
                    state[key] = False
    for sym in to_delete:
        del symbol_state[sym]
    sb_log("Daily state reset")

def get_forced_strategy() -> str:
    """Read manual override from Supabase bot_config table."""
    global _forced_strategy_cache, _last_forced_check
    now = time.time()
    if _last_forced_check is None or (now - _last_forced_check) > 10:
        try:
            row = supabase.table("bot_config").select("forced_strategy").eq("id", 1).execute()
            if row.data:
                _forced_strategy_cache = row.data[0]["forced_strategy"]
            else:
                _forced_strategy_cache = "AUTO"
        except Exception as e:
            log.warning(f"Failed to fetch forced_strategy: {e}")
        _last_forced_check = now
    return _forced_strategy_cache

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

def is_eod_window() -> bool:
    now_et = datetime.now(ET)
    return now_et.weekday() == 4 and now_et.hour == 15 and 45 <= now_et.minute < 55

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
        return 10000.0

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
# ORB-R STRATEGY
# ─────────────────────────────────────────────
ORB_REWARD_RISK = 3.0

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
                "orb_traded_today": False,
            }
        
        s = symbol_state[symbol]
        if s.get("orb_traded_today") or s.get("in_trade"):
            continue
        
        if s["box_high"] is None:
            box_high, box_low = get_yesterday_box(symbol)
            if box_high is None:
                continue
            box_range = box_high - box_low
            mid_price = (box_high + box_low) / 2
            if box_range / mid_price < MIN_BOX_PCT:
                sb_log(f"SKIP {symbol} — box too small")
                s["orb_traded_today"] = True
                continue
            s["box_high"] = box_high
            s["box_low"] = box_low
        
        box_high = s["box_high"]
        
        if not s["breakout_confirmed"]:
            if check_orb_breakout(symbol, box_high):
                s["breakout_confirmed"] = True
                sb_log(f"🚀 {symbol} ORB BREAKOUT confirmed")
            else:
                continue
        
        if not check_orb_retest(symbol, box_high):
            continue
        
        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=2)
        if df_5m.empty:
            continue
        today_et = datetime.now(ET).date()
        df_5m_today = df_5m[df_5m.index.date == today_et]
        
        if not check_orb_reversal_candle(df_5m_today, box_high):
            continue
        
        confirm_candle = df_5m_today.iloc[-1]
        entry_price = round(float(confirm_candle["close"]), 4)
        stop_price = round(float(confirm_candle["low"]) * 0.999, 4)
        if entry_price - stop_price < 0.01:
            stop_price = entry_price - 0.01
        
        min_stop_pct = 0.005 if symbol not in HIGH_VOL_STOCKS else 0.01
        min_stop_distance = entry_price * min_stop_pct
        if entry_price - stop_price < min_stop_distance:
            stop_price = entry_price - min_stop_distance
            risk = entry_price - stop_price
            if risk <= 0:
                continue
            target_price = round(entry_price + (ORB_REWARD_RISK * risk), 4)
            sb_log(f"Adjusted {symbol} stop to {stop_price:.2f} (min {min_stop_pct*100:.1f}%)")
        else:
            risk = entry_price - stop_price
            if risk <= 0 or risk / entry_price > 0.05:
                continue
            target_price = round(entry_price + (ORB_REWARD_RISK * risk), 4)
        
        cost = enter_trade(symbol, entry_price, stop_price, target_price, cash - total_cost, "ORB-R")
        if cost > 0:
            total_cost += cost
            s["in_trade"] = True
            s["strategy"] = "ORB-R"
            s["entry"] = entry_price
            s["stop"] = stop_price
            s["target"] = target_price
            s["qty"] = MAX_TRADE_USD / entry_price
            s["orb_traded_today"] = True
    return total_cost

# ─────────────────────────────────────────────
# VWAP STRATEGY
# ─────────────────────────────────────────────
VWAP_REWARD_RISK = 1.5

def calculate_vwap(df: pd.DataFrame) -> float:
    if df.empty or len(df) < 5:
        return None
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_tp_vol = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum()
    vwap = (cumulative_tp_vol / cumulative_vol).iloc[-1]
    return round(float(vwap), 4)

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
    stop_price = round(min(current_vwap * (1 - VWAP_STOP_PCT), float(latest["low"])), 4)
    
    min_stop_pct = 0.003 if symbol not in HIGH_VOL_STOCKS else 0.006
    min_stop_distance = entry_price * min_stop_pct
    if entry_price - stop_price < min_stop_distance:
        stop_price = entry_price - min_stop_distance
        risk = entry_price - stop_price
        if risk <= 0:
            return False, 0, 0, 0, 0
        target_price = round(entry_price + (VWAP_REWARD_RISK * risk), 4)
        log.info(f"Adjusted VWAP {symbol} stop to {stop_price:.2f} (min {min_stop_pct*100:.1f}%)")
        return True, entry_price, stop_price, target_price, float(latest["low"])
    
    risk = entry_price - stop_price
    if risk <= 0:
        return False, 0, 0, 0, 0
    target_price = round(entry_price + (VWAP_REWARD_RISK * risk), 4)
    
    return True, entry_price, stop_price, target_price, float(latest["low"])

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
# MOM STRATEGY (Momentum Breakout)
# ─────────────────────────────────────────────
MOM_REWARD_RISK = 2.0
MOM_LOOKBACK_MINUTES = 10
MOM_VOLUME_MULTIPLIER = 1.5

def check_mom_breakout(symbol: str) -> tuple:
    """
    Check for momentum breakout on 1-minute bars.
    Returns (is_breakout, entry_price, stop_price, target_price)
    """
    try:
        # Fetch 1-minute bars (last 30 minutes)
        df = get_bars(symbol, timeframe_minutes=1, days_back=1)
        if df.empty or len(df) < 15:
            return False, 0, 0, 0
        
        # Get today's bars only
        today_et = datetime.now(ET).date()
        today_bars = df[df.index.date == today_et]
        if today_bars.empty or len(today_bars) < 11:
            return False, 0, 0, 0
        
        # Calculate highest high of last 10 minutes (excluding current)
        lookback_high = today_bars["high"].iloc[-11:-1].max()
        current_high = today_bars["high"].iloc[-1]
        current_close = today_bars["close"].iloc[-1]
        
        # Calculate average volume of last 5 minutes
        avg_volume = today_bars["volume"].iloc[-6:-1].mean()
        current_volume = today_bars["volume"].iloc[-1]
        
        # Calculate RSI on close prices
        rsi_series = calc_rsi(today_bars["close"], period=14)
        rsi = rsi_series.iloc[-1] if not rsi_series.empty else 50
        
        # Breakout condition: price > lookback high, volume surge, RSI > 60
        if current_high > lookback_high and current_volume > avg_volume * MOM_VOLUME_MULTIPLIER and rsi > 60:
            entry_price = round(current_close, 4)
            
            # Stop loss based on stock volatility
            if symbol in HIGH_VOL_STOCKS:
                stop_pct = 0.012  # 1.2%
            else:
                stop_pct = 0.008   # 0.8%
            
            stop_price = round(entry_price * (1 - stop_pct), 4)
            risk = entry_price - stop_price
            target_price = round(entry_price + (MOM_REWARD_RISK * risk), 4)
            
            sb_log(f"🔥 MOM breakout detected for {symbol} at ${entry_price} (RSI: {rsi:.1f})")
            return True, entry_price, stop_price, target_price
        
        return False, 0, 0, 0
    except Exception as e:
        log.warning(f"MOM check error {symbol}: {e}")
        return False, 0, 0, 0

def run_mom_strategy(cash: float, held: dict) -> float:
    """Momentum breakout strategy entry logic."""
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
                "mom_traded_today": False,
            }
        
        s = symbol_state[symbol]
        if s.get("mom_traded_today") or s.get("in_trade"):
            continue
        
        # Check for momentum breakout
        is_breakout, entry_price, stop_price, target_price = check_mom_breakout(symbol)
        if not is_breakout:
            continue
        
        # Enter trade
        cost = enter_trade(symbol, entry_price, stop_price, target_price, cash - total_cost, "MOM")
        if cost > 0:
            total_cost += cost
            s["in_trade"] = True
            s["strategy"] = "MOM"
            s["entry"] = entry_price
            s["stop"] = stop_price
            s["target"] = target_price
            s["qty"] = MAX_TRADE_USD / entry_price
            s["mom_traded_today"] = True
            sb_log(f"📈 MOM trade executed for {symbol}")
    
    return total_cost
    
# ─────────────────────────────────────────────
# TOUCH AND TURN STRATEGY
# ─────────────────────────────────────────────
TURN_REWARD_RISK = 2.0
FIB_LEVEL = 0.382  # 38.2%

def calculate_atr(symbol: str, period: int = 14) -> float:
    """Calculate 14-day ATR from daily bars."""
    try:
        # Fetch daily bars
        end = datetime.now(pytz.utc)
        start = end - timedelta(days=30)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex",
        )
        bars = data_client.get_stock_bars(req).df
        if bars.empty or len(bars) < period:
            return 0.0
        
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")
        
        # Calculate True Range
        bars["prev_close"] = bars["close"].shift(1)
        bars["tr"] = bars[["high", "low", "prev_close"]].apply(
            lambda x: max(x["high"] - x["low"], 
                          abs(x["high"] - x["prev_close"]), 
                          abs(x["low"] - x["prev_close"])), axis=1
        )
        atr = bars["tr"].rolling(period).mean().iloc[-1]
        return round(float(atr), 4) if not pd.isna(atr) else 0.0
    except Exception as e:
        log.warning(f"ATR calculation error {symbol}: {e}")
        return 0.0

def get_opening_candle(symbol: str) -> tuple:
    """Get the first 15-minute candle of the trading day (9:30–9:45 ET)."""
    try:
        df = get_bars(symbol, timeframe_minutes=15, days_back=1)
        if df.empty or len(df) < 1:
            return None, None, None, None, None
        
        today_et = datetime.now(ET).date()
        today_bars = df[df.index.date == today_et]
        if today_bars.empty:
            return None, None, None, None, None
        
        # First candle of the day (should be 9:30–9:45)
        first_candle = today_bars.iloc[0]
        open_price = float(first_candle["open"])
        high = float(first_candle["high"])
        low = float(first_candle["low"])
        close = float(first_candle["close"])
        
        return open_price, high, low, close, first_candle
    except Exception as e:
        log.warning(f"Opening candle error {symbol}: {e}")
        return None, None, None, None, None

def check_touch_and_turn(symbol: str) -> tuple:
    """
    Check if the first 15-min candle is a manipulated red candle.
    Returns (is_setup, entry_price, stop_price, target_price, limit_order_active)
    """
    try:
        # Step 1: Get opening candle
        open_price, high, low, close, candle = get_opening_candle(symbol)
        if open_price is None:
            return False, 0, 0, 0, False
        
        # Step 2: Must be a red candle (close < open)
        if close >= open_price:
            return False, 0, 0, 0, False
        
        # Step 3: Calculate 14-day ATR and check volatility
        atr = calculate_atr(symbol, period=14)
        candle_range = high - low
        min_range = atr * 0.25  # 25% of ATR
        
        if candle_range < min_range or atr == 0:
            return False, 0, 0, 0, False
        
        # Step 4: Calculate Fibonacci levels
        fib_distance = (high - low) * FIB_LEVEL
        target_price = round(high - fib_distance, 4)  # 38.2% retracement from high
        entry_price = round(low, 4)  # Limit order at the low
        
        # Step 5: Stop loss is half of target distance
        target_distance = target_price - entry_price
        stop_distance = target_distance / 2
        stop_price = round(entry_price - stop_distance, 4) if stop_distance > 0 else entry_price - 0.01
        
        # Ensure stop is at least $0.01 below entry
        if entry_price - stop_price < 0.01:
            stop_price = entry_price - 0.01
        
        # Recalculate risk and target if stop was adjusted
        risk = entry_price - stop_price
        if risk <= 0:
            return False, 0, 0, 0, False
        
        target_price = round(entry_price + (TURN_REWARD_RISK * risk), 4)
        
        sb_log(f"🎯 Touch & Turn setup for {symbol}: Entry LIMIT ${entry_price}, Stop ${stop_price}, Target ${target_price}, ATR: {atr:.2f}")
        return True, entry_price, stop_price, target_price, True
    except Exception as e:
        log.warning(f"Touch & Turn error {symbol}: {e}")
        return False, 0, 0, 0, False

def run_touch_and_turn_strategy(cash: float, held: dict) -> float:
    """Touch and Turn strategy with limit order at the opening candle low."""
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
                "turn_traded_today": False,
                "limit_order_placed": False,
            }
        
        s = symbol_state[symbol]
        if s.get("turn_traded_today") or s.get("in_trade"):
            continue
        
        # Check if we have a setup
        is_setup, entry_price, stop_price, target_price, limit_order_active = check_touch_and_turn(symbol)
        if not is_setup:
            continue
        
        # Place limit order (not market order)
        # Note: This uses a limit order at the entry price (low of opening candle)
        # The order will only fill if price returns to that level
        if not s.get("limit_order_placed"):
            qty = MAX_TRADE_USD / entry_price
            if qty <= 0:
                continue
            
            actual_cost = round(qty * entry_price, 2)
            if cash - actual_cost < CASH_BUFFER:
                sb_log(f"SKIP {symbol} — would breach cash buffer")
                continue
            
            try:
                # Submit LIMIT order (not market)
                from alpaca.trading.requests import LimitOrderRequest
                trading_client.submit_order(LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    limit_price=entry_price,
                    time_in_force=TimeInForce.DAY,
                ))
                sb_log(f"🟢 TOUCH & TURN LIMIT ORDER placed for {qty:.4f} {symbol} @ ${entry_price:.2f} | Stop:${stop_price:.2f} | Target:${target_price:.2f}")
                
                # Mark that limit order is placed
                s["limit_order_placed"] = True
                s["pending_entry"] = entry_price
                s["pending_stop"] = stop_price
                s["pending_target"] = target_price
                s["pending_qty"] = qty
                s["pending_cost"] = actual_cost
                
                # We don't deduct cash yet – only when limit order fills
                # For now, just track that order is open
            except Exception as e:
                sb_log(f"Limit order error {symbol}: {e}")
                continue
        
        # Check if limit order has filled (we need to check positions)
        # This will be handled in the main loop via position monitoring
        # When the limit order fills, Alpaca will create a position
    
    return total_cost

def check_touch_and_turn_fills(cash: float, held: dict) -> float:
    """
    Check if any pending limit orders have filled.
    This should be called in the main loop after position check.
    """
    total_cost = 0.0
    for symbol, s in symbol_state.items():
        if s.get("limit_order_placed") and not s.get("in_trade"):
            # Check if position now exists
            try:
                positions = trading_client.get_all_positions()
                for p in positions:
                    if p.symbol == symbol:
                        # Limit order filled!
                        entry_price = float(p.avg_entry_price)
                        # The actual fill price may differ slightly from limit price
                        # Use the pending values for stop/target
                        stop_price = s.get("pending_stop", 0)
                        target_price = s.get("pending_target", 0)
                        qty = float(p.qty)
                        
                        s["in_trade"] = True
                        s["strategy"] = "TOUCH_TURN"
                        s["entry"] = entry_price
                        s["stop"] = stop_price
                        s["target"] = target_price
                        s["qty"] = qty
                        s["turn_traded_today"] = True
                        
                        actual_cost = qty * entry_price
                        total_cost += actual_cost
                        
                        sb_log(f"✅ TOUCH & TURN limit order filled for {qty:.4f} {symbol} @ ${entry_price:.2f}")
                        break
            except Exception as e:
                log.warning(f"Check fill error {symbol}: {e}")
    
    return total_cost

# ─────────────────────────────────────────────
# STRATEGY REGISTRY (ADD NEW STRATEGIES HERE)
# ─────────────────────────────────────────────
# Each strategy requires:
#   - name: The strategy identifier (must match strategy column in Supabase)
#   - time_window_start: (hour, minute) ET when the strategy should run in AUTO mode
#   - time_window_end: (hour, minute) ET when the strategy should stop in AUTO mode
#   - entry_func: The function that executes entry logic
#   - state_flag: The per‑symbol flag used to prevent multiple trades per day
#
# To add a new strategy:
#   1. Write your entry function (e.g., run_gap_strategy)
#   2. Add a dictionary entry below
#   3. (Optional) Add a row to Supabase 'strategies' table for dashboard display

STRATEGIES = {
    "TOUCH_TURN": {
        "name": "TOUCH_TURN",
        "time_window_start": (9, 30),   # 9:30 AM ET = 9:30 PM SGT
        "time_window_end": (10, 30),     # 10:30 AM ET = 10:30 PM SGT
        "entry_func": run_touch_and_turn_strategy,
        "state_flag": "turn_traded_today",
    },
    "MOM": {   # Put MOM first if you want it to run automatically in the morning
        "name": "MOM",
        "time_window_start": (9, 30),   # 9:30 AM ET = 9:30 PM SGT
        "time_window_end": (10, 30),    # 10:30 AM ET = 10:30 PM SGT
        "entry_func": run_mom_strategy,
        "state_flag": "mom_traded_today",
    },    
    "ORB-R": {
        "name": "ORB-R",
        "time_window_start": (9, 30),
        "time_window_end": (12, 0),
        "entry_func": run_orb_strategy,
        "state_flag": "orb_traded_today",
    },
    "VWAP": {
        "name": "VWAP",
        "time_window_start": (12, 0),
        "time_window_end": (15, 30),
        "entry_func": run_vwap_strategy,
        "state_flag": "vwap_traded_today",
    },
    
    # ============================================================
    # TO ADD A NEW STRATEGY, COPY THE BLOCK BELOW AND CUSTOMISE:
    # ============================================================
    # "NEW_STRATEGY": {
    #     "name": "NEW_STRATEGY",
    #     "time_window_start": (9, 30),
    #     "time_window_end": (11, 0),
    #     "entry_func": run_new_strategy,  # You must define this function
    #     "state_flag": "new_traded_today",
    # },
}

# ─────────────────────────────────────────────
# SESSION DETECTION (supports registry)
# ─────────────────────────────────────────────
def get_current_session() -> str:
    """Returns the strategy name ('ORB-R', 'VWAP', etc.) or 'CLOSED'."""
    forced = get_forced_strategy()
    now_et = datetime.now(ET)
    market_open = is_market_open()

    # Manual override takes precedence when market is open
    if market_open and forced in STRATEGIES:
        return forced
    if market_open and forced == "AUTO":
        # Find first strategy whose time window contains now
        for strat_name, cfg in STRATEGIES.items():
            start_h, start_m = cfg["time_window_start"]
            end_h, end_m = cfg["time_window_end"]
            start_time = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            end_time = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            if start_time <= now_et <= end_time:
                return strat_name
    return "CLOSED"

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
        sb_log("Weekly baseline reset")
    
    today = datetime.now(ET).date()
    if _last_reset_date != today:
        reset_daily_state()
        _last_reset_date = today
    
    if not is_market_open():
        log.info("Market closed")
        return
    
    try:
        account = trading_client.get_account()
        cash = float(account.buying_power)
        positions = trading_client.get_all_positions()
        held = {p.symbol: p for p in positions}
    except Exception as e:
        sb_log(f"Account error: {e}")
        return
    
    # EOD liquidation (uses stored strategy)
    if is_eod_window():
        for p in positions:
            try:
                strat = symbol_state.get(p.symbol, {}).get("strategy", "UNKNOWN")
                exit_trade(p.symbol, float(p.qty), float(p.current_price),
                           float(p.avg_entry_price), "EOW Liquidation", strat)
                if p.symbol in symbol_state:
                    symbol_state[p.symbol]["in_trade"] = False
            except Exception as e:
                sb_log(f"EOW error {p.symbol}: {e}")
        return
    
    # Monitor existing positions (stop/target)
    monitor_positions(held)
    
    # ─────────────────────────────────────────────
    # CHECK FOR TOUCH & TURN LIMIT ORDER FILLS
    # ─────────────────────────────────────────────
    filled_cost = check_touch_and_turn_fills(cash, held)
    if filled_cost > 0:
        cash -= filled_cost
        sb_log(f"Touch & Turn fills deducted: ${filled_cost:.2f}, remaining cash: ${cash:.2f}")
    
    if cash <= CASH_BUFFER:
        log.info(f"Cash ${cash:.2f} below buffer")
        return
    
    session = get_current_session()
    if session in STRATEGIES:
        total_cost = STRATEGIES[session]["entry_func"](cash, held)
        if total_cost:
            sb_log(f"{session} trades placed, total cost: ${total_cost:.2f}")
    else:
        log.info("Outside trading hours or no active strategy")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    sb_log("🤖 Scalable Trading Bot started (strategy registry)")
    sb_log(f"Watchlist: {len(WATCHLIST)} stocks | Max trade: ${MAX_TRADE_USD}")
    sb_log(f"Registered strategies: {', '.join(STRATEGIES.keys())}")
    
    baseline = load_baseline()
    sb_log(f"Weekly baseline: ${baseline:,.2f}")
    
    while True:
        try:
            run_strategy()
            send_heartbeat()
        except Exception as e:
            sb_log(f"Unhandled error: {e}")
        time.sleep(SCAN_INTERVAL)
