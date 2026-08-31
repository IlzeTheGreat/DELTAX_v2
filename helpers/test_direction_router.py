# File: helpers/test_direction_router.py
# Purpose: Validates the deterministic Core, Active, and Intraday direction-routing decision matrix without writing data or creating orders.

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


CONFIDENCE_THRESHOLD = 0.65
INTRADAY_NEWS_MINUTES = 60


@dataclass
class NewsCluster:
    cluster_key: str
    published_at: datetime
    direction: Optional[str]
    confidence: Optional[float]
    meaningful: Optional[bool]
    sufficient: Optional[bool]
    processed: bool = True


@dataclass
class Candidate:
    symbol: str
    strategy: str
    deviation_side: str
    technical_direction: str
    signal_price: float
    confirmation_price: Optional[float]
    news_clusters: list[NewsCluster] = field(default_factory=list)
    shortable: Optional[bool] = None
    easy_to_borrow: Optional[bool] = None
    now: Optional[datetime] = None
    session_open: Optional[datetime] = None
    previous_session_close: Optional[datetime] = None


@dataclass
class RouterResult:
    final_direction: str
    mode: Optional[str]
    confirmation_required: bool
    active_news: list[NewsCluster]
    rejection_reasons: list[str]


def is_material(cluster):
    return (
        cluster.processed
        and cluster.direction in {"bullish", "bearish"}
        and cluster.confidence is not None
        and cluster.confidence >= CONFIDENCE_THRESHOLD
        and cluster.meaningful is True
        and cluster.sufficient is True
    )


def select_active_intraday_news(candidate):
    if (
        candidate.now is None
        or candidate.session_open is None
        or candidate.previous_session_close is None
    ):
        raise ValueError(
            "Intraday candidates require now, session_open, "
            "and previous_session_close"
        )

    active = []
    regular_news_cutoff = candidate.now - timedelta(
        minutes=INTRADAY_NEWS_MINUTES
    )

    for cluster in candidate.news_clusters:
        published_at = cluster.published_at

        if published_at > candidate.now:
            continue

        is_premarket = (
            candidate.previous_session_close
            < published_at
            < candidate.session_open
        )

        is_recent_regular_news = (
            candidate.session_open
            <= published_at
            <= candidate.now
            and published_at >= regular_news_cutoff
        )

        if is_premarket or is_recent_regular_news:
            active.append(cluster)

    return sorted(active, key=lambda item: item.published_at)


def find_news_state(active_news):
    unprocessed = [
        cluster
        for cluster in active_news
        if not cluster.processed
    ]

    material_directions = {
        cluster.direction
        for cluster in active_news
        if is_material(cluster)
    }

    return unprocessed, material_directions


def apply_shortability_gate(candidate, result):
    if result.final_direction != "short":
        return result

    if candidate.shortable is not True:
        result.rejection_reasons.append("symbol_not_shortable")

    if candidate.easy_to_borrow is not True:
        result.rejection_reasons.append("symbol_not_easy_to_borrow")

    if result.rejection_reasons:
        result.final_direction = "reject"

    return result


