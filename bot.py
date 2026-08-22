"""
bot.py — Professional Daily SMA Trend-Following Bot with RVOL Gatekeeper
─────────────────────────────────────────────────────────────────────────────
VERSION 4.6 PRODUCTION UPDATES (fixes applied):
  1. Fixed Supabase serialization crash by cleaning NumPy types (np.float64) before saving.
  2. FIXED: market data now goes through a dedicated StockHistoricalDataClient instead of
     calling get_stock_bars() on the TradingClient, which doesn't have that method. This
     was why no trades were ever executed — every call to process_market_indicators() was
     silently failing and returning None.
  3. Integrated 10-Day Relative Volume (RVOL) filter into the Buy Execution Loop.
  4. Prevents low-volume false breakouts by requiring institutional volume backing (RVOL >= 1.3x).
  5. Uses Alpaca Free IEX feed natively for ultra-fast multi-symbol batched scans.
  6. Automatically synchronizes portfolio constraints (Max 8 concurrent active holdings).
  7. Fixed heartbeat JSON serialization - uses proper json.dumps() instead of str().
  8. Added robust error handling for empty peak_prices and missing database records.
  9. Integrated with existing Supabase tables: open_positions and realized_trades.
  10. FIXED: close_position() was indexing position_response.data (a list) with a string
      key, which throws a TypeError the moment any exit actually fires. Now correctly
      reads position_response.data[0].
  11. NEW: Shariah compliance gate — a position can never be sold on the same ET calendar
      day it was bought (qabd / rightful possession, ~T+1 settlement). See
      is_position_settled().
  12. NEW: WATCHLIST, thresholds, timezones, and calculate_sma() now live in
      strategy_config.py, shared with app.py and the backtest script, so they can't
      drift out of sync (this is also where MSTR was excluded for Shariah compliance —
      crypto balance-sheet exposure).
  13. NEW: reconcile_orphaned_positions() — Alpaca can hold positions with no matching
      row in Supabase's open_positions table (manual trades, or positions bought before
      tracking existed). Because is_position_settled() fail-safes to "unsettled" when it
      finds no entry date, an orphaned position could never be sold — permanently. This
      backfills a real entry date from Alpaca's own fill history at every startup, so
      no position gets stuck unsellable again.

Execution Infrastructure: Recommended to deploy 24/7 via Railway or AWS EC2.
"""

import os
import time
import logging
import json
from datetime import datetime, timedelta, timezone
import pandas as pd

# ── ALPACA SDK IMPORT MODULES ──
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.timeframe import TimeFrame
from supabase import create_client, Client

# ── SHARED STRATEGY CONFIG (single source of truth — see strategy_config.py) ──
from strategy_config import ET, SGT, MAX_CORES_BUDGET, RVOL_THRESHOLD, WATCHLIST, calculate_sma

# ─────────────────────────────────────────────
# LOGGING SYSTEM CONFIGURATION
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CREDENTIAL VALUATION LAYER
# ─────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "YOUR_ALPACA_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "YOUR_ALPACA_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_KEY")

try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
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
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
            logger.info("✅ Created initial bot_state record")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to ensure bot_state record: {e}")
        return False

def update_heartbeat_only():
    """
    Touch last_heartbeat WITHOUT running a full scan cycle. Used during
    off-market-hours sleep so the dashboard's is_bot_alive() staleness check
    doesn't falsely report the bot as OFF just because it's correctly idle
    (the market being closed is not the same as the process being dead).
    """
    try:
        now_utc = datetime.now(timezone.utc)
        supabase.table("bot_state").update({
            "last_heartbeat": now_utc.isoformat() + "+00",
            "updated_at": now_utc.strftime("%Y-%m-%d %H:%M:%S")
        }).eq("id", 1).execute()
    except Exception as e:
        logger.error(f"❌ Off-hours heartbeat update failed: {e}")

# ─────────────────────────────────────────────
# TECHNICAL ANALYSIS
# ─────────────────────────────────────────────
def process_market_indicators(symbol: str):
    """
    Queries historical daily bars from Alpaca free tier IEX endpoint,
    calculates SMA(20), SMA(50), and the 10-Day Relative Volume (RVOL).
    """
    try:
        end_date = datetime.now(ET)
        start_date = end_date - timedelta(days=90)

        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
            feed="iex"
        )

        bars = data_client.get_stock_bars(request_params)
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
        historical_volumes = volumes[-11:-1]
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
# POSITION & TRADE LOGGING TO EXISTING TABLES
# ─────────────────────────────────────────────

