import os
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


def test_database() -> None:
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM schema_migrations
                        WHERE version = '002_seed_strategy_and_universes'
                    ),
                    (SELECT COUNT(*) FROM universes),
                    (
                        SELECT COUNT(*)
                        FROM universe_memberships um
                        JOIN universes u ON u.id = um.universe_id
                        WHERE u.code = 'alyrise_base'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM strategy_configs
                        WHERE version = 'deltax_v2_strategy_v1'
                          AND is_active = true
                    );
                """
            )

            result = cursor.fetchone()

    migration_saved, universe_count, member_count, strategy_active = result

    assert migration_saved is True
    assert universe_count == 5
    assert member_count == 119
    assert strategy_active is True

    print("DATABASE: OK")
    print(f"Universes: {universe_count}")
    print(f"Alyrise members: {member_count}")
    print("Strategy active: yes")


def test_alpaca() -> None:
    trading_url = os.environ["ALPACA_TRADING_URL_PAPER"].rstrip("/")
    api_key = os.environ["ALPACA_API_KEY_PAPER"]
    api_secret = os.environ["ALPACA_API_SECRET_PAPER"]

    response = requests.get(
        f"{trading_url}/account",
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        },
        timeout=15,
    )

    response.raise_for_status()
    account = response.json()

    print("ALPACA PAPER: OK")
    print(f"Account status: {account['status']}")
    print(f"Equity: ${account['equity']}")
    print(f"Buying power: ${account['buying_power']}")


if __name__ == "__main__":
    test_database()
    test_alpaca()
    print("ALL CONNECTIONS: OK")