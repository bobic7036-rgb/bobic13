"""Фонові задачі: генерація зделок (з AI-фільтрами), моніторинг TP/SL, денний PnL-звіт."""
import asyncio
import time
from aiogram import Bot
from bot import db, analyzer, signals_core
from bot.db import _calc_pnl
from bot.formatting import signal_text, result_text, fmt_price

last_gen = {"ts": 0}


def consecutive_losses(statuses: list[str], max_loss: int) -> bool:
    """Стоп серії: чи останні N закритих зделок — суцільні SL."""
    n = 0
    for s in statuses:
        if s == "SL":
            n += 1
            if n >= max_loss:
                return True
        else:
            return False
    return False


async def _can_send_new(bot: Bot, uid: int) -> bool:
    """AI-захист капіталу: ліміт відкритих + стоп серії.

    ВАЖЛИВО: щоб захист НЕ блокував користувача назавжди — старі зделки
    мають час життя (signal_ttl_min). Ті, що «зависли» без TP/SL довше TTL,
    закриваються як EXPIRED і звільняють слоти для нових сигналів.
    """
    await expire_stale_trades()
    max_open = await db.get_int("max_open_trades")
    open_now = await db.count_open_for_user(uid)
    if open_now >= max_open:
        try:
            await bot.send_message(
                uid,
                f"ℹ️ У тебе зараз <b>{open_now}</b> активних зделок (ліміт {max_open}).\n"
                f"Нові сигнали прийдуть автоматично, як тільки якісь закриються по TP/SL "
                f"або через {await db.get_int('signal_ttl_min')} хв (авто-експірація).\n"
                f"Статус усіх: /mytrades")
        except Exception:
            pass
        return False
    max_loss = await db.get_int("max_consecutive_losses")
    if max_loss > 0 and consecutive_losses(await db.recent_statuses(uid, 6), max_loss):
        try:
            await bot.send_message(
                uid,
                f"🛡 <b>Пауза сигналів.</b> Минулі {max_loss} зделки поспіль закрились по стопу.\n"
                f"Бот бере паузу, щоб не розкручувати мінус. Слідуй за ринком далі — "
                f"як тільки серія стабілізується, сигнали відновляться автоматично.")
        except Exception:
            pass
        return False
    return True


async def expire_stale_trades(bot=None):
    """Закриває завислі OPEN-зделки старше TTL → статус EXPIRED.

    ВАЖЛИВО: PnL рахується ЧЕСНО по фактичній ринковій ціні на момент
    закриття (а не 0), щоб /pnl і денний звіт показували реальний результат.
    Користувачів сповіщаємо про результат (як і при TP/SL).
    """
    ttl = await db.get_int("signal_ttl_min")
    cutoff = int(time.time()) - ttl * 60
    cur = await db._db.execute(
        "SELECT * FROM trades WHERE status IN ('OPEN','TP1') AND created_at < ?",
        (cutoff,))
    stale = await cur.fetchall()
    # фактичні ціни для чесного PnL
    prices = {}
    if stale:
        from bot import analyzer
        for sym in {t["symbol"] for t in stale}:
            try:
                d = await analyzer.fetch_json(
                    f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}")
                prices[sym] = float(d["price"])
            except Exception:
                pass
    now = int(time.time())
    for t in stale:
        price = prices.get(t["symbol"]) or t["entry"]   # без даних — вважаймо вхід (pnl≈0)
        # ЧЕСНО: прибуток/збиток по ФАКТИЧНІЙ ринковій ціні на момент закриття
        pnl = _calc_pnl(t, "EXPIRED", price)
        await db._db.execute(
            "UPDATE trades SET status='EXPIRED', pnl=pnl+?, exit_price=?, closed_at=? WHERE id=?",
            (pnl, price, now, t["id"]))
        await db._db.commit()
    await db._db.execute(
        "UPDATE signal_log SET status='EXPIRED', closed_at=? "
        "WHERE status='OPEN' AND created_at < ?", (now, cutoff))
    await db._db.commit()
    # сповіщення (окремо, щоб не спамити по одній зделці)
    if bot is not None and prices:
        by_user = {}
        for t in stale:
            by_user.setdefault(t["user_id"], []).append(t)
        for uid, ts in by_user.items():
            lines = ["⌛ <b>Час зделки вийшов (TTL) — закриваємо по ринку:</b>", ""]
            tot = 0.0
            for t in ts:
                price = prices.get(t["symbol"]) or t["entry"]
                pnl = _calc_pnl(t, "EXPIRED", price)
                tot += pnl
                coin = t["symbol"].replace("USDT", "")
                lines.append(f"• {coin} {t['side']} @ {fmt_price(t['entry'])} "
                             f"→ {fmt_price(price)} ({'+' if pnl >= 0 else ''}{pnl:.2f} USDT)")
            lines.append("")
            lines.append(f"Разом: <b>{'+' if tot >= 0 else ''}{tot:.2f} USDT</b>. "
                         f"Ціна не дійшла ні до тейків, ні до стопу за "
                         f"{ttl} хв — слоти звільнено для нових сигналів.")
            try:
                await bot.send_message(uid, "\n".join(lines), parse_mode="HTML")
            except Exception:
                pass


