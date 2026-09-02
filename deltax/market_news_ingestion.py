# File: deltax/market_news_ingestion.py
# Purpose: Ingests locally filtered market-risk news from Finnhub and Marketaux into source_events.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.types.json import Jsonb


FINNHUB_URL = "https://finnhub.io/api/v1/news"
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_LOOKBACK_HOURS = 12
MARKETAUX_FREE_LIMIT = 3
ALPACA_PAGE_LIMIT = 50

# ETF universe used by the DELTAX ETF regime/rotation bot.
# Alpaca is queried both broadly and with these symbols because an important
# macro article may affect ETFs without the provider tagging the ETF ticker.
ALPACA_ETF_SYMBOLS = (
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLV", "XLC", "XLY", "XLP", "XLI", "XLE", "XLU", "XLB", "XLRE",
    "SMH", "IGV", "CIBR", "XBI", "IHI", "KRE", "IAI", "IYT", "ITA", "XOP",
    "GLD", "TLT", "BIL", "USO",
)
ALPACA_ETF_SYMBOL_SET = set(ALPACA_ETF_SYMBOLS)

# One deliberately broad Marketaux request. A stricter local filter is applied
# before any article is written to the database or later sent to AI.
MARKETAUX_SEARCH = (
    '(airstrike|"missile attack"|"economic sanctions"|"export controls"|tariff|'
    '"Strait of Hormuz"|"Taiwan Strait"|"oil supply"|OPEC|"pipeline outage"|'
    '"refinery outage"|"shipping disruption"|"semiconductor shortage"|'
    '"aircraft grounding"|"bank failure"|"banking crisis"|"credit downgrade"|'
    'cyberattack|ransomware|"cloud outage"|"power grid"|"nuclear plant"|'
    'earthquake|hurricane|wildfire|"drug pricing"|Medicare|antitrust)'
)

# Terms are evaluated locally against title and text. These are market-moving
# events, not company recommendations or general business commentary.
RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("airstrike", r"\bair[ -]?strikes?\b|\bbombard(?:ment|ed|ing)?\b"),
        ("missile_attack", r"\bmissile(?:s)?\b|\brocket attack\b"),
        ("military_escalation", r"\bmilitary (?:attack|invasion|escalation|strike)\b"),
        ("war", r"\bwar\b|\barmed conflict\b"),
        ("sanctions", r"\bsanctions?\b"),
        ("export_controls", r"\bexport controls?\b|\bexport ban\b"),
        ("tariff", r"\btariffs?\b|\btrade war\b"),
        ("hormuz", r"\b(?:strait of )?hormuz\b"),
        ("taiwan_strait", r"\btaiwan strait\b|\btaiwan blockade\b"),
        ("oil_supply", r"\boil supply\b|\bcrude oil (?:spike|surge|jump|shock|shortage|prices?)\b"),
        ("opec", r"\bOPEC\+?\b"),
        ("pipeline_outage", r"\b(?:oil|gas) pipeline (?:outage|shutdown|explosion|attack)\b"),
        ("refinery_outage", r"\brefiner(?:y|ies) (?:outage|shutdown|fire|explosion)\b"),
        ("shipping_disruption", r"\bshipping disruption\b|\bshipping route (?:closed|closure)\b"),
        ("semiconductor_shortage", r"\bsemiconductor shortage\b|\bchip shortage\b"),
        ("aircraft_grounding", r"\baircraft (?:grounding|grounded)\b|\bfleet grounded\b"),
        ("bank_failure", r"\bbank (?:failure|collapse|run)\b|\bbanking crisis\b"),
        ("credit_downgrade", r"\bcredit (?:rating )?downgrade\b|\bsovereign downgrade\b"),
        ("cyberattack", r"\bcyber[ -]?attack\b|\bransomware\b"),
        ("cloud_outage", r"\bcloud outage\b|\bdata center outage\b"),
        ("power_grid", r"\bpower grid (?:failure|outage|attack|emergency)\b"),
        ("nuclear_plant", r"\bnuclear (?:plant|reactor) (?:incident|accident|shutdown|attack)\b"),
        ("earthquake", r"\bearthquake\b"),
        ("hurricane", r"\bhurricane\b|\bmajor tropical storm\b"),
        ("wildfire", r"\bwildfires?\b"),
        ("drug_pricing", r"\bdrug pricing\b|\bMedicare drug price\b"),
        ("antitrust", r"\bantitrust\b|\bcompetition investigation\b"),
        ("fed_rates", r"\bfederal reserve\b|\bfed (?:rate|rates|cut|cuts|hike|hikes|meeting)\b|\bFOMC\b"),
        ("inflation", r"\binflation\b|\bCPI\b|\bPCE\b|\bconsumer price index\b"),
        ("jobs", r"\bnonfarm payrolls?\b|\bjobs report\b|\bunemployment\b"),
        ("recession", r"\brecession\b|\beconomic slowdown\b|\bgrowth slowdown\b"),
        ("gdp", r"\bGDP\b|\bgross domestic product\b"),
        ("treasury_yields", r"\btreasury yields?\b|\bbond yields?\b|\b10-year yield\b"),
    )
)


