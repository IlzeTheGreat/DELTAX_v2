import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed
from alpaca.data.historical import (
    OptionHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    StockLatestQuoteRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from alpaca.trading.requests import GetOptionContractsRequest


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]
DATA_FEED = os.environ["ALPACA_DATA_FEED_PAPER"].lower()


def test_trading_client() -> TradingClient:
    client = TradingClient(
        api_key=API_KEY,
        secret_key=API_SECRET,
        paper=True,
    )

    account = client.get_account()

    print("TRADING SDK: OK")
    print(f"Status: {account.status}")
    print(f"Equity: ${account.equity}")

    return client


def test_stock_data() -> None:
    client = StockHistoricalDataClient(API_KEY, API_SECRET)

    feed = DataFeed.SIP if DATA_FEED == "sip" else DataFeed.IEX

    request = StockLatestQuoteRequest(
        symbol_or_symbols=["SPY"],
        feed=feed,
    )

    quotes = client.get_stock_latest_quote(request)
    quote = quotes["SPY"]

    print("STOCK DATA SDK: OK")
    print(f"Feed: {feed.value}")
    print(f"SPY bid: ${quote.bid_price}")
    print(f"SPY ask: ${quote.ask_price}")
    print(f"Timestamp: {quote.timestamp}")


def test_option_data(trading_client: TradingClient) -> None:
    contract_request = GetOptionContractsRequest(
        underlying_symbols=["SPY"],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=date.today(),
        expiration_date_lte=date.today() + timedelta(days=45),
        limit=10,
    )

    response = trading_client.get_option_contracts(contract_request)
    contracts = response.option_contracts

    if not contracts:
        raise RuntimeError("No active SPY option contracts returned")

    contract_symbol = contracts[0].symbol

    option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)

    quote_request = OptionLatestQuoteRequest(
        symbol_or_symbols=[contract_symbol]
    )

    quotes = option_client.get_option_latest_quote(quote_request)
    quote = quotes[contract_symbol]

    print("OPTION DATA SDK: OK")
    print(f"Contract: {contract_symbol}")
    print(f"Bid: ${quote.bid_price}")
    print(f"Ask: ${quote.ask_price}")
    print(f"Timestamp: {quote.timestamp}")


if __name__ == "__main__":
    trading_client = test_trading_client()
    test_stock_data()
    test_option_data(trading_client)

    print("ALL ALPACA SDK TESTS: OK")