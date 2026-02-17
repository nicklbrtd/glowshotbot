from __future__ import annotations

from html import escape
from urllib.parse import quote

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler

from database import (
    get_user_by_tg_id,
    get_user_by_id,
    get_photo_by_id,
    get_active_photos_for_user,
    get_or_create_share_link_code,
    refresh_share_link_code,
    get_owner_tg_id_by_share_code,
    ensure_user_minimal_row,
    add_rating_by_tg_id,
    get_user_rating_value,
    get_link_ratings_count_for_photo,
    get_ratings_count_for_photo,
    is_user_premium_active,
    try_award_referral,
)
from utils.registration_guard import require_user_name
from handlers.upload import _tag_label, _device_emoji

router = Router()

_BOT_USERNAME: str | None = None

async def _get_bot_username(obj: Message | CallbackQuery) -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    bot = obj.bot if isinstance(obj, Message) else obj.message.bot
    me = await bot.get_me()
    _BOT_USERNAME = me.username or ""
    return _BOT_USERNAME

def _is_registered(u: dict | None) -> bool:
    # Minimal rows may exist (link rating). Registered = profile name is filled.
    if not u:
        return False
    return bool((u.get("name") or "").strip())

async def _get_share_counts(photo_id: int) -> tuple[int | None, int | None]:
    """Return (link_ratings_count, total_ratings_count). Uses best-effort DB helpers."""
    try:
        link_cnt = await get_link_ratings_count_for_photo(int(photo_id))
        total_cnt = await get_ratings_count_for_photo(int(photo_id))
        return int(link_cnt or 0), int(total_cnt or 0)
    except Exception:
        return None, None

def _rate_kb(
    *,
    owner_tg_id: int,
    code: str,
    photo_id: int,
    idx: int,
    rated_value: int | None,
    is_registered: bool,
    has_next_unrated: bool,
    is_rateable: bool,
    single_mode: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    flag = "1" if single_mode else "0"

    if is_rateable and rated_value is None:
        kb.row(
            *[
                InlineKeyboardButton(
                    text=str(i),
                    callback_data=f"lr:set:{owner_tg_id}:{photo_id}:{idx}:{i}:{code}:{flag}",
                    style="danger",
                )
                for i in range(1, 6)
            ]
        )
        kb.row(
            *[
                InlineKeyboardButton(
                    text=str(i),
                    callback_data=f"lr:set:{owner_tg_id}:{photo_id}:{idx}:{i}:{code}:{flag}",
                    style="success",
                )
                for i in range(6, 11)
            ]
        )
        return kb.as_markup()

    # Уже оценено или нельзя оценивать — даём кнопку дальше/регистрации/меню
    if has_next_unrated:
        kb.row(InlineKeyboardButton(text="➡️ Далее", callback_data=f"lr:next:{owner_tg_id}:{idx+1}:{code}"))
    else:
        if is_registered:
            kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"))
        else:
            kb.row(InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="auth:start"))

    return kb.as_markup()


