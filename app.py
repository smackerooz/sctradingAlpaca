"""
app.py — Dashboard (Streamlit Cloud)
View-only monitoring dashboard. All trading is done by bot.py on Railway.
Reads account data from Alpaca and trade/log history from Supabase.
"""

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
import pytz
import time
from datetime import datetime, timedelta
from supabase import create_client, Client
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ─────────────────────────────────────────────
# CONFIG — keep in sync with bot.py
# ─────────────────────────────────────────────
SGT           = pytz.timezone("Asia/Singapore")
TARGET_PROFIT = 200.0
MAX_TRADE_USD = 300.0
CASH_BUFFER   = 95_000.0
SCAN_INTERVAL = 10

# ── STOCK_PROFILES — keep in sync with bot.py ───────────────────────────────
STOCK_PROFILES = {
    # 🔵 Large-Cap Stable
    "AAPL"  : (0.010, 0.006, 0.004),
    "MSFT"  : (0.010, 0.006, 0.004),
    "GOOGL" : (0.010, 0.006, 0.004),
    "AMZN"  : (0.013, 0.008, 0.006),
    "ADBE"  : (0.013, 0.008, 0.006),
    "CRM"   : (0.013, 0.008, 0.006),
    # 🟡 Mid Volatility
    "AVGO"  : (0.013, 0.008, 0.006),
    "QCOM"  : (0.013, 0.008, 0.006),
    "AMAT"  : (0.013, 0.008, 0.006),
    "ASML"  : (0.013, 0.008, 0.006),
    # 🔴 High Volatility / Momentum
    "NVDA"  : (0.018, 0.010, 0.008),
    "TSLA"  : (0.020, 0.012, 0.009),
    "AMD"   : (0.015, 0.009, 0.007),
    "PLTR"  : (0.018, 0.010, 0.008),
    "SNOW"  : (0.018, 0.010, 0.008),
}
WATCHLIST = list(STOCK_PROFILES.keys())

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(page_title="Trading Bot Dashboard", page_icon="📈", layout="wide")

# ─────────────────────────────────────────────
# KEEPALIVE — prevents Streamlit Cloud from sleeping
# ─────────────────────────────────────────────
components.html("""
    <div style="font-family:monospace;font-size:12px;color:#aaa;background:#1a1a2e;
                border:1px solid #333;border-radius:6px;padding:5px 12px;
                display:inline-flex;align-items:center;gap:8px;margin-bottom:4px;">
        <span style="color:#26a65b;font-size:10px;">●</span>
        <span>Keepalive ping in: <strong id="cd" style="color:#4f8ef7;">5:00</strong></span>
        <span id="ps" style="color:#aaa;font-size:11px;"></span>
    </div>
    <script>
    var r=300;
    setInterval(function(){
        var m=Math.floor(r/60),s=r%60;
        document.getElementById('cd').textContent=m+':'+(s<10?'0':'')+s;
        document.getElementById('cd').style.color=r<=10?'#e74c3c':r<=60?'#f0a500':'#4f8ef7';
        if(r<=0){
            try{fetch(window.location.href,{mode:'no-cors',cache:'no-store'});}catch(e){}
            document.getElementById('ps').textContent='✅ Pinged!';
            setTimeout(function(){document.getElementById('ps').textContent='';},3000);
            r=300;
        } else { r--; }
    },1000);
    </script>
""", height=40)

# ─────────────────────────────────────────────
# CLIENTS — supports both flat and nested secrets
# ─────────────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

@st.cache_resource
def get_alpaca() -> TradingClient:
    # Support both nested [alpaca] and flat ALPACA_API_KEY formats
    try:
        api_key    = st.secrets["alpaca"]["api_key"]
        secret_key = st.secrets["alpaca"]["secret_key"]
    except Exception:
        api_key    = st.secrets["ALPACA_API_KEY"]
        secret_key = st.secrets["ALPACA_SECRET_KEY"]
    return TradingClient(api_key, secret_key, paper=True)

supabase       = get_supabase()
trading_client = get_alpaca()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def load_baseline() -> float:
    try:
        row = supabase.table("weekly_baseline").select("*").eq("id", 1).execute()
        if row.data:
            bl          = row.data[0]
            saved_date  = datetime.fromisoformat(bl["date"]).date()
            today       = datetime.now(SGT).date()
            last_monday = today - timedelta(days=today.weekday())
            if saved_date >= last_monday:
                return float(bl["baseline"])
    except Exception:
        pass
    try:
        return float(trading_client.get_account().last_equity)
    except Exception:
        return 0.0

