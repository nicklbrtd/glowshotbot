from aiogram import Router, F
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaDocument, ChatJoinRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
import html

from config import AUTHOR_APPLICATIONS_CHAT_ID, MODERATION_CHAT_ID
from database import (
    get_user_by_tg_id,
    ensure_user_author_code,
    set_user_author_status_by_tg_id,
    is_user_author_by_tg_id,
    get_user_rating_summary,
    get_author_settings,
    set_author_forward_allowed,
    set_author_badge_enabled,
)
from datetime import datetime
from keyboards.common import build_back_kb
from utils.i18n import t

router = Router()

AUTHOR_GROUP_INVITE_LINK = "https://t.me/+aajVP9raYyVkMTZi"


class AuthorApplyStates(StatesGroup):
    waiting_channel = State()
    waiting_works = State()
    waiting_bio = State()
    waiting_more_files = State()


@router.callback_query(F.data == "author:menu")
async def author_menu(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    if not await is_user_author_by_tg_id(int(callback.from_user.id)):
        await _deny_author_access(callback, state)
        return

    settings = await get_author_settings(user_id=int(user["id"]))
    await _render_author_main(callback, state, settings=settings)
    await callback.answer()


def _author_menu_text(*, settings: dict) -> str:
    forward_allowed = bool(settings.get("forward_allowed", True))
    badge_enabled = bool(settings.get("badge_enabled", True))
    forward_line = "✅ разрешена" if forward_allowed else "🔒 запрещена"
    badge_line = "✅ показывается" if badge_enabled else "🙈 скрыт"
    return (
        "🧑‍🎨 <b>Меню автора</b>\n\n"
        f"📨 Пересылка фото: {forward_line}\n"
        f"🎖 Бейдж автора: {badge_line}\n"
        "📁 Портфолио: 🔒 скоро (заглушка)"
    )


def _author_access_denied_text() -> str:
    return "🧑‍🎨 <b>Меню автора</b>\n\nСначала пройди подтверждение автора."


def _author_access_denied_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:profile"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"),
    )
    return kb.as_markup()


def _author_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="author:settings"))
    kb.row(InlineKeyboardButton(text="📁 Портфолио", callback_data="author:portfolio"))
    kb.row(InlineKeyboardButton(text="👥 Группа авторов", url=AUTHOR_GROUP_INVITE_LINK))
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:profile"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"),
    )
    return kb.as_markup()


def _author_settings_text(*, settings: dict) -> str:
    forward_allowed = bool(settings.get("forward_allowed", True))
    badge_enabled = bool(settings.get("badge_enabled", True))
    forward_line = "✅ разрешена" if forward_allowed else "🔒 запрещена"
    badge_line = "✅ показывается" if badge_enabled else "🙈 скрыт"
    return (
        "⚙️ <b>Настройки автора</b>\n\n"
        f"📨 Пересылка фото: {forward_line}\n"
        "Если выключено — бот отправляет фото как защищённое (нельзя переслать/сохранить в Telegram).\n"
        "Скриншоты не блокируются.\n\n"
        f"🎖 Бейдж автора: {badge_line}"
    )


def _author_settings_kb(*, settings: dict) -> InlineKeyboardMarkup:
    forward_allowed = bool(settings.get("forward_allowed", True))
    badge_enabled = bool(settings.get("badge_enabled", True))
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="🔒 Запретить пересылку" if forward_allowed else "✅ Разрешить пересылку",
            callback_data="author:toggle:forward",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="🙈 Скрыть бейдж" if badge_enabled else "👁 Показать бейдж",
            callback_data="author:toggle:badge",
        )
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="author:menu"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"),
    )
    return kb.as_markup()


def _author_portfolio_text() -> str:
    return (
        "📁 <b>Портфолио автора</b>\n\n"
        "Скоро: публичная страница лучших работ + ссылка для клиентов.\n\n"
        "План: выбрать 9 работ, показать итоги/награды, включить публичный доступ."
    )


def _author_portfolio_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="👥 Группа авторов", url=AUTHOR_GROUP_INVITE_LINK))
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="author:menu"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"),
    )
    return kb.as_markup()