def _fmt_pub_date(photo: dict) -> str:
    raw = (photo.get("created_at") or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime

        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        if "T" in raw:
            return raw.split("T", 1)[0]
        return raw

def _fmt_pub_date_short(photo: dict) -> str:
    raw = (photo.get("created_at") or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime

        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%d.%m")
    except Exception:
        if "T" in raw:
            return raw.split("T", 1)[0][:5]
        return raw[:5]


async def _get_active_photos_for_share(owner_tg_id: int) -> list[dict]:
    """Вернёт активные фото автора (до 2 шт.), отсортированные по дате создания (старше -> новее)."""
    owner = await get_user_by_tg_id(int(owner_tg_id))
    if not owner:
        return []
    premium_active = False
    try:
        premium_active = await is_user_premium_active(int(owner_tg_id))
    except Exception:
        premium_active = False
    try:
        photos = await get_active_photos_for_user(int(owner["id"]), limit=2)
        photos = sorted(photos, key=lambda p: (p.get("created_at") or ""))
    except Exception:
        photos = []
    photos = photos[:2]
    if premium_active:
        return photos
    # Без премиума — оставляем только одну (самую свежую из доступных)
    if not photos:
        return []
    return [photos[-1]]

async def _render_link_photo(
    target: Message | CallbackQuery,
    owner_tg_id: int,
    code: str,
    idx: int = 0,
    *,
    single_mode: bool = False,
):
    viewer_tg_id = target.from_user.id
    await ensure_user_minimal_row(viewer_tg_id, username=target.from_user.username)
    viewer_full = await get_user_by_tg_id(int(viewer_tg_id))
    is_reg = _is_registered(viewer_full)

    photos = await _get_active_photos_for_share(owner_tg_id)
    if not photos:
        txt = "❌ У автора сейчас нет активных фотографий."
        if isinstance(target, Message):
            await target.answer(txt, disable_notification=True)
        else:
            await target.answer(txt, show_alert=True)
        return

    viewer_user_id = viewer_full.get("id") if viewer_full else None

    # Найдём первую неоценённую пользователем фотографию (если есть)
    unrated_idx = None
    ratings_cache: list[int | None] = []
    for i, ph in enumerate(photos):
        rv = None
        if viewer_user_id:
            rv = await get_user_rating_value(int(ph["id"]), int(viewer_user_id))
        ratings_cache.append(rv)
        if rv is None and unrated_idx is None:
            unrated_idx = i

    if not single_mode and unrated_idx is None:
        # Все фото оценены — показываем финальный экран
        done_text = (
            "🔗⭐️ Все доступные фото автора уже оценены.\n\n"
            "Спасибо за оценки!"
        )
        kb = InlineKeyboardBuilder()
        if is_reg:
            kb.button(text="🏠 В меню", callback_data="menu:back")
        else:
            kb.button(text="📝 Зарегистрироваться", callback_data="auth:start")
        kb.adjust(1)

        # удаляем старое сообщение (если было) и шлём новое
        try:
            if isinstance(target, CallbackQuery):
                await target.message.delete()
            else:
                pass
        except Exception:
            pass
        if isinstance(target, CallbackQuery):
            await target.message.bot.send_message(
                chat_id=target.message.chat.id,
                text=done_text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
                parse_mode="HTML",
            )
        else:
            await target.bot.send_message(
                chat_id=target.chat.id,
                text=done_text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
                parse_mode="HTML",
            )
        return

    if single_mode:
        idx = max(0, min(idx, len(photos) - 1))
    else:
        idx = unrated_idx if idx >= len(photos) else idx
        idx = max(0, min(idx, len(photos) - 1))
    photo = photos[idx]
    rated_value = ratings_cache[idx]
    has_next_unrated = False if single_mode else any(rv is None for j, rv in enumerate(ratings_cache) if j != idx)

    owner_user = await get_user_by_id(int(photo["user_id"]))
    owner_name = (owner_user or {}).get("name") or ""

    title = (photo.get("title") or "Фотография").strip()
    pub = _fmt_pub_date(photo)
    pub_inline = f"  <i>{pub}</i>" if pub else ""

    is_rateable = bool(photo.get("ratings_enabled", True))
    if not is_rateable:
        title_line = f"<b>\"{title}\"</b>{pub_inline}"
        author_line = (f"Автор: {owner_name}\n" if owner_name else "Автор: —\n")
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"{title_line}\n"
            f"{author_line}"
            "\n🚫 Эта фотография недоступна для оценок.\n"
        )
    elif rated_value is None:
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"<b>\"{title}\"</b>{pub_inline}\n"
            + (f"Автор: {owner_name}\n" if owner_name else "Автор: —\n")
            + "\nПоставь оценку от 1 до 10 👇\n"
        )
    else:
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"<b>\"{title}\"</b>{pub_inline}\n"
            + (f"Автор: {owner_name}\n" if owner_name else "Автор: —\n")
            + f"\n<b>Твоя оценка:</b> {rated_value}"
        )

    kb = _rate_kb(
        owner_tg_id=owner_tg_id,
        code=code,
        photo_id=int(photo["id"]),
        idx=idx,
        rated_value=rated_value,
        is_registered=is_reg,
        has_next_unrated=has_next_unrated,
        is_rateable=is_rateable,
        single_mode=single_mode,
    )

    # Всегда показываем с фотографией. Удаляем старое сообщение, чтобы избежать багов с media.
    if isinstance(target, CallbackQuery):
        try:
            await target.message.delete()
        except Exception:
            pass
        await target.message.bot.send_photo(
            chat_id=target.message.chat.id,
            photo=photo["file_id"],
            caption=text,
            reply_markup=kb,
            disable_notification=True,
            parse_mode="HTML",
        )
    else:
        await target.bot.send_photo(
            chat_id=target.chat.id,
            photo=photo["file_id"],
            caption=text,
            reply_markup=kb,
            disable_notification=True,
            parse_mode="HTML",
        )