async def auto_tune():
    """Self-learning ІІ: підлаштовує планку сигналу під РЕАЛЬНИЙ ГРОШОВИЙ результат.

    Головна зміна: дивимось не лише на винрейт (%), а на чистий виграш (PnL) —
    бо 60% TP1 по +0.2$ проти 40% SL по -3$ = мінус, і навпаки.

    • середній PnL зделки < 0 або винрейт < 45% → жорсткішаємо планку;
    • обидва показники хороші → нарощуємо кількість зделок, планку НЕ знижуємо
      нижче безпечної межі (аналог FLOOR_MIN у generate_signals).
    """
    if (await db.get_int("auto_relax")) != 1:
        return
    st = await db.signal_stats(days=7)
    ex = await db.expectancy_stats(days=7)
    if st["total"] < 6 and ex["count"] < 6:
        return
    ms = await db.get_int("min_score")
    cnt = await db.get_int("signals_count")
    avg_pnl = ex["avg_pnl"] or 0
    losing = (ex["count"] >= 6 and avg_pnl < 0) or st["winrate"] < 45
    winning = st["winrate"] >= 58 and (ex["count"] < 6 or avg_pnl >= 0)
    if losing and ms < 90:
        await db.set_setting("min_score", min(90, ms + 2))
    elif winning and ms > 78:
        # ринок дає плюс → нарощуємо КІЛЬКІСТЬ, а не знижуємо якість
        if cnt < 5:
            await db.set_setting("signals_count", cnt + 1)
    elif st["total"] >= 15 and st["winrate"] >= 50 and cnt < 5 and not losing:
        await db.set_setting("signals_count", cnt + 1)


async def send_signals_round(bot: Bot):
    """Раунд авто-розсилки: ІІ генерує пакет ОДИН раз, далі deliver_package
    для кожного підписника (базового й PRO). PRO отримує ВСЕ з базової
    (ті самі авто-зделки кожні N хв) + плече під депозит.

    Захисти (ліміт відкритих / серія стопів / дублікати) — усередині
    signals_core.deliver_package: жоден користувач ніколи не залишається
    без конкретного результату, і залежних «мертвих» слотів більше немає."""
    await auto_tune()
    # спершу синхронізуємо активні зделки з ринком → слоти звільняються до розсилки
    await signals_core.sync_open_trades(bot)
    try:
        signals = await signals_core.get_round_signals(force_new=True)
    except Exception as e:
        print("generate_signals error:", e)
        return
    if not signals:
        print("no signals this round (навіть авто-розширення не знайшло сетапів)")
        return

    risk_pct = await db.get_int("risk_per_trade_pct")
    pro_auto = (await db.get_int("pro_auto_rounds")) == 1

    # БАЗОВІ підписники — сигнали без плеча
    base_users = await db.active_subscribers("base")
    for u in base_users:
        try:
            base_flag, _ = await db.get_auto_flags(u["user_id"])
            if not base_flag:     # користувач вимкнув авто-зделки через /settings
                continue
            await signals_core.deliver_package(
                bot, u["user_id"], pro=False, deposit=0,
                risk_pct=risk_pct, show_header=True)
        except Exception as e:
            print("deliver base error:", e)

    # ПРО підписники — ВСЕ як на базовій (авто-раунди) + плече під депозит
    if pro_auto:
        pro_users = await db.active_subscribers("pro")
        for u in pro_users:
            try:
                base_auto, pro_flag = await db.get_auto_flags(u["user_id"])
                if not pro_flag:      # користувач вимкнув авто-зделки через /settings
                    continue
                row = await db.get_user(u["user_id"])
                await signals_core.deliver_package(
                    bot, u["user_id"], pro=True, deposit=row["deposit"] or 0,
                    risk_pct=risk_pct, show_header=True)
            except Exception as e:
                print("deliver pro error:", e)


