"""
bot.py — Scalable trading bot with strategy registry
Run on Railway:
    Start command: python bot.py
    Environment: ALPACA_API_KEY, ALPACA_SECRET_KEY, SUPABASE_URL, SUPABASE_KEY, FINNHUB_API_KEY
"""

import os
import time
import pytz
import logging
import pandas as pd
import numpy as np
import random
import threading
import json
import websocket
from datetime import datetime, timedelta
from supabase import create_client, Client
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
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

# ── RISK MANAGEMENT ──────────────────────────────────────────────────────
TOTAL_EQUITY = 100000.0      # Total account balance
CASH_BUFFER = 95000.0         # Reserved cash (not for trading)
TRADING_CAPITAL = TOTAL_EQUITY - CASH_BUFFER  # = 5000.0

RISK_PER_TRADE_PCT = 0.01     # 1% of trading capital = $50 risk per trade
MAX_POSITION_PCT = 0.5        # Max 50% of trading capital per trade = $2,500

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
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

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
def calculate_qty(entry_price: float, stop_price: float) -> float:
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0.01:
        return 0.0
    dollar_risk = TRADING_CAPITAL * RISK_PER_TRADE_PCT
    qty_by_risk = dollar_risk / risk_per_share
    max_cash_per_trade = TRADING_CAPITAL * MAX_POSITION_PCT
    qty_by_cash = max_cash_per_trade / entry_price
    qty = min(qty_by_risk, qty_by_cash)
    return qty

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

def get_current_cash() -> float:
    try:
        account = trading_client.get_account()
        return float(account.buying_power)
    except Exception as e:
        sb_log(f"Error getting cash: {e}")
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
            for key in list(state.keys()):
                if key.endswith("_traded_today"):
                    state[key] = False
    for sym in to_delete:
        del symbol_state[sym]
    sb_log("Daily state reset")

def get_forced_strategy() -> str:
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
# FINNHUB WEBSOCKET FOR REAL-TIME DATA
# ─────────────────────────────────────────────
latest_prices: dict = {}
prices_lock = threading.Lock()
websocket_connected = False
ws_instance = None
reconnect_attempts = 0
max_reconnect_delay = 300
trade_counter = 0
last_log_time = 0

def on_websocket_message(ws, message):
    global latest_prices, trade_counter, last_log_time
    try:
        data = json.loads(message)
        if data.get("type") == "trade":
            trade_counter += len(data.get("data", []))
            now = time.time()
            if now - last_log_time > 10:
                log.info(f"📊 WebSocket received {trade_counter} trades in last 10 seconds")
                trade_counter = 0
                last_log_time = now
            for trade in data.get("data", []):
                symbol = trade.get("s")
                price = trade.get("p")
                if symbol and price:
                    with prices_lock:
                        latest_prices[symbol] = price
    except Exception as e:
        log.warning(f"WebSocket message error: {e}")

def on_websocket_error(ws, error):
    log.error(f"WebSocket error: {error}")

def on_websocket_close(ws, close_status_code, close_msg):
    global websocket_connected, reconnect_attempts
    websocket_connected = False
    log.warning(f"WebSocket connection closed. Code: {close_status_code}, Msg: {close_msg}")
    reconnect_attempts += 1
    delay = min(max_reconnect_delay, 2 ** reconnect_attempts) + random.uniform(0, 1)
    log.info(f"Reconnecting in {delay:.1f} seconds (attempt {reconnect_attempts})")
    threading.Timer(delay, connect_websocket).start()

def on_websocket_open(ws):
    global websocket_connected, reconnect_attempts
    websocket_connected = True
    reconnect_attempts = 0
    log.info(f"Finnhub WebSocket connected. Subscribing to {len(WATCHLIST)} symbols...")
    batch_size = 10
    for i in range(0, len(WATCHLIST), batch_size):
        batch = WATCHLIST[i:i+batch_size]
        for symbol in batch:
            ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
        time.sleep(0.2)
    log.info("Subscription requests sent for all symbols")

