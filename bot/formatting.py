import time
from bot import db


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 10:
        return f"{p:.3f}".rstrip("0").rstrip(".")
    if p >= 0.1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


def side_emoji(side: str) -> str:
    return "🟢 LONG" if side == "LONG" else "🔴 SHORT"


def est_minutes(t: dict) -> int | None:
    """Орієнтовний час до спрацювання TP1. Пріоритет — думка Фільтру №3 (Vision AI),
    інакше швидка оцінка через ATR/15m."""
    if t.get("eta_min"):
        return int(t["eta_min"])
    atr = t.get("atr") or 0.0
    entry = t.get("entry") or 0.0
    tp1 = t.get("tp1") or 0.0
    if atr > 0 and entry > 0 and tp1:
        dist = abs(tp1 - entry) / entry * 100          # % шляху до TP1
        atr_pct = atr / entry * 100                    # скільки % монета робить за ~1 годину (4*ATR15m≈година)
        hourly = max(atr_pct * 2.5, 0.15)              # помірний дрейф = 2.5 ATR-одиниці на годину
        mins = int(dist / hourly * 60)
        return max(10, min(180, mins))
    return None


def signal_text(t, lev=None, margin=None) -> str:
    coin = t["symbol"].replace("USDT", "")
    lines = [
        f"⚡️ <b>ЗДЕЛКА: {coin}USDT</b>",
        "",
        f"Напрямок: <b>{side_emoji(t['side'])}</b>",
        f"Шанс (за аналізом): <b>{t['score']}%</b>",
        f"📍 Вхід: <code>{fmt_price(t['entry'])}</code>",
        f"🛑 Стоп-лос: <code>{fmt_price(t['sl'])}</code>",
        f"✅ Тейк 1: <code>{fmt_price(t['tp1'])}</code>",
        f"✅ Тейк 2: <code>{fmt_price(t['tp2'])}</code>",
    ]
    eta = est_minutes(t)
    if eta:
        lines.append(f"⏱ Очікуване спрацювання TP1: <b>~{eta} хв</b> (за волатильністю та даними ІІ)")
    if t.get("vision"):
        lines.append(f"🛰 <i>Фільтр №3 (скрін Binance):</i> {t['vision']}")
    if lev:
        lines += [
            "",
            f"🎯 Рекомендоване плече: <b>x{lev}</b>",
            f"💰 Маржа на зделку: <b>{fmt_price(margin)} USDT</b> (~50% на TP1, ~50% на TP2)",
        ]
    lines += ["", f"🧠 <i>Чому так піде:</i> {t['reason']}"]
    return "\n".join(lines)


def result_text(t, status: str, price: float) -> str:
    coin = t["symbol"].replace("USDT", "")
    emoji = {"TP1": "✅", "TP2": "🏆", "SL": "🛑"}[status]
    label = {"TP1": "тейк-профіт 1", "TP2": "тейк-профіт 2", "SL": "СТОП-ЛОС"}[status]
    pnl = t["pnl"] or 0.0
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    entry = t["entry"] or 0.0
    # чесний % руху від входу до ФАКТИЧНОЇ ціни виходу
    side = 1 if t["side"] == "LONG" else -1
    move_pct = (price - entry) / entry * 100 * side if entry else 0.0
    tail = ""
    if status == "TP1":
        tail = "\n📌 Друга половина позиції лишається в ринку — тримаємо до TP2 🏆"
    return (
        f"{emoji} <b>РЕЗУЛЬТАТ: {coin}USDT ({t['side']})</b>\n"
        f"Ціна дійшла до {label}: <code>{fmt_price(price)}</code>\n"
        f"Вхід був: <code>{fmt_price(entry)}</code>, шанс {t['score']}%\n"
        f"Рух по ринку: <b>{'+' if move_pct >= 0 else ''}{move_pct:.2f}%</b>\n"
        f"PnL по зделці: <b>{pnl_str} USDT</b>" + tail
    )


def trade_line(t) -> str:
    """Один рядок списку зделок — ЧЕСНИЙ, з фактичною ціною виходу і % руху."""
    coin = t["symbol"].replace("USDT", "")
    entry = t["entry"] or 0.0
    st = t["status"]
    pnl = t["pnl"] or 0.0
    side = 1 if t["side"] == "LONG" else -1
    if st == "OPEN":
        eta = int(t.get("eta_min") or 0) if hasattr(t, "get") else 0
        try:
            eta = int(dict(t).get("eta_min") or 0)
        except Exception:
            eta = 0
        tag = f"⏳ відкрита (ІІ чекає TP1 ~{eta} хв)" if eta else "⏳ відкрита"
    elif st == "TP1":
        tag = "✅ TP1 взято (+%.2f$), тримаю другу половину" % pnl
    elif st == "TP2":
        tag = "🏆 TP2 (+%.2f$)" % pnl
    elif st == "SL":
        tag = "🛑 стоп (%.2f$)" % pnl
    elif st == "EXPIRED":
        tag = "⌛ прострочена (%.2f$)" % pnl
    else:
        tag = st
    # для закритих показуємо фактичну ціну виходу (exit_price), якщо є
    ep = (t["exit_price"] or 0.0) if st not in ("OPEN",) else 0.0
    if ep and entry:
        mv = (ep - entry) / entry * 100 * side
        detail = f" → {fmt_price(ep)} ({'+' if mv >= 0 else ''}{mv:.2f}%)"
    else:
        detail = ""
    return f"• {coin} {t['side']} @ {fmt_price(entry)}{detail} — {tag}"
