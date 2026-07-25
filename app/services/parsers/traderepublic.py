"""Parser for Trade Republic statements (PDF), read with pdfplumber.

The statement is a text PDF: the text of every page is flattened into one
string, split by transaction date, and each block is matched with regexes.
Positions are then grouped by ISIN, with an average cost and the P&L already
realised by partial sells.
"""
import io
import re
from decimal import Decimal

import pdfplumber

from app.services.parsers.broker_types import D, ParseResult, Position

# Header and footer lines that carry no transaction data.
BOILERPLATE = [
    "TRADE REPUBLIC BANK", "Page", "Página", "Pagina",
    "Statement summary", "Resumen de estado de cuenta",
]

# ISIN: two country letters, nine alphanumerics, one check digit.
ISIN_RE = r"[A-Z]{2}[A-Z0-9]{9}[0-9]"


def _parse_float(val: str) -> float:
    if not val:
        return 0.0
    try:
        return float(val.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _clean_name(n: str) -> str:
    n = n.replace("Comercio", "").replace("trade", "")
    return re.sub(r"\bdl[-.,\s\d]*$", "", n, flags=re.IGNORECASE).strip()


def _extract_transactions(text_full: str) -> list[dict]:
    lines = text_full.split("\n")
    clean = [line for line in lines if not any(b in line for b in BOILERPLATE)]
    text = "\n".join(clean).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    # The PDF sometimes glues the amount to the quantity. Split them again.
    text = re.sub(r"(quantity:\s*\d+)(\d\.\d{3},\d{2})", r"\1 \2", text)
    text = re.sub(r"(quantity:\s*\d+\.\d{6})(\d+,\d{2})", r"\1 \2", text)
    text = re.sub(r"(quantity:\s*\d+?)(\d{1,5},\d{2})", r"\1 \2", text)

    parts = re.split(r"(\d{2}\s+[a-zA-Z]{3,4}\s+\d{4})", text)
    rows: list[dict] = []

    for i in range(1, len(parts), 2):
        trade_date = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if content.startswith("-") or len(content) < 5:
            continue

        # Each block ends with "amount balance"; the amount is the second to last.
        money = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€?", content)
        amount_str = money[-2] if len(money) >= 2 else "0,00"
        amount = _parse_float(amount_str)

        desc = content
        for m in money:
            desc = desc.replace(m, "").replace("€", "")

        op = isin = name = ""
        quantity = "0"

        std = re.search(
            rf"(Buy|Sell|Savings).*?({ISIN_RE}).*?quantity:\s*([\d\.]+)",
            desc, re.IGNORECASE,
        )
        if std:
            word = std.group(1).lower()
            op = "Savings" if "sav" in word else ("Buy" if "buy" in word else "Sell")
            isin, quantity = std.group(2), std.group(3)
            start = desc.find(isin) + len(isin)
            end = desc.find("quantity")
            name = _clean_name(desc[start:end]) if end > start else isin
        else:
            # Some products print the ISIN and the quantity without the
            # Buy/Sell wording. Treat those as a buy unless "Sell" appears.
            loose = re.search(rf"({ISIN_RE}).*?quantity:\s*([\d\.]+)", desc, re.IGNORECASE)
            if loose:
                isin, quantity = loose.group(1), loose.group(2)
                op = "Sell" if re.search(r"\bsell\b", desc, re.IGNORECASE) else "Buy"
                name = isin
            elif "Interest" in desc or "Dividend" in desc:
                op, name = "Cash", "Interest/Dividends"

        if op:
            rows.append({
                "date": trade_date, "op": op, "isin": isin,
                "name": name, "quantity": quantity, "total": amount,
            })
    return rows


def parse(content: bytes) -> ParseResult:
    warnings: list[str] = []
    try:
        text_full = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text_full += (page.extract_text() or "") + "\n"
    except Exception as e:
        return ParseResult("traderepublic", [], [f"Could not read the PDF: {e}"])

    txs = _extract_transactions(text_full)

    # Group by ISIN, keeping the longest name seen for each one.
    by_isin: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for row in txs:
        if row["op"] == "Cash" or not row["isin"]:
            continue
        isin = row["isin"]
        by_isin.setdefault(isin, [])
        if isin not in names or len(row["name"]) > len(names[isin]):
            names[isin] = row["name"] or isin
        try:
            q = float(row["quantity"])
        except ValueError:
            q = 0.0
        by_isin[isin].append({"op": row["op"], "quantity": q, "total": row["total"]})

    positions: list[Position] = []
    for isin, trades in by_isin.items():
        qty = cost = pnl = 0.0
        source = "Manual"
        for t in trades:
            op = t["op"].lower()
            q, total = t["quantity"], t["total"]
            if "buy" in op or "sav" in op:
                qty += q
                cost += total
                if "sav" in op:
                    source = "Savings plan"
            elif "sell" in op:
                if qty > 1e-6:
                    # Average cost: the sold part leaves at the same unit price.
                    avg = cost / qty
                    sold_cost = avg * q
                    pnl += total - sold_cost
                    qty -= q
                    cost -= sold_cost
                else:
                    pnl += total
                    qty -= q
        # Rounding dust after selling everything: treat the position as closed.
        if abs(qty) < 1e-4:
            qty = cost = 0.0
        positions.append(Position(
            asset=names.get(isin, isin),
            isin=isin,
            quantity=Decimal(str(round(qty, 8))),
            invested=D(cost),
            withdrawn=Decimal("0.00"),
            realised_pnl=D(pnl),
            looks_sold=bool(qty == 0.0 and cost == 0.0),
            meta={"source": source},
        ))

    if not positions:
        warnings.append("No positions were detected in the PDF.")
    return ParseResult("traderepublic", positions, warnings)
