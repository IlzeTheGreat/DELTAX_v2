# dashboard/app.py
# DELTAX jury dashboard
#
# Read-only dashboard. It reads Neon only and never submits/cancels orders.
#
# Local:
#   streamlit run dashboard/app.py
#
# Streamlit Cloud:
#   Set DATABASE_URL in app Secrets.
#   Prefer a READ-ONLY Neon role for the dashboard.

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import psycopg
import streamlit as st
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from psycopg.rows import dict_row


load_dotenv()

NYSE_TZ = ZoneInfo("America/New_York")
NYSE_CALENDAR = mcal.get_calendar("NYSE")

LOGO_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "2026_Deltax_AI.png"
)


def as_nyse_time(value):
    """Return timestamp formatted in America/New_York."""
    if value is None or pd.isna(value):
        return "—"

    try:
        ts = pd.Timestamp(value)
    except Exception:
        return "—"

    if pd.isna(ts):
        return "—"

    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")

    ts = ts.tz_convert("America/New_York")

    if pd.isna(ts):
        return "—"

    return ts.strftime("%Y-%m-%d %H:%M:%S %Z")


def nyse_market_state():
    """Current NYSE session state and next open/close using exchange calendar."""
    now_utc = pd.Timestamp.now(tz="UTC")
    now_et = now_utc.tz_convert("America/New_York")

    start = (now_et - pd.Timedelta(days=7)).date()
    end = (now_et + pd.Timedelta(days=14)).date()

    schedule = NYSE_CALENDAR.schedule(
        start_date=start,
        end_date=end,
    )

    if schedule.empty:
        return {
            "now_et": now_et,
            "is_open": False,
            "label": "UNKNOWN",
            "next_event": None,
            "next_event_label": None,
            "countdown_seconds": None,
        }

    current_session = None
    for session_date, row in schedule.iterrows():
        market_open = row["market_open"]
        market_close = row["market_close"]
        if market_open <= now_utc <= market_close:
            current_session = (session_date, row)
            break

    if current_session is not None:
        _, row = current_session
        next_event = row["market_close"]
        return {
            "now_et": now_et,
            "is_open": True,
            "label": "OPEN",
            "next_event": next_event,
            "next_event_label": "Closes in",
            "countdown_seconds": max(
                0,
                int((next_event - now_utc).total_seconds()),
            ),
        }

    future_opens = schedule[
        schedule["market_open"] > now_utc
    ]

    if future_opens.empty:
        return {
            "now_et": now_et,
            "is_open": False,
            "label": "CLOSED",
            "next_event": None,
            "next_event_label": None,
            "countdown_seconds": None,
        }

    next_open = future_opens.iloc[0]["market_open"]

    return {
        "now_et": now_et,
        "is_open": False,
        "label": "CLOSED",
        "next_event": next_open,
        "next_event_label": "Opens in",
        "countdown_seconds": max(
            0,
            int((next_open - now_utc).total_seconds()),
        ),
    }


def countdown_text(seconds):
    if seconds is None:
        return "—"

    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


