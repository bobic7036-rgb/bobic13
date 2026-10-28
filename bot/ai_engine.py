"""Безкоштовне ІІ для аналізу монет.

Два рівні (працюють разом):

1) ЛОКАЛЬНИЙ AI-ЕНЖИН (безкоштовний, завжди працює):
   - мультитаймфайм: 15m / 1h / 4h — узгодженість трендів;
   - VWAP сесії + відхилення ціни;
   - стискавання волатильності (BB squeeze) перед виходом;
   - «бажання і страх» (greed/fear індекс по обсягах і руху);
   - підтримки/опори по локальних мінімумах/максимумах;
   - R:R фільтрація — у беремо лише сетапи, де тейк далеко за стоп.

2) GROQ LLM (безкоштовний ключ https://console.groq.com, llama-3.3-70b-versatile):
   фінальний вердикт по сетапу: GO / NO-GO + коригування шансу.
   Якщо ключа немає або сервіс недоступний — бот просто використовує
   локальний еngine (все одно безкоштовно).
"""
import asyncio
import json
import urllib.request

from bot.config import BINANCE_FUTURES_URL, GROQ_API_KEY, GROQ_MODEL


def _get(url: str, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


async def fetch_klines(symbol: str, interval: str, limit: int = 100):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None, _get,
            f"{BINANCE_FUTURES_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    except Exception:
        return []


# ---------- індикатори ----------

def _ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _atr(highs, lows, closes, period=14):
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    p = min(period, len(trs)) or 1
    return sum(trs[-p:]) / p


def _vwap(kl):
    """VWAP по останніх свічках (kl — сирий список klines)."""
    num = den = 0.0
    for k in kl:
        h, l, c, v = float(k[2]), float(k[3]), float(k[4]), float(k[5])
        tp = (h + l + c) / 3
        num += tp * v
        den += v
    return num / den if den else None


def _bb_width(closes, period=20):
    if len(closes) < period:
        return 0.0
    seg = closes[-period:]
    m = sum(seg) / period
    var = sum((c - m) ** 2 for c in seg) / period
    sd = var ** 0.5
    return (4 * sd) / m if m else 0.0


def _trend_score(closes, vols):
    """+1..+3 висхідний / -1..-3 низхідний: EMA9/EMA21 + нахил + обсяг."""
    if len(closes) < 25:
        return 0
    e9, e21 = _ema(closes, 9), _ema(closes, 21)
    slope = (closes[-1] - closes[-8]) / closes[-8] * 100 if len(closes) >= 8 else 0
    s = 0
    s += 1 if e9 > e21 else -1
    s += 1 if slope > 0.3 else (-1 if slope < -0.3 else 0)
    v_recent = sum(vols[-5:]) / 5
    v_base = sum(vols[-25:]) / 25 + 1e-9
    if v_recent / v_base > 1.3:
        s += 1 if slope > 0 else (-1 if slope < 0 else 0)
    return max(-3, min(3, s))


def _support_resistance(highs, lows, price, atr):
    """Локальні екстремуми -> рівні S/R; повертає відстань до них у ATR."""
    a = atr if atr and atr > 0 else price * 0.01
    sup_d, res_d = 99.0, 99.0
    # локальні мінімуми (підтримки) нижче ціни
    for i in range(2, len(lows) - 2):
        c = lows[i]
        if c <= min(lows[i-2:i]) and c <= min(lows[i+1:i+3]) and c < price:
            d = (price - c) / a
            if 0.15 <= d < sup_d:
                sup_d = d
    # локальні максимуми (опори) вище ціни
    for i in range(2, len(highs) - 2):
        c = highs[i]
        if c >= max(highs[i-2:i]) and c >= max(highs[i+1:i+3]) and c > price:
            d = (c - price) / a
            if 0.15 <= d < res_d:
                res_d = d
    if sup_d == 99.0:   # немає явного рівня — беремо 1.5 ATR як буфер
        sup_d = 1.5
    if res_d == 99.0:
        res_d = 1.5
    return sup_d, res_d


def _greed_fear(closes, vols, chg_pct, rsi):
    """Індекс бажання та страху 0..100 (>50 — жадібність, <50 — страх)."""
    gf = 50.0
    gf += max(-15, min(15, chg_pct * 1.5))          # рух ціни
    gf += (rsi - 50) * 0.4                          # RSI
    vr = (sum(vols[-6:]) / 6) / (sum(vols[-48:]) / 48 + 1e-9)
    if vr > 1.4 and chg_pct > 0:
        gf += 10                                    # агресивні покупки — гreed
    elif vr > 1.4 and chg_pct < 0:
        gf -= 10                                    # панічні продажі — fear
    return max(0, min(100, gf))


# ---------- BTC-контекст і широта ринку (кеш 5 хв) ----------

_btc_cache = {"ts": 0.0, "ctx": None}


async def fetch_tickers():
    """Тікери 24h Binance Futures (кеш 60 сек через analyzer)."""
    from bot import analyzer
    return await analyzer.fetch_json(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr")


async def _btc_context():
    """"{"chg6": зміна BTC за 6 год (%), "chg24": зміна BTC за 24 год (%),
    "strong_all": [монети сильніші за BTC (≥1п.п.)], "strong": [лідери ≥2п.п.],
    "weak": [(symbol, rs) найслабші], "breadth": частка монет сильніших за BTC}.

    Кешується на 5 хв. Це фундамент фільтрації якості: LONG — лише по strong_all,
    SHORT — пріоритетно по weak.
    """
    import time as _t
    if _btc_cache["ctx"] is not None and _t.time() - _btc_cache["ts"] < 300:
        return _btc_cache["ctx"]
    try:
        kl = await fetch_klines("BTCUSDT", "1h", 8)
        if not kl or len(kl) < 7:
            return None
        c = [float(k[4]) for k in kl]
        chg6 = (c[-1] - c[-6]) / c[-6] * 100
        # 24h-дані з тих самих тікерів: стабільніший RS за весь день
        tickers = await fetch_tickers()
        btc_chg24 = 0.0
        for t in tickers:
            if t["symbol"] == "BTCUSDT":
                btc_chg24 = float(t["priceChangePercent"])
                break
        strong_all, strong, weak = [], [], []
        above = 0
        head = tickers[:150]
        for t in head:
            pcp = float(t["priceChangePercent"])
            rs = pcp - btc_chg24
            if rs > 0:
                above += 1
            if rs >= 1.0:
                strong_all.append(t["symbol"])
                if rs >= 2.0:
                    strong.append(t["symbol"])
            elif rs <= -1.0:
                weak.append((t["symbol"], rs))
        weak.sort(key=lambda x: x[1])          # найдужче впали — на початку
        ctx = {"chg6": chg6, "chg24": btc_chg24,
               "strong_all": strong_all, "strong": strong, "weak": weak,
               "breadth": above / max(len(head), 1)}
        _btc_cache["ts"] = _t.time()
        _btc_cache["ctx"] = ctx
        return ctx
    except Exception:
        return None


async def market_bias() -> int:
    """Загальний напрямок ринку: +1 LONG-перевага, -1 SHORT-перевага, 0 нейтрально.

    Логіка: BTC падає + більшість альтів слабші за BTC → шорти альтів
    статистично найнадійніші; BTC росте + альти сильніші → лонги.
    """
    ctx = await _btc_context()
    if ctx is None:
        return 0
    b = ctx["breadth"]          # частка монет сильніших за BTC (0..1)
    chg = ctx["chg6"]
    if chg <= -0.8 and b < 0.45:
        return -1
    if chg >= 0.8 and b > 0.55:
        return 1
    if b < 0.30:                # дуже вузький ринок — майже лише шорт weak-alts
        return -1
    return 0


# ---------- головний локальний аналіз ----------

async def deep_analyze(symbol: str, base: dict) -> dict | None:
    """Підтверджує/завертає сетап по 3 таймфреймах. Повертає оновлену зделку або None."""
    kl15 = await fetch_klines(symbol, "15m", 96)
    kl1h = await fetch_klines(symbol, "1h", 100)
    kl4h = await fetch_klines(symbol, "4h", 60)
    if not kl15 or not kl1h or not kl4h:
        return None

    def unpack(kl):
        o = [float(k[1]) for k in kl]; h = [float(k[2]) for k in kl]
        l = [float(k[3]) for k in kl]; c = [float(k[4]) for k in kl]
        v = [float(k[5]) for k in kl]
        return o, h, l, c, v

    _, h1, l1, c1, v1 = unpack(kl15)
    _, h2, l2, c2, v2 = unpack(kl1h)
    _, h4, l4, c4, v4 = unpack(kl4h)

    price = c2[-1]
    t15 = _trend_score(c1, v1)
    t1h = _trend_score(c2, v2)
    t4h = _trend_score(c4, v4)
    side = base["side"]
    sign = 1 if side == "LONG" else -1

    # --- 0. BTC-спадність (relative strength) — головний фільтр якості ---
    # Крипта рухається за BTC: LONG монети, що відстає від BTC, або SHORT
    # сильної монети = майже гарантований стоп. Перевіряємо OBI (6h).
    btc_ctx = await _btc_context()
    obi_penalized = False
    if btc_ctx is not None and len(c2) >= 7:
        chg6 = (c2[-1] - c2[-7]) / c2[-7] * 100
        obi = chg6 - btc_ctx["chg6"]              # сила монети відносно BTC
        if (sign > 0 and obi < -2.0) or (sign < 0 and obi > 2.0):
            return None                            # йдемо проти BTC-сили — не беремо
        if abs(obi) >= 1.0 and ((obi > 0) == (sign > 0)):
            btc_ctx.setdefault("strong", []).append(symbol)
        # шортимо лише справді СЛАБКІ монети (ведуть ринок вниз), а не будь-які
        if sign < 0:
            weak_syms = {w[0] for w in btc_ctx.get("weak", [])}
            if weak_syms and symbol not in weak_syms and obi > -1.0:
                return None                        # монета не серед аутсайдерів — шорт слабкий

    # --- 0b. Свіжість імпульсу: ціна вже пройшла >0.8 ATR від старту руху?
    # Заходити в «хвіст» — класична причина мінусу при високому %.
    atr_now = _atr(h2, l2, c2)
    ref_entry = base.get("entry") or price
    moved = (price - ref_entry) * sign
    if atr_now > 0 and moved > 0.8 * atr_now:
        return None

    # --- 1. Узгодженість таймфреймів (головний фільтр якості) ---
    agree = sum(1 for t in (t15, t1h, t4h) if t * sign >= 1)   # м'яка згода
    strong = sum(1 for t in (t15, t1h, t4h) if t * sign >= 2)  # сильна згода
    conflict = sum(1 for t in (t15, t1h, t4h) if t * sign <= -2)
    if conflict >= 2:
        return None                                  # проти двох ТФ — не беремо
    score = base["score"]
    reasons = []
    if strong == 3 or (strong >= 2 and agree == 3):
        score += 18
        reasons.append("🔥 AI: тренди 15m/1h/4h повністю збігаються")
    elif agree >= 2:
        score += 10
        reasons.append("AI: більшість таймфреймів підтверджують напрямок")
    elif agree == 1:
        score -= 6
    else:
        score -= 10
        if score < 55:
            return None

    # бонус за силу відносно BTC (лідер ринку в свій бік)
    if btc_ctx is not None and len(c2) >= 7:
        chg6 = (c2[-1] - c2[-7]) / c2[-7] * 100
        obi = chg6 - btc_ctx["chg6"]
        if (obi > 0) == (sign > 0) and abs(obi) >= 1.5:
            score += 6
            reasons.append(f"монета сильніша за BTC на {abs(obi):.1f}% за 6 год — лідер руху")
        elif abs(obi) < 0.5:
            score -= 4     # ідуть внічию з BTC — менше ймовірності продовження

    # --- 2. VWAP ---
    vwap = _vwap(kl1h)
    if vwap:
        dev = (price - vwap) / vwap * 100
        if side == "LONG" and dev < -0.8:
            score += 5
            reasons.append(f"ціна нижче VWAP ({dev:.1f}%) — недооцінена в межах сесії")
        elif side == "SHORT" and dev > 0.8:
            score += 5
            reasons.append(f"ціна вище VWAP (+{dev:.1f}%) — переоцінена, логічний шорт")
        elif (side == "LONG" and dev > 2.5) or (side == "SHORT" and dev < -2.5):
            score -= 8                               # перегрів відносно середньої

    # --- 3. BB squeeze — вихід зі стиску по тренду ---
    w_now = _bb_width(c2)
    hist_w = [_bb_width(c2[:i]) for i in range(30, len(c2))]
    if hist_w and w_now < min(hist_w) * 1.15 and abs(t1h) >= 2:
        score += 7
        reasons.append("стиск волатильності (squeeze) — імпульльний рух починається")

    # --- 4. Підтримка/опір: стоп ставимо ЗА рівнем, тейк — ДО наступного ---
    atr = base["atr"]
    sup_d, res_d = _support_resistance(h2, l2, price, atr)   # у ATR
    entry = price
    if side == "LONG":
        sl = entry - min(max(1.2 * atr, (sup_d + 0.3) * atr), 2.5 * atr)  # стоп за підтримкою, але не ширше 2.5 ATR
        tp1 = entry + 1.6 * atr
        tp2 = entry + min(3.2, max(2.2, res_d - 0.2)) * atr  # тейк перед опором
        risk = entry - sl
        rr = (tp1 - entry) / risk if risk > 0 else 0
        if res_d < 1.2:
            return None                                      # до опору надто близько
        if rr >= 1.2:
            score += 5
            reasons.append(f"гарне співвідношення ризик/прибуток R:R={rr:.1f}")
        elif rr < 0.8:
            return None                              # ризик більший за потенційний прибуток
    else:
        sl = entry + min(max(1.2 * atr, (res_d + 0.3) * atr), 2.5 * atr)  # стоп за опором, максимум 2.5 ATR
        tp1 = entry - 1.6 * atr
        tp2 = entry - min(3.2, max(2.2, sup_d - 0.2)) * atr  # тейк перед підтримкою
        risk = sl - entry
        rr = (entry - tp1) / risk if risk > 0 else 0
        if sup_d < 1.2:
            return None
        if rr >= 1.2:
            score += 5
            reasons.append(f"гарне співвідношення ризик/прибуток R:R={rr:.1f}")
        elif rr < 0.8:
            return None                              # ризик більший за потенційний прибуток

    # --- 5. Бажання і страх ---
    chg = (price - c2[-24]) / c2[-24] * 100
    rsi = _rsi(c2)
    gf = _greed_fear(c2, v2, chg, rsi)
    if side == "LONG" and gf < 40:
        score += 6
        reasons.append(f"🧠 Страх ({gf:.0f}/100) на ринку — розумні купують, коли страшно")
    elif side == "LONG" and gf > 78:
        score -= 8
        reasons.append(f"⚠️ Надмірна жадібність ({gf:.0f}/100) — ризик зворотного руху")
    elif side == "SHORT" and gf > 72:
        score += 6
        reasons.append(f"🧠 Ринок занадто жадібний ({gf:.0f}/100) — історично час шортити")
    elif side == "SHORT" and gf < 28:
        score -= 8

    # --- 6. RSI екстремуми проти напрямку ---
    if side == "LONG" and rsi > 78:
        return None
    if side == "SHORT" and rsi < 25:
        return None

    # --- 7. AI-адаптивність: якщо останні зделки закривались по SL — підвищуємо планку ---
    try:
        from bot import db as _db
        recent = await _db.recent_statuses(base.get("user_id"), 6) if base.get("user_id") else []
        sl_streak = 0
        for stt in recent:
            if stt == "SL":
                sl_streak += 1
            else:
                break
        if sl_streak >= 2:
            score -= 8 * sl_streak   # після серії стопів потрібен значно сильніший сетап
    except Exception:
        pass

    score = max(5, min(95, round(score)))
    if score < 55:
        return None

    out = dict(base)
    out.update({
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "score": score, "rr": round(rr, 2), "gf": round(gf),
        "reason": "; ".join(reasons[:3] or ["AI: мультитаймфрейм підтверджено"]),
        "ai_confirmed": True,
    })
    return out


# ---------- GROQ LLM (безкоштовне ІІ, опційно) ----------

PROMPT = """Ти — суворий трейдер-аналітик Binance Futures. Оціни зделку і поверни СТРИКТО JSON без тексту довкола:
{{"verdict":"GO" або "NO-GO","confidence":число 0-100,"note":"одне коротке речення українською чому"}}
Критерії GO: тренди ТФ збігаються, R:R>=1.5, стоп за рівнем підтримки/опору, funding не проти, немає перегріву.

ЗДЕЛКА: {symbol} {side}
Вхід {entry}, стоп {sl}, TP1 {tp1}, TP2 {tp2}
Шанс локального AI: {score}%, R:R={rr}, Індекс бажання/страх={gf}/100
Funding rate: {funding}. Пояснення: {reason}
"""


async def groq_review(sig: dict) -> dict:
    """Повертає (оновлений sig) після вердикту LLM. Без ключа — без змін."""
    if not GROQ_API_KEY:
        return sig
    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0.2,
        "max_tokens": 150,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": PROMPT.format(
            symbol=sig["symbol"], side=sig["side"], entry=round(sig["entry"], 6),
            sl=round(sig["sl"], 6), tp1=round(sig["tp1"], 6), tp2=round(sig["tp2"], 6),
            score=sig["score"], rr=sig.get("rr", 1.5), gf=sig.get("gf", 50),
            funding=sig.get("funding", 0), reason=sig["reason"][:300])}],
    }).encode()

    def _post():
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {GROQ_API_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _post)
        data = json.loads(resp["choices"][0]["message"]["content"])
        verdict = str(data.get("verdict", "GO")).upper()
        conf = int(data.get("confidence", sig["score"]))
        note = str(data.get("note", ""))[:200]
        if verdict.startswith("NO"):
            return None                              # ІІ заблокувало зделку
        sig = dict(sig)
        sig["score"] = max(5, min(95, round((sig["score"] + conf) / 2)))
        sig["reason"] = f"🤖 Groq AI: {note}. {sig['reason']}"
        return sig
    except Exception:
        return sig                                   # сервіс недоступний — локальний AI вирішує
