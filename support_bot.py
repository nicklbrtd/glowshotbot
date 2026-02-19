import asyncio
import traceback
from datetime import datetime
from typing import Dict, Callable, Any, Awaitable

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply,
    Update,
    TelegramObject,
)
from aiogram.fsm.context import FSMContext
from aiogram.dispatcher.event.bases import SkipHandler

from config import SUPPORT_BOT_TOKEN, SUPPORT_CHAT_ID
from database import (
    init_db,
    get_support_users_full,
    is_user_premium_active,
    get_user_by_tg_id,
    get_user_by_id,
    get_user_by_username,
    get_active_photos_for_user,
    get_photo_ratings_list,
    admin_delete_last_rating_for_photo,
    admin_clear_ratings_for_photo,
    ensure_user_minimal_row,
    get_user_premium_status,
    log_bot_error,
)
from handlers.admin import router as admin_router
from handlers import moderator
from html import escape

# tickets[(user_id, ticket_id)] = информация о тикете (сообщение в чате поддержки и текст пользователя)
tickets: Dict[tuple[int, int], dict] = {}

# pending_replies[agent_id] = (user_id, ticket_id), которому нужно ответить
pending_replies: Dict[int, tuple[int, int]] = {}

# pending_sections[user_id] = выбранный раздел, после которого ждём сообщение/вложение от пользователя
pending_sections: Dict[int, str] = {}
# support_menu_messages[user_id] = (chat_id, msg_id) — последнее отправленное меню поддержки
support_menu_messages: Dict[int, tuple[int, int]] = {}
# support_resolved_count[operator_id] = количество закрытых тикетов за сессию (в памяти)
support_resolved_count: Dict[int, int] = {}
# active dialogue maps
active_ticket_by_user: Dict[int, int] = {}        # user_id -> ticket_id
active_ticket_by_operator: Dict[int, int] = {}    # operator_id -> ticket_id
ticket_operator: Dict[int, int] = {}              # ticket_id -> operator_id
ticket_user: Dict[int, int] = {}                  # ticket_id -> user_id
ticket_support_msg: Dict[int, int] = {}           # ticket_id -> support card message_id in SUPPORT_CHAT_ID


def _extract_chat_and_user_from_update(update: Update) -> tuple[int | None, int | None]:
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
    Логирование ошибок support-бота в bot_error_logs (админ-логи).
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
            raise
        except Exception as e:
            tb = traceback.format_exc()
            chat_id = None
            tg_user_id = None
            update_type = type(event).__name__

            if isinstance(event, Update):
                chat_id, tg_user_id = _extract_chat_and_user_from_update(event)
            else:
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
                pass
            raise