async def _load_photo_with_access(callback: CallbackQuery, photo_id: int) -> tuple[dict | None, dict | None]:
    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена", show_alert=True)
        return None, None

    owner_user = await get_user_by_id(int(photo["user_id"]))
    if not owner_user or int(owner_user.get("tg_id") or 0) != int(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return None, None

    return photo, owner_user

async def _build_share_links(
    bot_username: str, code: str, owner_tg_id: int, photo_id: int
) -> tuple[str, str, int, int]:
    photos = await _get_active_photos_for_share(owner_tg_id)
    link_pack = f"https://t.me/{bot_username}?start=rate_{code}"

    idx = 0
    for i, ph in enumerate(photos):
        if int(ph.get("id") or 0) == int(photo_id):
            idx = i
            break

    link_one = f"{link_pack}_p{idx + 1}"
    return link_pack, link_one, idx, len(photos)

def _build_share_text_links(
    photo: dict,
    link_one: str,
    link_cnt: int | None,
    total_cnt: int | None,
    photos_count: int,
) -> str:
    title = escape((photo.get("title") or "Фотография").strip())
    device_raw = (photo.get("device") or "").strip()
    device_emoji = _device_emoji(device_raw) or ""
    device = escape(device_raw + (f" {device_emoji}" if device_emoji and device_raw else device_emoji))
    raw_tag = (photo.get("tag") or "").strip()
    tag_label = _tag_label(raw_tag)
    tag = escape(tag_label)

    lines = [
        "🔗✨ Поделиться фотографией",
        "",
        "📸 <b>Эта фотография:</b>",
        f"<i>\"{title}\"</i>" + (f" ({device})" if device else ""),
    ]
    if tag_label:
        lines.append(f"Тег: {tag}")

    if photos_count <= 1:
        lines.extend(
            [
                "",
                "Поделиться ссылкой на эту фотографию можно по кнопке «Отправить ссылку (1)».",
                f"<code>{link_one}</code>",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Ссылка на эту фотографию:",
                f"<code>{link_one}</code>",
                "",
                'Также можно поделиться двумя фотографиями сразу по кнопке «Отправить ссылку (2)».',
            ]
        )

    lines.extend(
        [
            "",
            f"🔗⭐️ Оценки по ссылке: <b>{link_cnt}</b>" if link_cnt is not None else "⛓️‍💥⭐️ Оценки по ссылке: —",
            f"⭐️ Всего оценок: <b>{total_cnt}</b>" if total_cnt is not None else "⭐️ Всего оценок: —",
        ]
    )
    return "\n".join(lines)

def _build_share_text_tgk(photo: dict, link_one: str, owner_name: str | None) -> str:
    title = escape((photo.get("title") or "Фотография").strip())
    device_raw = (photo.get("device") or "").strip()
    device_emoji = _device_emoji(device_raw) or ""
    device = escape(device_raw + (f" {device_emoji}" if device_emoji and device_raw else device_emoji))
    raw_tag = (photo.get("tag") or "").strip()
    tag_label = _tag_label(raw_tag)
    tag = escape(tag_label)
    pub_short = _fmt_pub_date_short(photo)

    lines = [
        "<b>Моя фотография есть в GlowShot!</b>",
    ]
    title_line = f"<code>\"{title}\"</code>" + (f" ({device})" if device else "")
    if pub_short:
        title_line += f" — {pub_short}"
    lines.append(title_line)
    if tag_label:
        lines.append(f"Тег: {tag}")
    if owner_name:
        lines.append(f"Автор: {escape(owner_name)}")

    lines.extend(
        [
            "",
            "<b>Вы можете анонимно оценить эту фотографию по ссылке:</b>",
            f"<blockquote>{link_one}</blockquote>",
        ]
    )
    return "\n".join(lines)

def _share_links_kb(photo_id: int, link_one: str, link_pack: str, photos_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📣 Поделиться постом", callback_data=f"myphoto:share_tgk:{photo_id}"))
    if photos_count >= 2:
        kb.row(InlineKeyboardButton(text="📤 Отправить ссылку (2)", url=f"https://t.me/share/url?url={quote(link_pack)}"))
    else:
        kb.row(InlineKeyboardButton(text="📤 Отправить ссылку (1)", url=f"https://t.me/share/url?url={quote(link_one)}"))
    kb.row(InlineKeyboardButton(text="♻️ Обновить ссылки", callback_data=f"myphoto:share_refresh:a:{photo_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"))
    return kb.as_markup()

def _share_tgk_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="👀 Предпросмотр", callback_data=f"myphoto:share_preview:{photo_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:share_backlinks:{photo_id}"))
    return kb.as_markup()

