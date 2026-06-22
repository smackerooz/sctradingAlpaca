"""
app_v3_sma.py — Professional Trading Dashboard (SMA Trend-Following Edition)
─────────────────────────────────────────────────────────────────────────
UPDATES IN v3:
  1. Removed legacy ORB/VWAP intraday schedule tracking banners.
  2. Integrated full tracking matrix supporting your expanded 50-stock watchlist.
  3. Re-wired the native Backtester Engine to query and parse "SMA-CROSS" trade metrics.
  4. Patched strategy override models and liquidation P&L recording arrays.

Run on Streamlit Cloud:
    Secrets required: ALPACA_API_KEY, ALPACA_SECRET_KEY, supabase.url, supabase.key
"""

import streamlit as st
import pytz
import time
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
import streamlit.components.v1 as components
from supabase import create_client, Client

# ─────────────────────────────────────────────
# HTML TABLE HELPER
# ─────────────────────────────────────────────
def render_html_table(df: pd.DataFrame, pl_col: str = "pl_usd") -> None:
    if df.empty:
        st.info("No data recorded.")
        return

    def _cell(val, col):
        style = ""
        if col == pl_col:
            try:
                v = float(str(val).replace("$","").replace("+","").replace(",",""))
                style = "color:#4ade80;font-weight:600;" if v >= 0 else "color:#f87171;font-weight:600;"
            except:
                pass
        elif col in ("pl_display", "pl_pct"):
            s = str(val)
            style = "color:#4ade80;font-weight:600;" if "🟢" in s or "+" in s else "color:#f87171;font-weight:600;"
        return f'<td style="padding:8px 12px;border-bottom:1px solid #1e2330;white-space:nowrap;{style}">{val}</td>'

    headers = "".join(
        f'<th style="padding:8px 12px;border-bottom:2px solid #1e2330;color:#5a6478;'
        f'font-size:10px;letter-spacing:0.1em;text-transform:uppercase;'
        f'font-weight:500;text-align:left;white-space:nowrap;">{col}</th>'
        for col in df.columns
    )
    rows_html = ""
    for i, (_, row) in enumerate(df.iterrows()):
        bg = "#0f1219" if i % 2 == 0 else "#0d0f14"
        cells =
