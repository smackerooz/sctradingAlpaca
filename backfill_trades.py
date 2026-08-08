"""
Backfill historical trades from Alpaca to Supabase
Run this once to populate open_positions and realized_trades tables
"""

import os
from datetime import datetime
import pytz
from alpaca.trading.client import TradingClient
from supabase import create_client, Client

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
ET = pytz.timezone("US/Eastern")
SGT = pytz.timezone("Asia/Singapore")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "YOUR_ALPACA_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "YOUR_ALPACA_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_KEY")

try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Connected to Alpaca and Supabase")
except Exception as e:
    print(f"❌ Connection failed: {e}")
    exit(1)

# ─────────────────────────────────────────────
# GET CURRENT POSITIONS (FOR open_positions TABLE)
# ─────────────────────────────────────────────
def backfill_open_positions():
    """Get all current open positions from Alpaca and add to open_positions table"""
    print("\n📍 Fetching current positions from Alpaca...")
    
    try:
        positions = trading_client.get_all_positions()
        
        if not positions:
            print("⚠️  No open positions found in Alpaca")
            return
        
        positions_added = 0
        positions_skipped = 0
        
        for position in positions:
            symbol = position.symbol
            entry_price = float(position.avg_fill_price)
            qty = int(position.qty)
            
            # ✅ Check if already exists
            existing = supabase.table("open_positions").select("symbol").eq("symbol", symbol).execute()
            
            if existing.data:
                print(f"⏭️  Skipping {symbol} - already in open_positions")
                positions_skipped += 1
                continue
            
            try:
                position_data = {
                    "symbol": symbol,
                    "strategy": "BACKFILL",
                    "entry_price": entry_price,
                    "qtc": qty,
                    "updated_at": datetime.utcnow().isoformat()
                }
                
                supabase.table("open_positions").insert(position_data).execute()
                print(f"✅ Added {symbol} to open_positions | Entry: \${entry_price:.2f} | Qty: {qty}")
                positions_added += 1
                
            except Exception as e:
                print(f"❌ Failed to add {symbol}: {e}")
        
        print(f"\n✅ Open positions backfill complete! Added: {positions_added} | Skipped: {positions_skipped}")
        
    except Exception as e:
        print(f"❌ Failed to fetch positions: {e}")

