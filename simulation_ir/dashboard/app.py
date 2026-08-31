from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

DASHBOARD_DIR = Path(__file__).resolve().parent
SIMULATION_DIR = DASHBOARD_DIR.parent
ROOT_DIR = SIMULATION_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
STATE_FILE = SIMULATION_DIR / "deltax_event_iran_v2_state.json"
LOG_FILE = SIMULATION_DIR / "deltax_event_iran_v2_scheduler.log"

NY = ZoneInfo("America/New_York")
RIGA = ZoneInfo("Europe/Riga")

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"

LONG_SYMBOLS = ["APO", "APP", "BAC", "BX", "FFIV", "LYB", "WDAY", "WFC", "XYZ"]
SHORT_SYMBOLS = ["COHR", "F", "LITE", "LRCX", "MAS", "TEL"]
OPTION_FIRST = {"WFC", "BX", "BAC", "APP", "XYZ", "WDAY", "LRCX", "F", "LITE", "COHR"}

GAP_THRESHOLD = 0.50
REVERSAL_THRESHOLD = 0.25
DECISION_TIME_ET = "09:40"
EXIT_TIME_ET = "15:50"
STARTING_EQUITY = 100000.0

st.set_page_config(
    page_title="DELTAX Event Based Playbook",
    page_icon="⚡",
    layout="wide",
)

st.markdown(
    '''
    <style>
    .stApp {
        background:
            radial-gradient(circle at 15% 10%, rgba(116, 70, 180, 0.20), transparent 28%),
            radial-gradient(circle at 85% 0%, rgba(78, 42, 130, 0.16), transparent 24%),
            #140c22;
        color: #f6f2ff;
    }

    [data-testid="stHeader"] {
        background: rgba(20, 12, 34, 0.92);
    }

    [data-testid="stSidebar"] {
        background: #1b1030;
    }

    [data-testid="stMetric"] {
        background: #21143a;
        border: 1px solid #3a245f;
        border-radius: 14px;
        padding: 14px;
    }

    [data-testid="stMetricLabel"],
    [data-testid="stMetricValue"],
    [data-testid="stMetricDelta"] {
        color: #f6f2ff !important;
    }

    div[data-testid="stDataFrame"] {
        background: #1b1030;
        border: 1px solid #3a245f;
        border-radius: 14px;
        padding: 6px;
    }

    .stAlert {
        background: #21143a;
        border: 1px solid #4f3378;
        color: #f6f2ff;
    }

    h1, h2, h3 {
        color: #ffffff !important;
    }

    p, span, label, div {
        color: #eee7fb;
    }

    code {
        color: #f6f2ff !important;
    }

    details {
        background: #1b1030 !important;
        border: 1px solid #3a245f !important;
        border-radius: 12px !important;
        padding: 6px 10px !important;
    }

    hr {
        border-color: #3a245f;
    }
    </style>
    ''',
    unsafe_allow_html=True,
)

st.title("DELTAX Event Based Playbook")
st.caption("Paper-trading monitor for event-driven stock and options execution.")

load_dotenv(ENV_PATH)

API_KEY = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
TRADING_URL = (
    os.getenv("ALPACA_TRADING_URL_EVENT")
    or TRADING_BASE_DEFAULT
).strip().rstrip("/")

if not TRADING_URL.endswith("/v2"):
    TRADING_URL += "/v2"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

def alpaca_get(path: str):
    if not API_KEY or not API_SECRET:
        return None, "EVENT Alpaca credentials are missing in .env"
    try:
        r = requests.get(
            f"{TRADING_URL}{path}",
            headers=HEADERS,
            timeout=8,
        )
        if not r.ok:
            return None, f"{r.status_code}: {r.text[:250]}"
        return r.json(), None
    except Exception as exc:
        return None, str(exc)

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"days": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"days": {}}

def latest_log_lines(n: int = 120) -> str:
    if not LOG_FILE.exists():
        return "Scheduler log does not exist yet."
    try:
        lines = LOG_FILE.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        return "\n".join(lines[-n:])
    except Exception as exc:
        return f"Could not read log: {exc}"

def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "—"

def pct(value) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except Exception:
        return "—"

def position_side(p: dict) -> str:
    side = p.get("side")
    if side:
        return str(side).upper()
    try:
        return "LONG" if float(p.get("qty", 0)) >= 0 else "SHORT"
    except Exception:
        return "—"

account, account_err = alpaca_get("/account")
clock, clock_err = alpaca_get("/clock")
positions, positions_err = alpaca_get("/positions")
orders, orders_err = alpaca_get("/orders?status=all&limit=100&direction=desc")

now_riga = datetime.now(RIGA)
now_et = datetime.now(NY)

equity_value = float((account or {}).get("equity") or 0.0)
portfolio_pnl = equity_value - STARTING_EQUITY
portfolio_growth = (
    portfolio_pnl / STARTING_EQUITY
    if STARTING_EQUITY > 0
    else 0.0
)

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Portfolio growth", f"{portfolio_growth * 100:+.2f}%", delta=money(portfolio_pnl))
c2.metric("Equity", money(equity_value))
c3.metric("Buying power", money((account or {}).get("buying_power")))
c4.metric("Market", "OPEN" if (clock and clock.get("is_open")) else "CLOSED")
c5.metric("Riga time", now_riga.strftime("%H:%M:%S"))
c6.metric("New York time", now_et.strftime("%H:%M:%S"))

