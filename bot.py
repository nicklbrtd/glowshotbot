import asyncio
import traceback
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject, Update, Message, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.event.bases import SkipHandler

from utils.time import get_moscow_now

from config import BOT_TOKEN, MASTER_ADMIN_ID
from database import (
    init_db,
    log_bot_error,
    get_users_with_premium_expiring_tomorrow,
    mark_premium_expiry_reminder_sent,
    get_user_block_status_by_tg_id,
    set_user_block_status_by_tg_id,
    is_user_soft_deleted,
    reactivate_user_by_tg_id,
    hide_active_photos_for_user,
    restore_photos_from_status,
    get_user_by_tg_id,
)
def _premium_expiry_reminder_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Продлить подписку", callback_data="premium:plans")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="premium:reminder:dismiss")],
        ]
    )


def _admin_error_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Открыть логи", callback_data="admin:logs")],
        ]
    )


# простая анти-спам защита: одинаковая ошибка в том же хендлере не чаще раза в 30 секунд
_LAST_ADMIN_ERR: dict[str, float] = {}
_ADMIN_ERR_COOLDOWN_SEC = 30.0


def _err_key(handler_name: str | None, error_type: str, error_text: str) -> str:
    h = handler_name or "unknown"
    t = (error_text or "").strip()
    if len(t) > 180:
        t = t[:180]
    return f"{h}|{error_type}|{t}"


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
    app_registration,
    moderator,
    premium,
    payments,
    referrals,
    linklike,
    streak,
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


