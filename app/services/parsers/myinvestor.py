"""Parser for MyInvestor account exports (CSV)."""
import io
import re
from decimal import Decimal

import pandas as pd

from app.services.parsers.broker_types import D, ParseResult, Position

# Rows whose concept contains one of these are cash movements, not fund trades.
SKIP_WORDS = [
    "Transferencia", "Traspaso", "PERIODO", "Liquidación",
    "Liquidacion", "Intereses", "Retención", "Retencion",
]


def is_fund_row(concept: str) -> bool:
    c = str(concept).strip()
    if any(w in c for w in SKIP_WORDS):
        return False
    # Fund names are printed in upper case, so a lowercase letter means the row
    # is some other kind of movement.
    if re.search(r"[a-z]", c):
        return False
    return True


def parse(content: bytes) -> ParseResult:
    warnings: list[str] = []
    # MyInvestor exports latin1 with ';' and European decimals.
    try:
        df = pd.read_csv(io.BytesIO(content), sep=";", encoding="latin1")
    except Exception as e:
        return ParseResult("myinvestor", [], [f"Could not read the CSV: {e}"])

    if "Importe" not in df.columns or "Concepto" not in df.columns:
        return ParseResult(
            "myinvestor", [],
            ["The CSV has no 'Concepto' and 'Importe' columns."],
        )

    df["Importe"] = (
        df["Importe"].astype(str)
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
        .astype(float)
    )
    df = df[df["Concepto"].apply(is_fund_row)]
    df["Fund"] = df["Concepto"].apply(lambda x: str(x).split("@")[0].strip())
    df["Invested"] = df["Importe"].apply(lambda v: abs(v) if v < 0 else 0)
    df["Withdrawn"] = df["Importe"].apply(lambda v: v if v > 0 else 0)

    agg = df.groupby("Fund")[["Invested", "Withdrawn"]].sum().reset_index()
    agg = agg[(agg["Invested"] > 0.01) | (agg["Withdrawn"] > 0.01)]

    positions: list[Position] = []
    for _, row in agg.iterrows():
        inv = D(row["Invested"])
        wdr = D(row["Withdrawn"])
        positions.append(Position(
            asset=str(row["Fund"]),
            invested=inv,
            withdrawn=wdr,
            # Money in and money out nearly match: the fund was probably sold.
            looks_sold=bool(inv > 0 and abs(inv - wdr) < Decimal("50")),
        ))

    if not positions:
        warnings.append("No investment funds were detected in the CSV.")
    return ParseResult("myinvestor", positions, warnings)
