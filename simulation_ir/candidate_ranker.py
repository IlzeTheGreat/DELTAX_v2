from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


# ============================================================
# PATHS
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

TRADES_FILE = SCRIPT_DIR / "iran_event_strategy_sp500_trades.csv"
ORIGINAL_UNIVERSE_FILE = ROOT_DIR / "stocks.txt"

OUTPUT_ALL = SCRIPT_DIR / "iran_event_candidates_all.csv"
OUTPUT_NEW = SCRIPT_DIR / "iran_event_candidates_new.csv"
OUTPUT_TOP = SCRIPT_DIR / "iran_event_candidates_top.csv"


# ============================================================
# FILTERS
# ============================================================

MIN_TRADES = 2
MIN_WIN_RATE = 0.66
MIN_AVG_RETURN = 0.01

TOP_N = 30


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )


# ============================================================
# HELPERS
# ============================================================

def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def load_original_universe() -> set[str]:
    if not ORIGINAL_UNIVERSE_FILE.exists():
        raise FileNotFoundError(
            f"Original stocks.txt not found:\n{ORIGINAL_UNIVERSE_FILE}"
        )

    symbols = set()

    with open(
        ORIGINAL_UNIVERSE_FILE,
        "r",
        encoding="utf-8",
    ) as f:

        for line in f:
            symbol = line.strip().upper()

            if not symbol:
                continue

            if symbol.startswith("#"):
                continue

            symbols.add(symbol)

    return symbols


# ============================================================
# LOAD TRADES
# ============================================================

def load_trades() -> pd.DataFrame:

    if not TRADES_FILE.exists():
        raise FileNotFoundError(
            f"Trades file not found:\n{TRADES_FILE}"
        )

    df = pd.read_csv(TRADES_FILE)

    required = {
        "threshold",
        "trading_date",
        "symbol",
        "trade_direction",
        "prior_relative_return",
        "opening_return",
        "trade_return",
    }

    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Missing required columns: {sorted(missing)}"
        )

    for col in [
        "threshold",
        "prior_relative_return",
        "opening_return",
        "trade_return",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "symbol",
            "trade_direction",
            "trade_return",
            "threshold",
        ]
    )

    return df


# ============================================================
# RANKING
# ============================================================

def build_rankings(
    trades: pd.DataFrame,
    original_symbols: set[str],
) -> pd.DataFrame:

    # Use only the best tested threshold
    trades = trades[
        trades["threshold"] == 0.005
    ].copy()

    if trades.empty:
        raise RuntimeError(
            "No trades found for 0.50% threshold"
        )

    grouped = (
        trades
        .groupby(
            [
                "symbol",
                "trade_direction",
            ]
        )
        .agg(
            trades=(
                "trade_return",
                "count",
            ),
            wins=(
                "trade_return",
                lambda x:
                    int((x > 0).sum()),
            ),
            losses=(
                "trade_return",
                lambda x:
                    int((x <= 0).sum()),
            ),
            win_rate=(
                "trade_return",
                lambda x:
                    (x > 0).mean(),
            ),
            avg_return=(
                "trade_return",
                "mean",
            ),
            median_return=(
                "trade_return",
                "median",
            ),
            total_return=(
                "trade_return",
                "sum",
            ),
            best_trade=(
                "trade_return",
                "max",
            ),
            worst_trade=(
                "trade_return",
                "min",
            ),
            avg_prior_relative=(
                "prior_relative_return",
                "mean",
            ),
            avg_opening_move=(
                "opening_return",
                "mean",
            ),
        )
        .reset_index()
    )

    grouped["is_original_119"] = (
        grouped["symbol"].isin(
            original_symbols
        )
    )

    grouped["candidate_type"] = grouped[
        "is_original_119"
    ].map(
        {
            True: "ORIGINAL",
            False: "NEW_CANDIDATE",
        }
    )

    # --------------------------------------------------------
    # Quality filters
    # --------------------------------------------------------

    grouped["passes_filter"] = (
        (grouped["trades"] >= MIN_TRADES)
        & (
            grouped["win_rate"]
            >= MIN_WIN_RATE
        )
        & (
            grouped["avg_return"]
            >= MIN_AVG_RETURN
        )
    )

    # --------------------------------------------------------
    # Ranking score
    #
    # Rewards:
    # - average return
    # - win rate
    # - repeated signals
    #
    # log-like cap via trades factor to avoid a single metric
    # dominating too much.
    # --------------------------------------------------------

    grouped["score"] = (
        grouped["avg_return"]
        * grouped["win_rate"]
        * grouped["trades"].clip(
            upper=5
        )
    )

    grouped = grouped.sort_values(
        [
            "passes_filter",
            "score",
            "avg_return",
        ],
        ascending=[
            False,
            False,
            False,
        ],
    ).reset_index(drop=True)

    return grouped