@dataclass(frozen=True)
class NewsEvent:
    source: str
    external_id: str
    headline: str
    summary: str
    content: str
    source_url: str
    published_at: datetime
    source_updated_at: datetime | None
    symbols: tuple[str, ...]
    matched_risks: tuple[str, ...]
    raw_payload: dict[str, Any]

    @property
    def content_hash(self) -> str:
        normalized = "\n".join(
            part.strip().lower()
            for part in (self.headline, self.summary, self.source_url)
            if part.strip()
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_epoch_utc(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def matched_risk_labels(*parts: str) -> tuple[str, ...]:
    searchable = " ".join(parts)
    return tuple(label for label, pattern in RISK_PATTERNS if pattern.search(searchable))


def normalized_symbols(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        symbol = str(value or "").strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) and symbol not in result:
            result.append(symbol)
    return tuple(result)


def request_json(url: str, params: dict[str, Any], provider: str) -> tuple[Any, requests.Response]:
    try:
        response = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "DELTAX-v2/1.0"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"{provider} request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise RuntimeError(f"{provider} rejected the API credential (HTTP {response.status_code}).")
    if response.status_code in (402, 429):
        raise RuntimeError(f"{provider} quota or rate limit was reached (HTTP {response.status_code}).")
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"{provider} returned HTTP {response.status_code}.") from exc
    try:
        return response.json(), response
    except ValueError as exc:
        raise RuntimeError(f"{provider} returned invalid JSON.") from exc


def request_alpaca_news(
    api_key: str,
    api_secret: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        response = requests.get(
            ALPACA_NEWS_URL,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "DELTAX-v2/1.0",
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Alpaca request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise RuntimeError(
            f"Alpaca rejected the API credential (HTTP {response.status_code})."
        )
    if response.status_code in (402, 429):
        raise RuntimeError(
            f"Alpaca quota or rate limit was reached (HTTP {response.status_code})."
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Alpaca returned HTTP {response.status_code}: {response.text[:500]}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Alpaca returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Alpaca returned an unexpected response type.")
    return payload


def alpaca_item_to_event(
    item: dict[str, Any],
    cutoff: datetime,
    *,
    allow_etf_symbol_match: bool,
) -> NewsEvent | None:
    published_at = parse_iso_utc(
        item.get("created_at") or item.get("updated_at")
    )
    if published_at is None or published_at < cutoff:
        return None

    headline = clean_text(item.get("headline"))
    summary = clean_text(item.get("summary"))
    content = clean_text(item.get("content"))
    symbols = normalized_symbols(item.get("symbols") or ())

    # Use headline + summary for the ingestion gate. Full article content often
    # contains generic market/background paragraphs that create false positives
    # (for example a reverse-split article mentioning Hormuz elsewhere).
    risks = matched_risk_labels(headline, summary)

    # Broad Alpaca news must match a market-risk/macro pattern.
    # ETF-targeted news is also retained when Alpaca explicitly tags one of
    # our managed ETFs, even if no hardcoded risk keyword is present.
    etf_tagged = bool(ALPACA_ETF_SYMBOL_SET.intersection(symbols))
    if not headline or (not risks and not (allow_etf_symbol_match and etf_tagged)):
        return None

    external_id = clean_text(item.get("id"))
    if not external_id:
        external_id = hashlib.sha256(
            f"{headline}|{published_at.isoformat()}".encode("utf-8")
        ).hexdigest()

    matched = risks or ("etf_symbol_news",)
    return NewsEvent(
        source="alpaca_news",
        external_id=external_id,
        headline=headline,
        summary=summary,
        content=content,
        source_url=clean_text(item.get("url")),
        published_at=published_at,
        source_updated_at=parse_iso_utc(item.get("updated_at")),
        symbols=symbols,
        matched_risks=matched,
        raw_payload=item,
    )


def fetch_alpaca_request(
    api_key: str,
    api_secret: str,
    cutoff: datetime,
    *,
    symbols: tuple[str, ...] | None,
    allow_etf_symbol_match: bool,
) -> tuple[list[NewsEvent], int]:
    events: list[NewsEvent] = []
    page_token: str | None = None
    raw_returned = 0

    while True:
        params: dict[str, Any] = {
            "start": cutoff.isoformat(),
            "sort": "desc",
            "include_content": "true",
            "limit": ALPACA_PAGE_LIMIT,
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        if page_token:
            params["page_token"] = page_token

        payload = request_alpaca_news(api_key, api_secret, params)
        items = payload.get("news") or []
        if not isinstance(items, list):
            raise RuntimeError("Alpaca response field 'news' was not a list.")

        raw_returned += len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            event = alpaca_item_to_event(
                item,
                cutoff,
                allow_etf_symbol_match=allow_etf_symbol_match,
            )
            if event is not None:
                events.append(event)

        page_token = clean_text(payload.get("next_page_token"))
        if not page_token:
            break

    return events, raw_returned


def fetch_alpaca(
    api_key: str,
    api_secret: str,
    cutoff: datetime,
) -> tuple[list[NewsEvent], dict[str, Any]]:
    # 1) Broad market/macro news.
    broad_events, broad_raw = fetch_alpaca_request(
        api_key,
        api_secret,
        cutoff,
        symbols=None,
        allow_etf_symbol_match=False,
    )

    # 2) ETF-tagged news for the ETF bot universe.
    etf_events, etf_raw = fetch_alpaca_request(
        api_key,
        api_secret,
        cutoff,
        symbols=ALPACA_ETF_SYMBOLS,
        allow_etf_symbol_match=True,
    )

    # Same Alpaca article can appear in both calls. Keep it once.
    deduped: dict[str, NewsEvent] = {}
    for event in [*broad_events, *etf_events]:
        deduped[event.external_id] = event

    events = sorted(
        deduped.values(),
        key=lambda event: event.published_at,
        reverse=True,
    )

    return events, {
        "broad_returned": broad_raw,
        "etf_returned": etf_raw,
        "unique_relevant_after_local_filter": len(events),
        "etf_symbols_queried": len(ALPACA_ETF_SYMBOLS),
    }


def fetch_finnhub(api_key: str, cutoff: datetime) -> tuple[list[NewsEvent], dict[str, Any]]:
    payload, _ = request_json(
        FINNHUB_URL,
        {"category": "general", "minId": 0, "token": api_key},
        "Finnhub",
    )
    if not isinstance(payload, list):
        raise RuntimeError("Finnhub returned an unexpected response type.")

    events: list[NewsEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        published_at = parse_epoch_utc(item.get("datetime"))
        if published_at is None or published_at < cutoff:
            continue
        headline = clean_text(item.get("headline"))
        summary = clean_text(item.get("summary"))
        risks = matched_risk_labels(headline, summary)
        if not headline or not risks:
            continue
        external_id = clean_text(item.get("id"))
        if not external_id:
            external_id = hashlib.sha256(
                f"{headline}|{published_at.isoformat()}".encode("utf-8")
            ).hexdigest()
        related = clean_text(item.get("related"))
        symbols = normalized_symbols(related.split(",") if related else [])
        events.append(
            NewsEvent(
                source="finnhub_news",
                external_id=external_id,
                headline=headline,
                summary=summary,
                content="",
                source_url=clean_text(item.get("url")),
                published_at=published_at,
                source_updated_at=None,
                symbols=symbols,
                matched_risks=risks,
                raw_payload=item,
            )
        )
    return events, {"returned": len(payload)}


def marketaux_symbols(item: dict[str, Any]) -> tuple[str, ...]:
    entities = item.get("entities")
    if not isinstance(entities, list):
        return ()
    return normalized_symbols(
        entity.get("symbol")
        for entity in entities
        if isinstance(entity, dict)
    )


def fetch_marketaux(
    api_token: str,
    cutoff: datetime,
) -> tuple[list[NewsEvent], dict[str, Any]]:
    payload, response = request_json(
        MARKETAUX_URL,
        {
            "api_token": api_token,
            "language": "en",
            "group_similar": "true",
            "search": MARKETAUX_SEARCH,
            "published_after": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
            "sort": "published_at",
            "limit": MARKETAUX_FREE_LIMIT,
        },
        "Marketaux",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("Marketaux returned an unexpected response type.")

    events: list[NewsEvent] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        published_at = parse_iso_utc(item.get("published_at"))
        if published_at is None or published_at < cutoff:
            continue
        headline = clean_text(item.get("title"))
        summary = clean_text(item.get("description"))
        content = clean_text(item.get("snippet"))
        risks = matched_risk_labels(headline, summary, content)
        if not headline or not risks:
            continue
        external_id = clean_text(item.get("uuid"))
        if not external_id:
            external_id = hashlib.sha256(
                f"{headline}|{published_at.isoformat()}".encode("utf-8")
            ).hexdigest()
        events.append(
            NewsEvent(
                source="marketaux_news",
                external_id=external_id,
                headline=headline,
                summary=summary,
                content=content,
                source_url=clean_text(item.get("url")),
                published_at=published_at,
                source_updated_at=parse_iso_utc(item.get("updated_at")),
                symbols=marketaux_symbols(item),
                matched_risks=risks,
                raw_payload=item,
            )
        )

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return events, {
        "found": meta.get("found"),
        "returned": meta.get("returned", len(payload["data"])),
        "usage_remaining": response.headers.get("X-UsageLimit-Remaining"),
    }


def validate_schema(connection: psycopg.Connection[Any]) -> None:
    required = {
        "source_events": {
            "source", "external_id", "source_type", "headline", "summary",
            "content", "source_url", "published_at", "source_updated_at",
            "content_hash", "processing_status", "raw_payload",
        },
        "source_event_symbols": {"source_event_id", "symbol"},
        "instruments": {"symbol"},
        "strategy_configs": {"id", "version", "is_active"},
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            """,
            (list(required),),
        )
        actual: dict[str, set[str]] = {}
        for table_name, column_name in cursor.fetchall():
            actual.setdefault(table_name, set()).add(column_name)
    missing = {
        table: sorted(columns - actual.get(table, set()))
        for table, columns in required.items()
        if columns - actual.get(table, set())
    }
    if missing:
        raise RuntimeError(f"Database schema is missing required columns: {missing}")


def active_config(connection: psycopg.Connection[Any]) -> tuple[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id::text, version
            FROM strategy_configs
            WHERE is_active = true
            ORDER BY activated_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("No active strategy configuration was found.")
    return row[0], row[1]


def known_symbols(
    connection: psycopg.Connection[Any], symbols: Iterable[str]
) -> set[str]:
    candidates = sorted(set(symbols))
    if not candidates:
        return set()
    with connection.cursor() as cursor:
        cursor.execute("SELECT symbol FROM instruments WHERE symbol = ANY(%s)", (candidates,))
        return {row[0] for row in cursor.fetchall()}


def persist_events(
    connection: psycopg.Connection[Any], events: list[NewsEvent]
) -> dict[str, int]:
    counts = {"inserted": 0, "updated": 0, "symbol_links": 0}
    valid_symbols = known_symbols(
        connection,
        (symbol for event in events for symbol in event.symbols),
    )
    with connection.cursor() as cursor:
        for event in events:
            cursor.execute(
                """
                INSERT INTO source_events (
                    source, external_id, source_type, headline, summary, content,
                    source_url, published_at, source_updated_at, content_hash,
                    processing_status, raw_payload
                )
                VALUES (%s, %s, 'market_news', %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    headline = EXCLUDED.headline,
                    summary = EXCLUDED.summary,
                    content = EXCLUDED.content,
                    source_url = EXCLUDED.source_url,
                    published_at = EXCLUDED.published_at,
                    source_updated_at = EXCLUDED.source_updated_at,
                    content_hash = EXCLUDED.content_hash,
                    raw_payload = EXCLUDED.raw_payload,
                    processing_status = CASE
                        WHEN source_events.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                        THEN 'pending'
                        ELSE source_events.processing_status
                    END
                RETURNING id, (xmax = 0) AS inserted
                """,
                (
                    event.source, event.external_id, event.headline, event.summary,
                    event.content, event.source_url, event.published_at,
                    event.source_updated_at, event.content_hash,
                    Jsonb({**event.raw_payload, "deltax_matched_risks": event.matched_risks}),
                ),
            )
            event_id, inserted = cursor.fetchone()
            counts["inserted" if inserted else "updated"] += 1
            for symbol in event.symbols:
                if symbol not in valid_symbols:
                    continue
                cursor.execute(
                    """
                    INSERT INTO source_event_symbols (source_event_id, symbol)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (event_id, symbol),
                )
                counts["symbol_links"] += cursor.rowcount
    return counts


def print_events(events: list[NewsEvent]) -> None:
    for index, event in enumerate(sorted(events, key=lambda x: x.published_at, reverse=True), 1):
        print("-" * 78)
        print(f"Candidate {index} | source={event.source}")
        print(f"Published UTC: {event.published_at.isoformat()}")
        print(f"Risks: {', '.join(event.matched_risks)}")
        print(f"Symbols from provider: {', '.join(event.symbols) if event.symbols else 'none'}")
        print(f"Headline: {event.headline}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DELTAX multi-source market-news ingestion")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Validate configuration and schema only")
    mode.add_argument("--dry-run", action="store_true", help="Fetch and filter without database writes")
    mode.add_argument("--apply", action="store_true", help="Fetch, filter, and persist source events")
    parser.add_argument("--source", choices=("all", "alpaca", "finnhub", "marketaux"), default="all")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(project_root() / ".env")

    database_url = os.getenv("DATABASE_URL", "").strip()
    finnhub_key = os.getenv("FINNHUB_API_KEY", "").strip()
    marketaux_token = os.getenv("MARKETAUX_API_TOKEN", "").strip()

    # News is a shared intelligence layer, so it may authenticate with either
    # the MAIN/PAPER or EVENT Alpaca account. Prefer PAPER when present.
    alpaca_key = (
        os.getenv("ALPACA_API_KEY_PAPER", "").strip()
        or os.getenv("ALPACA_API_KEY_EVENT", "").strip()
        or os.getenv("ALPACA_API_KEY", "").strip()
    )
    alpaca_secret = (
        os.getenv("ALPACA_API_SECRET_PAPER", "").strip()
        or os.getenv("ALPACA_API_SECRET_EVENT", "").strip()
        or os.getenv("ALPACA_API_SECRET", "").strip()
    )

    required_credentials = {
        "alpaca": bool(alpaca_key and alpaca_secret),
        "finnhub": bool(finnhub_key),
        "marketaux": bool(marketaux_token),
    }

    if not database_url:
        print("ERROR: DATABASE_URL is missing from .env.", file=sys.stderr)
        return 1
    selected = ("alpaca", "finnhub", "marketaux") if args.source == "all" else (args.source,)
    missing = [source for source in selected if not required_credentials[source]]
    if missing:
        print(f"ERROR: Missing API credential for: {', '.join(missing)}.", file=sys.stderr)
        return 1
    if args.lookback_hours < 1 or args.lookback_hours > 72:
        print("ERROR: --lookback-hours must be between 1 and 72.", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(database_url) as connection:
            validate_schema(connection)
            config_id, config_version = active_config(connection)
            if args.check:
                print(json.dumps({
                    "active_strategy_config_id": config_id,
                    "active_strategy_version": config_version,
                    "selected_sources": list(selected),
                    "credentials_present": {name: required_credentials[name] for name in selected},
                    "lookback_hours": args.lookback_hours,
                    "marketaux_max_articles_per_request": MARKETAUX_FREE_LIMIT,
                    "alpaca_page_limit": ALPACA_PAGE_LIMIT,
                    "alpaca_etf_symbols": len(ALPACA_ETF_SYMBOLS),
                    "remote_requests_performed": 0,
                    "database_writes_performed": False,
                    "openai_requests_performed": 0,
                }, indent=2))
                print("MARKET NEWS INGESTION HEALTH CHECK: OK")
                return 0

            cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
            events: list[NewsEvent] = []
            provider_meta: dict[str, Any] = {}
            if "alpaca" in selected:
                fetched, meta = fetch_alpaca(alpaca_key, alpaca_secret, cutoff)
                events.extend(fetched)
                provider_meta["alpaca"] = {
                    **meta,
                    "relevant_after_local_filter": len(fetched),
                }
            if "finnhub" in selected:
                fetched, meta = fetch_finnhub(finnhub_key, cutoff)
                events.extend(fetched)
                provider_meta["finnhub"] = {**meta, "relevant_after_local_filter": len(fetched)}
            if "marketaux" in selected:
                fetched, meta = fetch_marketaux(marketaux_token, cutoff)
                events.extend(fetched)
                provider_meta["marketaux"] = {**meta, "relevant_after_local_filter": len(fetched)}

            print("DELTAX MARKET NEWS INGESTION")
            print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
            print(f"Config: {config_version}")
            print(f"Cutoff UTC: {cutoff.isoformat()}")
            print(json.dumps(provider_meta, indent=2, default=str))
            print(f"Total locally relevant candidates: {len(events)}")
            print_events(events)

            if args.dry_run:
                connection.rollback()
                print("\nNo database writes were performed.")
                print("No OpenAI requests were performed.")
                print("MARKET NEWS INGESTION DRY RUN: OK")
                return 0

            counts = persist_events(connection, events)
            connection.commit()
            print("\n" + json.dumps(counts, indent=2))
            print("No OpenAI requests were performed.")
            print("No event clusters, trade theses, intents, or orders were created.")
            print("MARKET NEWS INGESTION APPLY: OK")
            return 0
    except (RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
