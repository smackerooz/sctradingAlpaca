"""
Tradingbot_v4_SMA_RVOL.py — Professional Daily SMA Trend-Following Bot with RVOL Gatekeeper
─────────────────────────────────────────────────────────────────────────────
VERSION 4.1 PRODUCTION UPDATES:
  1. Integrated 10-Day Relative Volume (RVOL) filter into the Buy Execution Loop.
  2. Prevents low-volume false breakouts by requiring institutional volume backing (RVOL >= 1.3x).
  3. Uses Alpaca Free IEX feed natively for ultra-fast multi-symbol batched scans.
  4. Automatically synchronizes portfolio constraints (Max 8 concurrent active holdings).

Execution Infrastructure: Recommended to deploy 24/7 via Railway or AWS EC2.
"""

import os
import time
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.timeframe import TimeFrame
from supabase import create_client, Client

# ─────────────────────────────────────────────
# LOGGING SYSTEM CONFIGURATION
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIGURATION & TIMEZONE ANCHORS
# ─────────────────────────────────────────────
ET = pytz.timezone("US/Eastern")
SGT = pytz.timezone("Asia/Singapore")

MAX_CORES_BUDGET = 8
RVOL_THRESHOLD = 1.3  # Institutional volume backing filter (130% of 10-day average)

WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM",
    "MSTR", "PANW", "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA",
    "CRWD", "ZS", "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP",
    "SWKS", "FTNT", "ANET", "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT"
]

# ─────────────────────────────────────────────
# CREDENTIAL VALUATION LAYER
# ─────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "YOUR_ALPACA_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "YOUR_ALPACA_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_KEY")

try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Core execution clients securely bound to remote infrastructure.")
except Exception as e:
    logger.critical(f"Client initialisation vector failure: {e}")
    raise

# ─────────────────────────────────────────────
# TECHNICAL ANALYSIS CALCULATORS
# ─────────────────────────────────────────────
def calculate_sma(prices: list, period: int) -> float:
    if len(prices) < period:
        return 0.0
    return float(np.mean(prices[-period:]))

def process_market_indicators(symbol: str):
    """
    Queries historical daily bars from Alpaca free tier IEX endpoint,
    calculates SMA(20), SMA(50), and the 10-Day Relative Volume (RVOL).
    """
    try:
        end_date = datetime.now(ET)
        start_date = end_date - timedelta(days=90) # Buffer to guarantee 50 trading days
        
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
            feed="iex"
        )
        
        bars = trading_client.get_stock_bars(request_params)
        if not bars or symbol not in bars.data or len(bars.data[symbol]) < 55:
            return None
            
        df_bars = pd.DataFrame([{"close": bar.close, "volume": bar.volume} for bar in bars.data[symbol]])
        
        closes = df_bars["close"].tolist()
        volumes = df_bars["volume"].tolist()
        
        current_price = closes[-1]
        sma20 = calculate_sma(closes, 20)
        sma50 = calculate_sma(closes, 50)
        
        # ── RVOL COMPONENT ENGINE ──
        current_volume = volumes[-1]
        historical_volumes = volumes[-11:-1] # Past 10 complete trading days excluding today
        avg_10day_vol = sum(historical_volumes) / 10 if historical_volumes else 1.0
        rvol = current_volume / avg_10day_vol if avg_10day_vol > 0 else 1.0
        
        return {
            "current_price": current_price,
            "sma20": sma20,
            "sma50": sma50,
            "rvol": rvol,
            "current_volume": current_volume,
            "avg_volume": avg_10day_vol
        }
    except Exception as e:
        logger.error(f"Failed processing technical array for {symbol}: {e}")
        return None

