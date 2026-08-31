# helpers/patch_options_builder_keyerror.py
from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deltax" / "options_spread_intent_builder.py"
BACKUP = TARGET.with_suffix(".py.keyerror_backup")


def main():
    if not TARGET.exists():
        raise FileNotFoundError(TARGET)

    text = TARGET.read_text(encoding="utf-8")

    if 'SELECT COUNT(*) AS count' in text and 'cursor.fetchone()["count"]' in text:
        print("options_spread_intent_builder.py already contains KeyError fix.")
        return

    old_select = '            SELECT COUNT(*)\n'
    old_return = '        return int(cursor.fetchone()[0])\n'

    if old_return not in text:
        raise RuntimeError("Expected cursor.fetchone()[0] not found.")

    # Only the sector_idea_count query uses the broken numeric access.
    idx = text.find(old_return)
    select_idx = text.rfind(old_select, 0, idx)
    if select_idx == -1:
        raise RuntimeError("Expected COUNT(*) query not found before broken return.")

    text = (
        text[:select_idx]
        + '            SELECT COUNT(*) AS count\n'
        + text[select_idx + len(old_select):idx]
        + '        return int(cursor.fetchone()["count"])\n'
        + text[idx + len(old_return):]
    )

    shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(text, encoding="utf-8")

    print(f"Patched: {TARGET}")
    print(f"Backup:  {BACKUP}")
    print("Fixed KeyError(0) in sector_idea_count().")
    print("No DB writes or Alpaca orders were performed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
