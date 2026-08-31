# helpers/patch_scan_cycle_expiry.py
# Fixes Core/Active thesis expiry when a historical news confirmation deadline
# is earlier than the current technical signal time.

from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deltax" / "scan_cycle.py"
BACKUP = TARGET.with_suffix(".py.expiry_backup")


def main():
    if not TARGET.exists():
        raise FileNotFoundError(TARGET)

    text = TARGET.read_text(encoding="utf-8")

    old = '''        else:
            expires_at = candidate.confirmation_due_at + timedelta(
                minutes=CORE_ACTIVE_POST_CONFIRMATION_TTL_MINUTES
            )
'''

    new = '''        else:
            # A pre-market/news-momentum confirmation can already be in the
            # past when a fresh technical signal is detected later in-session.
            # The thesis must still remain valid after its actual signal time.
            expiry_anchor = max(
                candidate.signal_at,
                candidate.confirmation_due_at,
            )
            expires_at = expiry_anchor + timedelta(
                minutes=CORE_ACTIVE_POST_CONFIRMATION_TTL_MINUTES
            )
'''

    if new in text:
        print("scan_cycle.py already contains expiry fix.")
        return

    if old not in text:
        raise RuntimeError(
            "Expected expiry block not found. "
            "Do not modify scan_cycle.py manually from this patch."
        )

    shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(
        text.replace(old, new, 1),
        encoding="utf-8",
    )

    print(f"Patched: {TARGET}")
    print(f"Backup:  {BACKUP}")
    print(
        "Core/Active expiry now anchors to the later of "
        "signal_at and confirmation_due_at."
    )
    print("No DB writes or broker orders were performed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
