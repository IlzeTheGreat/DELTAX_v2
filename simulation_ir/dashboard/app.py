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

try:
    import psycopg
except Exception:
    psycopg = None


# =============================================================================
# PROJECT PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent


def find_project_root(start: Path) -> Path:
    """Walk upward until we find the project's .env."""
    for candidate in [start, *start.parents]:
        if (candidate / ".env").exists():
            return candidate
    return start.parent


DASHBOARD_DIR = Path(__file__).resolve().parent
SIMULATION_DIR = DASHBOARD_DIR.parent
ROOT_DIR = SIMULATION_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
LOG_FILE = ROOT_DIR / "logs" / "etf_trading_cycle.log"

LOGO_FILE = ROOT_DIR / "dashboard" / "assets" / "2026_Deltax_AI.png"

NY = ZoneInfo("America/New_York")
RIGA = ZoneInfo("Europe/Riga")

STARTING_EQUITY = 100000.0
STOP_LOSS_PCT = -1.5
TAKE_PROFIT_PCT = 3.0
NO_NEW_ENTRY_DD = -3.0
KILL_SWITCH_DD = -5.0
AI_MIN_CONF = 0.65
PRICE_CONFIRM_MIN = 4
ENTRY_NOTIONAL = 4000.0
ENTRY_BLOCK_TIME_ET = "15:30"
EOD_EXIT_TIME_ET = "15:55"

MANAGED_ETFS = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLV", "XLC", "XLY", "XLP", "XLI", "XLE", "XLU", "XLB", "XLRE",
    "SMH", "IGV", "CIBR", "XBI", "IHI", "KRE", "IAI", "IYT", "ITA", "XOP",
    "GLD", "TLT", "BIL", "USO",
]

load_dotenv(ENV_PATH)

API_KEY = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
TRADING_URL = (
    os.getenv("ALPACA_TRADING_URL_EVENT")
    or "https://paper-api.alpaca.markets"
).strip().rstrip("/")

if TRADING_URL.endswith("/v2"):
    TRADING_URL = TRADING_URL[:-3]

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


# =============================================================================
# PAGE / STYLE
# =============================================================================

