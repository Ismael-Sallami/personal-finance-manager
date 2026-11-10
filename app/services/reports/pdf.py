"""PDF reports (reportlab) in a fintech-statement style.

Reusable blocks: header band with a teal -> indigo gradient, KPI cards,
sections, category table with bars, stacked net worth and a footer with the
page number. Every report shares them."""
import io
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    Flowable,
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

TEAL, INDIGO = HexColor("#0d9488"), HexColor("#4338ca")
INK, MUTED, LINE = HexColor("#0f172a"), HexColor("#64748b"), HexColor("#e2e8f0")
SOFT = HexColor("#f8fafc")
GREEN, RED = HexColor("#059669"), HexColor("#e11d48")
ICON = Path(__file__).resolve().parents[2] / "static" / "img" / "icon-512.png"

MARGIN = 1.8 * cm
TOP = 1.5 * cm
W = A4[0] - 2 * MARGIN

_S = {
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, textColor=INK,
                         spaceBefore=16, spaceAfter=7, leading=15, keepWithNext=1),
    "p": ParagraphStyle("p", fontName="Helvetica", fontSize=9.5, textColor=INK, leading=14),
    "muted": ParagraphStyle("muted", fontName="Helvetica", fontSize=8.5, textColor=MUTED,
                            leading=12),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.5, textColor=INK,
                             leading=14, leftIndent=14, bulletIndent=2, spaceAfter=4),
}


def _eur(v) -> str:
    return money(float(v or 0))


def _pc(v, d: int = 1) -> str:
    return f"{float(v):.{d}f} %"


def _img(png: bytes, width=W) -> Image:
    img = Image(io.BytesIO(png))
    ratio = img.imageHeight / img.imageWidth
    img.drawWidth, img.drawHeight = width, width * ratio
    return img


def _no_data(text="No data in this period") -> Paragraph:
    return Paragraph(text, _S["muted"])


# ---------------------------------------------------------------- blocks
class Header(Flowable):
    """Full-bleed band (to the top and side edges) with icon, title, owner and
    generation date."""
    H = 2.3 * cm

    def __init__(self, title: str, subtitle: str):
        super().__init__()
        self.title, self.subtitle = title, subtitle
        self.width, self.height = W, self.H

    def wrap(self, aw, ah):
        return W, self.H

    def draw(self):
        c = self.canv
        c.saveState()
        x0, x1 = -MARGIN, A4[0] - MARGIN
        y1 = self.H + TOP
        n = 80
        for i in range(n):
            t = i / (n - 1)
            c.setFillColorRGB(TEAL.red + (INDIGO.red - TEAL.red) * t,
                              TEAL.green + (INDIGO.green - TEAL.green) * t,
                              TEAL.blue + (INDIGO.blue - TEAL.blue) * t)
            c.rect(x0 + (x1 - x0) * i / n, 0, (x1 - x0) / n + 0.6, y1, stroke=0, fill=1)
        try:
            c.drawImage(str(ICON), 0, 0.35 * cm, 1.7 * cm, 1.7 * cm, mask="auto")
        except Exception:  # the icon is decorative
            pass
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 21)
        c.drawString(2.2 * cm, 1.2 * cm, self.title)
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(1, 1, 1, 0.85)
        c.drawString(2.2 * cm, 0.6 * cm, self.subtitle)
        c.restoreState()


def _header(title: str, owner: str, extra: str = ""):
    sub = " · ".join(p for p in (owner, extra,
                                 f"Generated on {date.today().strftime('%d/%m/%Y')}") if p)
    return [Header(title, sub), Spacer(1, 0.5 * cm)]


def _h2(text: str) -> Paragraph:
    return Paragraph(text, _S["h2"])


class Kpis(Flowable):
    """Row of cards. items = (label, value, colour, delta) with
    delta = (text, sign, good_if_up) or None."""
    H = 2.3 * cm

    def __init__(self, items, gap=0.3 * cm):
        super().__init__()
        self.items, self.gap = items, gap

    def wrap(self, aw, ah):
        return W, self.H

    def draw(self):
        c = self.canv
        n = len(self.items)
        cw = (W - self.gap * (n - 1)) / n
        for i, (lab, val, col, delta) in enumerate(self.items):
            x = i * (cw + self.gap)
            c.setFillColor(SOFT)
            c.setStrokeColor(LINE)
            c.roundRect(x, 0, cw, self.H, 7, stroke=1, fill=1)
            c.setFillColor(col)
            c.roundRect(x, self.H - 3, cw, 3, 1.5, stroke=0, fill=1)
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 7.5)
            c.drawString(x + 10, self.H - 18, lab.upper())
            c.setFillColor(col)
            size = 15 if len(val) < 12 else 12.5
            c.setFont("Helvetica-Bold", size)
            c.drawString(x + 10, self.H - 40, val)
            if delta is None:
                continue
            txt, sign, good_up = delta
            if sign == 0:
                c.setFillColor(MUTED)
                c.setFont("Helvetica", 7.5)
                c.drawString(x + 10, 9, txt)
                continue
            ok = (sign > 0) == good_up
            c.setFillColor(GREEN if ok else RED)
            p = c.beginPath()
            if sign > 0:
                p.moveTo(x + 10, 9); p.lineTo(x + 18, 9); p.lineTo(x + 14, 16)
            else:
                p.moveTo(x + 10, 16); p.lineTo(x + 18, 16); p.lineTo(x + 14, 9)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
            c.setFont("Helvetica-Bold", 7.5)
            c.drawString(x + 23, 10, txt)


