"""Animated Telegram-style demo GIFs for the README (dark theme, no emoji fonts).

Usage: python scripts/make_demo_gifs.py docs/media   (needs Pillow and the Inter font)
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
W, H, S = 440, 600, 2  # draw at 2x, downscale for crisp text
F = "/usr/share/fonts/opentype/inter/Inter-{}.otf"
font = lambda w, sz: ImageFont.truetype(F.format(w), sz * S)
REG, MED, BOLD, SMALL = font("Regular", 14), font("Medium", 14), font("SemiBold", 15), font("Regular", 11)
BG, HEAD, BOT, ME, BTN, BTN_HI = "#0e1621", "#17212b", "#182533", "#2b5278", "#22303f", "#3d6a99"
TXT, DIM, ACC, OK, BAD = "#f5f5f5", "#7f91a4", "#6ab3f3", "#5fd38d", "#ef6f6f"


class Chat:
    def __init__(self):
        self.items, self.frames, self.durs = [], [], []

    def frame(self, ms, extra=None):
        im = Image.new("RGB", (W * S, H * S), BG)
        d = ImageDraw.Draw(im)
        # header
        d.rectangle([0, 0, W * S, 56 * S], fill=HEAD)
        d.ellipse([14 * S, 10 * S, 50 * S, 46 * S], fill="#3a7bd5")
        d.text((32 * S, 28 * S), "₣", font=BOLD, fill="white", anchor="mm")
        d.text((60 * S, 14 * S), "Finance Bot", font=BOLD, fill=TXT)
        d.text((60 * S, 34 * S), "bot", font=SMALL, fill=DIM)
        # messages, bottom-anchored
        hs = [it.height() for it in self.items]
        y = H * S - 16 * S - sum(hs) - 10 * S * len(hs)
        for it, h in zip(self.items, hs):
            if y + h > 60 * S:
                it.draw(d, y)
            y += h + 10 * S
        if extra:
            extra(d)
        self.frames.append(im.resize((W, H), Image.LANCZOS))
        self.durs.append(ms)

    def add(self, item, typing=True):
        if typing and isinstance(item, Bubble) and not item.me:
            t = Typing()
            self.items.append(t)
            for i in range(6):
                t.phase = i
                self.frame(140)
            self.items.remove(t)
        self.items.append(item)
        self.frame(700)
        return item

    def save(self, name):
        self.frame(2600)
        self.frames[0].save(OUT / name, save_all=True, append_images=self.frames[1:],
                            duration=self.durs, loop=0, optimize=True, disposal=2)
        print(name, len(self.frames), "frames")


class Bubble:
    def __init__(self, lines, me=False, buttons=None):
        self.lines, self.me, self.buttons, self.hi = lines, me, buttons or [], None

    def _w(self):
        return 300 * S if not self.me else max(MED.getlength(t) for t, *_ in self.lines) + 28 * S

    def height(self):
        return (len(self.lines) * 21 + 18) * S + len(self.buttons) * 40 * S

    def draw(self, d, y):
        w = self._w()
        x = (W * S - 14 * S - w) if self.me else 14 * S
        bh = (len(self.lines) * 21 + 18) * S
        d.rounded_rectangle([x, y, x + w, y + bh], 16 * S, fill=ME if self.me else BOT)
        ty = y + 10 * S
        for line in self.lines:
            text, f, col = (line + (MED, TXT))[:3] if len(line) < 3 else line
            d.text((x + 14 * S, ty), text, font=f, fill=col)
            ty += 21 * S
        by = y + bh + 6 * S
        for r, row in enumerate(self.buttons):
            bw = (w - 6 * S * (len(row) - 1)) / len(row)
            for c, label in enumerate(row):
                bx = x + c * (bw + 6 * S)
                d.rounded_rectangle([bx, by, bx + bw, by + 34 * S], 9 * S,
                                    fill=BTN_HI if self.hi == (r, c) else BTN)
                d.text((bx + bw / 2, by + 17 * S), label, font=REG, fill=TXT, anchor="mm")
            by += 40 * S


class Typing:
    phase = 0

    def height(self):
        return 34 * S

    def draw(self, d, y):
        d.rounded_rectangle([14 * S, y, 84 * S, y + 34 * S], 16 * S, fill=BOT)
        for i in range(3):
            r = (4 + (2 if (self.phase % 3) == i else 0)) * S
            cx = (34 + i * 15) * S
            d.ellipse([cx - r, y + 17 * S - r, cx + r, y + 17 * S + r], fill=DIM)


def L(text, f=MED, col=TXT):
    return (text, f, col)


# 1) /pace
c = Chat()
c.add(Bubble([L("/pace")], me=True))
c.add(Bubble([L("Pace · November", BOLD), L("Day 15: spent €620", REG),
              L("At this pace you end on €1,240", REG), L("Your 3-month average: €1,050", REG, DIM),
              L(""), L("vs last month, same day", BOLD),
              L("Eating out   €180   +45%", REG, BAD), L("Groceries    €210    −8%", REG, OK),
              L("Fuel             €40   new", REG, DIM)]))
c.save("bot-pace.gif")

# 2) /pending with suggestion + learned rule
c = Chat()
c.add(Bubble([L("/pending")], me=True))
card = c.add(Bubble([L("3 to categorize", BOLD), L("12/11 · Revolut", REG, DIM),
                     L("PRIMAPRIX 0231 — €7.35"),
                     L("I'll remember it for anything with “primaprix”.", SMALL, DIM)],
                    buttons=[["★ Groceries"], ["Eating out", "Leisure", "Shopping"],
                             ["Other category"], ["My transfer", "Skip"]]))
for _ in range(2):
    card.hi = (0, 0); c.frame(260)
    card.hi = None; c.frame(220)
card.hi = (0, 0); c.frame(400)
c.items.remove(card)
c.add(Bubble([L("PRIMAPRIX → Groceries  (and 2 more)", MED, OK),
              L("Rule: primaprix → Groceries", SMALL, DIM)]), typing=False)
c.add(Bubble([L("Nothing left to categorize.", MED)]))
c.save("bot-pending.gif")

# 3) month close with a growing donut
c = Chat()
close = c.add(Bubble([L("November close", BOLD), L("Income     €1,450", REG),
                      L("Expenses   €980", REG), L("Saved       €470", BOLD, OK),
                      L(""), L(""), L(""), L(""), L(""), L(""), L("")]))
parts = [(310, "#5b8def", "Groceries"), (220, "#f0a35e", "Rent share"),
         (180, "#e86a8d", "Eating out"), (120, "#7bd389", "Gym & subs"), (150, "#9c7ce8", "Other")]
total = sum(p for p, *_ in parts)


def donut(progress):
    def draw(d):
        h = close.height()
        y0 = H * S - 16 * S - h
        cx, cy, r = 92 * S, y0 + 170 * S, 58 * S
        a = -90
        for v, col, lab in parts:
            sweep = 360 * v / total * progress
            if sweep > 0.5:
                d.pieslice([cx - r, cy - r, cx + r, cy + r], a, a + sweep, fill=col)
            a += 360 * v / total * progress
        d.ellipse([cx - r * .55, cy - r * .55, cx + r * .55, cy + r * .55], fill=BOT)
        for i, (v, col, lab) in enumerate(parts):
            ly = cy - 52 * S + i * 22 * S
            if progress > i / len(parts):
                d.rounded_rectangle([170 * S, ly, 182 * S, ly + 12 * S], 3 * S, fill=col)
                d.text((190 * S, ly - 3 * S), f"{lab}  €{v}", font=REG, fill=TXT)
    return draw


for i in range(1, 19):
    c.frame(70, donut(1 - (1 - i / 18) ** 3))
c.frame(2600, donut(1))
c.frames[0].save(OUT / "bot-month-close.gif", save_all=True, append_images=c.frames[1:],
                 duration=c.durs, loop=0, optimize=True, disposal=2)
print("bot-month-close.gif", len(c.frames), "frames")