if account_err or clock_err:
    st.warning(
        f"Live Alpaca status issue. Account: {account_err or 'OK'} | "
        f"Clock: {clock_err or 'OK'}"
    )

st.subheader("Strategy rules")
r1, r2, r3, r4 = st.columns(4)
r1.metric("Event gap", f"≥ {GAP_THRESHOLD:.2f}%")
r2.metric("10m reversal", f"≥ {REVERSAL_THRESHOLD:.2f}%")
r3.metric("Decision", f"{DECISION_TIME_ET} ET")
r4.metric("Exit", f"{EXIT_TIME_ET} ET")

st.markdown(
    "**LONG:** today open must gap at least +0.50% vs previous close, then pull back at least 0.25% by 09:40 ET.  \n"
    "**SHORT:** today open must gap at least -0.50% vs previous close, then bounce at least 0.25% by 09:40 ET."
)

st.subheader("Event watchlist")

watch_rows = []

for symbol in LONG_SYMBOLS:
    watch_rows.append(
        {
            "Symbol": symbol,
            "Expected": "LONG",
            "Instrument priority": (
                "OPTION → stock fallback"
                if symbol in OPTION_FIRST
                else "STOCK"
            ),
        }
    )

for symbol in SHORT_SYMBOLS:
    watch_rows.append(
        {
            "Symbol": symbol,
            "Expected": "SHORT",
            "Instrument priority": (
                "OPTION → stock fallback"
                if symbol in OPTION_FIRST
                else "STOCK"
            ),
        }
    )

watch_df = pd.DataFrame(watch_rows).sort_values(["Expected", "Symbol"])
st.dataframe(
    watch_df,
    use_container_width=True,
    hide_index=True,
)

state = load_state()
today_key = now_et.date().isoformat()
today_state = state.get("days", {}).get(today_key, {})

st.subheader(f"Today — {today_key}")

signals = today_state.get("signals", {})
saved_orders = today_state.get("orders", {})

if signals:
    rows = []
    for symbol, item in signals.items():
        rows.append(
            {
                "Symbol": symbol,
                "Direction": item.get("direction"),
                "Previous close": item.get("previous_close"),
                "Today open": item.get("today_open"),
                "09:40 price": item.get("price_0940"),
                "Event gap": pct(item.get("event_gap")),
                "10m reversal": pct(item.get("reversal_10m")),
                "Decision": "TRADE",
            }
        )

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info(
        "No qualifying signals have been saved for today yet. "
        "Before 09:40 ET this is expected."
    )

st.subheader("Live EVENT account positions")

if positions_err:
    st.warning(f"Could not load positions: {positions_err}")
elif positions:
    pos_rows = []

    for p in positions:
        pos_rows.append(
            {
                "Symbol": p.get("symbol"),
                "Asset": p.get("asset_class"),
                "Side": position_side(p),
                "Qty": p.get("qty"),
                "Avg entry": p.get("avg_entry_price"),
                "Current": p.get("current_price"),
                "Market value": p.get("market_value"),
                "Unrealized P/L": money(p.get("unrealized_pl")),
                "Unrealized %": pct(p.get("unrealized_plpc")),
            }
        )

    st.dataframe(
        pd.DataFrame(pos_rows),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No open positions in the EVENT paper account.")

st.subheader("Orders tracked by event runner")

if saved_orders:
    order_rows = []

    for underlying, item in saved_orders.items():
        option = item.get("option") or {}

        order_rows.append(
            {
                "Underlying": underlying,
                "Direction": item.get("direction"),
                "Instrument": item.get("instrument"),
                "Tradable symbol": option.get("symbol", underlying),
                "Qty": item.get("qty"),
                "Side": item.get("side"),
                "Event gap": pct(item.get("event_gap")),
                "10m reversal": pct(item.get("reversal_10m")),
                "Order ID": item.get("order_id", "dry-run"),
            }
        )

    st.dataframe(
        pd.DataFrame(order_rows),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No event-runner orders recorded for today.")

with st.expander("Recent Alpaca EVENT account orders"):
    if orders_err:
        st.warning(f"Could not load orders: {orders_err}")
    elif orders:
        recent_rows = []

        for o in orders[:50]:
            recent_rows.append(
                {
                    "Created": o.get("created_at"),
                    "Symbol": o.get("symbol"),
                    "Side": o.get("side"),
                    "Qty": o.get("qty"),
                    "Type": o.get("type"),
                    "Status": o.get("status"),
                    "Filled qty": o.get("filled_qty"),
                    "Filled avg": o.get("filled_avg_price"),
                    "Client order ID": o.get("client_order_id"),
                }
            )

        st.dataframe(
            pd.DataFrame(recent_rows),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No orders returned by Alpaca.")

with st.expander("Raw strategy state"):
    st.code(
        json.dumps(today_state, indent=2, default=str),
        language="json",
    )

with st.expander("Scheduler log — latest 120 lines"):
    st.code(
        latest_log_lines(120),
        language="text",
    )

st.caption(
    f"State: {STATE_FILE} | Log: {LOG_FILE}"
)
