from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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


# -----------------------------------------------------------------------------
# Paths / config
# -----------------------------------------------------------------------------

DASHBOARD_DIR = Path(__file__).resolve().parent
ROOT_DIR = DASHBOARD_DIR.parent
ENV_PATH = ROOT_DIR / ".env"
LOG_FILE = ROOT_DIR / "logs" / "etf_trading_cycle.log"
LOGO_FILE = DASHBOARD_DIR / "assets" / "2026_Deltax_AI.png"

NY = ZoneInfo("America/New_York")
RIGA = ZoneInfo("Europe/Riga")

STARTING_EQUITY = 100000.0
ENTRY_NOTIONAL = 4000.0
STOP_LOSS_PCT = -1.5
TAKE_PROFIT_PCT = 3.0
NO_NEW_ENTRY_DD = -3.0
KILL_SWITCH_DD = -5.0
AI_MIN_CONF = 0.65
PRICE_CONFIRM_MIN = 4
MAX_NEW_TRADES_PER_CYCLE = 5
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


# -----------------------------------------------------------------------------
# Page
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="DELTAX AI Sector Rotation",
    page_icon="Δ",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root {
        --bg: #0A0D14;
        --panel: #111722;
        --panel2: #151D2B;
        --line: #263247;
        --text: #F6F8FC;
        --muted: #92A0B7;
        --green: #3BE3A3;
        --red: #FF6B7A;
        --blue: #65A7FF;
        --gold: #F8C85C;
        --violet: #9A7BFF;
    }

    .stApp {
        background:
            radial-gradient(circle at 12% 0%, rgba(101,167,255,.11), transparent 28%),
            radial-gradient(circle at 85% 0%, rgba(154,123,255,.10), transparent 25%),
            var(--bg);
        color: var(--text);
    }

    [data-testid="stHeader"] {
        background: rgba(10,13,20,.82);
        backdrop-filter: blur(10px);
    }

    [data-testid="stSidebar"] { background: #0E131D; }

    .hero {
        border: 1px solid var(--line);
        border-radius: 22px;
        padding: 22px 24px 18px 24px;
        background: linear-gradient(135deg, rgba(17,23,34,.98), rgba(21,29,43,.95));
        margin-bottom: 16px;
    }

    .hero-title {
        font-size: 34px;
        line-height: 1.05;
        font-weight: 800;
        letter-spacing: -0.7px;
        margin: 0;
    }

    .hero-sub {
        color: var(--muted);
        font-size: 14px;
        margin-top: 8px;
    }

    .badge {
        display: inline-block;
        border: 1px solid #35445F;
        border-radius: 999px;
        padding: 5px 10px;
        margin-right: 6px;
        font-size: 12px;
        color: #DDE7F7;
        background: rgba(255,255,255,.025);
    }

    [data-testid="stMetric"] {
        background: linear-gradient(180deg, #121926, #0F1520);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 14px 16px;
        min-height: 112px;
    }

    [data-testid="stMetricLabel"] {
        color: var(--muted) !important;
        font-size: 12px !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: .6px;
    }

    [data-testid="stMetricValue"] {
        color: var(--text) !important;
        font-weight: 800 !important;
    }

    [data-testid="stMetricDelta"] {
        font-weight: 700 !important;
    }

    .section-title {
        margin-top: 12px;
        font-size: 19px;
        font-weight: 800;
        letter-spacing: -.2px;
    }

    .small-muted {
        color: var(--muted);
        font-size: 12px;
    }

    .decision-card {
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 14px 16px;
        background: var(--panel);
        min-height: 124px;
        margin-bottom: 10px;
    }

    .decision-head {
        font-size: 13px;
        color: var(--muted);
        margin-bottom: 7px;
    }

    .decision-main {
        font-size: 22px;
        font-weight: 800;
        margin-bottom: 6px;
    }

    .good { color: var(--green); }
    .bad { color: var(--red); }
    .neutral { color: var(--gold); }
    .blue { color: var(--blue); }

    .rule-card {
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 12px 14px;
        background: #101620;
        min-height: 98px;
    }

    div[data-testid="stDataFrame"] {
        border: 1px solid var(--line);
        border-radius: 14px;
        overflow: hidden;
    }

    details {
        background: var(--panel) !important;
        border: 1px solid var(--line) !important;
        border-radius: 14px !important;
        padding: 4px 10px !important;
    }

    h1, h2, h3, p, span, label, div { color: #EEF3FB; }
    hr { border-color: var(--line); }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def alpaca_get(path: str, params: dict | None = None):
    if not API_KEY or not API_SECRET:
        return None, "EVENT Alpaca credentials missing"
    try:
        r = requests.get(
            f"{TRADING_URL}/v2{path}",
            headers=HEADERS,
            params=params,
            timeout=10,
        )
        if not r.ok:
            return None, f"{r.status_code}: {r.text[:300]}"
        return r.json(), None
    except Exception as exc:
        return None, str(exc)


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "—"


def pct_number(value, decimals=2) -> str:
    try:
        return f"{float(value):+.{decimals}f}%"
    except Exception:
        return "—"


def pct_fraction(value, decimals=2) -> str:
    try:
        return f"{float(value) * 100:+.{decimals}f}%"
    except Exception:
        return "—"


def num(value, decimals=2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "—"


def position_side(p: dict) -> str:
    side = str(p.get("side") or "").upper()
    if side in {"LONG", "SHORT"}:
        return side
    try:
        return "LONG" if float(p.get("qty", 0)) >= 0 else "SHORT"
    except Exception:
        return "—"


def safe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


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
    """Best-effort read of recent completed market AI analyses from Neon."""
    if not DATABASE_URL or psycopg is None:
        return [], "DATABASE_URL/psycopg unavailable"

    queries = [
        """
        SELECT
            cluster_key,
            updated_at,
            analysis_metadata
        FROM event_clusters
        WHERE event_type = 'market_news'
          AND analysis_status = 'completed'
          AND analysis_metadata IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 25
        """,
        """
        SELECT
            cluster_key,
            updated_at,
            analysis_metadata
        FROM event_clusters
        WHERE analysis_status = 'completed'
          AND analysis_metadata IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 25
        """,
    ]

    last_err = None
    for sql in queries:
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    rows = cur.fetchall()
            return [
                {
                    "cluster_key": r[0],
                    "updated_at": r[1],
                    "analysis": safe_json(r[2]),
                }
                for r in rows
            ], None
        except Exception as exc:
            last_err = str(exc)

    return [], last_err


def parse_ai_rows(records: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    impacts = []
    event_cards = []

    for rec in records:
        a = rec["analysis"] or {}
        summary = a.get("event_summary") or a.get("summary") or "Market event"
        mconf = a.get("market_confidence") or a.get("confidence")
        material = a.get("market_material")
        event_cards.append(
            {
                "updated_at": rec["updated_at"],
                "summary": summary,
                "confidence": mconf,
                "material": material,
                "cluster_key": rec["cluster_key"],
            }
        )

        for item in (a.get("sector_impacts") or []):
            impacts.append({
                "Type": "Sector",
                "Target": item.get("sector") or item.get("symbol") or "—",
                "Direction": str(item.get("direction") or "neutral").upper(),
                "AI confidence": item.get("confidence"),
                "Reason": item.get("reason") or "",
                "Updated": rec["updated_at"],
            })

        for item in (a.get("index_impacts") or []):
            impacts.append({
                "Type": "Index",
                "Target": item.get("symbol") or "—",
                "Direction": str(item.get("direction") or "neutral").upper(),
                "AI confidence": item.get("confidence"),
                "Reason": item.get("reason") or "",
                "Updated": rec["updated_at"],
            })

        for item in (a.get("symbol_impacts") or a.get("saved_symbol_impacts") or []):
            impacts.append({
                "Type": "Symbol",
                "Target": item.get("symbol") or "—",
                "Direction": str(item.get("direction") or "neutral").upper(),
                "AI confidence": item.get("confidence"),
                "Reason": item.get("reason") or "",
                "Updated": rec["updated_at"],
            })

    df = pd.DataFrame(impacts)
    if not df.empty:
        df = df.sort_values(
            ["AI confidence", "Updated"],
            ascending=[False, False],
            na_position="last",
        )
        df = df.drop_duplicates(subset=["Type", "Target"], keep="first")

    return df, event_cards


# -----------------------------------------------------------------------------
# Live data
# -----------------------------------------------------------------------------

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
cash = float(account.get("cash") or 0.0)
buying_power = float(account.get("buying_power") or 0.0)

total_pnl = equity - STARTING_EQUITY
total_growth_pct = (total_pnl / STARTING_EQUITY * 100) if STARTING_EQUITY else 0.0
day_pnl = equity - last_equity if last_equity else 0.0
day_pct = (day_pnl / last_equity * 100) if last_equity else 0.0

etf_positions = [
    p for p in positions
    if str(p.get("symbol") or "").upper() in MANAGED_ETFS
]

open_unrealized = sum(float(p.get("unrealized_pl") or 0.0) for p in etf_positions)
gross_exposure = sum(abs(float(p.get("market_value") or 0.0)) for p in etf_positions)

market_ai_records, ai_err = load_market_ai()
ai_impacts_df, ai_events = parse_ai_rows(market_ai_records)

logo_col, title_col = st.columns([1, 5])

with logo_col:
    if LOGO_FILE.exists():
        st.image(str(LOGO_FILE), width=150)

with title_col:
    st.markdown(
        f"""
        <div class="hero">
            <div class="hero-title">DELTAX AI Sector Rotation</div>
            <div class="hero-sub">
                Autonomous paper-trading agent · AI market regime + deterministic price confirmation · Alpaca EVENT account
            </div>
            <div style="margin-top:13px;">
                <span class="badge">EVENT $100K</span>
                <span class="badge">ETF LONG / SHORT</span>
                <span class="badge">5-min execution</span>
                <span class="badge">Exit-first risk control</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# -----------------------------------------------------------------------------
# Hero
# -----------------------------------------------------------------------------

market_status = "OPEN" if clock.get("is_open") else "CLOSED"
market_class = "good" if market_status == "OPEN" else "neutral"

st.markdown(
    f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;gap:20px;align-items:flex-start;">
            <div>
                <div class="hero-title">Δ DELTAX AI Sector Rotation</div>
                <div class="hero-sub">
                    Autonomous paper-trading agent · AI market regime + deterministic price confirmation · Alpaca EVENT account
                </div>
                <div style="margin-top:13px;">
                    <span class="badge">EVENT $100K</span>
                    <span class="badge">ETF LONG / SHORT</span>
                    <span class="badge">5-min execution</span>
                    <span class="badge">Exit-first risk control</span>
                </div>
            </div>
            <div style="text-align:right;">
                <div class="{market_class}" style="font-size:18px;font-weight:800;">● {market_status}</div>
                <div class="small-muted">{now_et.strftime('%H:%M:%S ET')} · {now_riga.strftime('%H:%M:%S Riga')}</div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Top KPIs
# -----------------------------------------------------------------------------

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Account equity", money(equity), delta=pct_number(total_growth_pct))
c2.metric("Total P/L", money(total_pnl), delta="vs $100,000 start")
c3.metric("Today's P/L", money(day_pnl), delta=pct_number(day_pct))
c4.metric("Open ETF P/L", money(open_unrealized))
c5.metric("ETF exposure", money(gross_exposure), delta=f"{len(etf_positions)} positions")
c6.metric("Buying power", money(buying_power))

if account_err or clock_err or positions_err:
    st.warning(
        "Live data warning · "
        f"Account: {account_err or 'OK'} · "
        f"Clock: {clock_err or 'OK'} · "
        f"Positions: {positions_err or 'OK'}"
    )


# -----------------------------------------------------------------------------
# Current positions
# -----------------------------------------------------------------------------

st.markdown('<div class="section-title">Live ETF positions</div>', unsafe_allow_html=True)
st.caption("Large-number view of what the agent currently owns or shorts in the EVENT paper account.")

if etf_positions:
    cols = st.columns(min(4, len(etf_positions)))
    for idx, p in enumerate(sorted(etf_positions, key=lambda x: float(x.get("unrealized_pl") or 0), reverse=True)):
        side = position_side(p)
        upl = float(p.get("unrealized_pl") or 0.0)
        uplpc = float(p.get("unrealized_plpc") or 0.0)
        cls = "good" if upl >= 0 else "bad"
        symbol = str(p.get("symbol") or "")
        with cols[idx % len(cols)]:
            st.markdown(
                f"""
                <div class="decision-card">
                    <div class="decision-head">{side} · {symbol}</div>
                    <div class="decision-main {cls}">{money(upl)}</div>
                    <div class="{cls}" style="font-weight:800;">{pct_fraction(uplpc)}</div>
                    <div class="small-muted" style="margin-top:8px;">
                        {p.get('qty')} shares · avg {money(p.get('avg_entry_price'))} · now {money(p.get('current_price'))}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    pos_rows = []
    for p in etf_positions:
        pos_rows.append({
            "ETF": p.get("symbol"),
            "Side": position_side(p),
            "Qty": float(p.get("qty") or 0),
            "Avg entry": float(p.get("avg_entry_price") or 0),
            "Current": float(p.get("current_price") or 0),
            "Market value": float(p.get("market_value") or 0),
            "Unrealized P/L": float(p.get("unrealized_pl") or 0),
            "Unrealized %": float(p.get("unrealized_plpc") or 0) * 100,
        })

    pos_df = pd.DataFrame(pos_rows)
    st.dataframe(
        pos_df.style.format({
            "Avg entry": "${:,.2f}",
            "Current": "${:,.2f}",
            "Market value": "${:,.2f}",
            "Unrealized P/L": "${:+,.2f}",
            "Unrealized %": "{:+.2f}%",
            "Qty": "{:,.0f}",
        }),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No managed ETF positions are currently open.")


# -----------------------------------------------------------------------------
# Why the agent is acting
# -----------------------------------------------------------------------------

st.markdown('<div class="section-title">AI market intelligence</div>', unsafe_allow_html=True)
st.caption("Latest completed market-event analyses from Neon. AI proposes direction; price confirmation decides whether a trade is allowed.")

if ai_events:
    latest = ai_events[0]
    conf = latest.get("confidence")
    conf_text = f"{float(conf)*100:.0f}%" if conf is not None else "—"

    a1, a2, a3 = st.columns([2.3, 1, 1])
    with a1:
        st.markdown(
            f"""
            <div class="decision-card">
                <div class="decision-head">LATEST MARKET EVENT</div>
                <div style="font-size:18px;font-weight:800;line-height:1.35;">
                    {latest.get('summary')}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with a2:
        st.metric("AI confidence", conf_text)
    with a3:
        st.metric("Material", "YES" if latest.get("material") else "NO")

    if not ai_impacts_df.empty:
        display_ai = ai_impacts_df.head(18).copy()
        display_ai["AI confidence"] = pd.to_numeric(display_ai["AI confidence"], errors="coerce") * 100
        display_ai["Updated"] = pd.to_datetime(display_ai["Updated"], errors="coerce").dt.strftime("%m-%d %H:%M")
        st.dataframe(
            display_ai[["Type", "Target", "Direction", "AI confidence", "Reason", "Updated"]].style.format(
                {"AI confidence": "{:.0f}%"}
            ),
            use_container_width=True,
            hide_index=True,
            height=430,
        )
else:
    st.info("No recent market AI records were readable from Neon.")
    if ai_err:
        st.caption(f"Neon read issue: {ai_err}")


# -----------------------------------------------------------------------------
# Decision pipeline
# -----------------------------------------------------------------------------

st.markdown('<div class="section-title">How a DELTAX ETF decision is made</div>', unsafe_allow_html=True)

p1, p2, p3, p4, p5 = st.columns(5)
pipeline_cards = [
    ("1 · NEWS", "Alpaca + Finnhub + Marketaux", "Macro / geopolitical / sector catalysts"),
    ("2 · AI REGIME", f"Confidence ≥ {AI_MIN_CONF:.2f}", "Direction and causal market impact"),
    ("3 · PRICE", f"{PRICE_CONFIRM_MIN}/5 confirmation", "Prev close · Open · VWAP · vs SPY · prior momentum"),
    ("4 · RISK", "$4K / entry", "Exit-first · daily gates · no late entries"),
    ("5 · EXECUTION", "Alpaca paper", "LONG strongest · SHORT weakest"),
]
for col, (head, big, small) in zip([p1, p2, p3, p4, p5], pipeline_cards):
    with col:
        st.markdown(
            f"""
            <div class="rule-card">
                <div class="decision-head">{head}</div>
                <div style="font-size:17px;font-weight:800;">{big}</div>
                <div class="small-muted" style="margin-top:5px;">{small}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# -----------------------------------------------------------------------------
# Risk
# -----------------------------------------------------------------------------

st.markdown('<div class="section-title">Risk engine</div>', unsafe_allow_html=True)

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
    st.success("Risk gate normal · New entries may be considered if AI + price confirmation pass.")


# -----------------------------------------------------------------------------
# Orders / decisions
# -----------------------------------------------------------------------------

st.markdown('<div class="section-title">Recent ETF orders & actions</div>', unsafe_allow_html=True)
st.caption("Alpaca orders attributable to the ETF strategy by DELTAX client_order_id prefixes.")

etf_orders = []
for o in orders:
    cid = str(o.get("client_order_id") or "")
    symbol = str(o.get("symbol") or "")
    if cid.startswith("dxe-etf-") or cid.startswith("dxe-etfx-") or symbol in MANAGED_ETFS:
        etf_orders.append(o)

if etf_orders:
    order_rows = []
    for o in etf_orders[:50]:
        cid = str(o.get("client_order_id") or "")
        if cid.startswith("dxe-etfx-"):
            action = "EXIT"
        elif cid.startswith("dxe-etf-"):
            action = "ENTRY"
        else:
            action = "ADOPTED / MANUAL"

        order_rows.append({
            "Time": o.get("created_at"),
            "Action": action,
            "Symbol": o.get("symbol"),
            "Side": str(o.get("side") or "").upper(),
            "Qty": o.get("qty"),
            "Status": str(o.get("status") or "").upper(),
            "Filled": o.get("filled_qty"),
            "Avg fill": o.get("filled_avg_price"),
            "Client order ID": cid,
        })

    orders_df = pd.DataFrame(order_rows)
    orders_df["Time"] = pd.to_datetime(orders_df["Time"], errors="coerce").dt.tz_convert(NY).dt.strftime("%m-%d %H:%M:%S ET")
    st.dataframe(
        orders_df,
        use_container_width=True,
        hide_index=True,
        height=420,
    )
else:
    st.info("No ETF orders found in the latest Alpaca order history.")


# -----------------------------------------------------------------------------
# Universe
# -----------------------------------------------------------------------------

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
