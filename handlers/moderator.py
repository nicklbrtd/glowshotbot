from __future__ import annotations

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from datetime import timedelta, datetime
from utils.time import get_moscow_now, format_party_id
from html import escape
import io

from database import (
    get_user_by_tg_id,
    is_moderator_by_tg_id,
    get_photo_by_id,
    mark_photo_deleted,
    set_photo_moderation_status,
    get_next_photo_for_moderation,
    get_user_by_id,
    get_photo_stats,
    get_photo_report_stats,
    add_moderator_review,
    get_next_photo_for_self_moderation,
    get_next_photo_for_detailed_moderation,
    get_moderation_message_for_photo,
    delete_moderation_message_for_photo,
    get_photo_ids_for_user,
    set_user_block_status_by_tg_id,
    get_user_by_username,
    hide_active_photos_for_user,
    restore_photos_from_status,
    set_photo_file_id_support,
    get_moderation_author_metrics,
)
from config import BOT_TOKEN, SUPPORT_BOT_TOKEN
from utils.moderation import (
    REPORT_REASON_LABELS,
    MODERATION_REASON_TEXTS,
    get_report_reasons,
    ReportReason,
)

# Роутер раздела модерации
router = Router()

_main_media_bot: Bot | None = None


def _is_support_bot(bot: Bot) -> bool:
    try:
        return bool(SUPPORT_BOT_TOKEN) and getattr(bot, "token", None) == SUPPORT_BOT_TOKEN
    except Exception:
        return False


async def _get_main_media_bot() -> Bot | None:
    global _main_media_bot
    if not BOT_TOKEN:
        return None
    if _main_media_bot is None:
        _main_media_bot = Bot(BOT_TOKEN)
    return _main_media_bot


async def _download_photo_bytes_from_main_bot(file_id: str) -> bytes | None:
    main_bot = await _get_main_media_bot()
    if main_bot is None:
        return None
    try:
        tg_file = await main_bot.get_file(file_id)
        buff = io.BytesIO()
        await main_bot.download_file(tg_file.file_path, destination=buff)
        return buff.getvalue()
    except Exception:
        return None


async def _send_photo_with_fallback(
    *,
    bot: Bot,
    chat_id: int,
    file_id: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
    parse_mode: str = "HTML",
) -> tuple[bool, str | None]:
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_notification=True,
        )
        return True, None
    except Exception:
        pass

    if _is_support_bot(bot):
        data = await _download_photo_bytes_from_main_bot(file_id)
        if data:
            try:
                sent = await bot.send_photo(
                    chat_id=chat_id,
                    photo=BufferedInputFile(data, filename="photo.jpg"),
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_notification=True,
                )
                support_file_id = sent.photo[-1].file_id if sent and sent.photo else None
                return True, support_file_id
            except Exception:
                pass

    return False, None


class ModeratorStates(StatesGroup):
    """Состояния FSM для модератора."""
    # Ввод причины удаления/бана
    waiting_ban_reason = State()
    # Поиск пользователя
    waiting_user_search_query = State()
    # Поиск пользователя для бана / разбана
    waiting_user_block_query = State()
    waiting_fullban_days = State()
    waiting_fullban_reason = State()


def build_moderator_menu() -> InlineKeyboardMarkup:
    """
    Клавиатура раздела модерации.

    Здесь:
    - самостоятельная проверка (любой активный контент),
    - раздел пользователей,
    - выход обратно в главное меню.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🧾 Проверять самостоятельно", callback_data="mod:self")
    kb.button(text="🚨 Очередь жалоб", callback_data="mod:queue")
    kb.button(text="🔍 Детальная проверка", callback_data="mod:deep")
    kb.button(text="👥 Пользователи", callback_data="mod:users")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()


def build_moderator_users_menu() -> InlineKeyboardMarkup:
    """
    Подменю работы с пользователями.

    Здесь:
    - поиск пользователя;
    - блок / разбан по ID или username (интерфейс);
    - список заблокированных (интерфейсная заглушка, пока не реализована в БД).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти пользователя", callback_data="mod:users_search")
    kb.button(text="🚫 Блок / разбан", callback_data="mod:users_block")
    kb.button(text="🧾 Список заблокированных", callback_data="mod:users_blocked")
    kb.button(text="⬅️ Назад", callback_data="mod:menu")
    kb.adjust(1)
    return kb.as_markup()


