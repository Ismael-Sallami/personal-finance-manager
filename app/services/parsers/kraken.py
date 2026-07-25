"""Parser for Kraken trade exports (trades.csv).

Handles pairs quoted in EUR, USD, USDT and USDC.
"""
import io
from decimal import Decimal

import pandas as pd

from app.services.parsers.broker_types import D, ParseResult, Position

QUOTES = ("/EUR", "/USD", "/USDT", "/USDC")


def parse(content: bytes) -> ParseResult:
    warnings: list[str] = []
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as e:
        return ParseResult("kraken", [], [f"Could not read the CSV: {e}"])

    needed = {"pair", "type", "cost", "vol"}
    if not needed.issubset(df.columns):
        return ParseResult(
            "kraken", [],
            [f"The CSV has no {needed} columns. Found: {list(df.columns)}"],
        )

    mask = df["pair"].astype(str).str.endswith(QUOTES)
    df_q = df[mask].copy()
    if df_q.empty:
        warnings.append("No pairs quoted in EUR/USD/USDT/USDC were found.")

    positions: list[Position] = []
    for pair in df_q["pair"].unique():
        d = df_q[df_q["pair"] == pair]
        buys = d[d["type"] == "buy"]
        sells = d[d["type"] == "sell"]
        net_invested = buys["cost"].sum() - sells["cost"].sum()
        volume = buys["vol"].sum() - sells["vol"].sum()
        # Keep the pair if coins are still held or money is still committed.
        if volume > 1e-7 or abs(net_invested) > 0.01:
            positions.append(Position(
                asset=str(pair),
                quantity=Decimal(str(round(volume, 8))),
                invested=D(net_invested),
                withdrawn=Decimal("0.00"),
            ))
    return ParseResult("kraken", positions, warnings)
