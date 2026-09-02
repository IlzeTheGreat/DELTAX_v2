from __future__ import annotations

import base64
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
# PATHS / CONFIG
# =============================================================================

DASHBOARD_DIR = Path(__file__).resolve().parent
ROOT_DIR = DASHBOARD_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
LOG_FILE = ROOT_DIR / "logs" / "etf_trading_cycle.log"

# Expected:
# project_root/
#   dashboard/
#     deltax_etf_dashboard.py
#     assets/
#       2026_Deltax_AI.png
LOGO_FILE = DASHBOARD_DIR / "assets" / "2026_Deltax_AI.png"

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


# =============================================================================
# PAGE CONFIG
# =============================================================================

st.set_page_config(
    page_title="DELTAX AI Sector Rotation",
    page_icon="Δ",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# =============================================================================
# THEME
# =============================================================================

st.markdown(
    """
    <style>
    :root {
        --bg: #120A1F;
        --bg2: #1A0D2E;
        --panel: #211238;
        --panel2: #281747;
        --panel3: #311C55;
        --line: #533177;
        --line-soft: #40265C;

        --text: #FAF7FF;
        --muted: #C4B4D8;

        --purple: #B178FF;
        --purple2: #8B5CF6;
        --purple3: #6D3EC5;

        --green: #46E6A7;
        --red: #FF7084;
        --gold: #F4C95D;
        --cyan: #73D2FF;
    }

    .stApp {
        background:
            radial-gradient(circle at 12% 0%, rgba(177,120,255,.18), transparent 30%),
            radial-gradient(circle at 88% 4%, rgba(109,62,197,.20), transparent 27%),
            linear-gradient(180deg, #120A1F 0%, #160B26 45%, #120A1F 100%);
        color: var(--text);
    }

    [data-testid="stHeader"] {
        background: rgba(18,10,31,.88);
        backdrop-filter: blur(10px);
    }

    [data-testid="stSidebar"] {
        background: #140B22;
    }

    .main .block-container {
        padding-top: 1.4rem;
        padding-bottom: 3rem;
        max-width: 1550px;
    }

    .hero {
        border: 1px solid var(--line);
        border-radius: 24px;
        padding: 22px 24px;
        background:
            linear-gradient(135deg, rgba(40,23,71,.98), rgba(28,15,49,.98));
        box-shadow:
            0 18px 55px rgba(0,0,0,.22),
            inset 0 1px 0 rgba(255,255,255,.03);
        margin-bottom: 18px;
    }

    .hero-title {
        font-size: 36px;
        line-height: 1.05;
        font-weight: 850;
        letter-spacing: -0.9px;
        margin: 0;
        color: #FFFFFF;
    }

    .hero-sub {
        color: var(--muted);
        font-size: 14px;
        margin-top: 8px;
        line-height: 1.5;
    }

    .badge {
        display: inline-block;
        border: 1px solid #66408B;
        border-radius: 999px;
        padding: 5px 10px;
        margin-right: 6px;
        margin-top: 8px;
        font-size: 12px;
        color: #F3E9FF;
        background: rgba(177,120,255,.07);
    }

    .logo-shell {
        border: 1px solid var(--line);
        border-radius: 24px;
        min-height: 158px;
        background: linear-gradient(180deg, rgba(40,23,71,.96), rgba(31,17,54,.96));
        display:flex;
        align-items:center;
        justify-content:center;
        padding: 16px;
    }

    [data-testid="stMetric"] {
        background:
            linear-gradient(180deg, rgba(39,22,68,.98), rgba(28,15,49,.98));
        border: 1px solid var(--line-soft);
        border-radius: 17px;
        padding: 15px 16px;
        min-height: 118px;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.025);
    }

    [data-testid="stMetricLabel"] {
        color: var(--muted) !important;
        font-size: 12px !important;
        font-weight: 700 !important;
        text-transform: uppercase;
        letter-spacing: .7px;
    }

    [data-testid="stMetricValue"] {
        color: var(--text) !important;
        font-weight: 850 !important;
        letter-spacing: -.3px;
    }

    [data-testid="stMetricDelta"] {
        font-weight: 750 !important;
    }

    .section-title {
        margin-top: 16px;
        margin-bottom: 4px;
        font-size: 20px;
        font-weight: 850;
        letter-spacing: -.25px;
        color: #FFFFFF;
    }

    .section-kicker {
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 10px;
    }

    .small-muted {
        color: var(--muted);
        font-size: 12px;
        line-height: 1.4;
    }

    .decision-card {
        border: 1px solid var(--line-soft);
        border-radius: 17px;
        padding: 15px 16px;
        background:
            linear-gradient(180deg, rgba(39,22,68,.98), rgba(28,15,49,.98));
        min-height: 128px;
        margin-bottom: 10px;
    }

    .decision-head {
        font-size: 12px;
        color: var(--muted);
        font-weight: 700;
        letter-spacing: .6px;
        text-transform: uppercase;
        margin-bottom: 7px;
    }

    .decision-main {
        font-size: 23px;
        font-weight: 850;
        margin-bottom: 6px;
    }

    .good { color: var(--green); }
    .bad { color: var(--red); }
    .neutral { color: var(--gold); }
    .purple { color: var(--purple); }

    .rule-card {
        border: 1px solid var(--line-soft);
        border-radius: 15px;
        padding: 13px 14px;
        background:
            linear-gradient(180deg, rgba(38,21,66,.98), rgba(28,15,49,.98));
        min-height: 104px;
    }

    div[data-testid="stDataFrame"] {
        border: 1px solid var(--line-soft);
        border-radius: 15px;
        overflow: hidden;
        background: #1D102F;
    }

    details {
        background: var(--panel) !important;
        border: 1px solid var(--line-soft) !important;
        border-radius: 14px !important;
        padding: 4px 10px !important;
    }

    .stAlert {
        border-radius: 14px;
        border: 1px solid var(--line-soft);
    }

    h1, h2, h3, p, span, label, div {
        color: #F5F0FF;
    }

    hr {
        border-color: var(--line-soft);
    }

    button[kind="header"] {
        color: white !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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
        lines = LOG_FILE.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()

        return "\n".join(lines[-n:])

    except Exception as exc:
        return f"Could not read log: {exc}"


def logo_data_uri(path: Path) -> str | None:
    """
    Embed logo as base64 in HTML.
    This is more reliable than relying on st.image layout/path behavior.
    """
    if not path.exists():
        return None

    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


def load_market_ai():
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
    event_cards = []

    for record in records:
        analysis = record["analysis"] or {}

        summary = (
            analysis.get("event_summary")
            or analysis.get("summary")
            or "Market event"
        )

        market_confidence = (
            analysis.get("market_confidence")
            if analysis.get("market_confidence") is not None
            else analysis.get("confidence")
        )

        event_cards.append(
            {
                "updated_at": record["updated_at"],
                "summary": summary,
                "confidence": market_confidence,
                "material": analysis.get("market_material"),
                "cluster_key": record["cluster_key"],
            }
        )

        for item in analysis.get("sector_impacts") or []:
            impacts.append(
                {
                    "Type": "Sector",
                    "Target": item.get("sector") or item.get("symbol") or "—",
                    "Direction": str(item.get("direction") or "neutral").upper(),
                    "AI confidence": item.get("confidence"),
                    "Reason": item.get("reason") or "",
                    "Updated": record["updated_at"],
                }
            )

        for item in analysis.get("index_impacts") or []:
            impacts.append(
                {
                    "Type": "Index",
                    "Target": item.get("symbol") or "—",
                    "Direction": str(item.get("direction") or "neutral").upper(),
                    "AI confidence": item.get("confidence"),
                    "Reason": item.get("reason") or "",
                    "Updated": record["updated_at"],
                }
            )

        symbol_impacts = (
            analysis.get("symbol_impacts")
            or analysis.get("saved_symbol_impacts")
            or []
        )

        for item in symbol_impacts:
            impacts.append(
                {
                    "Type": "Symbol",
                    "Target": item.get("symbol") or "—",
                    "Direction": str(item.get("direction") or "neutral").upper(),
                    "AI confidence": item.get("confidence"),
                    "Reason": item.get("reason") or "",
                    "Updated": record["updated_at"],
                }
            )

    dataframe = pd.DataFrame(impacts)

    if not dataframe.empty:
        dataframe = dataframe.sort_values(
            ["AI confidence", "Updated"],
            ascending=[False, False],
            na_position="last",
        )

        dataframe = dataframe.drop_duplicates(
            subset=["Type", "Target"],
            keep="first",
        )

    return dataframe, event_cards


# =============================================================================
# LIVE DATA
# =============================================================================

account, account_err = alpaca_get("/account")
clock, clock_err = alpaca_get("/clock")
positions, positions_err = alpaca_get("/positions")
orders, orders_err = alpaca_get(
    "/orders",
    {
        "status": "all",
        "limit": 100,
        "direction": "desc",
    },
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
total_growth_pct = (
    total_pnl / STARTING_EQUITY * 100
    if STARTING_EQUITY
    else 0.0
)

day_pnl = (
    equity - last_equity
    if last_equity
    else 0.0
)

day_pct = (
    day_pnl / last_equity * 100
    if last_equity
    else 0.0
)

etf_positions = [
    position
    for position in positions
    if str(position.get("symbol") or "").upper() in MANAGED_ETFS
]

open_unrealized = sum(
    float(position.get("unrealized_pl") or 0.0)
    for position in etf_positions
)

gross_exposure = sum(
    abs(float(position.get("market_value") or 0.0))
    for position in etf_positions
)

market_ai_records, ai_err = load_market_ai()
ai_impacts_df, ai_events = parse_ai_rows(market_ai_records)


# =============================================================================
# HERO + LOGO
# =============================================================================

market_status = "OPEN" if clock.get("is_open") else "CLOSED"
market_class = "good" if market_status == "OPEN" else "neutral"
logo_uri = logo_data_uri(LOGO_FILE)

logo_col, hero_col = st.columns([1.05, 5.2], gap="medium")

with logo_col:
    if logo_uri:
        st.markdown(
            f"""
            <div class="logo-shell">
                <img
                    src="{logo_uri}"
                    alt="DELTAX logo"
                    style="
                        width:100%;
                        max-width:190px;
                        max-height:125px;
                        object-fit:contain;
                        display:block;
                        margin:auto;
                    "
                />
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="logo-shell">
                <div style="text-align:center;">
                    <div style="font-size:38px;font-weight:900;color:#B178FF;">Δ</div>
                    <div style="font-size:13px;color:#C4B4D8;">DELTAX</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

with hero_col:
    st.markdown(
        f"""
        <div class="hero">
            <div style="display:flex;justify-content:space-between;gap:20px;align-items:flex-start;">
                <div>
                    <div class="hero-title">DELTAX AI Sector Rotation</div>
                    <div class="hero-sub">
                        Autonomous paper-trading agent · AI market intelligence +
                        deterministic price confirmation · Alpaca EVENT account
                    </div>

                    <div style="margin-top:10px;">
                        <span class="badge">EVENT $100K</span>
                        <span class="badge">ETF LONG / SHORT</span>
                        <span class="badge">5-min execution</span>
                        <span class="badge">Exit-first risk control</span>
                    </div>
                </div>

                <div style="text-align:right;min-width:170px;">
                    <div class="{market_class}" style="font-size:19px;font-weight:850;">
                        ● {market_status}
                    </div>
                    <div class="small-muted">
                        {now_et.strftime('%H:%M:%S ET')}<br>
                        {now_riga.strftime('%H:%M:%S Riga')}
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =============================================================================
# TOP KPIs
# =============================================================================

c1, c2, c3, c4, c5, c6 = st.columns(6)

c1.metric(
    "Account equity",
    money(equity),
    delta=pct_number(total_growth_pct),
)

c2.metric(
    "Total P/L",
    money(total_pnl),
    delta="vs $100,000 start",
)

c3.metric(
    "Today's P/L",
    money(day_pnl),
    delta=pct_number(day_pct),
)

c4.metric(
    "Open ETF P/L",
    money(open_unrealized),
)

c5.metric(
    "ETF exposure",
    money(gross_exposure),
    delta=f"{len(etf_positions)} positions",
)

c6.metric(
    "Buying power",
    money(buying_power),
)

if account_err or clock_err or positions_err:
    st.warning(
        "Live data warning · "
        f"Account: {account_err or 'OK'} · "
        f"Clock: {clock_err or 'OK'} · "
        f"Positions: {positions_err or 'OK'}"
    )


# =============================================================================
# LIVE POSITIONS
# =============================================================================

st.markdown(
    '<div class="section-title">Live ETF positions</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="section-kicker">Current holdings, direction and unrealized performance in the EVENT paper account.</div>',
    unsafe_allow_html=True,
)

if etf_positions:
    card_columns = st.columns(min(4, len(etf_positions)))

    sorted_positions = sorted(
        etf_positions,
        key=lambda item: float(item.get("unrealized_pl") or 0),
        reverse=True,
    )

    for index, position in enumerate(sorted_positions):
        side = position_side(position)
        upl = float(position.get("unrealized_pl") or 0.0)
        uplpc = float(position.get("unrealized_plpc") or 0.0)
        css_class = "good" if upl >= 0 else "bad"
        symbol = str(position.get("symbol") or "")

        with card_columns[index % len(card_columns)]:
            st.markdown(
                f"""
                <div class="decision-card">
                    <div class="decision-head">{side} · {symbol}</div>

                    <div class="decision-main {css_class}">
                        {money(upl)}
                    </div>

                    <div class="{css_class}" style="font-weight:850;font-size:16px;">
                        {pct_fraction(uplpc)}
                    </div>

                    <div class="small-muted" style="margin-top:8px;">
                        {position.get('qty')} shares<br>
                        avg {money(position.get('avg_entry_price'))}
                        · now {money(position.get('current_price'))}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    position_rows = []

    for position in etf_positions:
        position_rows.append(
            {
                "ETF": position.get("symbol"),
                "Side": position_side(position),
                "Qty": float(position.get("qty") or 0),
                "Avg entry": float(position.get("avg_entry_price") or 0),
                "Current": float(position.get("current_price") or 0),
                "Market value": float(position.get("market_value") or 0),
                "Unrealized P/L": float(position.get("unrealized_pl") or 0),
                "Unrealized %": float(position.get("unrealized_plpc") or 0) * 100,
            }
        )

    position_df = pd.DataFrame(position_rows)

    st.dataframe(
        position_df.style.format(
            {
                "Avg entry": "${:,.2f}",
                "Current": "${:,.2f}",
                "Market value": "${:,.2f}",
                "Unrealized P/L": "${:+,.2f}",
                "Unrealized %": "{:+.2f}%",
                "Qty": "{:,.0f}",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

else:
    st.info("No managed ETF positions are currently open.")


# =============================================================================
# AI MARKET INTELLIGENCE
# =============================================================================

st.markdown(
    '<div class="section-title">AI market intelligence</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="section-kicker">AI identifies market causality and directional impact. Price confirmation still decides whether execution is allowed.</div>',
    unsafe_allow_html=True,
)

if ai_events:
    latest = ai_events[0]

    confidence = latest.get("confidence")
    confidence_text = (
        f"{float(confidence) * 100:.0f}%"
        if confidence is not None
        else "—"
    )

    a1, a2, a3 = st.columns([2.4, 1, 1])

    with a1:
        st.markdown(
            f"""
            <div class="decision-card">
                <div class="decision-head">Latest market event</div>

                <div style="
                    font-size:18px;
                    font-weight:800;
                    line-height:1.38;
                ">
                    {latest.get('summary')}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with a2:
        st.metric(
            "AI confidence",
            confidence_text,
        )

    with a3:
        st.metric(
            "Material",
            "YES" if latest.get("material") else "NO",
        )

    if not ai_impacts_df.empty:
        display_ai = ai_impacts_df.head(18).copy()

        display_ai["AI confidence"] = (
            pd.to_numeric(
                display_ai["AI confidence"],
                errors="coerce",
            )
            * 100
        )

        display_ai["Updated"] = (
            pd.to_datetime(
                display_ai["Updated"],
                errors="coerce",
            )
            .dt.strftime("%m-%d %H:%M")
        )

        st.dataframe(
            display_ai[
                [
                    "Type",
                    "Target",
                    "Direction",
                    "AI confidence",
                    "Reason",
                    "Updated",
                ]
            ].style.format(
                {
                    "AI confidence": "{:.0f}%",
                }
            ),
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

st.markdown(
    '<div class="section-title">How DELTAX makes an ETF decision</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="section-kicker">AI explains why. Deterministic price data confirms whether the market agrees.</div>',
    unsafe_allow_html=True,
)

p1, p2, p3, p4, p5 = st.columns(5)

pipeline_cards = [
    (
        "1 · NEWS",
        "Alpaca + Finnhub + Marketaux",
        "Macro, geopolitical and sector catalysts",
    ),
    (
        "2 · AI REGIME",
        f"Confidence ≥ {AI_MIN_CONF:.2f}",
        "Direction + causal market impact",
    ),
    (
        "3 · PRICE",
        f"{PRICE_CONFIRM_MIN}/5 confirmation",
        "Prev close · Open · VWAP · vs SPY · prior momentum",
    ),
    (
        "4 · RISK",
        "$4K / entry",
        "Exit-first · daily gates · no late entries",
    ),
    (
        "5 · EXECUTION",
        "Alpaca paper",
        "LONG strongest · SHORT weakest",
    ),
]

for column, (head, big, small) in zip(
    [p1, p2, p3, p4, p5],
    pipeline_cards,
):
    with column:
        st.markdown(
            f"""
            <div class="rule-card">
                <div class="decision-head">{head}</div>

                <div style="
                    font-size:17px;
                    font-weight:850;
                    color:#FFFFFF;
                ">
                    {big}
                </div>

                <div class="small-muted" style="margin-top:5px;">
                    {small}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# =============================================================================
# RISK ENGINE
# =============================================================================

st.markdown(
    '<div class="section-title">Risk engine</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="section-kicker">Automatic protection runs before fresh entries are considered.</div>',
    unsafe_allow_html=True,
)

r1, r2, r3, r4, r5, r6 = st.columns(6)

r1.metric(
    "Hard stop",
    f"{STOP_LOSS_PCT:.1f}%",
)

r2.metric(
    "Take profit",
    f"+{TAKE_PROFIT_PCT:.1f}%",
)

r3.metric(
    "No new entries",
    f"{NO_NEW_ENTRY_DD:.0f}% day",
)

r4.metric(
    "Kill switch",
    f"{KILL_SWITCH_DD:.0f}% day",
)

r5.metric(
    "Entry block",
    f"{ENTRY_BLOCK_TIME_ET} ET",
)

r6.metric(
    "Flat by",
    f"{EOD_EXIT_TIME_ET} ET",
)

if day_pct <= KILL_SWITCH_DD:
    st.error(
        "KILL SWITCH ACTIVE · Managed ETF positions should be closed and new entries blocked."
    )

elif day_pct <= NO_NEW_ENTRY_DD:
    st.warning(
        "DAILY RISK GATE ACTIVE · Exits allowed, new entries blocked."
    )

else:
    st.success(
        "Risk gate normal · New entries may be considered when AI + price confirmation pass."
    )


# =============================================================================
# RECENT ORDERS / ACTIONS
# =============================================================================

st.markdown(
    '<div class="section-title">Recent ETF orders & actions</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="section-kicker">Entry and exit activity from the EVENT account. DELTAX orders are identified by client_order_id prefixes.</div>',
    unsafe_allow_html=True,
)

etf_orders = []

for order in orders:
    client_order_id = str(order.get("client_order_id") or "")
    symbol = str(order.get("symbol") or "")

    if (
        client_order_id.startswith("dxe-etf-")
        or client_order_id.startswith("dxe-etfx-")
        or symbol in MANAGED_ETFS
    ):
        etf_orders.append(order)

if etf_orders:
    order_rows = []

    for order in etf_orders[:50]:
        client_order_id = str(order.get("client_order_id") or "")

        if client_order_id.startswith("dxe-etfx-"):
            action = "EXIT"

        elif client_order_id.startswith("dxe-etf-"):
            action = "ENTRY"

        else:
            action = "ADOPTED / MANUAL"

        order_rows.append(
            {
                "Time": order.get("created_at"),
                "Action": action,
                "Symbol": order.get("symbol"),
                "Side": str(order.get("side") or "").upper(),
                "Qty": order.get("qty"),
                "Status": str(order.get("status") or "").upper(),
                "Filled": order.get("filled_qty"),
                "Avg fill": order.get("filled_avg_price"),
                "Client order ID": client_order_id,
            }
        )

    orders_df = pd.DataFrame(order_rows)

    orders_df["Time"] = (
        pd.to_datetime(
            orders_df["Time"],
            errors="coerce",
            utc=True,
        )
        .dt.tz_convert(NY)
        .dt.strftime("%m-%d %H:%M:%S ET")
    )

    st.dataframe(
        orders_df,
        use_container_width=True,
        hide_index=True,
        height=420,
    )

else:
    st.info(
        "No ETF orders found in the latest Alpaca order history."
    )


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
    st.code(
        latest_log_lines(160),
        language="text",
    )

with st.expander("System / data status"):
    st.json(
        {
            "logo_expected_at": str(LOGO_FILE),
            "logo_found": LOGO_FILE.exists(),
            "alpaca_event_account": "OK" if not account_err else account_err,
            "alpaca_clock": "OK" if not clock_err else clock_err,
            "alpaca_positions": "OK" if not positions_err else positions_err,
            "alpaca_orders": "OK" if not orders_err else orders_err,
            "neon_market_ai": "OK" if not ai_err else ai_err,
            "etf_log": str(LOG_FILE),
            "market_open": bool(clock.get("is_open")),
            "riga_time": now_riga.isoformat(),
            "new_york_time": now_et.isoformat(),
        }
    )

st.caption(
    "DELTAX · Alpaca AI Trading Agents Hackathon · "
    "AI explains market causality; deterministic market data confirms execution."
)