async def _render_author_screen(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup,
    *,
    parse_mode: str = "HTML",
) -> None:
    if await _edit_screen(callback.message.bot, state, text, kb, parse_mode=parse_mode):
        try:
            data = await state.get_data()
            msg_id, chat_id = _get_screen_ids(data)
            await state.update_data(
                author_msg_id=int(msg_id or callback.message.message_id),
                author_chat_id=int(chat_id or callback.message.chat.id),
            )
        except Exception:
            pass
        return

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=parse_mode,
        )
        try:
            await state.update_data(
                author_msg_id=callback.message.message_id,
                author_chat_id=callback.message.chat.id,
            )
        except Exception:
            pass
        return
    except Exception:
        pass

    old_message_id = callback.message.message_id
    try:
        sent = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode=parse_mode,
            disable_notification=True,
        )
    except Exception:
        return

    try:
        await callback.message.bot.delete_message(
            chat_id=callback.message.chat.id,
            message_id=old_message_id,
        )
    except Exception:
        pass

    try:
        await state.update_data(
            author_msg_id=sent.message_id,
            author_chat_id=sent.chat.id,
        )
    except Exception:
        pass


async def _render_author_main(callback: CallbackQuery, state: FSMContext, *, settings: dict | None = None) -> None:
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        return
    resolved_settings = settings or await get_author_settings(user_id=int(user["id"]))
    await _render_author_screen(
        callback,
        state,
        _author_menu_text(settings=resolved_settings),
        _author_menu_kb(),
    )


