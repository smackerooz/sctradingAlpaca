"""
Tradingbot_v4_SMA.py — Trend-Following SMA Crossover Production Engine (50 Stocks)
─────────────────────────────────────────────────────────────────────────
STRATEGY MIGRATE OVERVIEW:
  1. [REPLACED] Intraday 5-min ORB-R and VWAP breakout strategies completely removed.
  2. [STRATEGY] Implements Daily Moving Average Convergence: Bullish if Price > SMA20 > SMA50.
  3. [DATA ENGINE] Uses ultra-fast Alpaca batch data pipeline to pull Daily bars (prevents yfinance rate limits).
  4. [RISK] Maintains hard capital envelope caps, MAX_CONCURRENT position targets, and daily loss circuit breakers.

Run on Railway:
    Start command: python Tradingbot_v4_SMA.py
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
SCAN_INTERVAL = 30  # Scanning sequence optimized for daily matrix lookups

# ── Capital management ───────────────────────
EFFECTIVE_CAPITAL  = 12000.0   
RISK_PER_TRADE_PCT = 0.005     # $60 risk/trade
MAX_TRADE_USD      = 1000.0    
MAX_CONCURRENT     = 8         
DAILY_LOSS_LIMIT   = -150.0    

# ── SMA Strategy parameters ──────────────────
SMA_FAST           = 20
SMA_SLOW           = 50
DAYS_LOOKBACK      = 90        # Fetches ~3 months of historical data for accurate SMA window calculations

# ── Trailing stop ─────────────────────────────
TRAIL_ACTIVATE_R   = 1.0       
TRAIL_DISTANCE_PCT = 0.004     

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
# HIGH-PERFORMANCE BATCH FETCHING (DAILY DATA)
# ─────────────────────────────────────────────
def get_batch_daily_bars(symbols: list, days_back: int = DAYS_LOOKBACK) -> pd.DataFrame:
    """Fetches Day-interval bars using Alpaca multi-symbol calls to circumvent yfinance rate limits."""
    try:
        end   = datetime.now(pytz.utc)
        start = end - timedelta(days=days_back)
        req   = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex",
        )
        df = data_client.get_stock_bars(req).df
        return df
    except Exception as e:
        log.warning(f"Batch historical daily data fetch error: {e}")
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

def calc_position_size(symbol: str, entry: float, stop: float) -> float:
    stop_distance = entry - stop
    if stop_distance <= 0: return 0.0
    return round(min((EFFECTIVE_CAPITAL * RISK_PER_TRADE_PCT) / stop_distance, MAX_TRADE_USD / entry), 4)

# ─────────────────────────────────────────────
# EXECUTION PIPELINE
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
    target_price = round(actual_entry + (3.0 * risk), 4) # Emulates original asset properties structure (3R target window)
    
    sb_log(f"🟢 {strategy} BUY EXECUTION | {symbol} | Qty: {qty} | Fill: ${actual_entry:.2f} | Stop: ${stop_price:.2f} | Target: ${target_price:.2f}")
    save_open_position(symbol, strategy, actual_entry, qty, stop_price, target_price)
    return round(qty * actual_entry, 2)

def exit_trade(symbol: str, qty: float, current_price: float, entry_price: float, reason: str, strategy: str):
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
        sb_log(f"🔴 EXIT {symbol} @ ${current_price:.2f} — {reason}")
        save_trade(symbol, entry_price, current_price, qty, reason, strategy)
    except Exception as e: sb_log(f"Exit pipeline failure {symbol}: {e}")

# ─────────────────────────────────────────────
# DYNAMIC POSITION MANAGEMENT
# ─────────────────────────────────────────────
def monitor_positions(held: dict):
    for sym, p in held.items():
        s = symbol_state.get(sym, {})
        if not s.get("in_trade"): continue

        entry, stop, target, qty, strategy, entry_ts = s["entry"], s["stop"], s["target"], s["qty"], s["strategy"], s["entry_ts"]
        curr_p = float(p.current_price)
        risk = entry - stop

        # Trail stop tracking
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

# ─────────────────────────────────────────────
# CORE SMA CROSSOVER STRATEGY ENGINE
# ─────────────────────────────────────────────
def run_sma_strategy(held: dict, batch_df: pd.DataFrame):
    total_cost = 0.0
    now_et = datetime.now(ET)

    for symbol in WATCHLIST:
        if not can_open_new_position() or is_circuit_breaker_active(): break

        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None, "in_trade": False, "entry": 0.0, "stop": 0.0,
                "target": 0.0, "qty": 0.0, "traded_today": False, "entry_ts": None
            }

        s = symbol_state[symbol]

        # Extract local history out of structural memory matrix
        df_all = get_symbol_df_from_batch(batch_df, symbol)
        if df_all.empty or len(df_all) < SMA_SLOW: continue

        # Calculate Moving Averages matching analysis criteria
        df_all['SMA20'] = df_all['close'].rolling(window=SMA_FAST).mean()
        df_all['SMA50'] = df_all['close'].rolling(window=SMA_SLOW).mean()

        current_price = float(df_all['close'].iloc[-1])
        sma_20 = float(df_all['SMA20'].iloc[-1])
        sma_50 = float(df_all['SMA50'].iloc[-1])

        # Core logic convergence
        bullish = current_price > sma_20 > sma_50
        bearish = current_price < sma_20 < sma_50

        # EXECUTE BUY TRIGGER
        if bullish and symbol not in held and not s["traded_today"] and not s["in_trade"]:
            entry_price = round(current_price, 4)
            # Volatility-adjusted buffer safety stop loss (2.0% distance per parameters)
            stop_price  = round(entry_price * 0.98, 4) 
            risk        = entry_price - stop_price
            target_price = round(entry_price * 1.05, 4) # Balanced upside framework target
            
            cost = enter_trade(symbol, entry_price, stop_price, target_price, "SMA-CROSS")
            if cost > 0:
                total_cost += cost
                s.update({
                    "in_trade": True, "strategy": "SMA-CROSS", "entry": entry_price, 
                    "stop": stop_price, "target": target_price, "qty": calc_position_size(symbol, entry_price, stop_price), 
                    "traded_today": True, "entry_ts": now_et
                })

        # EXECUTE SELL TRIGGER (Exit criteria matching strategy shift)
        elif bearish and symbol in held and s.get("in_trade"):
            qty = s["qty"]
            exit_trade(symbol, qty, current_price, s["entry"], "SMA BEARISH REVERSAL SIGNALLED", "SMA-CROSS")
            s["in_trade"] = False

    return total_cost

# ─────────────────────────────────────────────
# SYSTEM DRIVER
# ─────────────────────────────────────────────
def reset_daily_state():
    global symbol_state
    for sym in list(symbol_state.keys()):
        if not symbol_state[sym].get("in_trade"): del symbol_state[sym]
        else: symbol_state[sym].update({"traded_today": False})
    sb_log(f"📅 Core Daily SMA Cache Purged. Bot state optimized.")

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
        log.error(f"Account inventory data failure: {e}")
        return

    # Check open exposure layers
    monitor_positions(held)
    if is_circuit_breaker_active(): return

    forced = get_forced_strategy()
    if forced != "CLOSED":
        # Production asset lookup loop execution
        batch_df = get_batch_daily_bars(WATCHLIST, days_back=DAYS_LOOKBACK)
        run_sma_strategy(held, batch_df)

if __name__ == "__main__":
    baseline = load_baseline()
    sb_log(f"🤖 Bot v4 Online — Core Engine Running Active SMA Strategy over {len(WATCHLIST)} mapped symbols.")
    while True:
        try:
            run_strategy()
            try: supabase.table("bot_state").upsert({"id": 1, "last_heartbeat": datetime.now(SGT).isoformat(), "updated_at": datetime.now(SGT).isoformat()}).execute()
            except Exception: pass
        except Exception as e: log.error(f"Execution thread crashed: {e}")
        time.sleep(SCAN_INTERVAL)
