# helpers/patch_scan_cycle_confirmation.py
from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deltax" / "scan_cycle.py"
BACKUP = TARGET.with_suffix(".py.confirmation_backup")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"Patch point not found: {label}")
    return text.replace(old, new, 1)


def main():
    if not TARGET.exists():
        raise FileNotFoundError(TARGET)

    text = TARGET.read_text(encoding="utf-8")

    if "def confirmation_price_from_bars(" in text:
        print("scan_cycle.py already contains historical confirmation patch.")
        return

    shutil.copy2(TARGET, BACKUP)

    marker = '''    def initial_route(
        self,
        technical,
        session,
        previous_close,
        news_map,
        asset_map,
        now,
    ):
'''

    helper = '''    @staticmethod
    def confirmation_price_from_bars(
        bars,
        confirmation_due_at: datetime,
    ) -> float | None:
        due = aware_utc(confirmation_due_at)
        eligible = []

        for bar in bars:
            ts = aware_utc(bar.timestamp)
            if ts < due and ts >= due - timedelta(minutes=6):
                eligible.append(bar)

        if not eligible:
            return None

        selected = max(
            eligible,
            key=lambda bar: aware_utc(bar.timestamp),
        )
        close = getattr(selected, "close", None)
        return float(close) if close is not None else None

    def historical_confirmation_price(
        self,
        symbol: str,
        confirmation_due_at: datetime,
        session_open: datetime,
        now: datetime,
    ) -> float | None:
        bars = self.scanner.fetch_intraday_bars(
            [symbol],
            aware_utc(session_open),
            aware_utc(now),
        )
        return self.confirmation_price_from_bars(
            bars.get(symbol, []),
            confirmation_due_at,
        )

''' + marker

    text = replace_once(
        text,
        marker,
        helper,
        "insert historical confirmation helpers",
    )

    old_initial = '''        candidate.confirmation_due_at = self.confirmation_due_at(
            candidate,
            provisional.direction,
            provisional.mode,
        )
        decision = self.router.route(candidate)
        return candidate, decision
'''

    new_initial = '''        candidate.confirmation_due_at = self.confirmation_due_at(
            candidate,
            provisional.direction,
            provisional.mode,
        )

        if candidate.confirmation_due_at <= aware_utc(now):
            confirmation_price = self.historical_confirmation_price(
                candidate.symbol,
                candidate.confirmation_due_at,
                candidate.session_open,
                now,
            )
            if confirmation_price is not None:
                candidate.confirmation_checked_at = aware_utc(now)
                candidate.confirmation_price = confirmation_price

        decision = self.router.route(candidate)
        return candidate, decision
'''

    text = replace_once(
        text,
        old_initial,
        new_initial,
        "patch initial_route overdue confirmation",
    )

    old_due = '''        symbols = sorted({row["symbol"] for row in due})
        prices = self.scanner.fetch_latest_prices(symbols)
        results = []

        for row in due:
            symbol = row["symbol"]
            confirmation_price = prices.get(symbol)
'''

    new_due = '''        symbols = sorted({row["symbol"] for row in due})
        bars_by_symbol = self.scanner.fetch_intraday_bars(
            symbols,
            aware_utc(session["session_open"]),
            aware_utc(now),
        )
        results = []

        for row in due:
            symbol = row["symbol"]
            confirmation_price = self.confirmation_price_from_bars(
                bars_by_symbol.get(symbol, []),
                row["confirmation_due_at"],
            )
'''

    text = replace_once(
        text,
        old_due,
        new_due,
        "patch due confirmations to historical price",
    )

    text = text.replace(
        '"reason": "confirmation_price_unavailable",',
        '"reason": "historical_confirmation_price_unavailable",',
        1,
    )

    TARGET.write_text(text, encoding="utf-8")

    print(f"Patched: {TARGET}")
    print(f"Backup:  {BACKUP}")
    print("Historical confirmation logic installed.")
    print("No DB writes or broker orders were performed by this patcher.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
