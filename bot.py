"""
Tradingbot_v3_Final.py — High-Performance ORB-R + VWAP Engine (50 Stocks)
─────────────────────────────────────────────────────────────────────────
OPTIMIZATIONS & UPDATES:
  1. Watchlist expanded to 50 institutional high-conviction names.
  2. High-volatility profile array expanded for specialized stop logic.
  3. Batch data pipelines dynamically map matrix data for the larger payload.
  4. Patched syntax anomaly in candlestick processing algorithms.

Run on Railway:
    Start command: python Tradingbot_v3_Final.py
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
# LOGGING & CONFIG
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ET            = pytz.timezone("US/Eastern")
SGT           = pytz.timezone("Asia/Singapore")
SCAN_INTERVAL = 10  

# ── Capital management ───────────────────────
EFFECTIVE_CAPITAL  = 12000.0   
RISK_PER_TRADE_PCT = 0.005     # $60 risk/trade
MAX_TRADE_USD      = 1000.0    
MAX_CONCURRENT     = 8         
DAILY_LOSS_LIMIT   = -150.0    

# ── ORB-R config ─────────────────────────────
ORB_MINUTES         = 30       
ORB_REWARD_RISK     = 3.0
ORB_RETEST_TOL_PCT  = 0.002    
MIN_BOX_PCT         = 0.005    
MAX_GAP_PCT         = 0.03     
ORB_STALE_HOURS     = 3.0      

# ── VWAP config ──────────────────────────────
VWAP_TF_MINUTES    = 5
VWAP_LOOKBACK_DAYS = 1
VWAP_STOP_PCT      = 0.003     
VWAP_REWARD_RISK   = 2.0       
VWAP_STALE_HOURS   = 2.0       

# ── Trailing stop ─────────────────────────────
TRAIL_ACTIVATE_R   = 1.0       
TRAIL_DISTANCE_PCT = 0.004     

# ── Volume filter ─────────────────────────────
VOLUME_MULTIPLIER  = 1.5       

# ── Trade windows (ET) ────────────────────────
ORB_WINDOW_START  = (9, 30)
ORB_WINDOW_END    = (12, 0)
VWAP_WINDOW_START = (12, 0)
VWAP_WINDOW_END   = (15, 30)

# Expanded to capture high Beta/ATR names from your new list
HIGH_VOL_STOCKS = [
    "NVDA", "AMD", "TSLA", "AVGO", "QCOM", "AMAT", "ASML", "CRWD", "PANW", "SHOP", "SNOW",
    "SMCI", "MSTR", "ARM", "LRCX", "MRVL", "MPWR", "ZS", "TEAM", "DASH", "UBER"
]

# ── EXPANDED WATCHLIST (50 STOCKS) ──────────────────────────────────────────
WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM", 
    "MSTR", "PANW", "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA", 
    "CRWD", "ZS", "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP", 
    "SWKS", "FTNT", "ANET", "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK", 
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

# ─────────────────────────────────────────────
# INITIALIZATION
# ─────────────────────────────────────────────
API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
SB_URL     = os.environ["SUPABASE_URL"]
SB_KEY     = os.environ["SUPABASE_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)
supabase: Client = create_client(SB_URL, SB_KEY)

symbol_state: dict = {}
baseline: float    = None
_forced_strategy_cache = "AUTO"
_last_forced_check     = None
_daily_pnl_cache       = None
_daily_pnl_date        = None

def sb_log(msg: str):
    try:
        supabase.table("bot_logs").insert({"message": msg, "created_at": datetime.now(SGT).isoformat()}).execute()
    except Exception:
        pass
    log.info(msg)

# ─────────────────────────────────────────────
# PERFORMANCE BATCH FETCHING & STATE
# ─────────────────────────────────────────────
def get_batch_bars(symbols: list, timeframe_minutes: int, days_back: int = 2) -> pd.DataFrame:
    try:
        end   = datetime.now(pytz.utc)
        start = end - timedelta(days=days_back)
        req   = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed="iex",
        )
        df = data_client.get_stock_bars(req).df
        if df.empty:
            return pd.DataFrame()
        
        df.index = df.index.set_levels(pd.to_datetime(df.index.levels[1], utc=True).tz_convert(ET), level="timestamp")
        return df
    except Exception as e:
        log.warning(f"Batch data fetch error: {e}")
        return pd.DataFrame()

def get_symbol_df_from_batch(batch_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    try:
        if batch_df.empty or symbol not in batch_df.index.levels[0]:
            return pd.DataFrame()
        df = batch_df.xs(symbol, level="symbol").copy()
        return df[["open", "high", "low", "close", "volume"]]
    except Exception:
        return pd.DataFrame()

# ─────────────────────────────────────────────
# POLICIES & CONTROLS
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

def is_market_open() -> bool:
    try:
        clock = trading_client.get_clock()
        return clock.is_open
    except Exception:
        now_et  = datetime.now(ET)
        weekday = now_et.weekday()
        hour, minute = now_et.hour, now_et.minute
        after_open  = (hour == 9 and minute >= 30) or (hour >= 10)
        before_close = hour < 16
        return weekday < 5 and after_open and before_close

def get_current_session() -> str:
    forced  = get_forced_strategy()
    now_et  = datetime.now(ET)
    if is_market_open() and forced in ["ORB-R", "VWAP"]:
        return "ORB" if forced == "ORB-R" else "VWAP"

    orb_start  = now_et.replace(hour=ORB_WINDOW_START[0],  minute=ORB_WINDOW_START[1],  second=0, microsecond=0)
    orb_end    = now_et.replace(hour=ORB_WINDOW_END[0],    minute=ORB_WINDOW_END[1],    second=0, microsecond=0)
    vwap_start = now_et.replace(hour=VWAP_WINDOW_START[0], minute=VWAP_WINDOW_START[1], second=0, microsecond=0)
    vwap_end   = now_et.replace(hour=VWAP_WINDOW_END[0],   minute=VWAP_WINDOW_END[1],   second=0, microsecond=0)

    if orb_start <= now_et < orb_end: return "ORB"
    elif vwap_start <= now_et < vwap_end: return "VWAP"
    else: return "CLOSED"

def is_eod_window() -> bool:
    now_et = datetime.now(ET)
    return now_et.weekday() == 4 and now_et.hour == 15 and 45 <= now_et.minute < 55

def get_daily_pnl() -> float:
    global _daily_pnl_cache, _daily_pnl_date
    today = datetime.now(SGT).date()
    try:
        result = supabase.table("realized_trades").select("pl_usd").eq("date", today.isoformat()).execute()
        total = sum(float(t["pl_usd"]) for t in result.data) if result.data else 0.0
        _daily_pnl_cache, _daily_pnl_date = total, today
        return total
    except Exception as e:
        log.warning(f"get_daily_pnl error: {e}")
        return _daily_pnl_cache or 0.0

def is_circuit_breaker_active() -> bool:
    if get_daily_pnl() <= DAILY_LOSS_LIMIT:
        sb_log(f"🛑 CIRCUIT BREAKER ACTIVE. Realised loss crossed threshold.")
        return True
    return False

def active_trade_count() -> int:
    return sum(1 for s in symbol_state.values() if s.get("in_trade"))

def can_open_new_position() -> bool:
    return active_trade_count() < MAX_CONCURRENT

# ─────────────────────────────────────────────
# INDICATORS & MATRICES
# ─────────────────────────────────────────────
def calculate_vwap(df: pd.DataFrame) -> float:
    if df.empty or len(df) < 5: return None
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    return round(float((typical_price * df["volume"]).cumsum().iloc[-1] / df["volume"].cumsum().iloc[-1]), 4)

def has_volume_confirmation(df: pd.DataFrame, lookback: int = 20) -> bool:
    if df.empty or len(df) < lookback + 1: return False
    avg_vol = df["volume"].iloc[-lookback-1:-1].mean()
    return (df["volume"].iloc[-1] / avg_vol) >= VOLUME_MULTIPLIER if avg_vol > 0 else False

def calc_position_size(symbol: str, entry: float, stop: float) -> float:
    stop_distance = entry - stop
    if stop_distance <= 0: return 0.0
    return round(min((EFFECTIVE_CAPITAL * RISK_PER_TRADE_PCT) / stop_distance, MAX_TRADE_USD / entry), 4)

# ── Candlestick Patterns ─────────────────────
def is_hammer(c: pd.Series) -> bool:
    body, total = abs(c["close"] - c["open"]), c["high"] - c["low"]
    lw = (c["open"] if c["close"] >= c["open"] else c["close"]) - c["low"]
    return total > 0 and body > 0 and (lw >= 2 * body) and (body / total <= 0.35)

def is_inverted_hammer(c: pd.Series) -> bool:
    body, total = abs(c["close"] - c["open"]), c["high"] - c["low"]
    uw = c["high"] - max(c["close"], c["open"])
    return total > 0 and body > 0 and (uw >= 2 * body) and (body / total <= 0.35)

def is_bullish_engulfing(p: pd.Series, c: pd.Series) -> bool:
    return p["close"] < p["open"] and c["close"] > c["open"] and c["open"] <= p["close"] and c["close"] >= p["open"]

def check_reversal_candle(df: pd.DataFrame, level: float, tolerance_pct: float) -> bool:
    if df.empty or len(df) < 2: return False
    latest, prev = df.iloc[-1], df.iloc[-2]
    if not (latest["low"] <= level * (1 + tolerance_pct) and latest["high"] >= level * (1 - tolerance_pct)):
        return False
    return is_hammer(latest) or is_inverted_hammer(latest) or is_bullish_engulfing(prev, latest)

# ─────────────────────────────────────────────
# CORE CALCULATIONS & RE-TESTING
# ─────────────────────────────────────────────
def compute_and_cache_orb_box(symbol: str, df: pd.DataFrame) -> tuple:
    if df.empty: return None, None, None
    today_et = datetime.now(ET).date()
    today_bars = df[df.index.date == today_et]
    
    orb_end_time = (datetime.combine(today_et, datetime.min.time()) + timedelta(minutes=90 + ORB_MINUTES)).time() 
    orb_bars = today_bars[(today_bars.index.time >= pd.Timestamp("09:30").time()) & (today_bars.index.time < orb_end_time)]
    
    if len(orb_bars) < 3: return None, None, None
    
    box_high = round(float(orb_bars["high"].max()), 4)
    box_low  = round(float(orb_bars["low"].min()), 4)
    
    yesterday_bars = df[df.index.date < today_et]
    prev_close = float(yesterday_bars["close"].iloc[-1]) if not yesterday_bars.empty else None
    
    return box_high, box_low, prev_close

def check_orb_breakout(df_today: pd.DataFrame, box_high: float) -> bool:
    if df_today.empty: return False
    post_orb = df_today[df_today.index.time >= pd.Timestamp("10:00").time()]
    return not post_orb[post_orb["close"] > box_high].empty

def check_orb_retest(df_today: pd.DataFrame, box_high: float) -> bool:
    if df_today.empty: return False
    return df_today["low"].iloc[-1] <= box_high * (1 + ORB_RETEST_TOL_PCT) and df_today["high"].iloc[-1] >= box_high * (1 - ORB_RETEST_TOL_PCT)

def check_vwap_retest(symbol: str, current_vwap: float, df_today: pd.DataFrame) -> tuple:
    if df_today.empty or len(df_today) < 3: return False, 0, 0, 0
    latest, prev = df_today.iloc[-1], df_today.iloc[-2]
    
    if not (float(latest["low"]) <= current_vwap * 1.001 and float(latest["high"]) >= current_vwap * 0.999):
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
    if risk <= 0: return False, 0, 0, 0
    
    return True, entry_price, stop_price, round(entry_price + (VWAP_REWARD_RISK * risk), 4)

# ─────────────────────────────────────────────
# EXECUTION ENGINE
# ─────────────────────────────────────────────
def save_open_position(symbol: str, strategy: str, entry_price: float, qty: float, stop: float, target: float):
    try:
        supabase.table("open_positions").upsert({
            "symbol": symbol, "strategy": strategy, "entry_price": entry_price,
            "stop_price": stop, "target_price": target, "qty": qty, "updated_at": datetime.now(SGT).isoformat()
        }, on_conflict="symbol").execute()
    except Exception as e: log.error(f"Error saving open DB state: {e}")

def remove_open_position(symbol: str):
    try: supabase.table("open_positions").delete().eq("symbol", symbol).execute()
    except Exception: pass

def save_trade(symbol, entry_price, exit_price, qty, reason, strategy):
    try:
        pl_usd = round((exit_price - entry_price) * float(qty), 2)
        pl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        supabase.table("realized_trades").insert({
            "date": datetime.now(SGT).date().isoformat(), "symbol": symbol, "strategy": strategy,
            "buy_price": f"${entry_price:.2f}", "sell_price": f"${exit_price:.2f}", "qty": round(float(qty), 4),
            "pl_usd": pl_usd, "pl_display": f"{'🟢' if pl_usd >= 0 else '🔴'} ${pl_usd:+.2f}",
            "pl_pct": f"{pl_pct:+.2f}%", "time_sgt": datetime.now(SGT).strftime("%H:%M:%S"), "reason": reason
        }).execute()
        remove_open_position(symbol)
    except Exception as e: sb_log(f"Save trade dynamically failed: {e}")

def enter_trade(symbol: str, entry_price: float, stop_price: float, target_price: float, strategy: str) -> float:
    qty = calc_position_size(symbol, entry_price, stop_price)
    if qty <= 0: return 0.0

    try:
        order = trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    except Exception as e:
        sb_log(f"Order entry rejected {symbol}: {e}")
        return 0.0

    actual_entry, filled = entry_price, False
    for _ in range(2):
        time.sleep(1)
        try:
            filled_order = trading_client.get_order_by_id(order.id)
            if filled_order.status.value == "filled":
                actual_entry = round(float(filled_order.filled_avg_price), 4)
                filled = True
                break
        except Exception: pass

    risk = actual_entry - stop_price
    stop_price   = round(actual_entry - risk, 4)
    target_price = round(actual_entry + (ORB_REWARD_RISK if strategy == "ORB-R" else VWAP_REWARD_RISK) * risk, 4)
    
    sb_log(f"🟢 {strategy} EXECUTION | {symbol} | Qty: {qty} | Fill: ${actual_entry:.2f}")
    save_open_position(symbol, strategy, actual_entry, qty, stop_price, target_price)
    return round(qty * actual_entry, 2)

def exit_trade(symbol: str, qty: float, current_price: float, entry_price: float, reason: str, strategy: str):
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
        sb_log(f"🔴 EXIT {symbol} @ ${current_price:.2f} — {reason}")
        save_trade(symbol, entry_price, current_price, qty, reason, strategy)
    except Exception as e: sb_log(f"Exit pipeline failure {symbol}: {e}")

# ─────────────────────────────────────────────
# MONITOR LOOP
# ─────────────────────────────────────────────
def monitor_positions(held: dict):
    for sym, p in held.items():
        s = symbol_state.get(sym, {})
        if not s.get("in_trade"): continue

        entry, stop, target, qty, strategy, entry_ts = s["entry"], s["stop"], s["target"], s["qty"], s["strategy"], s["entry_ts"]
        curr_p = float(p.current_price)
        risk = entry - stop

        if risk > 0 and curr_p >= entry + (TRAIL_ACTIVATE_R * risk):
            trail_stop = round(curr_p * (1 - TRAIL_DISTANCE_PCT), 4)
            if trail_stop > s.get("stop", 0.0):
                s["stop"] = stop = trail_stop
                sb_log(f"📈 TRAIL TS UPDATED {sym}: ${trail_stop:.2f}")

        if curr_p <= stop:
            exit_trade(sym, qty, curr_p, entry, f"STOP LOSS (${stop:.2f})", strategy)
            s["in_trade"] = False
        elif curr_p >= target:
            exit_trade(sym, qty, curr_p, entry, f"TAKE PROFIT (${target:.2f})", strategy)
            s["in_trade"] = False
        elif entry_ts and ((datetime.now(ET) - entry_ts).total_seconds() / 3600) >= (ORB_STALE_HOURS if strategy == "ORB-R" else VWAP_STALE_HOURS):
            exit_trade(sym, qty, curr_p, entry, "STALE TIMEOUT EXIT", strategy)
            s["in_trade"] = False
        elif get_current_session() == "CLOSED":
            exit_trade(sym, qty, curr_p, entry, "MARKET CLOSURE FORCED LIQUIDATION", strategy)
            s["in_trade"] = False

# ─────────────────────────────────────────────
# STATE STRATEGY RUNNERS
# ─────────────────────────────────────────────
def run_orb_strategy(held: dict, batch_df: pd.DataFrame) -> float:
    total_cost = 0.0
    now_et = datetime.now(ET)
    
    if now_et.hour < 10 or (now_et.hour == 9 and now_et.minute < 30 + ORB_MINUTES):
        return total_cost

    for symbol in WATCHLIST:
        if symbol in held or not can_open_new_position() or is_circuit_breaker_active(): break

        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None, "box_high": None, "box_low": None, "prev_close": None,
                "breakout_confirmed": False, "in_trade": False, "entry": 0.0, "stop": 0.0,
                "target": 0.0, "qty": 0.0, "traded_today": False, "entry_ts": None, "box_calculated": False
            }

        s = symbol_state[symbol]
        if s["traded_today"] or s.get("in_trade"): continue

        df_all = get_symbol_df_from_batch(batch_df, symbol)
        if df_all.empty: continue

        if not s["box_calculated"]:
            box_high, box_low, prev_close = compute_and_cache_orb_box(symbol, df_all)
            if box_high is None: continue
            
            box_range = box_high - box_low
            mid_price = (box_high + box_low) / 2
            if mid_price > 0 and box_range / mid_price < MIN_BOX_PCT:
                s["traded_today"] = True
                continue
            if prev_close and ((box_high - prev_close) / prev_close) > MAX_GAP_PCT:
                s["traded_today"] = True
                continue
                
            s.update({"box_high": box_high, "box_low": box_low, "prev_close": prev_close, "box_calculated": True})
            sb_log(f"📦 MEMORY CACHED: {symbol} ORB Box -> High: {box_high} Low: {box_low}")

        box_high = s["box_high"]
        today_df = df_all[df_all.index.date == now_et.date()]

        if not s["breakout_confirmed"]:
            if check_orb_breakout(today_df, box_high): s["breakout_confirmed"] = True
            else: continue

        if not check_orb_retest(today_df, box_high) or not has_volume_confirmation(today_df): continue
        if not check_reversal_candle(today_df, box_high, ORB_RETEST_TOL_PCT): continue

        confirm_candle = today_df.iloc[-1]
        entry_price = round(float(confirm_candle["close"]), 4)
        stop_price = round(min(round(float(confirm_candle["low"]) * 0.999, 4), entry_price - (entry_price * (0.01 if symbol in HIGH_VOL_STOCKS else 0.005))), 4)
        risk = entry_price - stop_price
        
        if risk <= 0 or risk / entry_price > 0.05: continue

        cost = enter_trade(symbol, entry_price, stop_price, round(entry_price + (ORB_REWARD_RISK * risk), 4), "ORB-R")
        if cost > 0:
            total_cost += cost
            s.update({"in_trade": True, "strategy": "ORB-R", "entry": entry_price, "stop": stop_price, "target": round(entry_price + (ORB_REWARD_RISK * risk), 4), "qty": calc_position_size(symbol, entry_price, stop_price), "traded_today": True, "entry_ts": now_et})

    return total_cost

def run_vwap_strategy(held: dict, batch_df: pd.DataFrame) -> float:
    total_cost = 0.0
    now_et = datetime.now(ET)

    for symbol in WATCHLIST:
        if symbol in held or not can_open_new_position() or is_circuit_breaker_active(): break

        if symbol not in symbol_state:
            symbol_state[symbol] = {"strategy": None, "in_trade": False, "entry": 0.0, "stop": 0.0, "target": 0.0, "qty": 0.0, "vwap_traded_today": False, "entry_ts": None}

        s = symbol_state[symbol]
        if s.get("vwap_traded_today") or s.get("in_trade"): continue

        df_all = get_symbol_df_from_batch(batch_df, symbol)
        if df_all.empty: continue
        
        today_df = df_all[df_all.index.date == now_et.date()]
        if today_df.empty or len(today_df) < 3: continue

        vwap = calculate_vwap(today_df)
        if vwap is None or float(today_df["close"].iloc[-1]) < vwap or not has_volume_confirmation(today_df): continue

        is_retest, entry_price, stop_price, target_price = check_vwap_retest(symbol, vwap, today_df)
        if not is_retest: continue

        cost = enter_trade(symbol, entry_price, stop_price, target_price, "VWAP")
        if cost > 0:
            total_cost += cost
            s.update({"in_trade": True, "strategy": "VWAP", "entry": entry_price, "stop": stop_price, "target": target_price, "qty": calc_position_size(symbol, entry_price, stop_price), "vwap_traded_today": True, "entry_ts": now_et})

    return total_cost

# ─────────────────────────────────────────────
# CONTROL ENVIRONMENT
# ─────────────────────────────────────────────
def reset_daily_state():
    global symbol_state
    for sym in list(symbol_state.keys()):
        if not symbol_state[sym].get("in_trade"): del symbol_state[sym]
        else: symbol_state[sym].update({"traded_today": False, "vwap_traded_today": False, "box_calculated": False})
    sb_log(f"📅 Watchlist processing state reset completed. Bot primed.")

def load_baseline() -> float:
    try:
        row = supabase.table("weekly_baseline").select("*").eq("id", 1).execute()
        if row.data and datetime.fromisoformat(row.data[0]["date"]).date() >= (datetime.now(SGT).date() - timedelta(days=datetime.now(SGT).date().weekday())):
            return float(row.data[0]["baseline"])
    except Exception: pass
    try: return float(trading_client.get_account().last_equity)
    except Exception: return 12000.0

def save_baseline(value: float):
    try: supabase.table("weekly_baseline").upsert({"id": 1, "baseline": value, "date": datetime.now(SGT).date().isoformat()}).execute()
    except Exception as e: log.error(f"Baseline syncing error: {e}")

_last_reset_date = None

def run_strategy():
    global baseline, _last_reset_date
    now_et = datetime.now(ET)
    
    if now_et.weekday() == 0 and now_et.hour == 9 and now_et.minute == 30:
        baseline = float(trading_client.get_account().equity)
        save_baseline(baseline)

    if _last_reset_date != now_et.date():
        reset_daily_state()
        _last_reset_date = now_et.date()

    if not is_market_open(): return

    try:
        positions = trading_client.get_all_positions()
        held = {p.symbol: p for p in positions}
    except Exception as e:
        log.error(f"Account properties loading error: {e}")
        return

    if is_eod_window():
        for p in positions:
            exit_trade(p.symbol, float(p.qty), float(p.current_price), float(p.avg_entry_price), "EOW Liquidation", symbol_state.get(p.symbol, {}).get("strategy", "UNKNOWN"))
            if p.symbol in symbol_state: symbol_state[p.symbol]["in_trade"] = False
        return

    monitor_positions(held)
    if is_circuit_breaker_active(): return

    session = get_current_session()
    if session in ["ORB", "VWAP"]:
        batch_df = get_batch_bars(WATCHLIST, timeframe_minutes=5, days_back=2)
        if session == "ORB":
            run_orb_strategy(held, batch_df)
        else:
            run_vwap_strategy(held, batch_df)

if __name__ == "__main__":
    baseline = load_baseline()
    sb_log(f"🤖 Bot v3 running with expanded watchlist: {len(WATCHLIST)} active tickers mapped.")
    while True:
        try:
            run_strategy()
            try: supabase.table("bot_state").upsert({"id": 1, "last_heartbeat": datetime.now(SGT).isoformat(), "updated_at": datetime.now(SGT).isoformat()}).execute()
            except Exception: pass
        except Exception as e: log.error(f"Global thread iteration crashed: {e}")
        time.sleep(SCAN_INTERVAL)
