import asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.config import BOT_TOKEN
from bot import db
from bot.handlers import router as user_router
from bot.payments import router as pay_router
from bot.scheduler import scheduler_loop


async def main():
    await db.init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(user_router)
    dp.include_router(pay_router)

    # меню команд
    await bot.set_my_commands([
        BotCommand(command="start", description="Головне меню / підписка"),
        BotCommand(command="subscribe", description="Обрати підписку"),
        BotCommand(command="signals", description="Актуальні зделки"),
        BotCommand(command="pro", description="PRO-зделки + плече (ПРОФЕСІЙНА)"),
        BotCommand(command="deposit", description="Вказати депозит (PRO)"),
        BotCommand(command="promo", description="Активація промокоду"),
        BotCommand(command="mytrades", description="Мої зделки"),
        BotCommand(command="pnl", description="Мій PnL"),
        BotCommand(command="status", description="Статус підписки"),
        BotCommand(command="help", description="Довідка"),
    ])

    print("🤖 Bot started")
    sched = asyncio.create_task(scheduler_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        sched.cancel()
        await db._db.close()


if __name__ == "__main__":
    asyncio.run(main())
