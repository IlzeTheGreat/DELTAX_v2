from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone, time as dt_time
from pathlib import Path

# Reuse the already-tested EVENT account, option selection, sizing and order code.
from deltax_event_iran_v2 import (
    Alpaca,
    Signal,
    execute_signal,
    load_environment,
    load_state,
    save_state,
    day_state,
    parse_bar_time,
    NY,
    WATCHLIST,
)

# ============================================================
# CURRENT-EVENT ADAPTIVE ONE-SHOT
# ============================================================

# Today's actual move overrides the historical Iran direction.
MIN_EVENT_MOVE = 0.0075          # 0.75% from previous close
MIN_POST_0940_CONFIRM = 0.0015   # 0.15% continuation after 09:40
MAX_TRADES = 999

STATE_TAG = "adaptive_today"


def previous_close(alpaca: Alpaca, symbol: str, trading_day):
    start = datetime.combine(
        trading_day - timedelta(days=10),
        dt_time(0, 0),
        tzinfo=NY,
    )
    end = datetime.combine(
        trading_day,
        dt_time(0, 0),
        tzinfo=NY,
    )

    bars = alpaca.stock_bars(
        [symbol],
        "1Day",
        start,
        end,
    ).get(symbol, [])

    valid = []
    for bar in bars:
        if parse_bar_time(bar["t"]).date() < trading_day:
            valid.append(bar)

    return float(valid[-1]["c"]) if valid else None


def price_0940_and_latest(alpaca: Alpaca, symbol: str, trading_day, now):
    start = datetime.combine(
        trading_day,
        dt_time(9, 30),
        tzinfo=NY,
    )

    bars = alpaca.stock_bars(
        [symbol],
        "1Min",
        start,
        now,
    ).get(symbol, [])

    if not bars:
        return None

    bar_0939 = None
    latest = None

    for bar in sorted(bars, key=lambda b: b["t"]):
        ts = parse_bar_time(bar["t"])

        if ts.hour == 9 and ts.minute == 39:
            bar_0939 = bar

        # Only use completed 1-minute bars.
        if ts < now.replace(second=0, microsecond=0):
            latest = bar

    if bar_0939 is None or latest is None:
        return None

    return float(bar_0939["c"]), float(latest["c"]), parse_bar_time(latest["t"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit paper orders. Without this flag: dry run only.",
    )
    args = parser.parse_args()

    alpaca = Alpaca(load_environment())
    clock = alpaca.clock()

    now = datetime.fromisoformat(
        clock["timestamp"].replace("Z", "+00:00")
    ).astimezone(NY)

    if not clock.get("is_open"):
        print("Market is closed.")
        return 0

    trading_day = now.date()

    print("=" * 118)
    print("DELTAX CURRENT-EVENT ADAPTIVE ONE-SHOT")
    print("=" * 118)
    print(f"Time ET: {now.isoformat()}")
    print(f"Min move vs prev close: {MIN_EVENT_MOVE * 100:.2f}%")
    print(f"Min continuation after 09:40: {MIN_POST_0940_CONFIRM * 100:.2f}%")
    print(f"Mode: {'EXECUTE PAPER' if args.execute else 'DRY RUN'}")
    print()
    print(
        f"{'SYM':<6} {'PREV':>9} {'09:40':>9} {'LATEST':>9} "
        f"{'EVENT':>9} {'POST40':>9} {'DIR':>6}  DECISION"
    )
    print("-" * 118)

    candidates = []

    for symbol in WATCHLIST:
        try:
            prev = previous_close(alpaca, symbol, trading_day)
            pair = price_0940_and_latest(alpaca, symbol, trading_day, now)

            if prev is None or pair is None:
                print(f"{symbol:<6} {'—':>9} {'—':>9} {'—':>9} {'—':>9} {'—':>9} {'—':>6}  MISSING DATA")
                continue

            p0940, latest, latest_ts = pair

            event_move = latest / prev - 1.0
            post40 = latest / p0940 - 1.0

            if event_move >= MIN_EVENT_MOVE:
                direction = "LONG"
                continuation_ok = post40 >= MIN_POST_0940_CONFIRM
            elif event_move <= -MIN_EVENT_MOVE:
                direction = "SHORT"
                continuation_ok = post40 <= -MIN_POST_0940_CONFIRM
            else:
                direction = "—"
                continuation_ok = False

            if direction != "—" and continuation_ok:
                decision = "TRADE"
                candidates.append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "prev": prev,
                        "p0940": p0940,
                        "latest": latest,
                        "event_move": event_move,
                        "post40": post40,
                        "latest_ts": latest_ts,
                    }
                )
            elif direction != "—":
                decision = "NO: MOVE LOST MOMENTUM"
            else:
                decision = "NO: EVENT MOVE < 0.75%"

            print(
                f"{symbol:<6} {prev:>9.2f} {p0940:>9.2f} {latest:>9.2f} "
                f"{event_move*100:>+8.2f}% {post40*100:>+8.2f}% "
                f"{direction:>6}  {decision}"
            )

        except Exception as exc:
            print(f"{symbol:<6} ERROR: {exc}")

    candidates.sort(key=lambda x: abs(x["event_move"]), reverse=True)

    print()
    print("=" * 118)
    print(f"QUALIFYING: {len(candidates)}")
    print("=" * 118)

    if not candidates:
        print("No adaptive current-event trades qualify.")
        return 0

    state = load_state()
    sday = day_state(state, trading_day)

    # Keep adaptive orders separate from the historical-playbook order namespace.
    adaptive_orders = sday.setdefault("adaptive_orders", {})

    for item in candidates[:MAX_TRADES]:
        symbol = item["symbol"]

        if symbol in adaptive_orders:
            print(f"{symbol}: already executed by adaptive runner today")
            continue

        signal = Signal(
            symbol=symbol,
            direction=item["direction"],
            previous_close=item["prev"],
            today_open=item["p0940"],      # execution helper only needs price fields
            price_0940=item["latest"],     # size/option selection uses current-ish spot
            event_gap=item["event_move"],
            reversal_10m=item["post40"],
        )

        # execute_signal checks sday["orders"] for historical-playbook duplicates.
        # Use a temporary execution namespace so adaptive trades are independently tracked.
        temp_day = {
            "signals": {},
            "orders": {},
            "exits_done": False,
        }

        result = execute_signal(
            alpaca=alpaca,
            signal=signal,
            state_day=temp_day,
            execute=args.execute,
        )

        print()
        print(
            f"{symbol} {item['direction']} | "
            f"event={item['event_move']*100:+.2f}% | "
            f"post09:40={item['post40']*100:+.2f}%"
        )
        print(result)

        if result.get("instrument") in {"OPTION", "STOCK"}:
            adaptive_orders[symbol] = result
            adaptive_orders[symbol]["adaptive_event_move"] = item["event_move"]
            adaptive_orders[symbol]["adaptive_post0940"] = item["post40"]

        save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
