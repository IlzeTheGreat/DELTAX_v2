from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderSide,
    OrderType,
    TimeInForce,
)
from alpaca.trading.requests import (
    ClosePositionRequest,
    GetCalendarRequest,
    GetOptionContractsRequest,
    MarketOrderRequest,
)


# ============================================================
# DELTAX LIVE EVENT OPTIONS STRATEGY
# ============================================================
#
# Paper-trading rules based on the backtests we ran:
#
#   Event bias threshold:       +/- 2.50%
#   Opening confirmation:       first 10 min moves >= 0.25%
#                               against event bias
#   LONG bias + opening down    -> CALL
#   SHORT bias + opening up     -> PUT
#   Target expiry:              ~7 DTE
#   Strike:                     ATM or ~1% OTM
#   Default exit:               same day around 15:50 ET
#   Overnight whitelist:        APP, LRCX, WDAY, XYZ
#   Whitelist exit:             next trading-day open
#
# IMPORTANT:
# - Default mode is DRY RUN.
# - Actual paper orders require --execute.
# - --manage-exits manages positions opened by this script.
# - State is persisted locally so exits know which trades are overnight.
# ============================================================


# ============================================================
# CONFIG
# ============================================================

MARKET_TZ = ZoneInfo("America/New_York")

# Event began before the first US trading session we want to use as baseline.
EVENT_START_DATE = date(2026, 8, 31)

BIAS_THRESHOLD = 0.025
OPENING_REVERSAL_THRESHOLD = 0.0025
OPENING_WINDOW_MINUTES = 10

TARGET_DTE = 7
DTE_TOLERANCE_DAYS = 3

MAX_RELATIVE_OPTION_SPREAD = 0.25

RISK_PER_TRADE = 0.01
MAX_TOTAL_NEW_PREMIUM_RISK = 0.05
MAX_NEW_POSITIONS = 5

DEFAULT_EXIT_TIME_ET = dt_time(15, 50)
NEXT_OPEN_EXIT_TIME_ET = dt_time(9, 35)

OVERNIGHT_WHITELIST = {
    "APP",
    "LRCX",
    "WDAY",
    "XYZ",
}

NEWS_LOOKBACK_MINUTES = 60

# Very conservative headline guard. Existing DeltaX AI news logic can later
# replace this function without changing the rest of the strategy.
BEARISH_WORDS = {
    "downgrade",
    "cuts guidance",
    "cut guidance",
    "misses",
    "missed",
    "investigation",
    "probe",
    "lawsuit",
    "fraud",
    "recall",
    "bankruptcy",
    "default",
    "warning",
    "plunges",
    "falls",
    "declines",
    "weak",
    "slump",
    "halt",
    "layoffs",
    "layoff",
}

BULLISH_WORDS = {
    "upgrade",
    "raises guidance",
    "raised guidance",
    "beats",
    "beat estimates",
    "record revenue",
    "approval",
    "approved",
    "wins contract",
    "contract win",
    "surges",
    "rises",
    "strong demand",
    "buyback",
    "acquisition",
}

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
SP500_PATH = ROOT_DIR / "sp500.txt"

STATE_PATH = SCRIPT_DIR / "event_options_state.json"
INTENTS_PATH = SCRIPT_DIR / "event_options_intents.json"
SCAN_PATH = SCRIPT_DIR / "event_options_scan.json"


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class Signal:
    symbol: str
    event_bias: str
    prior_relative_return: float
    opening_return_10m: float
    option_type: str


@dataclass
class OptionChoice:
    underlying_symbol: str
    option_symbol: str
    option_type: str
    expiration_date: str
    actual_dte: int
    strike_price: float
    underlying_price: float
    bid: float
    ask: float
    midpoint: float
    relative_spread: float
    selection_mode: str


# ============================================================
# BASIC HELPERS
# ============================================================

def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(MARKET_TZ)


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def load_env() -> None:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env not found: {ENV_PATH}")
    load_dotenv(ENV_PATH)


def env_required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env variable: {name}")
    return value


def load_symbols() -> list[str]:
    if not SP500_PATH.exists():
        raise FileNotFoundError(f"sp500.txt not found: {SP500_PATH}")

    symbols: list[str] = []

    with open(SP500_PATH, "r", encoding="utf-8") as f:
        for line in f:
            symbol = line.strip().upper()
            if not symbol or symbol.startswith("#"):
                continue
            symbols.append(symbol)

    return list(dict.fromkeys(symbols))


def chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"positions": {}}

    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"positions": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


# ============================================================
# CLIENTS
# ============================================================

def build_clients():
    load_env()

    api_key = env_required("ALPACA_API_KEY_PAPER")
    api_secret = env_required("ALPACA_API_SECRET_PAPER")
    trading_url = env_required("ALPACA_TRADING_URL_PAPER")
    data_feed = env_required("ALPACA_DATA_FEED_PAPER")

    trading_url = trading_url.rstrip("/")
    if trading_url.endswith("/v2"):
        trading_url = trading_url[:-3]

    trading_client = TradingClient(
        api_key=api_key,
        secret_key=api_secret,
        paper=False,
        url_override=trading_url,
    )

    stock_client = StockHistoricalDataClient(
        api_key,
        api_secret,
    )

    return (
        trading_client,
        stock_client,
        api_key,
        api_secret,
        data_feed,
    )


# ============================================================
# CALENDAR
# ============================================================

def get_sessions(
    trading_client: TradingClient,
    start_date: date,
    end_date: date,
) -> list[date]:

    rows = trading_client.get_calendar(
        GetCalendarRequest(
            start=start_date,
            end=end_date,
        )
    )

    return [x.date for x in rows]


def previous_sessions(
    trading_client: TradingClient,
    current_date: date,
) -> list[date]:

    sessions = get_sessions(
        trading_client,
        EVENT_START_DATE,
        current_date,
    )

    return [d for d in sessions if d < current_date]


def next_trading_day(
    trading_client: TradingClient,
    current_date: date,
) -> date | None:

    sessions = get_sessions(
        trading_client,
        current_date + timedelta(days=1),
        current_date + timedelta(days=10),
    )

    return sessions[0] if sessions else None


# ============================================================
# STOCK MARKET DATA
# ============================================================

def fetch_stock_bars(
    stock_client: StockHistoricalDataClient,
    symbols: list[str],
    data_feed: str,
    start_et: datetime,
    end_et: datetime,
) -> dict[str, list[Any]]:

    out: dict[str, list[Any]] = {}

    for batch in chunks(symbols, 100):
        req = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_et.astimezone(timezone.utc),
            end=end_et.astimezone(timezone.utc),
            feed=data_feed,
            adjustment="raw",
        )

        response = stock_client.get_stock_bars(req)
        data = response.data if hasattr(response, "data") else {}

        for symbol in batch:
            bars = data.get(symbol, [])
            out.setdefault(symbol, []).extend(bars)

    return out


def regular_session_bars(
    bars: list[Any],
    trading_date: date,
) -> list[Any]:

    out = []

    for bar in bars:
        ts = bar.timestamp

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        ts_et = ts.astimezone(MARKET_TZ)

        if ts_et.date() != trading_date:
            continue

        if not (
            dt_time(9, 30)
            <= ts_et.time()
            < dt_time(16, 0)
        ):
            continue

        out.append(bar)

    return sorted(
        out,
        key=lambda b: b.timestamp,
    )


def build_prior_event_bias(
    all_bars: dict[str, list[Any]],
    symbols: list[str],
    prior_dates: list[date],
) -> dict[str, dict[str, Any]]:

    if not prior_dates:
        return {
            symbol: {
                "event_bias": "NONE",
                "stock_cumulative_return": 0.0,
                "universe_cumulative_return": 0.0,
                "relative_return": 0.0,
            }
            for symbol in symbols
        }

    daily_returns_by_symbol: dict[str, dict[date, float]] = {}
    market_daily: dict[date, list[float]] = {
        d: [] for d in prior_dates
    }

    for symbol in symbols:
        daily_returns_by_symbol[symbol] = {}

        for d in prior_dates:
            day_bars = regular_session_bars(
                all_bars.get(symbol, []),
                d,
            )

            if not day_bars:
                continue

            day_open = float(day_bars[0].open)
            day_close = float(day_bars[-1].close)

            if day_open <= 0:
                continue

            r = day_close / day_open - 1.0
            daily_returns_by_symbol[symbol][d] = r
            market_daily[d].append(r)

    market_cum = 1.0

    for d in prior_dates:
        values = market_daily.get(d, [])
        if not values:
            continue
        market_cum *= 1.0 + sum(values) / len(values)

    market_cumulative_return = market_cum - 1.0

    result = {}

    for symbol in symbols:
        stock_cum = 1.0
        any_data = False

        for d in prior_dates:
            if d not in daily_returns_by_symbol[symbol]:
                continue
            any_data = True
            stock_cum *= (
                1.0
                + daily_returns_by_symbol[symbol][d]
            )

        stock_return = (
            stock_cum - 1.0
            if any_data
            else 0.0
        )

        relative = (
            stock_return
            - market_cumulative_return
        )

        if relative >= BIAS_THRESHOLD:
            bias = "LONG"
        elif relative <= -BIAS_THRESHOLD:
            bias = "SHORT"
        else:
            bias = "NONE"

        result[symbol] = {
            "event_bias": bias,
            "stock_cumulative_return": stock_return,
            "universe_cumulative_return": market_cumulative_return,
            "relative_return": relative,
        }

    return result


