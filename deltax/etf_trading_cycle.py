# File: helpers/etf_trading_cycle.py
# Purpose: DELTAX ETF live paper-trading cycle for the EVENT Alpaca account.
#
# Order of operations:
#   1) account-level daily risk gate
#   2) ETF exits
#   3) ETF entries (only if daily drawdown < 3%)
#
# Daily risk:
#   <= -3%: no new ETF entries; exits still run
#   <= -5%: kill switch; close all managed ETF share positions, no new entries
#
# Expected existing scripts:
#   helpers/etf_exit_manager.py
#   helpers/etf_signal_executor.py
#
# This wrapper is intended for Windows Task Scheduler every 5 minutes.
# It writes its own log because pythonw.exe has no console.

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "etf_trading_cycle.log"

EXIT_SCRIPT_CANDIDATES = (
    PROJECT_ROOT / "helpers" / "etf_exit_manager.py",
    PROJECT_ROOT / "deltax" / "etf_exit_manager.py",
)
ENTRY_SCRIPT_CANDIDATES = (
    PROJECT_ROOT / "helpers" / "etf_signal_executor.py",
    PROJECT_ROOT / "deltax" / "etf_signal_executor.py",
)

MANAGED_ETFS = {
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLV", "XLC", "XLY", "XLP", "XLI", "XLE", "XLU", "XLB", "XLRE",
    "SMH", "IGV", "CIBR", "XBI", "IHI", "KRE", "IAI", "IYT", "ITA", "XOP",
    "GLD", "TLT", "BIL", "USO",
}

NO_NEW_ENTRY_DRAWDOWN_PCT = -3.0
KILL_SWITCH_DRAWDOWN_PCT = -5.0
ENTRY_NOTIONAL_USD = 4000.0
MAX_NEW_TRADES_PER_CYCLE = 5
HTTP_TIMEOUT_SECONDS = 20
MAX_STAGE_OUTPUT_CHARS = 16000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now_iso()}] {message}"
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def compact(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_STAGE_OUTPUT_CHARS:
        return text
    return "...[truncated]...\n" + text[-MAX_STAGE_OUTPUT_CHARS:]


def first_existing(candidates: tuple[Path, ...]) -> Path:
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "Could not find required script. Checked: "
        + ", ".join(str(path) for path in candidates)
    )


def load_config() -> dict[str, str]:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("ALPACA_API_KEY_EVENT")
    secret = os.getenv("ALPACA_API_SECRET_EVENT")
    base = (os.getenv("ALPACA_TRADING_URL_EVENT") or "https://paper-api.alpaca.markets").rstrip("/")

    if not key or not secret:
        raise RuntimeError(
            "Missing ALPACA_API_KEY_EVENT / ALPACA_API_SECRET_EVENT in .env"
        )

    return {
        "key": key,
        "secret": secret,
        "base": base,
    }


