"""Charts as PNG (matplotlib, Agg backend) to embed in the PDFs.

Teal -> indigo palette, the same as the app icon. `group_items` decides the
order and colour of each category so the donut and the table match."""
from __future__ import annotations

import io
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from app.format import pct  # noqa: E402

TEAL = "#0d9488"
INDIGO = "#4338ca"
INK = "#0f172a"
MUTED = "#64748b"
COL_INCOME = "#10b981"
COL_EXPENSE = "#f43f5e"
COL_NET_WORTH = INDIGO
GREY_OTHER = "#cbd5e1"
PALETTE = ["#0d9488", "#4338ca", "#f59e0b", "#ec4899", "#0ea5e9",
           "#84cc16", "#a855f7", "#f97316", "#14b8a6", "#6366f1"]


def group_items(pairs, n: int = 7) -> list[tuple[str, float, str]]:
    """[(category, amount)] -> [(category, amount, colour)] positives only,
    largest first, with 'Other' grouping the rest."""
    pos = sorted(((str(c), float(v)) for c, v in pairs if v and float(v) > 0),
                 key=lambda p: p[1], reverse=True)
    top, resto = pos[:n], pos[n:]
    out = [(c, v, PALETTE[i % len(PALETTE)]) for i, (c, v) in enumerate(top)]
    if resto:
        out.append(("Other", sum(v for _, v in resto), GREY_OTHER))
    return out


def _save(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _empty(figsize=(7.2, 2.0), text="No data") -> bytes:
    fig, ax = plt.subplots(figsize=figsize)
    ax.text(0.5, 0.5, text, ha="center", va="center", color=MUTED, fontsize=10)
    ax.axis("off")
    return _save(fig)


def _style(ax):
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.tick_params(colors=MUTED, labelsize=7, length=0)
    ax.grid(axis="y", color="#e2e8f0", linewidth=0.6)
    ax.set_axisbelow(True)


def bars_income_expenses(labels, income, expenses, title: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(7.2, 2.8))
    x = list(range(len(labels)))
    w = 0.38
    ax.bar([i - w / 2 for i in x], income, width=w, label="Income", color=COL_INCOME)
    ax.bar([i + w / 2 for i in x], expenses, width=w, label="Expenses", color=COL_EXPENSE)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend(fontsize=7, frameon=False, loc="upper right", ncol=2)
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold", color=INK)
    _style(ax)
    return _save(fig)


def donut(labels, data, title: str = "") -> bytes:
    """Donut with a side legend (the bot also uses it in the month-end summary)."""
    items = group_items(zip(labels, data), n=9)
    if not items:
        return _empty((4.2, 3.2))
    fig, ax = plt.subplots(figsize=(4.2, 3.2))
    wedges, _ = ax.pie([v for _, v, _ in items], colors=[c for *_, c in items],
                       startangle=90, counterclock=False,
                       wedgeprops=dict(width=0.42, edgecolor="white", linewidth=1.5))
    total = sum(v for _, v, _ in items)
    ax.legend(wedges, [f"{c}  {pct(v / total * 100)}" for c, v, _ in items],
              loc="center left", bbox_to_anchor=(1, 0.5), fontsize=7, frameon=False)
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold")
    ax.axis("equal")
    return _save(fig)


def donut_labelled(items, centre: str = "", sub: str = "") -> bytes:
    """Donut with labels (name and %) outside. items = output of `group_items`."""
    if not items:
        return _empty((4.6, 3.2), "No expenses")
    fig, ax = plt.subplots(figsize=(4.6, 3.2))
    total = sum(v for _, v, _ in items)
    wedges, _ = ax.pie([v for _, v, _ in items], colors=[c for *_, c in items],
                       startangle=90, counterclock=False,
                       wedgeprops=dict(width=0.38, edgecolor="white", linewidth=2))
    pos = []
    for w, (c, v, _) in zip(wedges, items):
        ang = math.radians((w.theta2 + w.theta1) / 2)
        pos.append([math.cos(ang), math.sin(ang), c, v])
    # spread the labels on each side so they do not overlap
    for side in (1, -1):
        group = sorted((p for p in pos if (p[0] >= 0) == (side == 1)),
                       key=lambda p: p[1] * 1.28)
        y_prev = -9
        for p in group:
            y = max(p[1] * 1.28, y_prev + 0.2)
            y_prev = y
            p.append(y)
    for x, y0, c, v, y in pos:
        side = 1 if x >= 0 else -1
        ax.annotate(f"{c}\n{pct(v / total * 100)}", xy=(x * 0.81, y0 * 0.81),
                    xytext=(1.3 * side, y), ha="left" if side == 1 else "right",
                    va="center", fontsize=7, color=INK, linespacing=1.15,
                    arrowprops=dict(arrowstyle="-", color="#94a3b8", lw=0.6))
    if centre:
        ax.text(0, 0.07, centre, ha="center", va="center", fontsize=11,
                fontweight="bold", color=INK)
        ax.text(0, -0.15, sub, ha="center", va="center", fontsize=6.5, color=MUTED)
    ax.axis("equal")
    return _save(fig)


def cumulative(current, previous=None, lab_current: str = "This month",
               lab_previous: str = "Last month") -> bytes:
    """Cumulative spending per day: this month (teal) against the previous one (grey)."""
    if not current or not any(current):
        return _empty((7.2, 2.2))
    fig, ax = plt.subplots(figsize=(7.2, 2.5))
    if previous and any(previous):
        ax.plot(range(1, len(previous) + 1), previous, color="#94a3b8", linewidth=1.5,
                linestyle="--", label=lab_previous)
    d = range(1, len(current) + 1)
    ax.fill_between(d, current, alpha=0.12, color=TEAL)
    ax.plot(d, current, color=TEAL, linewidth=2.2, label=lab_current)
    ax.scatter([len(current)], [current[-1]], color=TEAL, s=18, zorder=3)
    ax.set_xlim(1, max(len(current), len(previous or [])))
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Day of the month", fontsize=7, color=MUTED)
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    _style(ax)
    return _save(fig)


def line(labels, data, title: str = "", color: str = COL_NET_WORTH) -> bytes:
    if not data:
        return _empty((7.2, 2.4))
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    x = list(range(len(data)))
    ax.plot(x, data, marker="o", markersize=3, color=color, linewidth=2)
    ax.fill_between(x, data, alpha=0.12, color=color)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold", color=INK)
    _style(ax)
    return _save(fig)
