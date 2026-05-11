"""
bot.py — Trading Bot (Railway)
Upgraded: 3-layer architecture + market regime filter + WFV-optimized watchlist
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
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG (WFV-OPTIMIZED WATCHLIST)
# ─────────────────────────────────────────────
SGT = pytz.timezone("Asia/Singapore")

WATCHLIST = [
    "AMD", "NVDA", "QCOM", "MU", "ARM",
    "ASML", "PANW", "SMCI", "AVGO", "KLAC", "AMAT"
]

MAX_TRADE_USD = 300.0
CASH_BUFFER = 95_000.0

# ─────────────────────────────────────────────
# STOCK PROFILES
# ─────────────────────────────────────────────
STOCK_PROFILES = {
    "AMD":  (0.015, 0.009, 0.007),
    "NVDA": (0.018, 0.010, 0.008),
    "QCOM": (0.013, 0.008, 0.006),
    "MU":   (0.013, 0.008, 0.006),
    "ARM":  (0.013, 0.008, 0.006),
    "ASML": (0.013, 0.008, 0.006),
    "PANW": (0.013, 0.008, 0.006),
    "SMCI": (0.018, 0.010, 0.008),
    "AVGO": (0.013, 0.008, 0.006),
    "KLAC": (0.013, 0.008, 0.006),
    "AMAT": (0.013, 0.008, 0.006),
}

def profile(symbol):
    return STOCK_PROFILES.get(symbol, (0.013, 0.008, 0.006))

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
# INDICATORS
# ─────────────────────────────────────────────
def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1).mean()
    avg_l = loss.ewm(com=period-1).mean()
    rs = avg_g / avg_l.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))

def calc_macd(series):
    ema_fast = series.ewm(span=12).mean()
    ema_slow = series.ewm(span=26).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9).mean()
    return macd - signal

def rsi_macd_confirmed(df):
    if len(df) < 30:
        return True
    rsi = calc_rsi(df["close"]).iloc[-1]
    hist = calc_macd(df["close"]).iloc[-1]
    return (rsi < 70) and (hist > 0)

# ─────────────────────────────────────────────
# MARKET REGIME FILTER (QQQ)
# ─────────────────────────────────────────────
def get_market_regime():
    try:
        end = datetime.now(pytz.utc)
        start = end - timedelta(days=5)

        req = StockBarsRequest(
            symbol_or_symbols="QQQ",
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed="iex",
        )

        df = data_client.get_stock_bars(req).df
        if df.empty:
            return True

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs("QQQ", level="symbol")

        close = df["close"]
        return close.rolling(10).mean().iloc[-1] > close.rolling(30).mean().iloc[-1]

    except Exception:
        return True

# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────
def get_bars(symbol):
    try:
        end = datetime.now(pytz.utc)
        start = end - timedelta(days=2)

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed="iex",
        )

        df = data_client.get_stock_bars(req).df

        if df.empty:
            return pd.DataFrame()

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")

        return df[["close"]]

    except Exception:
        return pd.DataFrame()

# ─────────────────────────────────────────────
# MAIN STRATEGY
# ─────────────────────────────────────────────
def run_strategy():

    # ── MARKET REGIME (LAYER 1 GLOBAL FILTER)
    market_ok = get_market_regime()
    if not market_ok:
        log.info("📉 QQQ bearish regime — no trades")
        return

    # ── ACCOUNT
    account = trading_client.get_account()
    cash = float(account.buying_power)
    positions = trading_client.get_all_positions()
    held = {p.symbol: p for p in positions}

    # ─────────────────────────────────────────────
    # EXIT LOGIC (unchanged core, slightly guarded)
    # ─────────────────────────────────────────────
    for sym, p in held.items():
        df = get_bars(sym)
        if df.empty or len(df) < 20:
            continue

        price = float(df["close"].iloc[-1])
        entry = float(p.avg_entry_price)

        s_ma = df["close"].rolling(5).mean().iloc[-1]
        l_ma = df["close"].rolling(20).mean().iloc[-1]

        pnl = (price - entry) / entry
        hard_sl, trail, _ = profile(sym)

        if pnl <= -hard_sl:
            trading_client.submit_order(MarketOrderRequest(
                symbol=sym, qty=p.qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            continue

        if pnl >= 0.02:
            trading_client.submit_order(MarketOrderRequest(
                symbol=sym, qty=p.qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
            continue

        if s_ma < l_ma and not market_ok:
            trading_client.submit_order(MarketOrderRequest(
                symbol=sym, qty=p.qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))

    # ─────────────────────────────────────────────
    # BUY LOGIC (3-LAYER ARCHITECTURE)
    # ─────────────────────────────────────────────
    if cash <= CASH_BUFFER:
        return

    for symbol in WATCHLIST:

        if symbol in held:
            continue

        df = get_bars(symbol)
        if df.empty or len(df) < 20:
            continue

        price = float(df["close"].iloc[-1])
        s_ma = df["close"].rolling(5).mean().iloc[-1]
        l_ma = df["close"].rolling(20).mean().iloc[-1]

        # ── LAYER 1: REGIME
        if not (market_ok and s_ma > l_ma):
            continue

        # ── LAYER 2: SETUP
        if not rsi_macd_confirmed(df):
            continue

        # ── LAYER 3: EXECUTION
        qty = round(MAX_TRADE_USD / price, 6)
        cost = qty * price

        if cash - cost < CASH_BUFFER:
            continue

        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))

        cash -= cost
        log.info(f"BUY {symbol} @ {price:.2f}")

# ─────────────────────────────────────────────
# LOOP
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Bot started (3-layer system + QQQ filter)")
    while True:
        try:
            run_strategy()
        except Exception as e:
            log.error(e)
        time.sleep(10)
