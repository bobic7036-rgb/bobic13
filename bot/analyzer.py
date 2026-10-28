"""Аналіз монет на Binance Futures + генерація зделок.

Використовуємо публічні REST-ендпоінти Binance Futures (без API-ключа):
 - /fapi/v1/ticker/24hr          — зміна ціни за 24 год, обсяг
 - /fapi/v1/klines               — свічки (1h) для RSI / EMA / тренду
 - /fapi/v1/premiumIndex         — funding rate (настрій ринку)

Логіка оцінки "шансу" (score 0-100): комбінуємо кілька індикаторів,
щоб визначити напрямок (LONG/SHORT) та впевненість. На основі ATR
ставимо вхід, стоп-лос і два тейк-профіти.
"""
import asyncio
import math
import random
import urllib.request
import json
from bot.config import BINANCE_FUTURES_URL, GROQ_API_KEY
from bot import ai_engine

_cache = {"tickers": None, "ts": 0}


def _get(url: str, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


async def fetch_json(url: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get, url)


async def get_top_symbols(count: int = 40):
    """Монети з найбільшим обсягом за 24 год (USDT-перпетуали)."""
    now = asyncio.get_running_loop().time()
    if _cache["tickers"] and now - _cache["ts"] < 60:
        tickers = _cache["tickers"]
    else:
        tickers = await fetch_json(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr")
        _cache["tickers"] = tickers
        _cache["ts"] = now
    usdt = [t for t in tickers
            if t["symbol"].endswith("USDT")
            and not any(x in t["symbol"] for x in ("UP", "DOWN", "BULL", "BEAR"))]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return usdt[:count]


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / period, sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        period = max(len(trs), 1)
    return sum(trs[-period:]) / period


async def analyze_symbol(sym: str, market_ctx: dict = None):
    """Повертає dict з аналізом або None.

    market_ctx — контекст ринку від ai_engine (_btc_context): список монет,
    сильніших за BTC. ЛОНГИ беремо ТІЛЬКИ по таких монетах (relative strength),
    шорти — навпаки, по слабких. Це головний фільтр «сміттєвих» лонгів.
    """
    try:
        kl = await fetch_json(
            f"{BINANCE_FUTURES_URL}/fapi/v1/klines?symbol={sym}&interval=1h&limit=100")
    except Exception:
        return None
    if not kl or len(kl) < 30:
        return None
    opens = [float(k[1]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    vols = [float(k[5]) for k in kl]
    price = closes[-1]

    rsi = _rsi(closes)
    ema20 = _ema(closes[-40:], 20)
    ema50 = _ema(closes, 50)
    atr = _atr(highs, lows, closes)

    # тренд останніх 24 годин
    chg24 = (price - closes[-24]) / closes[-24] * 100 if len(closes) >= 24 else 0
    vol_ratio = sum(vols[-6:]) / 6 / (sum(vols[-24:]) / 24 + 1e-9)

    # funding
    funding = 0.0
    try:
        p = await fetch_json(f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex?symbol={sym}")
        funding = float(p.get("lastFundingRate", 0))
    except Exception:
        pass

    score_long = 50.0
    reasons = []

    # 1. Перетин EMA — сила тренду
    if ema20 > ema50:
        score_long += 10
        reasons.append("EMA20 вище EMA50 — висхідний тренд")
    else:
        score_long -= 10
        reasons.append("EMA20 нижче EMA50 — низхідний тренд")

    # 2. RSI
    if rsi < 30:
        score_long += 15
        reasons.append(f"RSI={rsi:.0f} — перепроданість, можливе відскочування")
    elif rsi > 70:
        score_long -= 15
        reasons.append(f"RSI={rsi:.0f} — перекупленість, можливий відкат")
    elif 45 <= rsi <= 55 and ema20 > ema50:
        score_long += 5
        reasons.append(f"RSI={rsi:.0f} — нейтральний, тренд триває")

    # 3. Обсяги
    if vol_ratio > 1.5 and chg24 > 0:
        score_long += 10
        reasons.append(f"Обсяг виріс у {vol_ratio:.1f}x разом з ціною")
    elif vol_ratio > 1.5 and chg24 < 0:
        score_long -= 10
        reasons.append(f"Обсяг виріс у {vol_ratio:.1f}x при падінні ціни")

    # 4. Funding
    if funding < -0.0003:
        score_long += 8
        reasons.append(f"Funding negative ({funding*100:.3f}%) — шортисти платять, тиск вниз вичерпано")
    elif funding > 0.0005:
        score_long -= 8
        reasons.append(f"Funding високий ({funding*100:.3f}%) — ринок перевантажений лонгами")

    # 5. Моментум 24ч
    if 1 < chg24 < 8 and ema20 > ema50:
        score_long += 7
        reasons.append(f"+{chg24:.1f}% за 24г — помірний моментум без перегріву")
    elif chg24 > 12:
        score_long -= 7
        reasons.append(f"+{chg24:.1f}% за 24г — перегрів, ризик корекції")
    elif -8 < chg24 < -1 and ema20 < ema50:
        score_long -= 7
        reasons.append(f"{chg24:.1f}% за 24г — сильний тиск продавців")

    score_long = max(5, min(95, score_long))

    if score_long >= 50:
        side = "LONG"
        score = round(score_long)
    else:
        side = "SHORT"
        score = round(100 - score_long)

    # --- ФІЛЬТР ЯКОСТІ #1: relative strength проти BTC ------------------
    # Статистика бота: лонги по слабких монетах = мінус майже завжди.
    if market_ctx is not None:
        strong_set = set(market_ctx.get("strong_all", []))
        weak_list = market_ctx.get("weak", [])          # (symbol, rs) сортиовано вгору
        btc_chg = market_ctx.get("chg24", 0)
        rs = chg24 - btc_chg                            # сила монети проти BTC за 24г
        if side == "LONG" and sym not in strong_set:
            return None      # лонг монети, що відстає від BTC — сміттєвий сетап
        if side == "SHORT" and sym in strong_set:
            return None      # шорт лідера ринку — стоп майже гарантований
        if side == "SHORT" and rs > -1.0:
            score = max(5, score - 5)   # шорт не найслабшого — трохи м'якше
        if side == "LONG" and rs >= 3.0:
            score = min(95, score + 4)
            reasons.append(f"монета на {rs:.1f} п.п. сильніша за BTC — лідер ринку")

    # --- ФІЛЬТР ЯКОСТІ #2: мінімальна волатильність ---------------------
    # ATR < 0.35% ціни = флет: ціна не встигає дійти до тейків за TTL → EXPIRED.
    a = atr if atr > 0 else price * 0.01
    if a / price < 0.0035:
        return None

    # Рівні на базі ATR
    if side == "LONG":
        entry = price
        sl = price - 1.2 * a
        tp1 = price + 1.5 * a
        tp2 = price + 3.0 * a
    else:
        entry = price
        sl = price + 1.2 * a
        tp1 = price - 1.5 * a
        tp2 = price - 3.0 * a

    return {
        "symbol": sym,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "score": score,
        "reason": "; ".join(reasons[:3]),
        "atr": a,
        "price": price,
        "funding": funding,
    }


def pick_leverage(deposit: float, score: int, risk_pct: float = 2.0):
    """Підбір плеча: фіксований ризик на зделку (% депозиту), стоп ~1.5 ATR.

    Маржа = ризик / (відсоток_стопа * плече) — чим вищий шанс, тим більше
    дозволене плече; малий депозит — обмежуємо плече для захисту від ліквідації.
    """
    if deposit <= 0:
        return 1, 0
    risk_usdt = deposit * risk_pct / 100.0           # скільки готові втратити на стопі
    stop_frac = 0.015                                # ~1.5% рух до стопа (ATR-базовано)
    lev_cap = 20 if score >= 85 else (12 if score >= 75 else 6)
    if deposit < 50:
        lev_cap = min(lev_cap, 5)
    elif deposit < 200:
        lev_cap = min(lev_cap, 10)
    # позиція = risk / stop_frac; маржа = позиція / leverage
    position = risk_usdt / stop_frac
    margin = max(min(position / max(lev_cap, 3), deposit * 0.25), 5)
    lev = int(max(3, min(lev_cap, position / margin)))
    return lev, round(margin, 2)


async def generate_signals(count: int = 3, min_score: int = 80, ai_enabled: bool = True,
                           user_id: int = None, universe: int = 60,
                           min_results: int = 3):
    """Аналізує топ-монет → фільтр якості (RS проти BTC) → локальний AI-енжин
    (мультитаймфрейм) → Groq LLM вердикт.

    ЯКІСТЬ > КІЛЬКІСТЬ: «безлімітний» пошук лишився (якщо сетапів мало —
    розширюємо скан), але планку штучно НЕ знижуємо нижче 72: краще 1 якісна
    зделка в плюс, ніж 5 «90% впевненість», що йдуть у стоп.
    """
    # контекст ринку: BTC-імпульс + список сильних/слабких монет (кеш 5 хв)
    market_ctx = await ai_engine._btc_context()

    syms = await get_top_symbols(max(universe, 40))
    if market_ctx is not None:
        strong_set = set(market_ctx.get("strong_all", []))
        # пріоритет: сильні монети для LONG-кандидатів; решта — пул для SHORT
        queue = [s["symbol"] for s in syms if s["symbol"] in strong_set] + \
                [s["symbol"] for s in syms if s["symbol"] not in strong_set]
    else:
        queue = [s["symbol"] for s in syms]

    async def scan(n_syms: int, floor: int):
        tasks = [analyze_symbol(s, market_ctx) for s in queue[:n_syms]]
        analyzed = await asyncio.gather(*tasks)
        cands = [r for r in analyzed if r and r["score"] >= max(55, floor - 20)]
        if not cands:
            return []
        if ai_enabled:
            if user_id is not None:
                for r in cands:
                    r["user_id"] = user_id
            deepped = await asyncio.gather(*[ai_engine.deep_analyze(r["symbol"], r)
                                             for r in cands])
            res = [d for d in deepped if d and d["score"] >= floor]
        else:
            res = [r for r in cands if r["score"] >= floor]
        # AI-штраф токсичних монет: якщо бот уже двічі поспіль словив SL по парі — мінус 10
        try:
            from bot import db as _db
            for r in res:
                wr = await _db.symbol_winrate(r["symbol"])
                if wr is not None and wr <= 25:
                    r["score"] = max(5, r["score"] - 10)
                    r["reason"] += "; ⚠️ ця монета раніше вже ловила стопи — шанс знижено"
            res = [r for r in res if r["score"] >= floor]
        except Exception:
            pass
        res.sort(key=lambda x: x["score"], reverse=True)
        return res

    FLOOR_MIN = 72   # жорстка нижня межа:Signals нижче 72% не видаємо ніколи
    eff_floor = max(min_score, FLOOR_MIN)
    results = await scan(min(len(queue), 25), eff_floor)
    # Авто-розширення ПОШУКУ (не планки!): мало зделок → скануємо всю вселенну
    if len(results) < min_results:
        wider = await scan(len(queue), eff_floor)
        seen = {r["symbol"] for r in results}
        results += [r for r in wider if r["symbol"] not in seen]
    # Лише якщо ринокзагалі не дає сетапів вище 72 — один спуск до 72
    if len(results) < min_results and eff_floor > FLOOR_MIN:
        lower = await scan(len(queue), FLOOR_MIN)
        seen = {r["symbol"] for r in results}
        results += [r for r in lower if r["symbol"] not in seen]

    top = results[:max(count, min_results)]

    # Фільтр №2: Groq LLM-огляд (текстовий вердикт GO/NO-GO)
    if GROQ_API_KEY and top:
        reviewed = await asyncio.gather(*[ai_engine.groq_review(s) for s in top])
        kept = [s for s in reviewed if s and s["score"] >= FLOOR_MIN]
        top = kept if kept else top[:1]   # якщо LLM відсіяла ВСЕ — лишаємо топ-1 з попередженням, потік не блокуємо

    # ФІЛЬТР №3: Groq Vision AI на РЕАЛЬНОМУ скріншоті графіка Binance
    # (дає ETA «черільки хв відпрацює» + візуальний вердикт). Збій не блокує сигнал.
    if GROQ_API_KEY and top:
        from bot import vision
        vis = await asyncio.gather(*[vision.vision_review(s) for s in top])
        kept = [s for s in vis if s and s["score"] >= FLOOR_MIN]
        top = kept if kept else [v for v in vis if v][:1] or top
    return top