def route_core_active(candidate):
    active_news = candidate.news_clusters
    rejection_reasons = []

    unprocessed, material_directions = find_news_state(active_news)

    if unprocessed:
        rejection_reasons.append("fresh_news_not_analyzed")

    if material_directions == {"bullish", "bearish"}:
        rejection_reasons.append("conflicting_material_news")

    if rejection_reasons:
        return RouterResult(
            final_direction="reject",
            mode=None,
            confirmation_required=True,
            active_news=active_news,
            rejection_reasons=rejection_reasons,
        )

    if candidate.confirmation_price is None:
        return RouterResult(
            final_direction="reject",
            mode=None,
            confirmation_required=True,
            active_news=active_news,
            rejection_reasons=["price_confirmation_pending"],
        )

    price_up = candidate.confirmation_price > candidate.signal_price
    price_down = candidate.confirmation_price < candidate.signal_price

    if candidate.deviation_side == "downside":
        if "bearish" in material_directions:
            if price_down:
                result = RouterResult(
                    final_direction="short",
                    mode="news_momentum",
                    confirmation_required=True,
                    active_news=active_news,
                    rejection_reasons=[],
                )
            else:
                result = RouterResult(
                    final_direction="reject",
                    mode="news_momentum",
                    confirmation_required=True,
                    active_news=active_news,
                    rejection_reasons=[
                        "bearish_news_not_confirmed_by_price"
                    ],
                )
        elif price_up:
            result = RouterResult(
                final_direction="long",
                mode="mean_reversion",
                confirmation_required=True,
                active_news=active_news,
                rejection_reasons=[],
            )
        else:
            result = RouterResult(
                final_direction="reject",
                mode="mean_reversion",
                confirmation_required=True,
                active_news=active_news,
                rejection_reasons=[
                    "long_mean_reversion_not_confirmed"
                ],
            )

    elif candidate.deviation_side == "upside":
        if "bullish" in material_directions:
            if price_up:
                result = RouterResult(
                    final_direction="long",
                    mode="news_momentum",
                    confirmation_required=True,
                    active_news=active_news,
                    rejection_reasons=[],
                )
            else:
                result = RouterResult(
                    final_direction="reject",
                    mode="news_momentum",
                    confirmation_required=True,
                    active_news=active_news,
                    rejection_reasons=[
                        "bullish_news_not_confirmed_by_price"
                    ],
                )
        elif price_down:
            result = RouterResult(
                final_direction="short",
                mode="mean_reversion",
                confirmation_required=True,
                active_news=active_news,
                rejection_reasons=[],
            )
        else:
            result = RouterResult(
                final_direction="reject",
                mode="mean_reversion",
                confirmation_required=True,
                active_news=active_news,
                rejection_reasons=[
                    "short_mean_reversion_not_confirmed"
                ],
            )
    else:
        result = RouterResult(
            final_direction="reject",
            mode=None,
            confirmation_required=True,
            active_news=active_news,
            rejection_reasons=["invalid_deviation_side"],
        )

    return apply_shortability_gate(candidate, result)


def route_intraday(candidate):
    active_news = select_active_intraday_news(candidate)
    rejection_reasons = []

    expected_direction = {
        "downside": "long",
        "upside": "short",
    }.get(candidate.deviation_side)

    if expected_direction is None:
        rejection_reasons.append("invalid_deviation_side")
    elif candidate.technical_direction != expected_direction:
        rejection_reasons.append(
            "technical_direction_does_not_match_deviation"
        )

    unprocessed, material_directions = find_news_state(active_news)

    if unprocessed:
        rejection_reasons.append("fresh_news_not_analyzed")

    if material_directions == {"bullish", "bearish"}:
        rejection_reasons.append("conflicting_material_news")

    if candidate.technical_direction == "long":
        if "bearish" in material_directions:
            rejection_reasons.append("active_bearish_news_veto")

    elif candidate.technical_direction == "short":
        if "bullish" in material_directions:
            rejection_reasons.append("active_bullish_news_veto")
    else:
        rejection_reasons.append("invalid_technical_direction")

    result = RouterResult(
        final_direction=(
            "reject"
            if rejection_reasons
            else candidate.technical_direction
        ),
        mode="intraday_mean_reversion",
        confirmation_required=False,
        active_news=active_news,
        rejection_reasons=rejection_reasons,
    )

    return apply_shortability_gate(candidate, result)


def route_candidate(candidate):
    if candidate.strategy in {"Core", "Active"}:
        return route_core_active(candidate)

    if candidate.strategy == "Intraday":
        return route_intraday(candidate)

    return RouterResult(
        final_direction="reject",
        mode=None,
        confirmation_required=False,
        active_news=[],
        rejection_reasons=["unsupported_strategy"],
    )


def format_age(cluster, now):
    age = now - cluster.published_at
    return f"{age.total_seconds() / 60:.1f} minutes"


def format_optional(value):
    if value is None:
        return "n/a"

    return str(value)