def load_trades_today() -> list:
    try:
        today = datetime.now(SGT).date().isoformat()
        rows  = supabase.table("realized_trades").select("*") \
                        .eq("date", today).order("id", desc=True).execute()
        result = []
        for r in rows.data:
            result.append({
                "Symbol":     r["symbol"],
                "Buy Price":  r["buy_price"],
                "Sell Price": r["sell_price"],
                "Qty":        r["qty"],
                "P&L ($)":    r["pl_display"],
                "P&L (%)":    r["pl_pct"],
                "Time (SGT)": r["time_sgt"],
                "Reason":     r["reason"],
                "_pl_usd":    float(r["pl_usd"]),
            })
        return result
    except Exception:
        return []

def load_bot_logs() -> list:
    try:
        rows = supabase.table("bot_logs").select("message,created_at") \
                       .order("created_at", desc=True).limit(50).execute()
        result = []
        for r in rows.data:
            try:
                utc_time = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                sgt_time = utc_time.astimezone(SGT).strftime("%H:%M:%S")
                result.append(f"[{sgt_time}] {r['message']}")
            except Exception:
                result.append(r["message"])
        return result
    except Exception:
        return []

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1, min_periods=period).mean()
    avg_l = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def calc_macd(series, fast=12, slow=26, signal=9):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig, macd - sig

def compute_signal_score(df):
    close = df["close"].dropna() if "close" in df.columns else df["Close"].dropna()
    if len(close) < 30:
        return {"score":50,"direction":"⚪ Neutral","rsi":None,"macd_hist":None,
                "trend_score":0,"rsi_score":0,"macd_score":0,"momentum_score":0}
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(min(50,len(close))).mean().iloc[-1]
    curr  = close.iloc[-1]
    prev  = close.iloc[-6] if len(close) >= 6 else curr

    trend_score = 25 if curr>sma20>sma50 else (19 if curr>sma20 else (0 if curr<sma20<sma50 else 6))
    rsi_val     = calc_rsi(close).iloc[-1]; rsi_val = 50.0 if pd.isna(rsi_val) else rsi_val
    rsi_score   = 22 if rsi_val<30 else (18 if rsi_val<45 else (12 if rsi_val<55 else (17 if rsi_val<70 else 8)))
    _,_,hist    = calc_macd(close); hist_val = hist.iloc[-1]; hist_val = 0.0 if pd.isna(hist_val) else hist_val
    macd_score  = 25 if hist_val>0.1 else (20 if hist_val>0 else (5 if hist_val>-0.1 else 0))
    mom_pct     = (curr-prev)/prev if prev != 0 else 0.0; mom_pct = 0.0 if pd.isna(mom_pct) else mom_pct
    mom_score   = 25 if mom_pct>0.03 else (20 if mom_pct>0.01 else (15 if mom_pct>0 else (10 if mom_pct>-0.01 else (5 if mom_pct>-0.03 else 0))))
    score       = trend_score+rsi_score+macd_score+mom_score
    direction   = "🟢 Bullish" if score>=70 else ("🟡 Mild Bullish" if score>=55 else ("⚪ Neutral" if score>=45 else ("🟠 Mild Bearish" if score>=30 else "🔴 Bearish")))
    return {"score":score,"direction":direction,"rsi":round(rsi_val,1),
            "macd_hist":round(hist_val,4),"trend_score":trend_score,
            "rsi_score":rsi_score,"macd_score":macd_score,"momentum_score":mom_score}

# ─────────────────────────────────────────────
# FETCH ACCOUNT DATA
# ─────────────────────────────────────────────
try:
    account        = trading_client.get_account()
    CASH           = float(account.cash)
    EQUITY         = float(account.equity)
    positions      = trading_client.get_all_positions()
    unrealized     = round(sum(float(p.unrealized_pl) for p in positions), 2)
    total_holdings = round(sum(float(p.market_value)  for p in positions), 2)
except Exception:
    CASH,EQUITY,unrealized,total_holdings,positions = 0.0,0.0,0.0,0.0,[]

