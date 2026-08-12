"""
backtest_settlement_gate.py
─────────────────────────────────────────────────────────────────────────────
Replays the EXACT entry/exit logic from bot.py (SMA20/SMA50 trend + RVOL
gatekeeper) over historical daily bars, and runs it TWICE:

  Variant A — "baseline"     : current live logic, no holding-period rule.
  Variant B — "settlement"   : same logic, but a position can never be sold
                                on the same calendar day (ET) it was bought,
                                matching the Shariah qabd / T+1 settlement
                                gate added to bot.py.

Prints side-by-side metrics so you can see the actual impact rather than
guess at it. Uses the same Alpaca IEX data source as the live bot for
consistency — set ALPACA_API_KEY / ALPACA_SECRET_KEY env vars before running.

CAVEATS (read before trusting the numbers):
  1. Decision price == that day's close for BOTH the signal and the fill.
     The live bot actually evaluates intraday against the latest daily bar,
     so real fills will differ slightly from this idealized EOD backtest.
  2. No commissions modeled (Alpaca is commission-free) but also no
     slippage — real fills will be marginally worse.
  3. WATCHLIST below is today's list, applied across the whole backtest
     window — this is a form of survivorship bias since it doesn't reflect
     what would have been on the watchlist historically.
  4. This is a directional comparison tool (baseline vs settlement-gated),
     not a certified return projection.
"""

import os
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from strategy_config import ET, MAX_CORES_BUDGET, RVOL_THRESHOLD, WATCHLIST, calculate_sma

STARTING_CASH = 100_000.0
LOOKBACK_DAYS = 730  # ~2 years of daily bars

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "YOUR_ALPACA_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "YOUR_ALPACA_SECRET")


# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────
def fetch_all_bars(symbols, lookback_days):
    """Fetch daily bars for every symbol; return {symbol: DataFrame[date, close, volume]}."""
    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    end_date = datetime.now(ET)
    start_date = end_date - timedelta(days=lookback_days)

    frames = {}
    for symbol in symbols:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start_date,
                end=end_date,
                feed="iex",
            )
            bars = data_client.get_stock_bars(req)
            if not bars or symbol not in bars.data:
                print(f"⚠️  No data for {symbol}, skipping.")
                continue
            rows = [{"date": b.timestamp.astimezone(ET).date(), "close": b.close, "volume": b.volume}
                    for b in bars.data[symbol]]
            df = pd.DataFrame(rows).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
            if len(df) < 55:
                print(f"⚠️  Only {len(df)} bars for {symbol}, skipping (need 55+).")
                continue
            frames[symbol] = df
        except Exception as e:
            print(f"❌ Failed fetching {symbol}: {e}")
    return frames


# ─────────────────────────────────────────────
# INDICATORS (identical math to bot.py — calculate_sma imported from strategy_config)
# ─────────────────────────────────────────────
def indicators_as_of(df, idx):
    """Compute current_price/sma20/sma50/rvol using bars up to and including idx."""
    if idx < 54:  # need 55 bars: idx 0..idx inclusive = idx+1 bars
        return None
    closes = df["close"].iloc[:idx + 1].tolist()
    volumes = df["volume"].iloc[:idx + 1].tolist()

    current_price = closes[-1]
    sma20 = calculate_sma(closes, 20)
    sma50 = calculate_sma(closes, 50)

    current_volume = volumes[-1]
    historical_volumes = volumes[-11:-1]
    avg_10day_vol = sum(historical_volumes) / 10 if historical_volumes else 1.0
    rvol = current_volume / avg_10day_vol if avg_10day_vol > 0 else 1.0

    return {"price": current_price, "sma20": sma20, "sma50": sma50, "rvol": rvol}


# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_backtest(frames, apply_settlement_gate: bool):
    # Build the unified trading calendar (union of all trading days)
    all_dates = sorted(set(d for df in frames.values() for d in df["date"]))

    # Fast lookup: symbol -> {date: row_index}
    date_index = {sym: {d: i for i, d in enumerate(df["date"])} for sym, df in frames.items()}

    cash = STARTING_CASH
    positions = {}  # symbol -> {"entry_price": float, "entry_date": date, "qty": int}
    trade_log = []
    equity_curve = []

    for today in all_dates:
        # ── PHASE 1: EXITS ──
        for symbol in list(positions.keys()):
            if symbol not in date_index or today not in date_index[symbol]:
                continue
            idx = date_index[symbol][today]
            m = indicators_as_of(frames[symbol], idx)
            if not m:
                continue

            price, sma20, sma50 = m["price"], m["sma20"], m["sma50"]
            exit_signal = (price < sma20 < sma50) or (price < sma50)

            if exit_signal:
                pos = positions[symbol]
                if apply_settlement_gate and pos["entry_date"] == today:
                    continue  # blocked: same-day exit not allowed

                proceeds = price * pos["qty"]
                pl = proceeds - (pos["entry_price"] * pos["qty"])
                cash += proceeds
                holding_days = (today - pos["entry_date"]).days
                trade_log.append({
                    "symbol": symbol, "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": pos["entry_price"], "exit_price": price,
                    "qty": pos["qty"], "pl_usd": pl, "holding_days": holding_days
                })
                del positions[symbol]

        # ── PHASE 2: ENTRIES ──
        if len(positions) < MAX_CORES_BUDGET:
            for symbol in WATCHLIST:
                if symbol in positions or symbol not in date_index or today not in date_index[symbol]:
                    continue
                if len(positions) >= MAX_CORES_BUDGET:
                    break

                idx = date_index[symbol][today]
                m = indicators_as_of(frames[symbol], idx)
                if not m:
                    continue

                price, sma20, sma50, rvol = m["price"], m["sma20"], m["sma50"], m["rvol"]
                if (price > sma20 > sma50) and (rvol >= RVOL_THRESHOLD):
                    slots_left = MAX_CORES_BUDGET - len(positions)
                    target_allocation = min(cash / slots_left, cash * 0.12)
                    shares_to_buy = int(target_allocation // price)
                    if shares_to_buy > 0 and cash >= shares_to_buy * price:
                        cash -= shares_to_buy * price
                        positions[symbol] = {"entry_price": price, "entry_date": today, "qty": shares_to_buy}

        # ── MARK-TO-MARKET EQUITY ──
        mtm = cash
        for symbol, pos in positions.items():
            if symbol in date_index and today in date_index[symbol]:
                idx = date_index[symbol][today]
                mtm += frames[symbol]["close"].iloc[idx] * pos["qty"]
            else:
                mtm += pos["entry_price"] * pos["qty"]
        equity_curve.append({"date": today, "equity": mtm})

    return trade_log, equity_curve, cash, positions


# ─────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────
def summarize(label, trade_log, equity_curve):
    eq = pd.DataFrame(equity_curve)
    total_return_pct = (eq["equity"].iloc[-1] / STARTING_CASH - 1) * 100 if len(eq) else 0.0

    running_max = eq["equity"].cummax()
    drawdown = (eq["equity"] - running_max) / running_max
    max_drawdown_pct = drawdown.min() * 100 if len(eq) else 0.0

    trades = pd.DataFrame(trade_log)
    num_trades = len(trades)
    win_rate = (trades["pl_usd"] > 0).mean() * 100 if num_trades else 0.0
    avg_pl = trades["pl_usd"].mean() if num_trades else 0.0
    avg_holding_days = trades["holding_days"].mean() if num_trades else 0.0
    same_day_would_have_exited = (trades["holding_days"] == 0).sum() if num_trades else 0

    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    print(f"Final equity:          ${eq['equity'].iloc[-1]:,.2f}" if len(eq) else "No equity data")
    print(f"Total return:          {total_return_pct:.2f}%")
    print(f"Max drawdown:          {max_drawdown_pct:.2f}%")
    print(f"Number of trades:      {num_trades}")
    print(f"Win rate:              {win_rate:.1f}%")
    print(f"Avg P&L per trade:     ${avg_pl:,.2f}")
    print(f"Avg holding days:      {avg_holding_days:.1f}")
    print(f"Same-day exits found:  {same_day_would_have_exited}  (these are the trades the Shariah gate changes)")

    return {
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "num_trades": num_trades,
        "win_rate": win_rate,
        "avg_pl": avg_pl,
    }


if __name__ == "__main__":
    print("Fetching historical bars...")
    frames = fetch_all_bars(WATCHLIST, LOOKBACK_DAYS)
    print(f"Loaded {len(frames)} / {len(WATCHLIST)} symbols with sufficient history.\n")

    print("Running Variant A — baseline (no holding-period rule)...")
    trades_a, equity_a, _, _ = run_backtest(frames, apply_settlement_gate=False)
    result_a = summarize("VARIANT A: BASELINE (current live logic)", trades_a, equity_a)

    print("\nRunning Variant B — settlement-gated (Shariah same-day-exit block)...")
    trades_b, equity_b, _, _ = run_backtest(frames, apply_settlement_gate=True)
    result_b = summarize("VARIANT B: SETTLEMENT-GATED (no same-day exits)", trades_b, equity_b)

    print(f"\n{'=' * 60}\nDELTA (B - A)\n{'=' * 60}")
    print(f"Return difference:     {result_b['total_return_pct'] - result_a['total_return_pct']:+.2f} pp")
    print(f"Drawdown difference:   {result_b['max_drawdown_pct'] - result_a['max_drawdown_pct']:+.2f} pp")
    print(f"Trade count difference:{result_b['num_trades'] - result_a['num_trades']:+d}")