def connect_websocket():
    global ws_instance, websocket_connected, reconnect_attempts
    if not FINNHUB_API_KEY:
        log.warning("FINNHUB_API_KEY not set. WebSocket data will not be available.")
        return
    now_et = datetime.now(ET)
    market_opens = now_et.replace(hour=9, minute=30, second=0)
    market_closes = now_et.replace(hour=16, minute=0, second=0)
    thirty_min_before = market_opens - timedelta(minutes=30)
    if now_et < thirty_min_before or now_et > market_closes:
        delay = min(60, 2 ** reconnect_attempts)
        log.info(f"Market closed or not yet open. Next connection attempt in {delay}s")
        threading.Timer(delay, connect_websocket).start()
        return
    reconnect_attempts = 0
    ws_url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_websocket_open,
        on_message=on_websocket_message,
        on_error=on_websocket_error,
        on_close=on_websocket_close,
    )
    ws_instance = ws
    wst = threading.Thread(target=ws.run_forever, daemon=True)
    wst.start()
    log.info("Finnhub WebSocket thread started")

def get_realtime_price(symbol: str) -> float:
    with prices_lock:
        return latest_prices.get(symbol, None)

def get_current_price(symbol: str) -> float:
    if is_finnhub_connected():
        rt_price = get_realtime_price(symbol)
        if rt_price is not None and rt_price > 0:
            return rt_price
    try:
        df = get_bars(symbol, timeframe_minutes=1, days_back=1)
        if not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return None

def is_finnhub_connected() -> bool:
    return websocket_connected and len(latest_prices) > 0

def check_finnhub_health():
    if not FINNHUB_API_KEY:
        return
    if not websocket_connected:
        log.warning("Finnhub WebSocket is disconnected. Reconnection will be attempted by the close handler.")
    else:
        with prices_lock:
            recent_data = len(latest_prices) > 0
        if not recent_data:
            log.warning("Finnhub connected but no price data received – may be stale")

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

def run_orb_strategy() -> list:
    setups = []
    for symbol in WATCHLIST:
        if symbol in symbol_state and symbol_state[symbol].get("in_trade"):
            continue
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None,
                "box_high": None,
                "box_low": None,
                "breakout_confirmed": False,
                "in_trade": False,
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
        qty = calculate_qty(entry_price, stop_price)
        if qty <= 0:
            continue
        setups.append((symbol, entry_price, stop_price, target_price, qty, "ORB-R", False))
        s["orb_traded_today"] = True
    return setups

# ─────────────────────────────────────────────
# VWAP STRATEGY (WITH DEBUG LOGGING)
# ─────────────────────────────────────────────
VWAP_REWARD_RISK = 1.5

def check_vwap_retest(symbol: str, current_vwap: float) -> tuple:
    """Returns (is_retest, entry_price, stop_price, target_price, candle_low)"""
    try:
        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=VWAP_LOOKBACK_DAYS)
        if df_5m.empty or len(df_5m) < 10:
            sb_log(f"🔍 VWAP DEBUG {symbol}: insufficient 5-min bars (len={len(df_5m)})")
            return False, 0, 0, 0, 0

        today_et = datetime.now(ET).date()
        today_bars = df_5m[df_5m.index.date == today_et]
        if today_bars.empty or len(today_bars) < 3:
            sb_log(f"🔍 VWAP DEBUG {symbol}: insufficient today bars (len={len(today_bars)})")
            return False, 0, 0, 0, 0

        latest = today_bars.iloc[-1]
        prev = today_bars.iloc[-2] if len(today_bars) > 1 else latest
        candle_low = float(latest["low"])
        candle_high = float(latest["high"])
        vwap_near = (candle_low <= current_vwap * 1.001 and candle_high >= current_vwap * 0.999)

        sb_log(f"🔍 VWAP DEBUG {symbol}: VWAP={current_vwap:.4f}, low={candle_low:.4f}, high={candle_high:.4f}, vwap_near={vwap_near}")

        if not vwap_near:
            return False, 0, 0, 0, 0

        hammer = is_hammer(latest)
        inv_hammer = is_inverted_hammer(latest)
        engulfing = is_bullish_engulfing(prev, latest) if len(today_bars) > 1 else False
        sb_log(f"🔍 VWAP DEBUG {symbol}: hammer={hammer}, inv_hammer={inv_hammer}, engulfing={engulfing}")

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
            sb_log(f"🔍 VWAP DEBUG {symbol}: adjusted stop to {stop_price:.4f} (min {min_stop_pct*100:.1f}%)")
        else:
            risk = entry_price - stop_price
            if risk <= 0:
                return False, 0, 0, 0, 0
            target_price = round(entry_price + (VWAP_REWARD_RISK * risk), 4)

        sb_log(f"🔍 VWAP DEBUG {symbol}: RETEST TRUE! entry={entry_price:.4f}, stop={stop_price:.4f}, target={target_price:.4f}")
        return True, entry_price, stop_price, target_price, float(latest["low"])
    except Exception as e:
        sb_log(f"🔍 VWAP DEBUG {symbol}: exception in check_vwap_retest: {e}")
        return False, 0, 0, 0, 0