async def monitor_trades(bot: Bot):
    """Перевіряє відкриті зделки проти поточної ціни Binance.

    ЧЕСНИЙ PnL: зделка закривається по ФАКТИЧНОМУ тіку, а не «по гарному»:
    • якщо тік одразу перетнув SL (ціна пробила і стоп, і тейк між хвилинами) —
      рахуємо СТОП, а не тейк (консервативно, як на біржі);
    • після TP1 друга половина позиції тримається до TP2 або до фактичної ціни;
    • EXPIRED — закривається по ринковій ціні на момент TTL (у expire_stale_trades).
    """
    trades = await db.open_trades()
    if not trades:
        return
    symbols = {t["symbol"] for t in trades}
    prices = {}
    for sym in symbols:
        try:
            d = await analyzer.fetch_json(
                f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}")
            prices[sym] = float(d["price"])
        except Exception:
            continue

    closed = []  # (trade_row_after_update, status, price)
    for t in trades:
        price = prices.get(t["symbol"])
        if not price:
            continue
        hit = None
        sl_hit = price <= t["sl"] if t["side"] == "LONG" else price >= t["sl"]
        tp2_hit = price >= t["tp2"] if t["side"] == "LONG" else price <= t["tp2"]
        tp1_hit = price >= t["tp1"] if t["side"] == "LONG" else price <= t["tp1"]
        if sl_hit:
            # стоп має пріоритет: навіть якщо формально торкнулось тейка —
            # на реальному ринку міг бути фільт/прокол, рахуємо чесно
            hit = ("SL", price)
        elif tp2_hit:
            hit = ("TP2", t["tp2"])
        elif tp1_hit and t["status"] == "OPEN":
            hit = ("TP1", t["tp1"])
        if hit:
            await db.close_trade(t["id"], hit[0], hit[1])
            cur = await db._db.execute("SELECT * FROM trades WHERE id=?", (t["id"],))
            row = await cur.fetchone()
            closed.append((row, hit[0], hit[1]))

    # Паралельно оновлюємо журнал сигналів (для /stats і self-learning AI)
    try:
        for s in await db.open_signal_ids():
            price = prices.get(s["symbol"])
            if not price:
                continue
            st = None
            if s["side"] == "LONG":
                if price <= s["sl"]:
                    st = "SL"
                elif price >= s["tp2"]:
                    st = "TP2"
                elif price >= s["tp1"]:
                    st = "TP1"
            else:
                if price >= s["sl"]:
                    st = "SL"
                elif price <= s["tp2"]:
                    st = "TP2"
                elif price <= s["tp1"]:
                    st = "TP1"
            if st:
                await db.close_signal(s["id"], st)
    except Exception as e:
        print("signal_log update error:", e)

    for row, status, price in closed:
        if not row:
            continue
        try:
            await bot.send_message(row["user_id"], result_text(row, status, price), parse_mode="HTML")
        except Exception:
            pass


async def daily_report(bot: Bot):
    """Ввечері надсилає PRO-користувачам підсумок дня (з AI-статистикою)."""
    users = await db.active_subscribers("pro")
    for u in users:
        row = await db.get_user(u["user_id"])
        dep = row["deposit"] or 0
        day = await db.day_pnl(u["user_id"])
        tot = await db.total_pnl(u["user_id"])
        st = await db.winrate_stats(u["user_id"], days=30)
        pnl = day["s"] or 0
        pct = (pnl / dep * 100) if dep else 0
        advice = ""
        if st["total"] >= 5:
            if st["winrate"] >= 60:
                advice = "🤖 AI-висновок: система в плюсовій зоні, продовжуємо за планом."
            elif pnl < 0:
                advice = ("🤖 AI-висновок: серія невдач — бот автоматично підвищив фільтри "
                          "(менше зделок, вищий шанс), щоб вийти в плюс.")
            else:
                advice = "🤖 AI-висновок: винрейт стабільний, тримаємо ризик на кожен вхід ≤2%."
        text = (
            f"🌙 <b>Підсумок дня</b>\n\n"
            f"Депозит: <b>{fmt_price(dep)} USDT</b>\n"
            f"PnL за 24 год: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USDT</b> ({'+' if pct >= 0 else ''}{pct:.2f}%)\n"
            f"Зделок закрито за добу: <b>{day['c']}</b>\n"
            f"Загальний PnL: <b>{'+' if (tot['s'] or 0) >= 0 else ''}{(tot['s'] or 0):.2f} USDT</b>\n\n"
            f"📊 Статистика 30 днів (TP/SL):\n"
            f"✅ TP1: {st['tp1']} | 🏆 TP2: {st['tp2']} | 🛑 SL: {st['sl']}\n"
            f"Винрейт: <b>{st['winrate']}%</b>\n"
            + (f"\n{advice}" if advice else "")
        )
        try:
            await bot.send_message(u["user_id"], text, parse_mode="HTML")
        except Exception:
            pass


async def scheduler_loop(bot: Bot):
    """Головний цикл фонових задач."""
    last_report_day = None
    while True:
        now = time.time()
        interval = await db.get_int("signals_interval_min") * 60
        if now - last_gen["ts"] >= interval:
            last_gen["ts"] = now
            asyncio.create_task(send_signals_round(bot))
        await asyncio.sleep(60)   # моніторинг щохвилини
        try:
            await expire_stale_trades()   # авто-зачистка завислих зделок
            await monitor_trades(bot)
        except Exception as e:
            print("monitor error:", e)
        # звіт о 21:00
        lt = time.localtime(now)
        if lt.tm_hour == 21 and lt.tm_min < 5 and last_report_day != lt.tm_yday:
            last_report_day = lt.tm_yday
            asyncio.create_task(daily_report(bot))
