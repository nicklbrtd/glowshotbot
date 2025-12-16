import asyncio
import traceback
from config import BOT_TOKEN

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

from database import init_db, log_bot_error


from handlers import (
    legal_center,
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
    referrals,
)


class ErrorsToDbMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as e:
            tb = traceback.format_exc()

            # пытаемся достать чат / юзера максимально безопасно
            chat_id = None
            tg_user_id = None
            try:
                if hasattr(event, "chat") and event.chat:
                    chat_id = event.chat.id
            except Exception:
                pass
            try:
                if hasattr(event, "from_user") and event.from_user:
                    tg_user_id = event.from_user.id
            except Exception:
                pass

            handler_name = None
            try:
                h = data.get("handler")
                if h and hasattr(h, "__name__"):
                    handler_name = h.__name__
            except Exception:
                pass

            update_type = type(event).__name__

            try:
                await log_bot_error(
                    chat_id=chat_id,
                    tg_user_id=tg_user_id,
                    handler=handler_name,
                    update_type=update_type,
                    error_type=type(e).__name__,
                    error_text=str(e),
                    traceback_text=tb,
                )
            except Exception:
                # если даже логирование упало — не добиваем бота
                pass

            raise


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment (.env)")

    await init_db()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.update.middleware(ErrorsToDbMiddleware())
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
    dp.include_router(legal_center.router)
    dp.include_router(referrals.router)

    print("🤖 GlowShot запущен")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        tb = traceback.format_exc()
        await log_bot_error(
            chat_id=None,
            tg_user_id=None,
            handler="start_polling",
            update_type=None,
            error_type=type(e).__name__,
            error_text=str(e),
            traceback_text=tb,
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())