def run_vwap_strategy() -> list:
    setups = []
    for symbol in WATCHLIST:
        if symbol in symbol_state and symbol_state[symbol].get("in_trade"):
            continue
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None,
                "in_trade": False,
                "vwap_traded_today": False,
            }
        s = symbol_state[symbol]
        if s.get("vwap_traded_today") or s.get("in_trade"):
            continue

        df_5m = get_bars(symbol, timeframe_minutes=5, days_back=1)
        if df_5m.empty or len(df_5m) < 10:
            sb_log(f"🔍 VWAP DEBUG {symbol}: get_bars returned empty or too short (len={len(df_5m)})")
            continue

        # Calculate VWAP
        typical_price = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3
        cumulative_tp_vol = (typical_price * df_5m["volume"]).cumsum()
        cumulative_vol = df_5m["volume"].cumsum()
        vwap = round(float((cumulative_tp_vol / cumulative_vol).iloc[-1]), 4) if cumulative_vol.iloc[-1] != 0 else None
        if vwap is None:
            sb_log(f"🔍 VWAP DEBUG {symbol}: VWAP calculation failed (cumulative_vol zero)")
            continue

        current_price = float(df_5m["close"].iloc[-1])
        sb_log(f"🔍 VWAP DEBUG {symbol}: VWAP={vwap:.4f}, current_price={current_price:.4f}, above? {current_price > vwap}")

        if current_price < vwap:
            sb_log(f"🔍 VWAP DEBUG {symbol}: price below VWAP, skipping")
            continue

        is_retest, entry_price, stop_price, target_price, _ = check_vwap_retest(symbol, vwap)
        if not is_retest:
            continue

        qty = calculate_qty(entry_price, stop_price)
        if qty <= 0:
            sb_log(f"🔍 VWAP DEBUG {symbol}: calculated qty {qty} <= 0, skipping")
            continue

        sb_log(f"📊 {symbol} VWAP retest detected at ${vwap:.2f}")
        setups.append((symbol, entry_price, stop_price, target_price, qty, "VWAP", False))
        s["vwap_traded_today"] = True
    return setups

# ─────────────────────────────────────────────
# MOM STRATEGY
# ─────────────────────────────────────────────
MOM_REWARD_RISK = 2.0