def print_result(candidate, result):
    print("\n" + "=" * 78)
    print(f"Symbol: {candidate.symbol}")
    print(f"Strategy: {candidate.strategy}")
    print(f"Technical deviation side: {candidate.deviation_side}")
    print(
        "Technical proposed direction: "
        f"{candidate.technical_direction}"
    )

    if result.active_news:
        print("Active news clusters:")

        for cluster in result.active_news:
            reference_time = candidate.now or NOW

            print(
                f"  - {cluster.cluster_key} | "
                f"age={format_age(cluster, reference_time)}"
            )
            print(
                f"    AI direction={format_optional(cluster.direction)} | "
                f"confidence={format_optional(cluster.confidence)} | "
                f"material={format_optional(cluster.meaningful)} | "
                f"sufficient={format_optional(cluster.sufficient)} | "
                f"processed={cluster.processed}"
            )
    else:
        print("Active news clusters: none")

    print(
        "Confirmation required: "
        f"{'yes' if result.confirmation_required else 'no'}"
    )
    print(f"Signal price: {candidate.signal_price:.2f}")

    if result.confirmation_required:
        print(
            "Confirmation price: "
            f"{format_optional(candidate.confirmation_price)}"
        )
    else:
        print("Confirmation price: n/a")

    if (
        candidate.technical_direction == "short"
        or result.final_direction == "short"
        or (
            result.final_direction == "reject"
            and result.mode in {
                "news_momentum",
                "mean_reversion",
            }
            and candidate.shortable is not None
        )
    ):
        print(
            f"Shortable: {format_optional(candidate.shortable)} | "
            f"easy-to-borrow: "
            f"{format_optional(candidate.easy_to_borrow)}"
        )
    else:
        print("Shortable/easy-to-borrow: n/a")

    print(f"Final direction: {result.final_direction}")
    print(f"Mode: {format_optional(result.mode)}")

    if result.rejection_reasons:
        print("Rejection reasons:")

        for reason in result.rejection_reasons:
            print(f"  - {reason}")
    else:
        print("Rejection reasons: none")


def news(
    key,
    minutes_ago,
    direction,
    confidence,
    meaningful,
    sufficient,
    processed=True,
):
    return NewsCluster(
        cluster_key=key,
        published_at=NOW - timedelta(minutes=minutes_ago),
        direction=direction,
        confidence=confidence,
        meaningful=meaningful,
        sufficient=sufficient,
        processed=processed,
    )


NOW = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)
SESSION_OPEN = datetime(
    2026,
    8,
    31,
    13,
    30,
    tzinfo=timezone.utc,
)
PREVIOUS_SESSION_CLOSE = datetime(
    2026,
    8,
    28,
    20,
    0,
    tzinfo=timezone.utc,
)