def build_live_signals(
    all_bars: dict[str, list[Any]],
    bias: dict[str, dict[str, Any]],
    current_date: date,
) -> list[Signal]:

    signals: list[Signal] = []

    for symbol, bias_row in bias.items():
        event_bias = bias_row["event_bias"]

        if event_bias == "NONE":
            continue

        today = regular_session_bars(
            all_bars.get(symbol, []),
            current_date,
        )

        if len(today) < 2:
            continue

        opening = today[:2]

        day_open = float(opening[0].open)
        ten_min_close = float(opening[-1].close)

        if day_open <= 0:
            continue

        opening_return = (
            ten_min_close / day_open - 1.0
        )

        if (
            event_bias == "LONG"
            and opening_return
            <= -OPENING_REVERSAL_THRESHOLD
        ):
            signals.append(
                Signal(
                    symbol=symbol,
                    event_bias="LONG",
                    prior_relative_return=float(
                        bias_row["relative_return"]
                    ),
                    opening_return_10m=opening_return,
                    option_type="call",
                )
            )

        elif (
            event_bias == "SHORT"
            and opening_return
            >= OPENING_REVERSAL_THRESHOLD
        ):
            signals.append(
                Signal(
                    symbol=symbol,
                    event_bias="SHORT",
                    prior_relative_return=float(
                        bias_row["relative_return"]
                    ),
                    opening_return_10m=opening_return,
                    option_type="put",
                )
            )

    return signals


# ============================================================
# NEWS CONFLICT GUARD
# ============================================================

def recent_news(
    symbol: str,
    api_key: str,
    api_secret: str,
    now_utc: datetime,
) -> list[dict[str, Any]]:

    start = (
        now_utc
        - timedelta(
            minutes=NEWS_LOOKBACK_MINUTES
        )
    )

    params = {
        "symbols": symbol,
        "start": start.isoformat(),
        "end": now_utc.isoformat(),
        "sort": "desc",
        "limit": 20,
        "include_content": "false",
    }

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }

    try:
        response = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            params=params,
            headers=headers,
            timeout=15,
        )

        response.raise_for_status()

        payload = response.json()

        return payload.get("news", [])

    except Exception as exc:
        print(
            f"  NEWS WARNING {symbol}: "
            f"{repr(exc)}"
        )
        return []


def news_conflicts(
    signal: Signal,
    api_key: str,
    api_secret: str,
) -> tuple[bool, list[str]]:

    articles = recent_news(
        signal.symbol,
        api_key,
        api_secret,
        datetime.now(timezone.utc),
    )

    conflicts = []

    for article in articles:
        headline = str(
            article.get("headline") or ""
        ).lower()

        summary = str(
            article.get("summary") or ""
        ).lower()

        text = headline + " " + summary

        if signal.event_bias == "LONG":
            matched = [
                w
                for w in BEARISH_WORDS
                if w in text
            ]

        else:
            matched = [
                w
                for w in BULLISH_WORDS
                if w in text
            ]

        if matched:
            conflicts.append(
                article.get("headline") or ""
            )

    return bool(conflicts), conflicts


# ============================================================
# OPTIONS
# ============================================================