def build_fullban_days_keyboard(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="1 день", callback_data=f"mod:report_block_days:{photo_id}:1")
    kb.button(text="3 дня", callback_data=f"mod:report_block_days:{photo_id}:3")
    kb.button(text="7 дней", callback_data=f"mod:report_block_days:{photo_id}:7")
    kb.button(text="30 дней", callback_data=f"mod:report_block_days:{photo_id}:30")
    kb.button(text="⬅️ Назад", callback_data=f"mod:report_block_back:{photo_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def build_moderation_photo_keyboard(photo_id: int, source: str) -> InlineKeyboardMarkup:
    """
    Клавиатура для карточки модерации конкретной фотографии.

    source:
      - "queue"  — фото из очереди жалоб;
      - "self"   — фото из самостоятельной проверки;
      - "deep"   — фото из детальной проверки.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё ок", callback_data=f"mod:photo_ok:{source}:{photo_id}")
    kb.button(text="🗑 Удалить", callback_data=f"mod:photo_delete:{source}:{photo_id}")
    kb.button(text="⛔ Удалить + бан", callback_data=f"mod:photo_delete_ban:{source}:{photo_id}")
    kb.button(text="👤 Автор", callback_data=f"mod:photo_profile:{source}:{photo_id}")
    kb.button(text="⏭ Следующее", callback_data=f"mod:next:{source}")
    kb.button(text="⬅️ Меню", callback_data="mod:menu")
    kb.adjust(1)
    return kb.as_markup()


def _format_block_until(until_val) -> str:
    if not until_val:
        return "—"
    try:
        dt = datetime.fromisoformat(str(until_val))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(until_val)


def _get_status_target_id(user: dict) -> int | None:
    """
    Предпочитаем tg_id (его требует set_user_block_status_by_tg_id), иначе — внутренний id.
    """
    try:
        if user.get("tg_id"):
            return int(user["tg_id"])
    except Exception:
        pass
    try:
        if user.get("id"):
            return int(user["id"])
    except Exception:
        pass
    return None


async def _load_user_by_numeric_id(num_id: int) -> dict | None:
    """Пробуем загрузить пользователя по tg_id, если нет — по внутреннему id."""
    user = await get_user_by_tg_id(num_id)
    if user is not None:
        return user
    try:
        return await get_user_by_id(num_id)
    except Exception:
        return None


async def _resolve_user_for_status_query(query: str) -> dict | None:
    """
    Пытается найти пользователя по:
    - username (если указан с @ или без);
    - tg_id (число);
    - внутреннему id (число, если не нашли по tg_id).
    """
    q = (query or "").strip()
    if not q:
        return None

    if q.startswith("@"):
        user = await get_user_by_username(q.lstrip("@"))
        if user:
            return user

    # Попробуем интерпретировать как число
    try:
        num_id = int(q)
    except Exception:
        num_id = None

    if num_id is not None:
        user = await get_user_by_tg_id(num_id)
        if user:
            return user
        try:
            return await get_user_by_id(num_id)
        except Exception:
            return None

    return None


def _build_user_status_view(user: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    tg_id = user.get("tg_id")
    internal_id = user.get("id")
    username = (user.get("username") or "").strip()
    display_username = f"@{username}" if username else "—"
    display_name = user.get("name") or user.get("display_name") or "—"

    is_blocked = bool(user.get("is_blocked"))
    reason = (user.get("block_reason") or "—").strip()
    block_until = _format_block_until(user.get("block_until"))

    lines: list[str] = []
    lines.append("👤 <b>Статус пользователя</b>")
    lines.append(f"Имя: <b>{escape(str(display_name))}</b>")
    lines.append(f"Username: <code>{escape(display_username)}</code>")
    lines.append(f"TG ID: <code>{escape(str(tg_id) if tg_id else '—')}</code>")
    lines.append(f"User ID: <code>{escape(str(internal_id) if internal_id else '—')}</code>")
    lines.append("")
    lines.append("Состояние:")
    lines.append("⛔ Заблокирован" if is_blocked else "✅ Активен")
    lines.append("📸 Публикации: закрыты" if is_blocked else "📸 Публикации: доступны")
    lines.append(f"Причина: {escape(reason)}")
    lines.append(f"До: <code>{escape(block_until)}</code>")

    target_id = _get_status_target_id(user)
    if target_id is None:
        return "\n".join(lines), None

    kb = InlineKeyboardBuilder()
    kb.button(text="🔓 Разбанить", callback_data=f"mod:status:unban:{target_id}")
    kb.button(text="📸 Публ.ON", callback_data=f"mod:status:publish:{target_id}")
    kb.button(text="🔄 Обновить", callback_data=f"mod:status:refresh:{target_id}")
    kb.adjust(2, 1)

    return "\n".join(lines), kb.as_markup()


_SOURCE_SET = {"queue", "self", "deep"}
_BAN_DAYS = (1, 3, 7, 30)
_MOD_REASON_BUTTON_LABELS: dict[ReportReason, str] = {
    "selfie": "🤳 Селфи",
    "porn": "🔞 18+",
    "stolen": "🖼️ Чужое",
    "propaganda": "📢 Пропаганда",
    "violence": "💣 Насилие",
    "hate": "🔥 Ненависть",
    "illegal_ads": "🚫 Реклама",
    "other": "📝 Другое",
}


def _normalize_source(source: str | None) -> str:
    src = str(source or "").strip().lower()
    return src if src in _SOURCE_SET else "queue"


def _short_float(value: object | None) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


def _truncate(value: str | None, limit: int = 260) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _role_label(user: dict | None) -> str:
    if not user:
        return "обычный"
    labels: list[str] = []
    if bool(user.get("is_author")):
        labels.append("author")
    if bool(user.get("is_premium")):
        labels.append("premium")
    return "/".join(labels) if labels else "обычный"


def _format_reason_for_user(reason_key: ReportReason, custom_reason: str | None = None) -> str:
    if reason_key == "other":
        custom = (custom_reason or "").strip()
        return custom if custom else MODERATION_REASON_TEXTS["other"]
    return MODERATION_REASON_TEXTS.get(reason_key, MODERATION_REASON_TEXTS["other"])


def _reason_label(reason_key: ReportReason, custom_reason: str | None = None) -> str:
    if reason_key == "other" and (custom_reason or "").strip():
        return "📝 Другое"
    return REPORT_REASON_LABELS.get(reason_key, "📝 Другое")


async def _edit_or_replace_text(
    callback: CallbackQuery,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str = "HTML",
) -> None:
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        else:
            await callback.message.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_notification=True,
        )
    except Exception:
        pass


def _build_ban_days_keyboard(photo_id: int, source: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    src = _normalize_source(source)
    for days in _BAN_DAYS:
        kb.button(text=f"{days}д", callback_data=f"mod:ban_days:{src}:{photo_id}:{days}")
    kb.button(text="⬅️ Назад", callback_data=f"mod:photo_back:{src}:{photo_id}")
    kb.adjust(4, 1)
    return kb.as_markup()


def _build_reason_keyboard(*, photo_id: int, source: str, action: str, ban_days: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    src = _normalize_source(source)
    for reason in get_report_reasons():
        if reason == "other":
            continue
        if action == "ban":
            cb = f"mod:reason:ban:{src}:{photo_id}:{int(ban_days or 3)}:{reason}"
        else:
            cb = f"mod:reason:del:{src}:{photo_id}:{reason}"
        kb.button(text=_MOD_REASON_BUTTON_LABELS.get(reason, str(reason)), callback_data=cb)
    if action == "ban":
        kb.button(text="📝 Другое", callback_data=f"mod:reason_other:ban:{src}:{photo_id}:{int(ban_days or 3)}")
    else:
        kb.button(text="📝 Другое", callback_data=f"mod:reason_other:del:{src}:{photo_id}")
    kb.button(text="⬅️ Назад", callback_data=f"mod:photo_back:{src}:{photo_id}")
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def _build_author_profile_keyboard(*, photo_id: int, source: str, author: dict) -> InlineKeyboardMarkup:
    src = _normalize_source(source)
    author_tg_id = int(author.get("tg_id") or 0)
    author_id = int(author.get("id") or 0)

    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Бан загрузок 1д", callback_data=f"mod:author_ban:{src}:{photo_id}:1")
    kb.button(text="🚫 Бан загрузок 3д", callback_data=f"mod:author_ban:{src}:{photo_id}:3")
    kb.button(text="🚫 Бан загрузок 7д", callback_data=f"mod:author_ban:{src}:{photo_id}:7")
    kb.button(text="🚫 Бан загрузок 30д", callback_data=f"mod:author_ban:{src}:{photo_id}:30")
    if author_tg_id > 0:
        kb.button(text="🔓 Разбанить", callback_data=f"mod:author_unban:{src}:{photo_id}:{author_tg_id}")
    if author_id > 0:
        kb.button(text="👁 Скрыть активные фото", callback_data=f"mod:author_hide:{src}:{photo_id}:{author_id}")
        kb.button(text="🧹 Удалить все активные", callback_data=f"mod:author_purge:{src}:{photo_id}:{author_id}")
    kb.button(text="⬅️ Назад к фото", callback_data=f"mod:photo_back:{src}:{photo_id}")
    kb.adjust(2, 2, 1, 1, 1)
    return kb.as_markup()


async def _build_moderation_caption(
    photo: dict,
    *,
    show_reports: bool = True,
    show_stats: bool = True,
) -> str:
    photo_id = int(photo.get("id") or 0)
    title = _truncate(photo.get("title") or "Без названия", 80)
    tag = (photo.get("tag") or photo.get("category") or "photo").strip()
    device = (photo.get("device_info") or photo.get("device_type") or "—").strip()
    submit_day = photo.get("submit_day") or photo.get("day_key")
    party_short = format_party_id(submit_day, include_year_if_needed=True) if submit_day else ""

    author = None
    try:
        author = await get_user_by_id(int(photo.get("user_id") or 0))
    except Exception:
        author = None

    author_name = escape(str((author or {}).get("name") or (author or {}).get("display_name") or "—"))
    author_code = escape(str((author or {}).get("author_code") or "—"))
    blocked_icon = "⛔" if bool((author or {}).get("is_blocked")) else "✅"
    role = escape(_role_label(author))

    report_pending = 0
    report_total = 0
    if show_reports and photo_id > 0:
        try:
            rs = await get_photo_report_stats(photo_id)
            report_pending = int(rs.get("total_pending") or rs.get("pending") or 0)
            report_total = int(rs.get("total_all") or rs.get("total") or 0)
        except Exception:
            pass

    bayes = "—"
    votes = "0"
    views = "—"
    if show_stats and photo_id > 0:
        try:
            stats = await get_photo_stats(photo_id)
            bayes = _short_float(stats.get("bayes_score"))
            votes = str(int(stats.get("ratings_count") or 0))
            views = str(int(stats.get("views_count") or 0)) if stats.get("views_count") is not None else "—"
        except Exception:
            pass

    header = f"📷 <b>Модерация</b> · <code>ID {photo_id}</code>"
    if party_short:
        header += f" · <code>{escape(str(party_short))}</code>"

    lines: list[str] = [
        header,
        f"<code>\"{escape(title)}\"</code>",
        f"🏷️ <code>{escape(tag)}</code> · 📱 {escape(device)}",
        f"⭐ Bayes: <b>{bayes}</b> · 🗳 <b>{votes}</b> · 👁 <b>{escape(views)}</b>",
        f"🚨 Жалобы: <b>{report_pending}</b> pending / {report_total} all",
        f"👤 Автор: <b>{author_name}</b> (<code>{author_code}</code>) · {role} · {blocked_icon}",
    ]

    desc = _truncate(photo.get("description"), 260)
    if desc:
        lines.append("")
        lines.append(f"📝 {escape(desc)}")

    return "\n".join(lines)


async def _build_self_check_caption(photo: dict) -> str:
    return await _build_moderation_caption(photo, show_reports=True, show_stats=True)


def _pick_photo_file_id(photo: dict, bot: Bot) -> str | None:
    is_support = _is_support_bot(bot)
    if is_support and photo.get("file_id_support"):
        return str(photo.get("file_id_support"))
    raw = photo.get("file_id_public") or photo.get("file_id")
    return str(raw) if raw else None


async def _render_moderation_photo(
    callback: CallbackQuery,
    *,
    photo: dict,
    source: str,
) -> None:
    source = _normalize_source(source)
    caption = await _build_moderation_caption(photo, show_reports=True, show_stats=True)
    kb = build_moderation_photo_keyboard(int(photo["id"]), source=source)
    file_id = _pick_photo_file_id(photo, callback.message.bot)

    if callback.message.photo and file_id:
        try:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"),
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    if callback.message.photo and not file_id:
        await _edit_or_replace_text(
            callback,
            text=caption + "\n\n⚠️ Не удалось получить file_id фотографии.",
            reply_markup=kb,
        )
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    if file_id:
        sent, new_support_id = await _send_photo_with_fallback(
            bot=callback.message.bot,
            chat_id=callback.message.chat.id,
            file_id=file_id,
            caption=caption,
            reply_markup=kb,
        )
        if new_support_id:
            try:
                await set_photo_file_id_support(int(photo["id"]), str(new_support_id))
            except Exception:
                pass
        if sent:
            return

    await callback.message.bot.send_message(
        chat_id=callback.message.chat.id,
        text=caption + "\n\n⚠️ Не удалось загрузить превью фотографии.",
        reply_markup=kb,
        parse_mode="HTML",
        disable_notification=True,
    )


async def _show_empty_moderation_source(callback: CallbackQuery, source: str) -> None:
    src = _normalize_source(source)
    if src == "self":
        text = "Сейчас нет фотографий для самостоятельной проверки."
    elif src == "deep":
        text = "Сейчас нет фотографий на детальной проверке."
    else:
        text = "Сейчас нет фотографий, ожидающих модерации по жалобам."
    await _edit_or_replace_text(callback, text=text, reply_markup=build_moderator_menu())


async def _get_next_photo_by_source(callback: CallbackQuery, source: str) -> dict | None:
    src = _normalize_source(source)
    if src == "self":
        user = await get_user_by_tg_id(callback.from_user.id)
        if not user:
            await callback.answer("Сначала зарегистрируйся в боте через /start.", show_alert=True)
            return None
        return await get_next_photo_for_self_moderation(int(user["id"]))
    if src == "deep":
        return await get_next_photo_for_detailed_moderation()
    return await get_next_photo_for_moderation()


async def _show_next_by_source(callback: CallbackQuery, source: str) -> None:
    src = _normalize_source(source)
    photo = await _get_next_photo_by_source(callback, src)
    if not photo:
        await _show_empty_moderation_source(callback, src)
        return
    await _render_moderation_photo(callback, photo=photo, source=src)


async def show_next_photo_for_moderation(callback: CallbackQuery) -> None:
    await _show_next_by_source(callback, "queue")


async def show_next_photo_for_self_check(callback: CallbackQuery) -> None:
    await _show_next_by_source(callback, "self")


async def show_next_photo_for_deep_check(callback: CallbackQuery) -> None:
    await _show_next_by_source(callback, "deep")


@router.message(Command("chatid"))
async def moderator_chat_id(message: Message) -> None:
    """Helper: prints current chat_id so admin/mods can put it into .env as MODERATION_CHAT_ID."""
    tg_id = message.from_user.id

    user = await get_user_by_tg_id(tg_id)
    is_allowed = bool(user and (user.get("is_admin") or user.get("is_moderator") or user.get("is_support")))
    if not is_allowed:
        return

    chat_id = message.chat.id
    title = getattr(message.chat, "title", None)
    chat_type = getattr(message.chat, "type", None)

    lines: list[str] = []
    lines.append("🆔 <b>ID этого чата</b>")
    if title:
        lines.append(f"Название: <b>{title}</b>")
    if chat_type:
        lines.append(f"Тип: <code>{chat_type}</code>")
    lines.append(f"chat_id: <code>{chat_id}</code>")
    lines.append("")
    lines.append("Скопируй chat_id и вставь в .env как:")
    lines.append(f"<code>MODERATION_CHAT_ID={chat_id}</code>")

    try:
        await message.answer("\n".join(lines), parse_mode="HTML", disable_notification=True)
    except Exception:
        pass


@router.message(Command("moderator"))
async def moderator_entry(message: Message, state: FSMContext) -> None:
    """
    Вход в режим модератора по команде /moderator.

    Логика:
    - если пользователь не зарегистрирован в базе — просим сначала пройти обычную регистрацию;
    - если пользователь уже модератор — сразу показываем меню модерации;
    - если пользователь не модератор — говорим, что доступ выдаёт админ.
    """
    try:
        await message.delete()
    except Exception:
        # Например, если нет прав на удаление сообщения
        pass
    tg_id = message.from_user.id
    user = await get_user_by_tg_id(tg_id)

    if user is None:
        await message.answer(
            "Сначала зарегистрируйся в боте через /start, "
            "а потом повтори команду /moderator."
        )
        return

    if await is_moderator_by_tg_id(tg_id):
        # Уже модератор — сразу показываем меню
        await message.answer(
            "Ты уже модератор.\n\nВот твой раздел модерации:",
            reply_markup=build_moderator_menu(),
        )
        return

    await message.answer(
        "Режим модератора доступен только по назначению администратора.\n\n"
        "Если ты хочешь стать модератором, напиши админу бота."
    )


@router.message(Command("status"))
async def moderator_status(message: Message) -> None:
    """
    Карточка статуса пользователя для модераторов: /status @username или /status <tg_id|user_id>.
    Показывает блокировки и даёт кнопки для быстрого разбана.
    """
    if not await is_moderator_by_tg_id(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажи пользователя: /status @username или /status <tg_id|user_id>",
            disable_notification=True,
        )
        return

    query = parts[1].strip()
    user = await _resolve_user_for_status_query(query)
    if user is None:
        await message.answer(
            "Пользователь не найден. Попробуй другой username или ID.",
            disable_notification=True,
        )
        return

    text, kb = _build_user_status_view(user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_notification=True)


# Новый обработчик: вход в модераторскую панель по callback из главного меню
@router.callback_query(F.data == "moderator:menu")
async def moderator_menu_from_main(callback: CallbackQuery) -> None:
    """
    Вход в модераторскую панель по кнопке из главного меню.

    Используется callback_data="moderator:menu" из build_main_menu.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await _edit_or_replace_text(
        callback,
        text="Раздел модерации.",
        reply_markup=build_moderator_menu(),
    )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("mod:status:"))