# ─────────────────────────────────────────────
# GET CLOSED ORDERS (FOR realized_trades TABLE)
# ─────────────────────────────────────────────
def backfill_realized_trades():
    """Get all closed orders from Alpaca and populate realized_trades table"""
    print("\n📊 Fetching closed orders from Alpaca...")
    
    try:
        # Get all closed orders (limit 500 per API)
        orders = trading_client.get_orders(status="closed", limit=500)
        
        if not orders:
            print("⚠️  No closed orders found in Alpaca")
            return
        
        print(f"📋 Retrieved {len(orders)} closed orders from Alpaca")
        
        # Group orders by symbol to match buy/sell pairs
        symbol_orders = {}
        for order in orders:
            symbol = order.symbol
            if symbol not in symbol_orders:
                symbol_orders[symbol] = []
            symbol_orders[symbol].append(order)
        
        trades_added = 0
        trades_skipped = 0
        
        # Process each symbol's buy/sell pairs
        for symbol, orders_list in symbol_orders.items():
            # Sort by filled_at time
            orders_list.sort(key=lambda x: x.filled_at if x.filled_at else datetime.now())
            
            # Look for buy/sell pairs
            i = 0
            while i < len(orders_list) - 1:
                buy_order = orders_list[i]
                sell_order = orders_list[i + 1]
                
                # Check if we have a valid buy followed by a sell
                if (buy_order.side == "buy" and sell_order.side == "sell" and
                    buy_order.filled_at and sell_order.filled_at and
                    buy_order.filled_qty == sell_order.filled_qty):
                    
                    try:
                        entry_price = float(buy_order.filled_avg_price)
                        exit_price = float(sell_order.filled_avg_price)
                        qty = int(buy_order.filled_qty)
                        
                        # ✅ IMPROVED: Check by symbol + buy_price + sell_price + qty (more unique)
                        existing = supabase.table("realized_trades").select("id").eq(
                            "symbol", symbol
                        ).eq(
                            "buy_price", str(entry_price)
                        ).eq(
                            "sell_price", str(exit_price)
                        ).eq(
                            "qty", qty
                        ).execute()
                        
                        if existing.data:
                            print(f"⏭️  Skipping {symbol} trade (\${entry_price:.2f} → \${exit_price:.2f}) - already recorded")
                            trades_skipped += 1
                            i += 2
                            continue
                        
                        # Calculate P&L
                        pl_usd = (exit_price - entry_price) * qty
                        pl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price != 0 else 0
                        
                        # Format for display
                        pl_display = f"\${pl_usd:.2f}"
                        pc_pct = f"{pl_pct:.2f}%"
                        
                        # Get time in SGT from sell order
                        sell_time = sell_order.filled_at
                        if sell_time.tzinfo is None:
                            sell_time = ET.localize(sell_time)
                        sell_time_sgt = sell_time.astimezone(SGT)
                        time_sgt_str = sell_time_sgt.strftime("%Y-%m-%d %H:%M:%S")
                        date_str = sell_time_sgt.strftime("%Y-%m-%d")
                        
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
                            "reason": "BACKFILL",
                            "strategy": "BACKFILL",
                            "created_at": datetime.utcnow().isoformat()
                        }
                        
                        supabase.table("realized_trades").insert(trade_data).execute()
                        print(f"✅ Added trade: {symbol} | Buy: \${entry_price:.2f} | Sell: \${exit_price:.2f} | P&L: {pl_display} ({pc_pct})")
                        trades_added += 1
                        i += 2
                        
                    except Exception as e:
                        print(f"❌ Failed to add trade for {symbol}: {e}")
                        i += 1
                else:
                    i += 1
        
        print(f"\n✅ Realized trades backfill complete! Added: {trades_added} | Skipped: {trades_skipped}")
        
    except Exception as e:
        print(f"❌ Failed to fetch orders: {e}")

# ─────────────────────────────────────────────
# VERIFICATION FUNCTION
# ─────────────────────────────────────────────
def verify_backfill():
    """Verify what was backfilled to Supabase"""
    print("\n" + "=" * 60)
    print("📊 BACKFILL VERIFICATION")
    print("=" * 60)
    
    try:
        # Count open positions
        open_pos = supabase.table("open_positions").select("symbol", count="exact").execute()
        open_count = len(open_pos.data) if open_pos.data else 0
        print(f"\n📍 open_positions table: {open_count} records")
        if open_pos.data:
            for pos in open_pos.data[:5]:
                print(f"   - {pos['symbol']}")
            if len(open_pos.data) > 5:
                print(f"   ... and {len(open_pos.data) - 5} more")
        
        # Count realized trades
        realized = supabase.table("realized_trades").select("symbol", count="exact").execute()
        realized_count = len(realized.data) if realized.data else 0
        print(f"\n📊 realized_trades table: {realized_count} records")
        if realized.data:
            for trade in realized.data[:5]:
                print(f"   - {trade['symbol']}")
            if len(realized.data) > 5:
                print(f"   ... and {len(realized.data) - 5} more")
        
        print("\n" + "=" * 60)
        
    except Exception as e:
        print(f"❌ Verification failed: {e}")

# ─────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ALPACA TO SUPABASE BACKFILL SCRIPT")
    print("=" * 60)
    
    print("\nThis script will:")
    print("1️⃣  Fetch all current positions from Alpaca → open_positions table")
    print("2️⃣  Fetch all closed orders from Alpaca → realized_trades table")
    print("3️⃣  Check for duplicates before inserting")
    print("4️⃣  Verify backfill results")
    print("\n⏳ Proceeding in 3 seconds... (Press Ctrl+C to cancel)\n")
    
    import time
    time.sleep(3)
    
    # Run backfills
    backfill_open_positions()
    backfill_realized_trades()
    
    # Verify results
    verify_backfill()
    
    print("\n" + "=" * 60)
    print("✅ BACKFILL COMPLETE!")
    print("=" * 60)
    print("\n✨ Your Supabase tables are now populated with historical data.")
    print("🤖 You can now safely run your bot.py\n")