def log_open_position(symbol: str, entry_price: float, qty: int, strategy: str = "SMA_RVOL"):
    """Log new open position to open_positions table"""
    try:
        position_data = {
            "symbol": symbol,
            "strategy": strategy,
            "entry_price": entry_price,
            "qty": qty,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        response = supabase.table("open_positions").insert(position_data).execute()
        logger.info(f"✅ Open position logged: {symbol} @ ${entry_price:.2f} x {qty}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to log open position: {e}")
        return False

def close_position(symbol: str, exit_price: float, reason: str = "SMA_Crossover"):
    """Remove position from open_positions and record in realized_trades"""
    try:
        # Get the position from open_positions
        position_response = supabase.table("open_positions").select("*").eq("symbol", symbol).execute()

        if not position_response.data:
            logger.warning(f"⚠️ Position not found in open_positions: {symbol}")
            return False

        position = position_response.data[0]
        entry_price = float(position["entry_price"])
        qty = float(position["qty"])

        # Calculate P&L
        pl_usd = (exit_price - entry_price) * qty
        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        # Format for display
        pl_display = f"${pl_usd:.2f}"
        pc_pct = f"{pl_pct:.2f}%"

        # Get current time in SGT
        now_sgt = datetime.now(SGT)
        time_sgt_str = now_sgt.strftime("%Y-%m-%d %H:%M:%S")
        date_str = now_sgt.strftime("%Y-%m-%d")

        # Log to realized_trades
        trade_data = {
            "date": date_str,
            "symbol": symbol,
            "buy_price": str(entry_price),
            "sell_price": str(exit_price),
            "qty": qty,
            "pl_usd": pl_usd,
            "pl_display": pl_display,
            "pc_pct": pc_pct,
            "time_sgt": time_sgt_str,
            "reason": reason,
            "strategy": "SMA_RVOL",
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        supabase.table("realized_trades").insert(trade_data).execute()
        logger.info(f"✅ Realized trade logged: {symbol} | Entry: ${entry_price:.2f} | Exit: ${exit_price:.2f} | P&L: {pl_display} ({pc_pct})")

        # Remove from open_positions
        supabase.table("open_positions").delete().eq("symbol", symbol).execute()
        logger.info(f"✅ Position closed: {symbol}")

        return True
    except Exception as e:
        logger.error(f"❌ Failed to close position: {e}")
        return False

def is_position_open(symbol: str) -> bool:
    """Check if position exists in open_positions table"""
    try:
        response = supabase.table("open_positions").select("symbol").eq("symbol", symbol).execute()
        return len(response.data) > 0
    except Exception as e:
        logger.error(f"❌ Failed to check open position: {e}")
        return False

def is_position_settled(symbol: str) -> bool:
    """
    SHARIAH COMPLIANCE — QABD (RIGHTFUL POSSESSION) CHECK
    ───────────────────────────────────────────────────────
    A position may only be sold once at least one full ET calendar day has
    passed since it was opened (i.e. it was NOT bought today). This mirrors
    T+1 settlement and ensures the bot never closes a position on the same
    day it was acquired. If the entry date can't be determined, we treat the
    position as UNSETTLED (fail safe — block the sale) rather than assume
    it's tradeable.
    """
    try:
        response = supabase.table("open_positions").select("updated_at").eq("symbol", symbol).execute()
        if not response.data:
            logger.warning(f"⚠️ No entry timestamp found for {symbol} — treating as unsettled.")
            return False

        entry_raw = response.data[0]["updated_at"]
        entry_dt_utc = datetime.fromisoformat(entry_raw.replace("Z", "+00:00"))
        entry_date_et = entry_dt_utc.astimezone(ET).date()
        today_et = datetime.now(ET).date()

        settled = entry_date_et < today_et
        if not settled:
            logger.info(f"⏳ {symbol} bought today ({entry_date_et}) — not yet settled, exit blocked.")
        return settled
    except Exception as e:
        logger.error(f"❌ Failed to verify settlement status for {symbol}: {e}")
        return False  # fail safe: block the sale rather than risk a same-day close

def reconcile_orphaned_positions():
    """
    STARTUP SAFETY NET — RECONCILE ALPACA POSITIONS WITH SUPABASE TRACKING
    ───────────────────────────────────────────────────────────────────────
    Alpaca can hold positions that never got logged into open_positions
    (manual trades, positions from before tracking existed, etc). Because
    is_position_settled() fail-safes to "unsettled" whenever it finds no
    entry date, an orphaned position would otherwise be blocked from ever
    selling — permanently, not just today. This backfills a REAL entry date
    for each orphan from Alpaca's own most recent filled BUY order for that
    symbol, so the settlement gate can evaluate it correctly. Falls back to
    "now" only if no fill history can be found (rare), which conservatively
    re-starts that position's settlement clock rather than assuming it's safe.
    Runs once per bot startup; already-tracked symbols are skipped instantly.
    """
    try:
        live_positions = trading_client.get_all_positions()
        orphans = [p for p in live_positions if not is_position_open(p.symbol)]

        if not orphans:
            logger.info("✅ Reconciliation: no orphaned positions found.")
            return

        logger.warning(f"🔧 Reconciliation: {len(orphans)} position(s) held in Alpaca with no Supabase record — backfilling entry dates.")

        for pos in orphans:
            symbol = pos.symbol
            entry_date_iso = None

            try:
                orders_filter = GetOrdersRequest(
                    symbols=[symbol],
                    status=QueryOrderStatus.CLOSED,
                    side=OrderSide.BUY,
                    limit=1,
                )
                orders = trading_client.get_orders(filter=orders_filter)
                if orders and getattr(orders[0], "filled_at", None):
                    entry_date_iso = orders[0].filled_at.isoformat()
            except Exception as order_err:
                logger.error(f"❌ Could not fetch fill history for {symbol}: {order_err}")

            if not entry_date_iso:
                logger.warning(f"⚠️ No fill history found for {symbol} — using current time as fallback (this will block same-day exit for {symbol} today only).")
                entry_date_iso = datetime.now(timezone.utc).isoformat()

            try:
                supabase.table("open_positions").insert({
                    "symbol": symbol,
                    "strategy": "RECONCILED",
                    "entry_price": float(pos.avg_entry_price),
                    "qty": float(pos.qty),
                    "updated_at": entry_date_iso,
                }).execute()
                logger.info(f"✅ Reconciled {symbol}: entry_date={entry_date_iso}, qty={pos.qty}")
            except Exception as insert_err:
                logger.error(f"❌ Reconciliation insert failed for {symbol}: {insert_err}")

    except Exception as e:
        logger.error(f"❌ Reconciliation pass failed: {e}")

# ─────────────────────────────────────────────
# CORE EXECUTION LOOP MATRIX
# ─────────────────────────────────────────────
def run_execution_cycle():
    logger.info("Initializing automated scan iteration across watchlist...")

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
                # ── SHARIAH GATE: block same-day exits (qabd / rightful possession) ──
                if not is_position_settled(symbol):
                    continue

                logger.info(f"🚨 EXIT TRIGGER for {symbol}: Price={price:.2f}, SMA20={sma20:.2f}, SMA50={sma50:.2f}")
                try:
                    order = trading_client.submit_order(order_data=MarketOrderRequest(
                        symbol=symbol,
                        qty=active_holdings[symbol].qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    ))
                    logger.info(f"Exit order executed: {order.id}")

                    # ✅ LOG TO SUPABASE
                    close_position(symbol, price, reason="SMA_Crossover_Exit")

                except Exception as ex:
                    logger.error(f"Failed routing exit for {symbol}: {ex}")

        # ── PHASE 2: EVALUATE ENTRIES WITH RVOL GATEKEEPER ──
        if len(trading_client.get_all_positions()) >= MAX_CORES_BUDGET:
            logger.info("Portfolio at max capacity. Skipping entry scan.")
        else:
            for symbol in WATCHLIST:
                if symbol in active_holdings:
                    continue

                # ✅ Also check if already in open_positions table
                if is_position_open(symbol):
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

                            # ✅ LOG TO SUPABASE
                            log_open_position(symbol, price, shares_to_buy, strategy="SMA_RVOL")

                    except Exception as entry_ex:
                        logger.error(f"Failed submitting buy order for {symbol}: {entry_ex}")

        # ── PHASE 3: DATABASE SYNCHRONIZATION ──
        try:
            clean_peaks = {str(ticker): float(val) for ticker, val in peak_prices.items()}
            clean_peaks_json = json.dumps(clean_peaks)
            now_utc = datetime.now(timezone.utc)

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
                    "timestamp": datetime.now(timezone.utc).isoformat()
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
    logger.info("⚡ System Kernel Engaged. Continuous Daily SMA/RVOL Automation Core Online.")

    # TEST CONNECTION FIRST
    if not test_supabase_connection():
        logger.error("❌ Cannot connect to Supabase. Check environment variables and network.")
        exit(1)

    # Initialize database record on startup
    if not ensure_bot_state_record():
        logger.error("❌ Failed to initialize database. Exiting.")
        exit(1)

    # Backfill any Alpaca positions missing from Supabase tracking (see docstring #13)
    reconcile_orphaned_positions()

    # Run a quick test heartbeat on startup
    try:
        test_peaks = {"TEST": 100.0}
        test_json = json.dumps(test_peaks)
        supabase.table("bot_state").update({
            "peak_prices": test_json,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", 1).execute()
        logger.info("✅ Initial heartbeat test successful")
    except Exception as e:
        logger.error(f"❌ Initial heartbeat test failed: {e}")

    while True:
        now = datetime.now(ET)
        # Scan blocks execution logic runs every 5 minutes during active market framework hours
        if now.weekday() < 5 and (9 <= now.hour <= 16):
            run_execution_cycle()
            # Sleep in short increments (instead of one 300s blocking sleep) so the
            # heartbeat stays fresh throughout the wait — dashboard staleness threshold
            # is 180s, so a single 300s sleep left the bot looking "OFF" for the last
            # ~2 minutes of every 5-minute cycle even though it was completely healthy.
            for _ in range(5):
                time.sleep(60)
                update_heartbeat_only()
        else:
            logger.info("Market framework outside operational baseline standard hours. Sleep mode active.")
            update_heartbeat_only()  # keep the dashboard's liveness check accurate while idle
            time.sleep(120)  # shorter sleep so heartbeat stays fresh (dashboard staleness threshold is 180s)