def _delta(current, previous, good_up, suffix="vs last month", pp=False):
    """(text, sign, good_if_up) or None when there is no previous data."""
    if previous is None:
        return None
    if pp:
        d = current - previous
        if abs(d) < 0.05:
            return (f"= {suffix}", 0, good_up)
        return (f"{d:+.1f} pp {suffix}", 1 if d > 0 else -1, good_up)
    if not previous:
        return ("no previous month", 0, good_up)
    d = (current - previous) / abs(previous) * 100
    if abs(d) < 0.05:
        return (f"= {suffix}", 0, good_up)
    return (f"{d:+.1f} % {suffix}", 1 if d > 0 else -1, good_up)


def _rate(income, expense) -> float | None:
    return (income - expense) / income * 100 if income > 0 else None


def _flow_kpis(income, expense, prev=None, suffix="vs last month"):
    """Income, Expenses, Savings and Savings rate; prev = (expense, income) or None."""
    saved = income - expense
    rate = _rate(income, expense)
    pe, pi = prev if prev else (None, None)
    p_saved = (pi - pe) if prev else None
    p_rate = _rate(pi, pe) if prev else None
    col = GREEN if saved >= 0 else RED
    return Kpis([
        ("Income", _eur(income), TEAL, _delta(income, pi, True, suffix)),
        ("Expenses", _eur(expense), INDIGO, _delta(expense, pe, False, suffix)),
        ("Savings", _eur(saved), col,
         _delta(saved, p_saved, True, suffix) if prev else None),
        ("Savings rate", _pc(rate) if rate is not None else "n/a", col,
         _delta(rate, p_rate, True, suffix, pp=True)
         if prev and rate is not None and p_rate is not None else None),
    ])


class CatTable(Flowable):
    """Rows: colour dot, category, amount, % and a thin bar."""
    FILA = 0.62 * cm

    def __init__(self, items):
        super().__init__()
        self.items = items
        self.total = sum(v for _, v, _ in items) or 1

    def wrap(self, aw, ah):
        return W, self.FILA * (len(self.items) + 1)

    def draw(self):
        c = self.canv
        h = self.FILA * (len(self.items) + 1)
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(MUTED)
        y = h - self.FILA + 6
        c.drawString(0, y, "CATEGORY")
        c.drawRightString(W * 0.56, y, "AMOUNT")
        c.drawRightString(W * 0.66, y, "%")
        mx = max(v for _, v, _ in self.items)
        for i, (cat, v, col) in enumerate(self.items):
            y = h - self.FILA * (i + 2)
            c.setStrokeColor(LINE)
            c.setLineWidth(0.4)
            c.line(0, y, W, y)
            cy = y + self.FILA / 2
            c.setFillColor(col)
            c.circle(4, cy, 3, stroke=0, fill=1)
            c.setFillColor(INK)
            c.setFont("Helvetica", 9)
            c.drawString(14, cy - 3, cat[:30])
            c.drawRightString(W * 0.56, cy - 3, _eur(v))
            c.setFillColor(MUTED)
            c.drawRightString(W * 0.66, cy - 3, _pc(v / self.total * 100))
            bx, bw = W * 0.71, W * 0.29
            c.setFillColor(LINE)
            c.roundRect(bx, cy - 1.8, bw, 3.6, 1.8, stroke=0, fill=1)
            c.setFillColor(col)
            c.roundRect(bx, cy - 1.8, max(bw * v / mx, 3.6), 3.6, 1.8, stroke=0, fill=1)


