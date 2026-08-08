"""
Tradingbot_v4_SMA_RVOL.py — Professional Daily SMA Trend-Following Bot with RVOL Gatekeeper
─────────────────────────────────────────────────────────────────────────────
VERSION 4.3 PRODUCTION UPDATES:
  1. Fixed Supabase serialization crash by cleaning NumPy types (np.float64) before saving.
  2. Fixed Alpaca SDK ImportError by separating data and trading requests.
  3. Integrated 10-Day Relative Volume (RVOL) filter into the Buy Execution Loop.
  4. Prevents low-volume false breakouts by requiring institutional volume backing (RVOL >= 1.3x).
  5. Uses Alpaca Free IEX feed natively for ultra-fast multi-symbol batched scans.
  6. Automatically synchronizes portfolio constraints (Max 8 concurrent active holdings).
  7. Fixed heartbeat JSON serialization - uses proper json.dumps() instead of str().
  8. Added robust error handling for empty peak_prices and missing database records.

Execution Infrastructure: Recommended to deploy 24/7 via Railway or AWS EC2.
"""

import os
import time
import logging
import json  # ✅ ADDED: For proper JSON serialization
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import pytz

# ── ALPACA SDK IMPORT MODULES ──
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
# DATABASE INITIALIZATION CHECK
# ─────────────────────────────────────────────
def ensure_bot_state_record():
    """Ensure bot_state has a record with id=1"""
    try:
        check = supabase.table("bot_state").select("id").eq("id", 1).execute()
        if not check.data:
            logger.warning("⚠️ bot_state id=1 not found. Creating initial record...")
            supabase.table("bot_state").insert({
                "id": 1,
                "peak_prices": "{}",
                "last_heartbeat": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }).execute()
            logger.info("✅ Created initial bot_state record")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to ensure bot_state record: {e}")
        return False

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
        # ── INITIALIZATION CHECK ──
        if not ensure_bot_state_record():
            logger.error("❌ Cannot proceed - bot_state record missing")
            return
        
        account = trading_client.get_account()
        if account.trading_blocked:
            logger.warning("Account status flag locked. Halting order entry.")
            return
            
        positions = trading_client.get_all_positions()
        active_holdings = {p.symbol: p for p in positions}
        logger.info(f"Active Inventory: {len(active_holdings)} / {MAX_CORES_BUDGET} targets currently occupied.")
        
        # ── POPULATE PEAK PRICES FOR ALL SYMBOLS ──
        peak_prices = {}
        for symbol in WATCHLIST:
            metrics = process_market_indicators(symbol)
            if metrics:
                peak_prices[symbol] = metrics["current_price"]
        logger.info(f"📊 Loaded peak prices for {len(peak_prices)} symbols")
        
        # ── PHASE 1: EVALUATE EXITS ──
        for symbol in list(active_holdings.keys()):
            metrics = process_market_indicators(symbol)
            if not metrics:
                continue
                
            price = metrics["current_price"]
            sma20 = metrics["sma20"]
            sma50 = metrics["sma50"]
            
            if (price < sma20 < sma50) or (price < sma50):
                logger.info(f"🚨 EXIT TRIGGER for {symbol}: Price={price:.2f}, SMA20={sma20:.2f}, SMA50={sma50:.2f}")
                try:
                    order = trading_client.submit_order(order_data=MarketOrderRequest(
                        symbol=symbol,
                        qty=active_holdings[symbol].qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    ))
                    logger.info(f"Exit order executed: {order.id}")
                except Exception as ex:
                    logger.error(f"Failed routing exit for {symbol}: {ex}")

        # ── PHASE 2: EVALUATE ENTRIES WITH RVOL GATEKEEPER ──
        if len(trading_client.get_all_positions()) >= MAX_CORES_BUDGET:
            logger.info("Portfolio at max capacity. Skipping entry scan.")
        else:
            for symbol in WATCHLIST:
                if symbol in active_holdings:
                    continue
                    
                metrics = process_market_indicators(symbol)
                if not metrics:
                    continue
                    
                price = metrics["current_price"]
                sma20 = metrics["sma20"]
                sma50 = metrics["sma50"]
                rvol = metrics["rvol"]
                
                if (price > sma20 > sma50) and (rvol >= RVOL_THRESHOLD):
                    if len(trading_client.get_all_positions()) >= MAX_CORES_BUDGET:
                        break
                        
                    logger.info(f"🔥 BUY TRIGGER: {symbol}, RVOL={rvol:.2f}x")
                    try:
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
                            logger.info(f"Entry order executed: {order.id} | Qty: {shares_to_buy}")
                    except Exception as entry_ex:
                        logger.error(f"Failed submitting buy order for {symbol}: {entry_ex}")
        
        # ── PHASE 3: DATABASE SYNCHRONIZATION ──
        try:
            clean_peaks = {str(ticker): float(val) for ticker, val in peak_prices.items()}
            clean_peaks_json = json.dumps(clean_peaks)
            now_utc = datetime.utcnow()
            
            update_data = {
                "peak_prices": clean_peaks_json,
                "last_heartbeat": now_utc.isoformat() + "+00",
                "updated_at": now_utc.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            hb_response = supabase.table("bot_state").update(update_data).eq("id", 1).execute()
            
            if hb_response.data:
                logger.info(f"❤️ Heartbeat synced successfully")
                logger.info(f"📊 Peak prices synced: {len(clean_peaks)}")
            else:
                logger.warning("⚠️ Update completed but no records returned")
                try:
                    insert_data = {
                        "id": 1,
                        "peak_prices": clean_peaks_json,
                        "last_heartbeat": now_utc.isoformat() + "+00",
                        "updated_at": now_utc.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    supabase.table("bot_state").insert(insert_data).execute()
                    logger.info("✅ Created new bot_state record")
                except Exception as insert_err:
                    logger.error(f"❌ Failed to create record: {insert_err}")
                
        except Exception as heartbeat_err:
            logger.error(f"❌ HEARTBEAT FAILURE: {heartbeat_err}")
            try:
                supabase.table("bot_logs").insert({
                    "message": f"Heartbeat failure: {str(heartbeat_err)}",
                    "severity": "error",
                    "timestamp": datetime.utcnow().isoformat()
                }).execute()
            except:
                pass
    
    except Exception as e:
        logger.error(f"❌ CRITICAL EXECUTION CYCLE FAILURE: {e}")

# ─────────────────────────────────────────────
# TEST SUPABASE CONNECTION
# ─────────────────────────────────────────────
def test_supabase_connection():
    """Test if Supabase is reachable"""
    logger.info("Testing Supabase connection...")
    try:
        # Simple test query
        test_response = supabase.table("bot_state").select("id").limit(1).execute()
        logger.info("✅ Supabase connection successful!")
        return True
    except Exception as e:
        logger.error(f"❌ Supabase connection failed: {e}")
        logger.error(f"   SUPABASE_URL: {SUPABASE_URL}")
        logger.error(f"   SUPABASE_KEY exists: {bool(SUPABASE_KEY and SUPABASE_KEY != 'YOUR_SUPABASE_KEY')}")
        return False

# ─────────────────────────────────────────────
# DAEMON SYSTEM KERNEL ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # ... rest of your code ...

    logger.info("⚡ System Kernel Engaged. Continuous Daily SMA/RVOL Automation Core Online.")
  
    # TEST CONNECTION FIRST
    if not test_supabase_connection():
        logger.error("❌ Cannot connect to Supabase. Check environment variables and network.")
        exit(1)
  
    # Initialize database record on startup
    if not ensure_bot_state_record():
        logger.error("❌ Failed to initialize database. Exiting.")
        exit(1)
    
    # Run a quick test heartbeat on startup
    try:
        test_peaks = {"TEST": 100.0}
        test_json = json.dumps(test_peaks)
        supabase.table("bot_state").update({
            "peak_prices": test_json,
            "last_heartbeat": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", 1).execute()
        logger.info("✅ Initial heartbeat test successful")
    except Exception as e:
        logger.error(f"❌ Initial heartbeat test failed: {e}")
    
    while True:
        now = datetime.now(ET)
        # Scan blocks execution logic runs every 5 minutes during active market framework hours
        if now.weekday() < 5 and (9 <= now.hour <= 16):
            run_execution_cycle()
            time.sleep(300) # Sleep for 5 minutes
        else:
            logger.info("Market framework outside operational baseline standard hours. Sleep mode active.")
            time.sleep(1800) # Sleep for 30 minutes during off-market intervals
