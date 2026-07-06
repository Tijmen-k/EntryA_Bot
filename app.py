"""
Streamlit dashboard for the Entry A Bitget Futures bot.
Run with:  streamlit run app.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, time as _dt_time, date as _date, timedelta as _timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import psutil
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from dotenv import set_key

import importlib
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(override=True)   # refresh env vars from .env before reloading config
import config
importlib.reload(config)      # re-read .env on every Streamlit run so config changes take effect without a full restart
from src.data.feed import BitgetFeed
from src.broker.bitget import BitgetBroker
from src.journal import db as journal
from src.notifications.discord import DiscordNotifier
from src.risk.sizing import (
    SCALING_LADDER, get_current_rung, get_next_rung,
    get_ladder_progress, get_active_position_usdt, boost_streak_info,
)
from src.backtest.simulator import fetch_day_bars, fetch_reference_prices, run_simulation

_notifier = DiscordNotifier(config.DISCORD_WEBHOOK_URL)

# Base currency derived from symbol (e.g. BTCUSDT → BTC, ETHUSD → ETH)
_BASE_CCY: str = config.SYMBOL
for _suffix in ("USDT", "USDC", "USD", "BUSD"):
    if _BASE_CCY.endswith(_suffix):
        _BASE_CCY = _BASE_CCY[: -len(_suffix)]
        break

# Initialise journal DB on every startup (idempotent)
journal.init_db()

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Entry_A_Algo Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Global styling ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .block-container { padding-top: 1.4rem; padding-bottom: 2.5rem; max-width: 1400px; }

    div[data-testid="stMetric"] {
        background: rgba(255,255,255,0.035);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 14px 18px 10px 18px;
    }
    div[data-testid="stMetricLabel"] { font-size: 0.8rem; opacity: 0.75; }
    div[data-testid="stMetricValue"] { font-size: 1.55rem; font-weight: 700; }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 12px;
    }

    .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid rgba(255,255,255,0.08); }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 10px 20px;
        font-weight: 600;
    }
    .stTabs [aria-selected="true"] { background-color: rgba(0,204,150,0.08); }

    #entrya-topbar {
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 14px;
        padding: 14px 20px;
        margin-bottom: 6px;
    }

    div[data-testid="stExpander"] {
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.08);
    }

    h1, h2, h3 { letter-spacing: -0.01em; }
</style>
""", unsafe_allow_html=True)

# ── Session state ──────────────────────────────────────────────────────────────