def _expense_categories(expenses_cat, title="Expenses by category", total=None):
    items = charts.group_items(expenses_cat)
    out = [_h2(title)]
    if not items:
        return out + [_no_data("No expenses recorded")]
    tot = sum(v for _, v, _ in items)
    donut = _img(charts.donut_labelled(items, _eur(total if total else tot),
                                       "total expenses"), width=10 * cm)
    donut.hAlign = "CENTER"
    return out + [donut, Spacer(1, 0.3 * cm), CatTable(items)]


def _table(data, col_widths=None, right_from=1) -> Table:
    """Clean table: small grey header, only thin horizontal lines."""
    t = Table(data, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("ALIGN", (right_from, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
    ]))
    return t


class Stacked(Flowable):
    """Horizontal stacked bar: segments = [(name, value, colour)]."""
    def __init__(self, segs):
        super().__init__()
        self.segs = [s for s in segs if s[1] > 0]

    def wrap(self, aw, ah):
        return W, 1.0 * cm

    def draw(self):
        c = self.canv
        tot = sum(v for _, v, _ in self.segs)
        if not tot:
            return
        x = 0
        c.saveState()
        p = c.beginPath()
        p.roundRect(0, 0.45 * cm, W, 0.38 * cm, 4)
        c.clipPath(p, stroke=0, fill=0)
        for _, v, col in self.segs:
            w = W * v / tot
            c.setFillColor(col)
            c.rect(x, 0.45 * cm, w + 0.5, 0.38 * cm, stroke=0, fill=1)
            x += w
        c.restoreState()


NW_COL = {"Accounts": TEAL, "Savings": HexColor("#0ea5e9"),
          "Cash": HexColor("#f59e0b"), "Invested": INDIGO}


def _net_worth_block(nw: dict | None):
    out = [_h2("Net worth")]
    if not nw:
        return out + [_no_data("No accounts recorded")]
    parts = [("Accounts", nw.get("account", 0)), ("Savings", nw.get("savings", 0)),
             ("Cash", nw.get("cash", 0)), ("Invested", nw.get("investment", 0))]
    total = sum(v for _, v in parts)
    if total <= 0 and not any(v for _, v in parts):
        return out + [_no_data("No accounts recorded")]
    rows = [["", "AMOUNT", "% OF TOTAL"]]
    for n, v in parts:
        rows.append([n, _eur(v), _pc(v / total * 100) if total > 0 and v > 0 else "-"])
    rows.append(["Total", _eur(total), ""])
    t = _table(rows, col_widths=[W * 0.5, W * 0.28, W * 0.22])
    t.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]
                          + [("TEXTCOLOR", (0, i + 1), (0, i + 1), NW_COL[n])
                             for i, (n, _) in enumerate(parts)]))
    out += [Stacked([(n, v, NW_COL[n]) for n, v in parts]), Spacer(1, 0.1 * cm), t]
    platforms = nw.get("platforms") or []
    if platforms:
        out += [Spacer(1, 0.2 * cm), Paragraph(
            "Invested capital per platform: " + " · ".join(
                f"{n} {_eur(v)}" for n, v in platforms), _S["muted"])]
    return out


def _highlights(lines: list[str]):
    out = [_h2("Highlights")]
    out += [Paragraph(l, _S["bullet"], bulletText="•") for l in lines]
    return out


def highlight_lines(expense, income, expenses_cat, prev_cats, consumption, avg3) -> list[str]:
    """Three plain sentences: the category that changed most, pace and savings."""
    inv = {"investment", "investments", "investing"}
    cur = {c: float(v) for c, v in expenses_cat if c.lower() not in inv}
    prev = {c: float(v) for c, v in (prev_cats or []) if c.lower() not in inv}
    if prev and cur:
        cat = max(set(cur) | set(prev), key=lambda k: abs(cur.get(k, 0) - prev.get(k, 0)))
        a, b = cur.get(cat, 0), prev.get(cat, 0)
        d = a - b
        if abs(d) < 1:
            l1 = "Your spending by category is almost the same as last month."
        elif b > 0:
            l1 = (f"On <b>{cat}</b> you spent {_eur(abs(d))} "
                  f"{'more' if d > 0 else 'less'} than last month "
                  f"({d / b * 100:+.0f} %).")
        else:
            l1 = f"<b>{cat}</b> is new this month: {_eur(a)} with no previous spending."
    else:
        l1 = "There is no previous month yet to compare your categories with."
    if avg3 and consumption is not None:
        d = (consumption - avg3) / avg3 * 100
        if abs(d) < 3:
            l2 = (f"Your consumption this month ({_eur(consumption)}) is in line with your "
                  f"3-month average ({_eur(avg3)}).")
        else:
            l2 = (f"Your consumption this month ({_eur(consumption)}) is {abs(d):.0f} % "
                  f"{'above' if d > 0 else 'below'} your 3-month average ({_eur(avg3)}).")
    else:
        l2 = "There is no earlier history yet to measure your pace."
    rate, saved = _rate(income, expense), income - expense
    if rate is None:
        l3 = "There is no income this month, so the savings rate cannot be computed."
    elif saved >= 0:
        l3 = f"You saved <b>{_pc(rate, 0)}</b> of your income ({_eur(saved)})."
    else:
        l3 = f"You spent {_eur(-saved)} more than you earned this month."
    return [l.replace("€ ", "€&nbsp; ") for l in (l1, l2, l3)]


