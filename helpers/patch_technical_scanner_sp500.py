# File: helpers/patch_technical_scanner_sp500.py
# Purpose: Safely switch ONLY deltax/technical_scanner.py from the strategy
# base universe (alyrise_base) to the dedicated sp500_scan technical universe.
#
# The company-news ingestion universe is NOT changed.
# The active strategy config is NOT changed.
# A .bak backup is created before modification.
#
# Usage:
#   python helpers/patch_technical_scanner_sp500.py --check
#   python helpers/patch_technical_scanner_sp500.py --apply

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "deltax" / "technical_scanner.py"
BACKUP = TARGET.with_suffix(".py.bak")

OLD_LOAD = '        universe_name = self.config["universes"]["base"]\n'
NEW_LOAD = '        universe_name = "sp500_scan"\n'

OLD_HEALTH = '            "universe_name": self.config["universes"]["base"],\n'
NEW_HEALTH = '            "universe_name": "sp500_scan",\n'

MARKER = 'PROXY_SYMBOLS = ["SPY", "QQQ", "IWM"]\n'
CONST = 'TECHNICAL_SCAN_UNIVERSE = "sp500_scan"\n'


def inspect(text: str) -> dict:
    return {
        "target": str(TARGET),
        "exists": TARGET.exists(),
        "uses_strategy_base_in_load_universe": OLD_LOAD in text,
        "uses_strategy_base_in_health_check": OLD_HEALTH in text,
        "has_sp500_in_load_universe": (
            '        universe_name = TECHNICAL_SCAN_UNIVERSE\n' in text
            or NEW_LOAD in text
        ),
        "has_sp500_in_health_check": (
            '            "universe_name": TECHNICAL_SCAN_UNIVERSE,\n' in text
            or NEW_HEALTH in text
        ),
        "has_named_constant": CONST in text,
    }


def render_status(status: dict) -> None:
    for key, value in status.items():
        print(f"{key}: {value}")


def apply_patch(text: str) -> str:
    # Refuse to touch an unexpected version. Human beings call this
    # "being careful"; computers call it "not destroying Tuesday".
    if OLD_LOAD not in text and (
        'universe_name = TECHNICAL_SCAN_UNIVERSE' not in text
        and NEW_LOAD not in text
    ):
        raise RuntimeError(
            "Expected load_universe base-universe line was not found. "
            "Refusing to patch an unknown scanner version."
        )

    if OLD_HEALTH not in text and (
        '"universe_name": TECHNICAL_SCAN_UNIVERSE' not in text
        and NEW_HEALTH not in text
    ):
        raise RuntimeError(
            "Expected health_check universe line was not found. "
            "Refusing to patch an unknown scanner version."
        )

    if CONST not in text:
        if MARKER not in text:
            raise RuntimeError(
                "Could not find PROXY_SYMBOLS marker for constant insertion."
            )
        text = text.replace(
            MARKER,
            MARKER + CONST,
            1,
        )

    text = text.replace(
        OLD_LOAD,
        '        universe_name = TECHNICAL_SCAN_UNIVERSE\n',
        1,
    )
    text = text.replace(
        OLD_HEALTH,
        '            "universe_name": TECHNICAL_SCAN_UNIVERSE,\n',
        1,
    )

    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        raise RuntimeError(f"Target file not found: {TARGET}")

    text = TARGET.read_text(encoding="utf-8")

    if args.check:
        render_status(inspect(text))
        print("TECHNICAL SCANNER S&P500 PATCH CHECK: OK")
        return

    patched = apply_patch(text)

    if patched == text:
        print("No changes needed. Scanner already uses sp500_scan.")
        render_status(inspect(text))
        print("TECHNICAL SCANNER S&P500 PATCH: OK")
        return

    shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(patched, encoding="utf-8")

    status = inspect(patched)
    render_status(status)

    if not (
        status["has_sp500_in_load_universe"]
        and status["has_sp500_in_health_check"]
        and status["has_named_constant"]
    ):
        shutil.copy2(BACKUP, TARGET)
        raise RuntimeError(
            "Post-patch verification failed. Original scanner restored."
        )

    print(f"Backup: {BACKUP}")
    print("Strategy config changed: false")
    print("Company-news universe changed: false")
    print("Technical scanner universe: sp500_scan")
    print("TECHNICAL SCANNER S&P500 PATCH: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
