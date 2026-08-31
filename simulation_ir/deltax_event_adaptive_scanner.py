from __future__ import annotations

import argparse
from datetime import datetime, timedelta, time as dt_time

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

MIN_EVENT_MOVE = 0.0075
MIN_POST_0940_CONFIRM = 0.0015
SCAN_START = dt_time(9, 40)
ENTRY_CUTOFF = dt_time(11, 30)
MAX_ADAPTIVE_TRADES = 5


def previous_close(alpaca: Alpaca, symbol: str, trading_day):
    start = datetime.combine(trading_day - timedelta(days=10), dt_time(0, 0), tzinfo=NY)
    end = datetime.combine(trading_day, dt_time(0, 0), tzinfo=NY)

    bars = alpaca.stock_bars([symbol], "1Day", start, end).get(symbol, [])

    valid = [
        bar for bar in bars
        if parse_bar_time(bar["t"]).date() < trading_day
    ]

    return float(valid[-1]["c"]) if valid else None


def price_0940_and_latest(alpaca: Alpaca, symbol: str, trading_day, now):
    start = datetime.combine(trading_day, dt_time(9, 30), tzinfo=NY)

    bars = alpaca.stock_bars([symbol], "1Min", start, now).get(symbol, [])

    if not bars:
        return None

    bar_0939 = None
    latest = None
    current_minute = now.replace(second=0, microsecond=0)

    for bar in sorted(bars, key=lambda b: b["t"]):
        ts = parse_bar_time(bar["t"])

        if ts.hour == 9 and ts.minute == 39:
            bar_0939 = bar

        if ts < current_minute:
            latest = bar

    if bar_0939 is None or latest is None:
        return None

    return float(bar_0939["c"]), float(latest["c"])


def is_real_order(item: dict) -> bool:
    return bool(
        isinstance(item, dict)
        and item.get("order_id")
        and item.get("instrument") in {"OPTION", "STOCK"}
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    alpaca = Alpaca(load_environment())
    clock = alpaca.clock()

    now = datetime.fromisoformat(
        clock["timestamp"].replace("Z", "+00:00")
    ).astimezone(NY)

    print()
    print("=" * 110)
    print("DELTAX CURRENT-EVENT ADAPTIVE SCANNER")
    print("=" * 110)
    print(f"Time ET: {now.isoformat()}")
    print(f"Mode: {'EXECUTE PAPER' if args.execute else 'DRY RUN'}")
    print("Entry window: 09:40–11:30 ET")
    print(f"Max adaptive trades/day: {MAX_ADAPTIVE_TRADES}")

    if not clock.get("is_open"):
        print("Market closed.")
        return 0

    if now.time() < SCAN_START:
        print("Adaptive scan window has not started.")
        return 0

    if now.time() >= ENTRY_CUTOFF:
        print("Adaptive entry cutoff passed. No new trades.")
        return 0

    trading_day = now.date()
    state = load_state()
    sday = day_state(state, trading_day)
    adaptive_orders = sday.setdefault("adaptive_orders", {})

    # Clean old dry-run/phantom entries so they do not count toward the cap.
    phantom = [
        symbol
        for symbol, item in adaptive_orders.items()
        if not is_real_order(item)
    ]
    for symbol in phantom:
        adaptive_orders.pop(symbol, None)

    if phantom:
        save_state(state)
        print(f"Removed phantom adaptive entries: {', '.join(sorted(phantom))}")

    real_existing = {
        symbol
        for symbol, item in adaptive_orders.items()
        if is_real_order(item)
    }

    remaining_slots = max(0, MAX_ADAPTIVE_TRADES - len(real_existing))

    print(
        "Already adaptive-traded: "
        + (", ".join(sorted(real_existing)) if real_existing else "none")
    )
    print(f"Remaining slots: {remaining_slots}")

    if remaining_slots <= 0:
        print("Adaptive trade cap reached.")
        return 0

    candidates = []

    for symbol in WATCHLIST:
        if symbol in real_existing:
            continue

        try:
            prev = previous_close(alpaca, symbol, trading_day)
            pair = price_0940_and_latest(alpaca, symbol, trading_day, now)

            if prev is None or pair is None:
                print(f"{symbol}: MISSING DATA")
                continue

            p0940, latest = pair

            event_move = latest / prev - 1.0
            post40 = latest / p0940 - 1.0

            if event_move >= MIN_EVENT_MOVE:
                direction = "LONG"
                continuation_ok = post40 >= MIN_POST_0940_CONFIRM
            elif event_move <= -MIN_EVENT_MOVE:
                direction = "SHORT"
                continuation_ok = post40 <= -MIN_POST_0940_CONFIRM
            else:
                direction = None
                continuation_ok = False

            if direction and continuation_ok:
                print(
                    f"{symbol}: QUALIFY {direction} | "
                    f"event={event_move*100:+.2f}% | "
                    f"post09:40={post40*100:+.2f}%"
                )
                candidates.append({
                    "symbol": symbol,
                    "direction": direction,
                    "prev": prev,
                    "p0940": p0940,
                    "latest": latest,
                    "event_move": event_move,
                    "post40": post40,
                })
            else:
                print(
                    f"{symbol}: NO TRADE | "
                    f"event={event_move*100:+.2f}% | "
                    f"post09:40={post40*100:+.2f}%"
                )

        except Exception as exc:
            print(f"{symbol}: ERROR {exc}")

    candidates.sort(
        key=lambda x: abs(x["event_move"]),
        reverse=True,
    )

    if not candidates:
        print("No new qualifying adaptive signals.")
        return 0

    for item in candidates[:remaining_slots]:
        signal = Signal(
            symbol=item["symbol"],
            direction=item["direction"],
            previous_close=item["prev"],
            today_open=item["p0940"],
            price_0940=item["latest"],
            event_gap=item["event_move"],
            reversal_10m=item["post40"],
        )

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

        print(f"{item['symbol']}: {result}")

        if (
            args.execute
            and result.get("instrument") in {"OPTION", "STOCK"}
            and result.get("order_id")
        ):
            adaptive_orders[item["symbol"]] = result
            adaptive_orders[item["symbol"]]["adaptive_event_move"] = item["event_move"]
            adaptive_orders[item["symbol"]]["adaptive_post0940"] = item["post40"]
            save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
