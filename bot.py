import asyncio
import time
import traceback
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject, Update, Message, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.event.bases import SkipHandler

from utils.time import get_moscow_now, get_moscow_today

from config import BOT_TOKEN, MASTER_ADMIN_ID
from services.jobs import (
    finalize_party_job,
    daily_credits_grant_job,
    daily_results_publish_job,
    notifications_worker,
)
from database import (
    init_db,
    log_bot_error,
    get_users_with_premium_expiring_tomorrow,
    mark_premium_expiry_reminder_sent,
    get_user_block_status_by_tg_id,
    set_user_block_status_by_tg_id,
    is_user_soft_deleted,
    hide_active_photos_for_user,
    restore_photos_from_status,
    get_user_by_tg_id,
    get_tech_mode_state,
    get_update_mode_state,
    get_due_scheduled_broadcasts,
    mark_scheduled_broadcast_sent,
    mark_scheduled_broadcast_failed,
    get_all_users_tg_ids,
    get_premium_users,
    get_moderators,
    get_support_users,
    get_helpers,
    log_activity_event,
    get_user_by_id,
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

TECH_MODE_PHOTO_FILE_ID = "AgACAgIAAyEFAATVO5BPAAMmaYOxPhK6qvJxaQEXZ6qS4EpKVbMAArYOaxs3vSBI4HK0YtIU5asBAAMCAAN3AAM4BA"
TECH_MODE_CAPTION = "🛠 Технические работы. Попробуй позже."
_BROADCAST_SEND_DELAY_SEC = 0.05


async def _delete_message_after(bot: Bot, chat_id: int, message_id: int, delay_sec: int = 15) -> None:
    try:
        await asyncio.sleep(delay_sec)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


class UpdateModeMiddleware(BaseMiddleware):
    """
    Режим «Обновление»: при включении полностью игнорируем любые действия обычных пользователей.
    Админы/модераторы/саппорт работают как обычно. Сообщения не отправляем.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            state = await get_update_mode_state()
        except Exception:
            return await handler(event, data)

        if not bool(state.get("update_enabled")):
            return await handler(event, data)

        chat_id, tg_user_id = None, None
        try:
            if isinstance(event, Update):
                chat_id, tg_user_id = _extract_chat_and_user_from_update(event)
            elif hasattr(event, "from_user") and getattr(event, "from_user"):
                tg_user_id = event.from_user.id  # type: ignore[attr-defined]
                if hasattr(event, "chat") and getattr(event, "chat"):
                    chat_id = event.chat.id  # type: ignore[attr-defined]
        except Exception:
            pass

        if tg_user_id is None:
            return await handler(event, data)

        if MASTER_ADMIN_ID and tg_user_id == MASTER_ADMIN_ID:
            return await handler(event, data)

        try:
            u = await get_user_by_tg_id(int(tg_user_id))
        except Exception:
            u = None

        if u and (u.get("is_admin") or u.get("is_moderator") or u.get("is_support")):
            return await handler(event, data)

        # Полный игнор для остальных
        raise SkipHandler


async def scheduled_broadcast_loop(bot: Bot) -> None:
    """Периодически отправляет запланированные рассылки."""
    while True:
        try:
            due = await get_due_scheduled_broadcasts(limit=5)
        except Exception:
            due = []

        if not due:
            await asyncio.sleep(20)
            continue

        for item in due:
            try:
                target = str(item.get("target") or "")
                text_body = str(item.get("text") or "")
                created_by = item.get("created_by_tg_id")

                tg_ids: list[int] = []

                def _is_valid_user(u: dict) -> bool:
                    if not u:
                        return False
                    if u.get("is_deleted"):
                        return False
                    if u.get("is_blocked"):
                        return False
                    name = (u.get("name") or "").strip()
                    if not name:
                        return False
                    return True

                if target == "all":
                    tg_ids = await get_all_users_tg_ids()
                elif target == "premium":
                    users = await get_premium_users()
                    tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id") and _is_valid_user(u)]
                elif target == "moderators":
                    users = await get_moderators()
                    tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
                elif target == "support":
                    users = await get_support_users()
                    tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
                elif target == "helpers":
                    users = await get_helpers()
                    tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
                elif target == "test" and created_by:
                    tg_ids = [int(created_by)]

                tg_ids = list({uid for uid in tg_ids if uid})

                if target == "all":
                    header = ""
                elif target == "premium":
                    header = "💎 <b>Сообщение для GlowShot Premium</b>"
                elif target == "test":
                    header = "🧪 <b>Тестовая рассылка</b>"
                else:
                    header = "👥 <b>Сообщение для команды GlowShot</b>"

                send_text = text_body if not header else f"{header}\n\n{text_body}"

                total = len(tg_ids)
                sent = 0

                for uid in tg_ids:
                    try:
                        await bot.send_message(
                            chat_id=uid,
                            text=send_text,
                        )
                        sent += 1
                    except Exception:
                        continue
                    await asyncio.sleep(_BROADCAST_SEND_DELAY_SEC)

                await mark_scheduled_broadcast_sent(
                    int(item["id"]),
                    total_count=total,
                    sent_count=sent,
                )
            except Exception as e:
                try:
                    await mark_scheduled_broadcast_failed(int(item.get("id") or 0), str(e))
                except Exception:
                    pass

        await asyncio.sleep(1)


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


async def alltime_cache_refresh_loop() -> None:
    """Refresh all-time cache payload once per Moscow day (no visible messages)."""
    last_day = None
    while True:
        try:
            day_key = get_moscow_today()
            if day_key != last_day:
                try:
                    from database_results import refresh_alltime_cache_payload

                    await refresh_alltime_cache_payload(day_key=day_key)
                except Exception:
                    pass
                last_day = day_key
        except Exception:
            pass

        await asyncio.sleep(900)

from handlers.legal_center import router as help_center_router
from handlers.admin import router as admin_router
from handlers import (
    author,
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
    streak,
    feedback,
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


# Простое логирование активности для графиков (не чаще 1 раза в минуту на пользователя)
_ACTIVITY_LAST: dict[int, float] = {}
_ACTIVITY_COOLDOWN_SEC = 60.0


class ActivityLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user_id: int | None = None
        username: str | None = None
        kind = "update"
        try:
            if isinstance(event, Update):
                if event.message:
                    if event.message.from_user:
                        tg_user_id = event.message.from_user.id
                        username = event.message.from_user.username
                    kind = "message"
                elif event.callback_query:
                    if event.callback_query.from_user:
                        tg_user_id = event.callback_query.from_user.id
                        username = event.callback_query.from_user.username
                    kind = "callback"
        except Exception:
            tg_user_id = None

        if tg_user_id:
            now_ts = time.time()
            last_ts = _ACTIVITY_LAST.get(int(tg_user_id), 0.0)
            if now_ts - last_ts >= _ACTIVITY_COOLDOWN_SEC:
                _ACTIVITY_LAST[int(tg_user_id)] = now_ts
                try:
                    await log_activity_event(int(tg_user_id), kind=kind, username=username)
                except Exception:
                    pass

        return await handler(event, data)



class TechModeMiddleware(BaseMiddleware):
    """
    Глобальный тех.режим: блокирует доступ всем, кроме админов/модераторов/поддержки.
    Показывает фото-уведомление и удаляет его через 15 секунд.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            state = await get_tech_mode_state()
        except Exception:
            return await handler(event, data)

        if not bool(state.get("tech_enabled")):
            return await handler(event, data)

        start_at_raw = state.get("tech_start_at")
        if start_at_raw:
            try:
                start_dt = datetime.fromisoformat(str(start_at_raw))
                if get_moscow_now() < start_dt:
                    return await handler(event, data)
            except Exception:
                pass

        chat_id, tg_user_id = None, None
        try:
            if isinstance(event, Update):
                chat_id, tg_user_id = _extract_chat_and_user_from_update(event)
            elif hasattr(event, "from_user") and getattr(event, "from_user"):
                tg_user_id = event.from_user.id  # type: ignore[attr-defined]
                if hasattr(event, "chat") and getattr(event, "chat"):
                    chat_id = event.chat.id  # type: ignore[attr-defined]
        except Exception:
            pass

        if tg_user_id is None:
            return await handler(event, data)

        if MASTER_ADMIN_ID and tg_user_id == MASTER_ADMIN_ID:
            return await handler(event, data)

        try:
            u = await get_user_by_tg_id(int(tg_user_id))
        except Exception:
            u = None

        if u and (u.get("is_admin") or u.get("is_moderator") or u.get("is_support")):
            return await handler(event, data)

        if chat_id is not None:
            try:
                sent = await data["bot"].send_photo(
                    chat_id=chat_id,
                    photo=TECH_MODE_PHOTO_FILE_ID,
                    caption=TECH_MODE_CAPTION,
                    disable_notification=True,
                )
                asyncio.create_task(_delete_message_after(data["bot"], chat_id, sent.message_id, 15))
            except Exception:
                try:
                    sent = await data["bot"].send_message(
                        chat_id=chat_id,
                        text=TECH_MODE_CAPTION,
                        disable_notification=True,
                    )
                    asyncio.create_task(_delete_message_after(data["bot"], chat_id, sent.message_id, 15))
                except Exception:
                    pass

        if isinstance(event, Update) and event.callback_query:
            try:
                await event.callback_query.answer()
            except Exception:
                pass

        raise SkipHandler


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
    # фоновая отправка запланированных рассылок
    asyncio.create_task(scheduled_broadcast_loop(bot))
    # ежедневное обновление кэша итогов за всё время (без видимых сообщений)
    asyncio.create_task(alltime_cache_refresh_loop())
    # фоновые джобы жизненного цикла/экономики/итогов
    asyncio.create_task(finalize_party_job(bot))
    asyncio.create_task(daily_credits_grant_job(bot))
    asyncio.create_task(daily_results_publish_job(bot))

    async def _send_notification(_: int, item: dict):
        """Простой отправитель уведомлений из notification_queue."""
        user_id = int(item.get("user_id"))
        user = await get_user_by_id(user_id)
        if not user or not user.get("tg_id"):
            return
        chat_id = int(user["tg_id"])
        n_type = str(item.get("type") or "")
        payload = item.get("payload") or {}
        text = None
        if n_type == "final_rank":
            rank = payload.get("final_rank")
            text = f"📊 Итоги партии: ваше фото заняло место #{rank}. Спасибо за участие!"
        elif n_type == "daily_results_top":
            rank = int(payload.get("rank") or 0)
            submit_day = str(payload.get("submit_day") or "")
            threshold = int(payload.get("top_threshold") or 0)
            text = (
                f"🏆 Итоги за {submit_day} опубликованы.\n"
                f"Твоя работа в TOP {threshold}: место #{rank}."
            )
        elif n_type == "daily_recap_top":
            rank = payload.get("rank_hint")
            text = f"🔥 Ты в топ-{rank} за вчера! Продолжай."
        elif n_type == "daily_recap_personal":
            votes = payload.get("votes_count", 0)
            avg = payload.get("avg_score", 0)
            text = f"Сводка за сутки: +{votes} голосов, средняя {avg:.2f}."
        elif n_type == "migration_notice":
            expires_at = payload.get("expires_at")
            text = (
                "🚀 Обновили GlowShot!\n"
                "Фото теперь участвует 2 дня (день загрузки + следующий).\n"
                f"Текущее фото в игре до: {expires_at}\n"
                "Оценивай других: 1 оценка = +1 credit = 2 показа (в 15–16 — 4)."
            )
        if text:
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    asyncio.create_task(notifications_worker(bot, send_fn=_send_notification))

    dp = Dispatcher()

    # Режим обновления: полный игнор для всех, кроме админов/модераторов/поддержки
    dp.update.middleware(UpdateModeMiddleware())

    # Глобальный тех.режим: доступ только админам/модераторам/поддержке
    dp.update.middleware(TechModeMiddleware())

    # Глобальный блок: заблокированным доступны только /help и удаление аккаунта через профиль
    dp.update.middleware(BlockGuardMiddleware())

    # Логирование активности для графиков
    dp.update.middleware(ActivityLogMiddleware())

    # Логирование ошибок в БД
    dp.update.middleware(ErrorsToDbMiddleware())

    # Роутеры
    dp.include_router(linklike.router)
    dp.include_router(author.router)
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
    dp.include_router(feedback.router)
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
