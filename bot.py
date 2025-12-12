import asyncio

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN
from database import init_db

from handlers import (
    start,
    upload,
    rate,
    results,
    profile,
    admin,
    registration,
    moderator,
    support_link,
    premium,
    payments,
    terms,
    referrals,
)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment (.env)")

    await init_db()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.include_router(start.router)
    dp.include_router(registration.router)
    dp.include_router(profile.router)
    dp.include_router(upload.router)
    dp.include_router(rate.router)
    dp.include_router(results.router)
    dp.include_router(admin.router)
    dp.include_router(moderator.router)
    dp.include_router(support_link.router)
    dp.include_router(premium.router)
    dp.include_router(payments.router)
    dp.include_router(terms.router)
    dp.include_router(referrals.router)

    print("🤖 GlowShot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())