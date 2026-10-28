"""Графік монети до кожної зделки — малюємо локально (безкоштовно, без API-ключів).

TradingView-подібний свічковий графік по даних Binance Futures:
свічки 15m + EMA20/EMA50 + лінії ВХОДУ / СТОПУ / ТЕЙК1 / ТЕЙК2.
Модуль не тягне aiogram — щоб його можна було тестувати окремо.
"""
import io
import os

os.environ.setdefault("MPLBACKEND", "Agg")   # headless-safe (Railway/Render)

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg


def _setup_cyr():
    """Підбирає шрифт із кирилицею; якщо в системі немає — повертає латиніцю."""
    for name in ("DejaVu Sans", "Liberation Sans", "Noto Sans"):
        try:
            path = font_manager.findfont(name, fallback_to_default=False)
            if path and os.path.exists(path):
                matplotlib.rcParams["font.family"] = name
                return True
        except Exception:
            continue
    return False


_CYR = _setup_cyr()
TXT = "ВХІД" if _CYR else "ENTRY"
SL_ = "СТОП" if _CYR else "STOP"
TP1_ = "ТЕЙК 1" if _CYR else "TP1"
TP2_ = "ТЕЙК 2" if _CYR else "TP2"
SIDE_ = {"LONG": "ЛОНГ", "SHORT": "ШОРТ"} if _CYR else {"LONG": "LONG", "SHORT": "SHORT"}
TITLE_ = "Binance Futures · 15m" if _CYR else "Binance Futures · 15m"


def _ema(vals, period):
    if len(vals) < period:
        return None
    k = 2 / (period + 1)
    out = [sum(vals[:period]) / period]
    for v in vals[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return [None] * (period - 1) + out


def render_chart(klines: list, symbol: str, side: str, entry: float,
                 sl: float, tp1: float, tp2: float, score: int) -> bytes:
    """klines — сирий ответ Binance /fapi/v1/klines. Повертає PNG-байти."""
    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    vols = [float(k[5]) for k in klines]
    n = len(closes)
    x = list(range(n))

    fig = Figure(figsize=(9.5, 6.0), dpi=110)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(2, 1, 1)
    axv = fig.add_subplot(2, 1, 2)
    fig.patch.set_facecolor("#0b0e15")
    for a in (ax, axv):
        a.set_facecolor("#0b0e15")
        a.tick_params(colors="#8a93a6", labelsize=8)
        for s in a.spines.values():
            s.set_color("#2a3040")
        a.grid(color="#1c2230", linewidth=0.6, alpha=0.7)

    # ── свічки ──
    up = "#26a69a"
    down = "#ef5350"
    for i in range(n):
        c = up if closes[i] >= opens[i] else down
        ax.plot([x[i], x[i]], [lows[i], highs[i]], color=c, linewidth=0.8, zorder=2)
        ax.add_patch(matplotlib.patches.Rectangle(
            (x[i] - 0.36, min(opens[i], closes[i])), 0.72,
            max(abs(closes[i] - opens[i]), 1e-12),
            facecolor=c, edgecolor=c, linewidth=0.5, zorder=3))

    # ── EMA ──
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if e20:
        ax.plot(x, e20, color="#f5c518", linewidth=1.2,
                label="EMA20", zorder=4)
    if e50:
        ax.plot(x, e50, color="#7b61ff", linewidth=1.2,
                label="EMA50", zorder=4)

    # ── рівні зделки ──
    xr = (-n * 0.05, n * 1.02)
    def level(y, color, text, style="-"):
        ax.axhline(y, color=color, linewidth=1.4, linestyle=style, zorder=5)
        ax.text(xr[1] - 0.2, y, f" {text} {y:g}", color=color, fontsize=8,
                va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="round,pad=0.25", fc="#0b0e15", ec=color, lw=0.8))

    level(entry, "#42a5f5", TXT)
    level(sl, "#ff1744", SL_, "--")
    level(tp1, "#00e676", TP1_, "--")
    level(tp2, "#00c853", TP2_, "--")

    coin = symbol.replace("USDT", "")
    col_dir = up if side == "LONG" else down
    ax.set_title(f"{coin}USDT · {SIDE_.get(side, side)} · ІІ шанс {score}%  ({TITLE_})",
                 color="#e8ecf4", fontsize=12, pad=10)
    ax.legend(loc="upper left", fontsize=8, facecolor="#0b0e15",
              edgecolor="#2a3040", labelcolor="#c8d0de")

    ymin = min(min(lows), sl, tp1, tp2, entry)
    ymax = max(max(highs), sl, tp1, tp2, entry)
    pad = (ymax - ymin) * 0.06 or ymin * 0.01
    ax.set_ylim(ymin - pad, ymax + pad)
    ax.set_xlim(*xr)
    ax.set_xticks([])

    # ── обсяги ──
    colors = [up if closes[i] >= opens[i] else down for i in range(n)]
    axv.bar(x, vols, width=0.72, color=colors, alpha=0.75)
    axv.set_xlim(*xr)
    axv.set_ylim(0, max(vols) * 1.25)
    axv.ticklabel_format(axis="y", style="scientific", scilimits=(0, 0))
    axv.get_yaxis().get_offset_text().set_color("#8a93a6")
    axv.set_xticks([])

    fig.tight_layout(pad=1.2)
    buf = io.BytesIO()
    canvas.print_png(buf)
    plt_close = getattr(fig, "clear", None)
    if plt_close:
        plt_close()
    return buf.getvalue()
