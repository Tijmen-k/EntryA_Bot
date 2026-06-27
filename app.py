"""
Streamlit dashboard for the Entry A Kraken Futures bot.
Run with:  streamlit run app.py
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

import config
from src.data.feed import KrakenFeed
from src.broker.kraken import KrakenBroker

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Entry A Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state ─────────────────────────────────────────────────────────────

if "bot_proc" not in st.session_state:
    st.session_state.bot_proc = None
if "dry_run" not in st.session_state:
    st.session_state.dry_run = config.DRY_RUN

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_bot_state() -> dict:
    path = Path(config.STATE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def bot_is_running() -> bool:
    proc = st.session_state.bot_proc
    return proc is not None and proc.poll() is None


def start_bot(dry_run: bool) -> None:
    if bot_is_running():
        return
    args = [sys.executable, "main.py"]
    if dry_run:
        args.append("--dry-run")
    st.session_state.bot_proc = subprocess.Popen(args)


def stop_bot() -> None:
    proc = st.session_state.bot_proc
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    st.session_state.bot_proc = None


@st.cache_data(ttl=30)
def fetch_market_data():
    feed = KrakenFeed()
    bars = feed.fetch_ohlcv(config.SYMBOL, config.RESOLUTION, count=200)
    price = feed.get_current_price(config.SYMBOL)
    return bars, price


@st.cache_data(ttl=30)
def fetch_account_data():
    try:
        broker = KrakenBroker()
        balance = broker.get_account_balance()
        positions = broker.get_open_positions()
        return balance, positions
    except Exception:
        return 0.0, []


def _bias_label(is_bearish) -> str:
    if is_bearish is None:
        return "⏳ Pending"
    return "🔴 BEARISH (short only)" if is_bearish else "🟢 BULLISH (long only)"


def _tail_log(path: Path, n: int = 60) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Entry A")

    mode_icon = "🟢" if config.TRADING_MODE == "live" else "🟡"
    st.write(f"{mode_icon} **{config.TRADING_MODE.upper()}** mode")
    st.write(f"**Symbol:** `{config.SYMBOL}`")
    st.write(f"**Resolution:** `{config.RESOLUTION}`")
    st.write(f"**Leverage:** `{config.LEVERAGE}x`")
    st.write(f"**Risk/trade:** `{config.RISK_PER_TRADE_PCT*100:.1f}%`")

    st.divider()

    dry_run = st.toggle("Dry Run", value=st.session_state.dry_run)
    st.session_state.dry_run = dry_run

    running = bot_is_running()
    if running:
        st.success("Bot is RUNNING")
        if st.button("Stop Bot", type="secondary", use_container_width=True):
            stop_bot()
            st.rerun()
    else:
        st.error("Bot is STOPPED")
        if st.button("Start Bot", type="primary", use_container_width=True):
            start_bot(st.session_state.dry_run)
            st.rerun()

    st.divider()

    refresh_secs = st.select_slider(
        "Auto-refresh interval",
        options=[15, 30, 60, 120],
        value=30,
        format_func=lambda v: f"{v}s",
    )
    auto_refresh = st.toggle("Auto-refresh", value=True)
    if st.button("Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(f"Updated {datetime.now(tz=timezone.utc).strftime('%H:%M:%S')} UTC")


# ── Load data ─────────────────────────────────────────────────────────────────

bot_state  = load_bot_state()
bars, current_price = fetch_market_data()
balance, open_positions = fetch_account_data()

open_trade  = bot_state.get("open_trade")
daily_pnl   = float(bot_state.get("daily_pnl_pct",  0.0) or 0.0)
weekly_pnl  = float(bot_state.get("weekly_pnl_pct", 0.0) or 0.0)
london_bias = bot_state.get("london_bias_bearish")
ny_bias     = bot_state.get("ny_bias_bearish")
yest_open   = bot_state.get("yesterday_open")
price_09    = bot_state.get("price_at_09")
price_14    = bot_state.get("price_at_14")
prev_day_high = bot_state.get("prev_day_high")
prev_day_low  = bot_state.get("prev_day_low")
date_str    = bot_state.get("date", "—")


# ── Main ──────────────────────────────────────────────────────────────────────

st.title("Entry A — Trading Dashboard")

tab_dash, tab_chart, tab_logs = st.tabs(["Dashboard", "Chart", "Logs"])


# ─── Tab: Dashboard ───────────────────────────────────────────────────────────

with tab_dash:

    # Row 1: key metrics
    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric("Account Balance", f"${balance:,.2f}" if balance else "—")
    c2.metric(
        "Current Price",
        f"${current_price:,.2f}" if current_price else "—",
    )
    daily_pct  = daily_pnl  * 100
    weekly_pct = weekly_pnl * 100
    c3.metric(
        "Daily P&L",
        f"{daily_pct:+.2f}%",
        delta=f"{daily_pct:+.2f}%",
        delta_color="normal",
    )
    c4.metric(
        "Weekly P&L",
        f"{weekly_pct:+.2f}%",
        delta=f"{weekly_pct:+.2f}%",
        delta_color="normal",
    )
    c5.metric("Trading Day", date_str)

    st.divider()

    # Row 2: sessions + open trade
    col_l, col_n, col_t = st.columns(3)

    with col_l:
        st.subheader("London Session")
        st.write(f"**Window:** 09:00 – 14:00 UTC")
        st.write(f"**Bias:** {_bias_label(london_bias)}")
        if yest_open:
            st.write(f"**Yesterday Open:** `{yest_open:.4f}`")
        if price_09:
            st.write(f"**Price @ 09:00:** `{price_09:.4f}`")
        if prev_day_high and prev_day_low:
            pdr = (prev_day_high - prev_day_low) / prev_day_high * 100
            st.write(f"**Prev Day Range:** `{prev_day_low:.4f}` – `{prev_day_high:.4f}` ({pdr:.2f}%)")

    with col_n:
        st.subheader("NY Session")
        st.write(f"**Window:** 14:00 – 23:00 UTC")
        st.write(f"**Bias:** {_bias_label(ny_bias)}")
        if price_09:
            st.write(f"**Price @ 09:00:** `{price_09:.4f}`")
        if price_14:
            st.write(f"**Price @ 14:00:** `{price_14:.4f}`")
        if price_09 and price_14:
            move = (price_14 - price_09) / price_09 * 100
            st.write(f"**09→14 Move:** `{move:+.2f}%`")

    with col_t:
        st.subheader("Open Trade")
        if open_trade:
            sig       = open_trade.get("signal", {})
            direction = sig.get("direction", "—")
            entry     = sig.get("entry_price", 0.0)
            sl        = sig.get("sl_price", 0.0)
            tp        = sig.get("tp_price", 0.0)
            session   = sig.get("session", "—")
            contracts = float(open_trade.get("contracts", 0.0))

            badge = "🔴 SHORT" if direction == "short" else "🟢 LONG"
            st.write(f"**Direction:** {badge}  **Session:** {session}")
            st.write(f"**Contracts:** `{contracts:.4f}` ETH")

            col_e, col_sl, col_tp = st.columns(3)
            col_e.metric("Entry", f"{entry:.4f}")
            col_sl.metric("SL", f"{sl:.4f}")
            col_tp.metric("TP", f"{tp:.4f}")

            if current_price and entry and contracts:
                if direction == "short":
                    upnl_pct = (entry - current_price) / entry * 100 * config.LEVERAGE
                else:
                    upnl_pct = (current_price - entry) / entry * 100 * config.LEVERAGE
                color = "green" if upnl_pct >= 0 else "red"
                st.markdown(f"**Unrealised P&L:** :{color}[{upnl_pct:+.2f}%]")

            orb_h = sig.get("orb_high")
            orb_l = sig.get("orb_low")
            if orb_h and orb_l:
                st.write(f"**ORB:** `{orb_l:.4f}` – `{orb_h:.4f}`")
        else:
            st.info("No open trade")
            if open_positions:
                st.caption(f"Exchange shows {len(open_positions)} position(s) — may be from prior session")

    st.divider()

    # Row 3: risk limits
    st.subheader("Risk Limits")
    rc1, rc2 = st.columns(2)

    with rc1:
        daily_used = abs(min(daily_pnl, 0.0)) / config.MAX_DAILY_LOSS_PCT
        label = (
            f"Daily loss: {abs(min(daily_pct, 0.0)):.2f}% / {config.MAX_DAILY_LOSS_PCT*100:.1f}%"
        )
        st.write(label)
        bar_color = "red" if daily_used >= 1.0 else "normal"
        st.progress(min(daily_used, 1.0))
        if daily_used >= 1.0:
            st.error("Daily loss limit breached — trading halted today")

    with rc2:
        weekly_used = abs(min(weekly_pnl, 0.0)) / config.MAX_WEEKLY_LOSS_PCT
        label = (
            f"Weekly loss: {abs(min(weekly_pct, 0.0)):.2f}% / {config.MAX_WEEKLY_LOSS_PCT*100:.1f}%"
        )
        st.write(label)
        st.progress(min(weekly_used, 1.0))
        if weekly_used >= 1.0:
            st.error("Weekly loss limit breached")


# ─── Tab: Chart ───────────────────────────────────────────────────────────────

with tab_chart:
    st.subheader(f"{config.SYMBOL} — 1m candles (last 100 bars)")

    if bars:
        chart_bars = bars[-100:]
        fig = go.Figure()

        fig.add_trace(go.Candlestick(
            x=[b.timestamp for b in chart_bars],
            open=[b.open  for b in chart_bars],
            high=[b.high  for b in chart_bars],
            low=[b.low   for b in chart_bars],
            close=[b.close for b in chart_bars],
            name=config.SYMBOL,
            increasing_line_color="#00cc96",
            decreasing_line_color="#ef553b",
        ))

        if open_trade:
            sig   = open_trade.get("signal", {})
            orb_h = sig.get("orb_high")
            orb_l = sig.get("orb_low")
            entry = sig.get("entry_price")
            sl    = sig.get("sl_price")
            tp    = sig.get("tp_price")

            if orb_h:
                fig.add_hline(y=orb_h, line_dash="dot", line_color="orange",
                              line_width=1, annotation_text="ORB High",
                              annotation_position="right")
            if orb_l:
                fig.add_hline(y=orb_l, line_dash="dot", line_color="orange",
                              line_width=1, annotation_text="ORB Low",
                              annotation_position="right")
            if entry:
                fig.add_hline(y=entry, line_color="white", line_width=1,
                              annotation_text="Entry", annotation_position="right")
            if sl:
                fig.add_hline(y=sl, line_color="#ef553b", line_width=1,
                              annotation_text="SL", annotation_position="right")
            if tp:
                fig.add_hline(y=tp, line_color="#00cc96", line_width=1,
                              annotation_text="TP", annotation_position="right")

        if current_price:
            fig.add_hline(y=current_price, line_dash="dash", line_color="grey",
                          line_width=1, annotation_text="Last",
                          annotation_position="right")

        fig.update_layout(
            height=550,
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
            margin=dict(l=0, r=80, t=20, b=0),
            legend=dict(orientation="h", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("No candle data — check connectivity or API credentials")


# ─── Tab: Logs ────────────────────────────────────────────────────────────────

with tab_logs:
    log_dir = Path(config.LOG_DIR)

    col_blog, col_tlog = st.columns([3, 1])

    with col_blog:
        st.subheader("Bot Log (last 60 lines)")
        today_log = log_dir / f"bot_{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.log"
        # fall back to newest .log if today's doesn't exist yet
        if not today_log.exists() and log_dir.exists():
            candidates = [p for p in log_dir.glob("bot_*.log") if not p.suffix == ".gz"]
            if candidates:
                today_log = max(candidates, key=lambda p: p.stat().st_mtime)

        content = _tail_log(today_log, 60)
        if content:
            st.code(content, language="text")
        else:
            st.info("No bot log found — bot hasn't run today yet")

    with col_tlog:
        st.subheader("Trades")
        trades_log = log_dir / "trades.log"
        trade_content = _tail_log(trades_log, 30)
        if trade_content:
            st.code(trade_content, language="text")
        else:
            st.info("No trades logged yet")


# ── Auto-refresh ──────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(refresh_secs)
    st.cache_data.clear()
    st.rerun()