st.set_page_config(
    page_title="DELTAX | AI Trading Agent",
    page_icon="🏆",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        .block-container {
            padding-top: 2.6rem;
            padding-bottom: 2rem;
        }

        .dx-section {
            border: 1px solid rgba(128,128,128,0.20);
            border-radius: 14px;
            padding: 14px 16px 10px 16px;
            margin: 8px 0 14px 0;
            background: rgba(255,255,255,0.015);
        }

        .dx-section-title {
            font-size: 0.82rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            opacity: 0.72;
            margin-bottom: 4px;
        }

        .dx-section-subtitle {
            font-size: 0.88rem;
            opacity: 0.62;
            margin-bottom: 6px;
        }

        .dx-flow {
            border-left: 3px solid rgba(100,149,237,0.55);
            padding-left: 14px;
            margin: 8px 0 14px 0;
        }

        .dx-muted {
            opacity: 0.65;
            font-size: 0.88rem;
        }

        .dx-card-title {
            font-weight: 700;
            font-size: 1.02rem;
            margin-bottom: 2px;
        }

        div[data-testid="stMetric"] {
            border: 1px solid rgba(128,128,128,0.16);
            border-radius: 12px;
            padding: 10px 12px;
            background: rgba(255,255,255,0.012);
        }

        div[data-testid="stExpander"] {
            border-radius: 12px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if value:
        return value

    try:
        value = st.secrets["DATABASE_URL"]
    except Exception:
        value = None

    if not value:
        st.error("DATABASE_URL is not configured.")
        st.stop()

    return value


def query(sql: str, params=None):
    with psycopg.connect(
        database_url(),
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params or ())
            return [dict(row) for row in cursor.fetchall()]


@st.cache_data(ttl=20)
def load_dashboard():
    control = query(
        """
        SELECT *
        FROM bot_control
        WHERE id = 1
        """
    )

    portfolio = query(
        """
        SELECT *
        FROM portfolio_snapshots
        ORDER BY captured_at DESC
        LIMIT 1
        """
    )

    positions = query(
        """
        SELECT
            p.*,
            i.company_name,
            i.sector
        FROM positions p
        LEFT JOIN instruments i
          ON i.symbol = p.symbol
        ORDER BY COALESCE(p.opened_at, p.created_at) DESC
        LIMIT 100
        """
    )

    decisions = query(
        """
        SELECT
            ti.id AS intent_id,
            ti.intent_type,
            ti.asset_class,
            ti.strategy,
            ti.direction,
            ti.symbol,
            ti.side,
            ti.quantity,
            ti.order_type,
            ti.limit_price,
            ti.planned_entry_price,
            ti.stop_loss_price,
            ti.take_profit_price,
            ti.premium_type,
            ti.net_premium,
            ti.max_profit,
            ti.max_loss,
            ti.status AS intent_status,
            ti.created_at AS intent_created_at,
            ti.metadata AS intent_metadata,

            tt.id AS thesis_id,
            tt.status AS thesis_status,
            tt.signal_at,
            tt.signal_price,
            tt.reference_vwap,
            tt.deviation_pct,
            tt.atr_14,
            tt.atr_pct,
            tt.weak_indices_count,
            tt.technical_state,
            tt.market_state,
            tt.sector_state,
            tt.risk_state,
            tt.confirmation_price,
            tt.confirmation_passed,
            tt.rejection_reasons,

            aa.direction AS ai_direction,
            aa.confidence AS ai_confidence,
            aa.raw_response AS ai_raw_response,

            bo.id AS broker_order_id,
            bo.alpaca_order_id,
            bo.status AS broker_status,
            bo.filled_quantity,
            bo.filled_average_price,
            bo.submitted_at,
            bo.filled_at,

            p.id AS position_id,
            p.status AS position_status,
            p.average_entry_price,
            p.current_price,
            p.realized_pnl,
            p.unrealized_pnl,
            p.close_reason
        FROM trade_intents ti
        JOIN trade_theses tt
          ON tt.id = ti.trade_thesis_id
        LEFT JOIN ai_analyses aa
          ON aa.id = tt.ai_analysis_id
        LEFT JOIN broker_orders bo
          ON bo.trade_intent_id = ti.id
        LEFT JOIN positions p
          ON p.id = ti.position_id
          OR p.entry_intent_id = ti.id
        ORDER BY ti.created_at DESC
        LIMIT 150
        """
    )

    option_legs = query(
        """
        SELECT
            til.trade_intent_id,
            til.leg_number,
            til.contract_symbol,
            til.action,
            til.ratio_quantity,
            til.option_type,
            til.strike,
            til.expiration_date,
            til.reference_bid,
            til.reference_ask,
            til.reference_mid
        FROM trade_intent_legs til
        JOIN trade_intents ti
          ON ti.id = til.trade_intent_id
        ORDER BY ti.created_at DESC, til.leg_number
        LIMIT 300
        """
    )

    risk_events = query(
        """
        SELECT *
        FROM risk_events
        ORDER BY occurred_at DESC
        LIMIT 50
        """
    )

    recent_theses = query(
        """
        SELECT
            tt.symbol,
            tt.strategy,
            tt.direction,
            tt.status,
            tt.signal_at,
            tt.signal_price,
            tt.reference_vwap,
            tt.deviation_pct,
            tt.confirmation_price,
            tt.confirmation_passed,
            tt.rejection_reasons,
            aa.direction AS ai_direction,
            aa.confidence AS ai_confidence,
            aa.raw_response AS ai_raw_response
        FROM trade_theses tt
        LEFT JOIN ai_analyses aa
          ON aa.id = tt.ai_analysis_id
        ORDER BY tt.created_at DESC
        LIMIT 50
        """
    )

    return {
        "control": control[0] if control else {},
        "portfolio": portfolio[0] if portfolio else {},
        "positions": positions,
        "decisions": decisions,
        "option_legs": option_legs,
        "risk_events": risk_events,
        "recent_theses": recent_theses,
    }


def money(value):
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return str(value)


def pct(value):
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:+.2f}%"
    except Exception:
        return str(value)


def compact_json(value):
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return value

    if isinstance(value, dict):
        preferred = [
            "reasoning",
            "reason",
            "summary",
            "thesis",
            "rationale",
            "news_summary",
            "impact",
        ]
        for key in preferred:
            if value.get(key):
                return str(value[key])

    return json.dumps(value, ensure_ascii=False, default=str)


def ai_summary(row):
    raw = row.get("ai_raw_response")
    direction = row.get("ai_direction")
    confidence = row.get("ai_confidence")

    if not direction and not raw:
        return "No material AI news signal used."

    head = f"{str(direction).upper()}" if direction else "AI"
    if confidence is not None:
        head += f" · confidence {float(confidence):.2f}"

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {"raw": raw}

    detail = ""
    if isinstance(raw, dict):
        for key in (
            "reasoning",
            "reason",
            "summary",
            "market_impact",
            "thesis",
            "rationale",
        ):
            if raw.get(key):
                detail = str(raw[key])
                break

        flags = []
        for key in ("meaningful", "sufficient"):
            if key in raw:
                flags.append(f"{key}={raw[key]}")
        if flags:
            head += " · " + " · ".join(flags)

    return head + (f"\n\n{detail}" if detail else "")


def technical_summary(row):
    parts = []

    if row.get("signal_price") is not None:
        parts.append(f"Signal ${float(row['signal_price']):.2f}")

    if row.get("reference_vwap") is not None:
        parts.append(f"VWAP ${float(row['reference_vwap']):.2f}")

    if row.get("deviation_pct") is not None:
        parts.append(
            f"VWAP deviation {float(row['deviation_pct']) * 100:+.2f}%"
        )

    if row.get("atr_14") is not None:
        parts.append(f"ATR14 {float(row['atr_14']):.2f}")

    if row.get("weak_indices_count") is not None:
        parts.append(f"Weak indices {row['weak_indices_count']}")

    if row.get("confirmation_passed") is not None:
        parts.append(
            "10m confirmation PASS"
            if row["confirmation_passed"]
            else "10m confirmation FAIL"
        )

    state = row.get("technical_state")
    if state:
        parts.append("Technical state: " + compact_json(state))

    return " · ".join(parts) if parts else "Technical state unavailable."


def badge(value):
    return str(value or "—").upper()


data = load_dashboard()
control = data["control"]
portfolio = data["portfolio"]

if LOGO_PATH.exists():
    logo_b64 = base64.b64encode(
        LOGO_PATH.read_bytes()
    ).decode("ascii")

    st.markdown(
        f"""
        <div style="
            display:flex;
            align-items:center;
            gap:18px;
            margin:0 0 4px 0;
        ">
            <img
                src="data:image/png;base64,{logo_b64}"
                style="
                    width:110px;
                    height:auto;
                    display:block;
                "
            />
            <div style="
                font-size:42px;
                line-height:1;
                font-weight:800;
                letter-spacing:1.5px;
                margin:0;
            ">
                DELTAX V2
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        """
        <div style="
            font-size:42px;
            line-height:1;
            font-weight:800;
            letter-spacing:1.5px;
            margin:0 0 4px 0;
        ">
            DELTAX
        </div>
        """,
        unsafe_allow_html=True,
    )

st.caption(
    "Autonomous AI-assisted paper trading agent · "
    "technical signals + news intelligence + deterministic risk/execution"
)

st.markdown(
    """
    <div class="dx-section">
        <div class="dx-section-title">1 · Agent & Portfolio Status</div>
        <div class="dx-section-subtitle">
            Current operating state, capital and portfolio-level risk.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

status_cols = st.columns([1.3, 1, 1, 1, 1, 1])

with status_cols[0]:
    if control.get("kill_switch_active"):
        st.error("KILL SWITCH ACTIVE")
    elif control.get("execution_enabled"):
        st.success("AGENT ARMED · PAPER")
    else:
        st.warning("AGENT DISARMED")

with status_cols[1]:
    st.metric("Equity", money(portfolio.get("equity")))

with status_cols[2]:
    st.metric(
        "Daily P&L",
        money(portfolio.get("daily_pnl")),
        pct(portfolio.get("daily_pnl_pct")),
    )

with status_cols[3]:
    st.metric(
        "Open positions",
        int(portfolio.get("open_stock_positions") or 0)
        + int(portfolio.get("open_options_positions") or 0),
    )

with status_cols[4]:
    st.metric(
        "Stock risk",
        money(portfolio.get("stock_open_risk")),
    )

with status_cols[5]:
    st.metric(
        "Options risk",
        money(portfolio.get("options_open_risk")),
    )

st.markdown(
    """
    <div class="dx-section">
        <div class="dx-section-title">2 · Market Session</div>
        <div class="dx-section-subtitle">
            NYSE session status and exchange-local timing used by the trading agent.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

market = nyse_market_state()

market_cols = st.columns([1.2, 1.2, 1.5, 1.5])

with market_cols[0]:
    if market["is_open"]:
        st.success("NYSE OPEN")
    else:
        st.warning("NYSE CLOSED")

with market_cols[1]:
    st.metric(
        "NYSE time",
        market["now_et"].strftime("%H:%M:%S"),
    )
    st.caption(
        market["now_et"].strftime("%Y-%m-%d %Z")
    )

with market_cols[2]:
    if market["next_event"] is not None:
        st.metric(
            market["next_event_label"],
            countdown_text(
                market["countdown_seconds"]
            ),
        )
        st.caption(
            as_nyse_time(market["next_event"])
        )
    else:
        st.metric("Next session", "—")

with market_cols[3]:
    st.metric(
        "Last portfolio snapshot",
        as_nyse_time(portfolio.get("captured_at")).split(" ")[1]
        if portfolio.get("captured_at") else "—",
    )
    st.caption(
        as_nyse_time(portfolio.get("captured_at"))
        if portfolio.get("captured_at")
        else "No snapshot yet"
    )


tabs = st.tabs(
    [
        "Decision Feed",
        "Portfolio",
        "Signal Pipeline",
        "Risk & System",
    ]
)

legs_by_intent = {}
for leg in data["option_legs"]:
    legs_by_intent.setdefault(
        str(leg["trade_intent_id"]), []
    ).append(leg)


with tabs[0]:
    st.markdown(
        """
        <div class="dx-section">
            <div class="dx-section-title">3 · Decision Feed</div>
            <div class="dx-section-subtitle">
                Final trade decisions, execution state and the technical + AI reasoning behind each action.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    decisions = data["decisions"]

    st.markdown(
        """
        <div class="dx-flow">
            <b>Decision pipeline:</b>
            Technical signal → News / AI interpretation → Deterministic risk gates
            → Trade intent → Alpaca execution → Position / exit monitoring
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not decisions:
        st.info(
            "No trade intents yet. The feed will populate when the "
            "agent creates its first entry or exit decision."
        )

    for row in decisions:
        intent_id = str(row["intent_id"])
        timestamp = as_nyse_time(
            row.get("filled_at")
            or row.get("submitted_at")
            or row.get("intent_created_at")
        )

        filled_price = row.get("filled_average_price")
        planned_price = (
            row.get("planned_entry_price")
            or row.get("limit_price")
        )

        title = (
            f"{row['symbol']} · "
            f"{badge(row['direction'])} · "
            f"{badge(row['asset_class'])} · "
            f"{badge(row['strategy'])} · "
            f"{badge(row['intent_type'])} · "
            f"{badge(row.get('broker_status') or row['intent_status'])}"
        )

        with st.expander(title):
            c1, c2, c3, c4 = st.columns(4)

            c1.metric(
                "Price",
                money(filled_price or planned_price),
            )
            c2.metric(
                "Quantity",
                str(row.get("filled_quantity")
                    or row.get("quantity")
                    or "—"),
            )
            c3.metric(
                "Max risk",
                money(row.get("max_loss")),
            )
            c4.metric(
                "P&L",
                money(
                    row.get("realized_pnl")
                    if row.get("position_status") == "closed"
                    else row.get("unrealized_pnl")
                ),
            )

            st.markdown(
                f"**Status:** `{badge(row.get('broker_status') or row['intent_status'])}`  "
                f"**Time:** `{timestamp}`  "
                f"**Side:** `{row.get('side') or 'multi-leg'}`"
            )

            if row["asset_class"] == "stock":
                st.markdown(
                    f"**Entry/plan:** {money(planned_price)} · "
                    f"**Stop:** {money(row.get('stop_loss_price'))} · "
                    f"**Target:** {money(row.get('take_profit_price'))}"
                )
            else:
                legs = legs_by_intent.get(intent_id, [])
                if legs:
                    leg_df = pd.DataFrame(
                        [
                            {
                                "Action": leg["action"],
                                "Contract": leg["contract_symbol"],
                                "Type": leg["option_type"],
                                "Strike": float(leg["strike"]),
                                "Expiry": leg["expiration_date"],
                                "Ref bid": leg["reference_bid"],
                                "Ref ask": leg["reference_ask"],
                            }
                            for leg in legs
                        ]
                    )
                    st.dataframe(
                        leg_df,
                        use_container_width=True,
                        hide_index=True,
                    )

                st.markdown(
                    f"**Premium:** `{row.get('premium_type')}` · "
                    f"**Net premium:** {money(row.get('net_premium'))} · "
                    f"**Max profit:** {money(row.get('max_profit'))} · "
                    f"**Max loss:** {money(row.get('max_loss'))}"
                )

            st.markdown("---")
            reason_cols = st.columns(2)

            with reason_cols[0]:
                st.markdown("#### Technical signal")
                st.write(technical_summary(row))

            with reason_cols[1]:
                st.markdown("#### AI / news interpretation")
                st.write(ai_summary(row))

            st.markdown("#### Risk & execution context")
            risk_parts = []

            if row.get("market_state"):
                risk_parts.append(
                    "**Market:** " + compact_json(
                        row.get("market_state")
                    )
                )

            if row.get("sector_state"):
                risk_parts.append(
                    "**Sector:** " + compact_json(
                        row.get("sector_state")
                    )
                )

            if row.get("risk_state"):
                risk_parts.append(
                    "**Risk:** " + compact_json(
                        row.get("risk_state")
                    )
                )

            if risk_parts:
                for part in risk_parts:
                    st.markdown(part)
            else:
                st.caption(
                    "No additional market/sector risk context stored for this decision."
                )

            with st.expander("Full audit trail"):
                st.json(
                    {
                        "technical_state": row.get("technical_state"),
                        "market_state": row.get("market_state"),
                        "sector_state": row.get("sector_state"),
                        "risk_state": row.get("risk_state"),
                        "intent_metadata": row.get("intent_metadata"),
                        "rejection_reasons": row.get("rejection_reasons"),
                        "alpaca_order_id": row.get("alpaca_order_id"),
                    }
                )


with tabs[1]:
    st.markdown(
        """
        <div class="dx-section">
            <div class="dx-section-title">4 · Portfolio</div>
            <div class="dx-section-subtitle">
                Current open positions and their live P&L / risk state.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    open_rows = [
        row
        for row in data["positions"]
        if row.get("status") in ("opening", "open", "closing")
    ]

    if not open_rows:
        st.info("No open positions.")

    else:
        df = pd.DataFrame(
            [
                {
                    "Symbol": row["symbol"],
                    "Company": row.get("company_name"),
                    "Sector": row.get("sector"),
                    "Type": row["asset_class"],
                    "Strategy": row["strategy"],
                    "Direction": row["direction"],
                    "Status": row["status"],
                    "Qty": row["quantity"],
                    "Entry": row["average_entry_price"],
                    "Current": row["current_price"],
                    "Unrealized P&L": row["unrealized_pnl"],
                    "Max risk": row["initial_max_loss"],
                    "Opened (NYSE)": as_nyse_time(
                        row["opened_at"]
                    ),
                }
                for row in open_rows
            ]
        )
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
        )


with tabs[2]:
    st.markdown(
        """
        <div class="dx-section">
            <div class="dx-section-title">5 · Signal Pipeline</div>
            <div class="dx-section-subtitle">
                Latest trade theses before execution, including rejected or unconfirmed ideas.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="dx-flow">
            This view explains <b>why the agent did not trade as well as why it did</b>.
            It exposes rejected theses, confirmation failures and AI confidence.
        </div>
        """,
        unsafe_allow_html=True,
    )

    theses = data["recent_theses"]

    if not theses:
        st.info("No theses generated yet.")
    else:
        df = pd.DataFrame(
            [
                {
                    "Time (NYSE)": as_nyse_time(
                        row["signal_at"]
                    ),
                    "Symbol": row["symbol"],
                    "Strategy": row["strategy"],
                    "Direction": row["direction"],
                    "Status": row["status"],
                    "Signal": row["signal_price"],
                    "VWAP": row["reference_vwap"],
                    "Deviation %": (
                        float(row["deviation_pct"]) * 100
                        if row["deviation_pct"] is not None
                        else None
                    ),
                    "10m confirm": row["confirmation_passed"],
                    "AI direction": row["ai_direction"],
                    "AI confidence": row["ai_confidence"],
                    "Rejected because": ", ".join(
                        row.get("rejection_reasons") or []
                    ),
                }
                for row in theses
            ]
        )
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
        )


with tabs[3]:
    st.markdown(
        """
        <div class="dx-section">
            <div class="dx-section-title">6 · Risk & System</div>
            <div class="dx-section-subtitle">
                Trading permissions, kill switch, heartbeat and recorded risk events.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Execution",
        "ON" if control.get("execution_enabled") else "OFF",
    )
    c2.metric(
        "New entries",
        "ON" if control.get("new_entries_enabled") else "OFF",
    )
    c3.metric(
        "Kill switch",
        "ACTIVE" if control.get("kill_switch_active") else "OFF",
    )
    c4.metric(
        "Heartbeat",
        as_nyse_time(
            control.get("last_heartbeat_at")
        ),
    )

    if control.get("kill_switch_reason"):
        st.error(
            "Kill switch reason: "
            + str(control["kill_switch_reason"])
        )

    events = data["risk_events"]
    if not events:
        st.success("No risk events recorded.")
    else:
        risk_df = pd.DataFrame(events)

        if "occurred_at" in risk_df.columns:
            risk_df["occurred_at"] = risk_df[
                "occurred_at"
            ].apply(as_nyse_time)

        if "resolved_at" in risk_df.columns:
            risk_df["resolved_at"] = risk_df[
                "resolved_at"
            ].apply(as_nyse_time)

        preferred = [
            "occurred_at",
            "severity",
            "event_code",
            "symbol",
            "message",
            "resolved_at",
        ]
        cols = [
            col for col in preferred
            if col in risk_df.columns
        ]
        st.dataframe(
            risk_df[cols],
            use_container_width=True,
            hide_index=True,
        )


st.divider()
st.caption(
    "DELTAX v2 · Alpaca PAPER trading · read-only jury dashboard · "
    "technical signals + AI news reasoning + deterministic risk controls · all times NYSE/ET"
)
