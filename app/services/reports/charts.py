"""Charts rendered as PNG (matplotlib) to embed in the PDF reports.

The 'Agg' backend needs no display. Every function returns PNG bytes, ready for
reportlab.Image.
"""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from app.format import pct  # noqa: E402

# Same palette as the web pages: green income, red expenses, blue net worth.
COL_INCOME = "#22c55e"
COL_EXPENSE = "#ef4444"
COL_NET_WORTH = "#3b82f6"
PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ec4899",
           "#14b8a6", "#ef4444", "#84cc16", "#06b6d4", "#f97316"]


def _save(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def bars_income_expenses(labels: list[str], income: list[float],
                         expenses: list[float], title: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    x = range(len(labels))
    w = 0.4
    ax.bar([i - w / 2 for i in x], income, width=w, label="Income", color=COL_INCOME)
    ax.bar([i + w / 2 for i in x], expenses, width=w, label="Expenses", color=COL_EXPENSE)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=8)
    ax.legend(fontsize=8, frameon=False)
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold")
    ax.grid(axis="y", alpha=0.2)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig)


def donut(labels: list[str], data: list[float], title: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(4.2, 3.2))
    # A pie chart cannot take negative or zero slices (matplotlib raises), so
    # they are dropped from the chart. They still show up in the tables.
    pairs = [(l, float(v)) for l, v in zip(labels, data) if v and float(v) > 0]
    if not pairs:
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        ax.axis("off")
        return _save(fig)
    labels = [l for l, _ in pairs]
    data = [v for _, v in pairs]
    wedges, _ = ax.pie(data, colors=PALETTE[:len(data)], startangle=90,
                       wedgeprops=dict(width=0.42))
    total = sum(data)
    legend = [f"{l}  {pct(v / total * 100)}" for l, v in zip(labels, data)]
    ax.legend(wedges, legend, loc="center left", bbox_to_anchor=(1, 0.5),
              fontsize=7, frameon=False)
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold")
    ax.axis("equal")
    return _save(fig)


def line(labels: list[str], data: list[float], title: str = "",
         color: str = COL_NET_WORTH) -> bytes:
    fig, ax = plt.subplots(figsize=(7.2, 2.8))
    if not data:
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        ax.axis("off")
        return _save(fig)
    ax.plot(labels, data, marker="o", color=color, linewidth=2)
    ax.fill_between(range(len(data)), data, alpha=0.12, color=color)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=7, rotation=45, ha="right")
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold")
    ax.grid(axis="y", alpha=0.2)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig)