def _preview_static_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        *[
            InlineKeyboardButton(text=str(i), callback_data="myphoto:share_preview_noop", style="danger")
            for i in range(1, 6)
        ]
    )
    kb.row(
        *[
            InlineKeyboardButton(text=str(i), callback_data="myphoto:share_preview_noop", style="success")
            for i in range(6, 11)
        ]
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад (видно только вам)", callback_data=f"myphoto:share_preview_back:{photo_id}"))
    return kb.as_markup()

async def _edit_share_message(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

async def _render_share_screen(
    callback: CallbackQuery,
    photo: dict,
    code: str,
    mode: str = "a",
):
    bot_username = await _get_bot_username(callback)
    link_pack, link_one, _, photos_count = await _build_share_links(
        bot_username, code, int(callback.from_user.id), int(photo["id"])
    )
    link_cnt, total_cnt = await _get_share_counts(int(photo["id"]))
    owner_user = await get_user_by_id(int(photo["user_id"]))
    owner_name = (owner_user or {}).get("name") or ""

    if mode == "b":
        text = _build_share_text_tgk(photo, link_one, owner_name)
        kb = _share_tgk_kb(int(photo["id"]))
    else:
        text = _build_share_text_links(photo, link_one, link_cnt, total_cnt, photos_count)
        kb = _share_links_kb(int(photo["id"]), link_one, link_pack, photos_count)

    await _edit_share_message(callback, text, kb)

async def _render_share_preview(callback: CallbackQuery, photo: dict, code: str):
    bot_username = await _get_bot_username(callback)
    _, _, idx, _ = await _build_share_links(
        bot_username, code, int(callback.from_user.id), int(photo["id"])
    )

    owner_user = await get_user_by_id(int(photo["user_id"]))
    owner_name = (owner_user or {}).get("name") or (owner_user or {}).get("username") or ""

    title = (photo.get("title") or "Фотография").strip()
    pub = _fmt_pub_date(photo)
    pub_inline = f"  <i>{pub}</i>" if pub else ""

    is_rateable = bool(photo.get("ratings_enabled", True))
    if not is_rateable:
        title_line = f"<b>\"{title}\"</b>{pub_inline}"
        author_line = (f"Автор: {owner_name}\n" if owner_name else "Автор: —\n")
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"{title_line}\n"
            f"{author_line}"
            "\n🚫 Эта фотография недоступна для оценок.\n"
        )
    else:
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"<b>\"{title}\"</b>{pub_inline}\n"
            + (f"Автор: {owner_name}\n" if owner_name else "Автор: —\n")
            + "\nПоставь оценку от 1 до 10 👇\n"
        )

    kb = _preview_static_kb(int(photo["id"]))

    await _edit_share_message(callback, text, kb)

@router.callback_query(F.data.startswith("myphoto:share:"))
async def myphoto_share(callback: CallbackQuery):
    photo_id = int(callback.data.split(":")[2])
    photo, _ = await _load_photo_with_access(callback, photo_id)
    if not photo:
        return

    code = await get_or_create_share_link_code(int(callback.from_user.id))
    await _render_share_screen(callback, photo, code, mode="a")

    await callback.answer()

