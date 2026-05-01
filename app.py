import streamlit as st
import pytz
import time
import pandas as pd
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Super 74 Bot V3 FIXED", layout="wide")

SGT = pytz.timezone("Asia/Singapore")

SCAN_INTERVAL = 20
MAX_TRADES_PER_DAY = 15
MAX_OPEN_POSITIONS = 5
RISK_PER_TRADE = 0.004
COOLDOWN_SECONDS = 600

WATCHLIST = [
"AAPL","MSFT","NVDA","TSLA","META","AMZN","AMD","GOOGL","AVGO","ORCL",
"INTC","QCOM","TXN","ADBE","CRM","NFLX","CSCO","ASML","MU","AMAT",
"JPM","BAC","WMT","COST","PG","V","MA","UNH","HD","DIS",
"XOM","CVX","CAT","GE","BA","HON","MMM","UPS","FDX","LMT",
"ABBV","PEP","KO","PFE","TMO","LLY","AZN","NKE","SBUX","T",
"VZ","TM