async def _deny_author_access(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Сначала пройди подтверждение автора", show_alert=True)
    await _render_author_screen(
        callback,
        state,
        _author_access_denied_text(),
        _author_access_denied_kb(),
    )


@router.callback_query(F.data == "author:settings")
async def author_settings(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    if not await is_user_author_by_tg_id(int(callback.from_user.id)):
        await _deny_author_access(callback, state)
        return
    settings = await get_author_settings(user_id=int(user["id"]))
    await _render_author_screen(
        callback,
        state,
        _author_settings_text(settings=settings),
        _author_settings_kb(settings=settings),
    )
    await callback.answer()


@router.callback_query(F.data == "author:portfolio")
async def author_portfolio(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    if not await is_user_author_by_tg_id(int(callback.from_user.id)):
        await _deny_author_access(callback, state)
        return
    await _render_author_screen(
        callback,
        state,
        _author_portfolio_text(),
        _author_portfolio_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "author:toggle:forward")
async def author_toggle_forward(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    if not await is_user_author_by_tg_id(int(callback.from_user.id)):
        await _deny_author_access(callback, state)
        return
    settings = await get_author_settings(user_id=int(user["id"]))
    new_value = not bool(settings.get("forward_allowed", True))
    await set_author_forward_allowed(int(user["id"]), new_value)
    updated = await get_author_settings(user_id=int(user["id"]))
    await _render_author_screen(
        callback,
        state,
        _author_settings_text(settings=updated),
        _author_settings_kb(settings=updated),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data == "author:toggle:badge")
async def author_toggle_badge(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    if not await is_user_author_by_tg_id(int(callback.from_user.id)):
        await _deny_author_access(callback, state)
        return
    settings = await get_author_settings(user_id=int(user["id"]))
    new_value = not bool(settings.get("badge_enabled", True))
    await set_author_badge_enabled(int(user["id"]), new_value)
    updated = await get_author_settings(user_id=int(user["id"]))
    await _render_author_screen(
        callback,
        state,
        _author_settings_text(settings=updated),
        _author_settings_kb(settings=updated),
    )
    await callback.answer("Сохранено")


def _author_target_chat_id() -> int | None:
    """
    Returns chat id where author applications should be sent.
    Prefers AUTHOR_APPLICATIONS_CHAT_ID, falls back to MODERATION_CHAT_ID.
    """
    return AUTHOR_APPLICATIONS_CHAT_ID or MODERATION_CHAT_ID


def _author_apply_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1)
    return kb.as_markup()


async def _safe_delete(message):
    try:
        await message.delete()
    except Exception:
        pass


def _get_screen_ids(data: dict) -> tuple[int | None, int | None]:
    return data.get("author_msg_id"), data.get("author_chat_id")


async def _edit_screen(bot, state: FSMContext, text: str, kb: InlineKeyboardMarkup, parse_mode: str = "HTML"):
    data = await state.get_data()
    msg_id, chat_id = _get_screen_ids(data)
    if msg_id and chat_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
                parse_mode=parse_mode,
            )
            return True
        except Exception:
            return False
    return False


async def _should_warn_media_group(state: FSMContext, key: str, media_group_id: str | None) -> bool:
    """
    Returns True if we should show a warning for this media_group_id (or no group id).
    Prevents spamming same warning for albums.
    """
    if not media_group_id:
        return True
    data = await state.get_data()
    warned = data.get(key)
    if warned == media_group_id:
        return False
    await state.update_data(**{key: media_group_id})
    return True


async def _is_author_admin(bot, user_id: int) -> bool:
    """
    Checks if user is admin of the author applications chat.
    """
    chat_id = _author_target_chat_id()
    if chat_id is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    except Exception:
        return False


def _normalize_author_channel(value: str) -> str | None:
    """
    Accepts @username or t.me/username; returns '@username' or None.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        u = raw[1:].strip()
    else:
        lower = raw.lower()
        if lower.startswith("https://t.me/") or lower.startswith("http://t.me/"):
            u = raw.split("t.me/", 1)[1].strip().strip("/")
        elif lower.startswith("https://telegram.me/") or lower.startswith("http://telegram.me/"):
            u = raw.split("telegram.me/", 1)[1].strip().strip("/")
        elif lower.startswith("t.me/"):
            u = raw.split("t.me/", 1)[1].strip().strip("/")
        elif lower.startswith("telegram.me/"):
            u = raw.split("telegram.me/", 1)[1].strip().strip("/")
        else:
            return None
    u = u.strip()
    if not u or " " in u or "/" in u:
        return None
    return f"@{u}"


def _get_lang(user: object | None) -> str:
    if not user:
        return "ru"
    try:
        raw = (
            user.get("lang")  # type: ignore[attr-defined]
            or user.get("language")  # type: ignore[attr-defined]
            or user.get("language_code")  # type: ignore[attr-defined]
            or user.get("locale")  # type: ignore[attr-defined]
        )
        if raw:
            s = str(raw).strip().lower().split("-")[0]
            return s if s in ("ru", "en") else "ru"
    except Exception:
        pass
    return "ru"


@router.callback_query(F.data == "profile:be_author")
async def profile_be_author(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, странно. Попробуй /start.", show_alert=True)
        return

    lang = _get_lang(user)
    await state.clear()
    await state.update_data(
        author_apply_photos=[],
        author_channel=None,
        author_reason=None,
        author_msg_id=callback.message.message_id,
        author_chat_id=callback.message.chat.id,
    )

    text = t("profile.author.apply.benefits", lang)
    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.continue", lang), callback_data="author:step:channel")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)
    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "author:cancel")
async def profile_author_cancel(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await state.clear()
    await callback.message.edit_text(
        t("profile.author.apply.cancelled", lang),
        reply_markup=build_back_kb(callback_data="menu:profile", text=t("common.back", lang)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "author:step:channel")
async def author_step_channel(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await state.set_state(AuthorApplyStates.waiting_channel)
    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.no_channel_btn", lang), callback_data="author:channel:none")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)
    await _edit_screen(callback.message.bot, state, t("profile.author.apply.ask_channel", lang), kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "author:channel:none")
async def author_channel_none(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await state.update_data(author_channel=None)
    await state.set_state(AuthorApplyStates.waiting_works)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.works_next", lang), callback_data="author:works:next")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)

    await _edit_screen(callback.message.bot, state, t("profile.author.apply.ask_works", lang, count=0), kb.as_markup())
    await callback.answer()


@router.message(AuthorApplyStates.waiting_channel, F.text)
async def author_channel_text(message: Message, state: FSMContext):
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    chan = _normalize_author_channel(message.text or "")
    if not chan:
        await _edit_screen(
            message.bot,
            state,
            t("profile.author.apply.channel_invalid", lang) + "\n\n" + t("profile.author.apply.ask_channel", lang),
            _author_apply_kb(lang),
        )
        await _safe_delete(message)
        return

    await state.update_data(author_channel=chan)
    await state.set_state(AuthorApplyStates.waiting_works)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.works_next", lang), callback_data="author:works:next")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)

    await _edit_screen(
        message.bot,
        state,
        t("profile.author.apply.channel_saved", lang, channel=chan) + "\n\n" + t("profile.author.apply.ask_works", lang, count=0),
        kb.as_markup(),
    )
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_channel)
async def author_channel_other(message: Message):
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_works, F.document)
async def author_collect_work(message: Message, state: FSMContext):
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    doc = message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        if await _should_warn_media_group(state, "warn_non_image_works", message.media_group_id):
            await _edit_screen(
                message.bot,
                state,
                "Нужен файл-изображение (RAW/PNG/JPEG и т.п.). Отправь как файл, без сжатия.\n\n"
                + t("profile.author.apply.ask_works", lang, count=len(works)),
                _author_apply_kb(lang),
            )
        await _safe_delete(message)
        return

    data = await state.get_data()
    works: list[str] = list(data.get("author_apply_photos") or [])

    if len(works) >= 10:
        await _edit_screen(
            message.bot,
            state,
            t("profile.author.apply.too_many", lang),
            _author_apply_kb(lang),
        )
        await _safe_delete(message)
        return

    fid = doc.file_id
    if fid not in works:
        works.append(fid)
        await state.update_data(author_apply_photos=works)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.works_next", lang), callback_data="author:works:next")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)

    await _edit_screen(
        message.bot,
        state,
        t("profile.author.apply.count", lang, count=len(works)),
        kb.as_markup(),
    )
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_works, F.photo)
async def author_reject_photo(message: Message, state: FSMContext):
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    if await _should_warn_media_group(state, "warn_photo_as_file_works", message.media_group_id):
        await _edit_screen(
            message.bot,
            state,
            "Отправь работы как <b>файлы</b>, без сжатия. Используй скрепку → Файл.\n\n"
            + t("profile.author.apply.ask_works", lang, count=len((await state.get_data()).get("author_apply_photos") or [])),
            _author_apply_kb(lang),
        )
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_works)
async def author_waiting_other(message: Message, state: FSMContext):
    # ignore other types
    await _safe_delete(message)


@router.callback_query(F.data == "author:works:next")
async def author_works_next(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    works: list[str] = list(data.get("author_apply_photos") or [])
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    if len(works) < 5:
        await callback.answer(t("profile.author.apply.need_more", lang, count=len(works)), show_alert=True)
        return

    await state.set_state(AuthorApplyStates.waiting_bio)
    await _edit_screen(
        callback.message.bot,
        state,
        t("profile.author.apply.ask_bio", lang),
        build_back_kb(callback_data="author:cancel", text=t("profile.author.apply.cancel_btn", lang)),
    )
    await callback.answer()


@router.message(AuthorApplyStates.waiting_bio, F.text)
async def author_bio(message: Message, state: FSMContext):
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    text = (message.text or "").strip()
    words = text.split()
    if len(words) > 30:
        await _edit_screen(
            message.bot,
            state,
            t("profile.author.apply.bio_too_long", lang) + "\n\n" + t("profile.author.apply.ask_bio", lang),
            build_back_kb(callback_data="author:cancel", text=t("profile.author.apply.cancel_btn", lang)),
        )
        await _safe_delete(message)
        return

    data = await state.get_data()
    works: list[str] = list(data.get("author_apply_photos") or [])
    channel = data.get("author_channel")

    await state.update_data(author_reason=text)
    ok = await submit_author_application(message=message, state=state, works=works, channel=channel, reason=text)
    # Clear screen text after submission
    if ok:
        await _edit_screen(
            message.bot,
            state,
            t("profile.author.apply.sent", lang),
            build_back_kb(callback_data="menu:profile", text=t("common.back", lang)),
        )
    await state.clear()
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_bio)
async def author_bio_other(message: Message):
    # ignore non-text
    pass


async def submit_author_application(*, message: Message, state: FSMContext, works: list[str], channel: str | None, reason: str) -> bool:
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    total = len(works)
    if total < 5 or total > 10:
        await _edit_screen(
            message.bot,
            state,
            t("profile.author.apply.need_more", lang, count=total),
            _author_apply_kb(lang),
        )
        return False

    target_chat_id = _author_target_chat_id()
    if target_chat_id is None:
        await _edit_screen(
            message.bot,
            state,
            t("profile.author.apply.no_chat", lang),
            build_back_kb(callback_data="menu:profile", text=t("common.back", lang)),
        )
        return False

    name = (user.get("name") or user.get("display_name") or "—") if user else "—"
    username = user.get("username") if user else None
    tg_id = message.from_user.id
    author_code = "—"
    try:
        author_code = await ensure_user_author_code(int(tg_id))
    except Exception:
        author_code = "—"

    summary_lines = [
        "🧑‍🎨 <b>Новая заявка на авторство</b>",
        f"Имя: {html.escape(str(name), quote=False)}",
        f"Username: @{username}" if username else "Username: —",
        f"TG ID: <code>{tg_id}</code>",
        f"Код автора: <code>{html.escape(str(author_code), quote=False)}</code>",
        f"Канал: {html.escape(channel or '—', quote=False)}",
        f"Работы: {total} шт. (файлы)",
        "",
        "Описание автора:",
        html.escape(reason or "—", quote=False),
    ]

    mod_kb = InlineKeyboardBuilder()
    mod_kb.button(text="✅ Принять", callback_data=f"authorreq:approve:{tg_id}")
    mod_kb.button(text="❌ Отклонить", callback_data=f"authorreq:decline:{tg_id}")
    mod_kb.button(text="ℹ️ Дослать данные", callback_data=f"authorreq:more:{tg_id}")
    mod_kb.adjust(2, 1)

    try:
        await message.bot.send_message(
            chat_id=target_chat_id,
            text="\n".join(summary_lines),
            parse_mode="HTML",
            reply_markup=mod_kb.as_markup(),
        )

        media = []
        for idx, fid in enumerate(works):
            if idx == 0:
                media.append(InputMediaDocument(media=fid, caption="Работы для подтверждения авторства"))
            else:
                media.append(InputMediaDocument(media=fid))
        await message.bot.send_media_group(chat_id=target_chat_id, media=media)
    except Exception:
        await _edit_screen(
            message.bot,
            state,
            "Не удалось отправить заявку, попробуй позже или напиши в поддержку.",
            build_back_kb(callback_data="menu:profile", text=t("common.back", lang)),
        )
        return False

    return True


@router.callback_query(F.data == "profile:author:submit")
async def profile_author_submit(callback: CallbackQuery, state: FSMContext):
    # Legacy submit path — redirect to current flow if reached
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await callback.answer(t("profile.author.apply.use_new_flow", lang), show_alert=True)


@router.callback_query(F.data.startswith("authorreq:"))
async def author_request_action(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, action, tg_id_raw = parts
    try:
        target_tg_id = int(tg_id_raw)
    except ValueError:
        await callback.answer()
        return

    # Only admins of author applications chat may act
    if not await _is_author_admin(callback.message.bot, callback.from_user.id):
        await callback.answer("Только админы авторского чата могут решать заявки.", show_alert=True)
        return

    status_map = {
        "approve": "✅ Принято",
        "decline": "❌ Отклонено",
        "more": "ℹ️ Запрошены данные",
    }
    status_text = status_map.get(action)
    if not status_text:
        await callback.answer()
        return

    # Update application message
    base_text = callback.message.html_text or callback.message.text or ""
    suffix = f"\n\nСтатус: {status_text} (by @{callback.from_user.username or callback.from_user.id})"
    try:
        await callback.message.edit_text(base_text + suffix, reply_markup=None, parse_mode="HTML")
    except Exception:
        pass

    # Notify applicant in DM
    notify_text = None
    if action == "approve":
        notify_text = (
            "✅ Твоя заявка на авторство принята!\n"
            "Для начала зайди в группу авторов:"
        )
        await set_user_author_status_by_tg_id(target_tg_id, True)
        join_kb = InlineKeyboardBuilder()
        join_kb.button(
            text=t("profile.author.apply.join_group", _get_lang(await get_user_by_tg_id(target_tg_id))),
            url=AUTHOR_GROUP_INVITE_LINK,
        )
        join_kb.button(
            text="✅ Готово",
            callback_data="author:join:done",
        )
        try:
            await callback.message.bot.send_message(
                chat_id=target_tg_id,
                text=notify_text,
                reply_markup=join_kb.as_markup(),
            )
        except Exception:
            pass
        notify_text = None  # already sent with button
    elif action == "decline":
        notify_text = (
            "❌ Заявка на авторство отклонена.\n"
            "Если хочешь уточнить причины — напиши в поддержку."
        )
    elif action == "more":
        kb = InlineKeyboardBuilder()
        kb.button(text=t("profile.author.apply.more_add_btn", _get_lang(await get_user_by_tg_id(target_tg_id))), callback_data="author:more:add")
        kb.button(text=t("profile.author.apply.cancel_btn", _get_lang(await get_user_by_tg_id(target_tg_id))), callback_data="author:cancel")
        try:
            await callback.message.bot.send_message(
                chat_id=target_tg_id,
                text=t("profile.author.apply.more_prompt", _get_lang(await get_user_by_tg_id(target_tg_id))),
                reply_markup=kb.as_markup(),
                parse_mode="HTML",
            )
        except Exception:
            pass
        await callback.answer("Запросили доп. данные")
        return

    if notify_text:
        try:
            await callback.message.bot.send_message(
                chat_id=target_tg_id,
                text=notify_text,
            )
        except Exception:
            pass

    await callback.answer("Обновлено")


@router.callback_query(F.data == "author:join:done")
async def author_join_done(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    # Only for author group (closed join requests). Bot must be admin with Invite Users.
    chat_id = request.chat.id
    user_id = request.from_user.id

    # Approve only verified authors
    try:
        is_author = await is_user_author_by_tg_id(int(user_id))
    except Exception:
        is_author = False

    if not is_author:
        # ignore silently
        return

    try:
        await request.approve()
    except Exception:
        return

    # After approve, send summary to group
    user = await get_user_by_tg_id(int(user_id)) or {}
    lang = _get_lang(user)

    name = (user.get("name") or user.get("display_name") or "—")
    chan = user.get("tg_channel_link") or "—"
    created_at = user.get("created_at")
    days = "—"
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at))
            days = str((datetime.now() - dt).days + 1)
        except Exception:
            days = "—"

    rating_line = "—"
    try:
        if user.get("id"):
            summary = await get_user_rating_summary(int(user["id"])) or {}
            bayes = summary.get("bayes_received")
            cnt = summary.get("ratings_received")
            if bayes is not None:
                rating_line = f"{float(bayes):.2f}★ / {cnt or 0} оценок"
    except Exception:
        rating_line = "—"

    info_lines = [
        "👤 Новый автор в группе:",
        f"Имя: {name}",
        f"Канал: {chan}",
        f"TG ID: {user_id}",
        f"В боте дней: {days}",
        f"Рейтинг автора: {rating_line}",
    ]
    try:
        await request.bot.send_message(chat_id=chat_id, text="\n".join(info_lines))
    except Exception:
        pass

@router.callback_query(F.data == "author:more:add")
async def author_more_add(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await state.set_state(AuthorApplyStates.waiting_more_files)
    await state.update_data(author_more_files=[], author_msg_id=callback.message.message_id, author_chat_id=callback.message.chat.id)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.more_done_btn", lang), callback_data="author:more:done")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)

    await _edit_screen(
        callback.message.bot,
        state,
        t("profile.author.apply.more_send_files", lang),
        kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "author:more:done")
async def author_more_done(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await state.clear()
    await _edit_screen(
        callback.message.bot,
        state,
        t("profile.author.apply.more_sent", lang),
        build_back_kb(callback_data="menu:profile", text=t("common.back", lang)),
    )
    await callback.answer()


@router.message(AuthorApplyStates.waiting_more_files, F.document)
async def author_more_collect(message: Message, state: FSMContext):
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    doc = message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        if await _should_warn_media_group(state, "warn_non_image_more", message.media_group_id):
            await _edit_screen(
                message.bot,
                state,
                t("profile.author.apply.more_invalid", lang),
                _author_apply_kb(lang),
            )
        await _safe_delete(message)
        return

    data = await state.get_data()
    more_files: list[str] = list(data.get("author_more_files") or [])
    if len(more_files) >= 10:
        await message.reply(t("profile.author.apply.too_many", lang), parse_mode="HTML")
        return

    fid = doc.file_id
    if fid not in more_files:
        more_files.append(fid)
        await state.update_data(author_more_files=more_files)

        # Forward to moderators immediately
        target_chat_id = _author_target_chat_id()
        if target_chat_id:
            caption = f"Доп. материалы по заявке автора @{user.get('username') or user.get('name') or user.get('id')} (tg_id={message.from_user.id})"
            try:
                await message.bot.send_document(
                    chat_id=target_chat_id,
                    document=fid,
                    caption=caption,
                )
            except Exception:
                pass

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.author.apply.more_done_btn", lang), callback_data="author:more:done")
    kb.button(text=t("profile.author.apply.cancel_btn", lang), callback_data="author:cancel")
    kb.adjust(1, 1)

    await _edit_screen(
        message.bot,
        state,
        t("profile.author.apply.more_count", lang, count=len(more_files)),
        kb.as_markup(),
    )
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_more_files, F.photo)
async def author_more_photo_warn(message: Message, state: FSMContext):
    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)
    if await _should_warn_media_group(state, "warn_photo_as_file_more", message.media_group_id):
        await _edit_screen(
            message.bot,
            state,
            "Отправь дополнительные работы как <b>файлы</b>, без сжатия. Используй скрепку → Файл.",
            _author_apply_kb(lang),
        )
    await _safe_delete(message)


@router.message(AuthorApplyStates.waiting_more_files)
async def author_more_other(message: Message):
    # ignore other types
    await _safe_delete(message)