@router.callback_query(F.data.startswith("myphoto:share_tgk:"))
async def myphoto_share_tgk(callback: CallbackQuery):
    photo_id = int(callback.data.split(":")[2])
    photo, _ = await _load_photo_with_access(callback, photo_id)
    if not photo:
        return

    code = await get_or_create_share_link_code(int(callback.from_user.id))
    await _render_share_screen(callback, photo, code, mode="b")

    await callback.answer()

@router.callback_query(F.data.startswith("myphoto:share_preview:"))
async def myphoto_share_preview(callback: CallbackQuery):
    photo_id = int(callback.data.split(":")[2])
    photo, _ = await _load_photo_with_access(callback, photo_id)
    if not photo:
        return

    code = await get_or_create_share_link_code(int(callback.from_user.id))
    await _render_share_preview(callback, photo, code)

    await callback.answer()

@router.callback_query(F.data == "myphoto:share_preview_noop")
async def myphoto_share_preview_noop(callback: CallbackQuery):
    await callback.answer("Это предпросмотр, оценки не сохраняются.")

@router.callback_query(F.data.startswith("myphoto:share_preview_back:"))
async def myphoto_share_preview_back(callback: CallbackQuery):
    photo_id = int(callback.data.split(":")[2])
    photo, _ = await _load_photo_with_access(callback, photo_id)
    if not photo:
        return

    code = await get_or_create_share_link_code(int(callback.from_user.id))
    await _render_share_screen(callback, photo, code, mode="b")

    await callback.answer()

@router.callback_query(F.data.startswith("myphoto:share_backlinks:"))
async def myphoto_share_backlinks(callback: CallbackQuery):
    photo_id = int(callback.data.split(":")[2])
    photo, _ = await _load_photo_with_access(callback, photo_id)
    if not photo:
        return

    code = await get_or_create_share_link_code(int(callback.from_user.id))
    await _render_share_screen(callback, photo, code, mode="a")

    await callback.answer()