def _format_block_notice(block: dict, until_dt) -> str:
    reason = (block.get("block_reason") or "").strip()
    clean_reason = reason
    for prefix in ("FULL_BAN:", "UPLOAD_BAN:", "ADMIN_BAN:"):
        if clean_reason.startswith(prefix):
            clean_reason = clean_reason[len(prefix):].strip()
            break

    if until_dt is not None:
        days = max(1, int((until_dt - get_moscow_now()).total_seconds() // 86400 + 1))
        text_lines: list[str] = [f"⛔ Ваш аккаунт заблокирован на {days} дней."]
        text_lines.append(f"До: {until_dt.strftime('%d.%m.%Y %H:%M')} (МСК).")
    else:
        text_lines = ["⛔ Ваш аккаунт заблокирован."]

    if clean_reason:
        text_lines.append(f"Причина: {clean_reason}")
    text_lines.append("")
    text_lines.append("Доступно только: профиль (для удаления аккаунта) и /help.")
    return "\n".join(text_lines)


def _shorten(text: str, limit: int = 190) -> str:
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


class BlockGuardMiddleware(BaseMiddleware):
    """
    Глобальная проверка блокировки пользователя.
    Блокированному пользователю доступны только: /help и профиль (для удаления аккаунта).
    После удаления аккаунта бот молчит.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user_id: int | None = None

        try:
            if isinstance(event, Update):
                _, tg_user_id = _extract_chat_and_user_from_update(event)
            elif hasattr(event, "from_user") and getattr(event, "from_user"):
                tg_user_id = event.from_user.id  # type: ignore[attr-defined]
        except Exception:
            tg_user_id = None

        if tg_user_id is None:
            return await handler(event, data)

        # Если аккаунт удалён — разрешаем только /start (для повторной регистрации), остальное игнорируем.
        try:
            if await is_user_soft_deleted(int(tg_user_id)):
                # Разрешаем только стартовое сообщение для восстановления
                if isinstance(event, Update) and event.message:
                    text = (event.message.text or "").strip()
                    if text.startswith("/start"):
                        # даём пройти дальше в хендлеры; реанимация произойдёт после регистрации
                        pass
                    else:
                        # всё остальное гасим
                        raise SkipHandler
                else:
                    raise SkipHandler
        except SkipHandler:
            raise
        except Exception:
            pass

        block = {}
        try:
            block = await get_user_block_status_by_tg_id(int(tg_user_id))
        except Exception:
            block = {}

        is_blocked = bool(block.get("is_blocked"))
        until_dt = None
        until_raw = block.get("block_until")
        if until_raw:
            try:
                until_dt = datetime.fromisoformat(str(until_raw))
            except Exception:
                until_dt = None

        # Истёкшая блокировка: автоматически снимаем
        if is_blocked and until_dt is not None and until_dt <= get_moscow_now():
            try:
                await set_user_block_status_by_tg_id(int(tg_user_id), is_blocked=False, reason=None, until_iso=None)
                user_row = await get_user_by_tg_id(int(tg_user_id))
                if user_row:
                    await restore_photos_from_status(int(user_row["id"]), from_status="blocked_by_ban", to_status="active")
            except Exception:
                pass
            is_blocked = False

        if not is_blocked:
            return await handler(event, data)

        # Скрываем активные фото заблокированного пользователя из выдачи (однократно, best-effort)
        try:
            user_row = await get_user_by_tg_id(int(tg_user_id))
            if user_row:
                await hide_active_photos_for_user(int(user_row["id"]), new_status="blocked_by_ban")
        except Exception:
            pass

        block_text = _format_block_notice(block, until_dt)
        short_block_text = _shorten(block_text)

        # Разрешённые случаи для заблокированных: /help и Help-кнопки, профиль+удаление.
        def _is_profile_delete_callback(cb_data: str) -> bool:
            return cb_data in {
                "menu:profile",
                "profile:edit",
                "profile:delete",
                "profile:delete_confirm",
            }

        # Update с message / callback внутри
        if isinstance(event, Update):
            if event.message:
                text = (event.message.text or "").strip()
                if text.startswith("/help"):
                    return await handler(event, data)
                try:
                    await event.message.answer(block_text)
                except Exception:
                    pass
                raise SkipHandler

            if event.callback_query:
                data_str = event.callback_query.data or ""
                if data_str.startswith("help:") or _is_profile_delete_callback(data_str):
                    try:
                        await event.callback_query.answer(short_block_text, show_alert=True)
                    except Exception:
                        pass
                    return await handler(event, data)
                try:
                    await event.callback_query.answer(short_block_text, show_alert=True)
                except Exception:
                    pass
                raise SkipHandler

            return await handler(event, data)

        # Если middleware повешено на message/callback напрямую
        if isinstance(event, Message):
            text = (event.text or "").strip()
            if text.startswith("/help"):
                return await handler(event, data)
            try:
                await event.answer(block_text)
            except Exception:
                pass
            raise SkipHandler

        if isinstance(event, CallbackQuery):
            data_str = event.data or ""
            if data_str.startswith("help:") or _is_profile_delete_callback(data_str):
                try:
                    await event.answer(short_block_text, show_alert=True)
                except Exception:
                    pass
                return await handler(event, data)
            try:
                await event.answer(short_block_text, show_alert=True)
            except Exception:
                pass
            raise SkipHandler

        return await handler(event, data)



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
        except SkipHandler:
            # Пропускаем обработку без логирования (нормальный флоу BlockGuard)
            raise
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

            err_type = type(e).__name__
            err_text = str(e)

            logged_ok = False
            try:
                await log_bot_error(
                    chat_id=chat_id,
                    tg_user_id=tg_user_id,
                    handler=handler_name,
                    update_type=update_type,
                    error_type=err_type,
                    error_text=err_text,
                    traceback_text=tb,
                )
                logged_ok = True
            except Exception:
                # не убиваем бота, если БД/логирование легло
                logged_ok = False

            # Пушим мастеру-админу краткое уведомление (если задан) — даже если логирование упало,
            # чтобы ты видел, что бот реально падает.
            try:
                if MASTER_ADMIN_ID:
                    key = _err_key(handler_name, err_type, err_text)
                    now_ts = datetime.utcnow().timestamp()
                    last = _LAST_ADMIN_ERR.get(key, 0.0)
                    if now_ts - last >= _ADMIN_ERR_COOLDOWN_SEC:
                        _LAST_ADMIN_ERR[key] = now_ts

                        handler_label = handler_name or "—"
                        chat_label = str(chat_id) if chat_id is not None else "—"
                        user_label = str(tg_user_id) if tg_user_id is not None else "—"
                        log_flag = "✅" if logged_ok else "⚠️"

                        text = (
                            f"🚨 <b>Ошибка в боте</b> {log_flag}\n\n"
                            f"<b>{err_type}</b>: <code>{err_text[:700]}</code>\n\n"
                            f"Хендлер: <code>{handler_label}</code>\n"
                            f"Update: <code>{update_type}</code>\n"
                            f"chat_id: <code>{chat_label}</code>\n"
                            f"user_id: <code>{user_label}</code>\n\n"
                            "Открыть список логов?"
                        )

                        await data["bot"].send_message(
                            chat_id=MASTER_ADMIN_ID,
                            text=text,
                            reply_markup=_admin_error_kb(),
                            disable_notification=True,
                        )
            except Exception:
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

    # Глобальный блок: заблокированным доступны только /help и удаление аккаунта через профиль
    dp.update.middleware(BlockGuardMiddleware())

    # Логирование ошибок в БД
    dp.update.middleware(ErrorsToDbMiddleware())

    # Роутеры
    dp.include_router(linklike.router)
    dp.include_router(start.router)
    dp.include_router(registration.router)
    dp.include_router(app_registration.router)
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
    dp.include_router(streak.router)

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