if "bot_proc"     not in st.session_state: st.session_state.bot_proc     = None
if "dry_run"      not in st.session_state: st.session_state.dry_run      = config.DRY_RUN
if "manual_trade" not in st.session_state: st.session_state.manual_trade = None
if "sim_result"   not in st.session_state: st.session_state.sim_result   = None
if "sim_date"     not in st.session_state: st.session_state.sim_date     = None
if "sim_index"    not in st.session_state: st.session_state.sim_index    = 0
if "sim_playing"  not in st.session_state: st.session_state.sim_playing  = False

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_bot_state() -> dict:
    path = Path(config.STATE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


_PROJECT_DIR = str(Path(__file__).resolve().parent)


def _is_project_bot_process(proc: "psutil.Process") -> bool:
    try:
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    if not any(part.endswith("main.py") for part in cmdline):
        return False
    try:
        return os.path.normcase(proc.cwd()) == os.path.normcase(_PROJECT_DIR)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True  # cmdline matched but cwd unreadable — err on the side of flagging it


def _find_external_bot_pid() -> Optional[int]:
    """
    A `python main.py` process for this project that this Streamlit session didn't
    launch itself. session_state (and any subprocess handle it holds) is wiped every
    time the Streamlit server restarts, so without this check a freshly reloaded
    dashboard would show "STOPPED" and let Start Bot spawn a second, duplicate live
    bot on top of one already running (from a terminal, or an earlier session).
    """
    own_pid = st.session_state.bot_proc.pid if st.session_state.bot_proc else None
    for proc in psutil.process_iter(["pid"]):
        if proc.info["pid"] in (own_pid, os.getpid()):
            continue
        if _is_project_bot_process(proc):
            return proc.info["pid"]
    return None


def bot_is_running() -> bool:
    proc = st.session_state.bot_proc
    if proc is not None and proc.poll() is None:
        return True
    return _find_external_bot_pid() is not None


def start_bot(dry_run: bool) -> None:
    if bot_is_running():
        return
    args = [sys.executable, "main.py"]
    if dry_run:
        args.append("--dry-run")
    st.session_state.bot_proc = subprocess.Popen(args, cwd=_PROJECT_DIR)


def stop_bot() -> None:
    proc = st.session_state.bot_proc
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        st.session_state.bot_proc = None
        return

    # No subprocess handle in this session (e.g. started elsewhere, or a prior
    # session that has since restarted) — fall back to killing it by PID.
    pid = _find_external_bot_pid()
    if pid:
        try:
            ext = psutil.Process(pid)
            ext.terminate()
            ext.wait(timeout=5)
        except psutil.TimeoutExpired:
            ext.kill()
        except psutil.NoSuchProcess:
            pass
    st.session_state.bot_proc = None


@st.cache_data(ttl=30)
def fetch_market_data():
    feed  = BitgetFeed()
    bars  = feed.fetch_ohlcv(config.SYMBOL, config.RESOLUTION, count=200)
    price = feed.get_current_price(config.SYMBOL)
    return bars, price


@st.cache_data(ttl=60)
def fetch_bias_data():
    """Compute London/NY bias directly from bars — works whether bot is running or not."""
    from datetime import timedelta
    feed      = BitgetFeed()
    now_utc   = datetime.now(timezone.utc)
    today     = now_utc.date()
    yesterday = (now_utc - timedelta(days=1)).date()

    # Daily bars to get yesterday's open reliably
    daily = feed.fetch_ohlcv(config.SYMBOL, "1d", count=3)
    yest_open = next((b.open for b in daily if b.timestamp.date() == yesterday), None)
    if yest_open is None:
        yest_open = next((b.open for b in daily if b.timestamp.date() < today), None)

    # ~1000 1-min bars covers up to ~17 h back — enough for 09:00 UTC at any time of day
    intraday = feed.fetch_ohlcv(config.SYMBOL, config.RESOLUTION, count=1000)
    price_09 = next((b.close for b in intraday
                     if b.timestamp.date() == today
                     and b.timestamp.hour == config.SESSIONS[0].orb_start_h
                     and b.timestamp.minute == config.SESSIONS[0].orb_start_m), None)
    price_14 = next((b.close for b in intraday
                     if b.timestamp.date() == today
                     and b.timestamp.hour == config.SESSIONS[1].orb_start_h
                     and b.timestamp.minute == config.SESSIONS[1].orb_start_m), None)

    london_bias = (price_09 < yest_open)  if (price_09 and yest_open) else None
    ny_bias     = (price_14 < price_09)   if (price_14 and price_09)  else None

    return {
        "yest_open":    yest_open,
        "price_09":     price_09,
        "price_14":     price_14,
        "london_bias":  london_bias,
        "ny_bias":      ny_bias,
        "intraday":     intraday,   # reused for ORB computation in strategy tab
    }


@st.cache_data(ttl=30)
def fetch_account_data():
    try:
        broker    = BitgetBroker()
        balance   = broker.get_account_balance()
        positions = broker.get_open_positions()
        return balance, positions
    except Exception:
        return 0.0, []


def _bias_label(is_bearish) -> str:
    if is_bearish is None:
        return "Pending"
    return "BEARISH (short only)" if is_bearish else "BULLISH (long only)"


def _tail_log(path: Path, n: int = 60) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _update_env(key: str, value: str) -> None:
    set_key(".env", key, value)


def _set_bias_override(session_key: str, value) -> None:
    """
    Write a manual bias override directly into state.json for the given session
    ("london" | "ny"). The running bot (a separate process) re-reads just this
    field every tick (~1 min) via BotState/_sync_bias_overrides — value True/False
    forces bearish/bullish, None clears the override back to the auto-computed bias.
    """
    path = Path(config.STATE_FILE)
    path.parent.mkdir(exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
    data[f"{session_key}_bias_override"] = value
    path.write_text(json.dumps(data, indent=2))


def _orb_from_bars(bars, cfg) -> tuple:
    """Return (orb_high, orb_low, t_start, t_end) computed from 1-min bars today."""
    today   = datetime.now(timezone.utc).date()
    t_start = datetime.combine(today, _dt_time(cfg.orb_start_h, cfg.orb_start_m), tzinfo=timezone.utc)
    t_end   = datetime.combine(today, _dt_time(cfg.orb_end_h,   cfg.orb_end_m),   tzinfo=timezone.utc)
    orb_bars = [b for b in bars if t_start <= b.timestamp < t_end]
    if not orb_bars:
        return None, None, t_start, t_end
    return max(b.high for b in orb_bars), min(b.low for b in orb_bars), t_start, t_end


def _phase_from_time(cfg, now_utc, orb_h):
    """Estimate session phase from wall-clock time — works without the bot running."""
    from datetime import timedelta
    today      = now_utc.date()
    orb_start  = datetime.combine(today, _dt_time(cfg.orb_start_h, cfg.orb_start_m), tzinfo=timezone.utc)
    orb_end    = datetime.combine(today, _dt_time(cfg.orb_end_h,   cfg.orb_end_m),   tzinfo=timezone.utc)
    sess_close = datetime.combine(today, _dt_time(cfg.session_close_h, cfg.session_close_m), tzinfo=timezone.utc)
    cutoff     = sess_close - timedelta(minutes=config.NO_ENTRY_BEFORE_CLOSE_MINS)
    if now_utc >= sess_close:
        return "Done"
    if now_utc >= cutoff:
        return "Entry Cutoff"
    if now_utc < orb_start:
        return "Pre-Session"
    if now_utc < orb_end:
        mins_left = int((orb_end - now_utc).total_seconds() / 60)
        return f"Building ORB ({mins_left} min left)"
    if orb_h is None:
        return "No ORB Data"
    return "Waiting for Breakout"   # bot needed to track sweep/confirm


def _active_session_cfg():
    """Return the SessionConfig for the currently active session, or None."""
    h = datetime.now(timezone.utc).hour
    if config.SESSIONS[0].orb_start_h <= h < config.SESSIONS[0].session_close_h:
        return config.SESSIONS[0]
    if config.SESSIONS[1].orb_start_h <= h < config.SESSIONS[1].session_close_h:
        return config.SESSIONS[1]
    return None


# ── Header ─────────────────────────────────────────────────────────────────────

_htitle, _hspacer = st.columns([3, 1])
with _htitle:
    st.markdown("## 📈 Entry A — Trading Dashboard")

with st.container():
    st.markdown('<div id="entrya-topbar">', unsafe_allow_html=True)
    hc1, hc2, hc3, hc4, hc5, hc6 = st.columns([1, 1.6, 1.1, 1.3, 1, 1.3])

    with hc1:
        dry_run = st.toggle("Dry Run", value=st.session_state.dry_run)
        st.session_state.dry_run = dry_run

    running = bot_is_running()
    _is_external = st.session_state.bot_proc is None
    with hc2:
        if running:
            st.success("🟢 Bot RUNNING" + (" (external)" if _is_external else ""))
        else:
            st.error("🔴 Bot STOPPED")

    with hc3:
        if running:
            if st.button("Stop Bot", type="secondary", use_container_width=True, key="topbar_stop_bot"):
                stop_bot()
                st.rerun()
        else:
            if st.button("Start Bot", type="primary", use_container_width=True, key="topbar_start_bot"):
                start_bot(st.session_state.dry_run)
                st.rerun()

    with hc4:
        refresh_secs = st.select_slider(
            "Refresh interval", options=[15, 30, 60, 120], value=30,
            format_func=lambda v: f"{v}s", label_visibility="collapsed",
        )
    with hc5:
        auto_refresh = st.toggle("Auto-refresh", value=True)
    with hc6:
        if st.button("↻ Refresh Now", use_container_width=True, key="topbar_refresh_now"):
            st.cache_data.clear()
            st.rerun()

    st.caption(f"Updated {datetime.now(tz=timezone.utc).strftime('%H:%M:%S')} UTC"
               + ("  ·  Started outside this dashboard session — Stop Bot still works." if running and _is_external else ""))
    st.markdown('</div>', unsafe_allow_html=True)

st.write("")


# ── Load data ──────────────────────────────────────────────────────────────────

bot_state              = load_bot_state()
bars, current_price    = fetch_market_data()
balance, open_positions = fetch_account_data()

# ── Auto-detect when SL/TP fires and clears the position on the exchange ──────
_mt = st.session_state.get("manual_trade")
if _mt:
    _expected_side = _mt["direction"]   # "long" | "short"
    _still_open    = any(
        p.side == _expected_side and p.symbol == config.SYMBOL
        for p in open_positions
    )
    if not _still_open:
        _m_entry  = _mt["entry_price"]
        _m_lev    = _mt.get("leverage", 1)
        _m_size   = _mt.get("size", 0)
        _notional = _mt.get("notional_usdt", 0)
        _sl_price = _mt.get("sl_price")
        _tp_price = _mt.get("tp_price")
        _live     = current_price or _m_entry

        # Fetch actual P&L from Bitget history (most accurate)
        try:
            _broker_hist  = BitgetBroker()
            _pos_data     = _broker_hist.get_closed_position_data(_expected_side)
        except Exception:
            _pos_data = None

        if _pos_data is not None:
            _auto_pnl_usdt = _pos_data["net_pnl"]
            _auto_fees     = _pos_data["fees"]
            _exit_price    = _pos_data["exit_price"] or _live
        else:
            # Fallback: estimate from stored SL/TP prices
            if _expected_side == "long":
                _exit_price = _sl_price if (_live < _m_entry and _sl_price) else (_tp_price or _live)
                _auto_pnl_usdt = (_exit_price - _m_entry) * _m_size
            else:
                _exit_price = _sl_price if (_live > _m_entry and _sl_price) else (_tp_price or _live)
                _auto_pnl_usdt = (_m_entry - _exit_price) * _m_size
            _auto_fees = (_notional or 0) * 0.0006  # ~0.06% taker per side

        _margin = (_notional or 1) / max(_m_lev, 1)
        _auto_pnl_pct = _auto_pnl_usdt / _margin * 100
        journal.close_trade(
            trade_id   = _mt.get("trade_id", "unknown"),
            exit_price = _exit_price,
            pnl_usdt   = _auto_pnl_usdt,
            pnl_pct    = _auto_pnl_pct,
            fees_usdt  = _auto_fees,
        )
        st.session_state.manual_trade = None

open_trade    = bot_state.get("open_trade")
daily_pnl     = journal.get_daily_pnl_usdt()
weekly_pnl    = journal.get_weekly_pnl_usdt()
prev_day_high = bot_state.get("prev_day_high")
prev_day_low  = bot_state.get("prev_day_low")
date_str      = bot_state.get("date", "—")

# Bias / reference prices: prefer state.json (bot-computed), fall back to live computation
_bias_data  = fetch_bias_data()
london_bias = bot_state.get("london_bias_bearish") if bot_state.get("london_bias_bearish") is not None else _bias_data["london_bias"]
ny_bias     = bot_state.get("ny_bias_bearish")     if bot_state.get("ny_bias_bearish")     is not None else _bias_data["ny_bias"]

# Manual test override (set from the Strategy tab) always wins over the computed bias
london_bias_override = bot_state.get("london_bias_override")
ny_bias_override      = bot_state.get("ny_bias_override")
london_bias_effective = london_bias_override if london_bias_override is not None else london_bias
ny_bias_effective      = ny_bias_override     if ny_bias_override     is not None else ny_bias

yest_open   = bot_state.get("yesterday_open")  or _bias_data["yest_open"]
price_09    = bot_state.get("price_at_09")     or _bias_data["price_09"]
price_14    = bot_state.get("price_at_14")     or _bias_data["price_14"]
# Larger bar set for ORB computation in the strategy tab
_all_bars   = _bias_data["intraday"] or bars

# ORB / session state (saved by main.py)
_lon_phase     = bot_state.get("london_phase", "WAITING_FOR_ORB")
_lon_orb_h     = bot_state.get("london_orb_high")
_lon_orb_l     = bot_state.get("london_orb_low")
_lon_brk       = bot_state.get("london_breakout_level")
_ny_phase      = bot_state.get("ny_phase", "WAITING_FOR_ORB")
_ny_orb_h      = bot_state.get("ny_orb_high")
_ny_orb_l      = bot_state.get("ny_orb_low")
_ny_brk        = bot_state.get("ny_breakout_level")


# ── Main ───────────────────────────────────────────────────────────────────────

tab_dash, tab_strategy, tab_chart, tab_sim, tab_journal, tab_admin, tab_logs = st.tabs(
    ["Dashboard", "Strategy", "Chart", "Simulation", "Journal", "Admin", "Logs"]
)


# ─── Tab: Dashboard ────────────────────────────────────────────────────────────

with tab_dash:

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Account Balance", f"${balance:,.2f}" if balance else "—")
    c2.metric("Current Price",   f"${current_price:,.2f}" if current_price else "—")
    c3.metric("Daily P&L",  f"${daily_pnl:+,.2f}",  delta=f"${daily_pnl:+,.2f}",  delta_color="normal")
    c4.metric("Weekly P&L", f"${weekly_pnl:+,.2f}", delta=f"${weekly_pnl:+,.2f}", delta_color="normal")
    c5.metric("Trading Day", date_str)

    st.divider()

    # ── Performance Analytics ──────────────────────────────────────────────────
    _dc = journal.get_closed_trades()
    _ds = journal.get_stats()

    _avg_win   = _ds.get("avg_win",  0.0)
    _avg_loss  = _ds.get("avg_loss", 0.0)   # negative value
    _wr_pct    = _ds.get("win_rate", 0.0)
    _max_dd    = _ds.get("max_drawdown", 0.0)
    _total_pnl = _ds.get("total_pnl", 0.0)
    _payoff    = abs(_avg_win / _avg_loss) if _avg_loss < 0 else 0.0
    _expect    = (_avg_win * _wr_pct / 100) + (_avg_loss * (1 - _wr_pct / 100))
    _recovery  = _total_pnl / _max_dd if _max_dd > 0 else 0.0

    _cur_streak, _streak_dir = 0, ""
    if _dc:
        _sl_type = "win" if (_dc[-1]["pnl_usdt"] or 0) > 0 else "loss"
        for _st in reversed(_dc):
            _is_w = (_st["pnl_usdt"] or 0) > 0
            if (_is_w and _sl_type == "win") or (not _is_w and _sl_type == "loss"):
                _cur_streak += 1
            else:
                break
        _streak_dir = "W" if _sl_type == "win" else "L"

    pa1, pa2, pa3, pa4, pa5, pa6 = st.columns(6)
    pa1.metric("Expectancy",      f"${_expect:+.2f}" if _dc else "—")
    pa2.metric("Payoff Ratio",    f"{_payoff:.2f}x"  if _avg_loss < 0 else "—")
    pa3.metric("Avg Winner",      f"${_avg_win:+.2f}"  if _avg_win  else "—")
    pa4.metric("Avg Loser",       f"${_avg_loss:+.2f}" if _avg_loss else "—")
    pa5.metric("Recovery Factor", f"{_recovery:.2f}"  if _max_dd > 0 else "—")
    pa6.metric("Streak",          f"{_cur_streak}{_streak_dir}" if _cur_streak else "—")

    st.divider()

    # ── Session & Side Breakdown / Recent Form ─────────────────────────────────
    col_ses, col_side, col_recent = st.columns(3)

    with col_ses:
        with st.container(border=True):
            st.markdown("**Session Breakdown**")
            for _sname in ["London", "NY", "Manual"]:
                _st = [t for t in _dc if t.get("session") == _sname]
                _sw = sum(1 for t in _st if (t["pnl_usdt"] or 0) > 0)
                _sp = sum(t["pnl_usdt"] or 0 for t in _st)
                _swr = _sw / len(_st) * 100 if _st else 0
                _color = "#00cc96" if _sp >= 0 else "#ef553b"
                st.markdown(
                    f"**{_sname}** — {len(_st)} trades &nbsp;·&nbsp; {_swr:.0f}% WR &nbsp;·&nbsp; "
                    f"<span style='color:{_color}'>${_sp:+.2f}</span>",
                    unsafe_allow_html=True,
                )

    with col_side:
        with st.container(border=True):
            st.markdown("**Long vs Short**")
            for _side in ["long", "short"]:
                _st = [t for t in _dc if t.get("side") == _side]
                _sw = sum(1 for t in _st if (t["pnl_usdt"] or 0) > 0)
                _sp = sum(t["pnl_usdt"] or 0 for t in _st)
                _swr = _sw / len(_st) * 100 if _st else 0
                _color = "#00cc96" if _sp >= 0 else "#ef553b"
                st.markdown(
                    f"**{_side.upper()}** — {len(_st)} trades &nbsp;·&nbsp; {_swr:.0f}% WR &nbsp;·&nbsp; "
                    f"<span style='color:{_color}'>${_sp:+.2f}</span>",
                    unsafe_allow_html=True,
                )

    with col_recent:
        with st.container(border=True):
            st.markdown("**Last 10 Trades**")
            _recent10 = _dc[-10:] if _dc else []
            if _recent10:
                _dots = " ".join(
                    f"<span style='color:#00cc96;font-size:18px;'>■</span>" if (t["pnl_usdt"] or 0) > 0
                    else f"<span style='color:#ef553b;font-size:18px;'>■</span>"
                    for t in _recent10
                )
                st.markdown(_dots, unsafe_allow_html=True)
                _r10w  = sum(1 for t in _recent10 if (t["pnl_usdt"] or 0) > 0)
                _r10p  = sum(t["pnl_usdt"] or 0 for t in _recent10)
                _r10avg = _r10p / len(_recent10)
                _r10c  = "#00cc96" if _r10p >= 0 else "#ef553b"
                st.markdown(
                    f"WR: **{_r10w/len(_recent10)*100:.0f}%** &nbsp;·&nbsp; "
                    f"P&L: <span style='color:{_r10c}'><b>${_r10p:+.2f}</b></span> &nbsp;·&nbsp; "
                    f"Avg: <span style='color:{_r10c}'><b>${_r10avg:+.2f}</b></span>",
                    unsafe_allow_html=True,
                )
            else:
                st.info("No closed trades yet.")

    st.write("")

    # ── Bot Control & System ──────────────────────────────────────────────────
    with st.expander("⚙️  Bot Control & System", expanded=False):
        st.markdown("**API & Account**")
        _a1, _a2, _a3, _a4 = st.columns(4)
        _a1.metric("Mode",    config.TRADING_MODE.upper())
        _a2.metric("Symbol",  config.SYMBOL)
        _a3.metric("Balance", f"${balance:,.2f} USDT" if balance else "—")
        _a4.metric("Open Positions", len(open_positions))

        _tc1, _tc2 = st.columns(2)
        if _tc1.button("Test API Connection", key="dash_test_api_btn"):
            with st.spinner("Connecting…"):
                try:
                    _test_broker = BitgetBroker()
                    _test_bal    = _test_broker.get_account_balance()
                    st.success(f"Connected. Available balance: **${_test_bal:,.2f} USDT**")
                except Exception as exc:
                    st.error(f"Connection failed: {exc}")

        if _tc2.button("Test Discord Webhook", key="dash_test_discord_btn"):
            if not config.DISCORD_WEBHOOK_URL:
                st.error("DISCORD_WEBHOOK_URL is not set in .env")
            else:
                with st.spinner("Sending test message…"):
                    import requests as _req
                    _r = _req.post(
                        config.DISCORD_WEBHOOK_URL,
                        json={"content": f"✅ **EntryA-Bot** — webhook test from dashboard ({config.SYMBOL} | {config.TRADING_MODE.upper()})"},
                        timeout=10,
                    )
                    if _r.status_code in (200, 204):
                        st.success("Test message sent to Discord.")
                    else:
                        st.error(f"Webhook returned {_r.status_code}: {_r.text[:200]}")

        st.divider()

        st.markdown("**Bot Controls**")
        bc1, bc2, bc3 = st.columns(3)
        _running = bot_is_running()
        bc1.metric("Bot Status", "RUNNING" if _running else "STOPPED")
        bc2.metric("Mode", "DRY RUN" if st.session_state.dry_run else "LIVE")
        _pid_display = st.session_state.bot_proc.pid if st.session_state.bot_proc else _find_external_bot_pid()
        bc3.metric("PID", str(_pid_display) if _running and _pid_display else "—")

        ab1, ab2, ab3 = st.columns(3)
        if ab1.button("Start Bot (Live)", disabled=_running, use_container_width=True, type="primary", key="dash_start_live"):
            start_bot(dry_run=False)
            st.rerun()
        if ab2.button("Start Bot (Dry Run)", disabled=_running, use_container_width=True, key="dash_start_dry"):
            start_bot(dry_run=True)
            st.rerun()
        if ab3.button("Stop Bot", disabled=not _running, use_container_width=True, type="secondary", key="dash_stop_bot"):
            stop_bot()
            st.rerun()

    # ── Live Positions & Orders ───────────────────────────────────────────────
    with st.expander("📋  Live Positions & Orders", expanded=False):
        st.markdown("**Live Positions**")
        if open_positions:
            for pos in open_positions:
                with st.container(border=True):
                    pc1, pc2, pc3, pc4, pc5 = st.columns([2, 2, 2, 2, 1])
                    pc1.write(f"**{pos.symbol}** — {'LONG' if pos.side=='long' else 'SHORT'}")
                    pc2.write(f"Size: `{pos.size:.5f}` {_BASE_CCY}")
                    pc3.write(f"Entry: `{pos.entry_price:,.4f}`")
                    pc4.write(f"uPnL: `{pos.unrealised_pnl:+,.4f}` USDT")
                    if pc5.button("Close", key=f"dash_close_pos_{pos.symbol}_{pos.side}"):
                        _cp_ok = False
                        with st.spinner("Closing…"):
                            try:
                                _cb = BitgetBroker()
                                _cb.close_position(pos)
                                # Cancel any leftover SL/TP trigger orders for this
                                # symbol — Bitget doesn't always auto-cancel them
                                # when a position is closed this way, and since this
                                # bot only ever holds one position at a time, a
                                # blanket cancel here is safe.
                                try:
                                    _cb.cancel_all_orders()
                                except Exception:
                                    pass
                                st.cache_data.clear()
                                _cp_ok = True
                            except Exception as exc:
                                st.error(str(exc))
                        if _cp_ok:
                            st.success("Position closed.")
                            st.rerun()
        else:
            st.info("No open positions on the exchange.")

        st.divider()

        _oh1, _oh2 = st.columns([5, 1])
        _oh1.markdown("**Open Orders**")
        if _oh2.button("Refresh", use_container_width=True, key="dash_refresh_orders"):
            st.cache_data.clear()
            st.rerun()

        try:
            _broker_orders = BitgetBroker()
            open_orders    = _broker_orders.get_open_orders()
        except Exception:
            open_orders = []

        if open_orders:
            for ord_ in open_orders:
                with st.container(border=True):
                    oc1, oc2, oc3, oc4, oc5 = st.columns([2, 2, 2, 2, 1])
                    oc1.write(f"**{ord_.symbol}**")
                    oc2.write(f"{ord_.order_type.upper()} — {ord_.side.upper()}")
                    oc3.write(f"Size: `{ord_.size:.5f}` {_BASE_CCY}")
                    oc4.write(f"Status: `{ord_.status}`")
                    if oc5.button("Cancel", key=f"dash_cancel_ord_{ord_.order_id}"):
                        _co_ok = False
                        with st.spinner("Cancelling…"):
                            try:
                                _cb2 = BitgetBroker()
                                _cb2.cancel_order(ord_.order_id)
                                st.cache_data.clear()
                                _co_ok = True
                            except Exception as exc:
                                st.error(str(exc))
                        if _co_ok:
                            st.success("Order cancelled.")
                            st.rerun()
        else:
            st.info("No open orders.")

    # ── Manual Discord Alerts ─────────────────────────────────────────────────
    with st.expander("🔔  Manual Discord Alerts", expanded=False):
        st.caption("Use these when you enter or exit a trade manually on Bitget.")

        with st.form("discord_manual_entry"):
            st.markdown("**Manual Trade Entry Alert**")
            _mn1, _mn2, _mn3 = st.columns(3)
            _mn_session   = _mn1.selectbox("Session", ["London", "NY"], key="dash_mn_session")
            _mn_direction = _mn2.selectbox("Direction", ["long", "short"], key="dash_mn_direction")
            _mn_entry     = _mn3.number_input("Entry Price", min_value=0.01, value=float(current_price or 0), format="%.2f", key="dash_mn_entry")
            _mn4, _mn5, _mn6 = st.columns(3)
            _mn_sl        = _mn4.number_input("SL Price",    min_value=0.01, value=float(current_price or 0) * 0.99, format="%.2f", key="dash_mn_sl")
            _mn_tp        = _mn5.number_input("TP Price",    min_value=0.01, value=float(current_price or 0) * 1.01, format="%.2f", key="dash_mn_tp")
            _mn_size      = _mn6.number_input("Size (USDT)", min_value=1.0,  value=float(config.ALGO_POSITION_USDT), format="%.0f", key="dash_mn_size")
            if st.form_submit_button("Send Entry Alert", type="primary"):
                _ok = _notifier.trade_entry(
                    session=_mn_session,
                    direction=_mn_direction,
                    entry_price=_mn_entry,
                    sl_price=_mn_sl,
                    tp_price=_mn_tp,
                    contracts=round(_mn_size * config.LEVERAGE / _mn_entry / config.CONTRACT_SIZE) * config.CONTRACT_SIZE,
                    usdt_size=_mn_size,
                    entry_id="manual",
                )
                if _ok:
                    st.success("Entry alert sent to Discord.")
                else:
                    st.error("Discord send failed — check the webhook URL in .env and the app logs.")

        with st.form("discord_manual_close"):
            st.markdown("**Manual Trade Close Alert**")
            _mc1, _mc2, _mc3 = st.columns(3)
            _mc_session   = _mc1.selectbox("Session", ["London", "NY"], key="dash_mc_sess")
            _mc_direction = _mc2.selectbox("Direction", ["long", "short"], key="dash_mc_dir")
            _mc_result    = _mc3.selectbox("Result", ["tp_hit", "sl_hit"], key="dash_mc_result")
            _mc4, _mc5, _mc6 = st.columns(3)
            _mc_entry     = _mc4.number_input("Entry Price", min_value=0.01, value=float(current_price or 0), format="%.2f", key="dash_mc_entry")
            _mc_exit      = _mc5.number_input("Exit Price",  min_value=0.01, value=float(current_price or 0), format="%.2f", key="dash_mc_exit")
            _mc_pnl       = _mc6.number_input("P&L (USDT)",  value=0.0, format="%.2f", key="dash_mc_pnl")
            if st.form_submit_button("Send Close Alert", type="primary"):
                _ok = _notifier.trade_closed(
                    result=_mc_result,
                    session=_mc_session,
                    direction=_mc_direction,
                    entry_price=_mc_entry,
                    exit_price=_mc_exit,
                    pnl_usdt=_mc_pnl,
                    pnl_pct=0.0,
                    daily_pnl_pct=float(daily_pnl or 0),
                )
                if _ok:
                    st.success("Close alert sent to Discord.")
                else:
                    st.error("Discord send failed — check the webhook URL in .env and the app logs.")

    # ── Strategy & Risk Settings ──────────────────────────────────────────────
    with st.expander("🛠️  Strategy & Risk Settings", expanded=False):
        st.markdown("**Risk Parameters**")
        st.caption("Changes update `.env` and take effect on next app restart.")

        with st.form("risk_params"):
            rp1, rp2, rp3, rp4 = st.columns(4)
            new_leverage = rp1.number_input(
                "Leverage", min_value=1, max_value=125,
                value=int(config.LEVERAGE), step=1, key="dash_leverage",
            )
            new_risk = rp2.number_input(
                "Risk per Trade (%)", min_value=0.1, max_value=10.0,
                value=config.RISK_PER_TRADE_PCT * 100, step=0.1, format="%.1f", key="dash_risk_pct",
            )
            new_symbol = rp3.text_input("Symbol", value=config.SYMBOL, key="dash_symbol")
            new_mode   = rp4.selectbox(
                "Trading Mode", ["demo", "live"],
                index=0 if config.TRADING_MODE == "demo" else 1, key="dash_trading_mode",
            )

            if st.form_submit_button("Save Settings", type="primary"):
                _update_env("LEVERAGE",           str(new_leverage))
                _update_env("RISK_PER_TRADE_PCT", str(new_risk / 100))
                _update_env("SYMBOL",             new_symbol)
                _update_env("TRADING_MODE",       new_mode)
                st.success("Settings saved to `.env`. Restart the app to apply.")

        st.divider()

        st.markdown("**Strategy Parameters**")
        st.caption("Position size, SL%, and ORB timers. All times in UTC. Changes saved to `.env` — restart bot to apply.")

        with st.form("strategy_params"):
            _sp1, _sp2, _sp3 = st.columns(3)
            _new_algo_size = _sp1.number_input(
                "Algo Position Size (USDT)", min_value=10.0, max_value=100_000.0,
                value=float(config.ALGO_POSITION_USDT), step=10.0, format="%.0f",
                help="Fixed USDT margin per algo trade (uses configured leverage). Set 0 to use risk-% sizing.",
                key="dash_algo_size",
            )
            _new_london_sl = _sp2.number_input(
                "London SL %", min_value=0.05, max_value=5.0,
                value=config.LONDON_SL_PCT * 100, step=0.05, format="%.2f", key="dash_london_sl",
            )
            _new_ny_sl = _sp3.number_input(
                "NY SL %", min_value=0.05, max_value=5.0,
                value=config.NY_SL_PCT * 100, step=0.05, format="%.2f", key="dash_ny_sl",
            )

            st.markdown("ORB Window — London (UTC)")
            _ol1, _ol2, _ol3, _ol4 = st.columns(4)
            _new_lon_sh = _ol1.number_input("Start Hour",  0, 23, config.SESSIONS[0].orb_start_h, key="dash_lon_sh")
            _new_lon_sm = _ol2.number_input("Start Min",   0, 59, config.SESSIONS[0].orb_start_m, key="dash_lon_sm")
            _new_lon_eh = _ol3.number_input("End Hour",    0, 23, config.SESSIONS[0].orb_end_h,   key="dash_lon_eh")
            _new_lon_em = _ol4.number_input("End Min",     0, 59, config.SESSIONS[0].orb_end_m,   key="dash_lon_em")

            st.markdown("ORB Window — NY (UTC)")
            _on1, _on2, _on3, _on4 = st.columns(4)
            _new_ny_sh  = _on1.number_input("Start Hour",  0, 23, config.SESSIONS[1].orb_start_h, key="dash_ny_sh")
            _new_ny_sm  = _on2.number_input("Start Min",   0, 59, config.SESSIONS[1].orb_start_m, key="dash_ny_sm")
            _new_ny_eh  = _on3.number_input("End Hour",    0, 23, config.SESSIONS[1].orb_end_h,   key="dash_ny_eh")
            _new_ny_em  = _on4.number_input("End Min",     0, 59, config.SESSIONS[1].orb_end_m,   key="dash_ny_em")

            if st.form_submit_button("Save Strategy Settings", type="primary"):
                _update_env("ALGO_POSITION_USDT",  str(int(_new_algo_size)))
                _update_env("LONDON_SL_PCT",       f"{_new_london_sl / 100:.4f}")
                _update_env("NY_SL_PCT",           f"{_new_ny_sl / 100:.4f}")
                _update_env("LONDON_ORB_START_H",  str(_new_lon_sh))
                _update_env("LONDON_ORB_START_M",  str(_new_lon_sm))
                _update_env("LONDON_ORB_END_H",    str(_new_lon_eh))
                _update_env("LONDON_ORB_END_M",    str(_new_lon_em))
                _update_env("NY_ORB_START_H",      str(_new_ny_sh))
                _update_env("NY_ORB_START_M",      str(_new_ny_sm))
                _update_env("NY_ORB_END_H",        str(_new_ny_eh))
                _update_env("NY_ORB_END_M",        str(_new_ny_em))
                st.success("Strategy settings saved. Restart the app and bot to apply.")

    # ── Danger Zone ────────────────────────────────────────────────────────────
    with st.expander("⚠️  Danger Zone", expanded=False):
        st.warning("These actions are irreversible and affect live exchange positions.")

        dz1, dz2 = st.columns(2)

        if dz1.button("Close ALL Positions", type="secondary", use_container_width=True, key="dash_close_all_pos"):
            _dz_ok = False
            with st.spinner("Closing all positions…"):
                try:
                    _dz = BitgetBroker()
                    for pos in _dz.get_open_positions():
                        _dz.close_position(pos)
                    st.cache_data.clear()
                    _dz_ok = True
                except Exception as exc:
                    st.error(str(exc))
            if _dz_ok:
                st.success("All positions closed.")
                st.rerun()

        if dz2.button("Cancel ALL Orders", type="secondary", use_container_width=True, key="dash_cancel_all_orders"):
            _dz2_ok = False
            with st.spinner("Cancelling all orders…"):
                try:
                    _dz2 = BitgetBroker()
                    _dz2.cancel_all_orders()
                    st.cache_data.clear()
                    _dz2_ok = True
                except Exception as exc:
                    st.error(str(exc))
            if _dz2_ok:
                st.success("All orders cancelled.")
                st.rerun()


# ─── Tab: Strategy ─────────────────────────────────────────────────────────────

with tab_strategy:
    st.subheader("Entry A — Strategy Dashboard")

    _now_utc        = datetime.now(timezone.utc)
    _tz_offset_strat = datetime.now().astimezone().utcoffset()
    _act_cfg        = _active_session_cfg()

    # ── Session status row ─────────────────────────────────────────────────────
    _sc1, _sc2, _sc3, _sc4 = st.columns(4)
    _sc1.metric("Active Session", _act_cfg.name if _act_cfg else "None")

    _cur_phase = _phase_from_time(_act_cfg, _now_utc, _lon_orb_h or _ny_orb_h) if _act_cfg else "—"
    _sc2.metric("Phase", _cur_phase)
    _sc3.metric("London Bias", ("Bearish" if london_bias_effective else "Bullish") if london_bias_effective is not None else "—",
                delta="override" if london_bias_override is not None else None, delta_color="off")
    _sc4.metric("NY Bias",     ("Bearish" if ny_bias_effective else "Bullish") if ny_bias_effective is not None else "—",
                delta="override" if ny_bias_override is not None else None, delta_color="off")

    st.divider()

    # ── Manual bias override (testing) ─────────────────────────────────────────
    with st.expander("🧪 Manual bias override — test breakout logic without waiting for the real bias", expanded=False):
        st.caption(
            f"Forces a session's bias regardless of the computed London {config.SESSIONS[0].orb_start_h:02d}:{config.SESSIONS[0].orb_start_m:02d} "
            f"/ NY {config.SESSIONS[1].orb_start_h:02d}:{config.SESSIONS[1].orb_start_m:02d} rule, so you can "
            "watch a live sweep-and-reclaim confirm a trade. Takes effect on the bot's next tick (~1 min). "
            "Resets to Auto automatically at day rollover."
        )
        _bias_options     = {"Auto (computed)": None, "Bullish (long only)": False, "Bearish (short only)": True}
        _bias_options_inv = {v: k for k, v in _bias_options.items()}

        _ov1, _ov2 = st.columns(2)
        with _ov1:
            _sel_lon = st.selectbox("London bias override", list(_bias_options.keys()),
                                     index=list(_bias_options.values()).index(london_bias_override),
                                     key="london_bias_override_select")
            if _bias_options[_sel_lon] != london_bias_override:
                _set_bias_override("london", _bias_options[_sel_lon])
                st.rerun()
        with _ov2:
            _sel_ny = st.selectbox("NY bias override", list(_bias_options.keys()),
                                    index=list(_bias_options.values()).index(ny_bias_override),
                                    key="ny_bias_override_select")
            if _bias_options[_sel_ny] != ny_bias_override:
                _set_bias_override("ny", _bias_options[_sel_ny])
                st.rerun()

    st.divider()

    # ── ORB panels ─────────────────────────────────────────────────────────────
    _oc1, _oc2 = st.columns(2)

    for _orb_cfg, _orb_h_state, _orb_l_state, _orb_brk, _orb_bias, _col in [
        (config.SESSIONS[0], _lon_orb_h, _lon_orb_l, _lon_brk, london_bias_effective, _oc1),
        (config.SESSIONS[1], _ny_orb_h,  _ny_orb_l,  _ny_brk,  ny_bias_effective,     _oc2),
    ]:
        with _col:
            with st.container(border=True):
                st.markdown(f"**{_orb_cfg.name} Session**")
                _orb_t0 = datetime.combine(_now_utc.date(), _dt_time(_orb_cfg.orb_start_h, _orb_cfg.orb_start_m), tzinfo=timezone.utc)
                _orb_t1 = datetime.combine(_now_utc.date(), _dt_time(_orb_cfg.orb_end_h,   _orb_cfg.orb_end_m),   tzinfo=timezone.utc)
                _orb_t0_local = (_orb_t0 + _tz_offset_strat).strftime('%H:%M')
                _orb_t1_local = (_orb_t1 + _tz_offset_strat).strftime('%H:%M')
                st.caption(f"ORB {_orb_t0_local}–{_orb_t1_local} local | SL {_orb_cfg.sl_pct*100:.2f}%")

                # ORB from larger bar set (1000 bars) so it's always computable
                _live_h, _live_l, _, _ = _orb_from_bars(_all_bars, _orb_cfg)
                _h = _orb_h_state or _live_h
                _l = _orb_l_state or _live_l

                if _h and _l:
                    _orb_w_pct = (_h - _l) / _l * 100
                    _o1, _o2, _o3 = st.columns(3)
                    _o1.metric("ORB High", f"{_h:,.2f}")
                    _o2.metric("ORB Low",  f"{_l:,.2f}")
                    _o3.metric("Width",    f"{_orb_w_pct:.3f}%")
                    if _orb_brk:
                        st.caption(f"Breakout level: **{_orb_brk:,.2f}**")
                else:
                    st.info("ORB not built yet for today")

                if _orb_bias is True:
                    bias_txt = "BEARISH (short only)"
                elif _orb_bias is False:
                    bias_txt = "BULLISH (long only)"
                else:
                    _bias_avail_utc   = datetime.combine(_now_utc.date(), _dt_time(_orb_cfg.orb_start_h, _orb_cfg.orb_start_m), tzinfo=timezone.utc)
                    _bias_avail_local = (_bias_avail_utc + _tz_offset_strat).strftime('%H:%M')
                    bias_txt = f"Pending — available after {_bias_avail_local} local"
                st.write(f"Bias: **{bias_txt}**")

                # Phase computed from time — no bot required
                _live_phase = _phase_from_time(_orb_cfg, _now_utc, _h)
                st.write(f"Phase: **{_live_phase}**")

    st.divider()

    # ── Open algo trade ────────────────────────────────────────────────────────
    if open_trade:
        sig = open_trade.get("signal", {})
        _direction = sig.get("direction", "—")
        _entry     = sig.get("entry_price", 0.0)
        _sl        = sig.get("sl_price", 0.0)
        _tp        = sig.get("tp_price", 0.0)
        _orb_h_s   = sig.get("orb_high", 0.0)
        _orb_l_s   = sig.get("orb_low", 0.0)
        _brk_s     = sig.get("breakout_level", 0.0)

        st.markdown(f"### Live Trade — **{_direction.upper()}**  ({sig.get('session','?')})")

        _t1, _t2, _t3, _t4 = st.columns(4)
        _t1.metric("Entry",  f"{_entry:,.2f}")
        _t2.metric("SL",     f"{_sl:,.2f}",   delta=f"{(_sl-_entry)/_entry*100:+.3f}%", delta_color="inverse")
        _t3.metric("TP",     f"{_tp:,.2f}",   delta=f"{(_tp-_entry)/_entry*100:+.3f}%")
        if current_price and _entry:
            _upnl = (current_price - _entry) if _direction == "long" else (_entry - current_price)
            _upnl_pct = _upnl / _entry * 100
            _t4.metric("Live P&L", f"{_upnl_pct:+.3f}%", delta_color="normal")

        if _orb_h_s and _orb_l_s:
            st.write(f"ORB: **{_orb_l_s:,.2f}** – **{_orb_h_s:,.2f}**  |  Breakout at: **{_brk_s:,.2f}**")

        # TP calculation breakdown
        if prev_day_high and prev_day_low and _orb_h_s and _orb_l_s:
            _pdr = (prev_day_high - prev_day_low) / prev_day_high
            _pdr_pct = _pdr * 100
            if _direction == "short":
                _tp_calc = _orb_l_s * (1.0 - _pdr)
                st.caption(f"TP = ORB Low ({_orb_l_s:,.2f}) × (1 − {_pdr_pct:.3f}%) = **{_tp_calc:,.2f}**")
            else:
                _tp_calc = _orb_h_s * (1.0 + _pdr)
                st.caption(f"TP = ORB High ({_orb_h_s:,.2f}) × (1 + {_pdr_pct:.3f}%) = **{_tp_calc:,.2f}**")
            st.caption(f"Prev day range: {prev_day_low:,.2f} – {prev_day_high:,.2f}  ({_pdr_pct:.3f}%)")
    else:
        st.info("No active algo trade. Waiting for signal…")

    st.divider()

    # ── Strategy Conditions ────────────────────────────────────────────────────
    st.markdown("### Entry A — Trading Conditions")

    _cond_col1, _cond_col2 = st.columns(2)

    with _cond_col1:
        st.markdown(f"""
**Step 1 — Build Opening Range (ORB)**
> First 30 minutes of each session. Track the High and Low of every 1-minute bar. The ORB is the range price traded in during this window.

**Step 2 — Determine Session Bias**
> - **London:** Bearish if price at {config.SESSIONS[0].orb_start_h:02d}:{config.SESSIONS[0].orb_start_m:02d} UTC (London's ORB start) < yesterday's daily open
> - **NY:** Bearish if price at {config.SESSIONS[1].orb_start_h:02d}:{config.SESSIONS[1].orb_start_m:02d} UTC (NY's ORB start) < price at London's ORB start
> - Only trades aligned with the bias are taken.

**Step 3 — Wait for Liquidity Sweep (Breakout)**
> - **Bearish bias** → price sweeps **above** ORB High (liquidity grab above old high)
> - **Bullish bias** → price sweeps **below** ORB Low (liquidity grab below old low)
> - Invalidated if price extends **0.3%** beyond ORB boundary in the sweep direction.

**Step 4 — Confirm False Breakout**
> A 1-minute bar must **close back inside** the ORB boundary after the sweep. This confirms the move was a liquidity grab, not a true breakout.
        """)

    with _cond_col2:
        st.markdown("""
**Step 5 — Enter the Trade (Fade)**
> Market order placed just inside the ORB boundary + small buffer.
> - Short (fade the sweep above ORB High)
> - Long (fade the sweep below ORB Low)

**Step 6 — Stop Loss**
> Placed as % from entry (embedded in the order on Bitget):
> - London: **{lon_sl:.2f}%**
> - NY: **{ny_sl:.2f}%**

**Step 7 — Take Profit (Measured Move)**
> TP = ORB boundary ± (previous day's range projected from the ORB level)
> - Short: `TP = ORB Low × (1 − prev_day_range%)`
> - Long: `TP = ORB High × (1 + prev_day_range%)`
{pdr_line}

**Entry Cutoff**
> No new entries within **60 minutes** of session close.
        """.format(
            lon_sl=config.LONDON_SL_PCT * 100,
            ny_sl=config.NY_SL_PCT * 100,
            pdr_line=f"> Prev day range: **{(prev_day_high-prev_day_low)/prev_day_high*100:.3f}%** → TP ~{(prev_day_high-prev_day_low)/prev_day_high*100:.3f}% from ORB level" if prev_day_high and prev_day_low else "",
        ))

    # ── Prev day reference prices ──────────────────────────────────────────────
    if any([yest_open, price_09, price_14, prev_day_high, prev_day_low]):
        st.divider()
        st.markdown("### Reference Prices")
        _rp1, _rp2, _rp3, _rp4, _rp5 = st.columns(5)
        _rp1.metric("Yesterday Open", f"{yest_open:,.2f}" if yest_open else "—")
        _rp2.metric(f"Price @ London ORB ({config.SESSIONS[0].orb_start_h:02d}:{config.SESSIONS[0].orb_start_m:02d})",
                    f"{price_09:,.2f}"  if price_09  else "—")
        _rp3.metric(f"Price @ NY ORB ({config.SESSIONS[1].orb_start_h:02d}:{config.SESSIONS[1].orb_start_m:02d})",
                    f"{price_14:,.2f}"  if price_14  else "—")
        _rp4.metric("Prev Day High",  f"{prev_day_high:,.2f}" if prev_day_high else "—")
        _rp5.metric("Prev Day Low",   f"{prev_day_low:,.2f}"  if prev_day_low  else "—")


# ─── Tab: Chart ────────────────────────────────────────────────────────────────

with tab_chart:
    mt = st.session_state.manual_trade

    _now_chart = datetime.now(timezone.utc)
    st.subheader(f"{config.SYMBOL} — 1m candles")

    if bars:
        # Shift UTC timestamps to local time (auto-detected from OS) so the
        # chart x-axis matches the user's clock without browser conversion.
        _tz_offset = datetime.now().astimezone().utcoffset()
        _today_local = (_now_chart + _tz_offset).date()
        chart_bars = [b for b in _all_bars if (b.timestamp + _tz_offset).date() == _today_local]
        if not chart_bars:
            chart_bars = _all_bars[-5:]   # just after local midnight — show whatever we have
        _ts = [(b.timestamp + _tz_offset).replace(tzinfo=None) for b in chart_bars]
        fig = go.Figure()

        fig.add_trace(go.Candlestick(
            x=_ts,
            open=[b.open  for b in chart_bars],
            high=[b.high  for b in chart_bars],
            low=[b.low   for b in chart_bars],
            close=[b.close for b in chart_bars],
            name=config.SYMBOL,
            increasing_line_color="#00cc96",
            decreasing_line_color="#ef553b",
        ))

        # ── ORB boxes for both sessions ──────────────────────────────────────
        for _sc in config.SESSIONS:
            _oh, _ol, _t0, _t1 = _orb_from_bars(_all_bars, _sc)
            if _oh is None:
                continue
            _label_color = "rgba(255,165,0,0.85)"
            _t0n = (_t0 + _tz_offset).replace(tzinfo=None)
            _t1n = (_t1 + _tz_offset).replace(tzinfo=None)
            # Filled ORB rectangle
            fig.add_shape(
                type="rect", xref="x", yref="y",
                x0=_t0n.isoformat(), x1=_t1n.isoformat(),
                y0=_ol, y1=_oh,
                fillcolor="rgba(255,165,0,0.10)",
                line=dict(color="rgba(255,165,0,0.50)", width=1),
            )
            # ORB H/L lines extending right of the box, stopping at session close
            if _now_chart >= _t1:
                _ny_cfg = config.SESSIONS[1]
                _today  = _now_chart.date()
                # London lines end when NY session opens; NY lines end at session close
                if _sc.name == "London":
                    _line_end_utc = datetime.combine(
                        _today,
                        _dt_time(_ny_cfg.orb_start_h, _ny_cfg.orb_start_m),
                        tzinfo=timezone.utc,
                    )
                else:
                    _line_end_utc = datetime.combine(
                        _today,
                        _dt_time(_sc.session_close_h, _sc.session_close_m),
                        tzinfo=timezone.utc,
                    )
                _line_x0 = _t1n.isoformat()
                _line_x1 = (_line_end_utc + _tz_offset).replace(tzinfo=None).isoformat()
                for _ly, _lbl in [(_oh, f"{_sc.name} ORB H  {_oh:,.2f}"),
                                   (_ol, f"{_sc.name} ORB L  {_ol:,.2f}")]:
                    fig.add_shape(
                        type="line", xref="x", yref="y",
                        x0=_line_x0, x1=_line_x1,
                        y0=_ly, y1=_ly,
                        line=dict(color=_label_color, width=1, dash="dot"),
                    )
                    fig.add_annotation(
                        x=_line_x1, y=_ly, xref="x", yref="y",
                        text=_lbl, showarrow=False,
                        xanchor="left", font=dict(color=_label_color, size=10),
                    )

        # ── Bot algo trade lines ─────────────────────────────────────────────
        if open_trade:
            sig   = open_trade.get("signal", {})
            _bdir = sig.get("direction")
            _bentry = sig.get("entry_price")
            _bsl    = sig.get("sl_price")
            _btp    = sig.get("tp_price")
            _bbrk   = sig.get("breakout_level")

            # Breakout level
            if _bbrk:
                fig.add_hline(y=_bbrk, line_dash="dashdot", line_color="yellow",
                              line_width=1,
                              annotation_text=f"Breakout  {_bbrk:,.2f}",
                              annotation_position="right",
                              annotation_font=dict(color="yellow", size=10))

            # P&L shading
            if current_price and _bentry and _bdir:
                _bin_profit = (_bdir == "long" and current_price > _bentry) or \
                              (_bdir == "short" and current_price < _bentry)
                fig.add_hrect(
                    y0=min(_bentry, current_price),
                    y1=max(_bentry, current_price),
                    fillcolor="rgba(0,204,150,0.08)" if _bin_profit else "rgba(239,85,59,0.08)",
                    line_width=0,
                )

            _bot_entry_color = "#26a69a" if _bdir == "long" else "#ef5350"
            if _bentry:
                fig.add_hline(y=_bentry, line_color=_bot_entry_color, line_width=2,
                              annotation_text=f"Bot {(_bdir or '').upper()}  {_bentry:,.2f}",
                              annotation_position="right",
                              annotation_font=dict(color=_bot_entry_color, size=11))
            if _bsl:
                fig.add_hline(y=_bsl, line_color="#ef553b", line_width=1.5,
                              annotation_text=f"Bot SL  {_bsl:,.2f}",
                              annotation_position="right",
                              annotation_font=dict(color="#ef553b", size=10))
            if _btp:
                fig.add_hline(y=_btp, line_color="#00cc96", line_width=1.5,
                              annotation_text=f"Bot TP  {_btp:,.2f}",
                              annotation_position="right",
                              annotation_font=dict(color="#00cc96", size=10))

        # Manual trade overlay
        if mt:
            m_entry = mt["entry_price"]
            m_sl    = mt["sl_price"]
            m_tp    = mt["tp_price"]
            m_dir   = mt["direction"]
            m_lev   = mt["leverage"]
            is_long = m_dir == "long"

            if current_price:
                in_profit = (is_long and current_price > m_entry) or \
                            (not is_long and current_price < m_entry)
                fill = "rgba(0,204,150,0.13)" if in_profit else "rgba(239,85,59,0.13)"
                fig.add_hrect(
                    y0=min(m_entry, current_price),
                    y1=max(m_entry, current_price),
                    fillcolor=fill, line_width=0,
                )
                mid_y = (m_entry + current_price) / 2
                if is_long:
                    upnl_pct = (current_price - m_entry) / m_entry * m_lev * 100
                else:
                    upnl_pct = (m_entry - current_price) / m_entry * m_lev * 100
                fig.add_annotation(
                    x=chart_bars[-1].timestamp, y=mid_y,
                    text=f"<b>{upnl_pct:+.2f}%</b>", showarrow=False,
                    font=dict(size=13, color="#00cc96" if upnl_pct >= 0 else "#ef553b"),
                    xanchor="right",
                )

            entry_color = "#26a69a" if is_long else "#ef5350"
            dir_label   = "LONG" if is_long else "SHORT"
            sl_dist_pct = abs(m_sl - m_entry) / m_entry * 100
            tp_dist_pct = abs(m_tp - m_entry) / m_entry * 100

            fig.add_hline(y=m_entry, line_color=entry_color, line_dash="solid", line_width=2,
                          annotation_text=f"{dir_label}  {m_entry:.2f}",
                          annotation_position="right",
                          annotation_font=dict(color=entry_color, size=12))
            fig.add_hline(y=m_sl, line_color="#ef5350", line_width=1.5,
                          annotation_text=f"SL  {m_sl:.2f}  (−{sl_dist_pct:.2f}%)",
                          annotation_position="right",
                          annotation_font=dict(color="#ef5350", size=11))
            fig.add_hline(y=m_tp, line_color="#26a69a", line_width=1.5,
                          annotation_text=f"TP  {m_tp:.2f}  (+{tp_dist_pct:.2f}%)",
                          annotation_position="right",
                          annotation_font=dict(color="#26a69a", size=11))

        # ── Closed trades today — entry→exit line with PnL ─────────────────
        try:
            _closed_today = journal.get_closed_trades()
        except Exception:
            _closed_today = []

        for _ct in _closed_today:
            try:
                _ct_entry_t = pd.to_datetime(_ct["entry_time"], utc=True).to_pydatetime()
                _ct_exit_t  = pd.to_datetime(_ct["exit_time"],  utc=True).to_pydatetime()
            except Exception:
                continue
            _ct_entry_local = (_ct_entry_t + _tz_offset).replace(tzinfo=None)
            _ct_exit_local  = (_ct_exit_t  + _tz_offset).replace(tzinfo=None)
            if _ct_entry_local.date() != _today_local:
                continue
            if _ct.get("entry_price") is None or _ct.get("exit_price") is None:
                continue

            _ct_pnl   = _ct.get("pnl_usdt") or 0.0
            _ct_color = "#00cc96" if _ct_pnl >= 0 else "#ef553b"
            fig.add_trace(go.Scatter(
                x=[_ct_entry_local, _ct_exit_local],
                y=[_ct["entry_price"], _ct["exit_price"]],
                mode="lines+markers",
                line=dict(color=_ct_color, width=2, dash="dot"),
                marker=dict(size=9, symbol=["circle", "x"], color=_ct_color),
                name=f"{_ct.get('session','')} {_ct.get('side','').upper()} closed",
                showlegend=False,
                hovertemplate=(
                    f"{_ct.get('side','').upper()} {_ct.get('session','')}<br>"
                    f"Entry {_ct['entry_price']:,.2f} → Exit {_ct['exit_price']:,.2f}<br>"
                    f"PnL ${_ct_pnl:+,.2f}<extra></extra>"
                ),
            ))
            fig.add_annotation(
                x=_ct_exit_local, y=_ct["exit_price"],
                text=f"${_ct_pnl:+,.2f}", showarrow=True, arrowhead=2, ax=0, ay=-25,
                font=dict(color=_ct_color, size=11),
                bgcolor="rgba(0,0,0,0.55)",
            )

        if current_price:
            fig.add_hline(y=current_price, line_dash="dash", line_color="#888",
                          line_width=1, annotation_text=f"Last  {current_price:.2f}",
                          annotation_position="right")

        fig.update_layout(
            height=520, xaxis_rangeslider_visible=False, template="plotly_dark",
            margin=dict(l=0, r=130, t=20, b=30),
            legend=dict(orientation="h", y=1.02),
            xaxis=dict(title=f"Time (UTC{int(_tz_offset.total_seconds()//3600):+d})", type="date"),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("No candle data — check connectivity or API credentials")

    # ── Manual trading panel ───────────────────────────────────────────────────
    st.divider()

    if mt:
        m_dir   = mt["direction"]
        m_entry = mt["entry_price"]
        m_sl    = mt["sl_price"]
        m_tp    = mt["tp_price"]
        m_size  = mt["size"]
        m_lev   = mt["leverage"]
        is_long = m_dir == "long"
        badge   = "LONG" if is_long else "SHORT"

        st.subheader(f"Open Position — {badge}")

        if current_price and m_entry:
            if is_long:
                upnl_pct = (current_price - m_entry) / m_entry * m_lev * 100
            else:
                upnl_pct = (m_entry - current_price) / m_entry * m_lev * 100
            pnl_color = "green" if upnl_pct >= 0 else "red"
            pnl_str   = f"{upnl_pct:+.2f}%"
        else:
            upnl_pct, pnl_str, pnl_color = 0.0, "—", "gray"

        sl_delta = (m_sl - m_entry) / m_entry * 100
        tp_delta = (m_tp - m_entry) / m_entry * 100

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Entry",    f"{m_entry:.4f}")
        c2.metric("SL",       f"{m_sl:.4f}",  delta=f"{sl_delta:+.2f}%", delta_color="inverse")
        c3.metric("TP",       f"{m_tp:.4f}",  delta=f"{tp_delta:+.2f}%")
        _notional = mt.get("notional_usdt") or (m_size * m_entry)
        c4.metric("Size / Lev", f"${_notional:,.0f} · {m_lev}x", delta=f"{m_size:.5f} {_BASE_CCY}", delta_color="off")
        c5.metric("Unrealised P&L", pnl_str)

        st.markdown(f"**Live P&L (×{m_lev} lev): :{pnl_color}[{pnl_str}]**")

        opened_at = mt.get("opened_at", "")
        if opened_at:
            st.caption(f"Opened at {opened_at[:19]} UTC")

        if st.button("Close Position at Market", type="secondary", key="chart_close_position"):
            _close_ok  = False
            _close_err = ""
            with st.spinner("Closing…"):
                try:
                    _b = BitgetBroker()
                    # flash_close uses the dedicated endpoint Bitget UI uses;
                    # Bitget auto-cancels any preset SL/TP when the position closes
                    _hold_side = "long" if is_long else "short"
                    _closed    = _b.flash_close(_hold_side)
                    if not _closed:
                        raise RuntimeError(f"Close rejected: {_b.last_error}")

                    # Fetch actual P&L from Bitget history (most accurate)
                    _pos_data_close = _b.get_closed_position_data(_hold_side)
                    if _pos_data_close is not None:
                        _pnl_usdt  = _pos_data_close["net_pnl"]
                        _fees      = _pos_data_close["fees"]
                        _exit_used = _pos_data_close["exit_price"] or current_price or m_entry
                    else:
                        if is_long:
                            _pnl_usdt = (current_price - m_entry) * m_size
                        else:
                            _pnl_usdt = (m_entry - current_price) * m_size
                        _fees      = (_notional or 0) * 0.0006
                        _exit_used = current_price or m_entry
                    _margin_used = (_notional or 1) / max(m_lev, 1)
                    _pnl_pct = _pnl_usdt / _margin_used * 100
                    journal.close_trade(
                        trade_id   = mt.get("trade_id", "unknown"),
                        exit_price = _exit_used,
                        pnl_usdt   = _pnl_usdt,
                        pnl_pct    = _pnl_pct,
                        fees_usdt  = _fees,
                    )

                    st.session_state.manual_trade = None
                    st.cache_data.clear()
                    _close_ok = True
                except Exception as exc:
                    _close_err = str(exc)

            if _close_ok:
                st.success("Position closed.")
                st.rerun()
            elif _close_err:
                st.error(f"Close failed: {_close_err}")

    else:
        # ── Entry panel ────────────────────────────────────────────────────────
        st.subheader("Place Order")

        col_sz, col_lev, col_sl, col_tp = st.columns(4)
        with col_sz:
            trade_usdt = st.number_input(
                "Size (USDT)", min_value=1.0, max_value=100_000.0,
                value=100.0, step=10.0, format="%.0f",
            )
        with col_lev:
            trade_lev = st.number_input(
                "Leverage", min_value=1, max_value=50,
                value=int(config.LEVERAGE), step=1,
            )
        with col_sl:
            sl_pct_in = st.number_input(
                "Stop Loss %", min_value=0.1, max_value=20.0,
                value=0.7, step=0.1, format="%.1f",
            )
        with col_tp:
            tp_pct_in = st.number_input(
                "Take Profit %", min_value=0.1, max_value=50.0,
                value=1.4, step=0.1, format="%.1f",
            )

        if current_price:
            long_sl  = current_price * (1 - sl_pct_in / 100)
            long_tp  = current_price * (1 + tp_pct_in / 100)
            short_sl = current_price * (1 + sl_pct_in / 100)
            short_tp = current_price * (1 - tp_pct_in / 100)
            # Size = margin × leverage / price
            _preview_btc = trade_usdt * trade_lev / current_price
            p1, p2, p3, p4 = st.columns(4)
            p1.caption(f"Price:  **{current_price:,.2f}**")
            p2.caption(f"Position:  **{_preview_btc:.4f} {_BASE_CCY}** (${trade_usdt*trade_lev:,.0f} notional)")
            p3.caption(f"Long  →  SL {long_sl:,.2f}  |  TP {long_tp:,.2f}")
            p4.caption(f"Short →  SL {short_sl:,.2f}  |  TP {short_tp:,.2f}")

        # Actual BTC = margin × leverage / price, rounded to step size
        _step = config.CONTRACT_SIZE
        if current_price:
            _btc_steps       = max(1, round((trade_usdt * trade_lev / current_price) / _step))
            _actual_btc      = _btc_steps * _step
            _actual_notional = _actual_btc * current_price   # leveraged notional
        else:
            _actual_btc      = _step
            _actual_notional = trade_usdt * trade_lev
        required_margin = _actual_notional / max(trade_lev, 1)  # ≈ trade_usdt

        if balance <= 0:
            st.error("Demo balance is **$0 USDT**. Go to Bitget → Demo Trading → Add Demo Funds.")
        elif balance < required_margin:
            st.warning(
                f"Balance **${balance:,.2f} USDT** is below required margin "
                f"**${required_margin:,.2f} USDT** "
                f"({_actual_btc:.4f} BTC = ${_actual_notional:,.0f} notional at {trade_lev}×)."
            )
        else:
            st.caption(
                f"Balance: **${balance:,.2f} USDT** | "
                f"Position: **{_actual_btc:.4f} BTC** (${_actual_notional:,.0f} notional) | "
                f"Margin: **${required_margin:,.2f} USDT**"
            )

        _insufficient = bool(balance is not None and balance < required_margin)
        b_col, s_col  = st.columns(2)
        buy_clicked   = b_col.button("BUY / LONG",   use_container_width=True, type="primary",
                                     disabled=_insufficient)
        sell_clicked  = s_col.button("SELL / SHORT",  use_container_width=True,
                                     disabled=_insufficient)

        if buy_clicked or sell_clicked:
            if not current_price:
                st.error("No live price — check API connection.")
            else:
                direction  = "long" if buy_clicked else "short"
                is_long    = direction == "long"
                entry_est  = current_price
                # BTC position size = margin × leverage / price
                trade_size = trade_usdt * trade_lev / entry_est
                sl_price   = entry_est * (1 - sl_pct_in / 100) if is_long else entry_est * (1 + sl_pct_in / 100)
                tp_price   = entry_est * (1 + tp_pct_in / 100) if is_long else entry_est * (1 - tp_pct_in / 100)
                entry_side = "buy"  if is_long else "sell"
                exit_side  = "sell" if is_long else "buy"

                _order_ok  = False
                _order_err = ""
                with st.spinner("Placing orders…"):
                    try:
                        _oid = str(int(time.time() * 1000))[-9:]
                        _b   = BitgetBroker()
                        _b.set_leverage(trade_lev)
                        # Embed SL/TP in the entry order — Bitget links them to the
                        # position and closes it automatically when either is triggered
                        entry_id = _b.place_market_order(
                            entry_side, trade_size,
                            client_id = f"me{_oid}",
                            preset_sl = sl_price,
                            preset_tp = tp_price,
                        )
                        if not entry_id:
                            raise RuntimeError(f"Entry order rejected: {_b.last_error}")

                        _now_h   = datetime.now(timezone.utc).hour
                        _session = "London" if 9 <= _now_h < 14 else ("NY" if 14 <= _now_h < 23 else "Manual")

                        st.session_state.manual_trade = {
                            "trade_id":       _oid,
                            "direction":      direction,
                            "entry_price":    entry_est,
                            "size":           trade_size,
                            "notional_usdt":  _actual_notional,
                            "leverage":       trade_lev,
                            "sl_price":       sl_price,
                            "tp_price":       tp_price,
                            "entry_order_id": entry_id,
                            "opened_at":      datetime.now(tz=timezone.utc).isoformat(),
                            "session":        _session,
                        }

                        journal.open_trade(
                            trade_id      = _oid,
                            symbol        = config.SYMBOL,
                            side          = direction,
                            entry_price   = entry_est,
                            size_btc      = trade_size,
                            notional_usdt = _actual_notional,
                            session       = _session,
                            source        = "manual",
                            leverage      = trade_lev,
                        )

                        st.cache_data.clear()
                        _order_ok = True
                    except Exception as exc:
                        _order_err = str(exc)

                if _order_ok:
                    label = "LONG" if is_long else "SHORT"
                    st.success(
                        f"{label} ${trade_usdt:,.0f} USDT (~{trade_size:.5f} {_BASE_CCY}) "
                        f"@ ~{entry_est:,.2f} | SL {sl_price:,.2f} | TP {tp_price:,.2f}"
                    )
                    st.rerun()
                elif _order_err:
                    st.error(f"Order failed: {_order_err}")

    # ── Strategy conditions reference ─────────────────────────────────────────
    st.divider()
    with st.expander("Entry A — Trading Conditions", expanded=False):
        _cc1, _cc2 = st.columns(2)
        with _cc1:
            _lo_cfg, _ny_cfg = config.SESSIONS[0], config.SESSIONS[1]
            st.markdown(f"""
**1 — Build Opening Range (ORB)**
London {_lo_cfg.orb_start_h:02d}:{_lo_cfg.orb_start_m:02d}–{_lo_cfg.orb_end_h:02d}:{_lo_cfg.orb_end_m:02d} UTC &nbsp;|&nbsp; NY {_ny_cfg.orb_start_h:02d}:{_ny_cfg.orb_start_m:02d}–{_ny_cfg.orb_end_h:02d}:{_ny_cfg.orb_end_m:02d} UTC
Track the High and Low of every 1-minute bar in the window.

**2 — Session Bias**
- London: bearish if price@{_lo_cfg.orb_start_h:02d}:{_lo_cfg.orb_start_m:02d} < yesterday's open
- NY: bearish if price@{_ny_cfg.orb_start_h:02d}:{_ny_cfg.orb_start_m:02d} < price@{_lo_cfg.orb_start_h:02d}:{_lo_cfg.orb_start_m:02d}
Only trade in the direction of bias.

**3 — Liquidity Sweep**
- Bearish bias → sweep **above** ORB High
- Bullish bias → sweep **below** ORB Low
- Invalidated if price extends 0.3% beyond boundary.

**4 — Confirm False Breakout**
1-min bar must **close back inside** the ORB.
            """)
        with _cc2:
            st.markdown(f"""
**5 — Enter (Fade)**
Market order just inside the ORB boundary + small buffer.

**6 — Stop Loss**
- London: **{config.LONDON_SL_PCT*100:.2f}%** from entry
- NY: **{config.NY_SL_PCT*100:.2f}%** from entry
(Embedded as preset SL in the Bitget entry order)

**7 — Take Profit (Measured Move)**
- Short: `TP = ORB Low × (1 − prev_day_range%)`
- Long:  `TP = ORB High × (1 + prev_day_range%)`
{f"Prev day range: **{(prev_day_high-prev_day_low)/prev_day_high*100:.3f}%**" if prev_day_high and prev_day_low else "Prev day range: calculating…"}

**8 — Entry Cutoff**
No new entries within 60 min of session close.
            """)


# ─── Tab: Simulation ───────────────────────────────────────────────────────────

with tab_sim:
    st.subheader("Strategy Simulator — Playback & Testing")
    st.caption(
        "Replays Entry A's exact live logic (ORB → breakout → confirmation → entry → SL/TP) "
        "over a historical day, bar by bar, so you can test and watch how a trade would "
        "have played out without touching the exchange."
    )

    _sim_c1, _sim_c2, _sim_c3 = st.columns([2, 1, 1])
    with _sim_c1:
        _sim_date = st.date_input(
            "Day to simulate",
            value=st.session_state.sim_date or datetime.now(timezone.utc).date(),
            max_value=datetime.now(timezone.utc).date(),
            min_value=datetime.now(timezone.utc).date() - _timedelta(days=90),
        )
    with _sim_c2:
        st.write("")
        st.write("")
        _sim_run_clicked = st.button("▶ Run Simulation", type="primary", use_container_width=True)
    with _sim_c3:
        st.write("")
        st.write("")
        _sim_clear_clicked = st.button("Clear", use_container_width=True)

    if _sim_clear_clicked:
        st.session_state.sim_result  = None
        st.session_state.sim_date    = None
        st.session_state.sim_index   = 0
        st.session_state.sim_playing = False
        st.rerun()

    if _sim_run_clicked:
        with st.spinner(f"Fetching {_sim_date} and replaying the strategy…"):
            _sim_feed = BitgetFeed()
            _sim_bars = fetch_day_bars(_sim_feed, _sim_date)
            if not _sim_bars:
                st.error("No candle data returned for that day.")
            else:
                _sim_ref = fetch_reference_prices(_sim_feed, _sim_date)
                st.session_state.sim_result = run_simulation(
                    _sim_date, _sim_bars,
                    _sim_ref["yesterday_open"], _sim_ref["prev_day_high"], _sim_ref["prev_day_low"],
                )
                st.session_state.sim_date    = _sim_date
                st.session_state.sim_index   = 0
                st.session_state.sim_playing = False
                st.rerun()

    _sr = st.session_state.sim_result
    _sim_speed = 5

    if _sr is None:
        st.info("Pick a day and click **Run Simulation** to replay Entry A against real historical data.")
    elif not _sr.snapshots:
        st.warning("No bars available for that day.")
    else:
        _sim_n = len(_sr.snapshots)
        st.session_state.sim_index = min(st.session_state.sim_index, _sim_n - 1)

        _rc1, _rc2, _rc3, _rc4, _rc5 = st.columns(5)
        _rc1.metric("Yesterday Open", f"{_sr.yesterday_open:,.2f}" if _sr.yesterday_open else "—")
        _rc2.metric("Prev Day Range", f"{_sr.prev_day_low:,.0f}–{_sr.prev_day_high:,.0f}" if _sr.prev_day_high else "—")
        _rc3.metric("London Bias", ("BEARISH" if _sr.london_bias else "BULLISH") if _sr.london_bias is not None else "—")
        _rc4.metric("NY Bias",     ("BEARISH" if _sr.ny_bias     else "BULLISH") if _sr.ny_bias     is not None else "—")
        _rc5.metric("Signals Fired", str(len(_sr.trades)))

        st.divider()

        # ── Playback controls ──
        _pc1, _pc2, _pc3, _pc4 = st.columns([1, 1, 1, 4])
        if _pc1.button("⏮ Reset", use_container_width=True):
            st.session_state.sim_index   = 0
            st.session_state.sim_playing = False
            st.rerun()
        if _pc2.button("⏸ Pause" if st.session_state.sim_playing else "▶ Play", use_container_width=True):
            st.session_state.sim_playing = not st.session_state.sim_playing
            st.rerun()
        _sim_speed = _pc3.selectbox(
            "Speed", [1, 5, 15, 60], index=1, format_func=lambda v: f"{v}x",
            label_visibility="collapsed",
        )

        # Advance the index for this frame BEFORE the slider widget is instantiated below —
        # Streamlit forbids mutating a key-bound widget's session_state after it renders.
        if st.session_state.sim_playing:
            if st.session_state.sim_index >= _sim_n - 1:
                st.session_state.sim_playing = False
            else:
                st.session_state.sim_index = min(st.session_state.sim_index + _sim_speed, _sim_n - 1)

        st.slider("Scrub", min_value=0, max_value=_sim_n - 1, key="sim_index", label_visibility="collapsed")

        _tz_offset_sim = datetime.now().astimezone().utcoffset()
        _snap    = _sr.snapshots[st.session_state.sim_index]
        _cur_bar = _snap.bar
        _pc4.markdown(
            f"**{(_cur_bar.timestamp + _tz_offset_sim).strftime('%H:%M:%S')} local**  "
            f"({st.session_state.sim_index + 1} / {_sim_n})"
        )

        # ── Live status panel ──
        _st_col1, _st_col2 = st.columns(2)
        for _lbl, _sess_snap, _col in [("London", _snap.london, _st_col1), ("NY", _snap.ny, _st_col2)]:
            with _col:
                with st.container(border=True):
                    st.markdown(f"**{_lbl} Session**")
                    _bias_txt = (
                        ("BEARISH (short only)" if _sess_snap.bias_bearish else "BULLISH (long only)")
                        if _sess_snap.bias_bearish is not None else "Pending"
                    )
                    st.write(f"Phase: **{_sess_snap.phase.replace('_', ' ').title()}**")
                    st.write(f"Bias: **{_bias_txt}**")
                    if _sess_snap.orb_high and _sess_snap.orb_low:
                        st.write(f"ORB: **{_sess_snap.orb_low:,.2f} – {_sess_snap.orb_high:,.2f}**")
                    if _sess_snap.breakout_level:
                        st.write(f"Breakout level: **{_sess_snap.breakout_level:,.2f}**")

        if _snap.open_trade:
            _sot = _snap.open_trade
            _sot_live_pnl = (
                (_cur_bar.close - _sot["entry_price"]) / _sot["entry_price"]
                if _sot["direction"] == "long" else
                (_sot["entry_price"] - _cur_bar.close) / _sot["entry_price"]
            ) * 100
            st.success(
                f"**Trade Open** — {_sot['session']} {_sot['direction'].upper()}  |  "
                f"Entry {_sot['entry_price']:,.2f}  SL {_sot['sl_price']:,.2f}  TP {_sot['tp_price']:,.2f}  |  "
                f"Live {_cur_bar.close:,.2f}  ({_sot_live_pnl:+.2f}%)"
            )

        # ── Chart, bars up to the scrub point ──
        _sim_bars_so_far = [s.bar for s in _sr.snapshots[: st.session_state.sim_index + 1]]
        _sim_ts = [(b.timestamp + _tz_offset_sim).replace(tzinfo=None) for b in _sim_bars_so_far]

        _sfig = go.Figure()
        _sfig.add_trace(go.Candlestick(
            x=_sim_ts,
            open=[b.open for b in _sim_bars_so_far],
            high=[b.high for b in _sim_bars_so_far],
            low=[b.low for b in _sim_bars_so_far],
            close=[b.close for b in _sim_bars_so_far],
            name=config.SYMBOL,
            increasing_line_color="#00cc96",
            decreasing_line_color="#ef553b",
        ))

        for _sess_snap, _sess_color in [(_snap.london, "rgba(255,165,0,0.7)"), (_snap.ny, "rgba(138,43,226,0.7)")]:
            if _sess_snap.orb_high and _sess_snap.orb_low:
                _sfig.add_hline(y=_sess_snap.orb_high, line_dash="dot", line_color=_sess_color, line_width=1)
                _sfig.add_hline(y=_sess_snap.orb_low,  line_dash="dot", line_color=_sess_color, line_width=1)
            if _sess_snap.breakout_level:
                _sfig.add_hline(y=_sess_snap.breakout_level, line_dash="dashdot", line_color="yellow", line_width=1,
                                 annotation_text=f"Breakout {_sess_snap.breakout_level:,.2f}", annotation_position="right",
                                 annotation_font=dict(color="yellow", size=9))

        if _snap.open_trade:
            _sot = _snap.open_trade
            _sot_color = "#26a69a" if _sot["direction"] == "long" else "#ef5350"
            _sfig.add_hline(y=_sot["entry_price"], line_color=_sot_color, line_width=2,
                             annotation_text=f"Entry {_sot['entry_price']:,.2f}", annotation_position="right",
                             annotation_font=dict(color=_sot_color, size=10))
            _sfig.add_hline(y=_sot["sl_price"], line_color="#ef553b", line_width=1.5,
                             annotation_text=f"SL {_sot['sl_price']:,.2f}", annotation_position="right",
                             annotation_font=dict(color="#ef553b", size=9))
            _sfig.add_hline(y=_sot["tp_price"], line_color="#00cc96", line_width=1.5,
                             annotation_text=f"TP {_sot['tp_price']:,.2f}", annotation_position="right",
                             annotation_font=dict(color="#00cc96", size=9))
            _sot_in_profit = (_sot["direction"] == "long" and _cur_bar.close > _sot["entry_price"]) or \
                             (_sot["direction"] == "short" and _cur_bar.close < _sot["entry_price"])
            _sfig.add_hrect(
                y0=min(_sot["entry_price"], _cur_bar.close), y1=max(_sot["entry_price"], _cur_bar.close),
                fillcolor="rgba(0,204,150,0.08)" if _sot_in_profit else "rgba(239,85,59,0.08)", line_width=0,
            )

        _sfig.update_layout(
            height=520,
            xaxis=dict(title=f"Time (UTC{int(_tz_offset_sim.total_seconds()//3600):+d})", type="date"),
            xaxis_rangeslider_visible=False,
            margin=dict(l=10, r=10, t=10, b=10),
        )
        st.plotly_chart(_sfig, use_container_width=True)

        # ── Event log up to the scrub point ──
        with st.expander("Event log", expanded=False):
            _sim_events = []
            for _s in _sr.snapshots[: st.session_state.sim_index + 1]:
                for _e in _s.events:
                    _t = (_s.bar.timestamp + _tz_offset_sim).strftime("%H:%M")
                    _sim_events.append(f"`{_t}`  {_e}")
            if _sim_events:
                st.markdown("  \n".join(_sim_events[-60:]))
            else:
                st.caption("No phase transitions or signals yet at this point in the day.")

        # ── Trade summary ──
        if _sr.trades:
            st.markdown("#### Signals fired today")
            _sim_rows = [{
                "Session":    _t.session,
                "Direction":  _t.direction.upper(),
                "Entry Time": (_t.entry_time + _tz_offset_sim).strftime("%H:%M"),
                "Entry":      round(_t.entry_price, 2),
                "SL":         round(_t.sl_price, 2),
                "TP":         round(_t.tp_price, 2),
                "Result":     _t.result.replace("_", " ").title(),
                "Exit Time":  (_t.exit_time + _tz_offset_sim).strftime("%H:%M") if _t.exit_time else "—",
                "P&L %":      f"{_t.pnl_pct:+.2f}%" if _t.pnl_pct is not None else "—",
            } for _t in _sr.trades]
            st.dataframe(pd.DataFrame(_sim_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No signals fired on this day (bias may not align with a sweep, or no breakout occurred).")

    # ── Auto-play driver — this frame already rendered at the advanced index above;
    #    just pace and trigger the next frame. Index mutation happens pre-slider, not here.
    if st.session_state.sim_playing:
        time.sleep(0.2)
        st.rerun()


# ─── Tab: Journal ──────────────────────────────────────────────────────────────

with tab_journal:
    st.subheader("Trading Journal")

    all_trades    = journal.get_all_trades()
    closed_trades = journal.get_closed_trades()
    stats         = journal.get_stats()
    boosts        = journal.get_boosts()

    # ── Stats cards ────────────────────────────────────────────────────────────
    s1, s2, s3, s4, s5, s6 = st.columns(6)
    s1.metric("Total Trades",   stats.get("total_trades", 0))
    _wr = stats.get("win_rate", 0.0)
    s2.metric("Win Rate",       f"{_wr:.1f}%")
    _tp = stats.get("total_pnl", 0.0)
    s3.metric("Total P&L",      f"${_tp:+,.2f}", delta=f"${_tp:+,.2f}", delta_color="normal")
    _pf = stats.get("profit_factor", 0.0)
    s4.metric("Profit Factor",  f"{_pf:.2f}" if _pf != float('inf') else "∞")
    _md = stats.get("max_drawdown", 0.0)
    s5.metric("Max Drawdown",   f"${_md:,.2f}")
    _tf = stats.get("total_fees", 0.0)
    s6.metric("Total Fees",     f"${_tf:,.2f}")

    st.divider()

    if closed_trades:
        # ── Charts ─────────────────────────────────────────────────────────────
        ch1, ch2 = st.columns(2)

        with ch1:
            # Equity curve
            cumulative = []
            running = 0.0
            for t in closed_trades:
                running += t.get("pnl_usdt") or 0.0
                cumulative.append(running)
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(
                x=list(range(1, len(cumulative) + 1)),
                y=cumulative,
                mode="lines+markers",
                line=dict(color="#00cc96", width=2),
                fill="tozeroy",
                fillcolor="rgba(0,204,150,0.1)",
                name="Equity",
            ))
            fig_eq.update_layout(
                title="Equity Curve (USDT)", template="plotly_dark",
                height=280, margin=dict(l=10, r=10, t=40, b=20),
                xaxis_title="Trade #", yaxis_title="Cumulative P&L ($)",
            )
            st.plotly_chart(fig_eq, use_container_width=True)

        with ch2:
            # P&L per trade bar
            pnls   = [t.get("pnl_usdt") or 0.0 for t in closed_trades]
            colors = ["#00cc96" if p > 0 else "#ef553b" for p in pnls]
            fig_pnl = go.Figure()
            fig_pnl.add_trace(go.Bar(
                x=list(range(1, len(pnls) + 1)),
                y=pnls,
                marker_color=colors,
                name="P&L",
            ))
            fig_pnl.update_layout(
                title="P&L Per Trade (USDT)", template="plotly_dark",
                height=280, margin=dict(l=10, r=10, t=40, b=20),
                xaxis_title="Trade #", yaxis_title="P&L ($)",
            )
            st.plotly_chart(fig_pnl, use_container_width=True)

        st.divider()

    # ── Filters ────────────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns(3)
    with f1:
        filter_status = st.selectbox("Status", ["All", "Closed", "Open"], index=0)
    with f2:
        filter_side   = st.selectbox("Side",   ["All", "long", "short"],  index=0)
    with f3:
        filter_session = st.selectbox("Session", ["All", "London", "NY", "Manual"], index=0)

    # ── Trade table ────────────────────────────────────────────────────────────
    display = all_trades
    if filter_status  != "All": display = [t for t in display if t["status"] == filter_status.lower()]
    if filter_side    != "All": display = [t for t in display if t["side"]   == filter_side]
    if filter_session != "All": display = [t for t in display if t.get("session","Manual") == filter_session]

    if display:
        df = pd.DataFrame(display)
        # Format columns for display
        show_cols = ["trade_id", "symbol", "side", "entry_time", "exit_time",
                     "entry_price", "exit_price", "size_btc", "notional_usdt",
                     "pnl_usdt", "pnl_pct", "fees_usdt", "leverage",
                     "session", "source", "status", "notes"]
        df = df[[c for c in show_cols if c in df.columns]]
        df.columns = [c.replace("_", " ").title() for c in df.columns]

        # Colour P&L column
        def _color_pnl(val):
            if not isinstance(val, (int, float)):
                return ""
            return "color: #00cc96" if val > 0 else ("color: #ef553b" if val < 0 else "")

        styled = df.style.map(_color_pnl, subset=["Pnl Usdt"] if "Pnl Usdt" in df.columns else [])
        st.dataframe(styled, use_container_width=True, height=350)

        # CSV export
        csv_data = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download as CSV", data=csv_data,
            file_name=f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )
    else:
        st.info("No trades match the current filters.")

    # ── Notes editor ───────────────────────────────────────────────────────────
    if all_trades:
        st.divider()
        st.subheader("Edit Trade Notes")
        trade_options = {
            f"#{t['trade_id']} — {t['side'].upper()} {t['symbol']} @ {t.get('entry_price','?')} ({t['status']})": t["trade_id"]
            for t in all_trades
        }
        selected_label = st.selectbox("Select trade", list(trade_options.keys()))
        selected_id    = trade_options[selected_label]
        selected_trade = next(t for t in all_trades if t["trade_id"] == selected_id)

        note_text = st.text_area("Notes", value=selected_trade.get("notes", ""), height=100)
        col_save, col_del = st.columns([3, 1])
        if col_save.button("Save Notes"):
            journal.update_notes(selected_id, note_text)
            st.success("Notes saved.")
            st.rerun()
        if col_del.button("Delete Trade", type="secondary"):
            journal.delete_trade(selected_id)
            st.warning("Trade deleted from journal.")
            st.rerun()

    # ── Journal Reset ──────────────────────────────────────────────────────────
    st.divider()
    with st.expander("Reset Journal"):
        st.warning("This permanently deletes **all trades and level-up history** from the journal database. This cannot be undone.")
        _confirm = st.text_input("Type RESET to confirm", key="journal_reset_confirm")
        if st.button("Reset Journal", type="primary"):
            if _confirm == "RESET":
                journal.reset_journal()
                st.success("Journal cleared.")
                st.rerun()
            else:
                st.error("Type RESET exactly to confirm.")

    # ── Position Scaling & Boost Tracker ──────────────────────────────────────
    st.divider()
    st.markdown("### Position Scaling & Boost Tracker")

    _eq              = float(balance or 0)
    _cur, _nxt, _pct = get_ladder_progress(_eq)
    _at_max          = _nxt is None
    _streak          = boost_streak_info(closed_trades)
    _boost_on        = _streak["boost_active"]
    _pos_usdt_active = _cur.boosted_usdt if _boost_on else _cur.default_usdt
    _pos_btc         = _pos_usdt_active * config.LEVERAGE / float(current_price) if current_price else 0

    # Level-up banner
    _prev_boost  = boosts[0] if boosts else None
    _show_banner = (
        _prev_boost
        and _prev_boost["new_level"] == _cur.level
        and (_eq - _cur.min_equity) < 100
    )
    if _show_banner:
        _pb      = _prev_boost
        _pct_inc = (_pb["new_position_usdt"] - _pb["old_position_usdt"]) / max(_pb["old_position_usdt"], 1) * 100
        st.markdown(f"""
<div style="background:linear-gradient(270deg,#F1C40F,#E67E22,#F1C40F);
     border-radius:14px; padding:20px; text-align:center; margin-bottom:16px;">
  <div style="font-size:22px; font-weight:800; color:#1a1a1a;">
    Level {_pb['old_level']} → {_pb['new_level']} — Position Boost Unlocked!
  </div>
  <div style="font-size:13px; color:#2a2a2a; margin-top:6px;">
    Default ${_pb['old_position_usdt']:,.0f} → <b>${_pb['new_position_usdt']:,.0f} USDT</b>
    &nbsp;(+{_pct_inc:.0f}%) &nbsp;·&nbsp; from trade #{_pb['trade_count'] + 1}
  </div>
</div>
""", unsafe_allow_html=True)

    _jm1, _jm2, _jm3, _jm4, _jm5, _jm6 = st.columns(6)
    _jm1.metric("Equity",        f"${_eq:,.0f}")
    _jm2.metric("Position Size", f"${_pos_usdt_active:,.0f}")
    _jm3.metric("BTC Size",      f"{_pos_btc:.4f}")
    _jm4.metric("Wins / Last 6", f"{_streak['wins_in_last_6']} / {_streak['trades_in_last_6']}")
    _jm5.metric("2x Win Streak", "Yes" if _streak["last_2_wins"] else "No")
    _jm6.metric("Next Level At", f"${_nxt.min_equity:,.0f}" if _nxt else "MAX")

    st.divider()

    _lc1, _lc2 = st.columns([1, 2])

    with _lc1:
        st.markdown("**Scaling Ladder**")
        _rows = []
        for _r in SCALING_LADDER:
            _is_cur  = _r.level == _cur.level
            _is_next = _nxt and _r.level == _nxt.level
            _is_done = _r.level < _cur.level
            _bg  = "background:#0d2d0d;" if _is_cur else "background:#2d2800;" if _is_next else ""
            _col = "color:#aaa;" if _is_done else "color:#eee;" if _is_cur else "color:#ccc;" if _is_next else "color:#555;"
            _tag = " ◀" if _is_cur else " ←" if _is_next else " ✓" if _is_done else ""
            _rows.append(
                f"<tr style='{_bg}{_col}'>"
                f"<td style='padding:3px 8px;'>Lv {_r.level}</td>"
                f"<td style='padding:3px 8px;'>${_r.min_equity:,.0f}</td>"
                f"<td style='padding:3px 8px;'>${_r.default_usdt:,.0f}</td>"
                f"<td style='padding:3px 8px;color:#E67E22;'>${_r.boosted_usdt:,.0f}</td>"
                f"<td style='padding:3px 6px;font-size:11px;color:#888;'>{_tag}</td>"
                f"</tr>"
            )
        st.markdown(
            "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
            "<thead><tr style='color:#555;border-bottom:1px solid #333;font-size:11px;'>"
            "<th style='padding:3px 8px;text-align:left;'>Lvl</th>"
            "<th style='padding:3px 8px;text-align:left;'>Equity</th>"
            "<th style='padding:3px 8px;text-align:left;'>Default</th>"
            "<th style='padding:3px 8px;text-align:left;color:#E67E22;'>Boosted</th>"
            "<th></th></tr></thead><tbody>"
            + "".join(_rows)
            + "</tbody></table>",
            unsafe_allow_html=True,
        )

    with _lc2:
        st.markdown("**Level-Up History**")
        if boosts:
            _bdf = pd.DataFrame(boosts)
            _bdf["date"] = pd.to_datetime(_bdf["date"]).dt.strftime("%Y-%m-%d %H:%M")
            _bdf["pct_inc"] = (
                (_bdf["new_position_usdt"] - _bdf["old_position_usdt"])
                / _bdf["old_position_usdt"] * 100
            ).map("{:+.0f}%".format)
            _bdf = _bdf.rename(columns={
                "date": "Date", "trade_count": "Trade #",
                "old_level": "From Lv", "new_level": "To Lv",
                "old_position_usdt": "Old ($)", "new_position_usdt": "New ($)",
                "equity": "Equity", "pct_inc": "Delta",
            })
            _bdf["Old ($)"] = _bdf["Old ($)"].map("${:,.0f}".format)
            _bdf["New ($)"] = _bdf["New ($)"].map("${:,.0f}".format)
            _bdf["Equity"]  = _bdf["Equity"].map("${:,.0f}".format)
            st.dataframe(
                _bdf[["Date","Trade #","From Lv","To Lv","Old ($)","New ($)","Equity","Delta"]],
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No level-ups recorded yet.")


# ─── Tab: Admin ────────────────────────────────────────────────────────────────

with tab_admin:
    st.subheader("Admin Dashboard")

    # ── API Status ─────────────────────────────────────────────────────────────
    st.markdown("### API & Account")

    _a1, _a2, _a3, _a4 = st.columns(4)
    _a1.metric("Mode",    config.TRADING_MODE.upper())
    _a2.metric("Symbol",  config.SYMBOL)
    _a3.metric("Balance", f"${balance:,.2f} USDT" if balance else "—")
    _a4.metric("Open Positions", len(open_positions))

    _tc1, _tc2 = st.columns(2)
    if _tc1.button("Test API Connection", key="admin_test_api_btn"):
        with st.spinner("Connecting…"):
            try:
                _test_broker = BitgetBroker()
                _test_bal    = _test_broker.get_account_balance()
                st.success(f"Connected. Available balance: **${_test_bal:,.2f} USDT**")
            except Exception as exc:
                st.error(f"Connection failed: {exc}")

    if _tc2.button("Test Discord Webhook", key="admin_test_discord_btn"):
        if not config.DISCORD_WEBHOOK_URL:
            st.error("DISCORD_WEBHOOK_URL is not set in .env")
        else:
            with st.spinner("Sending test message…"):
                import requests as _req
                _r = _req.post(
                    config.DISCORD_WEBHOOK_URL,
                    json={"content": f"✅ **EntryA-Bot** — webhook test from dashboard ({config.SYMBOL} | {config.TRADING_MODE.upper()})"},
                    timeout=10,
                )
                if _r.status_code in (200, 204):
                    st.success("Test message sent to Discord.")
                else:
                    st.error(f"Webhook returned {_r.status_code}: {_r.text[:200]}")

    st.divider()

    # ── Manual Discord Alerts ──────────────────────────────────────────────────
    st.markdown("### Manual Discord Alerts")
    st.caption("Use these when you enter or exit a trade manually on Bitget.")

    with st.form("discord_manual_entry_admin"):
        st.markdown("**Manual Trade Entry Alert**")
        _mn1, _mn2, _mn3 = st.columns(3)
        _mn_session   = _mn1.selectbox("Session", ["London", "NY"], key="admin_mn_session")
        _mn_direction = _mn2.selectbox("Direction", ["long", "short"], key="admin_mn_direction")
        _mn_entry     = _mn3.number_input("Entry Price", min_value=0.01, value=float(current_price or 0), format="%.2f", key="admin_mn_entry")
        _mn4, _mn5, _mn6 = st.columns(3)
        _mn_sl        = _mn4.number_input("SL Price",    min_value=0.01, value=float(current_price or 0) * 0.99, format="%.2f", key="admin_mn_sl")
        _mn_tp        = _mn5.number_input("TP Price",    min_value=0.01, value=float(current_price or 0) * 1.01, format="%.2f", key="admin_mn_tp")
        _mn_size      = _mn6.number_input("Size (USDT)", min_value=1.0,  value=float(config.ALGO_POSITION_USDT), format="%.0f", key="admin_mn_size")
        if st.form_submit_button("Send Entry Alert", type="primary"):
            _ok = _notifier.trade_entry(
                session=_mn_session,
                direction=_mn_direction,
                entry_price=_mn_entry,
                sl_price=_mn_sl,
                tp_price=_mn_tp,
                contracts=round(_mn_size * config.LEVERAGE / _mn_entry / config.CONTRACT_SIZE) * config.CONTRACT_SIZE,
                usdt_size=_mn_size,
                entry_id="manual",
            )
            if _ok:
                st.success("Entry alert sent to Discord.")
            else:
                st.error("Discord send failed — check the webhook URL in .env and the app logs.")

    with st.form("discord_manual_close_admin"):
        st.markdown("**Manual Trade Close Alert**")
        _mc1, _mc2, _mc3 = st.columns(3)
        _mc_session   = _mc1.selectbox("Session", ["London", "NY"], key="admin_mc_sess")
        _mc_direction = _mc2.selectbox("Direction", ["long", "short"], key="admin_mc_dir")
        _mc_result    = _mc3.selectbox("Result", ["tp_hit", "sl_hit"], key="admin_mc_result")
        _mc4, _mc5, _mc6 = st.columns(3)
        _mc_entry     = _mc4.number_input("Entry Price", min_value=0.01, value=float(current_price or 0), format="%.2f", key="admin_mc_entry")
        _mc_exit      = _mc5.number_input("Exit Price",  min_value=0.01, value=float(current_price or 0), format="%.2f", key="admin_mc_exit")
        _mc_pnl       = _mc6.number_input("P&L (USDT)",  value=0.0, format="%.2f", key="admin_mc_pnl")
        if st.form_submit_button("Send Close Alert", type="primary"):
            _ok = _notifier.trade_closed(
                result=_mc_result,
                session=_mc_session,
                direction=_mc_direction,
                entry_price=_mc_entry,
                exit_price=_mc_exit,
                pnl_usdt=_mc_pnl,
                pnl_pct=0.0,
                daily_pnl_pct=float(daily_pnl or 0),
            )
            if _ok:
                st.success("Close alert sent to Discord.")
            else:
                st.error("Discord send failed — check the webhook URL in .env and the app logs.")

    st.divider()

    # ── Bot Controls ───────────────────────────────────────────────────────────
    st.markdown("### Bot Controls")

    bc1, bc2, bc3 = st.columns(3)
    _running = bot_is_running()
    bc1.metric("Bot Status", "RUNNING" if _running else "STOPPED")
    bc2.metric("Mode", "DRY RUN" if st.session_state.dry_run else "LIVE")
    _pid_display = st.session_state.bot_proc.pid if st.session_state.bot_proc else _find_external_bot_pid()
    bc3.metric("PID", str(_pid_display) if _running and _pid_display else "—")

    ab1, ab2, ab3 = st.columns(3)
    if ab1.button("Start Bot (Live)", disabled=_running, use_container_width=True, type="primary", key="admin_start_live"):
        start_bot(dry_run=False)
        st.rerun()
    if ab2.button("Start Bot (Dry Run)", disabled=_running, use_container_width=True, key="admin_start_dry"):
        start_bot(dry_run=True)
        st.rerun()
    if ab3.button("Stop Bot", disabled=not _running, use_container_width=True, type="secondary", key="admin_stop_bot"):
        stop_bot()
        st.rerun()

    st.divider()

    # ── Risk Parameters ────────────────────────────────────────────────────────
    st.markdown("### Risk Parameters")
    st.caption("Changes update `.env` and take effect on next app restart.")

    with st.form("risk_params_admin"):
        rp1, rp2, rp3, rp4 = st.columns(4)
        new_leverage = rp1.number_input(
            "Leverage", min_value=1, max_value=125,
            value=int(config.LEVERAGE), step=1, key="admin_leverage",
        )
        new_risk = rp2.number_input(
            "Risk per Trade (%)", min_value=0.1, max_value=10.0,
            value=config.RISK_PER_TRADE_PCT * 100, step=0.1, format="%.1f", key="admin_risk_pct",
        )
        new_symbol = rp3.text_input("Symbol", value=config.SYMBOL, key="admin_symbol")
        new_mode   = rp4.selectbox(
            "Trading Mode", ["demo", "live"],
            index=0 if config.TRADING_MODE == "demo" else 1, key="admin_trading_mode",
        )

        if st.form_submit_button("Save Settings", type="primary"):
            _update_env("LEVERAGE",           str(new_leverage))
            _update_env("RISK_PER_TRADE_PCT", str(new_risk / 100))
            _update_env("SYMBOL",             new_symbol)
            _update_env("TRADING_MODE",       new_mode)
            st.success("Settings saved to `.env`. Restart the app to apply.")

    st.divider()

    # ── Strategy Parameters ────────────────────────────────────────────────────
    st.markdown("### Strategy Parameters")
    st.caption("Position size, SL%, and ORB timers. All times in UTC. Changes saved to `.env` — restart bot to apply.")

    with st.form("strategy_params_admin"):
        _sp1, _sp2, _sp3 = st.columns(3)
        _new_algo_size = _sp1.number_input(
            "Algo Position Size (USDT)", min_value=10.0, max_value=100_000.0,
            value=float(config.ALGO_POSITION_USDT), step=10.0, format="%.0f",
            help="Fixed USDT margin per algo trade (uses configured leverage). Set 0 to use risk-% sizing.",
            key="admin_algo_size",
        )
        _new_london_sl = _sp2.number_input(
            "London SL %", min_value=0.05, max_value=5.0,
            value=config.LONDON_SL_PCT * 100, step=0.05, format="%.2f", key="admin_london_sl",
        )
        _new_ny_sl = _sp3.number_input(
            "NY SL %", min_value=0.05, max_value=5.0,
            value=config.NY_SL_PCT * 100, step=0.05, format="%.2f", key="admin_ny_sl",
        )

        st.markdown("**ORB Window — London (UTC)**")
        _ol1, _ol2, _ol3, _ol4 = st.columns(4)
        _new_lon_sh = _ol1.number_input("Start Hour",  0, 23, config.SESSIONS[0].orb_start_h, key="admin_lon_sh")
        _new_lon_sm = _ol2.number_input("Start Min",   0, 59, config.SESSIONS[0].orb_start_m, key="admin_lon_sm")
        _new_lon_eh = _ol3.number_input("End Hour",    0, 23, config.SESSIONS[0].orb_end_h,   key="admin_lon_eh")
        _new_lon_em = _ol4.number_input("End Min",     0, 59, config.SESSIONS[0].orb_end_m,   key="admin_lon_em")

        st.markdown("**ORB Window — NY (UTC)**")
        _on1, _on2, _on3, _on4 = st.columns(4)
        _new_ny_sh  = _on1.number_input("Start Hour",  0, 23, config.SESSIONS[1].orb_start_h, key="admin_ny_sh")
        _new_ny_sm  = _on2.number_input("Start Min",   0, 59, config.SESSIONS[1].orb_start_m, key="admin_ny_sm")
        _new_ny_eh  = _on3.number_input("End Hour",    0, 23, config.SESSIONS[1].orb_end_h,   key="admin_ny_eh")
        _new_ny_em  = _on4.number_input("End Min",     0, 59, config.SESSIONS[1].orb_end_m,   key="admin_ny_em")

        if st.form_submit_button("Save Strategy Settings", type="primary"):
            _update_env("ALGO_POSITION_USDT",  str(int(_new_algo_size)))
            _update_env("LONDON_SL_PCT",       f"{_new_london_sl / 100:.4f}")
            _update_env("NY_SL_PCT",           f"{_new_ny_sl / 100:.4f}")
            _update_env("LONDON_ORB_START_H",  str(_new_lon_sh))
            _update_env("LONDON_ORB_START_M",  str(_new_lon_sm))
            _update_env("LONDON_ORB_END_H",    str(_new_lon_eh))
            _update_env("LONDON_ORB_END_M",    str(_new_lon_em))
            _update_env("NY_ORB_START_H",      str(_new_ny_sh))
            _update_env("NY_ORB_START_M",      str(_new_ny_sm))
            _update_env("NY_ORB_END_H",        str(_new_ny_eh))
            _update_env("NY_ORB_END_M",        str(_new_ny_em))
            st.success("Strategy settings saved. Restart the app and bot to apply.")

    st.divider()

    # ── Positions & Orders ─────────────────────────────────────────────────────
    st.markdown("### Live Positions")

    if open_positions:
        for pos in open_positions:
            with st.container(border=True):
                pc1, pc2, pc3, pc4, pc5 = st.columns([2, 2, 2, 2, 1])
                pc1.write(f"**{pos.symbol}** — {'LONG' if pos.side=='long' else 'SHORT'}")
                pc2.write(f"Size: `{pos.size:.5f}` {_BASE_CCY}")
                pc3.write(f"Entry: `{pos.entry_price:,.4f}`")
                pc4.write(f"uPnL: `{pos.unrealised_pnl:+,.4f}` USDT")
                if pc5.button("Close", key=f"admin_close_pos_{pos.symbol}_{pos.side}"):
                    _cp_ok = False
                    with st.spinner("Closing…"):
                        try:
                            _cb = BitgetBroker()
                            _cb.close_position(pos)
                            try:
                                _cb.cancel_all_orders()
                            except Exception:
                                pass
                            st.cache_data.clear()
                            _cp_ok = True
                        except Exception as exc:
                            st.error(str(exc))
                    if _cp_ok:
                        st.success("Position closed.")
                        st.rerun()
    else:
        st.info("No open positions on the exchange.")

    st.divider()

    # ── Open Orders ────────────────────────────────────────────────────────────
    st.markdown("### Open Orders")
    if st.button("Refresh Orders", key="admin_refresh_orders"):
        st.cache_data.clear()
        st.rerun()

    try:
        _broker_orders = BitgetBroker()
        open_orders    = _broker_orders.get_open_orders()
    except Exception:
        open_orders = []

    if open_orders:
        for ord_ in open_orders:
            with st.container(border=True):
                oc1, oc2, oc3, oc4, oc5 = st.columns([2, 2, 2, 2, 1])
                oc1.write(f"**{ord_.symbol}**")
                oc2.write(f"{ord_.order_type.upper()} — {ord_.side.upper()}")
                oc3.write(f"Size: `{ord_.size:.5f}` {_BASE_CCY}")
                oc4.write(f"Status: `{ord_.status}`")
                if oc5.button("Cancel", key=f"admin_cancel_ord_{ord_.order_id}"):
                    _co_ok = False
                    with st.spinner("Cancelling…"):
                        try:
                            _cb2 = BitgetBroker()
                            _cb2.cancel_order(ord_.order_id)
                            st.cache_data.clear()
                            _co_ok = True
                        except Exception as exc:
                            st.error(str(exc))
                    if _co_ok:
                        st.success("Order cancelled.")
                        st.rerun()
    else:
        st.info("No open orders.")

    st.divider()

    # ── Danger Zone ────────────────────────────────────────────────────────────
    st.markdown("### Danger Zone")
    st.warning("These actions are irreversible and affect live exchange positions.")

    dz1, dz2 = st.columns(2)

    if dz1.button("Close ALL Positions", type="secondary", use_container_width=True, key="admin_close_all_pos"):
        _dz_ok = False
        with st.spinner("Closing all positions…"):
            try:
                _dz = BitgetBroker()
                for pos in _dz.get_open_positions():
                    _dz.close_position(pos)
                st.cache_data.clear()
                _dz_ok = True
            except Exception as exc:
                st.error(str(exc))
        if _dz_ok:
            st.success("All positions closed.")
            st.rerun()

    if dz2.button("Cancel ALL Orders", type="secondary", use_container_width=True, key="admin_cancel_all_orders"):
        _dz2_ok = False
        with st.spinner("Cancelling all orders…"):
            try:
                _dz2 = BitgetBroker()
                _dz2.cancel_all_orders()
                st.cache_data.clear()
                _dz2_ok = True
            except Exception as exc:
                st.error(str(exc))
        if _dz2_ok:
            st.success("All orders cancelled.")
            st.rerun()


# ─── Tab: Logs ─────────────────────────────────────────────────────────────────

with tab_logs:
    log_dir = Path(config.LOG_DIR)

    col_blog, col_tlog = st.columns([3, 1])

    with col_blog:
        st.subheader("Bot Log (last 60 lines)")
        today_log = log_dir / f"bot_{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.log"
        if not today_log.exists() and log_dir.exists():
            candidates = [p for p in log_dir.glob("bot_*.log")]
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


# ── Auto-refresh ───────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(refresh_secs)
    st.cache_data.clear()
    st.rerun()