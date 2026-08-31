# helpers/patch_runtime_stability.py
# DELTAX runtime stability hotfix:
# 1) news_worker advisory-lock connection uses autocommit and non-fatal unlock
# 2) stock intent JSONB serialization supports datetime/date/Decimal
#
# This script edits code only. It does not touch the DB or Alpaca.

from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]

NEWS = ROOT / "deltax" / "news_worker.py"
STOCK = ROOT / "deltax" / "stock_trade_intent_builder.py"


def backup(path: Path, suffix: str) -> None:
    target = path.with_suffix(path.suffix + suffix)
    if not target.exists():
        shutil.copy2(path, target)
        print(f"Backup: {target}")


def patch_news_worker() -> None:
    text = NEWS.read_text(encoding="utf-8")

    old_release = '''def release_lock(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
'''

    new_release = '''def release_lock(connection) -> None:
    # Session-level advisory locks are automatically released by Postgres if
    # the connection dies. Unlock is therefore best-effort and must never turn
    # a successful multi-minute news run into a failed worker.
    try:
        if connection.closed:
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    except psycopg.Error as exc:
        print(f"NEWS WORKER LOCK RELEASE WARNING: {exc}", file=sys.stderr)
'''

    old_connect = '''    with psycopg.connect(DATABASE_URL) as lock_connection:
'''

    new_connect = '''    # Advisory-lock connection must not sit idle inside a transaction while
    # OpenAI/news subprocesses run for several minutes. autocommit keeps the
    # session lock alive without triggering idle_in_transaction_session_timeout.
    with psycopg.connect(DATABASE_URL, autocommit=True) as lock_connection:
'''

    changed = False

    if new_release not in text:
        if old_release not in text:
            raise RuntimeError("news_worker.py release_lock patch point not found")
        text = text.replace(old_release, new_release, 1)
        changed = True

    if new_connect not in text:
        if old_connect not in text:
            raise RuntimeError("news_worker.py connection patch point not found")
        text = text.replace(old_connect, new_connect, 1)
        changed = True

    if changed:
        backup(NEWS, ".runtime_backup")
        NEWS.write_text(text, encoding="utf-8")
        print(f"Patched: {NEWS}")
    else:
        print("news_worker.py already patched")


def patch_stock_builder() -> None:
    text = STOCK.read_text(encoding="utf-8")

    marker = '''def decimal_value(value: Any, default: Decimal | None = None) -> Decimal | None:
'''

    helper = '''def jsonb_value(value: Any) -> Jsonb:
    # psycopg's default JSON encoder cannot serialize datetime/Decimal values
    # that legitimately appear in thesis metadata and risk-gate details.
    return Jsonb(
        value,
        dumps=lambda obj: json.dumps(obj, default=json_default),
    )


''' + marker

    changed = False

    if "def jsonb_value(" not in text:
        if marker not in text:
            raise RuntimeError("stock_trade_intent_builder.py helper patch point not found")
        text = text.replace(marker, helper, 1)
        changed = True

    # Risk-event details can contain cooldown timestamps.
    old_risk = '''                Jsonb(
                    {
                        "trade_thesis_id": str(thesis["id"]),
'''
    new_risk = '''                jsonb_value(
                    {
                        "trade_thesis_id": str(thesis["id"]),
'''
    if old_risk in text:
        text = text.replace(old_risk, new_risk, 1)
        changed = True

    # Intent metadata definitely contains signal_at / checked_at datetimes.
    old_metadata = '''                Jsonb(metadata),
'''
    new_metadata = '''                jsonb_value(metadata),
'''
    if old_metadata in text:
        text = text.replace(old_metadata, new_metadata, 1)
        changed = True

    if changed:
        backup(STOCK, ".runtime_backup")
        STOCK.write_text(text, encoding="utf-8")
        print(f"Patched: {STOCK}")
    else:
        print("stock_trade_intent_builder.py already patched")


def main() -> None:
    for path in (NEWS, STOCK):
        if not path.exists():
            raise FileNotFoundError(path)

    patch_news_worker()
    patch_stock_builder()

    print("")
    print("RUNTIME STABILITY PATCH: COMPLETE")
    print("No database writes performed.")
    print("No Alpaca orders submitted.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