# ---------------------------------------------------------------- document
class _Canvas(rl_canvas.Canvas):
    """Canvas with a 'Page n of N' footer (two passes to know N)."""
    label = ""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        n = len(self._pages)
        for st in self._pages:
            self.__dict__.update(st)
            self.setStrokeColor(LINE)
            self.setLineWidth(0.5)
            self.line(MARGIN, 1.4 * cm, A4[0] - MARGIN, 1.4 * cm)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            self.drawString(MARGIN, 0.9 * cm, f"Personal finance manager · {self.label}")
            self.drawRightString(A4[0] - MARGIN, 0.9 * cm,
                                 f"Page {self._pageNumber} of {n}")
            super().showPage()
        super().save()


def _build(elems, label: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=TOP, bottomMargin=2 * cm,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            title=label, author="Personal finance manager")
    canv = type("C", (_Canvas,), {"label": label})
    doc.build(elems, canvasmaker=canv)
    return buf.getvalue()


def _investment_note(invested):
    if invested and float(invested) > 0:
        return [Spacer(1, 0.25 * cm), Paragraph(
            f"Of the expenses, {_eur(invested)} was investing "
            "(it already counts in your net worth, it is not consumption).", _S["muted"])]
    return []


# ---------------------------------------------------------------- reports
def monthly_pdf(owner: str, year: int, month: int, expense: float,
                income: float, expenses_cat: list[tuple],
                invested: float = 0, extra: dict | None = None) -> bytes:
    """expenses_cat = [(category, total), ...]. `extra` (optional): prev=(expense,
    income), prev_cats, consumption, avg3, daily=(current, previous, label, label),
    top=[(merchant, amount, n)], subs=[(merchant, monthly_amount)],
    net_worth={account, savings, cash, investment, platforms}."""
    x = extra or {}
    title = f"{MONTHS[month]} {year} report"
    e = _header(title, owner)
    e.append(_flow_kpis(income, expense, x.get("prev")))
    e += _investment_note(invested)
    e += _highlights(highlight_lines(expense, income, expenses_cat, x.get("prev_cats"),
                                     x.get("consumption"), x.get("avg3")))
    e += _expense_categories(expenses_cat)

    if x.get("daily"):
        cur, prev, la, lp = x["daily"]
        e += [_h2("Cumulative spending of the month"),
              _img(charts.cumulative(cur, prev, la, lp))]

    top = x.get("top") or []
    e.append(_h2("Top 5 merchants"))
    if top:
        e.append(_table([["MERCHANT", "ENTRIES", "AMOUNT"]] +
                        [[str(n)[:38], str(k), _eur(v)] for n, v, k in top[:5]],
                        col_widths=[W * 0.58, W * 0.2, W * 0.22]))
    else:
        e.append(_no_data("No entries with a merchant (only with bank data)"))

    subs = x.get("subs") or []
    if subs:
        e.append(_h2("Detected subscriptions"))
        e.append(_table([["SUBSCRIPTION", "PER MONTH", "PER YEAR"]] +
                        [[str(n)[:38], _eur(v), _eur(float(v) * 12)] for n, v in subs],
                        col_widths=[W * 0.5, W * 0.25, W * 0.25]))
        tm = sum(float(v) for _, v in subs)
        e.append(Spacer(1, 0.15 * cm))
        e.append(Paragraph(f"Total: <b>{_eur(tm)}</b>&nbsp; a month, "
                           f"<b>{_eur(tm * 12)}</b>&nbsp; a year.", _S["muted"]))

    e += _net_worth_block(x.get("net_worth"))
    return _build(e, title)


def yearly_pdf(owner: str, year: int, months_income: list[float],
               months_expenses: list[float], expenses_cat: list[tuple],
               invested: float = 0) -> bytes:
    title = f"Yearly summary {year}"
    tot_i, tot_e = sum(months_income), sum(months_expenses)
    e = _header(title, owner)
    e.append(_flow_kpis(tot_i, tot_e))
    e += _investment_note(invested)
    e.append(_h2("Income vs expenses per month"))
    if tot_i or tot_e:
        e.append(_img(charts.bars_income_expenses(
            [MONTHS[m][:3] for m in range(1, 13)], months_income, months_expenses)))
    else:
        e.append(_no_data())
    e += _expense_categories(expenses_cat, "Expenses by category (year)")
    return _build(e, title)