def run_mom_strategy() -> list:
    setups = []
    for symbol in WATCHLIST:
        if symbol in symbol_state and symbol_state[symbol].get("in_trade"):
            continue
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None,
                "in_trade": False,
                "mom_traded_today": False,
            }
        s = symbol_state[symbol]
        if s.get("mom_traded_today") or s.get("in_trade"):
            continue
        try:
            df = get_bars(symbol, timeframe_minutes=1, days_back=1)
            if df.empty or len(df) < 15:
                continue
            today_et = datetime.now(ET).date()
            today_bars = df[df.index.date == today_et]
            if today_bars.empty or len(today_bars) < 11:
                continue
            lookback_high = today_bars["high"].iloc[-11:-1].max()
            current_price = get_current_price(symbol)
            if current_price is None:
                continue
            avg_volume = today_bars["volume"].iloc[-6:-1].mean()
            current_volume = today_bars["volume"].iloc[-1]
            rsi_series = calc_rsi(today_bars["close"], period=14)
            rsi = rsi_series.iloc[-1] if not rsi_series.empty else 50
            if current_price > lookback_high and current_volume > avg_volume * 1.5 and rsi > 60:
                entry_price = round(current_price, 4)
                if symbol in HIGH_VOL_STOCKS:
                    stop_pct = 0.012
                else:
                    stop_pct = 0.008
                stop_price = round(entry_price * (1 - stop_pct), 4)
                risk = entry_price - stop_price
                if risk <= 0:
                    continue
                target_price = round(entry_price + (MOM_REWARD_RISK * risk), 4)
                qty = calculate_qty(entry_price, stop_price)
                if qty <= 0:
                    continue
                sb_log(f"🔥 MOM breakout detected for {symbol} at ${entry_price} (RSI: {rsi:.1f})")
                setups.append((symbol, entry_price, stop_price, target_price, qty, "MOM", False))
                s["mom_traded_today"] = True
        except Exception as e:
            log.warning(f"MOM check error {symbol}: {e}")
    return setups

# ─────────────────────────────────────────────
# TOUCH AND TURN STRATEGY
# ─────────────────────────────────────────────
TURN_REWARD_RISK = 2.0

def run_touch_and_turn_strategy() -> list:
    setups = []
    for symbol in WATCHLIST:
        if symbol in symbol_state and symbol_state[symbol].get("in_trade"):
            continue
        if symbol not in symbol_state:
            symbol_state[symbol] = {
                "strategy": None,
                "in_trade": False,
                "turn_traded_today": False,
            }
        s = symbol_state[symbol]
        if s.get("turn_traded_today") or s.get("in_trade"):
            continue
        try:
            df = get_bars(symbol, timeframe_minutes=15, days_back=1)
            if df.empty or len(df) < 1:
                continue
            today_et = datetime.now(ET).date()
            today_bars = df[df.index.date == today_et]
            if today_bars.empty:
                continue
            first_candle = today_bars.iloc[0]
            open_price = float(first_candle["open"])
            high = float(first_candle["high"])
            low = float(first_candle["low"])
            close = float(first_candle["close"])
            if close >= open_price:
                continue
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
            if bars.empty:
                continue
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol, level="symbol")
            bars["prev_close"] = bars["close"].shift(1)
            bars["tr"] = bars[["high", "low", "prev_close"]].apply(
                lambda x: max(x["high"] - x["low"],
                              abs(x["high"] - x["prev_close"]),
                              abs(x["low"] - x["prev_close"])), axis=1
            )
            atr = bars["tr"].rolling(14).mean().iloc[-1]
            atr = round(float(atr), 4) if not pd.isna(atr) else 0.0
            candle_range = high - low
            min_range = atr * 0.25
            if candle_range < min_range or atr == 0:
                continue
            entry_price = round(low, 4)
            if symbol in HIGH_VOL_STOCKS:
                stop_pct = 0.012
            else:
                stop_pct = 0.008
            stop_price = round(entry_price * (1 - stop_pct), 4)
            risk = entry_price - stop_price
            if risk <= 0:
                continue
            target_price = round(entry_price + (TURN_REWARD_RISK * risk), 4)
            now_et = datetime.now(ET)
            window_end_et = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
            if now_et > window_end_et:
                s["turn_traded_today"] = True
                continue
            current_price = get_current_price(symbol)
            if current_price is None:
                continue
            price_diff_pct = abs((current_price - entry_price) / entry_price) * 100
            if price_diff_pct > 2.0:
                s["turn_traded_today"] = True
                continue
            qty = calculate_qty(entry_price, stop_price)
            if qty <= 0:
                continue
            sb_log(f"🎯 Touch & Turn setup for {symbol}: Entry LIMIT ${entry_price}, Stop ${stop_price}, Target ${target_price}, ATR: {atr:.2f}")
            setups.append((symbol, entry_price, stop_price, target_price, qty, "TOUCH_TURN", True))
            s["turn_traded_today"] = True
        except Exception as e:
            log.warning(f"Touch & Turn error {symbol}: {e}")
    return setups