async def moderator_status_actions(callback: CallbackQuery) -> None:
    """
    Обработка кнопок из карточки /status:
    - разбанить;
    - вернуть публикации (тот же разбан, но подчёркиваем смысл);
    - обновить карточку.
    """
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Только для модераторов.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные параметры.", show_alert=True)
        return

    action = parts[2]
    try:
        target_id = int(parts[3])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return

    user = await _load_user_by_numeric_id(target_id)
    if user is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    if action in {"unban", "publish"}:
        tg_id = user.get("tg_id")
        if not tg_id:
            await callback.answer("Нет tg_id для разбана.", show_alert=True)
            return

        try:
            await set_user_block_status_by_tg_id(
                int(tg_id),
                is_blocked=False,
                reason=None,
                until_iso=None,
            )
            try:
                await restore_photos_from_status(int(user.get("id") or 0), from_status="blocked_by_ban", to_status="active")
            except Exception:
                pass
            user = await _load_user_by_numeric_id(target_id) or user
            await callback.answer("Готово, ограничения сняты.")
        except Exception:
            await callback.answer("Не удалось обновить статус.", show_alert=True)
            return
    elif action == "refresh":
        await callback.answer("Обновлено.")
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    text, kb = _build_user_status_view(user)
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        try:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_notification=True,
            )
        except Exception:
            pass