def period_pdf(owner: str, label: str, expense: float, income: float,
               expenses_cat: list[tuple], invested: float = 0) -> bytes:
    title = f"{label} report"
    e = _header(title, owner)
    e.append(_flow_kpis(income, expense))
    e += _investment_note(invested)
    e += _expense_categories(expenses_cat)
    return _build(e, title)


def _simple_category_table(title, rows):
    return [_h2(title), _table([["CATEGORY", "AMOUNT"]] +
                               [[str(c)[:40], _eur(v)] for c, v in rows],
                               col_widths=[W * 0.7, W * 0.3])]


def _net_worth_kpis(kpis: dict) -> Kpis:
    profit = float(kpis.get("inv_profit", 0) or 0)
    col = GREEN if profit >= 0 else RED
    return Kpis([
        ("Net worth", _eur(kpis.get("net_worth", 0)), TEAL, None),
        ("Invested", _eur(kpis.get("inv_invested", 0)), INDIGO, None),
        ("Profit", _eur(profit), col, None),
        ("Return", pct(kpis.get("return_pct", 0), decimals=2), col, None),
    ])


def detailed_pdf(owner: str, label: str, kpis: dict,
                 investments: list[tuple], banks: list[tuple],
                 expenses_cat: list[tuple], income_cat: list[tuple],
                 cashflow: tuple, invested: float = 0) -> bytes:
    """investments = [(asset, broker, invested, value, profit, return_pct), ...]
    banks = [(name, balance), ...]; cashflow = (labels, income, expenses)."""
    title = "Detailed report"
    e = _header(title, owner, label)
    e.append(_net_worth_kpis(kpis))
    e += [Spacer(1, 0.3 * cm), Paragraph(
        f"Banks / cash: <b>{_eur(kpis.get('bank_balance', 0))}</b> · "
        f"Investments (value): <b>{_eur(kpis.get('inv_value', 0))}</b>", _S["muted"])]

    if investments:
        rows = [["ASSET", "BROKER", "INVESTED", "VALUE", "PROFIT", "RETURN"]]
        for asset, broker, inv, val, p, ret in investments:
            rows.append([str(asset)[:26], broker, _eur(inv), _eur(val), _eur(p),
                         pct(ret, decimals=2)])
        e += [_h2("Investments (detail)"), _table(
            rows, col_widths=[W * .28, W * .14, W * .16, W * .16, W * .16, W * .10],
            right_from=2)]
    if banks:
        e += [_h2("Banks / cash (detail)"), _table(
            [["ACCOUNT", "BALANCE"]] + [[str(n)[:40], _eur(s)] for n, s in banks],
            col_widths=[W * .7, W * .3])]

    labels, cf_inc, cf_exp = cashflow if cashflow else ([], [], [])
    if labels and (any(cf_inc) or any(cf_exp)):
        e += [_h2("Monthly cashflow"), _img(charts.bars_income_expenses(
            labels, cf_inc, cf_exp))]
    e += _expense_categories(expenses_cat)
    e += _investment_note(invested)
    if income_cat:
        e += _simple_category_table("Income by category", income_cat)
    return _build(e, title)


def net_worth_pdf(owner: str, kpis: dict, split: list[tuple],
                  investments: list[tuple], evolution: tuple | None = None) -> bytes:
    """investments = (asset, broker, invested, value, profit).
    evolution = (labels, data), optional."""
    title = "Net worth report"
    e = _header(title, owner)
    e.append(_net_worth_kpis(kpis))
    items = charts.group_items(split)
    if items:
        donut = _img(charts.donut_labelled(items, _eur(sum(v for _, v, _ in items)),
                                           "total"), width=10 * cm)
        donut.hAlign = "CENTER"
        e += [_h2("Split by broker and account"), donut]
    if evolution and evolution[1]:
        e += [_h2("Net worth over time"), _img(charts.line(*evolution))]
    if investments:
        rows = [["ASSET", "BROKER", "INVESTED", "VALUE", "PROFIT"]]
        for asset, broker, inv, val, p in investments:
            rows.append([str(asset)[:30], broker, _eur(inv), _eur(val), _eur(p)])
        e += [_h2("Investment detail"), _table(
            rows, col_widths=[W * .32, W * .16, W * .18, W * .17, W * .17],
            right_from=2)]
    return _build(e, title)