# ─────────────────────────────────────────────
# STRATEGY REGISTRY
# ─────────────────────────────────────────────
STRATEGIES = {
    "TOUCH_TURN": {
        "name": "TOUCH_TURN",
        "time_window_start": (9, 30),
        "time_window_end": (10, 30),
        "entry_func": run_touch_and_turn_strategy,
        "state_flag": "turn_traded_today",
    },
    "MOM": {
        "name": "MOM",
        "time_window_start": (9, 30),
        "time_window_end": (10, 30),
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
}

def get_current_session() -> str:
    forced = get_forced_strategy()
    now_et = datetime.now(ET)
    market_open = is_market_open()
    if market_open and forced in STRATEGIES:
        return forced
    if market_open and forced == "AUTO":
        for strat_name, cfg in STRATEGIES.items():
            start_h, start_m = cfg["time_window_start"]
            end_h, end_m = cfg["time_window_end"]
            start_time = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            end_time = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            if start_time <= now_et <= end_time:
                return strat_name
    return "CLOSED"

# ─────────────────────────────────────────────
# ORDER PLACEMENT AND FILL DETECTION
# ─────────────────────────────────────────────
def place_order(symbol: str, qty: float, entry_price: float, stop_price: float,
                target_price: float, strategy: str, is_limit: bool = False) -> bool:
    if qty <= 0:
        return False
    try:
        if is_limit:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                limit_price=entry_price,
                time_in_force=TimeInForce.DAY,
            )
            trading_client.submit_order(order)
            order_type = "LIMIT"
            sb_log(f"🟢 {strategy} LIMIT ORDER placed for {qty:.4f} {symbol} @ ${entry_price:.2f} | Stop:${stop_price:.2f} | Target:${target_price:.2f}")
        else:
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            trading_client.submit_order(order)
            order_type = "MARKET"
            sb_log(f"🟢 {strategy} MARKET ORDER placed for {qty:.4f} {symbol} | Entry: ~${entry_price:.2f} | Stop:${stop_price:.2f} | Target:${target_price:.2f}")

        if symbol not in symbol_state:
            symbol_state[symbol] = {}
        symbol_state[symbol]["pending_trade"] = {
            "strategy": strategy,
            "stop": stop_price,
            "target": target_price,
            "qty": qty,
            "is_limit": is_limit,
            "limit_price": entry_price if is_limit else None,
            "placed_at": datetime.now(ET).isoformat()
        }

        if not is_limit:
            s = symbol_state[symbol]
            s["in_trade"] = True
            s["strategy"] = strategy
            s["entry"] = entry_price
            s["stop"] = stop_price
            s["target"] = target_price
            s["qty"] = qty
            flag_map = {
                "MOM": "mom_traded_today",
                "ORB-R": "orb_traded_today",
                "VWAP": "vwap_traded_today",
                "TOUCH_TURN": "turn_traded_today"
            }
            if strategy in flag_map:
                s[flag_map[strategy]] = True
            symbol_state[symbol] = s

        return True
    except Exception as e:
        sb_log(f"Order error {symbol}: {e}")
        return False