@router.callback_query(F.data == "mod:menu")
async def moderator_menu_open(callback: CallbackQuery) -> None:
    """
    Открытие меню модератора по callback 'mod:menu'.

    Можно вызывать, например, из админского раздела или других мест.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await _edit_or_replace_text(
        callback,
        text="Раздел модерации.",
        reply_markup=build_moderator_menu(),
    )


@router.callback_query(F.data == "mod:queue")
async def moderator_queue(callback: CallbackQuery) -> None:
    """
    Запуск очереди модерации.

    Показывает модератору следующую фотографию,
    которая находится в статусе 'under_review'.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await show_next_photo_for_moderation(callback)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:self")
async def moderator_self_check(callback: CallbackQuery) -> None:
    """
    Самостоятельная проверка фотографий модератором.

    Интерфейс как при обычной оценке, но только две кнопки:
    - «в порядке»
    - «забанить»
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await show_next_photo_for_self_check(callback)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:deep")
async def moderator_deep_check(callback: CallbackQuery) -> None:
    """
    Режим детальной проверки фотографий.

    Показывает только те работы, которые модераторы явно отправили
    в статус 'under_detailed_review'.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await show_next_photo_for_deep_check(callback)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:users")
async def moderator_users_menu(callback: CallbackQuery) -> None:
    """
    Вход в раздел работы с пользователями.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await _edit_or_replace_text(
        callback,
        text="Раздел пользователей.\n\nВыбери нужное действие:",
        reply_markup=build_moderator_users_menu(),
    )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:users_search")
async def moderator_users_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Запуск поиска пользователя: просим модератора ввести ID или username.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    # Сохраняем, к какому сообщению привязано подменю
    await state.update_data(
        user_menu_msg_id=callback.message.message_id,
        user_menu_chat_id=callback.message.chat.id,
    )
    await state.set_state(ModeratorStates.waiting_user_search_query)

    text = (
        "🔍 Поиск пользователя.\n\n"
        "Отправь одним сообщением:\n"
        "• @username, или\n"
        "• ID Telegram, или\n"
        "• внутренний ID пользователя из базы (если знаешь)."
    )

    await _edit_or_replace_text(callback, text=text, reply_markup=None)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


def _parse_source_photo(data: str, prefix: str) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) == 4 and f"{parts[0]}:{parts[1]}" == prefix:
        try:
            return _normalize_source(parts[2]), int(parts[3])
        except Exception:
            return None
    if len(parts) == 3 and f"{parts[0]}:{parts[1]}" == prefix:
        try:
            return "queue", int(parts[2])
        except Exception:
            return None
    return None


def _build_notify_seen_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Просмотрено", callback_data="user:notify_seen")
    kb.adjust(1)
    return kb.as_markup()


async def _log_moderation_action(
    *,
    moderator_tg_id: int,
    photo_id: int,
    action: str,
    note: str | None = None,
) -> None:
    try:
        moderator = await get_user_by_tg_id(int(moderator_tg_id))
        if not moderator:
            return
        await add_moderator_review(
            moderator_user_id=int(moderator["id"]),
            photo_id=int(photo_id),
            action=action,
            note=note,
        )
    except Exception:
        pass


async def _send_author_delete_notice(
    *,
    bot: Bot,
    author: dict,
    reason_key: ReportReason,
    reason_custom: str | None = None,
) -> None:
    tg_id = author.get("tg_id")
    if not tg_id:
        return
    reason_label = _reason_label(reason_key, reason_custom)
    reason_text = _format_reason_for_user(reason_key, reason_custom)
    try:
        await bot.send_message(
            chat_id=int(tg_id),
            text=(
                "🗑 <b>Ваша фотография удалена модератором.</b>\n\n"
                f"Причина: {escape(reason_label)}\n"
                f"{escape(reason_text)}"
            ),
            parse_mode="HTML",
            reply_markup=_build_notify_seen_kb(),
            disable_notification=True,
        )
    except Exception:
        pass


async def _send_author_ban_notice(
    *,
    bot: Bot,
    author: dict,
    days: int,
    reason_key: ReportReason,
    reason_custom: str | None = None,
) -> None:
    tg_id = author.get("tg_id")
    if not tg_id:
        return
    until_dt = get_moscow_now() + timedelta(days=int(days))
    reason_label = _reason_label(reason_key, reason_custom)
    reason_text = _format_reason_for_user(reason_key, reason_custom)
    try:
        await bot.send_message(
            chat_id=int(tg_id),
            text=(
                "⛔ <b>Ваша фотография удалена, загрузка временно ограничена.</b>\n\n"
                f"Срок: <b>{int(days)}</b> дн.\n"
                f"До: <code>{until_dt.strftime('%d.%m.%Y %H:%M')}</code> (МСК)\n"
                f"Причина: {escape(reason_label)}\n"
                f"{escape(reason_text)}"
            ),
            parse_mode="HTML",
            reply_markup=_build_notify_seen_kb(),
            disable_notification=True,
        )
    except Exception:
        pass


async def _apply_moderation_decision(
    *,
    bot: Bot,
    moderator_tg_id: int,
    source: str,
    photo_id: int,
    reason_key: ReportReason,
    decision: str,
    reason_custom: str | None = None,
    ban_days: int | None = None,
) -> tuple[bool, str]:
    src = _normalize_source(source)
    photo = await get_photo_by_id(int(photo_id))
    if not photo:
        return False, "Фото не найдено."

    author = None
    try:
        author = await get_user_by_id(int(photo.get("user_id") or 0))
    except Exception:
        author = None

    try:
        await mark_photo_deleted(int(photo_id))
    except Exception:
        pass
    try:
        await set_photo_moderation_status(int(photo_id), "deleted_by_moderator")
    except Exception:
        pass
    try:
        await delete_moderation_message_for_photo(int(photo_id))
    except Exception:
        pass

    if decision == "ban" and author and author.get("tg_id"):
        days = int(ban_days or 3)
        until_dt = get_moscow_now() + timedelta(days=days)
        try:
            await set_user_block_status_by_tg_id(
                int(author["tg_id"]),
                is_blocked=True,
                reason=f"UPLOAD_BAN:{_format_reason_for_user(reason_key, reason_custom)}",
                until_iso=until_dt.isoformat(),
            )
        except Exception:
            pass
        try:
            await hide_active_photos_for_user(int(author.get("id") or 0), new_status="blocked_by_ban")
        except Exception:
            pass
        await _send_author_ban_notice(
            bot=bot,
            author=author,
            days=days,
            reason_key=reason_key,
            reason_custom=reason_custom,
        )
        await _log_moderation_action(
            moderator_tg_id=moderator_tg_id,
            photo_id=int(photo_id),
            action=f"{src}:ban:{days}:{reason_key}",
            note=(reason_custom or None),
        )
        return True, f"Удалено и выдан бан загрузок на {days} дн."

    if author:
        await _send_author_delete_notice(
            bot=bot,
            author=author,
            reason_key=reason_key,
            reason_custom=reason_custom,
        )
    await _log_moderation_action(
        moderator_tg_id=moderator_tg_id,
        photo_id=int(photo_id),
        action=f"{src}:delete:{reason_key}",
        note=(reason_custom or None),
    )
    return True, "Фото удалено."


async def _show_reason_picker(
    callback: CallbackQuery,
    *,
    source: str,
    photo_id: int,
    decision: str,
    ban_days: int | None = None,
) -> None:
    src = _normalize_source(source)
    if decision == "ban":
        text = (
            "📝 <b>Выбери причину</b>\n"
            f"Действие: удалить + бан на <b>{int(ban_days or 3)}</b> дн.\n\n"
            "Можно выбрать шаблон или нажать «Другое»."
        )
    else:
        text = (
            "📝 <b>Выбери причину</b>\n"
            "Действие: удалить фото.\n\n"
            "Можно выбрать шаблон или нажать «Другое»."
        )
    await _edit_or_replace_text(
        callback,
        text=text,
        reply_markup=_build_reason_keyboard(
            photo_id=int(photo_id),
            source=src,
            action=decision,
            ban_days=ban_days,
        ),
    )


async def _open_author_profile(callback: CallbackQuery, *, source: str, photo_id: int) -> None:
    src = _normalize_source(source)
    photo = await get_photo_by_id(int(photo_id))
    if not photo:
        await callback.answer("Фото не найдено.", show_alert=True)
        return
    author = await get_user_by_id(int(photo.get("user_id") or 0))
    if not author:
        await callback.answer("Автор не найден.", show_alert=True)
        return

    try:
        metrics = await get_moderation_author_metrics(int(author["id"]), days=30)
    except Exception:
        metrics = {
            "active_photos": 0,
            "deleted_by_mod_30d": 0,
            "reports_30d": 0,
            "bans_30d": 0,
        }
    status_label = "✅ активен"
    if bool(author.get("is_blocked")):
        status_label = f"⛔ до <code>{escape(_format_block_until(author.get('block_until')))}</code>"

    username = (author.get("username") or "").strip()
    tg_line = f"<code>{escape(str(author.get('tg_id') or '—'))}</code>"
    if username:
        tg_line += f" · @{escape(username)}"

    lines: list[str] = [
        f"👤 <b>Автор: {escape(str(author.get('name') or author.get('display_name') or '—'))}</b>",
        f"Код: <code>{escape(str(author.get('author_code') or '—'))}</code>",
        f"TG: {tg_line}",
        f"Роль: <b>{escape(_role_label(author))}</b>",
        f"Статус: {status_label}",
    ]
    block_reason = (author.get("block_reason") or "").strip()
    if block_reason:
        lines.append(f"Причина: {escape(block_reason)}")

    lines.extend(
        [
            "",
            "— Быстрые метрики —",
            f"📸 Активных фото: {int(metrics.get('active_photos') or 0)}",
            f"🗑 Удалено модерами (30д): {int(metrics.get('deleted_by_mod_30d') or 0)}",
            f"🚨 Жалоб на автора (30д): {int(metrics.get('reports_30d') or 0)}",
            f"⛔ Банов (30д): {int(metrics.get('bans_30d') or 0)}",
        ]
    )
    created_at = author.get("created_at")
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at))
            lines.append(f"🕒 Регистрация: {dt.strftime('%d.%m.%Y')}")
        except Exception:
            pass

    await _edit_or_replace_text(
        callback,
        text="\n".join(lines),
        reply_markup=_build_author_profile_keyboard(photo_id=photo_id, source=src, author=author),
    )


@router.callback_query(F.data.startswith("mod:next:"))
async def moderator_next_by_source(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    source = _normalize_source(parts[2] if len(parts) > 2 else "queue")
    await _show_next_by_source(callback, source)
    await callback.answer()


@router.callback_query(F.data.startswith("mod:photo_back:"))
async def moderator_photo_back(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_back")
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source, photo_id = parsed
    photo = await get_photo_by_id(int(photo_id))
    if not photo:
        await _show_next_by_source(callback, source)
        await callback.answer("Фото уже недоступно.", show_alert=False)
        return
    await _render_moderation_photo(callback, photo=photo, source=source)
    await callback.answer()


@router.callback_query(F.data.startswith("mod:photo_ok:"))
async def moderator_photo_ok(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_ok")
    if not parsed:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return
    source, photo_id = parsed
    try:
        await set_photo_moderation_status(int(photo_id), "good")
    except Exception:
        await callback.answer("Не удалось обновить статус.", show_alert=True)
        return
    await _log_moderation_action(
        moderator_tg_id=callback.from_user.id,
        photo_id=int(photo_id),
        action=f"{_normalize_source(source)}:ok",
    )
    await _show_next_by_source(callback, source)
    await callback.answer("Ок")


@router.callback_query(F.data.startswith("mod:photo_deep:"))
async def moderator_photo_deep(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    try:
        await set_photo_moderation_status(photo_id, "under_detailed_review")
    except Exception:
        await callback.answer("Не удалось обновить статус.", show_alert=True)
        return
    await _log_moderation_action(
        moderator_tg_id=callback.from_user.id,
        photo_id=photo_id,
        action="queue:deep",
    )
    await _show_next_by_source(callback, "queue")
    await callback.answer("Отправлено на deep-check")


@router.callback_query(F.data.startswith("mod:photo_profile:"))
async def moderator_photo_profile(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_profile")
    if not parsed:
        parts = (callback.data or "").split(":")
        if len(parts) == 3:
            try:
                parsed = ("queue", int(parts[2]))
            except Exception:
                parsed = None
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source, photo_id = parsed
    await _open_author_profile(callback, source=source, photo_id=photo_id)
    await callback.answer()


@router.callback_query(F.data.startswith("mod:photo_delete:"))
async def moderator_photo_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_delete")
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source, photo_id = parsed
    await state.clear()
    await _show_reason_picker(callback, source=source, photo_id=photo_id, decision="del")
    await callback.answer()


@router.callback_query(F.data.startswith("mod:photo_delete_ban:"))
async def moderator_photo_delete_ban(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_delete_ban")
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source, photo_id = parsed
    await state.clear()
    await _edit_or_replace_text(
        callback,
        text="⛔ <b>Удалить + бан</b>\n\nВыбери срок бана загрузок:",
        reply_markup=_build_ban_days_keyboard(photo_id=photo_id, source=source),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:ban_days:"))
async def moderator_ban_days(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        source = _normalize_source(parts[2])
        photo_id = int(parts[3])
        days = int(parts[4])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    if days not in _BAN_DAYS:
        await callback.answer("Неверный срок.", show_alert=True)
        return
    await _show_reason_picker(
        callback,
        source=source,
        photo_id=photo_id,
        decision="ban",
        ban_days=days,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:reason:del:"))
async def moderator_reason_delete_quick(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 6:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source = _normalize_source(parts[3])
    try:
        photo_id = int(parts[4])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    reason_key = str(parts[5]).strip()
    if reason_key not in get_report_reasons():
        await callback.answer("Неизвестная причина.", show_alert=True)
        return

    ok, msg = await _apply_moderation_decision(
        bot=callback.message.bot,
        moderator_tg_id=callback.from_user.id,
        source=source,
        photo_id=photo_id,
        reason_key=reason_key,  # type: ignore[arg-type]
        decision="delete",
    )
    if not ok:
        await callback.answer(msg, show_alert=True)
        return
    await _show_next_by_source(callback, source)
    await callback.answer(msg, show_alert=False)


@router.callback_query(F.data.startswith("mod:reason:ban:"))
async def moderator_reason_ban_quick(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 7:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source = _normalize_source(parts[3])
    try:
        photo_id = int(parts[4])
        days = int(parts[5])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    reason_key = str(parts[6]).strip()
    if reason_key not in get_report_reasons():
        await callback.answer("Неизвестная причина.", show_alert=True)
        return
    ok, msg = await _apply_moderation_decision(
        bot=callback.message.bot,
        moderator_tg_id=callback.from_user.id,
        source=source,
        photo_id=photo_id,
        reason_key=reason_key,  # type: ignore[arg-type]
        decision="ban",
        ban_days=days,
    )
    if not ok:
        await callback.answer(msg, show_alert=True)
        return
    await _show_next_by_source(callback, source)
    await callback.answer(msg, show_alert=False)


@router.callback_query(F.data.startswith("mod:reason_other:"))
async def moderator_reason_other(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # mod:reason_other:<del|ban>:<source>:<photo_id>[:days]
    if len(parts) not in {5, 6}:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    action_token = parts[2]
    if action_token not in {"del", "ban"}:
        await callback.answer("Некорректный тип действия.", show_alert=True)
        return
    source = _normalize_source(parts[3])
    try:
        photo_id = int(parts[4])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    ban_days = None
    if action_token == "ban":
        if len(parts) != 6:
            await callback.answer("Некорректные данные.", show_alert=True)
            return
        try:
            ban_days = int(parts[5])
        except Exception:
            await callback.answer("Некорректный срок.", show_alert=True)
            return
        if ban_days not in _BAN_DAYS:
            await callback.answer("Некорректный срок.", show_alert=True)
            return

    await state.update_data(
        mod_reason_photo_id=photo_id,
        mod_reason_source=source,
        mod_reason_action=action_token,
        mod_reason_ban_days=ban_days,
        mod_reason_prompt_msg_id=callback.message.message_id,
        mod_reason_prompt_chat_id=callback.message.chat.id,
    )
    await state.set_state(ModeratorStates.waiting_ban_reason)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=f"mod:photo_back:{source}:{photo_id}")
    kb.adjust(1)
    await _edit_or_replace_text(
        callback,
        text=(
            "📝 Введи причину (1–2 предложения).\n\n"
            "Лучше коротко и по делу."
        ),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:photo_skip:"))
async def moderator_photo_skip_legacy(callback: CallbackQuery) -> None:
    # Legacy callback from old cards. Now maps to "next".
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_skip")
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source, photo_id = parsed
    await _log_moderation_action(
        moderator_tg_id=callback.from_user.id,
        photo_id=photo_id,
        action=f"{_normalize_source(source)}:skip",
    )
    await _show_next_by_source(callback, source)
    await callback.answer("Пропущено")


@router.callback_query(F.data.startswith("mod:author_ban:"))
async def moderator_author_ban_from_profile(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        source = _normalize_source(parts[2])
        photo_id = int(parts[3])
        days = int(parts[4])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    await _show_reason_picker(
        callback,
        source=source,
        photo_id=photo_id,
        decision="ban",
        ban_days=days,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:author_unban:"))
async def moderator_author_unban(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 6:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        source = _normalize_source(parts[2])
        photo_id = int(parts[3])
        target_tg_id = int(parts[5])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    user = await get_user_by_tg_id(target_tg_id)
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    try:
        await set_user_block_status_by_tg_id(target_tg_id, is_blocked=False, reason=None, until_iso=None)
    except Exception:
        await callback.answer("Не удалось снять блок.", show_alert=True)
        return
    try:
        await restore_photos_from_status(int(user.get("id") or 0), from_status="blocked_by_ban", to_status="active")
    except Exception:
        pass
    await _open_author_profile(callback, source=source, photo_id=photo_id)
    await callback.answer("Разблокирован")


@router.callback_query(F.data.startswith("mod:author_hide:"))
async def moderator_author_hide_active(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 6:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        source = _normalize_source(parts[2])
        photo_id = int(parts[3])
        author_id = int(parts[5])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        changed = await hide_active_photos_for_user(author_id, new_status="blocked_by_ban")
    except Exception:
        changed = 0
    await _open_author_profile(callback, source=source, photo_id=photo_id)
    await callback.answer(f"Скрыто фото: {changed}")


@router.callback_query(F.data.startswith("mod:author_purge:"))
async def moderator_author_purge_active(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 6:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        source = _normalize_source(parts[2])
        photo_id = int(parts[3])
        author_id = int(parts[5])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        ids = await get_photo_ids_for_user(author_id)
    except Exception:
        ids = []
    deleted = 0
    for pid in ids:
        try:
            photo = await get_photo_by_id(int(pid))
            if not photo:
                continue
            if bool(photo.get("is_deleted")):
                continue
            if str(photo.get("moderation_status") or "") not in {"active", "good", "under_review", "under_detailed_review"}:
                continue
            await mark_photo_deleted(int(pid))
            await set_photo_moderation_status(int(pid), "deleted_by_moderator")
            deleted += 1
        except Exception:
            continue
    await _open_author_profile(callback, source=source, photo_id=photo_id)
    await callback.answer(f"Удалено активных: {deleted}")


@router.callback_query(F.data.startswith("mod:report_ok:"))
async def mod_report_ok(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    try:
        await set_photo_moderation_status(photo_id, "good")
    except Exception:
        await callback.answer("Не удалось обновить статус.", show_alert=True)
        return
    await _log_moderation_action(
        moderator_tg_id=callback.from_user.id,
        photo_id=photo_id,
        action="queue:ok",
    )
    await _show_next_by_source(callback, "queue")
    await callback.answer("Ок")


@router.callback_query(F.data.startswith("mod:report_delete:"))
async def mod_report_delete(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    await _show_reason_picker(callback, source="queue", photo_id=photo_id, decision="del")
    await callback.answer()


@router.callback_query(F.data.startswith("mod:report_block:"))
async def mod_report_block_start(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    await _edit_or_replace_text(
        callback,
        text="⛔ <b>Удалить + бан</b>\n\nВыбери срок бана загрузок:",
        reply_markup=_build_ban_days_keyboard(photo_id=photo_id, source="queue"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:report_block_days:"))
async def mod_report_block_days(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        photo_id = int(parts[2])
        days = int(parts[3])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    await _show_reason_picker(
        callback,
        source="queue",
        photo_id=photo_id,
        decision="ban",
        ban_days=days,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:report_block_back:"))
async def mod_report_block_back(callback: CallbackQuery) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    photo = await get_photo_by_id(photo_id)
    if not photo:
        await _show_next_by_source(callback, "queue")
        await callback.answer("Фото уже недоступно.")
        return
    await _render_moderation_photo(callback, photo=photo, source="queue")
    await callback.answer()


@router.callback_query(F.data.startswith("mod:photo_block:"))
async def moderator_photo_block(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    parsed = _parse_source_photo(callback.data or "", "mod:photo_block")
    if not parsed:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    source, photo_id = parsed
    await state.clear()
    await _show_reason_picker(callback, source=source, photo_id=photo_id, decision="del")
    await callback.answer()


@router.callback_query(F.data.startswith("mod:block_action:"))
async def moderator_block_action(callback: CallbackQuery, state: FSMContext) -> None:
    # legacy compatibility for very old cards: route to reason picker
    if not await is_moderator_by_tg_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только модераторам.", show_alert=True)
        return
    data = await state.get_data()
    photo_id = int(data.get("mod_ban_photo_id") or 0)
    source = _normalize_source(data.get("mod_ban_source") or "queue")
    action = (callback.data or "").split(":")[-1]
    if not photo_id:
        await callback.answer("Фото не найдено.", show_alert=True)
        return
    if action == "delete_and_ban":
        await _edit_or_replace_text(
            callback,
            text="⛔ <b>Удалить + бан</b>\n\nВыбери срок бана загрузок:",
            reply_markup=_build_ban_days_keyboard(photo_id=photo_id, source=source),
        )
    else:
        await _show_reason_picker(callback, source=source, photo_id=photo_id, decision="del")
    await callback.answer()

@router.message(ModeratorStates.waiting_user_search_query)
async def moderator_users_search_input(message: Message, state: FSMContext) -> None:
    """
    Обработка ввода поискового запроса модератором.

    Логика:
    - удаляем сообщение с вводом модератора (чистый чат);
    - пытаемся найти пользователя по ID Telegram или внутреннему ID;
    - username пока не поддерживаем напрямую (нет быстрого поиска в БД);
    - показываем краткую информацию о пользователе;
    - возвращаемся в подменю пользователей.
    """
    # Удаляем текст модератора
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    menu_msg_id = data.get("user_menu_msg_id")
    menu_chat_id = data.get("user_menu_chat_id", message.chat.id)

    query = (message.text or "").strip()
    if not query:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Пустой запрос. Попробуй ещё раз через раздел «Пользователи».",
        )
        await state.clear()
        return

    found_user = None

    # Поиск по username пока не реализован на уровне БД — заглушка
    if query.startswith("@"):
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=(
                "Поиск по username пока не реализован на уровне базы данных.\n\n"
                "Пока можно искать только по ID Telegram или внутреннему ID пользователя."
            ),
        )
    else:
        # Пробуем воспринимать как число: сначала как tg_id, потом как internal id
        try:
            num_id = int(query)
        except ValueError:
            num_id = None

        if num_id is not None:
            # Сначала пробуем как ID Telegram
            user = await get_user_by_tg_id(num_id)
            if user is None:
                # Если не нашли — пробуем как внутренний ID
                try:
                    user = await get_user_by_id(num_id)
                except Exception:
                    user = None
            found_user = user

    if found_user is None:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Пользователь не найден. Попробуй другой запрос.",
        )
    else:
        # Формируем краткую карточку пользователя
        lines: list[str] = []
        lines.append("👤 <b>Найден пользователь</b>")
        internal_id = found_user.get("id")
        tg_id_value = found_user.get("tg_id")
        username = found_user.get("username")
        name = found_user.get("name") or found_user.get("display_name")
        age = found_user.get("age")
        gender = found_user.get("gender")
        channel = found_user.get("channel_username") or found_user.get("channel_link")
        is_moderator_flag = found_user.get("is_moderator")
        is_admin_flag = found_user.get("is_admin")

        if internal_id is not None:
            lines.append(f"ID в базе: <code>{internal_id}</code>")
        if tg_id_value is not None:
            lines.append(f"ID Telegram: <code>{tg_id_value}</code>")
        if username:
            lines.append(f"Username: @{username}")
        if name:
            lines.append(f"Имя: <b>{name}</b>")
        if age:
            lines.append(f"Возраст: {age}")
        if gender:
            lines.append(f"Пол: {gender}")
        if channel:
            lines.append(f"Канал: {channel}")
        if is_admin_flag:
            lines.append("Роль: администратор")
        elif is_moderator_flag:
            lines.append("Роль: модератор")

        bio = found_user.get("bio")
        if bio:
            lines.append("")
            lines.append("Описание:")
            lines.append(bio)

        text = "\n".join(lines)

        await message.bot.send_message(
            chat_id=message.chat.id,
            text=text,
        )

    # Возвращаем подменю пользователей на том же сообщении, откуда стартовали
    if menu_msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text="Раздел пользователей.\n\nВыбери нужное действие:",
                reply_markup=build_moderator_users_menu(),
            )
        except Exception:
            await message.bot.send_message(
                chat_id=menu_chat_id,
                text="Раздел пользователей.\n\nВыбери нужное действие:",
                reply_markup=build_moderator_users_menu(),
            )

    await state.clear()


@router.callback_query(F.data == "mod:users_block")
async def moderator_users_block_stub(callback: CallbackQuery) -> None:
    """
    Заглушка для блока / разбана пользователей.

    Реальную логику блокировок можно будет добавить отдельно в database.py,
    а здесь только интерфейс.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    text = (
        "Раздел «Блок / разбан» пока в разработке.\n\n"
        "Когда добавим поддержку хранения глобальных блокировок "
        "в базе данных, здесь можно будет:\n"
        "• блокировать пользователя по ID или username;\n"
        "• снимать блокировку;\n"
        "• указывать причину ограничения."
    )

    await _edit_or_replace_text(
        callback,
        text=text,
        reply_markup=build_moderator_users_menu(),
    )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:users_blocked")