async def main():
    await init_db()

    bot = Bot(
        SUPPORT_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.update.middleware(ErrorsToDbMiddleware())

    def find_ticket_key_by_id(ticket_id: int) -> tuple[int, int] | None:
        for (uid, tid), _info in tickets.items():
            if int(tid) == int(ticket_id):
                return (uid, tid)
        return None
    
    async def is_support_operator(user_id: int) -> bool:
        """
        Разрешаем /h и /hd в личке бота только саппортам.
        Берём из БД get_support_users_full().
        """
        try:
            su = await get_support_users_full()
        except Exception:
            su = []
        for u in su or []:
            if isinstance(u, dict):
                try:
                    if int(u.get("tg_id")) == int(user_id):
                        return True
                except Exception:
                    continue
            elif isinstance(u, int):
                if int(u) == int(user_id):
                    return True
        return False

    def build_start_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🐞 Баг / ошибка", callback_data="support_section:bug")],
                [InlineKeyboardButton(text="💎 Вопрос по Премиум", callback_data="support_section:premium")],
                [InlineKeyboardButton(text="🔐 Доступ к боту", callback_data="support_section:access")],
                [InlineKeyboardButton(text="🚫 Жалоба", callback_data="support_section:complaint")],
                [InlineKeyboardButton(text="📝 Другое", callback_data="support_section:other")],
            ]
        )

    def build_staff_start_menu(is_admin: bool, is_moderator: bool, is_support: bool = False) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if is_admin:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🛠 Админ-панель",
                        callback_data="admin:menu",
                    )
                ]
            )
        if is_moderator:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🛡 Модерация",
                        callback_data="mod:menu",
                    )
                ]
            )
        if is_support:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🎧 Панель поддержки",
                        callback_data="support:dashboard",
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="🆘 Вопрос в поддержку", callback_data="support:open")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _is_admin_or_moderator(tg_id: int) -> bool:
        try:
            u = await get_user_by_tg_id(int(tg_id))
        except Exception:
            u = None
        return bool(u and (u.get("is_admin") or u.get("is_moderator")))

    async def _get_counts_photos(user_id: int) -> list[dict]:
        try:
            photos = await get_active_photos_for_user(int(user_id), limit=2)
        except Exception:
            photos = []
        try:
            photos = sorted(
                photos,
                key=lambda p: (p.get("created_at") or "", p.get("id") or 0),
                reverse=True,
            )
        except Exception:
            pass
        return photos[:2]

    def _build_counts_kb(*, target_id: int, photo_id: int, index: int, total: int) -> InlineKeyboardMarkup:
        has_prev = index > 0
        has_next = index < (total - 1)
        prev_idx = index - 1 if has_prev else index
        next_idx = index + 1 if has_next else index
        prev_cb = f"support:counts:view:{target_id}:{prev_idx}"
        next_cb = f"support:counts:view:{target_id}:{next_idx}"
        current_cb = f"support:counts:view:{target_id}:{index}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️", callback_data=prev_cb),
                    InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data=current_cb),
                    InlineKeyboardButton(text="➡️", callback_data=next_cb),
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Удалить последнюю",
                        callback_data=f"support:counts:drop1:{photo_id}:{target_id}:{index}",
                    ),
                    InlineKeyboardButton(
                        text="🧹 Аннулировать",
                        callback_data=f"support:counts:clear:{photo_id}:{target_id}:{index}",
                    ),
                ],
                [InlineKeyboardButton(text="🗑 Закрыть", callback_data="support:counts:del")],
            ]
        )
        return kb

    async def _build_counts_payload(target: dict, index: int, notice: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
        photos = await _get_counts_photos(int(target["id"]))
        display_name = (target.get("name") or "").strip()
        if not display_name:
            username = (target.get("username") or "").strip()
            display_name = f"@{username}" if username else f"id:{int(target.get('tg_id') or 0)}"
        if not photos:
            text = f"У пользователя {escape(display_name)} нет активных фото."
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🗑 Закрыть", callback_data="support:counts:del")]]
            )
            return text, kb

        idx = min(max(int(index), 0), len(photos) - 1)
        photo = photos[idx]
        photo_id = int(photo["id"])
        ratings = await get_photo_ratings_list(photo_id)
        try:
            ratings = sorted(
                ratings,
                key=lambda r: (r.get("created_at") or ""),
                reverse=True,
            )
        except Exception:
            pass

        lines: list[str] = [f"Оценки пользователя {escape(display_name)}"]
        if notice:
            lines.append(f"ℹ️ {escape(notice)}")
        lines.append("")
        lines.append(f"Фото #{idx + 1}: <code>{photo_id}</code>")
        title = (photo.get("title") or "").strip()
        if title:
            lines.append(f"<code>\"{escape(title)}\"</code>")
        lines.append(f"Всего оценок: <b>{len(ratings)}</b>")
        lines.append("")
        if ratings:
            for r in ratings:
                u_username = (r.get("username") or "").strip()
                u_name = (r.get("name") or "").strip()
                u_tg = r.get("tg_id")
                if u_username:
                    label = f"@{u_username}"
                elif u_name:
                    label = u_name
                elif u_tg:
                    label = f"id:{u_tg}"
                else:
                    label = "unknown"
                lines.append(f"{escape(label)} - {int(r.get('value') or 0)}")
        else:
            lines.append("Оценок пока нет.")

        kb = _build_counts_kb(
            target_id=int(target["id"]),
            photo_id=photo_id,
            index=idx,
            total=len(photos),
        )
        return "\n".join(lines), kb

    async def _edit_or_send_counts(callback_or_message, text: str, kb: InlineKeyboardMarkup) -> None:
        if isinstance(callback_or_message, CallbackQuery):
            try:
                await callback_or_message.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
                return
            except Exception:
                try:
                    await callback_or_message.message.delete()
                except Exception:
                    pass
                await callback_or_message.message.answer(text, reply_markup=kb, parse_mode="HTML")
                return
        await callback_or_message.answer(text, reply_markup=kb, parse_mode="HTML")

    @dp.message(Command("counts"))
    async def counts_cmd(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        if not await _is_admin_or_moderator(int(message.from_user.id)):
            try:
                await message.delete()
            except Exception:
                pass
            return

        try:
            await message.delete()
        except Exception:
            pass

        args = (command.args or "").strip()
        if not args:
            text = "Укажи username: /counts @username"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Удалить", callback_data="support:counts:del")]]
            )
            await message.answer(text, reply_markup=kb)
            return

        uname = args.split()[0].strip().lstrip("@")
        if not uname:
            text = "Укажи корректный username: /counts @username"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Удалить", callback_data="support:counts:del")]]
            )
            await message.answer(text, reply_markup=kb)
            return

        target = await get_user_by_username(uname)
        if not target:
            text = "Пользователь не найден."
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🗑 Закрыть", callback_data="support:counts:del")]]
            )
            await message.answer(text, reply_markup=kb)
            return

        text, kb = await _build_counts_payload(target, index=0)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    @dp.callback_query(F.data.regexp(r"^support:counts:view:(\d+):(\d+)$"))
    async def counts_view(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        if not await _is_admin_or_moderator(int(callback.from_user.id)):
            await callback.answer()
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 5:
            await callback.answer()
            return
        try:
            target_id = int(parts[3])
            index = int(parts[4])
        except Exception:
            await callback.answer()
            return
        target = await get_user_by_id(target_id)
        if not target:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        text, kb = await _build_counts_payload(target, index=index)
        await _edit_or_send_counts(callback, text, kb)
        await callback.answer()

    @dp.callback_query(F.data.regexp(r"^support:counts:drop1:(\d+):(\d+):(\d+)$"))
    async def counts_drop_last(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        if not await _is_admin_or_moderator(int(callback.from_user.id)):
            await callback.answer()
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 6:
            await callback.answer()
            return
        try:
            photo_id = int(parts[3])
            target_id = int(parts[4])
            index = int(parts[5])
        except Exception:
            await callback.answer()
            return
        result = await admin_delete_last_rating_for_photo(photo_id)
        target = await get_user_by_id(target_id)
        if not target:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        notice = "Последняя оценка удалена." if result.get("deleted") else "Для этой фотографии оценок нет."
        text, kb = await _build_counts_payload(target, index=index, notice=notice)
        await _edit_or_send_counts(callback, text, kb)
        await callback.answer("Готово")

    @dp.callback_query(F.data.regexp(r"^support:counts:clear:(\d+):(\d+):(\d+)$"))
    async def counts_clear(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        if not await _is_admin_or_moderator(int(callback.from_user.id)):
            await callback.answer()
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 6:
            await callback.answer()
            return
        try:
            photo_id = int(parts[3])
            target_id = int(parts[4])
            index = int(parts[5])
        except Exception:
            await callback.answer()
            return
        result = await admin_clear_ratings_for_photo(photo_id)
        target = await get_user_by_id(target_id)
        if not target:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        removed = int(result.get("removed") or 0)
        notice = f"Аннулировано оценок: {removed}."
        text, kb = await _build_counts_payload(target, index=index, notice=notice)
        await _edit_or_send_counts(callback, text, kb)
        await callback.answer("Готово")

    @dp.callback_query(F.data == "support:counts:del")
    async def counts_delete(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        if not await _is_admin_or_moderator(int(callback.from_user.id)):
            try:
                await callback.answer()
            except Exception:
                pass
            return
        try:
            await callback.message.delete()
        except Exception:
            pass
        try:
            await callback.answer()
        except Exception:
            pass

    def _role_label(is_admin: bool, is_moderator: bool) -> str:
        if is_admin and is_moderator:
            return "админ · модератор"
        if is_admin:
            return "админ"
        if is_moderator:
            return "модератор"
        return "пользователь"

    def _support_greeting_text(user_id: int, premium_line: str | None = None) -> str:
        today = datetime.now().strftime("%d.%m.%Y")
        lines = [
            "🤖 <b>Поддержка GlowShot</b>",
            f"ID: <code>{user_id}</code>",
            f"Дата: {today}",
        ]
        if premium_line:
            lines.append(premium_line)
        lines.append("")
        lines.append("Напиши, что случилось — я передам запрос команде.")
        return "\n".join(lines)

    def _support_dashboard_text(user_id: int) -> str:
        resolved = support_resolved_count.get(int(user_id), 0)
        active_tid = active_ticket_by_operator.get(int(user_id))
        lines = [
            "🎧 <b>Панель поддержки</b>",
            f"ID: <code>{user_id}</code>",
            f"Решено тикетов (с момента запуска): <b>{resolved}</b>",
        ]
        if active_tid:
            lines.append(f"Активный тикет: #{active_tid}")
        else:
            lines.append("Активный тикет: нет")
        lines.append("")
        lines.append("Чтобы создать новый тикет как пользователь — нажми «Вопрос в поддержку».")
        return "\n".join(lines)

    def build_support_operator_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="support:stats")],
                [InlineKeyboardButton(text="🆘 Вопрос в поддержку", callback_data="support:open")],
            ]
        )

    def _remember_menu(user_id: int, chat_id: int, msg_id: int) -> None:
        support_menu_messages[int(user_id)] = (int(chat_id), int(msg_id))

    async def _delete_old_menu_if_any(bot: Bot, user_id: int):
        chat_msg = support_menu_messages.get(int(user_id))
        if not chat_msg:
            return
        chat_id, msg_id = chat_msg
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    async def _send_support_menu(bot: Bot, user_id: int):
        """Отправляет пользователю стартовое меню поддержки (удаляет старое)."""
        try:
            premium_line = await _premium_label(user_id)
        except Exception:
            premium_line = None
        await _delete_old_menu_if_any(bot, user_id)
        try:
            sent = await bot.send_message(
                chat_id=int(user_id),
                text=_support_greeting_text(int(user_id), premium_line),
                reply_markup=build_start_menu(),
            )
            _remember_menu(user_id, sent.chat.id, sent.message_id)
        except Exception:
            pass

    async def _load_user_and_roles(tg_id: int, username: str | None) -> tuple[dict | None, bool, bool]:
        """Возвращает (user, is_admin, is_moderator), создавая минимальную запись при отсутствии."""
        user = await get_user_by_tg_id(int(tg_id))
        if user is None:
            try:
                user = await ensure_user_minimal_row(int(tg_id), username=username)
            except Exception:
                user = None
        is_admin = bool(user and user.get("is_admin"))
        is_moderator = bool(user and user.get("is_moderator"))
        return user, is_admin, is_moderator

    def section_label(code: str) -> str:
        mapping = {
            "bug": "Баг / ошибка",
            "premium": "Вопрос по Премиум",
            "access": "Доступ к боту",
            "complaint": "Жалоба",
            "other": "Другое",
        }
        return mapping.get(code, code)

    async def _premium_label(tg_id: int) -> str:
        try:
            status = await get_user_premium_status(int(tg_id)) or {}
            is_flag = bool(status.get("is_premium"))
            until_raw = status.get("premium_until")

            active = False
            try:
                active = await is_user_premium_active(int(tg_id))
            except Exception:
                active = False

            # Дополнительная проверка по дате, если флаг не обновился
            if not active and until_raw:
                try:
                    active = datetime.fromisoformat(str(until_raw)) > datetime.now()
                except Exception:
                    pass

            if active:
                return f"💎 Премиум: активен{f' (до {until_raw})' if until_raw else ''}"
            if is_flag or until_raw:
                return f"💤 Премиум: истёк{f' ({until_raw})' if until_raw else ''}"
            return "💤 Премиум: нет"
        except Exception:
            return "💤 Премиум: нет"

    @dp.message(CommandStart())
    async def start_menu(message: Message):
        # В чате поддержки /start не нужен
        if message.chat.id == SUPPORT_CHAT_ID:
            return

        try:
            await message.delete()
        except Exception:
            pass

        user, is_admin, is_moderator = await _load_user_and_roles(
            message.from_user.id, getattr(message.from_user, "username", None)
        )
        is_support = await is_support_operator(message.from_user.id)

        if is_admin or is_moderator:
            roles: list[str] = []
            if is_admin:
                roles.append("админ")
            if is_moderator:
                roles.append("модератор")
            if is_support and "саппорт" not in roles:
                roles.append("саппорт")

            roles_line = ", ".join(roles) if roles else "команда"
            text = (
                "👋 Привет! Это служебное меню поддержки.\n"
                f"Твоя роль: {roles_line}.\n\n"
                "Админ- и модераторская панели доступны прямо здесь, без перехода в основной бот.\n"
                "Если нужен привычный саппорт — жми «Вопрос в поддержку» и выбери раздел, как обычный пользователь."
            )
            sent = await message.answer(text, reply_markup=build_staff_start_menu(is_admin, is_moderator, is_support=is_support))
            _remember_menu(message.from_user.id, sent.chat.id, sent.message_id)
            return

        if is_support:
            sent = await message.answer(_support_dashboard_text(message.from_user.id), reply_markup=build_support_operator_menu())
            _remember_menu(message.from_user.id, sent.chat.id, sent.message_id)
            return

        sent = await message.answer(_support_greeting_text(message.from_user.id, await _premium_label(message.from_user.id)), reply_markup=build_start_menu())
        _remember_menu(message.from_user.id, sent.chat.id, sent.message_id)

    @dp.message(Command("admin"))
    async def admin_cmd_disabled(message: Message):
        # В саппорт-боте вход в админку только через /start и кнопку
        if message.chat.id == SUPPORT_CHAT_ID:
            return
        user, is_admin, is_moderator = await _load_user_and_roles(
            message.from_user.id, getattr(message.from_user, "username", None)
        )
        is_support = await is_support_operator(message.from_user.id)
        roles = []
        if is_admin:
            roles.append("админ")
        if is_moderator:
            roles.append("модератор")
        if is_support:
            roles.append("саппорт")

        roles_line = ", ".join(roles) if roles else "без роли"
        text = (
            "Админ-панель доступна из меню.\n"
            f"Твои роли: {roles_line}.\n"
            "Нажми «Админ-панель» или «Модерация» в стартовом меню."
        )
        kb = None
        if is_admin or is_moderator or is_support:
            kb = build_staff_start_menu(is_admin, is_moderator, is_support=is_support)
        else:
            kb = build_start_menu()
        await message.answer(text, reply_markup=kb)

    @dp.message(F.chat.id != SUPPORT_CHAT_ID, F.text.regexp(r"^/"))
    async def only_start_allowed(message: Message, state: FSMContext):
        # Разрешаем только /start в саппорт-боте (в личке)
        try:
            if await state.get_state():
                raise SkipHandler()
        except SkipHandler:
            raise
        except Exception:
            pass
        if (message.text or "").startswith("/start"):
            return

        _, is_admin, is_moderator = await _load_user_and_roles(
            message.from_user.id, getattr(message.from_user, "username", None)
        )
        is_support = await is_support_operator(message.from_user.id)

        roles = []
        if is_admin:
            roles.append("админ")
        if is_moderator:
            roles.append("модератор")
        if is_support:
            roles.append("саппорт")
        roles_line = ", ".join(roles) if roles else "без роли"

        text = (
            "Здесь доступна только команда /start.\n"
            f"Твои роли: {roles_line}.\n"
            "Нажми /start, чтобы открыть нужное меню."
        )

        kb = build_start_menu()
        if is_admin or is_moderator or is_support:
            kb = build_staff_start_menu(is_admin, is_moderator, is_support=is_support)

        await message.answer(text, reply_markup=kb)

    @dp.callback_query(F.data == "support:open")
    async def support_open_callback(callback: CallbackQuery):
        if callback.message and callback.message.chat.id == SUPPORT_CHAT_ID:
            await callback.answer("Работает только в личке.", show_alert=True)
            return

        pending_sections.pop(callback.from_user.id, None)
        try:
            await callback.message.edit_text(
                _support_greeting_text(callback.from_user.id, await _premium_label(callback.from_user.id)),
                reply_markup=build_start_menu(),
            )
        except Exception:
            await callback.message.answer(
                _support_greeting_text(callback.from_user.id, await _premium_label(callback.from_user.id)),
                reply_markup=build_start_menu(),
            )
        await callback.answer()

    @dp.callback_query(F.data.in_(("support:dashboard", "support:stats")))
    async def support_dashboard_callback(callback: CallbackQuery):
        if callback.message and callback.message.chat.id == SUPPORT_CHAT_ID:
            await callback.answer()
            return

        if not await is_support_operator(callback.from_user.id):
            await callback.answer("Только для команды поддержки.", show_alert=True)
            return

        try:
            await callback.message.edit_text(
                _support_dashboard_text(callback.from_user.id),
                reply_markup=build_support_operator_menu(),
            )
        except Exception:
            await callback.message.answer(
                _support_dashboard_text(callback.from_user.id),
                reply_markup=build_support_operator_menu(),
            )
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "menu:back")
    async def support_back_to_menu(callback: CallbackQuery):
        """Обработчик «в меню» для админки/модераторки внутри саппорт-бота."""
        if callback.message and callback.message.chat.id == SUPPORT_CHAT_ID:
            await callback.answer()
            return

        _, is_admin, is_moderator = await _load_user_and_roles(
            callback.from_user.id, getattr(callback.from_user, "username", None)
        )
        is_support = await is_support_operator(callback.from_user.id)
        target_text = _support_greeting_text(callback.from_user.id, await _premium_label(callback.from_user.id))
        target_kb = build_start_menu()
        if is_admin or is_moderator:
            roles = []
            if is_admin:
                roles.append("админ")
            if is_moderator:
                roles.append("модератор")
            if is_support and "саппорт" not in roles:
                roles.append("саппорт")
            roles_line = ", ".join(roles) if roles else "команда"
            target_text = (
                "👋 Привет! Это служебное меню поддержки.\n"
                f"Твоя роль: {roles_line}.\n\n"
                "Админ- и модераторская панели доступны прямо здесь, без перехода в основной бот.\n"
                "Если нужен привычный саппорт — жми «Вопрос в поддержку» и выбери раздел, как обычный пользователь."
            )
            target_kb = build_staff_start_menu(is_admin, is_moderator, is_support=is_support)
        else:
            if is_support:
                target_text = _support_dashboard_text(callback.from_user.id)
                target_kb = build_support_operator_menu()

        try:
            await callback.message.edit_text(target_text, reply_markup=target_kb)
        except Exception:
            await callback.message.answer(target_text, reply_markup=target_kb)
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("support_section:"))
    async def support_section_callback(callback: CallbackQuery):
        # Раздел выбирает только пользователь (не в SUPPORT_CHAT_ID)
        if callback.message and callback.message.chat.id == SUPPORT_CHAT_ID:
            await callback.answer("Эта кнопка не для чата поддержки.", show_alert=True)
            return

        try:
            _, code = callback.data.split(":", 1)
        except Exception:
            await callback.answer("Ошибка данных.", show_alert=True)
            return

        pending_sections[callback.from_user.id] = code

        await callback.message.answer(
            "Супер! опишите поподробнее, что у вас случилось, я отправлю ваш запрос админам и вам ответят в ближайшее время."
        )
        await callback.answer("Ок ✅")

    @dp.message(Command("chatid"))
    async def get_chat_id(message: Message):
        """
        Вспомогательная команда для получения ID текущего чата.
        Используй её в чате поддержки, чтобы узнать SUPPORT_CHAT_ID.
        """
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")

    @dp.message(F.chat.id == SUPPORT_CHAT_ID, Command("resolve"))
    async def resolve_ticket_manual(message: Message):
        """
        Ручная команда в чате поддержки: /resolve <ticket_id>
        Помечает тикет как решенный и обновляет сообщение.
        """
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /resolve <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return

        # Ищем любой тикет с таким номером
        key = None
        for (uid, tid), info in tickets.items():
            if tid == ticket_id:
                key = (uid, tid)
                break

        if key is None:
            await message.answer("Тикет с таким номером не найден (возможно, бот перезапускался).")
            return

        ticket = tickets[key]
        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"

        try:
            await message.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=support_msg_id,
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Раздел: <b>{ticket.get('section') or '—'}</b>\n\n"
                    f"Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            await message.answer("Не удалось обновить сообщение тикета, но статус помечен локально.")

        ticket["status"] = "resolved"
        op_id = ticket_operator.get(ticket_id)
        try:
            if op_id != message.from_user.id:
                support_resolved_count[message.from_user.id] = support_resolved_count.get(message.from_user.id, 0) + 1
        except Exception:
            pass
        await _send_support_menu(message.bot, message.from_user.id)
        await message.answer(f"Тикет #{ticket_id} помечен как решенный ✅")


    @dp.message(Command("h"))
    async def take_ticket_manual(message: Message):
        """Взять тикет в работу: /h <ticket_id>. Включает режим ответа для оператора."""
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /h <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return
        
        # /h в личке разрешаем только операторам
        if message.chat.id != SUPPORT_CHAT_ID:
            if not await is_support_operator(message.from_user.id):
                await message.answer("Команда доступна только операторам поддержки.")
                return

        key = find_ticket_key_by_id(ticket_id)
        if key is None:
            await message.answer("Тикет с таким номером не найден (возможно, бот перезапускался).")
            return

        user_id, tid = key

        # 1) быстрые ответы из группы: старый режим pending_replies
        if message.chat.id == SUPPORT_CHAT_ID:
            pending_replies[message.from_user.id] = (user_id, tid)

        # 2) диалог в личке бота
        active_ticket_by_operator[message.from_user.id] = tid
        active_ticket_by_user[user_id] = tid
        ticket_operator[tid] = message.from_user.id
        ticket_user[tid] = user_id
        try:
            t = tickets.get((user_id, tid))
            if t and t.get("support_msg_id"):
                ticket_support_msg[tid] = int(t.get("support_msg_id"))
        except Exception:
            pass

        # уведомим пользователя, что оператор подключился
        try:
            await message.bot.send_message(
                chat_id=user_id,
                text=(
                    "👤 <b>Оператор подключился!</b>\n"
                    f"Тикет #{ticket_id}\n\n"
                    "Напишите уточнения или дождитесь ответа — я передам оператору."
                ),
            )
        except Exception:
            pass

        if message.chat.id == SUPPORT_CHAT_ID:
            await message.answer(
                f"Ок, взял тикет #{ticket_id}. Для диалога зайди в личку бота и пиши там ✅\nЗакрыть: /hd {ticket_id}",
                reply_markup=ForceReply(selective=True),
            )
        else:
            await message.answer(
                f"Ок, взял тикет #{ticket_id}. Теперь просто пиши сюда — я буду пересылать пользователю.\nЗакрыть: /hd {ticket_id}",
            )


    @dp.message(Command("hd"))
    async def close_ticket_manual(message: Message):
        """Закрыть тикет вручную: /hd <ticket_id> (аналог /resolve)."""
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /hd <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return
        
        if message.chat.id != SUPPORT_CHAT_ID:
            if not await is_support_operator(message.from_user.id):
                await message.answer("Команда доступна только операторам поддержки.")
                return

        key = find_ticket_key_by_id(ticket_id)
        if key is None:
            await message.answer("Тикет с таким номером не найден (возможно, бот перезапускался).")
            return

        ticket = tickets.get(key)
        if not ticket:
            await message.answer("Тикет не найден (возможно, бот перезапускался).")
            return

        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"

        try:
            await message.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=support_msg_id,
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Раздел: <b>{ticket.get('section') or '—'}</b>\n\n"
                    f"Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            pass

        ticket["status"] = "resolved"
        # чистим карты диалога
        op_id = None
        try:
            op_id = ticket_operator.get(ticket_id)
            uid = ticket_user.get(ticket_id)
            if op_id:
                active_ticket_by_operator.pop(op_id, None)
                support_resolved_count[op_id] = support_resolved_count.get(op_id, 0) + 1
            if uid:
                active_ticket_by_user.pop(uid, None)
            ticket_operator.pop(ticket_id, None)
            ticket_user.pop(ticket_id, None)
        except Exception:
            pass

        # если оператор не был зафиксирован в картах, считаем закрывшего
        try:
            support_resolved_count[message.from_user.id] = support_resolved_count.get(message.from_user.id, 0) + 1
        except Exception:
            pass

        await _send_support_menu(message.bot, message.from_user.id)

        # Если кто-то держит режим ответа на этот тикет — снимем
        try:
            to_del = [aid for aid, v in pending_replies.items() if v == key]
            for aid in to_del:
                pending_replies.pop(aid, None)
        except Exception:
            pass

        # уведомим пользователя
        try:
            await message.bot.send_message(
                chat_id=int(ticket.get("user_id") or key[0]),
                text=(
                    "✅ <b>Вопрос закрыт поддержкой</b>\n"
                    f"Тикет #{ticket_id}\n\n"
                    "Если проблема появится снова — просто напишите /start и создайте новый тикет."
                ),
            )
        except Exception:
            pass

        try:
            support_msg_id2 = ticket_support_msg.get(ticket_id)
            if support_msg_id2:
                await message.bot.edit_message_reply_markup(
                    chat_id=SUPPORT_CHAT_ID,
                    message_id=int(support_msg_id2),
                    reply_markup=None,
                )
        except Exception:
            pass
        await message.answer(f"Тикет #{ticket_id} закрыт ✅")

    # --- Обработка сообщений из чата поддержки (SUPPORT_CHAT_ID) ---

    @dp.message(F.chat.id == SUPPORT_CHAT_ID)
    async def handle_support_chat_message(message: Message):
        """
        Сообщения в чате поддержки.
        Если у отправителя (сотрудника поддержки) есть незакрытый pending_replies,
        считаем это ответом пользователю.
        """
        agent_id = message.from_user.id

        # Если нет активного "режима ответа" для этого агента — игнорим
        if agent_id not in pending_replies:
            return

        user_id, ticket_id = pending_replies.pop(agent_id)

        if not message.text:
            await message.answer("Отправь, пожалуйста, ответ текстом, я перешлю его пользователю.")
            return

        # Кнопки для оценки ответа
        feedback_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Вопрос решен",
                        callback_data=f"ticket_feedback:{user_id}:{ticket_id}:resolved",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⚠️ Вопрос не решен",
                        callback_data=f"ticket_feedback:{user_id}:{ticket_id}:unresolved",
                    )
                ],
            ]
        )

        section = "—"
        try:
            section = (tickets.get((user_id, ticket_id)) or {}).get("section") or "—"
        except Exception:
            section = "—"

        await message.bot.send_message(
            chat_id=user_id,
            text=(
                "💬 <b>Ответ от поддержки!</b>\n"
                f"Тикет #{ticket_id}\n"
                f"Раздел: <b>{section}</b>\n\n"
                "Сообщение:\n"
                f"{message.text}"
            ),
            reply_markup=feedback_kb,
        )

        # Пишем в чат поддержки, что всё ок
        await message.answer(f"Ответ на тикет #{ticket_id} отправлен ✅")


    @dp.message()
    async def handle_support(message: Message, state: FSMContext):
        """ 
        Сообщения НЕ из SUPPORT_CHAT_ID:
        1) /start показывает меню разделов.
        2) После выбора раздела следующее сообщение/вложение создаёт тикет.
        """
        if message.chat.id == SUPPORT_CHAT_ID:
            # для чата поддержки есть отдельный хендлер выше
            return

        # Если есть активное состояние (админка/модерация и т.п.) — не перехватываем сообщение
        try:
            if await state.get_state():
                raise SkipHandler()
        except SkipHandler:
            raise
        except Exception:
            pass

        user = message.from_user
        user_db, is_admin, is_moderator = await _load_user_and_roles(
            user.id, getattr(user, "username", None)
        )
        premium_line = await _premium_label(user.id)

        # Если это оператор в личке бота и он ведёт тикет — пересылаем пользователю
        if user.id in active_ticket_by_operator:
            tid = active_ticket_by_operator[user.id]
            uid = ticket_user.get(tid)
            if not uid:
                await message.answer("Тикет не найден или уже закрыт.")
                active_ticket_by_operator.pop(user.id, None)
                return

            try:
                header = "💬 <b>Сообщение от оператора</b>\n" f"Тикет #{tid}\n\n"
                if message.text:
                    await message.bot.send_message(uid, header + message.text)
                else:
                    await message.bot.send_message(uid, header + "📎 Вложение")
                    await message.forward(uid)
            except Exception:
                pass

            await message.answer(f"Отправлено пользователю по тикету #{tid} ✅")
            return
        # Если по пользователю уже идёт диалог по активному тикету — пересылаем оператору
        if user.id in active_ticket_by_user:
            tid = active_ticket_by_user[user.id]
            op_id = ticket_operator.get(tid)

            if op_id:
                try:
                    prefix = "🗣️ <b>Сообщение от пользователя</b>\n" f"Тикет #{tid}\n\n"
                    if message.text:
                        await message.bot.send_message(op_id, prefix + message.text)
                    else:
                        await message.bot.send_message(op_id, prefix + "📎 Вложение")
                        await message.forward(op_id)
                except Exception:
                    pass

            await message.answer("✅ Сообщение передано оператору по вашему тикету.")
            return

        # Если пользователь ещё не выбрал раздел — показываем меню и удаляем его сообщение
        if user.id not in pending_sections:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(_support_greeting_text(user.id, premium_line), reply_markup=build_start_menu())
            return

        section_code = pending_sections.pop(user.id)
        section = section_label(section_code)

        ticket_id = message.message_id  # используем message_id как номер тикета

        premium_line = await _premium_label(user.id)
        try:
            premium_active = await is_user_premium_active(int(user.id))
        except Exception:
            premium_active = False
        status_label = "💎 приоритет (премиум)" if premium_active else _role_label(is_admin, is_moderator)

        header = (
            "🆘 <b>Новый запрос!</b>\n\n"
            f"Тикет: #{ticket_id}\n"
            f"ID пользователя: <code>{user.id}</code>\n"
            f"Username: @{user.username if user.username else '—'}\n"
            f"{premium_line}\n"
            f"Статус: {status_label}\n"
            f"Раздел: <b>{section}</b>\n\n"
            "Сообщение пользователя:\n"
        )

        # Админские кнопки: Ответить / Завершить
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✉️ Ответить",
                        callback_data=f"support_reply:{user.id}:{ticket_id}",
                    ),
                    InlineKeyboardButton(
                        text="✅ Завершить",
                        callback_data=f"support_close:{user.id}:{ticket_id}",
                    ),
                ]
            ]
        )

        # 1) Шлём заголовок в чат поддержки
        # Текст выводим здесь, а вложения — отдельным forward'ом ниже
        if message.text:
            body = header + message.text + "\n\n" + f"<b>Действия:</b> взять — /h {ticket_id} · закрыть — /hd {ticket_id}\n"
        else:
            body = header + "📎 Вложение\n\n" + f"<b>Действия:</b> взять — /h {ticket_id} · закрыть — /hd {ticket_id}\n"

        sent = await message.bot.send_message(
            chat_id=SUPPORT_CHAT_ID,
            text=body,
            reply_markup=admin_kb,
        )

        # карты для последующего диалога/закрытия
        ticket_user[ticket_id] = user.id
        ticket_support_msg[ticket_id] = sent.message_id

        # 1.1) Если есть вложения — форвардим оригинал (чтобы были фото/файл и т.д.)
        # Для чистого текста не форвардим, чтобы не дублировать.
        if not message.text:
            await message.forward(SUPPORT_CHAT_ID)

        # Сохраняем информацию о тикете в памяти
        tickets[(user.id, ticket_id)] = {
            "support_msg_id": sent.message_id,
            "user_id": user.id,
            "text": message.text or "📎 Вложение",
            "status": "open",
            "section": section,
            "priority": "premium" if premium_active else "normal",
            "is_premium": premium_active,
            "is_admin": is_admin,
            "is_moderator": is_moderator,
        }

        # 2) отвечаем юзеру
        await message.answer(
            f"Отлично! ваша заявка #{ticket_id} создана!\nОжидайте ответа от поддержки."
        )

    @dp.callback_query(F.data.startswith("ticket_feedback:"))
    async def ticket_feedback_callback(callback: CallbackQuery):
        """
        Обработка нажатий пользователя: вопрос решен / не решен.
        """
        try:
            _, user_id_str, ticket_id_str, status = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных.", show_alert=True)
            return

        key = (user_id, ticket_id)
        ticket = tickets.get(key)

        # В любом случае убираем клавиатуру, чтобы нельзя было тыкать бесконечно
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        if status == "resolved":
            if not ticket:
                await callback.answer("Тикет не найден (возможно, бот перезапускался).", show_alert=True)
                return

            support_msg_id = ticket.get("support_msg_id")
            original_text = ticket.get("text") or "—"

            # Обновляем сообщение в чате поддержки
            try:
                await callback.bot.edit_message_text(
                    chat_id=SUPPORT_CHAT_ID,
                    message_id=support_msg_id,
                    text=(
                        f"✅ Вопрос #{ticket_id} решен\n\n"
                        f"Раздел: <b>{ticket.get('section') or '—'}</b>\n\n"
                        f"Сообщение:\n"
                        f"{original_text}"
                    ),
                )
            except Exception:
                # Если не удалось отредактировать, просто игнорим
                pass

            ticket["status"] = "resolved"
            await callback.answer("Спасибо, отметили вопрос как решенный ✅", show_alert=False)
            # отправим пользователю меню поддержки
            await _send_support_menu(callback.bot, callback.from_user.id)
            return

        # статус "не решен" — эскалируем живому оператору поддержки
        # get_support_users() в проекте может возвращать:
        #  - list[int] (tg_id операторов)
        #  - list[dict] (с полями tg_id/username и т.п.)
        try:
            support_users = await get_support_users_full()
        except Exception:
            support_users = []

        # Нормализуем кандидатов -> список словарей {tg_id:int, username:str|None}
        candidates: list[dict] = []
        for u in (support_users or [])[:200]:
            if isinstance(u, int):
                candidates.append({"tg_id": u, "username": None})
                continue
            if isinstance(u, dict):
                tg_id = u.get("tg_id") or u.get("id") or u.get("user_id")
                if tg_id is None:
                    continue
                try:
                    tg_id = int(tg_id)
                except Exception:
                    continue
                candidates.append({"tg_id": tg_id, "username": u.get("username")})

        # Пытаемся написать первому оператору в личку
        operator_tg_id: int | None = None
        operator_username: str | None = None
        for c in candidates:
            tg_id = c.get("tg_id")
            if not tg_id:
                continue
            # не пингуем самого пользователя
            if tg_id == callback.from_user.id:
                continue
            try:
                await callback.bot.send_message(
                    chat_id=int(tg_id),
                    text=(
                        "🆘 <b>Нужна помощь: вопрос не решён</b>\n\n"
                        f"Тикет: #{ticket_id}\n"
                        f"Пользователь ID: <code>{user_id}</code>\n"
                        f"Username: @{callback.from_user.username if callback.from_user.username else '—'}\n"
                        f"Раздел: <b>{(ticket or {}).get('section') or '—'}</b>\n\n"
                        "Пожалуйста, зайдите в чат поддержки и ответьте пользователю по тикету."
                    ),
                )
                operator_tg_id = int(tg_id)
                operator_username = c.get("username")
                break
            except Exception:
                continue

        if operator_tg_id:
            # Пользователю: подтверждаем эскалацию
            if operator_username:
                text = (
                    f"— Вопрос #{ticket_id} не решен.\n"
                    f"Я передал ваш запрос оператору @{operator_username}.\n"
                    f"Ожидайте ответа от поддержки."
                )
            else:
                text = (
                    f"— Вопрос #{ticket_id} не решен.\n"
                    f"Я передал ваш запрос оператору поддержки.\n"
                    f"Ожидайте ответа от поддержки."
                )
        else:
            # Если не получилось никому написать в личку (часто из‑за ограничений Telegram),
            # то эскалируем в общий чат поддержки (SUPPORT_CHAT_ID), где бот писать может всегда.
            mentions = []
            for c in candidates[:10]:
                tg_id = c.get("tg_id")
                uname = c.get("username")
                if uname:
                    mentions.append(f"@{uname}")
                elif tg_id:
                    # упоминание по tg_id работает в группах/супергруппах
                    mentions.append(f'<a href="tg://user?id={int(tg_id)}">оператор</a>')

            ping_line = " ".join(mentions) if mentions else "(операторы не найдены)"

            try:
                await callback.bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    text=(
                        "⚠️ <b>Эскалация: вопрос не решён</b>\n\n"
                        f"Тикет: #{ticket_id}\n"
                        f"Пользователь ID: <code>{user_id}</code>\n"
                        f"Username: @{callback.from_user.username if callback.from_user.username else '—'}\n"
                        f"Раздел: <b>{(ticket or {}).get('section') or '—'}</b>\n\n"
                        "Пользователь отметил, что ответ не решил проблему. Нужен оператор.\n\n"
                        f"Пинг: {ping_line}\n\n"
                        f"<b>Действия:</b> возьмите тикет в личке бота: /h {ticket_id} · закрыть: /hd {ticket_id}"
                    ),
                )
            except Exception:
                pass

            text = (
                f"— Вопрос #{ticket_id} не решен.\n"
                f"Я передал запрос в общий чат поддержки, оператор подключится.\n"
                f"Ожидайте ответа от поддержки."
            )

        await callback.message.answer(text)
        await callback.answer("Сообщил, что вопрос не решен ⚠️", show_alert=False)

    # --- Callback на кнопку "Ответить" в чате поддержки ---

    @dp.callback_query(F.data.startswith("support_reply:"))
    async def support_reply_callback(callback: CallbackQuery):
        """
        Ставит для агента "режим ответа": следующее его сообщение в чате поддержки
        будет отправлено пользователю.
        """
        try:
            _, user_id_str, ticket_id_str = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных", show_alert=True)
            return

        agent_id = callback.from_user.id
        pending_replies[agent_id] = (user_id, ticket_id)

        await callback.message.answer(
            (
                f"Отвечаем пользователю по тикету #{ticket_id}.\n"
            ),
            reply_markup=ForceReply(selective=True),
        )

        await callback.answer("Режим ответа включен ✅")

    @dp.callback_query(F.data.startswith("support_close:"))
    async def support_close_callback(callback: CallbackQuery):
        """Закрыть тикет кнопкой из чата поддержки (аналог /resolve)."""
        try:
            _, user_id_str, ticket_id_str = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных", show_alert=True)
            return

        key = (user_id, ticket_id)
        ticket = tickets.get(key)
        if not ticket:
            await callback.answer("Тикет не найден (возможно, бот перезапускался).", show_alert=True)
            return

        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"
        section = ticket.get("section") or "—"

        try:
            await callback.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=support_msg_id,
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Раздел: <b>{section}</b>\n\n"
                    "Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            pass

        ticket["status"] = "resolved"
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        try:
            support_resolved_count[callback.from_user.id] = support_resolved_count.get(callback.from_user.id, 0) + 1
        except Exception:
            pass

        await _send_support_menu(callback.bot, callback.from_user.id)

        await callback.answer("Тикет закрыт ✅")

    # Подключаем панели админа и модератора в бота поддержки
    dp.include_router(admin_router)
    dp.include_router(moderator.router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