baseline     = load_baseline()
total_delta  = round(EQUITY - baseline, 2)
realized     = round(total_delta - unrealized, 2)
combined     = round(unrealized + realized, 2)
progress_pct = min(max(realized/TARGET_PROFIT,0.0),1.0) if realized>0 else 0.0

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
if "signal_results"   not in st.session_state: st.session_state.signal_results   = None
if "live_signal_time" not in st.session_state: st.session_state.live_signal_time = None

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("📊 Bot Info")
    st.metric("Weekly Baseline", f"${baseline:,.2f}")
    st.metric("Trade Budget",    f"${MAX_TRADE_USD:,.0f} / trade")
    st.metric("Weekly Target",   f"${TARGET_PROFIT:,.0f}")
    st.divider()

    # Manual liquidation
    if st.button("🧹 Manual Liquidation", use_container_width=True):
        try:
            trading_client.cancel_orders()
            time.sleep(1)
            for p in trading_client.get_all_positions():
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=p.qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                ))
            st.sidebar.success("✅ Liquidation orders sent.")
        except Exception as e:
            st.sidebar.error(f"Error: {e}")

    st.divider()
    st.write("**📋 Bot Logs**")
    for msg in load_bot_logs():
        st.caption(msg)

    st.divider()
    st.write("**Per-stock profiles:**")
    st.dataframe(pd.DataFrame([{
        "Symbol": s, "Hard SL": f"-{v[0]*100:.1f}%",
        "Trail":  f"-{v[1]*100:.1f}%", "Buy Trend": f"+{v[2]*100:.1f}%"}
        for s,v in STOCK_PROFILES.items()]),
        use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────
# TITLE + BOT HEALTH
# ─────────────────────────────────────────────
st.title("📈 Auto Trading Bot Dashboard")

try:
    hb_row = supabase.table("bot_state").select("last_heartbeat").eq("id",1).execute()
    if hb_row.data and hb_row.data[0].get("last_heartbeat"):
        last_hb = datetime.fromisoformat(hb_row.data[0]["last_heartbeat"])
        if last_hb.tzinfo is None: last_hb = SGT.localize(last_hb)
        secs = (datetime.now(SGT) - last_hb).total_seconds()
        if   secs < 60:  st.success(f"🟢 **BOT ALIVE** — Last heartbeat {int(secs)}s ago", icon="🤖")
        elif secs < 300: st.warning(f"🟡 **BOT SLOW** — Last heartbeat {int(secs//60)}m ago", icon="⚠️")
        else:            st.error(f"🔴 **BOT POSSIBLY CRASHED** — No heartbeat for {int(secs//60)} mins! Check Railway.", icon="🚨")
    else:
        st.info("⏳ Waiting for first heartbeat from bot...", icon="🤖")
except Exception:
    st.info("👁️ Dashboard — Bot running on Railway.", icon="🤖")

# ─────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────
tab_live, tab_signals, tab_backtest, tab_portfolio = st.tabs(
    ["🔴 Live Trading", "📡 Signal Scanner", "🧪 Backtesting", "📂 Portfolio Backtest"])

# ════════════════════════════════════════════
# TAB 1 — LIVE TRADING
# ════════════════════════════════════════════
with tab_live:
    st.write(f"## 🎯 Weekly Goal: ${TARGET_PROFIT:.0f} USD")
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Total Equity",      f"${EQUITY:,.2f}", delta=float(combined))
    c2.metric("Cash Balance",      f"${CASH:,.2f}")
    c3.metric("Total in Holdings", f"${total_holdings:,.2f}")
    c4.metric("Realized P&L",      f"${realized:,.2f}")
    c5.metric("Unrealized P&L",    f"${unrealized:,.2f}")
    st.progress(progress_pct, text=f"Weekly Goal: ${realized:.2f} / ${TARGET_PROFIT:.0f} ({int(progress_pct*100)}%)")

    # ── Live Holdings ────────────────────────────────────────────────
    st.write("### 📦 Live Holdings")
    if positions:
        pos_data = []
        for p in positions:
            hard_sl,trail_pct,_ = STOCK_PROFILES.get(p.symbol,(0.013,0.008,0.006))
            entry   = float(p.avg_entry_price)
            current = float(p.current_price)
            pos_data.append({
                "Symbol":     p.symbol,       "Qty":         p.qty,
                "Avg Cost":   f"${entry:.2f}", "Current":    f"${current:.2f}",
                "Trail Stop": f"${entry*(1-trail_pct):.2f}",
                "Hard SL":    f"${entry*(1-hard_sl):.2f}",
                "Value":      f"${float(p.market_value):,.2f}",
                "P&L ($)":    f"${float(p.unrealized_pl):.2f}",
                "P&L (%)":    f"{float(p.unrealized_plpc)*100:+.2f}%",
            })
        st.dataframe(pd.DataFrame(pos_data), use_container_width=True, height=280)
        st.caption(f"Total: **${total_holdings:,.2f}** across {len(positions)} position(s)")
    else:
        st.success("✅ Account is 100% Cash.")

    st.divider()

    # ── Today's Completed Trades ─────────────────────────────────────
    with st.expander("📊 Today's Completed Trades", expanded=True):
        trades_today = load_trades_today()
        if trades_today:
            cols = ["Symbol","Buy Price","Sell Price","Qty","P&L ($)","P&L (%)","Time (SGT)","Reason"]
            st.dataframe(pd.DataFrame(trades_today)[cols], use_container_width=True, hide_index=True)
            total_pl = sum(t["_pl_usd"] for t in trades_today)
            st.write(f"**{'🟢' if total_pl>=0 else '🔴'} Total Realized P&L Today: ${total_pl:+.2f}** ({len(trades_today)} trades)")
        else:
            st.info("No completed trades today yet.")

    # ── Activity Log ─────────────────────────────────────────────────
    with st.expander("📋 Activity Log", expanded=False):
        for msg in load_bot_logs():
            st.text(msg)

    # ── Live Signal Rankings ─────────────────────────────────────────
    st.write("### 📡 Live Signal Rankings")
    if st.button("🔄 Refresh Rankings", key="live_sig_btn"):
        rows=[]; bar=st.progress(0, text="Scanning...")
        for idx,sym in enumerate(WATCHLIST):
            bar.progress(idx/len(WATCHLIST), text=f"Scanning {sym}...")
            try:
                df_ls=yf.download(sym,period="1mo",interval="1h",progress=False)
                if df_ls.empty: continue
                df_ls=df_ls[["Close"]].rename(columns={"Close":"close"})
                sig=compute_signal_score(df_ls)
                rows.append({"Symbol":sym,"Score":sig["score"],"Signal":sig["direction"],
                             "RSI":sig["rsi"],"MACD Hist":sig["macd_hist"]})
            except: pass
        bar.progress(1.0, text="✅ Done!")
        df_live=pd.DataFrame(rows).sort_values("Score",ascending=False).reset_index(drop=True)
        df_live.insert(0,"Rank",range(1,len(df_live)+1))
        st.session_state.signal_results=df_live
        st.session_state.live_signal_time=datetime.now(SGT)

    if st.session_state.signal_results is not None:
        df_d=st.session_state.signal_results
        ts=st.session_state.live_signal_time.strftime("%H:%M:%S SGT") if st.session_state.live_signal_time else "—"
        bull_n=len(df_d[df_d["Score"]>=55]); bear_n=len(df_d[df_d["Score"]<=45])
        st.caption(f"Updated: **{ts}** | 🟢 {bull_n} Bullish | ⚪ {len(df_d)-bull_n-bear_n} Neutral | 🔴 {bear_n} Bearish")
        bc,rc=st.columns(2)
        with bc:
            st.markdown("**🟢 Top Bullish**")
            st.dataframe(df_d.head(8)[["Rank","Symbol","Score","Signal","RSI","MACD Hist"]],use_container_width=True,hide_index=True)
        with rc:
            st.markdown("**🔴 Top Bearish**")
            st.dataframe(df_d.tail(8).sort_values("Score")[["Rank","Symbol","Score","Signal","RSI","MACD Hist"]],use_container_width=True,hide_index=True)
        fig=go.Figure(go.Bar(x=df_d["Symbol"],y=df_d["Score"],
            marker_color=["#26a65b" if s>=55 else("#e74c3c" if s<=45 else"#868e96") for s in df_d["Score"]],
            text=df_d["Score"],textposition="outside"))
        fig.add_hline(y=55,line_dash="dot",line_color="#26a65b",annotation_text="Bullish")
        fig.add_hline(y=45,line_dash="dot",line_color="#e74c3c",annotation_text="Bearish")
        fig.update_layout(height=280,template="plotly_dark",yaxis_range=[0,115],margin=dict(l=0,r=0,t=10,b=0))
        st.plotly_chart(fig,use_container_width=True)
    else:
        st.info("👆 Click **Refresh Rankings** to load signal scores.")

# ════════════════════════════════════════════
# TAB 2 — SIGNAL SCANNER
# ════════════════════════════════════════════
with tab_signals:
    st.write("## 📡 Signal Scanner")
    c1,c2=st.columns([2,1])
    with c1: sig_period=st.selectbox("Data period",["5d","1mo","3mo"],index=1)
    with c2: run_scanner=st.button("🔍 Run Scan",type="primary",use_container_width=True)
    if run_scanner:
        scan_rows=[]; bar=st.progress(0,text="Scanning...")
        for idx,sym in enumerate(WATCHLIST):
            bar.progress(idx/len(WATCHLIST),text=f"Scanning {sym}...")
            try:
                df_sig=yf.download(sym,period=sig_period,interval="1h",progress=False)
                if df_sig.empty: continue
                df_sig=df_sig[["Close"]].rename(columns={"Close":"close"})
                sig=compute_signal_score(df_sig)
                hard_sl,trail_pct,_=STOCK_PROFILES.get(sym,(0.013,0.008,0.006))
                scan_rows.append({"Symbol":sym,"Score":sig["score"],"Signal":sig["direction"],
                    "RSI":sig["rsi"],"MACD Hist":sig["macd_hist"],
                    "Hard SL":f"-{hard_sl*100:.1f}%","Trail":f"-{trail_pct*100:.1f}%"})
            except: pass
        bar.progress(1.0,text="✅ Done!")
        df_scan=pd.DataFrame(scan_rows).sort_values("Score",ascending=False).reset_index(drop=True)
        df_scan.insert(0,"Rank",range(1,len(df_scan)+1))
        st.session_state.signal_results=df_scan
    if st.session_state.signal_results is not None:
        df_s=st.session_state.signal_results
        s1,s2,s3=st.columns(3)
        s1.metric("🟢 Bullish",len(df_s[df_s["Score"]>=55]))
        s2.metric("⚪ Neutral",len(df_s[(df_s["Score"]>45)&(df_s["Score"]<55)]))
        s3.metric("🔴 Bearish",len(df_s[df_s["Score"]<=45]))
        st.dataframe(df_s,use_container_width=True,hide_index=True)
    else:
        st.info("👆 Click **Run Scan** to analyse all watchlist stocks.")

# ════════════════════════════════════════════
# TAB 3 — BACKTESTING
# ════════════════════════════════════════════
with tab_backtest:
    st.write("## 🧪 Backtesting")
    st.info("Simulates the SMA crossover + hard stop + trailing stop strategy on historical hourly data.")

    bt_mode = st.radio("Mode", ["📊 All Stocks (Leaderboard)", "🔍 Single Stock (Deep Dive)"], horizontal=True)
    st.divider()

    cfg1,cfg2,cfg3 = st.columns(3)
    with cfg1: bt_period  = st.selectbox("Period",["1mo","3mo","6mo","1y"],index=1)
    with cfg2: bt_max_usd = st.number_input("Max $ per trade",min_value=100,max_value=10000,value=300,step=100)
    with cfg3: use_profile= st.checkbox("Use per-stock profiles",value=True)

    if not use_profile:
        ov1,ov2,ov3=st.columns(3)
        with ov1: ov_hard_sl=st.slider("Hard Stop %",0.005,0.05,0.013,0.001,format="%.3f")
        with ov2: ov_trail  =st.slider("Trail Stop %",0.005,0.05,0.008,0.001,format="%.3f")
        with ov3: ov_trend  =st.slider("Buy Trend %",0.001,0.02,0.006,0.001,format="%.3f")

    if bt_mode == "🔍 Single Stock (Deep Dive)":
        bt_symbol=st.selectbox("Symbol",WATCHLIST)

    def run_backtest_single(symbol, period, hard_sl, trail_pct, buy_trend, max_usd):
        df=yf.download(symbol,period=period,interval="1h",progress=False)
        if df.empty: return None, None
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        df=df[["Close"]].rename(columns={"Close":"close"}).dropna()
        df["avg20"]=df["close"].rolling(20).mean(); df.dropna(inplace=True)
        cash=10000.0; pos=0.0; entry=0.0; peak=0.0; trades=[]
        for ts,row in df.iterrows():
            price=float(row["close"]); avg=float(row["avg20"])
            if pos>0:
                peak=max(peak,price); pnl=(price-entry)/entry
                gain=(peak-entry)/entry; trail_active=gain>=(trail_pct*0.5)
                if pnl<=-hard_sl or (trail_active and price<=peak*(1-trail_pct)):
                    reason="HARD SL" if pnl<=-hard_sl else "TRAIL STOP"
                    pl_usd=round((price-entry)*pos,2); cash+=price*pos
                    trades.append({"Date":str(ts)[:16],"Action":f"SELL ({reason})","Price":round(price,2),"P&L ($)":pl_usd,"P&L (%)":f"{pnl*100:+.2f}%"})
                    pos=entry=peak=0.0
            elif pos==0 and price>avg*(1+buy_trend) and cash>=max_usd:
                qty=round(max_usd/price,6); cash-=qty*price; pos=qty; entry=price; peak=price
                trades.append({"Date":str(ts)[:16],"Action":"BUY","Price":round(price,2),"P&L ($)":0,"P&L (%)":"0.00%"})
        final=cash+pos*float(df["close"].iloc[-1])
        sells=[t for t in trades if "SELL" in t["Action"]]
        wins=[t for t in sells if t["P&L ($)"]>0]; losses=[t for t in sells if t["P&L ($)"]<=0]
        return {"symbol":symbol,"final":round(final,2),"pl":round(final-10000,2),
                "trades":len(sells),"wins":len(wins),"losses":len(losses),
                "win_rate":round(len(wins)/len(sells)*100,1) if sells else 0,
                "avg_win":round(sum(t["P&L ($)"] for t in wins)/len(wins),2) if wins else 0,
                "avg_loss":round(sum(t["P&L ($)"] for t in losses)/len(losses),2) if losses else 0,
                "df":df}, pd.DataFrame(trades)

    run_bt=st.button("▶️ Run Backtest",type="primary",use_container_width=True)

    if run_bt and bt_mode=="📊 All Stocks (Leaderboard)":
        all_r=[]; bar=st.progress(0,text="Running...")
        for idx,sym in enumerate(WATCHLIST):
            bar.progress(idx/len(WATCHLIST),text=f"Running {sym}...")
            hard_sl,trail,trend=STOCK_PROFILES.get(sym,(0.013,0.008,0.006)) if use_profile else (ov_hard_sl,ov_trail,ov_trend)
            r,_=run_backtest_single(sym,bt_period,hard_sl,trail,trend,bt_max_usd)
            if r: all_r.append(r)
        bar.progress(1.0,text="✅ Done!")
        if all_r:
            all_r_s=sorted(all_r,key=lambda x:x["pl"],reverse=True)
            total_pl=sum(r["pl"] for r in all_r); total_t=sum(r["trades"] for r in all_r)
            overall_wr=round(sum(r["wins"] for r in all_r)/total_t*100,1) if total_t else 0
            h1,h2,h3=st.columns(3)
            h1.metric("Combined P&L",f"${total_pl:+,.2f}")
            h2.metric("Total Trades",total_t)
            h3.metric("Overall Win Rate",f"{overall_wr}%")
            lb=pd.DataFrame([{"Rank":i+1,"Symbol":r["symbol"],"P&L ($)":f"${r['pl']:+,.2f}",
                "Final":f"${r['final']:,.2f}","Trades":r["trades"],
                "Win Rate":f"{r['win_rate']}%","Avg Win":f"${r['avg_win']:+.2f}",
                "Avg Loss":f"${r['avg_loss']:.2f}"} for i,r in enumerate(all_r_s)])
            st.dataframe(lb,use_container_width=True,hide_index=True)
            fig=go.Figure(go.Bar(x=[r["symbol"] for r in all_r_s],y=[r["pl"] for r in all_r_s],
                marker_color=["#26a65b" if r["pl"]>=0 else "#e74c3c" for r in all_r_s],
                text=[f"${r['pl']:+,.0f}" for r in all_r_s],textposition="outside"))
            fig.update_layout(height=320,template="plotly_dark",yaxis_title="P&L ($)",margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig,use_container_width=True)

    elif run_bt and bt_mode=="🔍 Single Stock (Deep Dive)":
        hard_sl,trail,trend=STOCK_PROFILES.get(bt_symbol,(0.013,0.008,0.006)) if use_profile else (ov_hard_sl,ov_trail,ov_trend)
        r,tlog=run_backtest_single(bt_symbol,bt_period,hard_sl,trail,trend,bt_max_usd)
        if r:
            m1,m2,m3,m4,m5=st.columns(5)
            m1.metric("Final Equity",f"${r['final']:,.2f}",delta=f"${r['pl']:+,.2f}")
            m2.metric("Trades",r["trades"]); m3.metric("Win Rate",f"{r['win_rate']}%")
            m4.metric("Avg Win",f"${r['avg_win']:+.2f}"); m5.metric("Avg Loss",f"${r['avg_loss']:.2f}")
            fig=go.Figure()
            fig.add_trace(go.Scatter(x=r["df"].index,y=r["df"]["close"],mode="lines",name="Price",line=dict(color="#4f8ef7",width=1.5)))
            fig.add_trace(go.Scatter(x=r["df"].index,y=r["df"]["avg20"],mode="lines",name="SMA20",line=dict(color="#f0a500",width=1,dash="dot")))
            if tlog is not None and not tlog.empty:
                buys=tlog[tlog["Action"]=="BUY"]; sells=tlog[tlog["Action"].str.contains("SELL")]
                fig.add_trace(go.Scatter(x=pd.to_datetime(buys["Date"]),y=buys["Price"],mode="markers",name="Buy",marker=dict(color="lime",size=9,symbol="triangle-up")))
                fig.add_trace(go.Scatter(x=pd.to_datetime(sells["Date"]),y=sells["Price"],mode="markers",name="Sell",marker=dict(color="red",size=9,symbol="triangle-down")))
            fig.update_layout(height=380,template="plotly_dark",margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig,use_container_width=True)
            with st.expander("📋 Trade Log"):
                if tlog is not None and not tlog.empty: st.dataframe(tlog,use_container_width=True)

    elif not run_bt:
        st.info("👆 Configure settings and click **Run Backtest**.")

# ════════════════════════════════════════════
# TAB 4 — PORTFOLIO BACKTEST
# ════════════════════════════════════════════
with tab_portfolio:
    st.write("## 📂 Portfolio Backtest — Shared Capital Simulation")
    st.info("Simulates all stocks trading simultaneously from one shared capital pool — mirrors how the real bot works.")

    pcfg1,pcfg2,pcfg3=st.columns(3)
    with pcfg1: p_period   =st.selectbox("Period",["1mo","3mo","6mo","1y"],index=1,key="pp")
    with pcfg2: p_capital  =st.number_input("Starting Capital ($)",min_value=1000,max_value=200000,value=100000,step=1000,key="pc")
    with pcfg3: p_max_trade=st.number_input("Max $ per trade",min_value=50,max_value=10000,value=300,step=50,key="pmt")
    p_use_profile=st.checkbox("Use per-stock profiles",value=True,key="pup")
    if not p_use_profile:
        pov1,pov2,pov3=st.columns(3)
        with pov1: p_hard_sl=st.slider("Hard Stop %",0.005,0.05,0.013,0.001,format="%.3f",key="phs")
        with pov2: p_trail  =st.slider("Trail Stop %",0.005,0.05,0.008,0.001,format="%.3f",key="pts")
        with pov3: p_trend  =st.slider("Buy Trend %",0.001,0.02,0.006,0.001,format="%.3f",key="pbt")

    if st.button("▶️ Run Portfolio Backtest",type="primary",use_container_width=True,key="rpb"):
        with st.spinner("📥 Downloading data..."):
            all_data={}
            for sym in WATCHLIST:
                try:
                    df_r=yf.download(sym,period=p_period,interval="1h",progress=False)
                    if df_r.empty: continue
                    if isinstance(df_r.columns,pd.MultiIndex): df_r.columns=df_r.columns.get_level_values(0)
                    df_r=df_r[["Close"]].rename(columns={"Close":"close"}).dropna()
                    df_r["avg20"]=df_r["close"].rolling(20).mean(); df_r.dropna(inplace=True)
                    all_data[sym]=df_r
                except: pass

        if not all_data:
            st.error("No data downloaded.")
        else:
            with st.spinner("⚙️ Simulating..."):
                all_ts=sorted(set(ts for df in all_data.values() for ts in df.index))
                cash=float(p_capital); positions_p={}; trade_log=[]; equity_curve=[]; sym_pl={s:0.0 for s in all_data}
                for ts in all_ts:
                    hv=sum(all_data[s].loc[ts,"close"]*p["qty"] if ts in all_data[s].index else p["entry"]*p["qty"] for s,p in positions_p.items())
                    equity_curve.append({"ts":ts,"equity":round(cash+hv,2),"cash":round(cash,2),"pos":len(positions_p)})
                    to_close=[]
                    for sym,pos in positions_p.items():
                        if ts not in all_data[sym].index: continue
                        price=float(all_data[sym].loc[ts,"close"])
                        hard_sl,trail_pct,_=STOCK_PROFILES.get(sym,(0.013,0.008,0.006)) if p_use_profile else (p_hard_sl,p_trail,p_trend)
                        pos["peak"]=max(pos["peak"],price)
                        pnl=(price-pos["entry"])/pos["entry"]
                        gain=(pos["peak"]-pos["entry"])/pos["entry"]
                        trail_hit=gain>=(trail_pct*0.5) and price<=pos["peak"]*(1-trail_pct)
                        if pnl<=-hard_sl or trail_hit:
                            pl_usd=round((price-pos["entry"])*pos["qty"],4)
                            sym_pl[sym]+=pl_usd; to_close.append((sym,price,pos["qty"],pl_usd,pnl))
                    for sym,price,qty,pl_usd,pnl in to_close:
                        cash+=price*qty; del positions_p[sym]
                        trade_log.append({"Ts":str(ts)[:16],"Symbol":sym,"Action":"SELL","Price":round(price,4),"P&L ($)":pl_usd,"P&L (%)":f"{pnl*100:+.2f}%","Cash":round(cash,2)})
                    for sym in WATCHLIST:
                        if sym in positions_p or sym not in all_data or ts not in all_data[sym].index: continue
                        row=all_data[sym].loc[ts]; price=float(row["close"]); avg=float(row["avg20"]) if not pd.isna(row["avg20"]) else None
                        if avg is None: continue
                        _,_,buy_trend=STOCK_PROFILES.get(sym,(0.013,0.008,0.006)) if p_use_profile else (0,0,p_trend)
                        if price>avg*(1+buy_trend) and cash>=p_max_trade:
                            qty=round(p_max_trade/price,6); cash-=qty*price
                            positions_p[sym]={"qty":qty,"entry":price,"peak":price}
                            trade_log.append({"Ts":str(ts)[:16],"Symbol":sym,"Action":"BUY","Price":round(price,4),"P&L ($)":0,"P&L (%)":"0%","Cash":round(cash,2)})
                for sym,pos in positions_p.items():
                    last=float(all_data[sym]["close"].iloc[-1]) if sym in all_data else pos["entry"]
                    pl=round((last-pos["entry"])*pos["qty"],4); sym_pl[sym]+=pl; cash+=last*pos["qty"]
                final=cash; total_pl=round(final-p_capital,2); total_ret=round(total_pl/p_capital*100,2)
                sells=[t for t in trade_log if t["Action"]=="SELL"]
                wins=[t for t in sells if t["P&L ($)"]>0]; losses=[t for t in sells if t["P&L ($)"]<=0]
                wr=round(len(wins)/len(sells)*100,1) if sells else 0
                eq_vals=[e["equity"] for e in equity_curve]; peak_e=eq_vals[0]; max_dd=0.0
                for eq in eq_vals: peak_e=max(peak_e,eq); max_dd=max(max_dd,(peak_e-eq)/peak_e*100)

            r1,r2,r3,r4,r5=st.columns(5)
            r1.metric("Final Equity",f"${final:,.2f}",delta=f"${total_pl:+,.2f}")
            r2.metric("Total Return",f"{total_ret:+.2f}%")
            r3.metric("Win Rate",f"{wr}%")
            r4.metric("Max Drawdown",f"-{max_dd:.2f}%")
            r5.metric("Total Trades",len(sells))

            df_eq=pd.DataFrame(equity_curve)
            fig_eq=go.Figure(go.Scatter(x=df_eq["ts"],y=df_eq["equity"],mode="lines",
                line=dict(color="#4f8ef7",width=2),fill="tozeroy",fillcolor="rgba(79,142,247,0.08)"))
            fig_eq.add_hline(y=p_capital,line_dash="dot",line_color="gray",annotation_text=f"Start: ${p_capital:,}")
            fig_eq.update_layout(height=350,template="plotly_dark",yaxis_title="Portfolio Value ($)",margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig_eq,use_container_width=True)

            sym_rows=sorted([{"Symbol":s,"Realized P&L ($)":round(v,2),"Result":"🟢 Profit" if v>0 else("🔴 Loss" if v<0 else"⚪ Flat")}
                for s,v in sym_pl.items() if v!=0.0],key=lambda x:x["Realized P&L ($)"],reverse=True)
            if sym_rows: st.dataframe(pd.DataFrame(sym_rows),use_container_width=True,hide_index=True)

            with st.expander("📋 Full Trade Log"):
                if trade_log: st.dataframe(pd.DataFrame(trade_log),use_container_width=True)
    else:
        st.info("👆 Configure settings and click **Run Portfolio Backtest**.")

# ─────────────────────────────────────────────
# AUTO REFRESH
# ─────────────────────────────────────────────
time.sleep(SCAN_INTERVAL)
st.rerun()