def check_pending_fills() -> float:
    total_cost = 0.0
    try:
        positions = trading_client.get_all_positions()
        position_dict = {p.symbol: p for p in positions}
        for symbol, s in list(symbol_state.items()):
            if s.get("pending_trade") and not s.get("in_trade"):
                pending = s["pending_trade"]
                if not pending.get("is_limit", False):
                    continue
                if symbol in position_dict:
                    p = position_dict[symbol]
                    entry_price = float(p.avg_entry_price)
                    qty = float(p.qty)
                    actual_cost = qty * entry_price
                    if s.get("pending_filled"):
                        continue
                    s["in_trade"] = True
                    s["strategy"] = pending["strategy"]
                    s["entry"] = entry_price
                    s["stop"] = pending["stop"]
                    s["target"] = pending["target"]
                    s["qty"] = qty
                    s["pending_filled"] = True
                    flag_map = {
                        "MOM": "mom_traded_today",
                        "ORB-R": "orb_traded_today",
                        "VWAP": "vwap_traded_today",
                        "TOUCH_TURN": "turn_traded_today"
                    }
                    if pending["strategy"] in flag_map:
                        s[flag_map[pending["strategy"]]] = True
                    total_cost += actual_cost
                    sb_log(f"✅ {pending['strategy']} limit order filled for {qty:.4f} {symbol} @ ${entry_price:.2f} (cost: ${actual_cost:.2f})")
                    del s["pending_trade"]
    except Exception as e:
        log.warning(f"Check fills error: {e}")
    return total_cost

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

    cash = get_current_cash()
    try:
        positions = trading_client.get_all_positions()
        held = {p.symbol: p for p in positions}
    except Exception as e:
        sb_log(f"Account error: {e}")
        return

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

    monitor_positions(held)
    filled_cost = check_pending_fills()
    if filled_cost > 0:
        cash = get_current_cash()
        sb_log(f"Limit order fills detected: ${filled_cost:.2f} deducted, current cash: ${cash:.2f}")

    if cash <= CASH_BUFFER:
        log.info(f"Cash ${cash:.2f} below buffer (${CASH_BUFFER:.2f}) — no new trades")
        return

    session = get_current_session()
    setups = []
    if session in STRATEGIES:
        setups = STRATEGIES[session]["entry_func"]()
    else:
        log.info("Outside trading hours or no active strategy")
        return

    total_pending = 0.0
    for symbol, entry, stop, target, qty, strategy, is_limit in setups:
        current_cash = get_current_cash()
        estimated_cost = qty * entry * 1.01
        if current_cash - estimated_cost < CASH_BUFFER:
            sb_log(f"SKIP {symbol} — would breach cash buffer (cash ${current_cash:.2f} - ${estimated_cost:.2f} < ${CASH_BUFFER})")
            continue
        if place_order(symbol, qty, entry, stop, target, strategy, is_limit):
            total_pending += estimated_cost
    if total_pending > 0:
        sb_log(f"{session} orders placed, estimated total: ${total_pending:.2f}")

if __name__ == "__main__":
    sb_log("🤖 Scalable Trading Bot started (strategy registry)")
    sb_log(f"Watchlist: {len(WATCHLIST)} stocks")
    sb_log(f"Trading capital: ${TRADING_CAPITAL:.2f} (${RISK_PER_TRADE_PCT*100:.1f}% risk = ${TRADING_CAPITAL*RISK_PER_TRADE_PCT:.2f} per trade)")
    sb_log(f"Max position size: ${TRADING_CAPITAL*MAX_POSITION_PCT:.2f} ({MAX_POSITION_PCT*100:.0f}% of capital)")
    sb_log(f"Registered strategies: {', '.join(STRATEGIES.keys())}")
    baseline = load_baseline()
    sb_log(f"Weekly baseline: ${baseline:,.2f}")
    if FINNHUB_API_KEY:
        connect_websocket()
    else:
        log.warning("FINNHUB_API_KEY not set – using Alpaca IEX data (delayed).")
    while True:
        try:
            run_strategy()
            send_heartbeat()
            check_finnhub_health()
        except Exception as e:
            sb_log(f"Unhandled error: {e}")
        time.sleep(SCAN_INTERVAL)