def fetch_option_contracts(
    trading_client: TradingClient,
    signal: Signal,
    current_date: date,
) -> list[Any]:

    ctype = (
        ContractType.CALL
        if signal.option_type == "call"
        else ContractType.PUT
    )

    req = GetOptionContractsRequest(
        underlying_symbols=[signal.symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=(
            current_date
            + timedelta(
                days=TARGET_DTE
                - DTE_TOLERANCE_DAYS
            )
        ),
        expiration_date_lte=(
            current_date
            + timedelta(
                days=TARGET_DTE
                + DTE_TOLERANCE_DAYS
            )
        ),
        type=ctype,
        limit=10000,
    )

    response = trading_client.get_option_contracts(
        req
    )

    return (
        getattr(
            response,
            "option_contracts",
            None,
        )
        or []
    )


def latest_option_quotes(
    option_symbols: list[str],
    api_key: str,
    api_secret: str,
) -> dict[str, dict[str, Any]]:

    if not option_symbols:
        return {}

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }

    out = {}

    for batch in chunks(
        option_symbols,
        100,
    ):
        params = {
            "symbols": ",".join(batch),
        }

        response = requests.get(
            "https://data.alpaca.markets/v1beta1/options/quotes/latest",
            params=params,
            headers=headers,
            timeout=20,
        )

        response.raise_for_status()

        payload = response.json()

        # Alpaca response is normally {"quotes": {symbol: {...}}}
        quotes = payload.get(
            "quotes",
            payload,
        )

        if isinstance(quotes, dict):
            out.update(quotes)

    return out


def quote_bid_ask(
    quote_payload: dict[str, Any],
) -> tuple[float, float]:

    bid = (
        quote_payload.get("bp")
        or quote_payload.get("bid_price")
        or 0
    )

    ask = (
        quote_payload.get("ap")
        or quote_payload.get("ask_price")
        or 0
    )

    return float(bid), float(ask)


def choose_option(
    trading_client: TradingClient,
    signal: Signal,
    underlying_price: float,
    current_date: date,
    api_key: str,
    api_secret: str,
) -> OptionChoice | None:

    contracts = fetch_option_contracts(
        trading_client,
        signal,
        current_date,
    )

    if not contracts:
        return None

    candidates = []

    for c in contracts:
        if not getattr(c, "tradable", False):
            continue

        dte = (
            c.expiration_date
            - current_date
        ).days

        strike = float(c.strike_price)

        atm_distance = abs(
            strike / underlying_price - 1.0
        )

        if signal.option_type == "call":
            otm_1_target = (
                underlying_price * 1.01
            )
        else:
            otm_1_target = (
                underlying_price * 0.99
            )

        otm_distance = abs(
            strike / otm_1_target - 1.0
        )

        mode = (
            "ATM"
            if atm_distance <= otm_distance
            else "OTM_1"
        )

        distance = min(
            atm_distance,
            otm_distance,
        )

        candidates.append(
            {
                "contract": c,
                "dte": dte,
                "strike": strike,
                "mode": mode,
                "distance": distance,
                "dte_distance": abs(
                    dte - TARGET_DTE
                ),
            }
        )

    candidates.sort(
        key=lambda x: (
            x["dte_distance"],
            x["distance"],
        )
    )

    # Quote only a manageable shortlist.
    shortlist = candidates[:12]

    quotes = latest_option_quotes(
        [
            x["contract"].symbol
            for x in shortlist
        ],
        api_key,
        api_secret,
    )

    ranked = []

    for x in shortlist:
        symbol = x["contract"].symbol
        quote = quotes.get(symbol, {})

        bid, ask = quote_bid_ask(quote)

        if ask <= 0:
            continue

        midpoint = (
            (bid + ask) / 2.0
            if bid > 0
            else ask
        )

        relative_spread = (
            (ask - bid) / midpoint
            if bid > 0
            and midpoint > 0
            else 1.0
        )

        if (
            relative_spread
            > MAX_RELATIVE_OPTION_SPREAD
        ):
            continue

        ranked.append(
            (
                relative_spread,
                x["dte_distance"],
                x["distance"],
                x,
                bid,
                ask,
                midpoint,
            )
        )

    if not ranked:
        return None

    ranked.sort(
        key=lambda x: (
            x[0],
            x[1],
            x[2],
        )
    )

    (
        relative_spread,
        _,
        _,
        x,
        bid,
        ask,
        midpoint,
    ) = ranked[0]

    c = x["contract"]

    return OptionChoice(
        underlying_symbol=signal.symbol,
        option_symbol=c.symbol,
        option_type=signal.option_type,
        expiration_date=str(
            c.expiration_date
        ),
        actual_dte=x["dte"],
        strike_price=x["strike"],
        underlying_price=underlying_price,
        bid=bid,
        ask=ask,
        midpoint=midpoint,
        relative_spread=relative_spread,
        selection_mode=x["mode"],
    )