# ============================================================
# PRINT
# ============================================================

def print_section(
    title: str,
    df: pd.DataFrame,
) -> None:

    print()
    print("=" * 100)
    print(title)
    print("=" * 100)

    if df.empty:
        print("No candidates.")
        return

    for _, r in df.iterrows():

        print(
            f"{r['symbol']:6} "
            f"{r['trade_direction']:5} | "
            f"trades={int(r['trades']):2} | "
            f"win={r['win_rate'] * 100:5.1f}% | "
            f"avg={pct(r['avg_return']):>8} | "
            f"median={pct(r['median_return']):>8} | "
            f"best={pct(r['best_trade']):>8} | "
            f"worst={pct(r['worst_trade']):>8} | "
            f"{r['candidate_type']}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    print("=" * 100)
    print("IRAN EVENT CANDIDATE RANKER")
    print("=" * 100)

    print()
    print(
        f"Filters: trades >= {MIN_TRADES}, "
        f"win rate >= {MIN_WIN_RATE * 100:.0f}%, "
        f"avg return >= {MIN_AVG_RETURN * 100:.2f}%"
    )

    original_symbols = load_original_universe()

    print(
        f"Original universe symbols: "
        f"{len(original_symbols)}"
    )

    trades = load_trades()

    print(
        f"Trades loaded: {len(trades):,}"
    )

    rankings = build_rankings(
        trades,
        original_symbols,
    )

    # --------------------------------------------------------
    # All
    # --------------------------------------------------------

    rankings.to_csv(
        OUTPUT_ALL,
        index=False,
    )

    # --------------------------------------------------------
    # Qualified
    # --------------------------------------------------------

    qualified = rankings[
        rankings["passes_filter"]
    ].copy()

    new_candidates = qualified[
        qualified["candidate_type"]
        == "NEW_CANDIDATE"
    ].copy()

    new_candidates.to_csv(
        OUTPUT_NEW,
        index=False,
    )

    # --------------------------------------------------------
    # Top shortlist
    # --------------------------------------------------------

    top = qualified.head(
        TOP_N
    ).copy()

    top.to_csv(
        OUTPUT_TOP,
        index=False,
    )

    # --------------------------------------------------------
    # Console
    # --------------------------------------------------------

    print_section(
        "TOP QUALIFIED CANDIDATES",
        top,
    )

    print_section(
        "TOP NEW CANDIDATES OUTSIDE ORIGINAL 119",
        new_candidates.head(TOP_N),
    )

    long_new = new_candidates[
        new_candidates[
            "trade_direction"
        ] == "LONG"
    ].head(15)

    short_new = new_candidates[
        new_candidates[
            "trade_direction"
        ] == "SHORT"
    ].head(15)

    print_section(
        "BEST NEW LONG CANDIDATES",
        long_new,
    )

    print_section(
        "BEST NEW SHORT CANDIDATES",
        short_new,
    )

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)

    print(
        f"Qualified candidates: "
        f"{len(qualified)}"
    )

    print(
        f"New candidates: "
        f"{len(new_candidates)}"
    )

    print(
        f"New LONG candidates: "
        f"{len(new_candidates[new_candidates['trade_direction'] == 'LONG'])}"
    )

    print(
        f"New SHORT candidates: "
        f"{len(new_candidates[new_candidates['trade_direction'] == 'SHORT'])}"
    )

    print()
    print("FILES CREATED")
    print("-" * 100)

    print(
        f"All rankings:       {OUTPUT_ALL}"
    )

    print(
        f"New candidates:     {OUTPUT_NEW}"
    )

    print(
        f"Top shortlist:      {OUTPUT_TOP}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())