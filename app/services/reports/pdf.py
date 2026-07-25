"""PDF reports (reportlab) with charts embedded as images (matplotlib).

Four reports:
- monthly_pdf: KPIs of the month + category doughnut + table.
- yearly_pdf: KPIs of the year + monthly bars + category doughnut.
- period_pdf: same as monthly, for any range of months.
- detailed_pdf: net worth broken down, categories and cashflow.
- net_worth_pdf: investments, split per broker, evolution.
"""
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.format import money, pct
from app.services.reports import charts

MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _img(png: bytes, width=16 * cm) -> Image:
    bio = io.BytesIO(png)
    img = Image(bio)
    ratio = img.imageHeight / img.imageWidth
    img.drawWidth = width
    img.drawHeight = width * ratio
    return img


def _table(data, col_widths=None) -> Table:
    t = Table(data, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _doc(buf):
    return SimpleDocTemplate(buf, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm,
                             leftMargin=2 * cm, rightMargin=2 * cm)


def _cover(elems, styles, title, subtitle):
    elems.append(Paragraph(title, styles["Title"]))
    elems.append(Paragraph(subtitle, styles["Normal"]))
    elems.append(Spacer(1, 0.5 * cm))


def _investment_note(elems, styles, invested) -> None:
    """Note under the savings figure: the part of the spending that was really
    investing, and therefore already counted in net worth. Only when non zero."""
    if invested and float(invested) > 0:
        elems.append(Paragraph(
            f"ℹ Of the expenses, {money(invested)} was invested "
            "(already part of your net worth, not consumption).", styles["Italic"]))
        elems.append(Spacer(1, 0.3 * cm))


def monthly_pdf(owner: str, year: int, month: int, expense: float,
                income: float, expenses_cat: list[tuple],
                invested: float = 0) -> bytes:
    """expenses_cat = [(category, total), ...]."""
    buf = io.BytesIO()
    doc = _doc(buf)
    styles = getSampleStyleSheet()
    elems = []
    _cover(elems, styles, f"Report {MONTHS[month]} {year}",
           f"{owner} · generated {date.today().strftime('%d/%m/%Y')}")

    saved = income - expense
    elems.append(_table([
        ["Item", "Amount"],
        ["Income", money(income)],
        ["Expenses", money(expense)],
        ["Saved", money(saved)],
    ], col_widths=[9 * cm, 6 * cm]))
    elems.append(Spacer(1, 0.3 * cm))
    _investment_note(elems, styles, invested)
    elems.append(Spacer(1, 0.3 * cm))

    if expenses_cat:
        labels = [c for c, _ in expenses_cat]
        data = [float(v or 0) for _, v in expenses_cat]
        elems.append(Paragraph("Expenses by category", styles["Heading2"]))
        elems.append(_img(charts.donut(labels, data), width=12 * cm))
        elems.append(Spacer(1, 0.4 * cm))
        elems.append(_table([["Category", "Amount"]] +
                            [[c, money(v)] for c, v in expenses_cat],
                            col_widths=[9 * cm, 6 * cm]))

    doc.build(elems)
    return buf.getvalue()


def yearly_pdf(owner: str, year: int, months_income: list[float],
               months_expenses: list[float], expenses_cat: list[tuple],
               invested: float = 0) -> bytes:
    buf = io.BytesIO()
    doc = _doc(buf)
    styles = getSampleStyleSheet()
    elems = []
    _cover(elems, styles, f"Yearly summary {year}",
           f"{owner} · generated {date.today().strftime('%d/%m/%Y')}")

    tot_i = sum(months_income)
    tot_e = sum(months_expenses)
    elems.append(_table([
        ["Item", "Amount"],
        ["Total income", money(tot_i)],
        ["Total expenses", money(tot_e)],
        ["Saved this year", money(tot_i - tot_e)],
    ], col_widths=[9 * cm, 6 * cm]))
    elems.append(Spacer(1, 0.3 * cm))
    _investment_note(elems, styles, invested)
    elems.append(Spacer(1, 0.3 * cm))

    elems.append(Paragraph("Income vs expenses by month", styles["Heading2"]))
    elems.append(_img(charts.bars_income_expenses(
        [MONTHS[m][:3] for m in range(1, 13)], months_income, months_expenses)))
    elems.append(Spacer(1, 0.5 * cm))

    if expenses_cat:
        labels = [c for c, _ in expenses_cat]
        data = [float(v or 0) for _, v in expenses_cat]
        elems.append(Paragraph("Expenses by category (year)", styles["Heading2"]))
        elems.append(_img(charts.donut(labels, data), width=12 * cm))

    doc.build(elems)
    return buf.getvalue()


def period_pdf(owner: str, label: str, expense: float, income: float,
               expenses_cat: list[tuple], invested: float = 0) -> bytes:
    """Report for any range of months, with `label` as the free-form title
    (for example 'Jan 2026 - Apr 2026')."""
    buf = io.BytesIO()
    doc = _doc(buf)
    styles = getSampleStyleSheet()
    elems = []
    _cover(elems, styles, f"Report {label}",
           f"{owner} · generated {date.today().strftime('%d/%m/%Y')}")

    saved = income - expense
    elems.append(_table([
        ["Item", "Amount"],
        ["Income", money(income)],
        ["Expenses", money(expense)],
        ["Saved", money(saved)],
    ], col_widths=[9 * cm, 6 * cm]))
    elems.append(Spacer(1, 0.3 * cm))
    _investment_note(elems, styles, invested)
    elems.append(Spacer(1, 0.3 * cm))

    if expenses_cat:
        labels = [c for c, _ in expenses_cat]
        data = [float(v or 0) for _, v in expenses_cat]
        elems.append(Paragraph("Expenses by category", styles["Heading2"]))
        elems.append(_img(charts.donut(labels, data), width=12 * cm))
        elems.append(Spacer(1, 0.4 * cm))
        elems.append(_table([["Category", "Amount"]] +
                            [[c, money(v)] for c, v in expenses_cat],
                            col_widths=[9 * cm, 6 * cm]))

    doc.build(elems)
    return buf.getvalue()


def detailed_pdf(owner: str, label: str, kpis: dict,
                 investments: list[tuple], banks: list[tuple],
                 expenses_cat: list[tuple], income_cat: list[tuple],
                 cashflow: tuple, invested: float = 0) -> bytes:
    """Detailed report: net worth broken down, categories and monthly cashflow.

    investments = [(asset, broker, invested, value, profit, return_pct), ...]
    banks = [(name, balance), ...]; cashflow = (labels, income, expenses).
    """
    buf = io.BytesIO()
    doc = _doc(buf)
    styles = getSampleStyleSheet()
    elems = []
    _cover(elems, styles, "Detailed report",
           f"{owner} · {label} · generated {date.today().strftime('%d/%m/%Y')}")

    # --- Net worth summary (current KPIs) ---
    elems.append(Paragraph("Net worth summary", styles["Heading2"]))
    elems.append(_table([
        ["Item", "Value"],
        ["Net worth", money(kpis.get("net_worth", 0))],
        ["Investments (value)", money(kpis.get("inv_value", 0))],
        ["Invested", money(kpis.get("inv_invested", 0))],
        ["Profit", money(kpis.get("inv_profit", 0))],
        ["Return", pct(kpis.get("return_pct", 0), decimals=2)],
        ["Banks / cash", money(kpis.get("bank_balance", 0))],
    ], col_widths=[9 * cm, 6 * cm]))
    elems.append(Spacer(1, 0.5 * cm))

    # --- Investments in detail ---
    if investments:
        elems.append(Paragraph("Investments (detail)", styles["Heading2"]))
        rows = [["Asset", "Broker", "Invested", "Value", "Profit", "Return"]]
        for asset, broker, inv, val, profit, ret in investments:
            rows.append([str(asset)[:28], broker, money(inv), money(val), money(profit),
                         pct(ret, decimals=2)])
        elems.append(_table(rows, col_widths=[5 * cm, 2.6 * cm, 2.4 * cm, 2.4 * cm,
                                              2.4 * cm, 2.2 * cm]))
        elems.append(Spacer(1, 0.5 * cm))

    # --- Accounts in detail ---
    if banks:
        elems.append(Paragraph("Banks / cash (detail)", styles["Heading2"]))
        elems.append(_table([["Account", "Balance"]] +
                            [[str(n)[:40], money(s)] for n, s in banks],
                            col_widths=[9 * cm, 6 * cm]))
        elems.append(Spacer(1, 0.5 * cm))

    # --- Monthly cashflow of the period ---
    labels, cf_inc, cf_exp = cashflow if cashflow else ([], [], [])
    if labels and (any(cf_inc) or any(cf_exp)):
        elems.append(Paragraph("Monthly cashflow (income vs expenses)", styles["Heading2"]))
        elems.append(_img(charts.bars_income_expenses(labels, cf_inc, cf_exp)))
        elems.append(Spacer(1, 0.5 * cm))

    # --- Expenses by category ---
    if expenses_cat:
        labels_e = [c for c, _ in expenses_cat]
        data_e = [float(v or 0) for _, v in expenses_cat]
        elems.append(Paragraph("Expenses by category", styles["Heading2"]))
        elems.append(_img(charts.donut(labels_e, data_e), width=12 * cm))
        elems.append(Spacer(1, 0.3 * cm))
        elems.append(_table([["Category", "Amount"]] +
                            [[c, money(v)] for c, v in expenses_cat],
                            col_widths=[9 * cm, 6 * cm]))
        elems.append(Spacer(1, 0.3 * cm))
        _investment_note(elems, styles, invested)
        elems.append(Spacer(1, 0.2 * cm))

    # --- Income by category ---
    if income_cat:
        elems.append(Paragraph("Income by category", styles["Heading2"]))
        elems.append(_table([["Category", "Amount"]] +
                            [[c, money(v)] for c, v in income_cat],
                            col_widths=[9 * cm, 6 * cm]))

    doc.build(elems)
    return buf.getvalue()


def net_worth_pdf(owner: str, kpis: dict, split: list[tuple],
                  investments: list[tuple], evolution: tuple | None = None) -> bytes:
    """investments = (asset, broker, invested, value, profit).
    evolution = (labels, data), optional."""
    buf = io.BytesIO()
    doc = _doc(buf)
    styles = getSampleStyleSheet()
    elems = []
    _cover(elems, styles, "Net worth report",
           f"{owner} · {date.today().strftime('%d/%m/%Y')}")

    elems.append(_table([
        ["Item", "Value"],
        ["Net worth", money(kpis.get("net_worth", 0))],
        ["Invested", money(kpis.get("inv_invested", 0))],
        ["Profit", money(kpis.get("inv_profit", 0))],
        ["Return", pct(kpis.get("return_pct", 0), decimals=2)],
    ], col_widths=[9 * cm, 6 * cm]))
    elems.append(Spacer(1, 0.6 * cm))

    if split:
        elems.append(Paragraph("Split by broker and account", styles["Heading2"]))
        elems.append(_img(charts.donut([c for c, _ in split],
                                       [float(v or 0) for _, v in split]), width=12 * cm))
        elems.append(Spacer(1, 0.4 * cm))

    if evolution and evolution[1]:
        elems.append(Paragraph("Net worth over time", styles["Heading2"]))
        elems.append(_img(charts.line(evolution[0], evolution[1])))
        elems.append(Spacer(1, 0.4 * cm))

    if investments:
        elems.append(Paragraph("Investment detail", styles["Heading2"]))
        rows = [["Asset", "Broker", "Invested", "Value", "Profit"]]
        for asset, broker, inv, val, profit in investments:
            rows.append([str(asset)[:32], broker, money(inv), money(val), money(profit)])
        elems.append(_table(rows, col_widths=[6 * cm, 3 * cm, 2.5 * cm, 2.5 * cm, 2.5 * cm]))

    doc.build(elems)
    return buf.getvalue()