# ============================================================
# POSITION SIZING / ORDERS
# ============================================================

def get_account_equity(
    trading_client: TradingClient,
) -> float:

    account = trading_client.get_account()

    return float(account.equity)


def contracts_for_risk(
    equity: float,
    option_ask: float,
    remaining_risk_budget: float,
) -> int:

    contract_cost = (
        option_ask * 100.0
    )

    if contract_cost <= 0:
        return 0

    per_trade_budget = (
        equity * RISK_PER_TRADE
    )

    budget = min(
        per_trade_budget,
        remaining_risk_budget,
    )

    return max(
        0,
        math.floor(
            budget / contract_cost
        ),
    )


def submit_buy(
    trading_client: TradingClient,
    option_symbol: str,
    qty: int,
):

    request = MarketOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
    )

    return trading_client.submit_order(
        order_data=request
    )


# ============================================================
# SCAN
# ============================================================

def scan(
    execute: bool,
) -> dict[str, Any]:

    (
        trading_client,
        stock_client,
        api_key,
        api_secret,
        data_feed,
    ) = build_clients()

    now = now_et()
    current_date = now.date()

    clock = trading_client.get_clock()

    if current_date < EVENT_START_DATE:
        raise RuntimeError(
            f"Current date {current_date} "
            f"is before event start "
            f"{EVENT_START_DATE}"
        )

    prior_dates = previous_sessions(
        trading_client,
        current_date,
    )

    print("=" * 94)
    print("DELTAX EVENT OPTIONS SCAN")
    print("=" * 94)
    print(f"Now ET: {now.isoformat()}")
    print(f"Market open: {clock.is_open}")
    print(
        "Prior event sessions: "
        + (
            ", ".join(
                str(x)
                for x in prior_dates
            )
            if prior_dates
            else "none"
        )
    )

    if not prior_dates:
        print()
        print(
            "No prior event trading session yet. "
            "Today is baseline only; no event-bias "
            "trades will be opened."
        )

        payload = {
            "generated_at_et": now.isoformat(),
            "event_start_date": str(
                EVENT_START_DATE
            ),
            "prior_sessions": [],
            "signals": [],
            "intents": [],
        }

        SCAN_PATH.write_text(
            json.dumps(
                payload,
                indent=2,
            ),
            encoding="utf-8",
        )

        return payload

    # Need full event history plus today's first ten minutes.
    start_et = datetime.combine(
        EVENT_START_DATE,
        dt_time(9, 30),
        tzinfo=MARKET_TZ,
    )

    end_et = now + timedelta(minutes=1)

    symbols = load_symbols()

    print(
        f"Loading 5-minute bars for "
        f"{len(symbols)} symbols..."
    )

    all_bars = fetch_stock_bars(
        stock_client,
        symbols,
        data_feed,
        start_et,
        end_et,
    )

    bias = build_prior_event_bias(
        all_bars,
        symbols,
        prior_dates,
    )

    signals = build_live_signals(
        all_bars,
        bias,
        current_date,
    )

    signals.sort(
        key=lambda s: abs(
            s.prior_relative_return
        ),
        reverse=True,
    )

    print(
        f"Raw qualifying stock signals: "
        f"{len(signals)}"
    )

    equity = get_account_equity(
        trading_client
    )

    max_new_risk = (
        equity
        * MAX_TOTAL_NEW_PREMIUM_RISK
    )

    remaining_risk = max_new_risk

    intents = []
    accepted = 0

    for signal in signals:
        if accepted >= MAX_NEW_POSITIONS:
            break

        print()
        print(
            f"{signal.symbol} "
            f"{signal.event_bias} | "
            f"prior rel="
            f"{pct(signal.prior_relative_return)} | "
            f"10m="
            f"{pct(signal.opening_return_10m)}"
        )

        conflict, headlines = (
            news_conflicts(
                signal,
                api_key,
                api_secret,
            )
        )

        if conflict:
            print(
                "  BLOCKED by conflicting news:"
            )
            for h in headlines[:3]:
                print(f"    - {h}")

            intents.append(
                {
                    "symbol":
                        signal.symbol,
                    "status":
                        "BLOCKED_NEWS",
                    "signal":
                        asdict(signal),
                    "headlines":
                        headlines[:5],
                }
            )
            continue

        today_bars = regular_session_bars(
            all_bars.get(
                signal.symbol,
                [],
            ),
            current_date,
        )

        if len(today_bars) < 2:
            continue

        underlying_price = float(
            today_bars[1].close
        )

        choice = choose_option(
            trading_client,
            signal,
            underlying_price,
            current_date,
            api_key,
            api_secret,
        )

        if choice is None:
            print(
                "  No liquid ATM/1% OTM "
                "7-DTE option found."
            )

            intents.append(
                {
                    "symbol":
                        signal.symbol,
                    "status":
                        "NO_OPTION",
                    "signal":
                        asdict(signal),
                }
            )
            continue

        qty = contracts_for_risk(
            equity,
            choice.ask,
            remaining_risk,
        )

        if qty < 1:
            print(
                "  Skipped: contract too expensive "
                "for remaining risk budget."
            )

            intents.append(
                {
                    "symbol":
                        signal.symbol,
                    "status":
                        "RISK_BLOCK",
                    "signal":
                        asdict(signal),
                    "option":
                        asdict(choice),
                }
            )
            continue

        premium_risk = (
            qty
            * choice.ask
            * 100.0
        )

        exit_mode = (
            "NEXT_OPEN"
            if signal.symbol
            in OVERNIGHT_WHITELIST
            else "SAME_DAY_1550"
        )

        intent = {
            "symbol":
                signal.symbol,
            "status":
                "READY",
            "signal":
                asdict(signal),
            "option":
                asdict(choice),
            "qty":
                qty,
            "premium_risk_estimate":
                premium_risk,
            "exit_mode":
                exit_mode,
            "execute":
                execute,
        }

        print(
            f"  {signal.option_type.upper()} "
            f"{choice.option_symbol}"
        )
        print(
            f"  strike={choice.strike_price:.2f} | "
            f"DTE={choice.actual_dte} | "
            f"{choice.selection_mode} | "
            f"bid={choice.bid:.2f} "
            f"ask={choice.ask:.2f} | "
            f"spread="
            f"{choice.relative_spread * 100:.1f}%"
        )
        print(
            f"  qty={qty} | "
            f"risk~${premium_risk:.2f} | "
            f"exit={exit_mode}"
        )

        if execute:
            try:
                order = submit_buy(
                    trading_client,
                    choice.option_symbol,
                    qty,
                )

                intent["order_id"] = str(
                    getattr(
                        order,
                        "id",
                        "",
                    )
                )
                intent["status"] = (
                    "ORDER_SUBMITTED"
                )

                state = load_state()

                state["positions"][
                    choice.option_symbol
                ] = {
                    "underlying_symbol":
                        signal.symbol,
                    "option_symbol":
                        choice.option_symbol,
                    "qty":
                        qty,
                    "opened_at_et":
                        now.isoformat(),
                    "signal_date":
                        str(current_date),
                    "exit_mode":
                        exit_mode,
                    "next_exit_date":
                        (
                            str(
                                next_trading_day(
                                    trading_client,
                                    current_date,
                                )
                            )
                            if exit_mode
                            == "NEXT_OPEN"
                            else str(current_date)
                        ),
                }

                save_state(state)

                print(
                    "  PAPER ORDER SUBMITTED"
                )

            except Exception as exc:
                intent["status"] = (
                    "ORDER_ERROR"
                )
                intent["error"] = repr(exc)

                print(
                    f"  ORDER ERROR: {repr(exc)}"
                )

        remaining_risk -= premium_risk
        accepted += 1
        intents.append(intent)

    payload = {
        "generated_at_et": now.isoformat(),
        "event_start_date": str(
            EVENT_START_DATE
        ),
        "prior_sessions": [
            str(d)
            for d in prior_dates
        ],
        "equity": equity,
        "max_new_premium_risk":
            max_new_risk,
        "signals": [
            asdict(x)
            for x in signals
        ],
        "intents": intents,
    }

    SCAN_PATH.write_text(
        json.dumps(
            payload,
            indent=2,
        ),
        encoding="utf-8",
    )

    INTENTS_PATH.write_text(
        json.dumps(
            intents,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 94)
    print("SCAN COMPLETE")
    print("=" * 94)
    print(
        f"Signals: {len(signals)}"
    )
    print(
        f"Accepted intents: "
        f"{sum(1 for x in intents if x['status'] in {'READY','ORDER_SUBMITTED'})}"
    )
    print(
        f"Output: {INTENTS_PATH}"
    )

    return payload


# ============================================================
# EXIT MANAGER
# ============================================================

def should_exit_position(
    pos: dict[str, Any],
    now: datetime,
) -> bool:

    exit_mode = pos.get(
        "exit_mode"
    )

    signal_date = date.fromisoformat(
        pos["signal_date"]
    )

    if exit_mode == "SAME_DAY_1550":
        return (
            now.date() == signal_date
            and now.time()
            >= DEFAULT_EXIT_TIME_ET
        )

    if exit_mode == "NEXT_OPEN":
        next_exit_date = pos.get(
            "next_exit_date"
        )

        if not next_exit_date:
            return False

        target = date.fromisoformat(
            next_exit_date
        )

        return (
            now.date() >= target
            and now.time()
            >= NEXT_OPEN_EXIT_TIME_ET
        )

    return False


def manage_exits(
    execute: bool,
) -> None:

    (
        trading_client,
        _,
        _,
        _,
        _,
    ) = build_clients()

    state = load_state()

    positions = state.get(
        "positions",
        {}
    )

    now = now_et()

    print("=" * 94)
    print("DELTAX EVENT OPTIONS EXIT MANAGER")
    print("=" * 94)
    print(f"Now ET: {now.isoformat()}")
    print(
        f"Tracked positions: "
        f"{len(positions)}"
    )

    to_remove = []

    for option_symbol, pos in positions.items():
        due = should_exit_position(
            pos,
            now,
        )

        print(
            f"{option_symbol} | "
            f"{pos.get('underlying_symbol')} | "
            f"{pos.get('exit_mode')} | "
            f"due={due}"
        )

        if not due:
            continue

        if not execute:
            print(
                "  DRY RUN: would close position."
            )
            continue

        try:
            trading_client.close_position(
                symbol_or_asset_id=option_symbol,
                close_options=ClosePositionRequest(
                    qty=str(
                        pos.get("qty", 1)
                    )
                ),
            )

            print(
                "  PAPER CLOSE SUBMITTED"
            )

            to_remove.append(
                option_symbol
            )

        except Exception as exc:
            print(
                f"  CLOSE ERROR: {repr(exc)}"
            )

    for symbol in to_remove:
        positions.pop(
            symbol,
            None,
        )

    state["positions"] = positions

    save_state(state)


# ============================================================
# CHECK
# ============================================================

def check() -> None:
    (
        trading_client,
        _,
        _,
        _,
        data_feed,
    ) = build_clients()

    account = (
        trading_client.get_account()
    )

    clock = trading_client.get_clock()

    state = load_state()

    print(
        json.dumps(
            {
                "event_start_date":
                    str(EVENT_START_DATE),
                "bias_threshold":
                    BIAS_THRESHOLD,
                "opening_window_minutes":
                    OPENING_WINDOW_MINUTES,
                "opening_reversal_threshold":
                    OPENING_REVERSAL_THRESHOLD,
                "target_dte":
                    TARGET_DTE,
                "overnight_whitelist":
                    sorted(
                        OVERNIGHT_WHITELIST
                    ),
                "risk_per_trade":
                    RISK_PER_TRADE,
                "max_total_new_premium_risk":
                    MAX_TOTAL_NEW_PREMIUM_RISK,
                "max_new_positions":
                    MAX_NEW_POSITIONS,
                "data_feed":
                    data_feed,
                "account_equity":
                    str(account.equity),
                "buying_power":
                    str(account.buying_power),
                "market_is_open":
                    clock.is_open,
                "tracked_positions":
                    len(
                        state.get(
                            "positions",
                            {},
                        )
                    ),
                "sp500_file":
                    str(SP500_PATH),
                "sp500_exists":
                    SP500_PATH.exists(),
            },
            indent=2,
        )
    )


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and Alpaca connection.",
    )

    parser.add_argument(
        "--scan",
        action="store_true",
        help="Run event scan and create option intents.",
    )

    parser.add_argument(
        "--manage-exits",
        action="store_true",
        help="Close positions whose exit rule is due.",
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit PAPER orders/closures.",
    )

    args = parser.parse_args()

    if args.check:
        check()
        return 0

    if args.manage_exits:
        manage_exits(
            execute=args.execute
        )
        return 0

    if args.scan:
        scan(
            execute=args.execute
        )
        return 0

    parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