async def moderator_users_blocked_stub(callback: CallbackQuery) -> None:
    """
    Заглушка для списка заблокированных пользователей.

    Реальную выборку из базы можно будет добавить отдельно.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    text = (
        "Список заблокированных пользователей пока не реализован.\n\n"
        "Когда появится таблица/функции для хранения таких блокировок, "
        "здесь можно будет посмотреть их в удобном виде."
    )

    await _edit_or_replace_text(
        callback,
        text=text,
        reply_markup=build_moderator_users_menu(),
    )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.message(ModeratorStates.waiting_ban_reason)
async def moderator_ban_reason_input(message: Message, state: FSMContext) -> None:
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    photo_id = int(data.get("mod_reason_photo_id") or 0)
    source = _normalize_source(data.get("mod_reason_source") or "queue")
    action_token = str(data.get("mod_reason_action") or "del")
    ban_days = int(data.get("mod_reason_ban_days") or 3)
    prompt_msg_id = data.get("mod_reason_prompt_msg_id")
    prompt_chat_id = int(data.get("mod_reason_prompt_chat_id") or message.chat.id)

    reason = (message.text or "").strip()
    if not reason:
        reason = "Причина не указана."

    if not photo_id:
        await state.clear()
        return

    decision = "ban" if action_token == "ban" else "delete"
    _ok, _msg = await _apply_moderation_decision(
        bot=message.bot,
        moderator_tg_id=message.from_user.id,
        source=source,
        photo_id=photo_id,
        reason_key="other",
        decision=decision,
        reason_custom=reason,
        ban_days=ban_days,
    )

    if prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=prompt_chat_id, message_id=int(prompt_msg_id))
        except Exception:
            pass

    # Показываем следующий экран в том же режиме.
    next_photo = None
    if source == "self":
        user = await get_user_by_tg_id(message.from_user.id)
        if user:
            next_photo = await get_next_photo_for_self_moderation(int(user["id"]))
    elif source == "deep":
        next_photo = await get_next_photo_for_detailed_moderation()
    else:
        next_photo = await get_next_photo_for_moderation()

    if not next_photo:
        empty_text = (
            "Сейчас нет фотографий для самостоятельной проверки."
            if source == "self"
            else "Сейчас нет фотографий на детальной проверке."
            if source == "deep"
            else "Сейчас нет фотографий, ожидающих модерации по жалобам."
        )
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=empty_text,
            reply_markup=build_moderator_menu(),
            disable_notification=True,
        )
        await state.clear()
        return

    caption = await _build_moderation_caption(next_photo, show_reports=True, show_stats=True)
    file_id = _pick_photo_file_id(next_photo, message.bot)
    if file_id:
        sent, new_support_id = await _send_photo_with_fallback(
            bot=message.bot,
            chat_id=message.chat.id,
            file_id=file_id,
            caption=caption,
            reply_markup=build_moderation_photo_keyboard(int(next_photo["id"]), source=source),
        )
        if new_support_id:
            try:
                await set_photo_file_id_support(int(next_photo["id"]), str(new_support_id))
            except Exception:
                pass
        if not sent:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text=caption + "\n\n⚠️ Не удалось загрузить превью фотографии.",
                reply_markup=build_moderation_photo_keyboard(int(next_photo["id"]), source=source),
                parse_mode="HTML",
                disable_notification=True,
            )
    else:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=caption + "\n\n⚠️ Не удалось получить file_id.",
            reply_markup=build_moderation_photo_keyboard(int(next_photo["id"]), source=source),
            parse_mode="HTML",
            disable_notification=True,
        )

    await state.clear()


@router.callback_query(F.data == "user:notify_seen")
async def user_notify_seen(callback: CallbackQuery) -> None:
    """
    Пользователь нажал «Просмотрено» под служебным уведомлением.

    Логика:
    - удаляем сообщение с уведомлением;
    - не создаём новых сообщений.
    """
    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