st.set_page_config(
    page_title="DELTAX AI Sector Rotation",
    page_icon="🍀",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
:root {
    --bg: #13091f;
    --panel: #211035;
    --panel2: #2a1544;
    --border: #59327d;
    --text: #fbf8ff;
    --muted: #c6b6d7;
    --purple: #a968ff;
}

.stApp {
    background:
        radial-gradient(circle at 8% 0%, rgba(169,104,255,.18), transparent 28%),
        radial-gradient(circle at 92% 0%, rgba(120,62,198,.15), transparent 25%),
        linear-gradient(180deg, #13091f 0%, #180c27 45%, #11081b 100%);
}

[data-testid="stHeader"] {
    background: rgba(19,9,31,.90);
}

.block-container {
    padding-top: 3rem;
    padding-bottom: 3rem;
    max-width: 1540px;
}

[data-testid="stMetric"] {
    background: linear-gradient(180deg, #28143f, #201032);
    border: 1px solid #50306d;
    border-radius: 18px;
    padding: 15px 16px;
    min-height: 116px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.03);
}

[data-testid="stMetricLabel"] {
    color: #c6b6d7 !important;
    text-transform: uppercase;
    letter-spacing: .65px;
    font-weight: 700 !important;
    font-size: 12px !important;
}

[data-testid="stMetricValue"] {
    color: #ffffff !important;
    font-weight: 850 !important;
}

[data-testid="stMetricDelta"] {
    font-weight: 750 !important;
}

div[data-testid="stDataFrame"] {
    border: 1px solid #50306d;
    border-radius: 15px;
    overflow: hidden;
}

details {
    background: #211035 !important;
    border: 1px solid #50306d !important;
    border-radius: 14px !important;
}

h1, h2, h3, p, span, label, div {
    color: #f8f3ff;
}

.hero {
    background: linear-gradient(135deg, #2b1646, #1d0e30);
    border: 1px solid #5a337e;
    border-radius: 22px;
    padding: 21px 24px;
    margin-bottom: 12px;
}

.hero-title {
    font-size: 34px;
    font-weight: 850;
    line-height: 1.06;
    letter-spacing: -.8px;
    color: white;
}

.hero-sub {
    margin-top: 8px;
    color: #c6b6d7;
    font-size: 14px;
}

.badge {
    display:inline-block;
    margin-top: 12px;
    margin-right: 6px;
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid #71459b;
    background: rgba(169,104,255,.09);
    color: #f5ebff;
    font-size: 12px;
}

.section-title {
    font-size: 20px;
    font-weight: 850;
    margin-top: 18px;
    margin-bottom: 2px;
}

.section-sub {
    color: #c6b6d7;
    font-size: 12px;
    margin-bottom: 10px;
}

.pipeline {
    background: linear-gradient(180deg, #28143f, #201032);
    border: 1px solid #50306d;
    border-radius: 15px;
    padding: 13px 14px;
    min-height: 108px;
}

.pipeline-step {
    color: #c6b6d7;
    font-size: 11px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .7px;
}

.pipeline-main {
    color: white;
    font-size: 17px;
    font-weight: 850;
    margin-top: 5px;
}

.pipeline-sub {
    color: #c6b6d7;
    font-size: 12px;
    margin-top: 5px;
    line-height: 1.35;
}
</style>
""", unsafe_allow_html=True)


# =============================================================================
# HELPERS
# =============================================================================

def alpaca_get(path: str, params: dict | None = None):
    if not API_KEY or not API_SECRET:
        return None, "EVENT Alpaca credentials missing"
    try:
        response = requests.get(
            f"{TRADING_URL}/v2{path}",
            headers=HEADERS,
            params=params,
            timeout=10,
        )
        if not response.ok:
            return None, f"{response.status_code}: {response.text[:300]}"
        return response.json(), None
    except Exception as exc:
        return None, str(exc)


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "—"


def pct_number(value) -> str:
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "—"


def pct_fraction(value) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except Exception:
        return "—"


def position_side(position: dict) -> str:
    side = str(position.get("side") or "").upper()
    if side in {"LONG", "SHORT"}:
        return side
    try:
        return "LONG" if float(position.get("qty", 0)) >= 0 else "SHORT"
    except Exception:
        return "—"


def safe_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}


def latest_log_lines(n: int = 120) -> str:
    if not LOG_FILE.exists():
        return "ETF trading cycle log does not exist yet."
    try:
        return "\n".join(
            LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        )
    except Exception as exc:
        return f"Could not read log: {exc}"


def load_market_ai():
    if not DATABASE_URL or psycopg is None:
        return [], "DATABASE_URL/psycopg unavailable"

    queries = [
        """
        SELECT cluster_key, updated_at, analysis_metadata
        FROM event_clusters
        WHERE event_type = 'market_news'
          AND analysis_status = 'completed'
          AND analysis_metadata IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 25
        """,
        """
        SELECT cluster_key, updated_at, analysis_metadata
        FROM event_clusters
        WHERE analysis_status = 'completed'
          AND analysis_metadata IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 25
        """,
    ]

    last_error = None
    for sql in queries:
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    rows = cursor.fetchall()
            return [
                {
                    "cluster_key": row[0],
                    "updated_at": row[1],
                    "analysis": safe_json(row[2]),
                }
                for row in rows
            ], None
        except Exception as exc:
            last_error = str(exc)

    return [], last_error


def parse_ai_rows(records: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    impacts = []
    events = []

    for record in records:
        analysis = record["analysis"] or {}
        events.append({
            "updated_at": record["updated_at"],
            "summary": analysis.get("event_summary") or analysis.get("summary") or "Market event",
            "confidence": (
                analysis.get("market_confidence")
                if analysis.get("market_confidence") is not None
                else analysis.get("confidence")
            ),
            "material": analysis.get("market_material"),
        })

        for item in analysis.get("sector_impacts") or []:
            impacts.append({
                "Type": "Sector",
                "Target": item.get("sector") or item.get("symbol") or "—",
                "Direction": str(item.get("direction") or "neutral").upper(),
                "AI confidence": item.get("confidence"),
                "Reason": item.get("reason") or "",
                "Updated": record["updated_at"],
            })

        for item in analysis.get("index_impacts") or []:
            impacts.append({
                "Type": "Index",
                "Target": item.get("symbol") or "—",
                "Direction": str(item.get("direction") or "neutral").upper(),
                "AI confidence": item.get("confidence"),
                "Reason": item.get("reason") or "",
                "Updated": record["updated_at"],
            })

        for item in (
            analysis.get("symbol_impacts")
            or analysis.get("saved_symbol_impacts")
            or []
        ):
            impacts.append({
                "Type": "Symbol",
                "Target": item.get("symbol") or "—",
                "Direction": str(item.get("direction") or "neutral").upper(),
                "AI confidence": item.get("confidence"),
                "Reason": item.get("reason") or "",
                "Updated": record["updated_at"],
            })

    df = pd.DataFrame(impacts)
    if not df.empty:
        df = df.sort_values(
            ["AI confidence", "Updated"],
            ascending=[False, False],
            na_position="last",
        ).drop_duplicates(["Type", "Target"], keep="first")

    return df, events


# =============================================================================
# LIVE DATA
# =============================================================================

account, account_err = alpaca_get("/account")
clock, clock_err = alpaca_get("/clock")
positions, positions_err = alpaca_get("/positions")
orders, orders_err = alpaca_get(
    "/orders",
    {"status": "all", "limit": 100, "direction": "desc"},
)

account = account or {}
clock = clock or {}
positions = positions or []
orders = orders or []

now_riga = datetime.now(RIGA)
now_et = datetime.now(NY)

equity = float(account.get("equity") or 0.0)
last_equity = float(account.get("last_equity") or 0.0)
buying_power = float(account.get("buying_power") or 0.0)

total_pnl = equity - STARTING_EQUITY
total_growth_pct = total_pnl / STARTING_EQUITY * 100 if STARTING_EQUITY else 0.0
day_pnl = equity - last_equity if last_equity else 0.0
day_pct = day_pnl / last_equity * 100 if last_equity else 0.0

etf_positions = [
    p for p in positions
    if str(p.get("symbol") or "").upper() in MANAGED_ETFS
]

open_unrealized = sum(float(p.get("unrealized_pl") or 0.0) for p in etf_positions)
gross_exposure = sum(abs(float(p.get("market_value") or 0.0)) for p in etf_positions)

market_ai_records, ai_err = load_market_ai()
ai_impacts_df, ai_events = parse_ai_rows(market_ai_records)


# =============================================================================
# HEADER
# =============================================================================

logo_col, title_col = st.columns([1, 5], gap="medium")

with logo_col:
    if LOGO_FILE:
        st.image(str(LOGO_FILE), use_container_width=True)
    else:
        st.markdown("## Δ DELTAX")
        st.caption("Logo file not found")

with title_col:
    market_status = "OPEN" if clock.get("is_open") else "CLOSED"
    hero_html = (
        '<div class="hero">'
        '<div class="hero-title">DELTAX AI Sector Rotation</div>'
        '<div class="hero-sub">Autonomous paper-trading agent · AI market intelligence + deterministic price confirmation · Alpaca EVENT account</div>'
        '<span class="badge">EVENT $100K</span>'
        '<span class="badge">ETF LONG / SHORT</span>'
        '<span class="badge">5-min execution</span>'
        '<span class="badge">Exit-first risk control</span>'
        f'<span class="badge">Market: {market_status}</span>'
        f'<span class="badge">{now_et.strftime("%H:%M:%S ET")}</span>'
        '</div>'
    )
    st.markdown(hero_html, unsafe_allow_html=True)


# =============================================================================
# KPIs
# =============================================================================

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Account equity", money(equity), delta=pct_number(total_growth_pct))
c2.metric("Total P/L", money(total_pnl), delta="vs $100,000 start")
c3.metric("Today's P/L", money(day_pnl), delta=pct_number(day_pct))
c4.metric("Open ETF P/L", money(open_unrealized))
c5.metric("ETF exposure", money(gross_exposure), delta=f"{len(etf_positions)} positions")
c6.metric("Buying power", money(buying_power))

if account_err or clock_err or positions_err:
    st.warning(
        f"Live data warning · Account: {account_err or 'OK'} · "
        f"Clock: {clock_err or 'OK'} · Positions: {positions_err or 'OK'}"
    )


# =============================================================================
# LIVE POSITIONS
# =============================================================================

st.markdown('<div class="section-title">Live ETF positions</div>', unsafe_allow_html=True)
st.markdown('<div class="section-sub">Current direction and unrealized performance in the EVENT paper account.</div>', unsafe_allow_html=True)

if etf_positions:
    sorted_positions = sorted(
        etf_positions,
        key=lambda p: float(p.get("unrealized_pl") or 0),
        reverse=True,
    )

    card_cols = st.columns(min(4, len(sorted_positions)))
    for i, p in enumerate(sorted_positions):
        with card_cols[i % len(card_cols)]:
            st.metric(
                f"{position_side(p)} · {p.get('symbol')}",
                money(p.get("unrealized_pl")),
                delta=pct_fraction(p.get("unrealized_plpc")),
            )
            st.caption(
                f"{p.get('qty')} shares · avg {money(p.get('avg_entry_price'))} · "
                f"now {money(p.get('current_price'))}"
            )

    pos_df = pd.DataFrame([
        {
            "ETF": p.get("symbol"),
            "Side": position_side(p),
            "Qty": float(p.get("qty") or 0),
            "Avg entry": float(p.get("avg_entry_price") or 0),
            "Current": float(p.get("current_price") or 0),
            "Market value": float(p.get("market_value") or 0),
            "Unrealized P/L": float(p.get("unrealized_pl") or 0),
            "Unrealized %": float(p.get("unrealized_plpc") or 0) * 100,
        }
        for p in etf_positions
    ])

    st.dataframe(
        pos_df.style.format({
            "Qty": "{:,.0f}",
            "Avg entry": "${:,.2f}",
            "Current": "${:,.2f}",
            "Market value": "${:,.2f}",
            "Unrealized P/L": "${:+,.2f}",
            "Unrealized %": "{:+.2f}%",
        }),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No managed ETF positions are currently open.")


# =============================================================================
# AI MARKET INTELLIGENCE
# =============================================================================

st.markdown('<div class="section-title">AI market intelligence</div>', unsafe_allow_html=True)
st.markdown('<div class="section-sub">AI identifies market causality. Deterministic price confirmation decides whether the trade is allowed.</div>', unsafe_allow_html=True)

if ai_events:
    latest = ai_events[0]
    confidence = latest.get("confidence")
    confidence_text = f"{float(confidence) * 100:.0f}%" if confidence is not None else "—"

    a1, a2, a3 = st.columns([3, 1, 1])
    with a1:
        st.info(latest.get("summary") or "Market event")
    with a2:
        st.metric("AI confidence", confidence_text)
    with a3:
        st.metric("Material", "YES" if latest.get("material") else "NO")

    if not ai_impacts_df.empty:
        display_ai = ai_impacts_df.head(18).copy()
        display_ai["AI confidence"] = pd.to_numeric(
            display_ai["AI confidence"], errors="coerce"
        ) * 100
        display_ai["Updated"] = pd.to_datetime(
            display_ai["Updated"], errors="coerce"
        ).dt.strftime("%m-%d %H:%M")

        st.dataframe(
            display_ai[
                ["Type", "Target", "Direction", "AI confidence", "Reason", "Updated"]
            ].style.format({"AI confidence": "{:.0f}%"}),
            use_container_width=True,
            hide_index=True,
            height=430,
        )
else:
    st.info("No recent market AI records were readable from Neon.")
    if ai_err:
        st.caption(f"Neon read issue: {ai_err}")


# =============================================================================
# DECISION PIPELINE
# =============================================================================

st.markdown('<div class="section-title">How DELTAX makes an ETF decision</div>', unsafe_allow_html=True)
st.markdown('<div class="section-sub">AI explains why. Market data confirms whether the market agrees.</div>', unsafe_allow_html=True)

pipeline = [
    ("1 · NEWS", "Alpaca + Finnhub + Marketaux", "Macro, geopolitical and sector catalysts"),
    ("2 · AI REGIME", f"Confidence ≥ {AI_MIN_CONF:.2f}", "Direction + causal market impact"),
    ("3 · PRICE", f"{PRICE_CONFIRM_MIN}/5 confirmation", "Prev close · Open · VWAP · vs SPY · prior momentum"),
    ("4 · RISK", "$4K / entry", "Exit-first · daily gates · no late entries"),
    ("5 · EXECUTION", "Alpaca paper", "LONG strongest · SHORT weakest"),
]

cols = st.columns(5)
for col, (step, main, sub) in zip(cols, pipeline):
    with col:
        html = (
            '<div class="pipeline">'
            f'<div class="pipeline-step">{step}</div>'
            f'<div class="pipeline-main">{main}</div>'
            f'<div class="pipeline-sub">{sub}</div>'
            '</div>'
        )
        st.markdown(html, unsafe_allow_html=True)


# =============================================================================
# RISK ENGINE
# =============================================================================

st.markdown('<div class="section-title">Risk engine</div>', unsafe_allow_html=True)
st.markdown('<div class="section-sub">Protection runs before fresh entries are considered.</div>', unsafe_allow_html=True)

r1, r2, r3, r4, r5, r6 = st.columns(6)
r1.metric("Hard stop", f"{STOP_LOSS_PCT:.1f}%")
r2.metric("Take profit", f"+{TAKE_PROFIT_PCT:.1f}%")
r3.metric("No new entries", f"{NO_NEW_ENTRY_DD:.0f}% day")
r4.metric("Kill switch", f"{KILL_SWITCH_DD:.0f}% day")
r5.metric("Entry block", f"{ENTRY_BLOCK_TIME_ET} ET")
r6.metric("Flat by", f"{EOD_EXIT_TIME_ET} ET")

if day_pct <= KILL_SWITCH_DD:
    st.error("KILL SWITCH ACTIVE · Managed ETF positions should be closed and new entries blocked.")
elif day_pct <= NO_NEW_ENTRY_DD:
    st.warning("DAILY RISK GATE ACTIVE · Exits allowed, new entries blocked.")
else:
    st.success("Risk gate normal · New entries may be considered when AI + price confirmation pass.")


# =============================================================================
# ORDERS
# =============================================================================

st.markdown('<div class="section-title">Recent ETF orders & actions</div>', unsafe_allow_html=True)
st.markdown('<div class="section-sub">Entries, exits and adopted/manual EVENT-account ETF activity.</div>', unsafe_allow_html=True)

etf_orders = []
for order in orders:
    cid = str(order.get("client_order_id") or "")
    symbol = str(order.get("symbol") or "")
    if cid.startswith("dxe-etf-") or cid.startswith("dxe-etfx-") or symbol in MANAGED_ETFS:
        etf_orders.append(order)

if etf_orders:
    rows = []
    for order in etf_orders[:50]:
        cid = str(order.get("client_order_id") or "")
        if cid.startswith("dxe-etfx-"):
            action = "EXIT"
        elif cid.startswith("dxe-etf-"):
            action = "ENTRY"
        else:
            action = "ADOPTED / MANUAL"

        rows.append({
            "Time": order.get("created_at"),
            "Action": action,
            "Symbol": order.get("symbol"),
            "Side": str(order.get("side") or "").upper(),
            "Qty": order.get("qty"),
            "Status": str(order.get("status") or "").upper(),
            "Filled": order.get("filled_qty"),
            "Avg fill": order.get("filled_avg_price"),
            "Client order ID": cid,
        })

    orders_df = pd.DataFrame(rows)
    orders_df["Time"] = (
        pd.to_datetime(orders_df["Time"], errors="coerce", utc=True)
        .dt.tz_convert(NY)
        .dt.strftime("%m-%d %H:%M:%S ET")
    )
    st.dataframe(orders_df, use_container_width=True, hide_index=True, height=420)
else:
    st.info("No ETF orders found in the latest Alpaca order history.")


# =============================================================================
# DETAILS
# =============================================================================

with st.expander("Managed ETF universe"):
    st.write(
        "**Broad:** SPY, QQQ, IWM, DIA  \n"
        "**GICS sectors:** XLK, XLF, XLV, XLC, XLY, XLP, XLI, XLE, XLU, XLB, XLRE  \n"
        "**Focused:** SMH, IGV, CIBR, XBI, IHI, KRE, IAI, IYT, ITA, XOP  \n"
        "**Macro / defensive:** GLD, TLT, BIL, USO"
    )

with st.expander("ETF trading cycle log"):
    st.code(latest_log_lines(160), language="text")

with st.expander("System / data status"):
    st.json({
        "project_root": str(ROOT_DIR),
        "script_dir": str(SCRIPT_DIR),
        "logo_found": bool(LOGO_FILE),
        "logo_path": str(LOGO_FILE) if LOGO_FILE else None,
        "alpaca_event_account": "OK" if not account_err else account_err,
        "alpaca_clock": "OK" if not clock_err else clock_err,
        "alpaca_positions": "OK" if not positions_err else positions_err,
        "alpaca_orders": "OK" if not orders_err else orders_err,
        "neon_market_ai": "OK" if not ai_err else ai_err,
        "etf_log": str(LOG_FILE),
        "market_open": bool(clock.get("is_open")),
        "riga_time": now_riga.isoformat(),
        "new_york_time": now_et.isoformat(),
    })

st.caption(
    "DELTAX · Alpaca AI Trading Agents Hackathon · "
    "AI explains market causality; deterministic market data confirms execution."
)