class AlpacaEvent:
    def __init__(self, cfg: dict[str, str]):
        self.base = cfg["base"]
        self.headers = {
            "APCA-API-KEY-ID": cfg["key"],
            "APCA-API-SECRET-KEY": cfg["secret"],
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        response = requests.request(
            method,
            f"{self.base}{path}",
            headers=self.headers,
            params=params,
            json=json_body,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise RuntimeError(
                f"Alpaca {method} {path} failed "
                f"{response.status_code}: {response.text[:1000]}"
            )
        if not response.text.strip():
            return None
        return response.json()

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/account")

    def clock(self) -> dict[str, Any]:
        return self._request("GET", "/clock")

    def positions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/positions")

    def open_orders(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/orders",
            params={"status": "open", "limit": 500, "direction": "desc"},
        )

    def submit_market_close(
        self,
        *,
        symbol: str,
        qty: str,
        side: str,
        client_order_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/orders",
            json_body={
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )


def daily_drawdown_pct(account: dict[str, Any]) -> float:
    equity = float(account.get("equity") or 0)
    last_equity = float(account.get("last_equity") or 0)

    if last_equity <= 0:
        raise RuntimeError("Alpaca account last_equity is unavailable or invalid.")

    return (equity / last_equity - 1.0) * 100.0


def safe_qty(raw_qty: Any) -> str:
    qty = abs(float(raw_qty))
    if qty <= 0:
        raise ValueError("qty must be > 0")
    if qty.is_integer():
        return str(int(qty))
    return f"{qty:.8f}".rstrip("0").rstrip(".")


def kill_switch_close_all_managed(api: AlpacaEvent) -> list[dict[str, Any]]:
    positions = api.positions()
    open_orders = api.open_orders()

    symbols_with_open_exit = {
        str(order.get("symbol") or "").upper()
        for order in open_orders
        if str(order.get("client_order_id") or "").lower().startswith("dxe-etfx-")
    }

    actions: list[dict[str, Any]] = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    for pos in positions:
        symbol = str(pos.get("symbol") or "").upper()
        if symbol not in MANAGED_ETFS:
            continue

        qty_float = float(pos.get("qty") or 0)
        if qty_float == 0:
            continue

        if symbol in symbols_with_open_exit:
            actions.append({
                "symbol": symbol,
                "status": "SKIPPED_OPEN_EXIT_EXISTS",
            })
            continue

        side = "sell" if qty_float > 0 else "buy"
        cid = f"dxe-etfx-{stamp}-{symbol.lower()}-kill"[:48]

        order = api.submit_market_close(
            symbol=symbol,
            qty=safe_qty(qty_float),
            side=side,
            client_order_id=cid,
        )
        actions.append({
            "symbol": symbol,
            "qty": safe_qty(qty_float),
            "side": side,
            "status": order.get("status"),
            "order_id": order.get("id"),
            "client_order_id": cid,
        })

    return actions


def run_script(stage: str, script: Path, args: list[str]) -> dict[str, Any]:
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    started = now_iso()
    t0 = time.monotonic()

    process = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        check=False,
    )

    result = {
        "stage": stage,
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "duration_seconds": round(time.monotonic() - t0, 3),
        "started_at": started,
        "stdout": compact(process.stdout),
        "stderr": compact(process.stderr),
    }

    log(json.dumps(result, ensure_ascii=False, default=str))
    return result


def main() -> int:
    started = time.monotonic()
    log("ETF_CYCLE START")

    cfg = load_config()
    api = AlpacaEvent(cfg)
    account = api.account()
    clock = api.clock()

    drawdown = daily_drawdown_pct(account)
    is_open = bool(clock.get("is_open"))

    summary: dict[str, Any] = {
        "status": "ok",
        "market_open": is_open,
        "equity": float(account.get("equity") or 0),
        "last_equity": float(account.get("last_equity") or 0),
        "daily_drawdown_pct": round(drawdown, 4),
        "entry_gate": "ALLOW",
        "stages": [],
    }

    # No orders outside regular market hours. Scheduled task can still run;
    # the cycle exits quickly and safely.
    if not is_open:
        summary["status"] = "market_closed"
        summary["entry_gate"] = "BLOCK_MARKET_CLOSED"
        summary["total_duration_seconds"] = round(time.monotonic() - started, 3)
        log("ETF_CYCLE END " + json.dumps(summary, ensure_ascii=False))
        return 0

    # Account-level kill switch has priority over all strategy logic.
    if drawdown <= KILL_SWITCH_DRAWDOWN_PCT:
        summary["status"] = "kill_switch"
        summary["entry_gate"] = "BLOCK_KILL_SWITCH"
        summary["kill_actions"] = kill_switch_close_all_managed(api)
        summary["total_duration_seconds"] = round(time.monotonic() - started, 3)
        log("ETF_CYCLE END " + json.dumps(summary, ensure_ascii=False, default=str))
        return 0

    exit_script = first_existing(EXIT_SCRIPT_CANDIDATES)
    exit_result = run_script(
        "etf_exit_manager",
        exit_script,
        ["--execute"],
    )
    summary["stages"].append(exit_result)

    # Fail closed. If exit management itself failed, do not create fresh risk.
    if not exit_result["ok"]:
        summary["status"] = "failed"
        summary["entry_gate"] = "BLOCK_EXIT_STAGE_FAILED"
        summary["total_duration_seconds"] = round(time.monotonic() - started, 3)
        log("ETF_CYCLE END " + json.dumps(summary, ensure_ascii=False, default=str))
        return 1

    # -3% daily drawdown: manage exits, but no new positions.
    if drawdown <= NO_NEW_ENTRY_DRAWDOWN_PCT:
        summary["status"] = "risk_gate_no_new_entries"
        summary["entry_gate"] = "BLOCK_DAILY_DRAWDOWN"
        summary["total_duration_seconds"] = round(time.monotonic() - started, 3)
        log("ETF_CYCLE END " + json.dumps(summary, ensure_ascii=False, default=str))
        return 0

    entry_script = first_existing(ENTRY_SCRIPT_CANDIDATES)
    entry_result = run_script(
        "etf_signal_executor",
        entry_script,
        [
            "--execute",
            "--notional", str(ENTRY_NOTIONAL_USD),
            "--max-trades", str(MAX_NEW_TRADES_PER_CYCLE),
        ],
    )
    summary["stages"].append(entry_result)

    if not entry_result["ok"]:
        early_confirmation_message = (
            "Live confirmation starts at" in (entry_result.get("stdout") or "")
        )

        if early_confirmation_message:
            summary["status"] = "waiting_for_confirmation"
            summary["entry_gate"] = "BLOCK_BEFORE_0940_ET"
        else:
            summary["status"] = "failed"
            summary["entry_gate"] = "ENTRY_STAGE_FAILED"

    summary["total_duration_seconds"] = round(time.monotonic() - started, 3)
    log("ETF_CYCLE END " + json.dumps(summary, ensure_ascii=False, default=str))

    return 0 if summary["status"] != "failed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("ETF_CYCLE INTERRUPTED")
        raise SystemExit(130)
    except Exception as exc:
        log(f"ETF_CYCLE ERROR {type(exc).__name__}: {exc}")
        raise
