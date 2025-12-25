import asyncio
import traceback
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject, Update
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from utils.time import get_moscow_now

from config import BOT_TOKEN
from database import (
    init_db,
    log_bot_error,
    get_users_with_premium_expiring_tomorrow,
    mark_premium_expiry_reminder_sent,
)
def _premium_expiry_reminder_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Продлить подписку", callback_data="premium:plans")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="premium:reminder:dismiss")],
        ]
    )


async def premium_expiry_reminder_loop(bot: Bot) -> None:
    """Раз в час проверяем, у кого премиум заканчивается завтра, и шлём уведомление 1 раз."""
    while True:
        try:
            offset = 0
            batch = 2000

            while True:
                users = await get_users_with_premium_expiring_tomorrow(limit=batch, offset=offset)
                if not users:
                    break

                for u in users:
                    tg_id = int(u["tg_id"])
                    premium_until = str(u["premium_until"])

                    should_send = await mark_premium_expiry_reminder_sent(tg_id, premium_until)
                    if not should_send:
                        continue

                    # красивый формат даты
                    human = premium_until
                    try:
                        dt = datetime.fromisoformat(premium_until)
                        human = dt.strftime("%d.%m.%Y")
                    except Exception:
                        pass

                    text = (
                        "⏳ <b>Премиум заканчивается завтра</b>\n\n"
                        f"Подписка активна до <b>{human}</b>.\n"
                        "Продлить сейчас?"
                    )

                    try:
                        await bot.send_message(
                            chat_id=tg_id,
                            text=text,
                            reply_markup=_premium_expiry_reminder_kb(),
                            disable_notification=True,
                        )
                    except Exception:
                        # пользователь мог заблокировать бота/удалить чат и т.п.
                        pass

                offset += batch

        except Exception:
            # не валим polling из-за фонового задания
            pass

        await asyncio.sleep(3600)

from handlers.legal_center import router as help_center_router
from handlers.admin import router as admin_router
from handlers import (
    start,
    upload,
    rate,
    results,
    profile,
    registration,
    moderator,
    premium,
    payments,
    referrals,
    linklike,
)


def _extract_chat_and_user_from_update(update: Update) -> tuple[int | None, int | None]:
    """
    Достаём chat_id и tg_user_id максимально безопасно из Update.
    """
    chat_id = None
    tg_user_id = None

    try:
        if update.message:
            chat_id = update.message.chat.id
            if update.message.from_user:
                tg_user_id = update.message.from_user.id

        elif update.callback_query:
            if update.callback_query.from_user:
                tg_user_id = update.callback_query.from_user.id
            if update.callback_query.message:
                chat_id = update.callback_query.message.chat.id

        elif update.inline_query:
            if update.inline_query.from_user:
                tg_user_id = update.inline_query.from_user.id

        elif update.chosen_inline_result:
            if update.chosen_inline_result.from_user:
                tg_user_id = update.chosen_inline_result.from_user.id

        elif update.edited_message:
            chat_id = update.edited_message.chat.id
            if update.edited_message.from_user:
                tg_user_id = update.edited_message.from_user.id

    except Exception:
        pass

    return chat_id, tg_user_id


class ErrorsToDbMiddleware(BaseMiddleware):
    """
    Ловит любые исключения в хендлерах и пишет в bot_error_logs.
    ВАЖНО: это middleware на уровне update, поэтому event чаще всего Update.
    """

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

            chat_id = None
            tg_user_id = None
            update_type = type(event).__name__

            # если это Update — вытаскиваем нормально
            if isinstance(event, Update):
                chat_id, tg_user_id = _extract_chat_and_user_from_update(event)
            else:
                # fallback (на случай если middleware повесили не туда)
                try:
                    if hasattr(event, "chat") and getattr(event, "chat"):
                        chat_id = event.chat.id
                except Exception:
                    pass
                try:
                    if hasattr(event, "from_user") and getattr(event, "from_user"):
                        tg_user_id = event.from_user.id
                except Exception:
                    pass

            handler_name = None
            try:
                h = data.get("handler")
                # иногда это объект-хендлер, иногда функция
                if hasattr(h, "__name__"):
                    handler_name = h.__name__
                else:
                    handler_name = str(h) if h else None
            except Exception:
                handler_name = None

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
                # не убиваем бота, если БД/логирование легло
                pass

            raise


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment (.env)")

    # Поднимаем БД и таблицы
    await init_db()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # фоновая проверка: напоминания о скором окончании премиума
    asyncio.create_task(premium_expiry_reminder_loop(bot))

    dp = Dispatcher()

    # Логирование ошибок в БД
    dp.update.middleware(ErrorsToDbMiddleware())

    # Роутеры
    dp.include_router(linklike.router)
    dp.include_router(start.router)
    dp.include_router(registration.router)
    dp.include_router(profile.router)
    dp.include_router(upload.router)
    dp.include_router(rate.router)
    dp.include_router(results.router)
    dp.include_router(admin_router)    
    dp.include_router(moderator.router)
    dp.include_router(premium.router)
    dp.include_router(payments.router)
    dp.include_router(referrals.router)
    dp.include_router(help_center_router)

    print("🤖 GlowShot запущен")

    try:
        await dp.start_polling(bot)
    except Exception as e:
        tb = traceback.format_exc()
        try:
            await log_bot_error(
                chat_id=None,
                tg_user_id=None,
                handler="start_polling",
                update_type=None,
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=tb,
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(main())