# ─────────────────────────────────────────────
# CORE EXECUTION LOOP MATRIX
# ─────────────────────────────────────────────
def run_execution_cycle():
    logger.info("Initializing automated scan iteration across 50-stock index portfolio...")
    
    try:
        account = trading_client.get_account()
        if account.trading_blocked:
            logger.warning("Account status flag locked. Halting order entry.")
            return
            
        positions = trading_client.get_all_positions()
        active_holdings = {p.symbol: p for p in positions}
        logger.info(f"Active Inventory: {len(active_holdings)} / {MAX_CORES_BUDGET} targets currently occupied.")
        
        # ── PHASE 1: EVALUATE EXITS & TECHNICAL DEGRADATION ──
        for symbol in list(active_holdings.keys()):
            metrics = process_market_indicators(symbol)
            if not metrics:
                continue
                
            price = metrics["current_price"]
            sma20 = metrics["sma20"]
            sma50 = metrics["sma50"]
            
            # Rule 1: Perfect Bearish Alignment Check (Price < SMA20 < SMA50)
            # Rule 2: Multi-Day Moving Average Violation Check (Price < SMA50)
            if (price < sma20 < sma50) or (price < sma50):
                logger.info(f"🚨 EXIT TRIGGER BLOCK MET FOR {symbol}: Price={price:.2f}, SMA20={sma20:.2f}, SMA50={sma50:.2f}")
                try:
                    order = trading_client.submit_order(order_data=MarketOrderRequest(
                        symbol=symbol,
                        qty=active_holdings[symbol].qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    ))
                    logger.info(f"Exit transaction clean routed to Alpaca Desk: {order.id}")
                    # Log to Supabase realized_trades here if required
                except Exception as ex:
                    logger.error(f"Failed routing liquidation execution vector for {symbol}: {ex}")

        # ── PHASE 2: EVALUATE ENTRYS WITH INSTITUTIONAL RVOL GATEKEEPER ──
        if len(trading_client.get_all_positions()) >= MAX_CORES_BUDGET:
            logger.info("Portfolio capacity fully loaded at max configuration footprint. Skipping entry scan.")
            return
            
        for symbol in WATCHLIST:
            if symbol in active_holdings:
                continue # Skip stocks we already own
                
            metrics = process_market_indicators(symbol)
            if not metrics:
                continue
                
            price = metrics["current_price"]
            sma20 = metrics["sma20"]
            sma50 = metrics["sma50"]
            rvol = metrics["rvol"]
            
            # ── STRUCTURAL BUY GATEWAY UNLOCKED BY VOLUME ──
            if (price > sma20 > sma50) and (rvol >= RVOL_THRESHOLD):
                # Verify capacity boundary conditions one final check right before sending order
                if len(trading_client.get_all_positions()) >= MAX_CORES_BUDGET:
                    break
                    
                logger.info(f"🔥 INSTITUTIONAL ACCUMULATION TRIGGER DETECTED: Ticker={symbol}, RVOL={rvol:.2f}x (Threshold={RVOL_THRESHOLD}x)")
                try:
                    # Risk parameter: 1/8th allocation envelope per core position
                    cash_available = float(trading_client.get_account().cash)
                    target_allocation = min(cash_available / (MAX_CORES_BUDGET - len(trading_client.get_all_positions())), cash_available * 0.12)
                    shares_to_buy = int(target_allocation // price)
                    
                    if shares_to_buy > 0:
                        order = trading_client.submit_order(order_data=MarketOrderRequest(
                            symbol=symbol,
                            qty=shares_to_buy,
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.GTC
                        ))
                        logger.info(f"Entry order executed successfully via Alpaca API: {order.id} | Qty: {shares_to_buy}")
                except Exception as entry_ex:
                    logger.error(f"Failed submitting purchase ticket for target {symbol}: {entry_ex}")
                    
        # Update heartbeats inside Supabase engine
        try:
            supabase.table("bot_state").update({"last_heartbeat": datetime.utcnow().isoformat()}).eq("id", 1).execute()
        except:
            pass
            
    except Exception as cycle_ex:
        logger.error(f"Global execution iteration sequence error: {cycle_ex}")

# ─────────────────────────────────────────────
# DAEMON SYSTEM KERNEL ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("⚡ System Kernel Engaged. Continuous Daily SMA/RVOL Automation Core Online.")
    while True:
        now = datetime.now(ET)
        # Scan blocks execution logic runs every 5 minutes during active market framework hours
        if now.weekday() < 5 and (9 <= now.hour <= 16):
            run_execution_cycle()
            time.sleep(300) # Sleep for 5 minutes
        else:
            logger.info("Market framework outside operational baseline standard hours. Sleep mode active.")
            time.sleep(1800) # Sleep for 30 minutes during off-market intervals