TEST_CASES = [
    (
        "core_downside_neutral_price_up",
        Candidate(
            symbol="CORE1",
            strategy="Core",
            deviation_side="downside",
            technical_direction="router_decides",
            signal_price=100.00,
            confirmation_price=101.00,
            news_clusters=[
                news(
                    "core1-neutral",
                    20,
                    "neutral",
                    0.15,
                    False,
                    True,
                )
            ],
        ),
        "long",
        "mean_reversion",
    ),
    (
        "active_downside_bearish_price_down",
        Candidate(
            symbol="ACTV1",
            strategy="Active",
            deviation_side="downside",
            technical_direction="router_decides",
            signal_price=100.00,
            confirmation_price=98.50,
            news_clusters=[
                news(
                    "actv1-bearish",
                    25,
                    "bearish",
                    0.80,
                    True,
                    True,
                )
            ],
            shortable=True,
            easy_to_borrow=True,
        ),
        "short",
        "news_momentum",
    ),
    (
        "core_upside_neutral_price_down",
        Candidate(
            symbol="CORE2",
            strategy="Core",
            deviation_side="upside",
            technical_direction="router_decides",
            signal_price=100.00,
            confirmation_price=99.00,
            news_clusters=[],
            shortable=True,
            easy_to_borrow=True,
        ),
        "short",
        "mean_reversion",
    ),
    (
        "active_upside_bullish_price_up",
        Candidate(
            symbol="ACTV2",
            strategy="Active",
            deviation_side="upside",
            technical_direction="router_decides",
            signal_price=100.00,
            confirmation_price=102.00,
            news_clusters=[
                news(
                    "actv2-bullish",
                    30,
                    "bullish",
                    0.78,
                    True,
                    True,
                )
            ],
        ),
        "long",
        "news_momentum",
    ),
    (
        "core_news_price_conflict",
        Candidate(
            symbol="CORE3",
            strategy="Core",
            deviation_side="downside",
            technical_direction="router_decides",
            signal_price=100.00,
            confirmation_price=101.00,
            news_clusters=[
                news(
                    "core3-bearish",
                    35,
                    "bearish",
                    0.82,
                    True,
                    True,
                )
            ],
            shortable=True,
            easy_to_borrow=True,
        ),
        "reject",
        "news_momentum",
    ),
    (
        "intraday_long_bearish_veto",
        Candidate(
            symbol="INTD1",
            strategy="Intraday",
            deviation_side="downside",
            technical_direction="long",
            signal_price=50.00,
            confirmation_price=None,
            news_clusters=[
                news(
                    "intd1-bearish",
                    30,
                    "bearish",
                    0.85,
                    True,
                    True,
                )
            ],
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "reject",
        "intraday_mean_reversion",
    ),
    (
        "intraday_long_neutral_news",
        Candidate(
            symbol="INTD2",
            strategy="Intraday",
            deviation_side="downside",
            technical_direction="long",
            signal_price=40.00,
            confirmation_price=None,
            news_clusters=[
                news(
                    "intd2-neutral",
                    15,
                    "neutral",
                    0.15,
                    False,
                    False,
                )
            ],
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "long",
        "intraday_mean_reversion",
    ),
    (
        "intraday_short_bullish_veto",
        Candidate(
            symbol="INTD3",
            strategy="Intraday",
            deviation_side="upside",
            technical_direction="short",
            signal_price=75.00,
            confirmation_price=None,
            news_clusters=[
                news(
                    "intd3-bullish",
                    10,
                    "bullish",
                    0.90,
                    True,
                    True,
                )
            ],
            shortable=True,
            easy_to_borrow=True,
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "reject",
        "intraday_mean_reversion",
    ),
    (
        "intraday_short_without_veto",
        Candidate(
            symbol="INTD4",
            strategy="Intraday",
            deviation_side="upside",
            technical_direction="short",
            signal_price=85.00,
            confirmation_price=None,
            news_clusters=[],
            shortable=True,
            easy_to_borrow=True,
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "short",
        "intraday_mean_reversion",
    ),
    (
        "intraday_conflicting_material_news",
        Candidate(
            symbol="INTD5",
            strategy="Intraday",
            deviation_side="downside",
            technical_direction="long",
            signal_price=60.00,
            confirmation_price=None,
            news_clusters=[
                news(
                    "intd5-bullish",
                    25,
                    "bullish",
                    0.80,
                    True,
                    True,
                ),
                news(
                    "intd5-bearish",
                    20,
                    "bearish",
                    0.82,
                    True,
                    True,
                ),
            ],
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "reject",
        "intraday_mean_reversion",
    ),
    (
        "intraday_unprocessed_fresh_news",
        Candidate(
            symbol="INTD6",
            strategy="Intraday",
            deviation_side="downside",
            technical_direction="long",
            signal_price=45.00,
            confirmation_price=None,
            news_clusters=[
                news(
                    "intd6-unprocessed",
                    5,
                    None,
                    None,
                    None,
                    None,
                    processed=False,
                )
            ],
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "reject",
        "intraday_mean_reversion",
    ),
    (
        "intraday_premarket_bearish_veto",
        Candidate(
            symbol="INTD7",
            strategy="Intraday",
            deviation_side="downside",
            technical_direction="long",
            signal_price=35.00,
            confirmation_price=None,
            news_clusters=[
                NewsCluster(
                    cluster_key="intd7-premarket-bearish",
                    published_at=datetime(
                        2026,
                        8,
                        31,
                        12,
                        0,
                        tzinfo=timezone.utc,
                    ),
                    direction="bearish",
                    confidence=0.88,
                    meaningful=True,
                    sufficient=True,
                    processed=True,
                )
            ],
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "reject",
        "intraday_mean_reversion",
    ),
    (
        "intraday_short_not_borrowable",
        Candidate(
            symbol="INTD8",
            strategy="Intraday",
            deviation_side="upside",
            technical_direction="short",
            signal_price=90.00,
            confirmation_price=None,
            news_clusters=[],
            shortable=True,
            easy_to_borrow=False,
            now=NOW,
            session_open=SESSION_OPEN,
            previous_session_close=PREVIOUS_SESSION_CLOSE,
        ),
        "reject",
        "intraday_mean_reversion",
    ),
]


def main():
    passed = 0

    print("DELTAX DIRECTION ROUTER TEST")
    print(f"Confidence threshold: {CONFIDENCE_THRESHOLD:.2f}")

    for (
        test_name,
        candidate,
        expected_direction,
        expected_mode,
    ) in TEST_CASES:
        result = route_candidate(candidate)
        print_result(candidate, result)

        assert result.final_direction == expected_direction, (
            f"{test_name}: expected direction "
            f"{expected_direction}, got {result.final_direction}"
        )

        assert result.mode == expected_mode, (
            f"{test_name}: expected mode "
            f"{expected_mode}, got {result.mode}"
        )

        passed += 1
        print(f"Scenario result: PASS | {test_name}")

    print("\n" + "=" * 78)
    print(f"Passed scenarios: {passed}/{len(TEST_CASES)}")
    print("No database writes were performed.")
    print("No trade theses or orders were created.")
    print("DIRECTION ROUTER TEST: OK")


if __name__ == "__main__":
    main()