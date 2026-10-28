"""Підписки та оплата Telegram Stars (зірками).

Для адміністратора (ADMIN_ID) ВСЕ безкоштовно: підписка видається
автоматично без оплати. Для всіх інших — реальна оплата зірками через
send_invoice (валюта XTR) + pre_checkout_query + successful_payment.

ВАЖЛИВО (баг виправлено): для XTR сума в send_invoice передається ПРЯМО
В ЗІРКАХ (amount = кількість ⭐), а не в «тонах». Попередній STAR=10^6 давав
помилку CURRENCY_TOTAL_AMOUNT_INVALID → «не вдалося створити рахунок».
"""
import time
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, SuccessfulPayment,
)
from aiogram.filters import Command, CommandStart
from bot import db
from bot.config import ADMIN_ID, STAR

router = Router()

PLAN_NAMES = {"base": "БАЗОВА", "pro": "ПРОФЕСІЙНА"}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def subscription_kb(user_id: int):
    if is_admin(user_id):
        btn = InlineKeyboardButton(
            text="👑 Адмін — доступ безкоштовний (активний)", callback_data="adm_free")
        return InlineKeyboardMarkup(inline_keyboard=[[btn]])
    base_stars = await db.get_int("stars_base")
    pro_stars = await db.get_int("stars_pro")
    base_days = await db.get_int("base_days")
    pro_days = await db.get_int("pro_days")
    rows = [
        [InlineKeyboardButton(text=f"📦 БАЗОВА — ⭐ {base_stars} / {base_days} дн.",
                              callback_data="buy:base")],
        [InlineKeyboardButton(text=f"💎 ПРОФЕСІЙНА — ⭐ {pro_stars} / {pro_days} дн.",
                              callback_data="buy:pro")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm_free")
async def cb_adm_free(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Тільки для адміна 😉", show_alert=True)
        return
    await cb.answer("👑 Тобі все безкоштовно!")


@router.message(Command("subscribe"))
@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    await db.ensure_user(uid, message.from_user.username or "")
    # 👑 адміну — авто-підписка назавжди при кожному /start
    if is_admin(uid):
        for p in ("base", "pro"):
            await db.set_subscription(uid, p, 36500)  # ~100 років
    u = await db.get_user(uid)
    now = time.time()

    def until(ts):
        if is_admin(uid):
            return "♾️ безкоштовно (адмін)"
        d = (ts - now) // 86400 if ts else -1
        return f"ще {int(d)} дн." if d >= 0 else "❌ немає"

    text = (
        "🤖 <b>Crypto Signals Bot</b>\n"
        "Готові зделки з Binance Futures: вхід, стоп, 2 тейки, шанс і пояснення.\n\n"
        f"📦 Базова підписка: {until(u['base_until'])}\n"
        f"💎 Професійна підписка: {until(u['pro_until'])}\n\n"
        + ("👑 Ти адмін — усі функції БЕЗКОШТОВНО та назавжди.\n"
           if is_admin(uid) else
           "Оплата зірками (⭐ Telegram Stars) — миттєво і безпечно.\n"
           "Обери підписку 👇 або напиши /help")
    )
    await message.answer(text, parse_mode="HTML", reply_markup=await subscription_kb(uid))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery):
    plan = cb.data.split(":")[1]
    if plan not in PLAN_NAMES:
        await cb.answer()
        return

    # 👑 АДМІН — все безкоштовно, активація без оплати
    if is_admin(cb.from_user.id):
        await db.set_subscription(cb.from_user.id, plan, 36500)
        name = PLAN_NAMES[plan]
        await cb.message.answer(
            f"👑 <b>{name}</b> підписка активована БЕЗКОШТОВНО (ти адмін). ♾️",
            parse_mode="HTML",
        )
        await cb.answer("Безкоштовно для адміна 👑")
        return

    stars = max(await db.get_int(f"stars_{plan}"), 1)   # кількість зірок
    days = await db.get_int(f"{plan}_days")
    amount = stars * STAR                    # для XTR: напряму в зірках (STAR=1)
    title = f"Підписка {PLAN_NAMES[plan]} {days} дн."[:32]      # назва ≤32 символів
    description = f"Доступ до сигналів. Оплата ⭐ Telegram Stars ({days} днів)."[:255]
    payload = f"sub:{plan}:{cb.from_user.id}"

    try:
        # Для ⭐ XTR: provider_token="" і amount ПРЯМО В ЗІРКАХ (перевірено live-API).
        await cb.bot.send_invoice(
            chat_id=cb.from_user.id,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
            title=title,
            description=description,
            payload=payload,
            provider_token="",
        )
    except Exception as e:
        await cb.message.answer(f"⚠️ Не вдалося створити рахунок: {e}")
    await cb.answer()


# ------------------- ЕТАПИ ОПЛАТИ ЗІРКАМИ -------------------

@router.pre_checkout_query()
async def process_pre_checkout(pc):
    # Валідація: валюта XTR (зірки). Для XTR total_amount — це ПРЯМО кількість
    # зірок (1..100000), тому поріг «50 тон» прибрано (заважав би ціні 3⭐).
    ok = pc.currency == "XTR" and pc.total_amount >= 1
    if ok:
        await pc.answer(success=True)
    else:
        await pc.answer(success=False, error_message="⚠️ Невірні параметри оплати.")


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    pay: SuccessfulPayment = message.successful_payment
    try:
        _, plan, uid_s = pay.invoice_payload.split(":")
        uid = int(uid_s)
    except (ValueError, AttributeError):
        return
    if plan not in PLAN_NAMES:
        return
    days = await db.get_int(f"{plan}_days")
    await db.set_subscription(uid, plan, days)
    stars = max(pay.total_amount // STAR, 1)   # для XTR total_amount = зірки
    name = PLAN_NAMES[plan]
    if plan == "pro":
        extra = ("\n\n💎 Твоя PRO-підписка активна! Доступно все:\n"
                 "/pro або /signals — пакет зделок просто зараз\n"
                 "/deposit 100 — вкажи депозит, ІІ підбере плече\n"
                 "Автоматичні зделки приходитимуть самі кожні N хв (як на базовій).")
    elif plan == "base":
        extra = ("\n\n📦 Твоя базова підписка активна!\n"
                 "/signals — зделки просто зараз + авто-розсилка кожні N хв.")
    else:
        extra = ""
    try:
        await message.bot.send_message(
            uid,
            f"🎉 <b>Оплата зірками пройшла успішно!</b>\n"
            f"Активовано <b>{name}</b> підписку на {days} днів (⭐ {stars})."
            + extra,
            parse_mode="HTML",
        )
    except Exception:
        pass
    # сповіщення адміну про продаж
    try:
        await message.bot.send_message(
            ADMIN_ID,
            f"💰 Продаж! @{message.from_user.username or ''} (id {message.from_user.id})\n"
            f"План: {name} — ⭐ {stars} за {days} днів",
        )
    except Exception:
        pass
