"""
strategy_config.py — Single source of truth for the SMA/RVOL trend-following
strategy: watchlist, timezones, and thresholds.

Imported by:
  - bot.py                      (Railway execution engine)
  - app.py                      (Streamlit dashboard)
  - backtest_settlement_gate.py (backtest tool)

Keeping this in one place is what prevents the watchlist drifting out of
sync between the bot and the dashboard (which is what happened before —
app.py had removed MSTR for Shariah compliance, bot.py still had it).

MSTR is intentionally excluded from WATCHLIST — Shariah-compliance screen,
cryptocurrency balance-sheet exposure.
"""

import numpy as np
import pytz

# ── Timezones ──
ET = pytz.timezone("US/Eastern")
SGT = pytz.timezone("Asia/Singapore")

# ── Portfolio & signal thresholds ──
MAX_CORES_BUDGET = 8
RVOL_THRESHOLD = 1.3  # Institutional volume backing filter (130% of 10-day average)

# ── Watchlist (MSTR excluded — see module docstring) ──
WATCHLIST = [
    "NVDA", "AMD", "AVGO", "QCOM", "AMAT", "ASML", "MU", "KLAC", "SMCI", "ARM",
    "PANW", "TSM", "LRCX", "ON", "MPWR", "MRVL", "NXPI", "TEAM", "INTA",
    "CRWD", "ZS", "ADBE", "WDAY", "SNPS", "NOW", "SHOP", "TXN", "CDNS", "MCHP",
    "SWKS", "FTNT", "ANET", "UBER", "DASH", "TSLA", "ISRG", "VRTX", "LLY", "MRK",
    "AAPL", "JNJ", "PEP", "LIN", "REGN", "INTC", "PG", "NKE", "ADSK", "MDT",
]


def calculate_sma(prices: list, period: int) -> float:
    """Simple moving average over the last `period` values of `prices`."""
    if len(prices) < period:
        return 0.0
    return float(np.mean(prices[-period:]))