@router.callback_query(F.data.startswith("myphoto:share_refresh:"))
async def myphoto_share_refresh(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    mode = "a"
    photo_id_part = None
    if len(parts) >= 4:
        mode = parts[2]
        photo_id_part = parts[3]
    elif len(parts) >= 3:
        photo_id_part = parts[2]
    try:
        photo_id = int(photo_id_part or 0)
    except ValueError:
        await callback.answer("Фотография не найдена", show_alert=True)
        return
    mode = mode if mode in ("a", "b") else "a"

    photo, _ = await _load_photo_with_access(callback, photo_id)
    if not photo:
        return

    code = await refresh_share_link_code(int(callback.from_user.id))
    await _render_share_screen(callback, photo, code, mode=mode)

    await callback.answer("Ссылки обновлены")

@router.message(CommandStart())
async def start_rate_link(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args.startswith("rate_"):
        raise SkipHandler

    payload = args.replace("rate_", "", 1).strip()
    photo_idx = None
    if "_p" in payload:
        base, suffix = payload.rsplit("_p", 1)
        if suffix in ("1", "2") and base:
            payload = base
            photo_idx = int(suffix) - 1

    code = payload
    owner_tg_id = await get_owner_tg_id_by_share_code(code)
    if not owner_tg_id:
        await message.answer("❌ Эта ссылка не активна или устарела.", disable_notification=True)
        return

    await _render_link_photo(
        message,
        int(owner_tg_id),
        code,
        idx=photo_idx or 0,
        single_mode=photo_idx is not None,
    )
    # Удаляем служебное /start, чтобы в чате осталось только фото для оценки
    try:
        await message.delete()
    except Exception:
        pass

@router.callback_query(F.data.startswith("lr:set:"))
async def lr_set(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    # lr:set:<owner_tg_id>:<photo_id>:<idx>:<value>:<code>[:single_flag]
    _, _, owner_tg_id_s, photo_id_s, idx_s, value_s, code = parts[:7]
    single_mode = False
    if len(parts) >= 8:
        single_mode = parts[7] == "1"
    owner_tg_id = int(owner_tg_id_s)
    photo_id = int(photo_id_s)
    idx = int(idx_s)
    value = int(value_s)

    owner_user = await get_user_by_tg_id(owner_tg_id)
    photo = await get_photo_by_id(photo_id)
    if (
        not photo
        or photo.get("is_deleted")
        or str(photo.get("moderation_status") or "").lower() not in ("active", "good")
    ):
        await callback.answer("❌ Фото недоступно.", show_alert=True)
        return
    if owner_user and int(photo.get("user_id") or 0) != int(owner_user.get("id") or 0):
        await callback.answer("❌ Фото недоступно.", show_alert=True)
        return

    if not bool(photo.get("ratings_enabled", True)):
        await callback.answer("Автор отключил оценки для этого фото.", show_alert=True)
        return

    owner_user = await get_user_by_id(int(photo["user_id"]))
    if owner_user and int(owner_user.get("tg_id") or 0) == int(callback.from_user.id):
        await callback.answer("Нельзя оценивать свою фотографию.", show_alert=True)
        return

    await ensure_user_minimal_row(int(callback.from_user.id), username=callback.from_user.username)

    ok = await add_rating_by_tg_id(
        photo_id=int(photo["id"]),
        rater_tg_id=int(callback.from_user.id),
        value=value,
        source="link",
        source_code=code,
    )

    await callback.answer("✅ Оценка учтена!" if ok else "Ты уже оценивал(а) этот кадр.", show_alert=not ok)

    if not ok:
        return

    # Referral award: only once, only when user already satisfies registration/vote conditions.
    try:
        rewarded, referrer_tg_id, referee_tg_id = await try_award_referral(int(callback.from_user.id))
    except Exception:
        rewarded = False
        referrer_tg_id = None
        referee_tg_id = None

    if rewarded:
        if referrer_tg_id:
            try:
                await callback.message.bot.send_message(
                    chat_id=referrer_tg_id,
                    text="🎉 Твой друг активировал рефералку: +2 credits и 3 часа Premium",
                    disable_notification=True,
                )
            except Exception:
                pass
        if referee_tg_id:
            try:
                await callback.message.bot.send_message(
                    chat_id=referee_tg_id,
                    text="🎁 Бонус за приглашение получен: +2 credits и 3 часа Premium",
                    disable_notification=True,
                )
            except Exception:
                pass

    # После успешной оценки:
    # - если это пакетная ссылка (несколько фото), сразу показываем следующую неоценённую;
    # - если это одиночная ссылка, показываем итог с предложением регистрации/меню.
    try:
        await callback.message.delete()
    except Exception:
        pass

    if not single_mode:
        # Переходим к следующей неоценённой (или к финальному экрану, если все оценены)
        await _render_link_photo(
            callback,
            int(owner_tg_id),
            code,
            idx=999,  # гарантированно попадём на первую неоценённую
            single_mode=False,
        )
        return

    owner_name = (owner_user or {}).get("name") or ""
    title = (photo.get("title") or "Фотография").strip()
    pub = _fmt_pub_date(photo)
    pub_inline = f"  <i>{pub}</i>" if pub else ""

    result_text = (
        "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
        f"<b>\"{title}\"</b>{pub_inline}\n"
        + (f"Автор: {owner_name}\n" if owner_name else "Автор: —\n")
        + f"\n<b>Твоя оценка:</b> {value}"
    )

    viewer_full = await get_user_by_tg_id(int(callback.from_user.id))
    is_reg = _is_registered(viewer_full)
    kb = InlineKeyboardBuilder()
    if is_reg:
        kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"))
    else:
        kb.row(InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="auth:start"))

    await callback.message.bot.send_message(
        chat_id=callback.message.chat.id,
        text=result_text,
        reply_markup=kb.as_markup(),
        disable_notification=True,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("lr:next:"))
async def lr_next(callback: CallbackQuery):
    _, _, owner_tg_id_s, idx_s, code = (callback.data or "").split(":", 4)
    owner_tg_id = int(owner_tg_id_s)
    idx = int(idx_s)
    await _render_link_photo(callback, owner_tg_id, code, idx=idx)

@router.callback_query(F.data == "lr:done")
async def lr_done(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
